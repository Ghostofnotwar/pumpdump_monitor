"""One WAL-mode SQLite connection, additive migrations and atomic cooldown claims."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from settings_rules import DEFAULT_SETTINGS, validate_settings


def utc_iso() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database is not initialized")
        return self._connection

    async def init(self):
        if self._connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self.path, timeout=15)
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=15000")
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("""CREATE TABLE IF NOT EXISTS user_settings (
                chat_id INTEGER PRIMARY KEY, timeframe TEXT NOT NULL DEFAULT '5',
                pump_threshold REAL NOT NULL DEFAULT 5, volume_min_usd REAL NOT NULL DEFAULT 100000,
                zone_pct REAL DEFAULT 2, ob_mult REAL DEFAULT 1.5,
                volume_delta_mult REAL DEFAULT 1.5, fvg_enabled INTEGER DEFAULT 1,
                lot_threshold REAL DEFAULT 10, paused INTEGER NOT NULL DEFAULT 0,
                theme TEXT NOT NULL DEFAULT 'dark', direction TEXT NOT NULL DEFAULT 'both',
                show_delta INTEGER NOT NULL DEFAULT 0, show_oi INTEGER NOT NULL DEFAULT 0,
                show_liq INTEGER NOT NULL DEFAULT 0, show_heatmap INTEGER NOT NULL DEFAULT 0,
                show_sma INTEGER NOT NULL DEFAULT 1, updated_at TEXT)""")
            async with db.execute("PRAGMA table_info(user_settings)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            # Only constant migration identifiers, never user-controlled SQL identifiers.
            migrations = {
                "timeframe": "TEXT NOT NULL DEFAULT '5'",
                "pump_threshold": "REAL NOT NULL DEFAULT 5",
                "volume_min_usd": "REAL NOT NULL DEFAULT 100000",
                "zone_pct": "REAL DEFAULT 2",
                "ob_mult": "REAL DEFAULT 1.5",
                "volume_delta_mult": "REAL DEFAULT 1.5",
                "fvg_enabled": "INTEGER DEFAULT 1",
                "lot_threshold": "REAL DEFAULT 10",
                "paused": "INTEGER NOT NULL DEFAULT 0",
                "theme": "TEXT NOT NULL DEFAULT 'dark'",
                "direction": "TEXT NOT NULL DEFAULT 'both'",
                "show_delta": "INTEGER NOT NULL DEFAULT 0",
                "show_oi": "INTEGER NOT NULL DEFAULT 0",
                "show_liq": "INTEGER NOT NULL DEFAULT 0",
                "show_heatmap": "INTEGER NOT NULL DEFAULT 0",
                "show_sma": "INTEGER NOT NULL DEFAULT 1",
                "updated_at": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    await db.execute(f"ALTER TABLE user_settings ADD COLUMN {name} {definition}")
            await db.execute("""CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                symbol TEXT NOT NULL, direction TEXT NOT NULL, change_percent REAL NOT NULL,
                price REAL NOT NULL, screenshot_path TEXT, timestamp TEXT NOT NULL)""")
            await db.execute("CREATE INDEX IF NOT EXISTS alerts_chat_id_idx ON alerts(chat_id, id DESC)")
            # Keep the legacy journal for compatibility; new charts are tracked in alerts.
            await db.execute("""CREATE TABLE IF NOT EXISTS snapshots_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, timeframe TEXT,
                screenshot_path TEXT, created_at TEXT)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS alert_cooldowns (
                chat_id INTEGER NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL,
                last_sent_at REAL NOT NULL, PRIMARY KEY(chat_id, symbol, direction))""")
            await db.execute("PRAGMA user_version=2")
            await db.commit()
        except BaseException:
            await db.close()
            raise
        self._connection = db

    async def close(self):
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    @asynccontextmanager
    async def _transaction(self):
        async with self._lock:
            try:
                yield self.connection
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    async def get_settings(self, chat_id: int) -> dict:
        async with self._transaction() as db:
            await db.execute(
                "INSERT OR IGNORE INTO user_settings(chat_id, updated_at) VALUES (?, ?)", (chat_id, utc_iso())
            )
            async with db.execute("SELECT * FROM user_settings WHERE chat_id=?", (chat_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row)

    async def save_settings(self, chat_id: int, fields: dict) -> dict:
        fields = validate_settings(fields)
        await self.get_settings(chat_id)
        assignments = ", ".join(f"{key}=? " for key in fields)
        async with self._transaction() as db:
            await db.execute(
                f"UPDATE user_settings SET {assignments}, updated_at=? WHERE chat_id=?",
                [*fields.values(), utc_iso(), chat_id],
            )
        return await self.get_settings(chat_id)

    async def active_settings(self) -> list[dict]:
        async with self._lock:
            async with self.connection.execute("SELECT * FROM user_settings WHERE paused=0") as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def reserve_alert(self, chat_id: int, symbol: str, direction: str, now: float, cooldown: float) -> bool:
        async with self._transaction() as db:
            async with db.execute(
                """INSERT INTO alert_cooldowns VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, symbol, direction) DO UPDATE SET last_sent_at=excluded.last_sent_at
                WHERE alert_cooldowns.last_sent_at <= excluded.last_sent_at - ?
                RETURNING chat_id""",
                (chat_id, symbol, direction, now, cooldown),
            ) as cursor:
                return await cursor.fetchone() is not None

    async def release_alert(self, chat_id: int, symbol: str, direction: str, claimed_at: float):
        """Release only our claim; never erase a newer worker's reservation."""
        async with self._transaction() as db:
            await db.execute(
                "DELETE FROM alert_cooldowns WHERE chat_id=? AND symbol=? AND direction=? AND last_sent_at=?",
                (chat_id, symbol, direction, claimed_at),
            )

    async def add_alert(
        self, chat_id: int, symbol: str, direction: str, change: float, price: float, screenshot: str | None
    ):
        # Store a filename, not a host-specific absolute path.
        image = Path(screenshot).name if screenshot else None
        async with self._transaction() as db:
            await db.execute(
                """INSERT INTO alerts(chat_id, symbol, direction, change_percent, price, screenshot_path, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, symbol, direction, change, price, image, utc_iso()),
            )

    @staticmethod
    def _alert_dict(row) -> dict:
        result = dict(row)
        timestamp = result.get("timestamp")
        if timestamp:
            try:
                parsed = datetime.fromisoformat(timestamp)
                if parsed.tzinfo is None:
                    result["timestamp"] = parsed.replace(tzinfo=UTC).isoformat()
            except (TypeError, ValueError):
                pass  # Preserve legacy malformed data for display rather than hide the alert.
        return result

    async def get_alerts(self, chat_id: int, limit: int = 5) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Alert limit must be 1..50")
        async with self._lock:
            async with self.connection.execute(
                "SELECT * FROM alerts WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id, limit)
            ) as cursor:
                return [self._alert_dict(row) for row in await cursor.fetchall()]

    async def get_alert(self, chat_id: int, alert_id: int) -> dict | None:
        async with self._lock:
            async with self.connection.execute(
                "SELECT * FROM alerts WHERE chat_id=? AND id=?", (chat_id, alert_id)
            ) as cursor:
                row = await cursor.fetchone()
                return self._alert_dict(row) if row else None

    async def recent_alert_count(self, chat_id: int, symbol: str, direction: str) -> int:
        cutoff = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        async with self._lock:
            async with self.connection.execute(
                """SELECT COUNT(*) FROM alerts WHERE chat_id=? AND symbol=?
                AND direction=? AND timestamp>=?""",
                (chat_id, symbol, direction, cutoff),
            ) as cursor:
                return (await cursor.fetchone())[0]

    async def purge_history(self, days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        async with self._transaction() as db:
            async with db.execute("DELETE FROM alerts WHERE timestamp<? RETURNING id", (cutoff,)) as cursor:
                count = len(await cursor.fetchall())
            await db.execute("DELETE FROM snapshots_journal WHERE created_at<?", (cutoff,))
            await db.execute(
                "DELETE FROM alert_cooldowns WHERE last_sent_at<?", (datetime.now(UTC).timestamp() - 86400)
            )
            return count

    async def ping(self) -> bool:
        try:
            async with self._lock:
                async with self.connection.execute("SELECT 1") as cursor:
                    return (await cursor.fetchone())[0] == 1
        except Exception:
            return False


# Backwards-compatible defaults for offline chart/debugging tools, not runtime state.
DEFAULTS = dict(DEFAULT_SETTINGS)
