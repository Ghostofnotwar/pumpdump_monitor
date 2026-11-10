import asyncio
from pathlib import Path

import matplotlib.pyplot as plt
from PIL import Image

from chart_builder import build_liquidation_heatmap, build_snapshot
from market_indicators import MarketIndicators, parse_binance_indicators, parse_heatmap


def test_public_indicators_normalize_real_trade_oi_and_forceorder_rows():
    data = parse_binance_indicators(
        [{"T": 1_700_000_000_000, "q": "2", "m": False}, {"T": 1_700_000_060_000, "q": "1", "m": True}],
        [{"timestamp": 1_700_000_000_000, "sumOpenInterest": "123.5"}],
        [
            {"time": 1_700_000_000_000, "side": "SELL", "executedQty": "2", "avgPrice": "100"},
            {"time": 1_700_000_000_000, "side": "BUY", "executedQty": "1", "avgPrice": "101"},
        ],
    )
    assert data["cvd"] == [[1_700_000_000_000, 2.0], [1_700_000_060_000, 1.0]]
    assert data["oi"] == [[1_700_000_000_000, 123.5]]
    assert data["liquidations"] == [[1_700_000_000_000, 200.0, 101.0]]


def test_heatmap_parser_validates_sparse_indices_and_intensity_is_unitless():
    payload = parse_heatmap(
        {
            "data": {
                "y_axis": [99, 100, 101],
                "price_candlesticks": [[1_700_000_000_000, 99, 101, 98, 100]],
                "liquidation_leverage_data": [[0, 1, 12.4], [10, 1, 5], [0, 99, 8], [0, 2, -1]],
            }
        }
    )
    assert payload == {
        "y_axis": [99.0, 100.0, 101.0],
        "candles": [(1_700_000_000_000, 100.0)],
        "points": [[0, 1, 12.4]],
    }
    descending = parse_heatmap(
        {
            "y_axis": [101, 100, 99],
            "price_candlesticks": [
                [1_700_000_060_000, 99, 101, 98, 100],
                [1_700_000_000_000, 99, 101, 98, 99.5],
            ],
            "liquidation_leverage_data": [[0, 0, 4], [1, 2, 8]],
        }
    )
    assert descending["y_axis"] == [99.0, 100.0, 101.0]
    assert descending["candles"] == [(1_700_000_000_000, 99.5), (1_700_000_060_000, 100.0)]
    assert descending["points"] == [[1, 2, 4], [0, 0, 8]]


def test_coinglass_missing_key_is_graceful():
    assert asyncio.run(MarketIndicators().heatmap("BTCUSDT", "5")) is None


async def test_unavailable_public_feeds_and_coinglass_errors_degrade_gracefully(monkeypatch):
    async def unavailable(*args, **kwargs):
        raise RuntimeError("feed disabled / subscription required")

    indicators = MarketIndicators(coinglass_api_key="dummy-test-key")
    monkeypatch.setattr(indicators, "_json", unavailable)
    values = await indicators.indicators("BTCUSDT", "5")
    assert values["cvd"] == values["oi"] == values["liquidations"] == []
    assert await indicators.heatmap("BTCUSDT", "5") is None


async def test_cache_deduplicates_parallel_api_calls():
    indicators = MarketIndicators(cache_seconds=30)
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"value": 1}

    values = await asyncio.gather(*(indicators._cached(("same",), loader) for _ in range(8)))
    assert calls == 1 and all(value == {"value": 1} for value in values)


def test_render_separate_heatmap_png_and_three_indicator_panels(klines, snapshot_dir):
    start = 1_700_000_000_000
    indicators = {
        "source": "Binance USD-M public data",
        "cvd": [[start + i * 60_000, float(i - 30)] for i in range(80)],
        "oi": [[start + i * 60_000, 1000 + i * 2] for i in range(80)],
        "liquidations": [[start + i * 60_000, 10_000 + i * 100, 9000 + i * 75] for i in range(80)],
    }
    settings = {"theme": "dark", "show_delta": 1, "show_oi": 1, "show_liq": 1}
    path = build_snapshot("BTCUSDT", klines, [], settings, snapshot_dir=snapshot_dir, indicators=indicators)
    assert Path(path).stat().st_size > 10_000
    with Image.open(path) as image:
        height = image.height
        image.verify()
    assert height > 1200  # distinct readable CVD, OI and liquidation panels

    heatmap = {
        "y_axis": [99.0, 100.0, 101.0, 102.0],
        "candles": [(start + i * 60_000, 100 + i * 0.02) for i in range(20)],
        "points": [[i, p, float((i + 1) * (p + 1))] for i in range(20) for p in range(4)],
    }
    before = len(plt.get_fignums())
    output = build_liquidation_heatmap("BTCUSDT", heatmap, snapshot_dir=snapshot_dir)
    assert "HEATMAP" in Path(output).name
    with Image.open(output) as image:
        image.verify()
    assert len(plt.get_fignums()) == before


def test_missing_derivative_series_does_not_break_snapshot(klines, snapshot_dir):
    path = build_snapshot(
        "ETHUSDT",
        klines,
        [],
        {"show_delta": 1, "show_oi": 1, "show_liq": 1},
        snapshot_dir=snapshot_dir,
        indicators={"cvd": [], "oi": [], "liquidations": [], "source": "unavailable"},
    )
    assert Path(path).is_file()
