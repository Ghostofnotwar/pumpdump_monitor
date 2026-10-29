"""Validated configuration. Importing this module does not require a bot token."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent


class ConfigError(ValueError):
    pass


def _number(name: str, default: str, low: float, high: float, integer: bool = False):
    raw = os.getenv(name, default)
    try:
        value = int(raw) if integer else float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name}: expected a number") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name}: expected a value between {low:g} and {high:g}")
    return value


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ConfigError(f"{name}: expected true or false")
    return value in {"true", "1", "yes"}


def database_path(url: str) -> Path:
    if not url.startswith("sqlite:///"):
        raise ConfigError("Only sqlite:///path URLs are supported; PostgreSQL is not implemented")
    raw = url.removeprefix("sqlite:///")
    if not raw or raw == ":memory:" or "?" in raw or "#" in raw:
        raise ConfigError("DATABASE_URL must name a persistent SQLite file")
    path = Path(raw)
    return (path if path.is_absolute() else BASE_DIR / path).resolve()


def _http_url(name: str, value: str, *, https_only: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        valid = parsed.hostname and not parsed.username and not parsed.password
        valid = valid and not parsed.query and not parsed.fragment
        valid = valid and parsed.scheme in ({"https"} if https_only else {"https", "http"})
    except ValueError as exc:
        raise ConfigError(f"{name}: invalid URL") from exc
    if not valid:
        raise ConfigError(
            f"{name}: use an absolute {'HTTPS' if https_only else 'HTTP(S)'} URL without credentials/query"
        )
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    db_path: Path
    snapshot_dir: Path
    port: int = 8000
    bybit_rest: str = "https://api.bybit.com"
    web_app_url: str = ""
    allowed_chat_ids: frozenset[int] = frozenset()
    pairs_update_sec: float = 300
    poll_interval_sec: float = 2
    alert_window_sec: float = 300
    alert_cooldown_sec: float = 600
    max_symbols: int = 500
    min_universe_volume_usd: float = 50_000
    request_timeout_sec: float = 15
    max_concurrent_requests: int = 8
    alert_workers: int = 2
    alert_queue_size: int = 100
    alert_retention_days: int = 30
    snapshot_retention_days: int = 7
    snapshot_max_mb: int = 512
    cleanup_interval_sec: float = 3600
    drop_pending_updates: bool = False
    log_level: str = "INFO"

    @property
    def history_size(self) -> int:
        return max(32, int(self.alert_window_sec / self.poll_interval_sec) + 32)

    @property
    def poll_tolerance_sec(self) -> float:
        return max(10.0, self.poll_interval_sec * 3)

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv(BASE_DIR / ".env", override=False)
        db_path = database_path(os.getenv("DATABASE_URL", "sqlite:///data/bot.db"))
        snapshot_raw = os.getenv("SNAPSHOT_DIR")
        snapshot_dir = Path(snapshot_raw) if snapshot_raw else db_path.parent / "snapshots"
        if not snapshot_dir.is_absolute():
            snapshot_dir = BASE_DIR / snapshot_dir
        raw_ids = os.getenv("ALLOWED_CHAT_IDS", "")
        try:
            allowed = frozenset(int(x.strip()) for x in raw_ids.split(",") if x.strip())
        except ValueError as exc:
            raise ConfigError("ALLOWED_CHAT_IDS: comma-separated integer chat IDs required") from exc
        if 0 in allowed:
            raise ConfigError("ALLOWED_CHAT_IDS: zero is not a chat ID")
        level = os.getenv("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("LOG_LEVEL: invalid logging level")
        token = os.getenv("BOT_TOKEN", "").strip()
        if token.startswith("123456:"):
            raise ConfigError("BOT_TOKEN: replace the example token with your real token")
        rest = os.getenv("BYBIT_REST") or os.getenv("CLOUDFLARE_PROXY") or "https://api.bybit.com"
        web_url = os.getenv("WEB_APP_URL", "").strip()
        return cls(
            bot_token=token,
            db_path=db_path,
            snapshot_dir=snapshot_dir.resolve(),
            port=_number("PORT", "8000", 1, 65535, integer=True),
            bybit_rest=_http_url("BYBIT_REST", rest),
            web_app_url=_http_url("WEB_APP_URL", web_url, https_only=True) if web_url else "",
            allowed_chat_ids=allowed,
            pairs_update_sec=_number("PAIRS_UPDATE_MIN", "5", 1, 1440) * 60,
            poll_interval_sec=_number("POLL_INTERVAL_SEC", "2", 1, 60),
            alert_window_sec=_number("ALERT_WINDOW_SEC", "300", 60, 3600),
            alert_cooldown_sec=_number("ALERT_COOLDOWN_SEC", "600", 60, 86400),
            max_symbols=_number("MAX_SYMBOLS", "500", 1, 2000, integer=True),
            min_universe_volume_usd=_number("MIN_UNIVERSE_VOLUME_USD", "50000", 0, 10_000_000),
            request_timeout_sec=_number("BYBIT_TIMEOUT_SEC", "15", 1, 60),
            max_concurrent_requests=_number("MAX_CONCURRENT_REQUESTS", "8", 1, 20, integer=True),
            alert_workers=_number("ALERT_WORKERS", "2", 1, 8, integer=True),
            alert_queue_size=_number("ALERT_QUEUE_SIZE", "100", 10, 1000, integer=True),
            alert_retention_days=_number("ALERT_RETENTION_DAYS", "30", 1, 365, integer=True),
            snapshot_retention_days=_number("SNAPSHOT_RETENTION_DAYS", "7", 1, 90, integer=True),
            snapshot_max_mb=_number("SNAPSHOT_MAX_MB", "512", 10, 10000, integer=True),
            drop_pending_updates=_bool("DROP_PENDING_UPDATES"),
            log_level=level,
        )

    def require_bot_token(self) -> None:
        if not self.bot_token or ":" not in self.bot_token or any(c.isspace() for c in self.bot_token):
            raise ConfigError("BOT_TOKEN is required (set it in the environment or local .env)")
