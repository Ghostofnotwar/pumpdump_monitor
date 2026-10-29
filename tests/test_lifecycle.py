import asyncio
import os
import signal
import socket
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

import main
from db import Database


class FakeUpdater:
    def __init__(self):
        self.running = False

    async def start_polling(self, **kwargs):
        self.running = True

    async def stop(self):
        self.running = False


class FakeApplication:
    def __init__(self):
        self.bot_data = {}
        self.bot = AsyncMock()
        self.updater = FakeUpdater()
        self.initialized = False
        self.running = False

    async def initialize(self):
        self.initialized = True

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False

    async def shutdown(self):
        self.initialized = False


class FakeMonitor:
    def __init__(self, bot, client, db, config):
        self.client = client
        self.running = False
        self.fatal = asyncio.Event()
        self.stop_called = False

    async def start(self):
        self.running = True

    async def stop(self, **kwargs):
        self.stop_called = True
        self.running = False
        await self.client.close()


@pytest.fixture
def runtime(config, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    app = FakeApplication()
    monitors = []

    def build(config, db, monitor):
        app.bot_data.update(config=config, db=db, monitor=monitor)
        return app

    def monitor_factory(*args):
        monitor = FakeMonitor(*args)
        monitors.append(monitor)
        return monitor

    monkeypatch.setattr(main, "build_application", build)
    monkeypatch.setattr(main, "Monitor", monitor_factory)
    return replace(config, port=port), app, monitors


async def wait_http(config):
    async with httpx.AsyncClient() as client:
        for _ in range(200):
            try:
                response = await client.get(f"http://127.0.0.1:{config.port}/healthz")
                if response.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            await asyncio.sleep(0.01)
    raise AssertionError("HTTP did not start")


async def test_sigterm_stops_every_subsystem(runtime):
    config, app, monitors = runtime
    previous = signal.getsignal(signal.SIGTERM)
    task = asyncio.create_task(main.run(config))
    try:
        await wait_http(config)
        assert app.updater.running and monitors[0].running
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 10)
        assert monitors[0].stop_called
        assert not app.updater.running and not app.running and not app.initialized
        assert monitors[0].client.session is None
        assert not await Database(config.db_path).ping()
        assert signal.getsignal(signal.SIGTERM) is previous
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_startup_failure_cleans_http_and_database(runtime, monkeypatch):
    config, app, monitors = runtime

    async def fail():
        raise RuntimeError("invalid token")

    monkeypatch.setattr(app, "initialize", fail)
    with pytest.raises(RuntimeError, match="invalid token"):
        await asyncio.wait_for(main.run(config), 10)
    assert not await Database(config.db_path).ping()
    assert not monitors[0].running


async def test_worker_fatal_requests_process_restart(runtime):
    config, app, monitors = runtime
    stop = asyncio.Event()
    task = asyncio.create_task(main.run(config, stop))
    try:
        await wait_http(config)
        monitors[0].fatal.set()
        with pytest.raises(RuntimeError, match="supervised monitor worker"):
            await asyncio.wait_for(task, 10)
        assert monitors[0].stop_called and not app.updater.running
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_stop_event_is_awaited(runtime):
    config, app, monitors = runtime
    stop = asyncio.Event()
    task = asyncio.create_task(main.run(config, stop))
    await wait_http(config)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert not app.initialized
