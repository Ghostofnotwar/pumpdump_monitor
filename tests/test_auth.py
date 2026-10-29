import pytest
from conftest import TOKEN

from auth import InitDataError, validate_init_data


def test_valid_signature_returns_private_user_not_supplied_chat(signed_data):
    raw = signed_data(chat_id="999999", chat_type="group")
    assert validate_init_data(raw, TOKEN) == 12345


@pytest.mark.parametrize(
    "case", ["empty", "wrong-token", "expired", "future", "tampered", "duplicate", "missing-hash", "oversized"]
)
def test_invalid_init_data(signed_data, case):
    now = 1800000000
    raw = signed_data(now=now)
    token = TOKEN
    if case == "empty":
        raw = ""
    elif case == "wrong-token":
        token = "999:other"
    elif case == "expired":
        raw = signed_data(now=now - 3601)
    elif case == "future":
        raw = signed_data(now=now + 31)
    elif case == "tampered":
        raw = raw.replace("Test", "Mallory")
    elif case == "duplicate":
        raw += "&hash=abc"
    elif case == "missing-hash":
        raw = raw.split("&hash=")[0]
    elif case == "oversized":
        raw += "&extra=" + "x" * 17000
    with pytest.raises(InitDataError):
        validate_init_data(raw, token, now=now)


def test_signed_but_invalid_user(signed_data):
    import json

    for user in [{"id": True}, {"id": -1}, {"id": "123"}, [], {"id": 2**53}]:
        raw = signed_data(user=json.dumps(user))
        with pytest.raises(InitDataError):
            validate_init_data(raw, TOKEN)


def test_signature_matches_telegram_algorithm(signed_data):
    import hashlib
    import hmac
    from urllib.parse import parse_qsl

    raw = signed_data()
    fields = dict(parse_qsl(raw))
    provided = fields.pop("hash")
    secret = hashlib.sha256(b"WebAppData" + TOKEN.encode()).digest()  # Wrong key construction.
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    wrong = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    assert provided != wrong
