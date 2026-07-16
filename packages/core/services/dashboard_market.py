"""Fetch short-lived stock quotes for user-generated dashboard modules."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
import re
import time
from typing import Any

import httpx

from packages.core.config import get_settings


logger = logging.getLogger(__name__)

_FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
_CACHE_TTL_SECONDS = 15
_MAX_SYMBOLS = 8
_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,11}$")
_quote_cache: dict[tuple[str, ...], tuple[float, list[dict[str, Any]]]] = {}
_quote_cache_lock = asyncio.Lock()


class DashboardMarketDataUnavailable(RuntimeError):
    """Raised when a live market-data provider cannot serve the request."""


def normalize_stock_symbols(symbols: list[str]) -> list[str]:
    """Return a small, unique list of safe ticker symbols."""

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_symbol in symbols:
        symbol = str(raw_symbol or "").strip().upper()
        if not _SYMBOL_PATTERN.fullmatch(symbol) or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
        if len(normalized) >= _MAX_SYMBOLS:
            break
    return normalized


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _quote_timestamp(value: object) -> str | None:
    timestamp = _optional_number(value)
    if timestamp is None or timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _normalize_quote(symbol: str, payload: object) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    price = _optional_number(raw.get("c"))
    available = price is not None and price > 0
    return {
        "symbol": symbol,
        "price": price if available else None,
        "change": _optional_number(raw.get("d")) if available else None,
        "change_percent": _optional_number(raw.get("dp")) if available else None,
        "open": _optional_number(raw.get("o")) if available else None,
        "high": _optional_number(raw.get("h")) if available else None,
        "low": _optional_number(raw.get("l")) if available else None,
        "previous_close": _optional_number(raw.get("pc")) if available else None,
        "currency": "USD",
        "updated_at": _quote_timestamp(raw.get("t")) if available else None,
        "status": "ok" if available else "unavailable",
        "provider": "Finnhub",
    }


async def get_dashboard_stock_quotes(*, symbols: list[str]) -> list[dict[str, Any]]:
    """Return cached live quotes from the configured Finnhub account."""

    normalized = normalize_stock_symbols(symbols)
    if not normalized:
        raise ValueError("At least one valid stock symbol is required")

    cache_key = tuple(normalized)
    now = time.monotonic()
    cached = _quote_cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return [dict(item) for item in cached[1]]

    api_key = get_settings().FINNHUB_API_KEY.strip()
    if not api_key:
        if cached:
            return [dict(item) for item in cached[1]]
        raise DashboardMarketDataUnavailable(
            "Live market data is not configured for this deployment"
        )

    try:
        async with httpx.AsyncClient(
            timeout=8.0,
            follow_redirects=True,
            headers={
                "User-Agent": "ManorAI-Dashboard/1.0",
                "X-Finnhub-Token": api_key,
            },
        ) as client:
            responses = await asyncio.gather(
                *(
                    client.get(_FINNHUB_QUOTE_URL, params={"symbol": symbol})
                    for symbol in normalized
                ),
                return_exceptions=True,
            )

        quotes: list[dict[str, Any]] = []
        for symbol, response in zip(normalized, responses, strict=True):
            if isinstance(response, Exception):
                logger.warning("Dashboard stock quote failed for %s: %s", symbol, response)
                quotes.append(_normalize_quote(symbol, {}))
                continue
            response.raise_for_status()
            quotes.append(_normalize_quote(symbol, response.json()))
    except Exception as exc:
        logger.warning("Dashboard stock request failed: %s", exc)
        if cached:
            return [dict(item) for item in cached[1]]
        raise DashboardMarketDataUnavailable(
            "Live market data is temporarily unavailable"
        ) from exc

    async with _quote_cache_lock:
        _quote_cache[cache_key] = (now, quotes)
        if len(_quote_cache) > 128:
            oldest_key = min(_quote_cache, key=lambda key: _quote_cache[key][0])
            _quote_cache.pop(oldest_key, None)
    return [dict(item) for item in quotes]
