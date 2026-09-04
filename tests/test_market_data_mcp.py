"""Contract tests for the read-only market-data MCP adapters."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from packages.core.ai.mcp import alpaca_market_data, alpha_vantage, twelve_data
from packages.core.ai.mcp import _market_data as market_data_common
from packages.core.ai.mcp._market_data import (
    MarketDataClientFactory,
    MarketDataError,
    MarketDataProvider,
    MarketDataResultFactory,
)


def _mock_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(market_data_common.httpx, "AsyncClient", factory)


def _result_text(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize(
    "module",
    [alpaca_market_data, alpha_vantage, twelve_data],
)
def test_market_data_schema_handler_parity(module) -> None:
    assert {tool["name"] for tool in module.list_tools()} == set(module._HANDLERS)
    for tool in module.list_tools():
        assert tool["parameters"]["properties"]["result_offset"]["type"] == "integer"


def test_alpaca_uses_json_credential_routing() -> None:
    from packages.core.ai.tools.mcp_builtin import _JSON_BLOB_PROVIDERS

    assert "alpaca_market_data" in _JSON_BLOB_PROVIDERS


@pytest.mark.asyncio
async def test_alpaca_news_lookback_uses_server_time_and_returns_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_at = datetime(2026, 9, 1, 19, 24, 33, tzinfo=timezone.utc)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"news": [], "next_page_token": None})

    monkeypatch.setattr(alpaca_market_data, "_utc_now", lambda: observed_at)
    _mock_http(monkeypatch, handler)
    result = await alpaca_market_data.call_tool(
        "get_news",
        {"symbols": ["AAPL"], "lookback_hours": 24},
        json.dumps({"api_key": "key-id", "api_secret": "secret-value"}),
    )

    assert result["isError"] is False
    assert len(captured) == 1
    request = captured[0]
    assert request.url.params["start"] == "2026-08-31T19:24:33Z"
    assert request.url.params["end"] == "2026-09-01T19:24:33Z"
    payload = _result_text(result)
    assert payload["observed_at"] == "2026-09-01T19:24:33Z"
    assert payload["request_window"] == {
        "start": "2026-08-31T19:24:33Z",
        "end": "2026-09-01T19:24:33Z",
        "lookback_hours": 24,
    }


@pytest.mark.asyncio
async def test_alpaca_news_rejects_lookback_with_explicit_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedClient:
        def __init__(self, **_kwargs):
            raise AssertionError("invalid news window must not call the provider")

    monkeypatch.setattr(market_data_common.httpx, "AsyncClient", UnexpectedClient)
    result = await alpaca_market_data.call_tool(
        "get_news",
        {"start": "2026-09-01T00:00:00Z", "lookback_hours": 24},
        json.dumps({"api_key": "key-id", "api_secret": "secret-value"}),
    )

    assert result["isError"] is True
    assert "cannot be combined" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_alpaca_quote_uses_both_credentials_and_labels_iex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"quote": {"ap": 123.45}})

    _mock_http(monkeypatch, handler)
    result = await alpaca_market_data.call_tool(
        "get_latest_quote",
        {"symbol": "aapl"},
        json.dumps({"api_key": "key-id", "api_secret": "secret-value"}),
    )

    assert result["isError"] is False
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/v2/stocks/AAPL/quotes/latest"
    assert request.url.params["feed"] == "iex"
    assert request.headers["APCA-API-KEY-ID"] == "key-id"
    assert request.headers["APCA-API-SECRET-KEY"] == "secret-value"
    payload = _result_text(result)
    assert payload["feed_scope"] == "single_exchange"
    assert payload["market_data_only"] is True


@pytest.mark.asyncio
async def test_alpaca_gold_option_chain_uses_native_filters_and_labels_indicative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "snapshots": {
                    "GLD260116C00400000": {
                        "latestQuote": {"bp": 7.1, "ap": 7.4},
                        "impliedVolatility": 0.21,
                        "greeks": {"delta": 0.52},
                    }
                },
                "next_page_token": "next-native-page",
            },
        )

    _mock_http(monkeypatch, handler)
    result = await alpaca_market_data.call_tool(
        "get_option_chain",
        {
            "underlying_symbol": "gld",
            "option_type": "call",
            "strike_price_gte": 390,
            "strike_price_lte": 410,
            "expiration_date": "2026-01-16",
            "limit": 25,
        },
        json.dumps({"api_key": "key-id", "api_secret": "secret-value"}),
    )

    assert result["isError"] is False
    request = captured[0]
    assert request.url.path == "/v1beta1/options/snapshots/GLD"
    assert request.url.params["feed"] == "indicative"
    assert request.url.params["type"] == "call"
    assert request.url.params["strike_price_gte"] == "390.0"
    assert request.url.params["strike_price_lte"] == "410.0"
    assert request.url.params["expiration_date"] == "2026-01-16"
    assert request.url.params["limit"] == "25"
    payload = _result_text(result)
    assert payload["feed"] == "indicative"
    assert payload["feed_scope"] == "free_indicative_options"
    assert "trades are delayed and quotes are modified" in payload["feed_limitations"]
    assert "GLD260116C00400000" in payload["data"]["snapshots"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "expected_path", "response_key"),
    [
        ("get_option_snapshot", "/v1beta1/options/snapshots", "snapshots"),
        ("get_option_latest_quote", "/v1beta1/options/quotes/latest", "quotes"),
    ],
)
async def test_alpaca_single_option_reads_use_occ_symbol_and_requested_feed(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    expected_path: str,
    response_key: str,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={response_key: {"GLD260116P00350000": {}}})

    _mock_http(monkeypatch, handler)
    result = await alpaca_market_data.call_tool(
        tool,
        {"contract_symbol": "gld260116p00350000", "feed": "opra"},
        json.dumps({"api_key": "key-id", "api_secret": "secret-value"}),
    )

    assert result["isError"] is False
    assert captured[0].url.path == expected_path
    assert captured[0].url.params["symbols"] == "GLD260116P00350000"
    assert captured[0].url.params["feed"] == "opra"
    payload = _result_text(result)
    assert payload["feed_scope"] == "official_opra_options"
    assert "paid entitlement" in payload["feed_limitations"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("get_option_snapshot", {"contract_symbol": "GLD-CALL"}, "contract_symbol"),
        (
            "get_option_chain",
            {"underlying_symbol": "GLD", "expiration_date": "2026-02-30"},
            "calendar date",
        ),
        (
            "get_option_chain",
            {"underlying_symbol": "GLD", "strike_price_gte": 410, "strike_price_lte": 390},
            "strike_price_gte",
        ),
    ],
)
async def test_alpaca_rejects_invalid_option_queries_before_http(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict,
    message: str,
) -> None:
    class UnexpectedClient:
        def __init__(self, **_kwargs):
            raise AssertionError("HTTP must not start for invalid option arguments")

    monkeypatch.setattr(market_data_common.httpx, "AsyncClient", UnexpectedClient)
    result = await alpaca_market_data.call_tool(
        tool,
        arguments,
        json.dumps({"api_key": "key-id", "api_secret": "secret-value"}),
    )

    assert result["isError"] is True
    assert message in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_alpaca_rejects_missing_secret_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedClient:
        def __init__(self, **_kwargs):
            raise AssertionError("HTTP must not start for malformed credentials")

    monkeypatch.setattr(market_data_common.httpx, "AsyncClient", UnexpectedClient)
    result = await alpaca_market_data.call_tool(
        "get_snapshot",
        {"symbol": "AAPL"},
        json.dumps({"api_key": "key-id"}),
    )

    assert result["isError"] is True
    assert "api_secret" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_alpha_vantage_query_auth_and_function_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"quarterlyReports": []})

    _mock_http(monkeypatch, handler)
    result = await alpha_vantage.call_tool(
        "get_fundamentals",
        {"symbol": "MSFT", "statement": "cash_flow"},
        "alpha-key",
    )

    assert result["isError"] is False
    params = captured[0].url.params
    assert params["apikey"] == "alpha-key"
    assert params["function"] == "CASH_FLOW"
    assert params["symbol"] == "MSFT"
    assert _result_text(result)["dataset"] == "cash_flow"


@pytest.mark.asyncio
async def test_factory_authentication_cannot_be_overridden_by_request_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    _mock_http(monkeypatch, handler)
    client = MarketDataClientFactory.create(
        MarketDataProvider.ALPHA_VANTAGE,
        "authoritative-key",
    )
    await client.get("/query", params={"apikey": "untrusted-override"})

    assert captured[0].url.params["apikey"] == "authoritative-key"


@pytest.mark.asyncio
async def test_alpha_vantage_200_error_is_error_and_redacts_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"Note": "Rate limit reached for alpha-secret-key"},
        )

    _mock_http(monkeypatch, handler)
    result = await alpha_vantage.call_tool(
        "get_quote",
        {"symbol": "IBM"},
        "alpha-secret-key",
    )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "Rate limit" in text
    assert "alpha-secret-key" not in text
    assert "[redacted]" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adjusted", "expected_function"),
    [(None, "TIME_SERIES_DAILY"), (True, "TIME_SERIES_DAILY_ADJUSTED")],
)
async def test_alpha_vantage_daily_defaults_to_non_premium_raw_data(
    monkeypatch: pytest.MonkeyPatch,
    adjusted: bool | None,
    expected_function: str,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"Time Series (Daily)": {}})

    _mock_http(monkeypatch, handler)
    arguments: dict[str, object] = {"symbol": "IBM"}
    if adjusted is not None:
        arguments["adjusted"] = adjusted
    result = await alpha_vantage.call_tool(
        "get_daily_series",
        arguments,
        "alpha-key",
    )

    assert result["isError"] is False
    assert captured[0].url.params["function"] == expected_function


@pytest.mark.asyncio
async def test_twelve_data_time_series_is_bounded_and_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"values": []})

    _mock_http(monkeypatch, handler)
    result = await twelve_data.call_tool(
        "get_time_series",
        {"symbol": "BTC/USD", "interval": "1h", "outputsize": 250},
        "twelve-key",
    )

    assert result["isError"] is False
    params = captured[0].url.params
    assert captured[0].url.path == "/time_series"
    assert params["symbol"] == "BTC/USD"
    assert params["interval"] == "1h"
    assert params["outputsize"] == "250"
    assert params["apikey"] == "twelve-key"


@pytest.mark.asyncio
async def test_twelve_data_ignores_blank_optional_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"symbol": "AAPL", "close": "100"})

    _mock_http(monkeypatch, handler)
    result = await twelve_data.call_tool(
        "get_quote",
        {"symbol": "AAPL", "exchange": "", "country": "   "},
        "twelve-key",
    )

    assert result["isError"] is False
    params = captured[0].url.params
    assert "exchange" not in params
    assert "country" not in params


@pytest.mark.asyncio
async def test_twelve_data_rejects_unbounded_or_unknown_indicator_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedClient:
        def __init__(self, **_kwargs):
            raise AssertionError("HTTP must not start for invalid arguments")

    monkeypatch.setattr(market_data_common.httpx, "AsyncClient", UnexpectedClient)
    result = await twelve_data.call_tool(
        "get_technical_indicator",
        {
            "symbol": "AAPL",
            "interval": "1day",
            "indicator": "arbitrary_endpoint",
        },
        "twelve-key",
    )

    assert result["isError"] is True
    assert "indicator" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_twelve_data_macd_uses_macd_period_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"values": []})

    _mock_http(monkeypatch, handler)
    result = await twelve_data.call_tool(
        "get_technical_indicator",
        {
            "symbol": "MSFT",
            "interval": "1day",
            "indicator": "macd",
            "fast_period": 10,
            "slow_period": 24,
            "signal_period": 8,
        },
        "twelve-key",
    )

    assert result["isError"] is False
    params = captured[0].url.params
    assert captured[0].url.path == "/macd"
    assert params["fast_period"] == "10"
    assert params["slow_period"] == "24"
    assert params["signal_period"] == "8"
    assert "time_period" not in params


@pytest.mark.asyncio
async def test_twelve_data_200_provider_error_is_mcp_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "error", "code": 429, "message": "quota exhausted"},
        )

    _mock_http(monkeypatch, handler)
    result = await twelve_data.call_tool(
        "get_price",
        {"symbol": "AAPL"},
        "twelve-key",
    )

    assert result["isError"] is True
    assert "quota exhausted" in result["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "tool", "credentials"),
    [
        (alpaca_market_data, "get_latest_trade", "{}"),
        (alpha_vantage, "get_quote", "alpha-key"),
        (twelve_data, "get_quote", "twelve-key"),
    ],
)
async def test_market_data_rejects_non_object_arguments(
    module,
    tool: str,
    credentials: str,
) -> None:
    result = await module.call_tool(tool, [], credentials)
    assert result["isError"] is True
    assert "object" in result["content"][0]["text"]


def test_market_data_worker_logs_redact_query_credentials() -> None:
    """Exercise worker boot without the API's logging setup hiding the leak."""
    script = textwrap.dedent("""
        import asyncio
        import io
        from unittest.mock import patch
        import httpx
        from packages.core.celery_app import celery_app
        from packages.core.ai.mcp import alpha_vantage, twelve_data

        stream = io.StringIO()
        celery_app.log.setup_logging_subsystem(loglevel="INFO", logfile=stream)
        real_client = httpx.AsyncClient

        async def main():
            for module, key in (
                (alpha_vantage, "review-only-alpha-key"),
                (twelve_data, "review-only-twelve-key"),
            ):
                transport = httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"price": "123.45"})
                )
                with patch(
                    "packages.core.ai.mcp._market_data.httpx.AsyncClient",
                    lambda **kwargs: real_client(transport=transport, **kwargs),
                ):
                    result = await module.call_tool("get_quote", {"symbol": "IBM"}, key)
                assert not result["isError"]
                assert key not in stream.getvalue()
            assert stream.getvalue().count("HTTP Request") == 2
            assert "<redacted>" in stream.getvalue()

        asyncio.run(main())
    """)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 429])
@pytest.mark.parametrize("nested", [False, True])
async def test_market_data_redacts_before_error_truncation(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    nested: bool,
) -> None:
    secret = "review-only-secret-crossing-the-error-boundary"
    message = "x" * 385 + secret + " quota exceeded"
    payload = {"error": {"message": message}} if nested else {"Note": message}
    module = twelve_data if nested else alpha_vantage
    if nested:
        payload["status"] = "error"
    _mock_http(monkeypatch, lambda request: httpx.Response(status, json=payload))

    result = await module.call_tool("get_quote", {"symbol": "IBM"}, secret)

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert secret[:15] not in text
    assert "[redacted]" in text
    assert len(text) <= 500


def _daily_bars(count: int = 100) -> dict:
    return {
        (date(2026, 8, 30) - timedelta(days=index)).isoformat(): {
            "1. open": "123.4567",
            "2. high": "127.1234",
            "3. low": "120.9876",
            "4. close": "125.4567",
            "5. volume": "12345678",
        }
        for index in range(count)
    }


@pytest.mark.asyncio
async def test_market_data_daily_pages_are_complete_json_and_lossless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.core.ai.agentic_loop import _compact_tool_result_for_context
    from packages.core.ai.tools.mcp_builtin import _mcp_tool_result_to_text

    bars = _daily_bars()
    metadata = {"2. Symbol": "IBM"}
    _mock_http(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={"Meta Data": metadata, "Time Series (Daily)": bars},
        ),
    )
    collected: dict = {}
    offset = 0
    for _ in range(100):
        result = await alpha_vantage.call_tool(
            "get_daily_series",
            {"symbol": "IBM", "result_offset": offset},
            "review-only-key",
        )
        assert result["isError"] is False
        text = result["content"][0]["text"]
        page = json.loads(text)
        assert len(text) <= MarketDataResultFactory.MAX_CHARS
        assert json.loads(_compact_tool_result_for_context("mcp__alpha_vantage__get_daily_series", text)) == page
        assert result["structuredContent"] == page
        assert _mcp_tool_result_to_text(result).structured_content == page
        assert page["data"]["Meta Data"] == metadata
        records = page["data"]["Time Series (Daily)"]
        assert records and not collected.keys() & records.keys()
        collected.update(records)
        next_offset = page.get("pagination", {}).get("next_offset")
        if next_offset is None:
            break
        assert next_offset > offset
        offset = next_offset
    else:
        pytest.fail("pagination did not terminate")
    assert collected == bars


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-1, True, "1", 1.5, 1_000_001])
async def test_market_data_rejects_invalid_result_offset_before_http(
    monkeypatch: pytest.MonkeyPatch,
    offset,
) -> None:
    class UnexpectedClient:
        def __init__(self, **_kwargs):
            raise AssertionError("invalid pagination must not call the provider")

    monkeypatch.setattr(market_data_common.httpx, "AsyncClient", UnexpectedClient)
    result = await alpha_vantage.call_tool(
        "get_daily_series",
        {"symbol": "IBM", "result_offset": offset},
        "review-only-key",
    )
    assert result["isError"] is True
    assert "result_offset" in result["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "tool", "arguments", "credentials", "collection_keys"),
    [
        (
            alpaca_market_data,
            "get_bars",
            {"symbol": "IBM"},
            '{"api_key":"review-key","api_secret":"review-secret"}',
            ("bars",),
        ),
        (
            alpaca_market_data,
            "get_option_chain",
            {"underlying_symbol": "GLD"},
            '{"api_key":"review-key","api_secret":"review-secret"}',
            ("snapshots",),
        ),
        (alpaca_market_data, "get_news", {}, '{"api_key":"review-key","api_secret":"review-secret"}', ("news",)),
        (twelve_data, "get_time_series", {"symbol": "IBM"}, "review-key", ("values",)),
        (twelve_data, "get_technical_indicator", {"symbol": "IBM"}, "review-key", ("values",)),
        (alpha_vantage, "get_fundamentals", {"symbol": "IBM"}, "review-key", ("annualReports", "quarterlyReports")),
        (
            alpha_vantage,
            "get_fundamentals",
            {"symbol": "IBM", "statement": "earnings"},
            "review-key",
            ("annualEarnings", "quarterlyEarnings"),
        ),
        (alpha_vantage, "search_symbols", {"keywords": "IBM"}, "review-key", ("bestMatches",)),
    ],
)
async def test_market_data_pages_preserve_every_collection_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
    module,
    tool: str,
    arguments: dict,
    credentials: str,
    collection_keys: tuple[str, ...],
) -> None:
    from packages.core.ai.agentic_loop import _compact_tool_result_for_context

    source = {
        key: [{"id": index, "description": "测试" * 200} for index in range(40 if i == 0 else 17)]
        for i, key in enumerate(collection_keys)
    }
    source.update({"symbol": "IBM", "next_page_token": "native-page-token"})
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=source)

    _mock_http(monkeypatch, handler)
    collected = {key: [] for key in collection_keys}
    offset = 0
    for _ in range(50):
        result = await module.call_tool(tool, {**arguments, "result_offset": offset}, credentials)
        assert result["isError"] is False
        page = _result_text(result)
        assert len(result["content"][0]["text"]) <= MarketDataResultFactory.MAX_CHARS
        assert result["structuredContent"] == page
        assert (
            json.loads(
                _compact_tool_result_for_context(f"mcp__{page['provider']}__{tool}", result["content"][0]["text"])
            )
            == page
        )
        assert page["data"]["symbol"] == "IBM"
        assert page["data"]["next_page_token"] == "native-page-token"
        for key in collection_keys:
            collected[key].extend(page["data"][key])
        next_offset = page["pagination"]["next_offset"]
        if next_offset is None:
            break
        assert next_offset > offset
        offset = next_offset
    else:
        pytest.fail("pagination did not terminate")
    assert len(captured) > 1
    assert all("result_offset" not in request.url.params for request in captured)
    assert collected == {key: source[key] for key in collection_keys}


@pytest.mark.parametrize(
    "data",
    [{"description": "x" * 12_001}, {"news": [{"content": "x" * 12_001}]}],
)
def test_market_data_oversized_indivisible_result_fails_explicitly(data) -> None:
    with pytest.raises(MarketDataError, match="response limit"):
        MarketDataResultFactory.create({"provider": "test", "data": data})


def test_market_data_rejects_offset_beyond_current_response() -> None:
    with pytest.raises(MarketDataError, match="outside"):
        MarketDataResultFactory.create({"data": {"values": [{"price": "123"}]}}, offset=1)


def test_market_data_redacts_longer_overlapping_credentials_first() -> None:
    client = MarketDataClientFactory.create(
        MarketDataProvider.ALPACA,
        json.dumps({"api_key": "review-key", "api_secret": "review-key-sensitive-suffix"}),
    )
    assert client._redact("review-key-sensitive-suffix and review-key") == "[redacted] and [redacted]"
