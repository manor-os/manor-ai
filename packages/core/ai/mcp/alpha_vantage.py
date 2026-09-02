"""Read-only Alpha Vantage market and company-data MCP adapter."""

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

_PROVIDER = MarketDataProvider.ALPHA_VANTAGE
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,31}$")
_FUNDAMENTAL_FUNCTIONS = {
    "income_statement": "INCOME_STATEMENT",
    "balance_sheet": "BALANCE_SHEET",
    "cash_flow": "CASH_FLOW",
    "earnings": "EARNINGS",
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
        return mcp_err(f"Unknown Alpha Vantage tool: {name}")
    if not isinstance(arguments, dict):
        return mcp_err("arguments must be an object")
    try:
        offset = MarketDataResultFactory.offset(arguments)
        client = MarketDataClientFactory.create(_PROVIDER, bearer_token)
        return MarketDataResultFactory.create(await handler(client, dict(arguments)), offset=offset)
    except MarketDataError as exc:
        return mcp_err(str(exc))
    except Exception:  # noqa: BLE001
        logger.exception("Alpha Vantage tool %s failed", name)
        return mcp_err("Alpha Vantage call failed unexpectedly.")


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


def _required_text(
    arguments: Dict[str, Any],
    field: str,
    *,
    max_length: int,
) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
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


def _result(data: Any, *, dataset: str) -> Dict[str, Any]:
    return {
        "provider": _PROVIDER.value,
        "market_data_only": True,
        "dataset": dataset,
        "data": data,
    }


async def _get_quote(client: MarketDataClient, arguments: Dict[str, Any]) -> Any:
    symbol = _symbol(arguments)
    data = await client.get(
        "/query",
        params={"function": "GLOBAL_QUOTE", "symbol": symbol},
    )
    return _result(data, dataset="global_quote")


async def _search_symbols(
    client: MarketDataClient,
    arguments: Dict[str, Any],
) -> Any:
    keywords = _required_text(arguments, "keywords", max_length=120)
    data = await client.get(
        "/query",
        params={"function": "SYMBOL_SEARCH", "keywords": keywords},
    )
    return _result(data, dataset="symbol_search")


async def _get_daily_series(
    client: MarketDataClient,
    arguments: Dict[str, Any],
) -> Any:
    symbol = _symbol(arguments)
    adjusted = arguments.get("adjusted", False)
    if not isinstance(adjusted, bool):
        raise MarketDataError("adjusted must be a boolean.")
    outputsize = _choice(arguments, "outputsize", {"compact", "full"}, "compact")
    data = await client.get(
        "/query",
        params={
            "function": "TIME_SERIES_DAILY_ADJUSTED" if adjusted else "TIME_SERIES_DAILY",
            "symbol": symbol,
            "outputsize": outputsize,
            "datatype": "json",
        },
    )
    return _result(
        data,
        dataset="daily_adjusted" if adjusted else "daily",
    )


async def _get_company_overview(
    client: MarketDataClient,
    arguments: Dict[str, Any],
) -> Any:
    symbol = _symbol(arguments)
    data = await client.get(
        "/query",
        params={"function": "OVERVIEW", "symbol": symbol},
    )
    return _result(data, dataset="company_overview")


async def _get_fundamentals(
    client: MarketDataClient,
    arguments: Dict[str, Any],
) -> Any:
    symbol = _symbol(arguments)
    statement = _choice(
        arguments,
        "statement",
        set(_FUNDAMENTAL_FUNCTIONS),
        "income_statement",
    )
    data = await client.get(
        "/query",
        params={
            "function": _FUNDAMENTAL_FUNCTIONS[statement],
            "symbol": symbol,
        },
    )
    return _result(data, dataset=statement)


_SYMBOL_PROPERTY = {
    "symbol": {
        "type": "string",
        "description": "Market symbol, for example IBM or 7203.TRT.",
    }
}

_TOOLS: Dict[str, Dict[str, Any]] = {
    "get_quote": {
        "description": "Get the latest read-only global quote for one symbol.",
        "required": ["symbol"],
        "properties": dict(_SYMBOL_PROPERTY),
    },
    "search_symbols": {
        "description": "Search Alpha Vantage's read-only symbol directory.",
        "required": ["keywords"],
        "properties": {"keywords": {"type": "string", "maxLength": 120}},
    },
    "get_daily_series": {
        "description": "Get read-only daily price history for one symbol.",
        "required": ["symbol"],
        "properties": {
            **_SYMBOL_PROPERTY,
            "outputsize": {
                "type": "string",
                "enum": ["compact", "full"],
                "description": "full requires an eligible premium plan.",
            },
            "adjusted": {
                "type": "boolean",
                "description": ("Defaults to false. Adjusted daily data requires an eligible premium plan."),
            },
        },
    },
    "get_company_overview": {
        "description": "Get normalized read-only company overview fields.",
        "required": ["symbol"],
        "properties": dict(_SYMBOL_PROPERTY),
    },
    "get_fundamentals": {
        "description": (
            "Get one reported read-only fundamentals dataset. Defaults to the "
            "income statement to avoid silently consuming multiple API credits."
        ),
        "required": ["symbol"],
        "properties": {
            **_SYMBOL_PROPERTY,
            "statement": {
                "type": "string",
                "enum": sorted(_FUNDAMENTAL_FUNCTIONS),
            },
        },
    },
}

_Handler = Callable[[MarketDataClient, Dict[str, Any]], Awaitable[Any]]
_HANDLERS: Dict[str, _Handler] = {
    "get_quote": _get_quote,
    "search_symbols": _search_symbols,
    "get_daily_series": _get_daily_series,
    "get_company_overview": _get_company_overview,
    "get_fundamentals": _get_fundamentals,
}
