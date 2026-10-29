import pytest

from config import Config, ConfigError, database_path
from settings_rules import validate_settings


def test_import_and_config_do_not_require_token(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    config = Config.from_env()
    assert config.bot_token == ""
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        config.require_bot_token()


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1", "abc", "61"])
def test_invalid_poll_interval(monkeypatch, value):
    monkeypatch.setenv("POLL_INTERVAL_SEC", value)
    with pytest.raises(ConfigError):
        Config.from_env()


def test_rejects_example_token(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:ABC-DEF...")
    with pytest.raises(ConfigError, match="example"):
        Config.from_env()


def test_database_urls_explicit_and_persistent(tmp_path):
    assert database_path("sqlite:///data/bot.db").name == "bot.db"
    assert database_path(f"sqlite:///{tmp_path / 'bot.db'}") == tmp_path / "bot.db"
    for url in ["postgresql://localhost/db", "sqlite:///:memory:", "sqlite:///", "sqlite:///db?mode=ro"]:
        with pytest.raises(ConfigError):
            database_path(url)


@pytest.mark.parametrize(
    "url",
    ["http://t.me/app", "https://user:secret@example.com", "https://example.com/?token=x", "https://example.com/#x"],
)
def test_web_app_url_requires_https_without_credentials(monkeypatch, url):
    monkeypatch.setenv("WEB_APP_URL", url)
    with pytest.raises(ConfigError):
        Config.from_env()


def test_settings_allowlist_and_ranges():
    assert validate_settings({"paused": True, "pump_threshold": 10}) == {"paused": 1, "pump_threshold": 10.0}
    for fields in [
        {},
        {"chat_id": 1},
        {"paused=1 WHERE 1=1--": 1},
        {"pump_threshold": float("nan")},
        {"pump_threshold": 51},
        {"volume_min_usd": 1},
        {"timeframe": "2"},
        {"direction": "invalid"},
        {"theme": "auto"},
        {"paused": 2},
    ]:
        with pytest.raises(ValueError):
            validate_settings(fields)
