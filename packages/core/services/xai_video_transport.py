"""Credential-bound, DNS-pinned transport for user-configured Grok relays."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from packages.core.config import get_settings
from packages.core.services.dashboard_http import (
    DashboardHttpError,
    PublicHttpsEndpoint,
    create_public_https_transport,
    resolve_public_https_target,
    validate_dashboard_http_url,
)

XAI_API_BASE = "https://api.x.ai/v1"
XAI_MAX_VIDEO_BYTES = 128 * 1024 * 1024
XAI_BINDING_KEY = "xai_credential_binding"
_BINDING_ERROR = "Grok credentials or relay changed, or this job has no credential binding. Create a new video job."


class XaiTransportError(RuntimeError):
    """Safe error text: never include credentials, URLs or provider payloads."""


def normalize_xai_url(value: str | None) -> str:
    raw = str(value or XAI_API_BASE).strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        if (
            any(ord(char) <= 32 or ord(char) == 127 for char in raw)
            or "?" in raw or "#" in raw
            or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.port == 0
        ):
            raise ValueError("invalid URL")
        explicit_oss = os.getenv("DEPLOYMENT_MODE", "").strip().lower() == "oss"
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if explicit_oss and loopback and parsed.scheme in {"http", "https"}:
            return str(httpx.URL(raw)).rstrip("/")
        return str(httpx.URL(validate_dashboard_http_url(
            raw, allow_nonstandard_port=True,
        ))).rstrip("/")
    except (DashboardHttpError, ValueError, httpx.InvalidURL):
        raise XaiTransportError("Grok relay requires a public HTTPS URL without user information, query or fragment. Loopback is only available in explicit OSS mode.") from None


def xai_poll_url(base_url: str | None, request_id: str) -> str:
    # A response identifier is one path component, never a URL or dot segment.
    if not isinstance(request_id, str) or not request_id or request_id in {".", ".."} or any(
        ord(char) <= 32 or ord(char) == 127 for char in request_id
    ):
        raise XaiTransportError("Grok returned an invalid video identifier.")
    return f"{normalize_xai_url(base_url)}/videos/{quote(request_id, safe='')}"


async def xai_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    timeout: Any = 30.0,
    json_body: dict[str, Any] | None = None,
    max_bytes: int = 1024 * 1024,
) -> httpx.Response:
    normalized = normalize_xai_url(url)
    parsed = urlsplit(normalized)
    try:
        if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            # normalize_xai_url has already required explicit OSS mode. Do not
            # resolve localhost through DNS or allow arbitrary private hosts.
            target = PublicHttpsEndpoint(
                normalized, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                ("::1",) if parsed.hostname == "::1" else ("127.0.0.1",),
            )
        else:
            target = await resolve_public_https_target(normalized, allow_nonstandard_port=True)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=create_public_https_transport(target),
        ) as client:
            async with client.stream(method, target.url, headers=headers, json=json_body) as response:
                if response.is_redirect:
                    raise XaiTransportError("Grok relay redirects are not allowed.")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise XaiTransportError("Grok relay response exceeds the size limit.")
                    chunks.append(chunk)
                return httpx.Response(
                    response.status_code, headers={
                        key: value for key, value in response.headers.items()
                        if key.lower() not in {"content-encoding", "content-length"}
                    },
                    content=b"".join(chunks), request=response.request,
                )
    except XaiTransportError:
        raise
    except (DashboardHttpError, httpx.HTTPError, OSError, ValueError):
        raise XaiTransportError("Grok relay request failed or its network target is not allowed.") from None


def xai_credential_binding(*, entity_id: str, model: str, base_url: str, api_key: str) -> dict[str, str]:
    base = normalize_xai_url(base_url)
    payload = json.dumps(["xai", entity_id, model, base, api_key], separators=(",", ":"))
    fingerprint = hmac.new(
        get_settings().JWT_SECRET_KEY.encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()
    return {"provider": "xai", "model": model, "base_url": base, "fingerprint": fingerprint}


def require_xai_job_binding(
    binding: Any, *, entity_id: str, model: str, base_url: str, api_key: str, is_byok: bool,
) -> str:
    if not model.startswith("xai/") or not is_byok or not api_key or not isinstance(binding, dict):
        raise XaiTransportError(_BINDING_ERROR)
    expected = xai_credential_binding(entity_id=entity_id, model=model, base_url=base_url, api_key=api_key)
    if any(binding.get(key) != expected[key] for key in ("provider", "model", "base_url")) or not hmac.compare_digest(
        str(binding.get("fingerprint") or ""), expected["fingerprint"],
    ):
        raise XaiTransportError(_BINDING_ERROR)
    return expected["base_url"]
