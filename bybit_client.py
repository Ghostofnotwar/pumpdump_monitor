"""Public Bybit v5 REST client with timeouts, bounded retries and pagination."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import aiohttp

logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class BybitError(RuntimeError):
    pass


class BybitClient:
    def __init__(self, base_url: str = "https://api.bybit.com", timeout: float = 15, concurrency: int = 8):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(concurrency)
        self._start_lock = asyncio.Lock()

    async def start(self):
        async with self._start_lock:
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    headers={"User-Agent": "pumpdump-monitor/2", "Accept": "application/json"},
                )

    async def close(self):
        if self.session is not None:
            await self.session.close()
            self.session = None

    @staticmethod
    def _retry_delay(headers: aiohttp.typedefs.LooseHeaders, attempt: int) -> float:
        delay = min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.2)
        retry_after = headers.get("Retry-After") if headers else None
        if retry_after:
            try:
                seconds = float(retry_after)
            except ValueError:
                try:
                    date = parsedate_to_datetime(retry_after)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=UTC)
                    seconds = (date - datetime.now(UTC)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    seconds = 0
            if math.isfinite(seconds):
                delay = max(delay, min(30.0, max(0.0, seconds)))
        return delay

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if self.session is None or self.session.closed:
            raise BybitError("Bybit client is not started")
        for attempt in range(3):
            retry = False
            delay = 0.0
            try:
                async with self._semaphore:
                    async with self.session.get(f"{self.base_url}{path}", params=params or {}) as response:
                        retry = response.status == 429 or response.status >= 500
                        delay = self._retry_delay(response.headers, attempt)
                        body = await response.content.read(MAX_RESPONSE_BYTES + 1)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise BybitError(f"Bybit response too large at {path}")
                        if response.status != 200:
                            if retry and attempt < 2:
                                logger.warning("Bybit HTTP %s at %s; retry %s/2", response.status, path, attempt + 1)
                                await asyncio.sleep(delay)
                                continue
                            raise BybitError(f"Bybit HTTP {response.status} at {path}")
                        data = json.loads(body)
                if not isinstance(data, dict):
                    raise BybitError(f"Invalid Bybit response at {path}")
                code = data.get("retCode")
                if code != 0:
                    # Do not log response bodies, proxy URLs or credentials.
                    if code in (10006, 10016, 10018):
                        retry = True
                    else:
                        raise BybitError(f"Bybit API error {code} at {path}")
                if retry:
                    if attempt == 2:
                        raise BybitError(f"Bybit retries exhausted at {path}")
                    logger.warning("Bybit transient API code %s; retry %s/2", code, attempt + 1)
                    await asyncio.sleep(delay)
                    continue
                result = data.get("result")
                if not isinstance(result, dict):
                    raise BybitError(f"Missing Bybit result at {path}")
                return result
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                retry = True
                delay = self._retry_delay({}, attempt)
                if attempt == 2:
                    raise BybitError(f"Bybit request failed at {path}: {type(exc).__name__}") from exc
            if not retry:
                break
            if attempt == 2:
                raise BybitError(f"Bybit retries exhausted at {path}")
            logger.warning("Bybit transient failure at %s; retry %s/2", path, attempt + 1)
            await asyncio.sleep(delay)
        raise BybitError(f"Bybit request failed at {path}")

    @staticmethod
    def _list(result: dict, path: str) -> list:
        rows = result.get("list")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise BybitError(f"Invalid Bybit list at {path}")
        return rows

    async def get_linear_symbols(self) -> list[dict]:
        rows, cursor, seen = [], None, set()
        for _ in range(20):
            params = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = await self._get("/v5/market/instruments-info", params)
            rows.extend(self._list(result, "instruments-info"))
            cursor = result.get("nextPageCursor")
            if not cursor:
                return rows
            if cursor in seen:
                break
            seen.add(cursor)
        raise BybitError("Bybit instrument pagination did not terminate")

    async def get_tickers(self) -> list[dict]:
        return self._list(await self._get("/v5/market/tickers", {"category": "linear"}), "tickers")

    async def get_klines(self, symbol: str, interval: str, limit: int = 80) -> list:
        if not 1 <= limit <= 1000:
            raise ValueError("Kline limit must be 1..1000")
        result = await self._get(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
            },
        )
        return self._list(result, "kline")

    async def get_recent_trade(self, symbol: str, limit: int = 60) -> list:
        if not 1 <= limit <= 1000:
            raise ValueError("Trade limit must be 1..1000")
        return self._list(
            await self._get(
                "/v5/market/recent-trade",
                {
                    "category": "linear",
                    "symbol": symbol,
                    "limit": limit,
                },
            ),
            "recent-trade",
        )
