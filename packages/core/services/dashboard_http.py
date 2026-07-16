from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx


MAX_DASHBOARD_HTTP_BYTES = 250_000
_BLOCKED_HOST_SUFFIXES = (
    ".internal",
    ".local",
    ".localhost",
)


class DashboardHttpError(RuntimeError):
    pass


class DashboardHttpPolicyError(DashboardHttpError):
    pass


class DashboardHttpUnavailable(DashboardHttpError):
    pass


def validate_dashboard_http_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2_000:
        raise DashboardHttpPolicyError("Public JSON URL is missing or too long")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https":
        raise DashboardHttpPolicyError("Public JSON requests require HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise DashboardHttpPolicyError("Public JSON URL host is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise DashboardHttpPolicyError("Public JSON URL port is invalid") from exc
    if port not in {None, 443}:
        raise DashboardHttpPolicyError("Public JSON requests require the standard HTTPS port")

    hostname = parsed.hostname.rstrip(".").casefold()
    if (
        hostname in {"localhost", "localhost.localdomain"}
        or hostname.endswith(_BLOCKED_HOST_SUFFIXES)
    ):
        raise DashboardHttpPolicyError("Private network hosts are not available")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise DashboardHttpPolicyError("Private network addresses are not available")

    netloc = f"[{hostname}]" if ":" in hostname else hostname
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


async def _resolve_public_host(hostname: str) -> None:
    try:
        results = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            443,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise DashboardHttpUnavailable("Public JSON host could not be resolved") from exc
    addresses = {
        str(result[4][0])
        for result in results
        if result and len(result) > 4 and result[4]
    }
    if not addresses:
        raise DashboardHttpUnavailable("Public JSON host did not resolve")
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise DashboardHttpPolicyError("Public JSON host resolved unexpectedly") from exc
        if not address.is_global:
            raise DashboardHttpPolicyError("Public JSON host resolved to a private network")


async def get_dashboard_http_json(url: str) -> object:
    normalized = validate_dashboard_http_url(url)
    hostname = urlsplit(normalized).hostname or ""
    await _resolve_public_host(hostname)

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=6.0),
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "Manor-AI-Dashboard/1.0",
            },
        ) as client:
            async with client.stream("GET", normalized) as response:
                if response.is_redirect:
                    raise DashboardHttpPolicyError("Public JSON redirects are not followed")
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type and content_type != "application/json" and not content_type.endswith("+json"):
                    raise DashboardHttpUnavailable("Public endpoint did not return JSON")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_DASHBOARD_HTTP_BYTES:
                        raise DashboardHttpUnavailable("Public JSON response is too large")
                    chunks.append(chunk)
    except DashboardHttpError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise DashboardHttpUnavailable("Public JSON request failed") from exc

    try:
        return json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DashboardHttpUnavailable("Public endpoint returned invalid JSON") from exc
