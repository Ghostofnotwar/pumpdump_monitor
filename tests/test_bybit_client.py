import pytest
from aiohttp import web

from bybit_client import BybitClient, BybitError


async def test_retry_rate_limit_and_api_codes():
    calls = []

    async def handler(request):
        calls.append(request.path)
        if len(calls) == 1:
            return web.json_response({"message": "rate limited"}, status=429, headers={"Retry-After": "0"})
        if len(calls) == 2:
            return web.json_response({"retCode": 10006, "result": {}})
        return web.json_response({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT"}]}})

    app = web.Application()
    app.router.add_get("/v5/market/tickers", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = BybitClient(f"http://127.0.0.1:{port}", timeout=2)
    try:
        await client.start()
        session = client.session
        await client.start()
        assert client.session is session
        assert await client.get_tickers() == [{"symbol": "BTCUSDT"}]
        assert len(calls) == 3
    finally:
        await client.close()
        await runner.cleanup()
    assert client.session is None


async def test_pagination_and_bad_responses():
    async def handler(request):
        if request.query.get("cursor") == "next":
            return web.json_response({"retCode": 0, "result": {"list": [{"symbol": "ETHUSDT"}], "nextPageCursor": ""}})
        if request.path.endswith("tickers"):
            return web.json_response({"retCode": 10001, "retMsg": "secret upstream body"})
        return web.json_response({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT"}], "nextPageCursor": "next"}})

    app = web.Application()
    app.router.add_get("/v5/market/instruments-info", handler)
    app.router.add_get("/v5/market/tickers", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    client = BybitClient(f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}", timeout=2)
    try:
        await client.start()
        assert [row["symbol"] for row in await client.get_linear_symbols()] == ["BTCUSDT", "ETHUSDT"]
        with pytest.raises(BybitError, match="10001") as error:
            await client.get_tickers()
        assert "secret upstream body" not in str(error.value)
    finally:
        await client.close()
        await runner.cleanup()


async def test_timeout_raises_instead_of_empty_success(monkeypatch):
    client = BybitClient()
    await client.start()

    def timeout(*args, **kwargs):
        raise TimeoutError

    async def no_delay(*_):
        return None

    monkeypatch.setattr(client.session, "get", timeout)
    monkeypatch.setattr("bybit_client.asyncio.sleep", no_delay)
    try:
        with pytest.raises(BybitError, match="TimeoutError"):
            await client.get_tickers()
    finally:
        await client.close()


async def test_closed_client_requires_start():
    with pytest.raises(BybitError, match="not started"):
        await BybitClient().get_tickers()
