"""Supervised polling, rolling detection and bounded, cooldown-protected delivery."""

from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter, TelegramError

from bybit_client import BybitClient
from chart_builder import build_snapshot
from config import Config
from db import Database
from detector import rolling_change
from snapshots import cleanup_snapshots, snapshot_path

logger = logging.getLogger(__name__)
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,31}\Z")


def utc_now() -> datetime:
    return datetime.now(UTC)


def market_links(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📊 TradingView", url=f"https://www.tradingview.com/chart/?symbol=BYBIT%3A{symbol}.P"
                ),
                InlineKeyboardButton("⚡ Bybit", url=f"https://www.bybit.com/trade/usdt/{symbol}"),
            ]
        ]
    )


def alert_caption(
    symbol: str,
    direction: str,
    change: float,
    price: float,
    window: float,
    timeframe: str,
    turnover: float,
    number: int = 1,
    test: bool = False,
) -> str:
    icon = "🧪" if test else ("🟢" if direction == "PUMP" else "🔴")
    label = "TEST · NOT A SIGNAL" if test else direction
    return (
        f"{icon} <b>{label} {html.escape(symbol)}</b>\n\n"
        f"📈 <b>{change:+.2f}%</b> за {window / 60:g} мин\n"
        f"💰 Цена: <code>{price:.8g}</code> USDT\n"
        f"📊 Оборот 24ч: <code>{turnover:,.0f}</code> USDT\n"
        f"🕯 Свечи: <code>{html.escape(timeframe)}{'m' if timeframe != 'D' else ''}</code> · "
        f"{utc_now():%H:%M:%S} UTC\n"
        f"Сигнал #{number} за час · окно не равно ТФ свечей\n"
        "<i>Информационный сигнал, не инвестиционная рекомендация.</i>"
    )


@dataclass
class AlertJob:
    chat_id: int
    symbol: str
    direction: str
    change: float
    price: float
    window: float
    turnover: float
    claimed_at: float
    created_at: float = field(default_factory=time.monotonic)
    delivered: bool = False


class Monitor:
    def __init__(self, bot, client: BybitClient, db: Database, config: Config):
        self.bot, self.client, self.db, self.config = bot, client, db, config
        self.symbols: list[str] = []
        self._symbol_set: set[str] = set()
        self.turnover: dict[str, float] = {}
        self.price_history: dict[str, deque] = {}
        self.running = False
        self.fatal = asyncio.Event()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._queue: asyncio.Queue[AlertJob] = asyncio.Queue(maxsize=config.alert_queue_size)
        self._pending: set[tuple[int, str, str]] = set()
        self._render_semaphore = asyncio.Semaphore(1)
        self._send_lock = asyncio.Lock()
        self._next_send: dict[int, float] = {}
        self._retry_after: dict[int, float] = {}
        self.start_time = utc_now()
        self.last_update_time = None
        self.last_poll_time = None
        self.cycles_count = 0
        self.sent_count = 0
        self.failed_count = 0
        self.dropped_count = 0
        self.last_error = ""
        self.recent_activity = deque(maxlen=8)

    @property
    def ready(self) -> bool:
        return bool(
            self.running
            and self.symbols
            and self.last_poll_time
            and (utc_now() - self.last_poll_time).total_seconds() <= max(15, self.config.poll_interval_sec * 4)
        )

    def status(self) -> dict:
        return {
            "ready": self.ready,
            "running": self.running,
            "pairs": len(self.symbols),
            "max_pairs": self.config.max_symbols,
            "window_sec": self.config.alert_window_sec,
            "poll_interval_sec": self.config.poll_interval_sec,
            "last_poll": self.last_poll_time.isoformat() if self.last_poll_time else None,
            "last_registry_update": self.last_update_time.isoformat() if self.last_update_time else None,
            "uptime_sec": int((utc_now() - self.start_time).total_seconds()),
            "cycles": self.cycles_count,
            "queued_alerts": self._queue.qsize(),
            "sent_alerts": self.sent_count,
            "failed_alerts": self.failed_count,
            "dropped_alerts": self.dropped_count,
            "last_error": self.last_error,
        }

    async def start(self):
        if self.running:
            return
        await self.client.start()
        self.running = True
        self._stop.clear()
        self.fatal.clear()
        for name, coroutine in (
            ("registry", self._registry_loop()),
            ("poll", self._poll_loop()),
            ("cleanup", self._cleanup_loop()),
        ):
            self._spawn(name, coroutine)
        for index in range(self.config.alert_workers):
            self._spawn(f"alert-{index}", self._alert_worker())

    def _spawn(self, name: str, coroutine):
        task = asyncio.create_task(self._supervise(coroutine), name=f"monitor-{name}")
        self._tasks.append(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task):
        if not task.cancelled() and task.exception() is not None:
            logger.error("Background task %s failed: %s", task.get_name(), type(task.exception()).__name__)
            self.fatal.set()

    async def _supervise(self, coroutine):
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"Internal task error: {type(exc).__name__}"
            logger.exception("Unexpected monitor task failure")
            raise

    async def _sleep(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return False
        except TimeoutError:
            return True

    async def stop(self, grace: float = 8):
        self._stop.set()
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=grace)
            for task in pending:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        # Anything still queued was not delivered: release its cooldown reservation.
        while not self._queue.empty():
            job = self._queue.get_nowait()
            await self.db.release_alert(job.chat_id, job.symbol, job.direction, job.claimed_at)
            self._pending.discard((job.chat_id, job.symbol, job.direction))
            self._queue.task_done()
        self.running = False
        await self.client.close()

    async def _registry_loop(self):
        failures = 0
        while not self._stop.is_set():
            try:
                await self._update_symbols()
                failures = 0
                delay = self.config.pairs_update_sec
            except Exception as exc:
                failures += 1
                self.last_error = f"Registry unavailable: {type(exc).__name__}"
                logger.warning("Registry update failed (%s)", type(exc).__name__)
                delay = min(60, 2 ** min(failures, 6))
            if not await self._sleep(delay):
                break

    async def _update_symbols(self):
        instruments, tickers = await asyncio.gather(self.client.get_linear_symbols(), self.client.get_tickers())
        volumes = {}
        for ticker in tickers:
            try:
                symbol, volume = ticker["symbol"], float(ticker.get("turnover24h", 0))
                if SYMBOL.fullmatch(symbol) and math.isfinite(volume) and volume >= 0:
                    volumes[symbol] = volume
            except (KeyError, TypeError, ValueError):
                continue
        eligible = [
            row["symbol"]
            for row in instruments
            if row.get("status") == "Trading"
            and row.get("contractType") == "LinearPerpetual"
            and row.get("quoteCoin") == "USDT"
            and isinstance(row.get("symbol"), str)
            and SYMBOL.fullmatch(row["symbol"])
            and volumes.get(row["symbol"], 0) >= self.config.min_universe_volume_usd
        ]
        eligible = sorted(set(eligible), key=lambda symbol: (-volumes[symbol], symbol))[: self.config.max_symbols]
        if not eligible:
            raise ValueError("No eligible perpetual contracts")
        self.symbols, self._symbol_set = eligible, set(eligible)
        self.turnover = {symbol: volumes[symbol] for symbol in eligible}
        self.price_history = {
            symbol: history for symbol, history in self.price_history.items() if symbol in self._symbol_set
        }
        self.last_update_time = utc_now()
        logger.info("Registry updated: %s USDT perpetual contracts", len(self.symbols))

    async def _poll_loop(self):
        failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                if self.symbols:
                    tickers = await self.client.get_tickers()
                    await self._process_tickers(tickers)
                    self.last_error = ""
                    failures = 0
            except Exception as exc:
                failures += 1
                self.last_error = f"Market/alert processing error: {type(exc).__name__}"
                logger.warning("Poll failed (%s); history gaps will suppress signals", type(exc).__name__)
            delay = min(30, 2 ** min(failures, 5)) if failures else self.config.poll_interval_sec
            if not await self._sleep(max(0.05, delay - (time.monotonic() - started))):
                break

    async def _process_tickers(self, tickers: list[dict]):
        now = time.time()
        settings = await self.db.active_settings()
        for ticker in tickers:
            symbol = ticker.get("symbol")
            if not isinstance(symbol, str) or symbol not in self._symbol_set:
                continue
            try:
                price, turnover = float(ticker.get("lastPrice", 0)), float(ticker.get("turnover24h", 0))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(price) or price <= 0 or not math.isfinite(turnover) or turnover < 0:
                continue
            self.turnover[symbol] = turnover
            history = self.price_history.setdefault(symbol, deque(maxlen=self.config.history_size))
            if history and now <= history[-1][0]:
                continue
            history.append((now, price))
            cutoff = now - self.config.alert_window_sec - self.config.poll_tolerance_sec * 2
            while history and history[0][0] < cutoff:
                history.popleft()
            result = rolling_change(
                history,
                now,
                self.config.alert_window_sec,
                self.config.poll_tolerance_sec,
                self.config.poll_tolerance_sec,
            )
            if result is None:
                continue
            change, window = result
            if abs(change) >= 1:
                entry = f"{utc_now():%H:%M} | {symbol} | {change:+.2f}% / {window / 60:g}m"
                self.recent_activity = deque(
                    (item for item in self.recent_activity if f"| {symbol} |" not in item), maxlen=8
                )
                self.recent_activity.append(entry)
            direction = "PUMP" if change > 0 else "DUMP"
            for row in settings:
                if row["direction"] not in ("both", direction.lower()) or abs(change) < row["pump_threshold"]:
                    continue
                if turnover < row["volume_min_usd"]:
                    continue
                await self._enqueue(AlertJob(row["chat_id"], symbol, direction, change, price, window, turnover, now))
        self.last_poll_time, self.cycles_count = utc_now(), self.cycles_count + 1

    async def _enqueue(self, job: AlertJob):
        key = (job.chat_id, job.symbol, job.direction)
        if key in self._pending or self._retry_after.get(job.chat_id, 0) > time.monotonic():
            return
        if self._queue.full():
            self.dropped_count += 1
            logger.warning("Alert queue full; dropping %s %s", job.symbol, job.direction)
            return
        if not await self.db.reserve_alert(*key, job.claimed_at, self.config.alert_cooldown_sec):
            return
        self._pending.add(key)
        self._queue.put_nowait(job)

    async def _alert_worker(self):
        while not self._stop.is_set() or not self._queue.empty():
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            try:
                if time.monotonic() - job.created_at > max(60, self.config.poll_interval_sec * 5):
                    logger.warning("Dropping stale queued alert for %s", job.symbol)
                    self.dropped_count += 1
                    continue
                await self._deliver(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failed_count += 1
                self.last_error = f"Alert delivery failed: {type(exc).__name__}"
                logger.warning("Alert delivery failed (%s)", type(exc).__name__)
                self._retry_after[job.chat_id] = time.monotonic() + 30
            finally:
                if not job.delivered:
                    await self.db.release_alert(job.chat_id, job.symbol, job.direction, job.claimed_at)
                self._pending.discard((job.chat_id, job.symbol, job.direction))
                self._queue.task_done()

    async def render(self, symbol: str, settings: dict, info: dict | None = None) -> Path | None:
        async with self._render_semaphore:
            klines = await self.client.get_klines(symbol, settings["timeframe"], limit=80)
            path = await asyncio.to_thread(
                build_snapshot, symbol, klines, [], settings, info, snapshot_dir=self.config.snapshot_dir
            )
            return snapshot_path(self.config.snapshot_dir, path)

    async def _pace_chat(self, chat_id: int):
        async with self._send_lock:
            now = time.monotonic()
            scheduled = max(now, self._next_send.get(chat_id, 0))
            self._next_send[chat_id] = scheduled + 1.1
        await asyncio.sleep(max(0, scheduled - time.monotonic()))

    async def _deliver(self, job: AlertJob):
        settings = await self.db.get_settings(job.chat_id)
        if settings["paused"] or settings["direction"] not in ("both", job.direction.lower()):
            return
        if (
            abs(job.change) < settings["pump_threshold"]
            or self.turnover.get(job.symbol, 0) < settings["volume_min_usd"]
        ):
            return
        if self._retry_after.get(job.chat_id, 0) > time.monotonic():
            return
        info = {"direction": job.direction, "change_percent": job.change, "window_sec": job.window}
        image = None
        try:
            image = await self.render(job.symbol, settings, info)
        except Exception as exc:
            logger.warning(
                "Chart unavailable for %s (%s); sending text alert", job.symbol, type(exc).__name__, exc_info=True
            )
        number = await self.db.recent_alert_count(job.chat_id, job.symbol, job.direction) + 1
        caption = alert_caption(
            job.symbol, job.direction, job.change, job.price, job.window, settings["timeframe"], job.turnover, number
        )
        try:
            await self._pace_chat(job.chat_id)
            if image:
                with image.open("rb") as photo:
                    await self.bot.send_photo(
                        job.chat_id,
                        photo=photo,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=market_links(job.symbol),
                        read_timeout=30,
                        write_timeout=30,
                    )
            else:
                await self.bot.send_message(
                    job.chat_id,
                    caption + "\nГрафик временно недоступен.",
                    parse_mode="HTML",
                    reply_markup=market_links(job.symbol),
                    read_timeout=30,
                    write_timeout=30,
                )
            job.delivered = True
            self.sent_count += 1
        except Forbidden:
            await self.db.save_settings(job.chat_id, {"paused": 1})
            logger.info("Paused chat after bot was blocked")
            return
        except RetryAfter as exc:
            retry_after = (
                exc.retry_after.total_seconds() if hasattr(exc.retry_after, "total_seconds") else float(exc.retry_after)
            )
            self._retry_after[job.chat_id] = time.monotonic() + retry_after + 1
            raise
        except TelegramError:
            raise
        try:
            await self.db.add_alert(
                job.chat_id, job.symbol, job.direction, job.change, job.price, str(image) if image else None
            )
        except Exception:
            # Keep cooldown: Telegram accepted the message, even if history persistence failed.
            logger.exception("Sent alert could not be saved; cooldown retained")

    async def _cleanup_loop(self):
        while not self._stop.is_set():
            try:
                await self.db.purge_history(self.config.alert_retention_days)
                await asyncio.to_thread(
                    cleanup_snapshots,
                    self.config.snapshot_dir,
                    self.config.snapshot_retention_days,
                    self.config.snapshot_max_mb,
                )
            except Exception as exc:
                logger.warning("Cleanup failed (%s)", type(exc).__name__)
            if not await self._sleep(self.config.cleanup_interval_sec):
                break
