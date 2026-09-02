"""Shared, read-only HTTP boundary for market-data MCP providers.

The three providers use different authentication shapes but share the same
runtime guarantees: bounded requests, JSON-only responses, provider errors
surfaced as MCP errors, and credentials removed from any upstream message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

import httpx


class MarketDataProvider(str, Enum):
    ALPACA = "alpaca_market_data"
    ALPHA_VANTAGE = "alpha_vantage"
    TWELVE_DATA = "twelve_data"

    @property
    def display_name(self) -> str:
        return {
            self.ALPACA: "Alpaca Market Data",
            self.ALPHA_VANTAGE: "Alpha Vantage",
            self.TWELVE_DATA: "Twelve Data",
        }[self]


class MarketDataError(RuntimeError):
    """Safe, user-facing provider failure."""


class MarketDataResultFactory:
    """Bound tool output by whole records, never by cutting serialized JSON."""

    # Fit the Agent loop's ordinary tool-result budget, not only the MCP
    # envelope limit. The context-compactor regression guards this boundary.
    MAX_CHARS = 4_000
    OFFSET_PARAMETER = {
        "type": "integer",
        "minimum": 0,
        "maximum": 1_000_000,
        "description": (
            "Continue a large result using pagination.next_offset with unchanged "
            "query arguments. Each page re-fetches live provider data, not a "
            "frozen snapshot; pin historical date bounds when available."
        ),
    }
    # The collection fields in the supported provider endpoints. All other
    # response fields (including timestamps and native page tokens) stay intact.
    _COLLECTION_KEYS = frozenset(
        {
            "bars",
            "news",
            "snapshots",
            "values",
            "bestMatches",
            "annualReports",
            "quarterlyReports",
            "annualEarnings",
            "quarterlyEarnings",
            "Time Series (Daily)",
        }
    )

    @staticmethod
    def offset(arguments: Mapping[str, Any]) -> int:
        value = arguments.get("result_offset", 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
            raise MarketDataError("result_offset must be an integer between 0 and 1000000.")
        return value

    @classmethod
    def create(cls, payload: dict[str, Any], *, offset: int = 0) -> dict[str, Any]:
        def encode(value: dict[str, Any]) -> str:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

        def envelope(value: dict[str, Any], text: str) -> dict[str, Any]:
            return {
                "content": [{"type": "text", "text": text}],
                "structuredContent": value,
                "isError": False,
            }

        text = encode(payload)
        if offset == 0 and len(text) <= cls.MAX_CHARS:
            return envelope(payload, text)

        data = payload.get("data")
        collections = {
            key: list(value.items()) if isinstance(value, dict) else value
            for key, value in (data.items() if isinstance(data, dict) else ())
            if key in cls._COLLECTION_KEYS and isinstance(value, (list, dict))
        }
        total = max((len(value) for value in collections.values()), default=0)
        if offset and offset >= total:
            raise MarketDataError("result_offset is outside the current provider response; restart at 0.")

        def page(size: int) -> dict[str, Any]:
            return {
                **payload,
                "data": {
                    **data,
                    **{
                        key: dict(value[offset : offset + size])
                        if isinstance(data[key], dict)
                        else value[offset : offset + size]
                        for key, value in collections.items()
                    },
                },
                "pagination": {
                    "offset": offset,
                    "next_offset": offset + size if offset + size < total else None,
                    "totals": {key: len(value) for key, value in collections.items()},
                    "source": "refetched_provider_response",
                },
            }

        # Use one common record window for all collections so annual and
        # quarterly datasets both remain reachable without gaps or duplicates.
        low, high = 1, total - offset
        result = None
        while low <= high:
            size = (low + high) // 2
            candidate = page(size)
            text = encode(candidate)
            if len(text) <= cls.MAX_CHARS:
                result = envelope(candidate, text)
                low = size + 1
            else:
                high = size - 1
        if result is None:
            raise MarketDataError(
                "Market data metadata or a single record exceeds the tool response limit. "
                "Request less upstream data (for news, disable include_content)."
            )
        return result


@dataclass(frozen=True)
class MarketDataClient:
    provider: MarketDataProvider
    base_url: str
    headers: Mapping[str, str]
    auth_params: Mapping[str, str]
    sensitive_values: tuple[str, ...]
    timeout_seconds: float = 20.0

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        query = {
            key: value
            for key, value in {**dict(params or {}), **self.auth_params}.items()
            if value is not None and value != ""
        }
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.get(
                    url,
                    params=query or None,
                    headers=dict(self.headers) or None,
                )
        except httpx.TimeoutException as exc:
            raise MarketDataError(f"{self.provider.display_name} timed out. Please try again.") from exc
        except httpx.RequestError as exc:
            raise MarketDataError(f"{self.provider.display_name} is temporarily unavailable.") from exc

        try:
            payload: Any = response.json()
        except Exception as exc:
            raise MarketDataError(f"{self.provider.display_name} returned an invalid response.") from exc

        if not response.is_success:
            detail = _provider_error_message(payload) or f"HTTP {response.status_code}"
            raise MarketDataError(self._redact(f"{self.provider.display_name} request failed: {detail}"))

        provider_error = _successful_response_error(self.provider, payload)
        if provider_error:
            raise MarketDataError(self._redact(f"{self.provider.display_name} request failed: {provider_error}"))
        return payload

    def _redact(self, message: str) -> str:
        clean = message
        for value in sorted(self.sensitive_values, key=len, reverse=True):
            if value:
                clean = clean.replace(value, "[redacted]")
        return clean[:500]


class MarketDataClientFactory:
    """Build the provider client from the dispatcher credential envelope."""

    @classmethod
    def create(
        cls,
        provider: MarketDataProvider,
        bearer_token: str,
    ) -> MarketDataClient:
        if provider is MarketDataProvider.ALPACA:
            credentials = _json_credentials(bearer_token, provider)
            api_key = _credential_text(credentials, "api_key", provider)
            api_secret = _credential_text(credentials, "api_secret", provider)
            return MarketDataClient(
                provider=provider,
                base_url="https://data.alpaca.markets",
                headers={
                    "APCA-API-KEY-ID": api_key,
                    "APCA-API-SECRET-KEY": api_secret,
                    "Accept": "application/json",
                },
                auth_params={},
                sensitive_values=(api_key, api_secret),
            )

        api_key = _flat_api_key(bearer_token, provider)
        base_url = {
            MarketDataProvider.ALPHA_VANTAGE: "https://www.alphavantage.co",
            MarketDataProvider.TWELVE_DATA: "https://api.twelvedata.com",
        }[provider]
        return MarketDataClient(
            provider=provider,
            base_url=base_url,
            headers={"Accept": "application/json"},
            auth_params={"apikey": api_key},
            sensitive_values=(api_key,),
        )


def _json_credentials(
    bearer_token: str,
    provider: MarketDataProvider,
) -> dict[str, Any]:
    if not isinstance(bearer_token, str) or not bearer_token.strip():
        raise MarketDataError(f"{provider.display_name} credentials are missing.")
    try:
        credentials = json.loads(bearer_token)
    except (TypeError, ValueError) as exc:
        raise MarketDataError(f"{provider.display_name} credentials are malformed.") from exc
    if not isinstance(credentials, dict):
        raise MarketDataError(f"{provider.display_name} credentials must be a JSON object.")
    return credentials


def _credential_text(
    credentials: Mapping[str, Any],
    field: str,
    provider: MarketDataProvider,
) -> str:
    value = credentials.get(field)
    if not isinstance(value, str) or not value.strip():
        raise MarketDataError(f"{provider.display_name} requires a non-empty {field}.")
    return value.strip()


def _flat_api_key(bearer_token: str, provider: MarketDataProvider) -> str:
    if not isinstance(bearer_token, str) or not bearer_token.strip():
        raise MarketDataError(f"{provider.display_name} API key is missing.")
    return bearer_token.strip()


def _provider_error_message(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in (
        "Error Message",
        "Information",
        "Note",
        "message",
        "detail",
        "error",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("message") or value.get("detail")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None


def _successful_response_error(
    provider: MarketDataProvider,
    payload: Any,
) -> str | None:
    if not isinstance(payload, dict):
        return None
    if provider is MarketDataProvider.ALPHA_VANTAGE:
        for key in ("Error Message", "Information", "Note"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    elif provider is MarketDataProvider.TWELVE_DATA:
        if str(payload.get("status") or "").lower() == "error":
            return _provider_error_message(payload) or "provider error"
        code = payload.get("code")
        if isinstance(code, int) and code >= 400:
            return _provider_error_message(payload) or f"provider code {code}"
    elif provider is MarketDataProvider.ALPACA:
        code = payload.get("code")
        if isinstance(code, int) and code >= 400:
            return _provider_error_message(payload) or f"provider code {code}"
    return None


__all__ = [
    "MarketDataClient",
    "MarketDataClientFactory",
    "MarketDataError",
    "MarketDataProvider",
    "MarketDataResultFactory",
]
