import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

from config import Config
from db import Database

TOKEN = "123456789:test-token-not-real"


@pytest.fixture
def config(tmp_path):
    return Config(
        bot_token=TOKEN,
        db_path=tmp_path / "bot.db",
        snapshot_dir=tmp_path / "snapshots",
        poll_interval_sec=1,
        alert_window_sec=60,
        alert_cooldown_sec=60,
    )


@pytest.fixture
async def db(config):
    database = Database(config.db_path)
    await database.init()
    yield database
    await database.close()


@pytest.fixture
def signed_data():
    def sign(user_id=12345, token=TOKEN, now=None, **extra):
        fields = {
            "auth_date": str(int(time.time() if now is None else now)),
            "query_id": "test-query",
            "user": json.dumps({"id": user_id, "first_name": "Test"}),
            **extra,
        }
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(fields)

    return sign


@pytest.fixture
def klines():
    return [
        [
            1700000000000 + i * 60000,
            str(100 + i * 0.1),
            str(101 + i * 0.1),
            str(99 + i * 0.1),
            str(100.5 + i * 0.1),
            str(10 + i),
            str(1000 + i),
        ]
        for i in range(80)
    ]


@pytest.fixture
def snapshot_dir(tmp_path):
    path = Path(tmp_path) / "images"
    path.mkdir()
    return path
