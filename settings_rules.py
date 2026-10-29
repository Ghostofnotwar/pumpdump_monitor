"""Single allowlist and validation boundary for Telegram and HTTP settings."""

import math

TIMEFRAMES = ("1", "3", "5", "15", "30", "60", "240", "D")
DEFAULT_SETTINGS = {
    "timeframe": "5",
    "pump_threshold": 5.0,
    "volume_min_usd": 100_000.0,
    "zone_pct": 2.0,
    "ob_mult": 1.5,
    "volume_delta_mult": 1.5,
    "fvg_enabled": 1,
    "lot_threshold": 10.0,
    "paused": 0,
    "theme": "dark",
    "direction": "both",
    "show_delta": 0,
    "show_oi": 0,
    "show_liq": 0,
    "show_heatmap": 0,
    "show_sma": 1,
}
EDITABLE_FIELDS = frozenset(
    {
        "timeframe",
        "pump_threshold",
        "volume_min_usd",
        "paused",
        "theme",
        "direction",
        "show_delta",
        "show_heatmap",
        "show_sma",
    }
)
BOOLEAN_FIELDS = frozenset({"paused", "show_delta", "show_heatmap", "show_sma"})


def validate_settings(fields: dict) -> dict:
    if not isinstance(fields, dict) or not fields:
        raise ValueError("Provide at least one settings field")
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise ValueError("Unsupported settings field")
    result = dict(fields)
    for key, value in result.items():
        if key in BOOLEAN_FIELDS:
            if type(value) not in (bool, int) or value not in (0, 1):
                raise ValueError(f"{key}: expected a boolean")
            result[key] = int(value)
        elif key == "timeframe":
            if str(value) not in TIMEFRAMES:
                raise ValueError("Unsupported chart timeframe")
            result[key] = str(value)
        elif key in {"theme", "direction"}:
            choices = {"dark", "light"} if key == "theme" else {"both", "pump", "dump"}
            if not isinstance(value, str) or value not in choices:
                raise ValueError(f"Unsupported {key}")
        else:
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{key}: expected a finite number")
            low, high = (1, 50) if key == "pump_threshold" else (100_000, 10_000_000)
            if not low <= value <= high:
                raise ValueError(f"{key}: expected {low}..{high}")
            result[key] = float(value)
    return result
