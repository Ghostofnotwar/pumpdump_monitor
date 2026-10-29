import asyncio
import time
from collections import deque
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden, RetryAfter

from monitor import AlertJob, Monitor


class FakeClient:
    def __init__(self, klines):
        self.klines = klines
        self.started = False

    async def start(self):
        self.started = True

    async def close(self):
        self.started = False

    async def get_klines(self, *args, **kwargs):
        return self.klines


class FakeBot:
    def __init__(self):
        self.photos = []
        self.messages = []
        self.closed = []

    async def send_photo(self, chat_id, photo, **kwargs):
        self.photos.append((chat_id, photo.read(), kwargs, photo))
        self.closed.append(photo)

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))


@pytest.fixture
def monitor(config, db, klines):
    config.snapshot_dir.mkdir()
    bot = FakeBot()
    return Monitor(bot, FakeClient(klines), db, config)


async def seed(monitor, change=6):
    await monitor.db.get_settings(1)
    monitor.symbols = ["BTCUSDT"]
    monitor._symbol_set = {"BTCUSDT"}
    now = time.time()
    monitor.price_history["BTCUSDT"] = deque([(now - 60 + second, 100) for second in range(60)], maxlen=100)
    await monitor._process_tickers([{"symbol": "BTCUSDT", "lastPrice": str(100 + change), "turnover24h": "200000"}])
    return now


async def test_pump_and_dump_honor_user_settings(monitor):
    await seed(monitor, 6)
    assert monitor._queue.qsize() == 1
    job = monitor._queue.get_nowait()
    assert job.direction == "PUMP" and job.change == pytest.approx(6)
    monitor._queue.task_done()
    await monitor.db.release_alert(1, "BTCUSDT", "PUMP", job.claimed_at)
    monitor._pending.clear()
    monitor.price_history.clear()
    await monitor.db.save_settings(1, {"direction": "pump"})
    await seed(monitor, -6)
    assert monitor._queue.empty()
    await monitor.db.save_settings(1, {"direction": "dump"})
    await seed(monitor, -6)
    assert monitor._queue.get_nowait().direction == "DUMP"


async def test_threshold_volume_and_pause_are_effective(monitor):
    await monitor.db.get_settings(1)
    await monitor.db.save_settings(1, {"pump_threshold": 10, "volume_min_usd": 300000})
    await seed(monitor, 6)
    assert monitor._queue.empty()
    await monitor.db.save_settings(1, {"pump_threshold": 5, "volume_min_usd": 100000, "paused": True})
    await seed(monitor, 6)
    assert monitor._queue.empty()


async def test_no_duplicate_during_sustained_move(monitor):
    await seed(monitor)
    await seed(monitor)
    assert monitor._queue.qsize() == 1


async def test_delivery_closes_file_and_records_history(monitor):
    await seed(monitor)
    job = monitor._queue.get_nowait()
    await monitor._deliver(job)
    assert job.delivered
    assert all(photo.closed for photo in monitor.bot.closed)
    assert len(await monitor.db.get_alerts(1)) == 1
    assert monitor.sent_count == 1
    assert "PUMP" in monitor.bot.photos[0][2]["caption"]


async def test_chart_failure_falls_back_to_text(monitor, monkeypatch):
    await seed(monitor)
    monkeypatch.setattr(monitor, "render", AsyncMock(side_effect=RuntimeError("no candles")))
    job = monitor._queue.get_nowait()
    await monitor._deliver(job)
    assert job.delivered and len(monitor.bot.messages) == 1
    assert "График временно недоступен" in monitor.bot.messages[0][1]


async def test_blocked_bot_pauses_chat(monitor, monkeypatch):
    await seed(monitor)
    monkeypatch.setattr(monitor.bot, "send_photo", AsyncMock(side_effect=Forbidden("blocked")))
    job = monitor._queue.get_nowait()
    await monitor._deliver(job)
    assert (await monitor.db.get_settings(1))["paused"] == 1
    assert not job.delivered


async def test_retry_after_releases_claim_for_retry(monitor, db):
    await seed(monitor)
    job = monitor._queue.get_nowait()
    monitor.bot.send_photo = AsyncMock(side_effect=RetryAfter(1))
    await monitor._queue.put(job)
    monitor._pending.add((1, "BTCUSDT", "PUMP"))
    monitor._stop.set()
    await asyncio.wait_for(monitor._alert_worker(), 5)
    assert monitor.failed_count == 1
    assert not monitor._pending
    assert await db.reserve_alert(1, "BTCUSDT", "PUMP", job.claimed_at, 60)


async def test_start_stop_supervises_all_tasks_and_drains(monitor):
    monitor.client.get_linear_symbols = AsyncMock(
        return_value=[
            {"symbol": "BTCUSDT", "status": "Trading", "contractType": "LinearPerpetual", "quoteCoin": "USDT"}
        ]
    )
    monitor.client.get_tickers = AsyncMock(
        return_value=[{"symbol": "BTCUSDT", "lastPrice": "100", "turnover24h": "200000"}]
    )
    await monitor.start()
    for _ in range(200):
        if monitor.symbols:
            break
        await asyncio.sleep(0.01)
    assert len(monitor._tasks) == 3 + monitor.config.alert_workers
    assert monitor.symbols == ["BTCUSDT"]
    await monitor.stop()
    assert not monitor.running and not monitor._tasks
    assert not monitor.client.started
    assert not any(task.get_name().startswith("monitor-") and not task.done() for task in asyncio.all_tasks())


async def test_queue_is_bounded(monitor):
    await monitor.db.get_settings(1)
    monitor._queue = asyncio.Queue(maxsize=1)
    job = AlertJob(1, "BTCUSDT", "PUMP", 6, 106, 60, 200000, time.time())
    await monitor._enqueue(job)
    await monitor._enqueue(AlertJob(1, "ETHUSDT", "DUMP", -6, 94, 60, 200000, time.time()))
    assert monitor.dropped_count == 1
    assert monitor._queue.qsize() == 1
