"""Generic outbound HTTP webhook MCP module.

Credentials are delivered as a JSON object by the MCP dispatcher. The
endpoint may live in that connection config or be supplied per call::

    {"url": "https://hooks.example.test/events",
     "bearer_token": "...", "secret": "...",
     "headers": {"X-Source": "manor"}}

Webhook calls are deliberately single-attempt. A response includes the
idempotency key so a caller can retry the same delivery explicitly without
Manor silently duplicating an external side effect.
"""
from __future__ import annotations

import json
import re
import secrets
from enum import StrEnum
from typing import Any, Dict, List

import httpx

from packages.core.ai.mcp._http import mcp_err, mcp_ok
from packages.core.services.dashboard_http import (
    DashboardHttpError,
    PublicHttpsEndpoint,
    create_public_https_transport,
    resolve_public_https_target,
    validate_dashboard_http_url,
)


_DEFAULT_TIMEOUT = 15.0
_MAX_TIMEOUT = 30.0
_RETRYABLE_STATUSES = {408, 409, 425, 429}
_CREDENTIAL_KEYS = (
    "bearer_token",
    "access_token",
    "token",
    "secret",
    "webhook_secret",
)


class WebhookReservedHeader(StrEnum):
    AUTHORIZATION = "authorization"
    CONNECTION = "connection"
    CONTENT_LENGTH = "content-length"
    CONTENT_TYPE = "content-type"
    HOST = "host"
    IDEMPOTENCY_KEY = "idempotency-key"
    KEEP_ALIVE = "keep-alive"
    PROXY_AUTHORIZATION = "proxy-authorization"
    PROXY_CONNECTION = "proxy-connection"
    TE = "te"
    TRAILER = "trailer"
    TRANSFER_ENCODING = "transfer-encoding"
    UPGRADE = "upgrade"
    WEBHOOK_SECRET = "x-webhook-secret"


_TRANSPORT_OWNED_HEADERS = frozenset({
    WebhookReservedHeader.CONNECTION,
    WebhookReservedHeader.CONTENT_LENGTH,
    WebhookReservedHeader.HOST,
    WebhookReservedHeader.KEEP_ALIVE,
    WebhookReservedHeader.PROXY_AUTHORIZATION,
    WebhookReservedHeader.PROXY_CONNECTION,
    WebhookReservedHeader.TE,
    WebhookReservedHeader.TRAILER,
    WebhookReservedHeader.TRANSFER_ENCODING,
    WebhookReservedHeader.UPGRADE,
})
_RUNTIME_OWNED_HEADERS = frozenset({
    WebhookReservedHeader.AUTHORIZATION,
    WebhookReservedHeader.CONTENT_TYPE,
    WebhookReservedHeader.IDEMPOTENCY_KEY,
    WebhookReservedHeader.WEBHOOK_SECRET,
})
_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


_TOOLS: Dict[str, Dict[str, Any]] = {
    "send": {
        "description": (
            "Send one JSON payload to a configured HTTP webhook. The call is "
            "single-attempt; retry explicitly with the returned idempotency key."
        ),
        "required": ["payload"],
        "properties": {
            "url": {
                "type": "string",
                "description": "HTTPS endpoint; defaults to the connected webhook URL.",
            },
            "payload": {"type": "object", "description": "JSON event payload."},
            "headers": {"type": "object", "description": "Additional request headers."},
            "idempotency_key": {"type": "string"},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 30},
        },
    },
}


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": {
                "type": "object",
                "required": spec["required"],
                "properties": spec["properties"],
            },
        }
        for name, spec in _TOOLS.items()
    ]


def _credentials(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Webhook credentials are malformed; reconnect the integration.") from exc
    if not isinstance(value, dict):
        raise ValueError("Webhook credentials must be a JSON object.")
    return value


def _has_configured_credentials(config: dict[str, Any]) -> bool:
    return any(config.get(key) not in (None, "") for key in _CREDENTIAL_KEYS) or bool(
        config.get("headers")
    )


def _normalize_endpoint(value: Any) -> str:
    try:
        return validate_dashboard_http_url(
            str(value or ""),
            allow_nonstandard_port=True,
        )
    except DashboardHttpError as exc:
        raise ValueError(f"Webhook URL must be public HTTPS: {exc}") from exc


def _endpoint(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    requested_value = arguments.get("url")
    configured_value = config.get("url") or config.get("webhook_url") or config.get("endpoint")
    credentialed = _has_configured_credentials(config)
    configured_url = (
        _normalize_endpoint(configured_value)
        if configured_value
        else ""
    )
    requested_url = (
        _normalize_endpoint(requested_value)
        if requested_value
        else ""
    )
    if credentialed:
        if not configured_url:
            raise ValueError("Credentialed webhook connections require a configured URL.")
        if requested_url and requested_url != configured_url:
            raise ValueError("Credentialed webhook calls must use the configured URL.")
    endpoint = requested_url or configured_url
    if not endpoint:
        raise ValueError("Webhook URL must be an absolute public HTTPS URL.")
    return endpoint


async def _resolve_public_endpoint(url: str) -> PublicHttpsEndpoint:
    try:
        return await resolve_public_https_target(
            url,
            allow_nonstandard_port=True,
        )
    except DashboardHttpError as exc:
        raise ValueError(f"Webhook URL must be public HTTPS: {exc}") from exc


def _timeout(value: Any) -> float:
    if value in (None, ""):
        return _DEFAULT_TIMEOUT
    if isinstance(value, bool):
        raise ValueError("timeout_seconds must be between 1 and 30.")
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timeout_seconds must be between 1 and 30.") from exc
    if seconds < 1 or seconds > _MAX_TIMEOUT:
        raise ValueError("timeout_seconds must be between 1 and 30.")
    return seconds


class WebhookHeadersFactory:
    """Merge owner configuration and call input without authority inversion."""

    @staticmethod
    def _normalize(raw: object, *, source: str) -> list[tuple[str, str, str]]:
        if not isinstance(raw, dict):
            raise ValueError("headers must be objects.")
        normalized: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for raw_name, raw_value in raw.items():
            name = str(raw_name).strip()
            value = str(raw_value)
            folded = name.casefold()
            if not _HEADER_NAME_PATTERN.fullmatch(name):
                raise ValueError(f"{source} header name is invalid.")
            if "\r" in value or "\n" in value:
                raise ValueError(f"{source} header value is invalid.")
            if folded in seen:
                raise ValueError(f"{source} headers contain a duplicate name: {name}.")
            seen.add(folded)
            normalized.append((name, value, folded))
        return normalized

    @staticmethod
    def _set_default(
        result: dict[str, str],
        names: set[str],
        name: str,
        value: str,
    ) -> None:
        if name.casefold() not in names:
            result[name] = value
            names.add(name.casefold())

    @classmethod
    def create(
        cls,
        arguments: dict[str, Any],
        config: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, str]:
        configured_raw = config.get("headers")
        requested_raw = arguments.get("headers")
        configured = cls._normalize(
            {} if configured_raw is None else configured_raw,
            source="Configured",
        )
        requested = cls._normalize(
            {} if requested_raw is None else requested_raw,
            source="Requested",
        )
        result: dict[str, str] = {}
        names: set[str] = set()

        for name, value, folded in configured:
            if folded in _TRANSPORT_OWNED_HEADERS or folded == WebhookReservedHeader.IDEMPOTENCY_KEY:
                raise ValueError(f"Configured webhook header is reserved: {name}.")
            result[name] = value
            names.add(folded)

        for name, value, folded in requested:
            if folded in _TRANSPORT_OWNED_HEADERS or folded in _RUNTIME_OWNED_HEADERS:
                raise ValueError(f"Requested webhook header is reserved: {name}.")
            if folded in names:
                raise ValueError(f"Requested headers cannot override configured header: {name}.")
            result[name] = value
            names.add(folded)

        cls._set_default(result, names, "Content-Type", "application/json")
        bearer = config.get("bearer_token") or config.get("access_token") or config.get("token")
        if bearer:
            cls._set_default(result, names, "Authorization", f"Bearer {str(bearer).strip()}")
        secret = config.get("secret") or config.get("webhook_secret")
        if secret:
            cls._set_default(result, names, "X-Webhook-Secret", str(secret).strip())
        result["Idempotency-Key"] = idempotency_key
        return result


def _headers(arguments: dict[str, Any], config: dict[str, Any], idempotency_key: str) -> dict[str, str]:
    return WebhookHeadersFactory.create(arguments, config, idempotency_key)


async def call_tool(name: str, arguments: Dict[str, Any], bearer_token: str) -> Dict[str, Any]:
    spec = _TOOLS.get(name)
    if spec is None:
        return mcp_err(f"Unknown Webhook tool: {name!r}")
    if not isinstance(arguments, dict):
        return mcp_err("arguments must be an object")
    args = dict(arguments)
    missing = [field for field in spec["required"] if args.get(field) is None]
    if missing:
        return mcp_err(f"Missing required params: {', '.join(missing)}")
    if not isinstance(args.get("payload"), dict):
        return mcp_err("payload must be a JSON object.")

    try:
        config = _credentials(bearer_token)
        url = _endpoint(args, config)
        timeout = _timeout(args.get("timeout_seconds"))
        supplied_idempotency_key = args.get("idempotency_key")
        if supplied_idempotency_key is not None and not isinstance(supplied_idempotency_key, str):
            return mcp_err("idempotency_key must be a string")
        idempotency_key = str(supplied_idempotency_key or secrets.token_urlsafe(18)).strip()
        if (
            not idempotency_key
            or len(idempotency_key) > 255
            or "\r" in idempotency_key
            or "\n" in idempotency_key
        ):
            return mcp_err("idempotency_key must be a safe, non-blank header value.")
        headers = _headers(args, config, idempotency_key)
        target = await _resolve_public_endpoint(url)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=create_public_https_transport(target),
        ) as client:
            response = await client.request("POST", target.url, json=args["payload"], headers=headers)
    except ValueError as exc:
        return mcp_err(str(exc))
    except (httpx.TimeoutException, httpx.NetworkError):
        return mcp_err(json.dumps({"error": "webhook request failed", "retryable": True}))
    except Exception:
        return mcp_err(json.dumps({"error": "webhook request failed", "retryable": True}))

    if 200 <= response.status_code < 300:
        return mcp_ok(
            {
                "status_code": response.status_code,
                "delivered": True,
                "idempotency_key": idempotency_key,
            }
        )
    retryable = response.status_code in _RETRYABLE_STATUSES or response.status_code >= 500
    return mcp_err(
        json.dumps(
            {
                "error": f"webhook endpoint returned HTTP {response.status_code}",
                "status_code": response.status_code,
                "retryable": retryable,
                "idempotency_key": idempotency_key,
            }
        )
    )


_HANDLERS = {"send": call_tool}


__all__ = ["call_tool", "list_tools"]
