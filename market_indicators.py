"""Optional real derivatives feeds and Coinglass liquidation heatmap.

Indicator fallbacks use Binance USD-M public market data (no user credentials):
aggTrades for signed-volume CVD, openInterestHist for OI, and allForceOrders
for liquidation orders. Unavailable feeds remain unavailable; no values are made up.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import UTC, datetime

import aiohttp

logger = logging.getLogger(__name__)
BINANCE = "https://fapi.binance.com"
COINGLASS = "https://open-api-v4.coinglass.com"


def _stamp(value) -> int | None:
    try:
        value = int(value)
        # Binance timestamps are milliseconds; accept seconds in fixture/API variants.
        return value * 1000 if value < 10_000_000_000 else value
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def parse_binance_indicators(trades: list, oi_rows: list, liquidation_rows: list) -> dict:
    """Normalize independent Binance records to timestamped numeric series."""
    signed = []
    for row in trades if isinstance(trades, list) else []:
        if not isinstance(row, dict):
            continue
        ts, qty = _stamp(row.get("T", row.get("time"))), _valid_number(row.get("q", row.get("qty")))
        if ts is None or qty is None or qty < 0:
            continue
        # Buyer is maker => taker aggressor sold; otherwise taker aggressor bought.
        delta = -qty if row.get("m", row.get("isBuyerMaker", False)) else qty
        signed.append((ts, delta))
    signed.sort()
    cvd, total = [], 0.0
    for ts, delta in signed:
        total += delta
        cvd.append([ts, total])

    oi = []
    for row in oi_rows if isinstance(oi_rows, list) else []:
        if not isinstance(row, dict):
            continue
        ts = _stamp(row.get("timestamp", row.get("time")))
        value = _valid_number(row.get("sumOpenInterest", row.get("openInterest")))
        if ts is not None and value is not None and value >= 0:
            oi.append([ts, value])

    # Binance allForceOrders returns side (BUY/SELL), price and executed quantity.
    # BUY liquidations are forced buybacks of shorts; SELL are liquidated longs.
    buckets: dict[int, list[float]] = {}
    for row in liquidation_rows if isinstance(liquidation_rows, list) else []:
        if not isinstance(row, dict):
            continue
        ts = _stamp(row.get("time", row.get("T")))
        qty = _valid_number(row.get("executedQty", row.get("origQty", row.get("q"))))
        price = _valid_number(row.get("avgPrice", row.get("price", row.get("ap"))))
        if ts is None or qty is None or price is None or qty <= 0 or price <= 0:
            continue
        bucket = buckets.setdefault(ts, [0.0, 0.0])
        bucket[0 if str(row.get("side", "")).upper() == "SELL" else 1] += qty * price
    liquidations = [[ts, amounts[0], amounts[1]] for ts, amounts in sorted(buckets.items())]
    return {
        "cvd": cvd,
        "oi": sorted(oi),
        "liquidations": liquidations,
        "source": "Binance aggTrades · up to 1000 recent aggregates (partial window)",
    }


def parse_heatmap(response: dict) -> dict | None:
    """Unwrap Coinglass v4 envelopes, validating sparse [candle, price, value] tuples."""
    body = response.get("data", response) if isinstance(response, dict) else None
    if isinstance(body, list) and body:
        body = body[0]
    if not isinstance(body, dict):
        return None
    y_axis, candles, points = body.get("y_axis"), body.get("price_candlesticks"), body.get("liquidation_leverage_data")
    if not isinstance(y_axis, list) or not isinstance(candles, list) or not isinstance(points, list):
        return None
    prices = [_valid_number(value) for value in y_axis]
    if not prices or any(value is None or value <= 0 for value in prices):
        return None
    normalized_candles = []
    candle_source_indices = []
    for source_index, candle in enumerate(candles):
        if isinstance(candle, dict):
            ts = _stamp(candle.get("time", candle.get("timestamp", candle.get("t"))))
            close = _valid_number(candle.get("close", candle.get("c")))
        elif isinstance(candle, (list, tuple)) and len(candle) >= 5:
            ts, close = _stamp(candle[0]), _valid_number(candle[4])
        else:
            continue
        if ts is not None and close is not None and close > 0:
            normalized_candles.append((ts, close))
            candle_source_indices.append(source_index)
    if not normalized_candles:
        return None
    source_to_normalized = {source: index for index, source in enumerate(candle_source_indices)}
    clean_points = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) < 3:
            continue
        try:
            candle_index, price_index = int(point[0]), int(point[1])
            value = _valid_number(point[2])
        except (TypeError, ValueError, OverflowError):
            continue
        if value is not None and value >= 0 and candle_index in source_to_normalized and 0 <= price_index < len(prices):
            clean_points.append([source_to_normalized[candle_index], price_index, value])
    if not clean_points:
        return None
    # Display monotonic axes even if the provider ships price/time descending.
    price_order = sorted(range(len(prices)), key=prices.__getitem__)
    price_reindex = {old: new for new, old in enumerate(price_order)}
    candle_order = sorted(range(len(normalized_candles)), key=lambda index: normalized_candles[index][0])
    candle_reindex = {old: new for new, old in enumerate(candle_order)}
    normalized_candles = [normalized_candles[index] for index in candle_order]
    remapped = [[candle_reindex[candle], price_reindex[price], value] for candle, price, value in clean_points]
    return {"y_axis": [prices[index] for index in price_order], "candles": normalized_candles, "points": remapped}


class MarketIndicators:
    """Short-lived cached optional public feeds; missing/paid feeds never block alerts."""

    def __init__(self, coinglass_api_key: str = "", timeout: float = 12, cache_seconds: int = 300):
        self.coinglass_api_key = coinglass_api_key.strip()
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def _json(self, session, url, params=None, headers=None):
        async with session.get(url, params=params, headers=headers) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            result = await response.json(content_type=None)
            if not isinstance(result, (list, dict)):
                raise RuntimeError("Unexpected JSON response")
            return result

    async def _cached(self, key, loader):
        async with self._lock:
            entry = self._cache.get(key)
            if entry and time.monotonic() - entry[0] < self.cache_seconds:
                return entry[1]
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(loader())
                self._inflight[key] = task
        try:
            value = await asyncio.shield(task)
        except BaseException:
            async with self._lock:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)
            raise
        async with self._lock:
            self._cache[key] = (time.monotonic(), value)
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
        return value

    async def indicators(self, symbol: str, timeframe: str, limit: int = 200) -> dict:
        """Get actual CVD/OI/liquidation observations independently; failed components are empty."""
        interval = {"1": "5m", "3": "5m", "D": "1d", "60": "1h", "240": "4h"}.get(timeframe, f"{timeframe}m")
        key = ("indicators", symbol, interval)

        async def load():
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            headers = {"User-Agent": "pumpdump-monitor/2", "Accept": "application/json"}
            start = int(
                (datetime.now(UTC).timestamp() - limit * (86400 if timeframe == "D" else int(timeframe) * 60)) * 1000
            )
            params = {"symbol": symbol, "startTime": start, "limit": 1000}
            oi_params = {"symbol": symbol, "period": interval, "limit": min(500, limit)}
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:

                async def request(name, path, query):
                    try:
                        result = await self._json(session, f"{BINANCE}{path}", query)
                        if not isinstance(result, list):
                            logger.warning("%s feed returned unexpected data for %s", name, symbol)
                            return []
                        return result
                    except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
                        logger.warning("%s feed unavailable for %s (%s)", name, symbol, type(exc).__name__)
                        return []

                trades, oi, liquidations = await asyncio.gather(
                    request("Binance aggTrades", "/fapi/v1/aggTrades", params),
                    request("Binance open interest", "/futures/data/openInterestHist", oi_params),
                    request("Binance liquidations", "/fapi/v1/allForceOrders", params),
                )
                return parse_binance_indicators(trades, oi, liquidations)

        try:
            return await self._cached(key, load)
        except Exception as exc:
            logger.warning("Derivatives indicators unavailable for %s (%s)", symbol, type(exc).__name__)
            return {"cvd": [], "oi": [], "liquidations": [], "source": "unavailable"}

    async def heatmap(self, symbol: str, timeframe: str) -> dict | None:
        if not self.coinglass_api_key:
            logger.info("Coinglass heatmap disabled: COINGLASS_API_KEY is not configured")
            return None
        interval = {"1": "5m", "3": "5m", "D": "1d", "60": "1h", "240": "4h"}.get(timeframe, f"{timeframe}m")
        key = ("coinglass-heatmap-model1", symbol, interval)

        async def load():
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            params = {"exchange": "Binance", "symbol": symbol, "interval": interval}
            headers = {"CG-API-KEY": self.coinglass_api_key, "Accept": "application/json"}
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    payload = await self._json(
                        session,
                        f"{COINGLASS}/api/futures/liquidation/aggregated-heatmap/model1",
                        params,
                        headers,
                    )
                parsed = parse_heatmap(payload)
                if parsed is None:
                    logger.warning("Coinglass heatmap returned no usable data for %s", symbol)
                return parsed
            except (aiohttp.ClientError, TimeoutError, ValueError, RuntimeError) as exc:
                # Common for unavailable Professional/Enterprise plans and missing entitlements.
                logger.warning(
                    "Coinglass heatmap unavailable for %s (%s); continuing without it", symbol, type(exc).__name__
                )
                return None

        try:
            return await self._cached(key, load)
        except Exception as exc:
            logger.warning("Coinglass heatmap unavailable for %s (%s)", symbol, type(exc).__name__)
            return None
