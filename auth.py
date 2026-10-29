"""Validate Telegram Mini App initData on the server, not in browser JavaScript."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_INIT_DATA_BYTES = 16_384
MAX_AGE_SECONDS = 3600


class InitDataError(ValueError):
    pass


def validate_init_data(raw: str, bot_token: str, *, now: float | None = None, max_age: int = MAX_AGE_SECONDS) -> int:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > MAX_INIT_DATA_BYTES:
        raise InitDataError("Missing or oversized Telegram initialization data")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, encoding="utf-8")
    except (UnicodeError, ValueError) as exc:
        raise InitDataError("Malformed initialization data") from exc
    if len(pairs) > 100 or len({key for key, _ in pairs}) != len(pairs):
        raise InitDataError("Duplicate or excessive initialization fields")
    fields = dict(pairs)
    supplied_hash = fields.pop("hash", "")
    if len(supplied_hash) != 64 or any(c not in "0123456789abcdefABCDEF" for c in supplied_hash):
        raise InitDataError("Invalid signature")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash.lower()):
        raise InitDataError("Invalid signature")
    try:
        issued_at = int(fields["auth_date"])
        user = json.loads(fields["user"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InitDataError("Missing user or authentication date") from exc
    current = time.time() if now is None else now
    if not current - max_age <= issued_at <= current + 30:
        raise InitDataError("Initialization data expired; reopen the Mini App")
    if not isinstance(user, dict) or type(user.get("id")) is not int or not 0 < user["id"] < 2**53:
        raise InitDataError("Invalid Telegram user")
    # Never trust a supplied chat_id. A Telegram private chat ID equals the user ID.
    return user["id"]
