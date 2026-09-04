"""Read-only Alpaca Market Data MCP adapter.

This module never touches Alpaca brokerage or order endpoints. Stock results
default to the single-exchange ``iex`` feed. Option results default to the free
``indicative`` feed and explicitly disclose its delayed trades and modified
quotes.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List
from urllib.parse import quote

from packages.core.ai.mcp._http import mcp_err
from packages.core.ai.mcp._market_data import (
    MarketDataClient,
    MarketDataClientFactory,
    MarketDataError,
    MarketDataProvider,
    MarketDataResultFactory,
)

logger = logging.getLogger(__name__)

_PROVIDER = MarketDataProvider.ALPACA
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,31}$")
_OPTION_CONTRACT_RE = re.compile(r"^[A-Z0-9]{1,6}\d{6}[CP]\d{8}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FEEDS = {"iex", "sip", "delayed_sip", "otc", "boats", "overnight"}
_OPTION_FEEDS = {"indicative", "opra"}
_FEED_SCOPES = {
    "iex": "single_exchange",
    "sip": "consolidated_us_exchanges",
    "delayed_sip": "consolidated_us_exchanges_delayed",
    "otc": "over_the_counter",
    "boats": "alternative_trading_system",
    "overnight": "derived_overnight",
    "indicative": "free_indicative_options",
    "opra": "official_opra_options",
}
_TIMEFRAMES = {
    "1Min",
    "5Min",
    "15Min",
    "30Min",
    "1Hour",
    "2Hour",
    "4Hour",
    "1Day",
    "1Week",
    "1Month",
}


def list_tools() -> List[Dict[str, Any]]:
    return [_tool_definition(name, spec) for name, spec in _TOOLS.items()]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        return mcp_err(f"Unknown Alpaca Market Data tool: {name}")
    if not isinstance(arguments, dict):
        return mcp_err("arguments must be an object")
    try:
        offset = MarketDataResultFactory.offset(arguments)
        client = MarketDataClientFactory.create(_PROVIDER, bearer_token)
        return MarketDataResultFactory.create(await handler(client, dict(arguments)), offset=offset)
    except MarketDataError as exc:
        return mcp_err(str(exc))
    except Exception:  # noqa: BLE001
        logger.exception("Alpaca Market Data tool %s failed", name)
        return mcp_err("Alpaca Market Data call failed unexpectedly.")


def _tool_definition(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": name,
        "description": spec["description"],
        "parameters": {
            "type": "object",
            "required": list(spec.get("required", [])),
            "properties": {
                **spec.get("properties", {}),
                "result_offset": dict(MarketDataResultFactory.OFFSET_PARAMETER),
            },
        },
    }


def _symbol(arguments: Dict[str, Any], field: str = "symbol") -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not _SYMBOL_RE.fullmatch(value.strip()):
        raise MarketDataError(f"{field} must be a valid market symbol.")
    return value.strip().upper()


def _optional_text(
    arguments: Dict[str, Any],
    field: str,
    *,
    max_length: int = 64,
) -> str | None:
    value = arguments.get(field)
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if not isinstance(value, str) or len(value) > max_length:
        raise MarketDataError(f"{field} must be a non-empty string up to {max_length} characters.")
    return value.strip()


def _choice(
    arguments: Dict[str, Any],
    field: str,
    choices: set[str],
    default: str,
) -> str:
    value = arguments.get(field, default)
    if not isinstance(value, str) or value not in choices:
        raise MarketDataError(f"{field} must be one of: {', '.join(sorted(choices))}.")
    return value


def _bounded_int(
    arguments: Dict[str, Any],
    field: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = arguments.get(field, default)
    if isinstance(value, bool):
        raise MarketDataError(f"{field} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketDataError(f"{field} must be an integer.") from exc
    if isinstance(value, float) and value != parsed:
        raise MarketDataError(f"{field} must be an integer.")
    if not minimum <= parsed <= maximum:
        raise MarketDataError(f"{field} must be between {minimum} and {maximum}.")
    return parsed


def _feed(arguments: Dict[str, Any]) -> str:
    return _choice(arguments, "feed", _FEEDS, "iex")


def _option_feed(arguments: Dict[str, Any]) -> str:
    return _choice(arguments, "feed", _OPTION_FEEDS, "indicative")


def _option_contract(arguments: Dict[str, Any]) -> str:
    value = arguments.get("contract_symbol")
    if not isinstance(value, str):
        raise MarketDataError("contract_symbol must be a valid OCC option symbol.")
    symbol = value.strip().upper()
    if not _OPTION_CONTRACT_RE.fullmatch(symbol):
        raise MarketDataError("contract_symbol must be a valid OCC option symbol.")
    return symbol


def _optional_date(arguments: Dict[str, Any], field: str) -> str | None:
    value = _optional_text(arguments, field, max_length=10)
    if value is None:
        return None
    if not _DATE_RE.fullmatch(value):
        raise MarketDataError(f"{field} must use YYYY-MM-DD format.")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise MarketDataError(f"{field} must be a valid calendar date.") from exc
    return value


def _optional_nonnegative_number(arguments: Dict[str, Any], field: str) -> float | None:
    value = arguments.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MarketDataError(f"{field} must be a non-negative number.")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise MarketDataError(f"{field} must be a non-negative number.")
    return parsed


def _with_provenance(data: Any, *, feed: str | None = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "provider": _PROVIDER.value,
        "market_data_only": True,
        "data": data,
    }
    if feed:
        result["feed"] = feed
        result["feed_scope"] = _FEED_SCOPES[feed]
        if feed == "indicative":
            result["feed_limitations"] = (
                "Free indicative options feed: trades are delayed and quotes are modified; "
                "do not describe this result as OPRA or real-time consolidated data."
            )
        elif feed == "opra":
            result["feed_limitations"] = (
                "Official OPRA options feed; a paid entitlement is required and provider "
                "timestamps determine freshness."
            )
    return result


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


async def _get_bars(client: MarketDataClient, arguments: Dict[str, Any]) -> Any:
    symbol = _symbol(arguments)
    feed = _feed(arguments)
    params = {
        "timeframe": _choice(arguments, "timeframe", _TIMEFRAMES, "1Day"),
        "start": _optional_text(arguments, "start"),
        "end": _optional_text(arguments, "end"),
        "limit": _bounded_int(arguments, "limit", 100, 1, 10_000),
        "adjustment": _choice(
            arguments,
            "adjustment",
            {"raw", "split", "dividend", "all"},
            "all",
        ),
        "feed": feed,
    }
    data = await client.get(f"/v2/stocks/{quote(symbol, safe='')}/bars", params=params)
    return _with_provenance(data, feed=feed)


async def _get_latest_quote(
    client: MarketDataClient,
    arguments: Dict[str, Any],
) -> Any:
    symbol = _symbol(arguments)
    feed = _feed(arguments)
    data = await client.get(
        f"/v2/stocks/{quote(symbol, safe='')}/quotes/latest",
        params={"feed": feed},
    )
    return _with_provenance(data, feed=feed)


async def _get_latest_trade(
    client: MarketDataClient,
    arguments: Dict[str, Any],
) -> Any:
    symbol = _symbol(arguments)
    feed = _feed(arguments)
    data = await client.get(
        f"/v2/stocks/{quote(symbol, safe='')}/trades/latest",
        params={"feed": feed},
    )
    return _with_provenance(data, feed=feed)


async def _get_snapshot(client: MarketDataClient, arguments: Dict[str, Any]) -> Any:
    symbol = _symbol(arguments)
    feed = _feed(arguments)
    data = await client.get(
        f"/v2/stocks/{quote(symbol, safe='')}/snapshot",
        params={"feed": feed},
    )
    return _with_provenance(data, feed=feed)


async def _get_news(client: MarketDataClient, arguments: Dict[str, Any]) -> Any:
    raw_symbols = arguments.get("symbols")
    symbols: list[str] = []
    if raw_symbols is not None:
        if not isinstance(raw_symbols, list) or not 1 <= len(raw_symbols) <= 10:
            raise MarketDataError("symbols must be an array containing 1-10 symbols.")
        symbols = [_symbol({"symbol": value}) for value in raw_symbols]
    include_content = arguments.get("include_content", False)
    if not isinstance(include_content, bool):
        raise MarketDataError("include_content must be a boolean.")
    start = _optional_text(arguments, "start")
    end = _optional_text(arguments, "end")
    lookback_hours = None
    if arguments.get("lookback_hours") is not None:
        lookback_hours = _bounded_int(arguments, "lookback_hours", 24, 1, 168)
        if start is not None or end is not None:
            raise MarketDataError("lookback_hours cannot be combined with start or end.")
    observed_at = _utc_now()
    if lookback_hours is not None:
        end = _rfc3339_utc(observed_at)
        start = _rfc3339_utc(observed_at - timedelta(hours=lookback_hours))
    params = {
        "symbols": ",".join(symbols) if symbols else None,
        "start": start,
        "end": end,
        "limit": _bounded_int(arguments, "limit", 10, 1, 50),
        "sort": _choice(arguments, "sort", {"asc", "desc"}, "desc"),
        "include_content": str(include_content).lower(),
    }
    data = await client.get("/v1beta1/news", params=params)
    result = _with_provenance(data)
    result["observed_at"] = _rfc3339_utc(observed_at)
    result["request_window"] = {"start": start, "end": end}
    if lookback_hours is not None:
        result["request_window"]["lookback_hours"] = lookback_hours
    return result


async def _get_option_chain(client: MarketDataClient, arguments: Dict[str, Any]) -> Any:
    underlying_symbol = _symbol(arguments, "underlying_symbol")
    feed = _option_feed(arguments)
    strike_price_gte = _optional_nonnegative_number(arguments, "strike_price_gte")
    strike_price_lte = _optional_nonnegative_number(arguments, "strike_price_lte")
    if (
        strike_price_gte is not None
        and strike_price_lte is not None
        and strike_price_gte > strike_price_lte
    ):
        raise MarketDataError("strike_price_gte must be less than or equal to strike_price_lte.")
    expiration_date_gte = _optional_date(arguments, "expiration_date_gte")
    expiration_date_lte = _optional_date(arguments, "expiration_date_lte")
    if (
        expiration_date_gte is not None
        and expiration_date_lte is not None
        and expiration_date_gte > expiration_date_lte
    ):
        raise MarketDataError("expiration_date_gte must be before or equal to expiration_date_lte.")
    params = {
        "feed": feed,
        "limit": _bounded_int(arguments, "limit", 100, 1, 1_000),
        "updated_since": _optional_text(arguments, "updated_since"),
        "page_token": _optional_text(arguments, "page_token", max_length=512),
        "type": _choice(arguments, "option_type", {"call", "put"}, "")
        if arguments.get("option_type") not in (None, "")
        else None,
        "strike_price_gte": strike_price_gte,
        "strike_price_lte": strike_price_lte,
        "expiration_date": _optional_date(arguments, "expiration_date"),
        "expiration_date_gte": expiration_date_gte,
        "expiration_date_lte": expiration_date_lte,
        "root_symbol": _optional_text(arguments, "root_symbol", max_length=16),
    }
    data = await client.get(
        f"/v1beta1/options/snapshots/{quote(underlying_symbol, safe='')}",
        params=params,
    )
    return _with_provenance(data, feed=feed)


async def _get_option_snapshot(client: MarketDataClient, arguments: Dict[str, Any]) -> Any:
    contract_symbol = _option_contract(arguments)
    feed = _option_feed(arguments)
    data = await client.get(
        "/v1beta1/options/snapshots",
        params={
            "symbols": contract_symbol,
            "feed": feed,
            "updated_since": _optional_text(arguments, "updated_since"),
        },
    )
    return _with_provenance(data, feed=feed)


async def _get_option_latest_quote(client: MarketDataClient, arguments: Dict[str, Any]) -> Any:
    contract_symbol = _option_contract(arguments)
    feed = _option_feed(arguments)
    data = await client.get(
        "/v1beta1/options/quotes/latest",
        params={"symbols": contract_symbol, "feed": feed},
    )
    return _with_provenance(data, feed=feed)


_COMMON_SYMBOL_PROPERTIES = {
    "symbol": {
        "type": "string",
        "description": "US market symbol, for example AAPL.",
    },
    "feed": {
        "type": "string",
        "enum": sorted(_FEEDS),
        "description": "Defaults to iex; iex is a single-exchange feed.",
    },
}

_OPTION_FEED_PROPERTY = {
    "type": "string",
    "enum": sorted(_OPTION_FEEDS),
    "description": (
        "Defaults to indicative, the free feed with delayed trades and modified quotes. "
        "OPRA requires a paid entitlement."
    ),
}

_OPTION_CONTRACT_PROPERTIES = {
    "contract_symbol": {
        "type": "string",
        "description": "OCC option contract symbol, for example GLD270115C00400000.",
    },
    "feed": dict(_OPTION_FEED_PROPERTY),
}

_TOOLS: Dict[str, Dict[str, Any]] = {
    "get_bars": {
        "description": "Get read-only historical OHLCV bars for one US stock.",
        "required": ["symbol"],
        "properties": {
            **_COMMON_SYMBOL_PROPERTIES,
            "timeframe": {"type": "string", "enum": sorted(_TIMEFRAMES)},
            "start": {"type": "string", "description": "Optional RFC3339 start."},
            "end": {"type": "string", "description": "Optional RFC3339 end."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10_000},
            "adjustment": {
                "type": "string",
                "enum": ["all", "dividend", "raw", "split"],
            },
        },
    },
    "get_latest_quote": {
        "description": "Get the latest read-only bid/ask quote for one US stock.",
        "required": ["symbol"],
        "properties": dict(_COMMON_SYMBOL_PROPERTIES),
    },
    "get_latest_trade": {
        "description": "Get the latest read-only trade for one US stock.",
        "required": ["symbol"],
        "properties": dict(_COMMON_SYMBOL_PROPERTIES),
    },
    "get_snapshot": {
        "description": "Get latest trade, quote, minute/day bars, and prior day bar.",
        "required": ["symbol"],
        "properties": dict(_COMMON_SYMBOL_PROPERTIES),
    },
    "get_news": {
        "description": (
            "Get read-only market news, optionally filtered by symbols. Use lookback_hours "
            "for a server-timed latest-news window."
        ),
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
            },
            "start": {"type": "string", "description": "Optional RFC3339 start."},
            "end": {"type": "string", "description": "Optional RFC3339 end."},
            "lookback_hours": {
                "type": "integer",
                "minimum": 1,
                "maximum": 168,
                "description": (
                    "Closed lookback window ending at the adapter's current UTC request time; "
                    "cannot be combined with start or end. Use 24 for latest news."
                ),
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "sort": {"type": "string", "enum": ["asc", "desc"]},
            "include_content": {"type": "boolean"},
        },
    },
    "get_option_chain": {
        "description": (
            "Search the read-only US listed option chain for an underlying such as GLD or IAU, "
            "including latest trade, quote, implied volatility, and Greeks when available."
        ),
        "required": ["underlying_symbol"],
        "properties": {
            "underlying_symbol": {
                "type": "string",
                "description": "US option underlying symbol, for example GLD or IAU.",
            },
            "feed": dict(_OPTION_FEED_PROPERTY),
            "option_type": {"type": "string", "enum": ["call", "put"]},
            "strike_price_gte": {"type": "number", "minimum": 0},
            "strike_price_lte": {"type": "number", "minimum": 0},
            "expiration_date": {
                "type": "string",
                "description": "Exact expiration date in YYYY-MM-DD format.",
            },
            "expiration_date_gte": {
                "type": "string",
                "description": "Earliest expiration date in YYYY-MM-DD format.",
            },
            "expiration_date_lte": {
                "type": "string",
                "description": "Latest expiration date in YYYY-MM-DD format.",
            },
            "updated_since": {
                "type": "string",
                "description": "Optional RFC3339 timestamp or YYYY-MM-DD date.",
            },
            "root_symbol": {
                "type": "string",
                "description": "Optional adjusted-contract root symbol filter.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 1_000},
            "page_token": {
                "type": "string",
                "description": "Provider next_page_token from a prior request.",
            },
        },
    },
    "get_option_snapshot": {
        "description": (
            "Get a read-only snapshot for one US listed option contract, including latest trade, "
            "quote, implied volatility, and Greeks when available."
        ),
        "required": ["contract_symbol"],
        "properties": {
            **_OPTION_CONTRACT_PROPERTIES,
            "updated_since": {
                "type": "string",
                "description": "Optional RFC3339 timestamp or YYYY-MM-DD date.",
            },
        },
    },
    "get_option_latest_quote": {
        "description": "Get the latest read-only bid/ask quote for one US listed option contract.",
        "required": ["contract_symbol"],
        "properties": dict(_OPTION_CONTRACT_PROPERTIES),
    },
}

_Handler = Callable[[MarketDataClient, Dict[str, Any]], Awaitable[Any]]
_HANDLERS: Dict[str, _Handler] = {
    "get_bars": _get_bars,
    "get_latest_quote": _get_latest_quote,
    "get_latest_trade": _get_latest_trade,
    "get_snapshot": _get_snapshot,
    "get_news": _get_news,
    "get_option_chain": _get_option_chain,
    "get_option_snapshot": _get_option_snapshot,
    "get_option_latest_quote": _get_option_latest_quote,
}
