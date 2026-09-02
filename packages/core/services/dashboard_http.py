from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx
import idna


MAX_DASHBOARD_HTTP_BYTES = 250_000
MAX_PUBLIC_HTTPS_ADDRESSES = 8
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


@dataclass(frozen=True)
class PublicHttpsEndpoint:
    """One normalized HTTPS target pinned to its validated DNS answers."""

    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


def _canonical_hostname(value: str) -> str:
    hostname = str(value or "").rstrip(".").lower()
    if not hostname:
        raise DashboardHttpPolicyError("Public JSON URL host is invalid")
    try:
        return ipaddress.ip_address(hostname).compressed
    except ValueError:
        pass
    if not hostname.isascii():
        try:
            hostname = idna.encode(hostname).decode("ascii")
        except idna.IDNAError as exc:
            raise DashboardHttpPolicyError("Public JSON URL host is invalid") from exc
    if (
        len(hostname) > 253
        or any(not label or len(label) > 63 for label in hostname.split("."))
    ):
        raise DashboardHttpPolicyError("Public JSON URL host is invalid")
    return hostname


def validate_dashboard_http_url(
    value: str,
    *,
    allow_nonstandard_port: bool = False,
) -> str:
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
    if port == 0:
        raise DashboardHttpPolicyError("Public JSON URL port is invalid")
    if not allow_nonstandard_port and port not in {None, 443}:
        raise DashboardHttpPolicyError("Public JSON requests require the standard HTTPS port")

    hostname = _canonical_hostname(parsed.hostname)
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
    if port not in {None, 443}:
        netloc = f"{netloc}:{port}"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


async def _resolve_public_host(hostname: str, port: int = 443) -> tuple[str, ...]:
    try:
        results = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise DashboardHttpUnavailable("Public JSON host could not be resolved") from exc
    addresses = tuple(dict.fromkeys(
        str(result[4][0])
        for result in results
        if result and len(result) > 4 and result[4]
    ))
    if not addresses:
        raise DashboardHttpUnavailable("Public JSON host did not resolve")
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise DashboardHttpPolicyError("Public JSON host resolved unexpectedly") from exc
        if not address.is_global:
            raise DashboardHttpPolicyError("Public JSON host resolved to a private network")
    return addresses[:MAX_PUBLIC_HTTPS_ADDRESSES]


async def resolve_public_https_target(
    value: str,
    *,
    allow_nonstandard_port: bool = False,
) -> PublicHttpsEndpoint:
    """Resolve once so validation and the eventual TCP connection share DNS."""

    normalized = validate_dashboard_http_url(
        value,
        allow_nonstandard_port=allow_nonstandard_port,
    )
    parsed = urlsplit(normalized)
    hostname = parsed.hostname or ""
    port = 443 if parsed.port is None else parsed.port
    addresses = await _resolve_public_host(hostname, port)
    return PublicHttpsEndpoint(normalized, hostname, port, addresses)


async def validate_public_https_url(value: str) -> str:
    """Normalize a public HTTPS URL and reject private DNS resolution."""

    return (await resolve_public_https_target(value)).url


class _PinnedPublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect only to addresses validated for one public HTTPS hostname."""

    def __init__(
        self,
        target: PublicHttpsEndpoint,
        *,
        backend: httpcore.AsyncNetworkBackend | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._target = target
        self._network_backend = backend or httpcore.AnyIOBackend()
        self._monotonic = monotonic

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        if (
            _canonical_hostname(host) != self._target.hostname
            or port != self._target.port
        ):
            raise httpcore.ConnectError("Public HTTPS transport target changed")

        last_error: Exception | None = None
        deadline = None if timeout is None else self._monotonic() + max(timeout, 0.0)
        for address in self._target.addresses:
            remaining_timeout = timeout
            if deadline is not None:
                remaining_timeout = deadline - self._monotonic()
                if remaining_timeout <= 0:
                    raise httpcore.ConnectTimeout(
                        "Public HTTPS connection deadline exceeded"
                    ) from last_error
            try:
                return await self._network_backend.connect_tcp(
                    address,
                    port,
                    timeout=remaining_timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("Public HTTPS target has no validated addresses")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Public HTTPS transport does not support Unix sockets")

    async def sleep(self, seconds: float) -> None:
        await self._network_backend.sleep(seconds)


class _PinnedPublicHttpsTransport(httpx.AsyncHTTPTransport):
    def __init__(self, target: PublicHttpsEndpoint) -> None:
        super().__init__(trust_env=False)
        # httpx does not expose a network-backend constructor argument. Its
        # transport contract delegates requests and close() entirely to this
        # httpcore pool, which lets us retain normal TLS hostname validation.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            network_backend=_PinnedPublicNetworkBackend(target),
        )


def create_public_https_transport(
    target: PublicHttpsEndpoint,
) -> httpx.AsyncHTTPTransport:
    """Build an isolated transport pinned to one validated public target."""

    return _PinnedPublicHttpsTransport(target)


async def get_dashboard_http_json(url: str) -> object:
    target = await resolve_public_https_target(url)

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=6.0),
            follow_redirects=False,
            trust_env=False,
            transport=create_public_https_transport(target),
            headers={
                "Accept": "application/json",
                "User-Agent": "Manor-AI-Dashboard/1.0",
            },
        ) as client:
            async with client.stream("GET", target.url) as response:
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
