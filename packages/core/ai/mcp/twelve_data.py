"""Read-only Twelve Data market-data MCP adapter."""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Dict, List

from packages.core.ai.mcp._http import mcp_err
from packages.core.ai.mcp._market_data import (
    MarketDataClient,
    MarketDataClientFactory,
    MarketDataError,
    MarketDataProvider,
    MarketDataResultFactory,
)

logger = logging.getLogger(__name__)

_PROVIDER = MarketDataProvider.TWELVE_DATA
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,31}$")
_INTERVALS = {
    "1min",
    "5min",
    "15min",
    "30min",
    "45min",
    "1h",
    "2h",
    "4h",
    "8h",
    "1day",
    "1week",
    "1month",
}
_INDICATORS = {"sma", "ema", "rsi", "macd", "bbands"}
_MA_TYPES = {
    "SMA",
    "EMA",
    "WMA",
    "DEMA",
    "TEMA",
    "TRIMA",
    "KAMA",
    "MAMA",
    "T3MA",
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
        return mcp_err(f"Unknown Twelve Data tool: {name}")
    if not isinstance(arguments, dict):
        return mcp_err("arguments must be an object")
    try:
        offset = MarketDataResultFactory.offset(arguments)
        client = MarketDataClientFactory.create(_PROVIDER, bearer_token)
        return MarketDataResultFactory.create(await handler(client, dict(arguments)), offset=offset)
    except MarketDataError as exc:
        return mcp_err(str(exc))
    except Exception:  # noqa: BLE001
        logger.exception("Twelve Data tool %s failed", name)
        return mcp_err("Twelve Data call failed unexpectedly.")


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


def _symbol(arguments: Dict[str, Any]) -> str:
    value = arguments.get("symbol")
    if not isinstance(value, str) or not _SYMBOL_RE.fullmatch(value.strip()):
        raise MarketDataError("symbol must be a valid market symbol.")
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


def _bounded_float(
    arguments: Dict[str, Any],
    field: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = arguments.get(field, default)
    if isinstance(value, bool):
        raise MarketDataError(f"{field} must be a number.")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketDataError(f"{field} must be a number.") from exc
    if not minimum <= parsed <= maximum:
        raise MarketDataError(f"{field} must be between {minimum} and {maximum}.")
    return parsed


def _base_params(arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "symbol": _symbol(arguments),
        "exchange": _optional_text(arguments, "exchange", max_length=32),
        "country": _optional_text(arguments, "country", max_length=64),
    }


def _result(data: Any, *, dataset: str) -> Dict[str, Any]:
    return {
        "provider": _PROVIDER.value,
        "market_data_only": True,
        "dataset": dataset,
        "data": data,
    }


async def _get_price(client: MarketDataClient, arguments: Dict[str, Any]) -> Any:
    data = await client.get("/price", params=_base_params(arguments))
    return _result(data, dataset="price")


async def _get_quote(client: MarketDataClient, arguments: Dict[str, Any]) -> Any:
    data = await client.get("/quote", params=_base_params(arguments))
    return _result(data, dataset="quote")


async def _get_time_series(
    client: MarketDataClient,
    arguments: Dict[str, Any],
) -> Any:
    params = {
        **_base_params(arguments),
        "interval": _choice(arguments, "interval", _INTERVALS, "1day"),
        "outputsize": _bounded_int(arguments, "outputsize", 100, 1, 5_000),
        "start_date": _optional_text(arguments, "start_date"),
        "end_date": _optional_text(arguments, "end_date"),
        "timezone": _optional_text(arguments, "timezone", max_length=64),
        "order": _choice(arguments, "order", {"asc", "desc"}, "desc"),
        "format": "JSON",
    }
    data = await client.get("/time_series", params=params)
    return _result(data, dataset="time_series")


async def _get_technical_indicator(
    client: MarketDataClient,
    arguments: Dict[str, Any],
) -> Any:
    indicator = _choice(arguments, "indicator", _INDICATORS, "rsi")
    params = {
        **_base_params(arguments),
        "interval": _choice(arguments, "interval", _INTERVALS, "1day"),
        "series_type": _choice(
            arguments,
            "series_type",
            {"open", "high", "low", "close"},
            "close",
        ),
        "outputsize": _bounded_int(arguments, "outputsize", 100, 1, 5_000),
        "format": "JSON",
    }
    if indicator == "macd":
        fast_period = _bounded_int(arguments, "fast_period", 12, 1, 500)
        slow_period = _bounded_int(arguments, "slow_period", 26, 1, 800)
        if fast_period >= slow_period:
            raise MarketDataError("fast_period must be smaller than slow_period.")
        params.update(
            {
                "fast_period": fast_period,
                "slow_period": slow_period,
                "signal_period": _bounded_int(
                    arguments,
                    "signal_period",
                    9,
                    1,
                    500,
                ),
            }
        )
    else:
        params["time_period"] = _bounded_int(
            arguments,
            "time_period",
            20 if indicator == "bbands" else 14,
            1,
            500,
        )
        if indicator == "bbands":
            params["sd"] = _bounded_float(arguments, "sd", 2.0, 0.1, 10.0)
            params["ma_type"] = _choice(arguments, "ma_type", _MA_TYPES, "SMA")
    data = await client.get(f"/{indicator}", params=params)
    return _result(data, dataset=indicator)


_COMMON_PROPERTIES = {
    "symbol": {
        "type": "string",
        "description": "Market symbol or pair, for example AAPL or BTC/USD.",
    },
    "exchange": {"type": "string", "maxLength": 32},
    "country": {"type": "string", "maxLength": 64},
}

_TOOLS: Dict[str, Dict[str, Any]] = {
    "get_price": {
        "description": "Get the latest read-only price for one symbol.",
        "required": ["symbol"],
        "properties": dict(_COMMON_PROPERTIES),
    },
    "get_quote": {
        "description": "Get a read-only quote and current market statistics.",
        "required": ["symbol"],
        "properties": dict(_COMMON_PROPERTIES),
    },
    "get_time_series": {
        "description": "Get bounded read-only historical OHLCV time-series data.",
        "required": ["symbol"],
        "properties": {
            **_COMMON_PROPERTIES,
            "interval": {"type": "string", "enum": sorted(_INTERVALS)},
            "outputsize": {"type": "integer", "minimum": 1, "maximum": 5_000},
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
            "timezone": {"type": "string", "maxLength": 64},
            "order": {"type": "string", "enum": ["asc", "desc"]},
        },
    },
    "get_technical_indicator": {
        "description": "Get one bounded read-only technical indicator series.",
        "required": ["symbol"],
        "properties": {
            **_COMMON_PROPERTIES,
            "interval": {"type": "string", "enum": sorted(_INTERVALS)},
            "indicator": {"type": "string", "enum": sorted(_INDICATORS)},
            "time_period": {"type": "integer", "minimum": 1, "maximum": 500},
            "fast_period": {"type": "integer", "minimum": 1, "maximum": 500},
            "slow_period": {"type": "integer", "minimum": 1, "maximum": 800},
            "signal_period": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
            },
            "sd": {"type": "number", "minimum": 0.1, "maximum": 10.0},
            "ma_type": {"type": "string", "enum": sorted(_MA_TYPES)},
            "series_type": {
                "type": "string",
                "enum": ["close", "high", "low", "open"],
            },
            "outputsize": {"type": "integer", "minimum": 1, "maximum": 5_000},
        },
    },
}

_Handler = Callable[[MarketDataClient, Dict[str, Any]], Awaitable[Any]]
_HANDLERS: Dict[str, _Handler] = {
    "get_price": _get_price,
    "get_quote": _get_quote,
    "get_time_series": _get_time_series,
    "get_technical_indicator": _get_technical_indicator,
}
