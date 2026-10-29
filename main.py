"""One process/event loop: Telegram long polling + monitor + FastAPI health/Mini App."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import contextmanager

import uvicorn
from telegram import MenuButtonDefault, MenuButtonWebApp, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import AIORateLimiter, Application, CallbackQueryHandler, CommandHandler
from telegram.request import HTTPXRequest

from bot_handlers import (
    BOT_COMMANDS,
    alerts_cmd,
    button_callback,
    error_handler,
    help_cmd,
    pause_cmd,
    resume_cmd,
    settings_callback,
    start,
    test_alert,
)
from bybit_client import BybitClient
from config import Config, ConfigError
from db import Database
from monitor import Monitor
from web_app import create_app

logger = logging.getLogger(__name__)


class EmbeddedServer(uvicorn.Server):
    """The owner of the event loop handles signals for every subsystem, not just HTTP."""

    @contextmanager
    def capture_signals(self):
        yield


def configure_logging(config: Config):
    logging.basicConfig(level=config.log_level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def build_application(config: Config, db: Database, monitor: Monitor) -> Application:
    request = HTTPXRequest(connection_pool_size=16, connect_timeout=10, read_timeout=35, write_timeout=35)
    updates_request = HTTPXRequest(connection_pool_size=2, connect_timeout=10, read_timeout=20, write_timeout=20)
    application = (
        Application.builder()
        .token(config.bot_token)
        .request(request)
        .get_updates_request(updates_request)
        .rate_limiter(AIORateLimiter(max_retries=1))
        .build()
    )
    application.bot_data.update(config=config, db=db, monitor=monitor)
    for name, callback in (
        ("start", start),
        ("help", help_cmd),
        ("settings", settings_callback),
        ("alerts", alerts_cmd),
        ("pause", pause_cmd),
        ("resume", resume_cmd),
        ("test", test_alert),
    ):
        application.add_handler(CommandHandler(name, callback))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_error_handler(error_handler)
    return application


async def run(config: Config, stop_event: asyncio.Event | None = None):
    """Run until SIGTERM/SIGINT, an injected stop event, or a fatal worker failure."""
    config.require_bot_token()
    db = Database(config.db_path)
    client = BybitClient(config.bybit_rest, config.request_timeout_sec, config.max_concurrent_requests)
    application = None
    monitor = None
    server = None
    server_task = None
    waits = []
    installed_signals = []
    stop = stop_event or asyncio.Event()
    if stop_event is None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
                installed_signals.append(sig)
            except NotImplementedError:
                pass  # Platform without Unix loop signal handlers; Ctrl+C still cancels asyncio.run.
    try:
        await db.init()
        monitor = Monitor(None, client, db, config)
        application = build_application(config, db, monitor)
        monitor.bot = application.bot
        server = EmbeddedServer(
            uvicorn.Config(
                create_app(config, db, monitor, application),
                host="0.0.0.0",
                port=config.port,
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=5,
            )
        )
        # One signal owner stops Telegram, workers, HTTP and SQLite in order.
        server_task = asyncio.create_task(server.serve(), name="http-server")
        await application.initialize()
        await application.start()
        await application.updater.start_polling(
            drop_pending_updates=config.drop_pending_updates,
            allowed_updates=["message", "callback_query"],
            bootstrap_retries=3,
            error_callback=lambda error: logger.warning("Telegram polling error (%s)", type(error).__name__),
        )
        await monitor.start()
        try:
            await application.bot.set_my_commands(BOT_COMMANDS)
            menu = (
                MenuButtonWebApp("Монитор", WebAppInfo(config.web_app_url))
                if config.web_app_url
                else MenuButtonDefault()
            )
            await application.bot.set_chat_menu_button(menu_button=menu)
        except TelegramError:
            logger.warning("Could not update bot commands/menu; polling remains active")
        logger.info("Started: polling + monitor + HTTP on port %s", config.port)
        waits = [
            asyncio.create_task(stop.wait(), name="stop-signal"),
            asyncio.create_task(monitor.fatal.wait(), name="monitor-fatal"),
        ]
        done, _ = await asyncio.wait([*waits, server_task], return_when=asyncio.FIRST_COMPLETED)
        if server_task in done:
            await server_task
            if not server.started:
                raise RuntimeError("HTTP server failed to start")
            if not stop.is_set():
                raise RuntimeError("HTTP server exited unexpectedly")
        if waits[1] in done:
            raise RuntimeError("A supervised monitor worker failed; restarting the process is required")
    finally:
        for sig in installed_signals:
            asyncio.get_running_loop().remove_signal_handler(sig)
        for task in waits:
            task.cancel()
        if waits:
            await asyncio.gather(*waits, return_exceptions=True)
        if application and application.updater and application.updater.running:
            try:
                await asyncio.wait_for(application.updater.stop(), timeout=5)
            except Exception:
                logger.exception("Telegram polling shutdown failed")
        if monitor and monitor.running:
            try:
                await monitor.stop(grace=5)
            except Exception:
                logger.exception("Monitor shutdown failed; continuing resource cleanup")
        if server:
            server.should_exit = True
        if server_task:
            try:
                await asyncio.wait_for(server_task, timeout=7)
            except TimeoutError:
                server_task.cancel()
                await asyncio.gather(server_task, return_exceptions=True)
            except Exception:
                logger.exception("HTTP shutdown failed")
        if application:
            try:
                if application.running:
                    await asyncio.wait_for(application.stop(), timeout=5)
            finally:
                try:
                    if application.initialized:
                        await application.shutdown()
                except Exception:
                    logger.exception("Telegram application shutdown failed")
        try:
            await client.close()
        finally:
            await db.close()
        logger.info("Shutdown complete")


def main():
    try:
        config = Config.from_env()
        configure_logging(config)
        asyncio.run(run(config))
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
