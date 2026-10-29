import uuid

import httpx
import pytest

from web_app import create_app


@pytest.fixture
async def client(config, db):
    app = create_app(config, db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_health_liveness_independent_of_market_and_static_ui(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert (await client.get("/readyz")).status_code == 503
    response = await client.get("/webapp")
    assert response.status_code == 200
    assert "Pump" in response.text
    assert "frame-ancestors" in response.headers["content-security-policy"]
    assert (await client.get("/static/app.js")).status_code == 200


@pytest.mark.parametrize("path", ["/api/settings", "/api/alerts", "/api/status"])
async def test_private_api_requires_signature(client, path):
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path + ("&chat_id=1" if "?" in path else "?chat_id=1"))).status_code == 401


async def test_settings_use_signed_user_and_ignore_query_chat(client, signed_data):
    headers = {"X-Telegram-Init-Data": signed_data(user_id=12345)}
    response = await client.get("/api/settings?chat_id=99999", headers=headers)
    assert response.status_code == 200
    assert response.json()["pump_threshold"] == 5
    response = await client.post("/api/settings", headers=headers, json={"pump_threshold": 10, "direction": "dump"})
    assert response.status_code == 200
    assert (await client.get("/api/settings", headers=headers)).json()["direction"] == "dump"
    other = {"X-Telegram-Init-Data": signed_data(user_id=67890)}
    assert (await client.get("/api/settings", headers=other)).json()["pump_threshold"] == 5


@pytest.mark.parametrize(
    "payload",
    [
        {"chat_id": 1},
        {"paused": 1, "sql": "drop"},
        {"pump_threshold": 100},
        {"pump_threshold": None},
        {},
        {"timeframe": "2"},
        {"volume_min_usd": "100000"},
    ],
)
async def test_invalid_settings_rejected(client, signed_data, payload):
    response = await client.post("/api/settings", headers={"X-Telegram-Init-Data": signed_data()}, json=payload)
    assert response.status_code == 422


async def test_allowlist_applies_to_valid_signature(config, db, signed_data):
    from dataclasses import replace

    app = create_app(replace(config, allowed_chat_ids=frozenset({999})), db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/settings", headers={"X-Telegram-Init-Data": signed_data()})).status_code == 403


async def test_alert_and_image_owner_isolation(client, db, signed_data, config):
    await db.add_alert(12345, "BTCUSDT", "DUMP", -6, 100, None)
    row = (await db.get_alerts(12345))[0]
    headers = {"X-Telegram-Init-Data": signed_data(user_id=12345)}
    result = await client.get("/api/alerts?limit=10", headers=headers)
    assert result.json()["alerts"][0]["symbol"] == "BTCUSDT"
    other = {"X-Telegram-Init-Data": signed_data(user_id=67890)}
    assert (await client.get("/api/alerts", headers=other)).json() == {"alerts": []}
    assert (await client.get(f"/api/alerts/{row['id']}/image", headers=other)).status_code == 404
    assert (await client.get("/api/alerts?limit=51", headers=headers)).status_code == 422
    config.snapshot_dir.mkdir()
    name = f"BTCUSDT_{uuid.uuid4().hex}.png"
    (config.snapshot_dir / name).write_bytes(b"\x89PNG\r\n\x1a\ntest")
    await db.add_alert(12345, "BTCUSDT", "PUMP", 6, 100, name)
    row = (await db.get_alerts(12345))[0]
    assert (await client.get(f"/api/alerts/{row['id']}/image", headers=headers)).status_code == 200
    assert (await client.get(f"/api/alerts/{row['id']}/image", headers=other)).status_code == 404
