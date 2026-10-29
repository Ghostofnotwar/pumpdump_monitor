import sqlite3
import tomllib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backup_db import backup_database
from bot_handlers import authorized


async def test_bot_allowlist_and_group_admin_boundary(config, db):
    monitor = SimpleNamespace(config=config)
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"config": config, "db": db, "monitor": monitor}), bot=AsyncMock()
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=SimpleNamespace(id=1),
        effective_message=None,
    )
    assert await authorized(update, context, write=True)
    restricted = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"config": replace(config, allowed_chat_ids=frozenset({2})), "db": db, "monitor": monitor}
        ),
        bot=AsyncMock(),
    )
    assert not await authorized(update, restricted, write=True)
    update.effective_chat = SimpleNamespace(id=1, type="group")
    context.bot.get_chat_member.return_value = SimpleNamespace(status="member")
    assert not await authorized(update, context, write=True)
    context.bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
    assert await authorized(update, context, write=True)
    assert await authorized(update, context, write=False)


async def test_consistent_backup_and_no_overwrite(config, db, tmp_path):
    await db.save_settings(1, {"pump_threshold": 10})
    output = tmp_path / "backups" / "backup.db"
    assert backup_database(config.db_path, output) == output
    with sqlite3.connect(output) as backup:
        assert backup.execute("SELECT pump_threshold FROM user_settings WHERE chat_id=1").fetchone()[0] == 10
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(FileExistsError):
        backup_database(config.db_path, output)


def test_fly_single_instance_persistent_configuration():
    root = Path(__file__).resolve().parents[1]
    fly = tomllib.loads((root / "fly.toml").read_text())
    assert fly["http_service"]["auto_stop_machines"] == "off"
    assert fly["http_service"]["min_machines_running"] == 1
    assert fly["deploy"]["strategy"] == "immediate"
    assert fly["mounts"][0]["destination"] == "/data"
    assert fly["env"]["DATABASE_URL"] == "sqlite:////data/bot.db"
    assert fly["env"]["SNAPSHOT_DIR"].startswith("/data/")
    assert fly["http_service"]["checks"][0]["path"] == "/healthz"
    assert "gosu app" in (root / "entrypoint.sh").read_text()
    assert "BOT_TOKEN" not in fly["env"]
    compose = (root / "docker-compose.yml").read_text()
    assert "bot-data:/data" in compose
