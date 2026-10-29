import asyncio
import time

import aiosqlite
import pytest

from db import Database


async def test_settings_defaults_and_isolation(db):
    first = await db.get_settings(1)
    assert first["direction"] == "both"
    assert first["show_oi"] == 0
    await db.save_settings(1, {"pump_threshold": 10})
    assert (await db.get_settings(2))["pump_threshold"] == 5
    assert (await db.get_settings(1))["pump_threshold"] == 10
    assert [row["chat_id"] for row in await db.active_settings()] == [1, 2]
    await db.save_settings(1, {"paused": 1})
    assert [row["chat_id"] for row in await db.active_settings()] == [2]


async def test_unknown_and_malicious_settings_do_not_change_database(db):
    await db.get_settings(1)
    with pytest.raises(ValueError):
        await db.save_settings(1, {"theme = 'light', paused": 1})
    assert (await db.get_settings(1))["paused"] == 0


async def test_cooldown_atomic_under_concurrency(db):
    now = time.time()
    results = await asyncio.gather(*(db.reserve_alert(1, "BTCUSDT", "PUMP", now, 600) for _ in range(30)))
    assert sum(results) == 1
    assert not await db.reserve_alert(1, "BTCUSDT", "PUMP", now + 599, 600)
    assert await db.reserve_alert(1, "BTCUSDT", "DUMP", now, 600)
    assert await db.reserve_alert(2, "BTCUSDT", "PUMP", now, 600)
    assert await db.reserve_alert(1, "BTCUSDT", "PUMP", now + 600, 600)


async def test_release_cannot_remove_newer_claim(db):
    now = time.time()
    assert await db.reserve_alert(1, "BTCUSDT", "PUMP", now, 60)
    assert await db.reserve_alert(1, "BTCUSDT", "PUMP", now + 60, 60)
    await db.release_alert(1, "BTCUSDT", "PUMP", now)
    assert not await db.reserve_alert(1, "BTCUSDT", "PUMP", now + 61, 60)


async def test_cooldown_survives_restart(config):
    db = Database(config.db_path)
    await db.init()
    now = time.time()
    await db.reserve_alert(1, "BTCUSDT", "PUMP", now, 600)
    await db.close()
    db = Database(config.db_path)
    await db.init()
    try:
        assert not await db.reserve_alert(1, "BTCUSDT", "PUMP", now + 1, 600)
    finally:
        await db.close()


async def test_legacy_migration_preserves_settings_and_alerts(config):
    async with aiosqlite.connect(config.db_path) as connection:
        await connection.execute(
            "CREATE TABLE user_settings(chat_id INTEGER PRIMARY KEY, timeframe TEXT DEFAULT '5', pump_threshold REAL DEFAULT 5, paused INTEGER DEFAULT 0)"
        )
        await connection.execute("INSERT INTO user_settings VALUES (1, '15', 10, 1)")
        await connection.execute(
            "CREATE TABLE alerts(id INTEGER PRIMARY KEY, chat_id INTEGER, symbol TEXT, direction TEXT, change_percent REAL, price REAL, screenshot_path TEXT, timestamp TEXT)"
        )
        await connection.execute(
            "INSERT INTO alerts VALUES (1, 1, 'BTCUSDT', 'PUMP', 6, 100, '/tmp/snapshots/old.png', '2025-01-01T00:00:00')"
        )
        await connection.commit()
    db = Database(config.db_path)
    await db.init()
    try:
        settings = await db.get_settings(1)
        assert settings["timeframe"] == "15" and settings["pump_threshold"] == 10
        assert settings["paused"] == 1 and settings["direction"] == "both"
        assert (await db.get_alerts(1))[0]["symbol"] == "BTCUSDT"
    finally:
        await db.close()


async def test_alert_ownership_persistence_and_limit(db):
    await db.add_alert(1, "BTCUSDT", "DUMP", -6, 100, "/data/snapshots/chart.png")
    await db.add_alert(2, "ETHUSDT", "PUMP", 6, 100, None)
    rows = await db.get_alerts(1)
    assert len(rows) == 1 and rows[0]["screenshot_path"] == "chart.png"
    assert await db.get_alert(1, rows[0]["id"])
    assert await db.get_alert(2, rows[0]["id"]) is None
    assert await db.recent_alert_count(1, "BTCUSDT", "DUMP") == 1
    for limit in [0, 51, -1, True]:
        with pytest.raises(ValueError):
            await db.get_alerts(1, limit)


async def test_legacy_timestamp_is_explicitly_utc(db):
    await db.add_alert(1, "BTCUSDT", "PUMP", 6, 100, None)
    async with db._transaction() as connection:
        await connection.execute("UPDATE alerts SET timestamp='2025-01-01T00:00:00'")
    row = (await db.get_alerts(1))[0]
    assert row["timestamp"] == "2025-01-01T00:00:00+00:00"


async def test_ping_and_closed_database(config):
    db = Database(config.db_path)
    assert not await db.ping()
    await db.init()
    assert await db.ping()
    await db.close()
    assert not await db.ping()
