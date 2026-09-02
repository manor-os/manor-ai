"""Remote MCP transport — talk to a vendor-hosted MCP server over HTTP.

The Model Context Protocol ships an HTTP+JSON-RPC binding (and an
SSE variant). Stripe (``mcp.stripe.com``), PayPal (``mcp.paypal.com``),
Cloudflare, Linear, etc. all expose their MCP servers this way and use
OAuth 2.0 to mint a per-user session token. Manor's existing in-process
wrappers (``packages/core/ai/mcp/{provider}.py``) cover the curated
core; remote MCP fills the long tail without writing more wrappers.

Protocol coverage
─────────────────
This client implements the slice Manor actually needs:

  * ``initialize`` — the JSON-RPC handshake; client sends its
    capabilities, server responds with its version + capabilities
  * ``tools/list`` — fetched on connect / refresh; result cached in
    ``mcp_servers.tools_cached``
  * ``tools/call`` — single-shot tool invocation, returns the
    standard MCP envelope ``{content: [...], isError: bool}``

Streamable HTTP JSON and SSE responses are supported. Manor does not
subscribe to standalone server-push notification streams because its
agent loop remains request/response based.

Auth
────
``access_token`` is the OAuth access_token Manor obtained through
its standard ``/oauth/{server_key}/start`` flow. The vendor's MCP
server validates it on every request via the ``Authorization``
header (or, on a few vendors, a query param — pass ``token_in='query'``
to switch).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 30.0
_LIST_TOOLS_TIMEOUT = 15.0
_MAX_LIST_TOOLS_PAGES = 100
_PROTOCOL_VERSION = "2025-06-18"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


# ── Errors ──────────────────────────────────────────────────────────────────


@dataclass
class RemoteMCPError(RuntimeError):
    """Raised on JSON-RPC error response or HTTP failure. ``code``
    matches the JSON-RPC error code (``-32600``-style for protocol
    issues; vendor-specific positive codes for tool-level failures)."""

    code: int
    message: str
    data: Optional[Dict[str, Any]] = None

    def __str__(self) -> str:
        return f"Remote MCP error {self.code}: {self.message}"


class RemoteMCPResponseMediaType(StrEnum):
    JSON = "application/json"
    SSE = "text/event-stream"


class RemoteMCPResponseFactory:
    """Build one bounded JSON-RPC response from JSON or streaming SSE."""

    @staticmethod
    def _json_payload(raw: str) -> Dict[str, Any] | None:
        if not raw.strip():
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RemoteMCPError(
                code=-32700,
                message=f"Vendor returned non-JSON: {raw[:200]}",
            ) from exc
        if not isinstance(payload, dict):
            raise RemoteMCPError(
                code=-32600,
                message="Vendor returned an invalid JSON-RPC response.",
            )
        return payload

    @staticmethod
    def _record_size(total: int, value: str | bytes) -> int:
        size = len(value if isinstance(value, bytes) else value.encode("utf-8"))
        next_total = total + size
        if next_total > _MAX_RESPONSE_BYTES:
            raise RemoteMCPError(
                code=-32000,
                message="Vendor MCP response exceeded the configured size limit.",
            )
        return next_total

    @classmethod
    async def _read_text(cls, response: httpx.Response) -> str:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total = cls._record_size(total, chunk)
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    @classmethod
    async def _read_json(cls, response: httpx.Response) -> Dict[str, Any] | None:
        return cls._json_payload(await cls._read_text(response))

    @classmethod
    async def _read_sse(
        cls,
        response: httpx.Response,
        request_id: int,
    ) -> Dict[str, Any]:
        data_lines: list[str] = []
        total = 0

        def consume_event() -> Dict[str, Any] | None:
            if not data_lines:
                return None
            raw = "\n".join(data_lines).strip()
            data_lines.clear()
            if not raw or raw == "[DONE]":
                return None
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return None
            if isinstance(payload, dict) and payload.get("id") == request_id:
                return payload
            return None

        async for line in response.aiter_lines():
            total = cls._record_size(total, f"{line}\n")
            if line == "":
                matched = consume_event()
                if matched is not None:
                    return matched
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        matched = consume_event()
        if matched is not None:
            return matched
        raise RemoteMCPError(
            code=-32700,
            message="Vendor SSE stream did not contain the requested JSON-RPC response.",
        )

    @classmethod
    async def create(
        cls,
        response: httpx.Response,
        *,
        request_id: int | None,
    ) -> Dict[str, Any] | None:
        content_type = str(response.headers.get("content-type", "")).lower()
        if RemoteMCPResponseMediaType.SSE.value in content_type:
            # Notifications have no JSON-RPC response. Close the stream without
            # waiting on a vendor push channel that may stay open indefinitely.
            if request_id is None:
                return None
            return await cls._read_sse(response, request_id)
        return await cls._read_json(response)


# ── Client ──────────────────────────────────────────────────────────────────


class RemoteMCPClient:
    """Async JSON-RPC client for an MCP HTTP endpoint.

    One instance per ``(endpoint, access_token)`` pair. Cheap to
    construct — there's no persistent connection to manage; httpx pools
    sockets per-call.
    """

    def __init__(
        self,
        endpoint: str,
        access_token: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        token_in: str = "header",  # "header" | "query"
        client_name: str = "manor-ai",
        client_version: str = "1.0.0",
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.access_token = access_token.strip() if isinstance(access_token, str) else ""
        self.timeout = timeout
        self.token_in = token_in
        self.client_name = client_name
        self.client_version = client_version
        self._request_seq = 0
        self._session_id: str | None = None
        self._protocol_version = _PROTOCOL_VERSION
        self._initialized = False
        self._initialize_result: Dict[str, Any] = {}

    def _next_id(self) -> int:
        self._request_seq += 1
        return self._request_seq

    def _headers(self) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token_in == "header" and self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        if self._initialized:
            h["MCP-Protocol-Version"] = self._protocol_version
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _params(self, base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p = dict(base or {})
        if self.token_in == "query" and self.access_token:
            p["access_token"] = self.access_token
        return p

    # ── Streamable HTTP / JSON-RPC core ─────────────────────────────

    async def _post_message(
        self,
        body: Dict[str, Any],
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any] | None:
        if not self.access_token:
            raise RemoteMCPError(
                code=-32001,
                message="OAuth access token is required. Reconnect the integration.",
            )
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as cx:
                async with cx.stream(
                    "POST",
                    self.endpoint,
                    json=body,
                    headers=self._headers(),
                    params=self._params() or None,
                ) as response:
                    if response.status_code == 401:
                        raise RemoteMCPError(
                            code=-32001,
                            message="OAuth token rejected by vendor MCP server. Reconnect.",
                        )
                    if response.status_code >= 400:
                        error_payload = await RemoteMCPResponseFactory._read_text(response)
                        raise RemoteMCPError(
                            code=response.status_code,
                            message=(f"HTTP {response.status_code}: {error_payload[:300]}"),
                        )

                    session_id = response.headers.get("Mcp-Session-Id")
                    if session_id:
                        self._session_id = str(session_id)

                    request_id = body.get("id")
                    return await RemoteMCPResponseFactory.create(
                        response,
                        request_id=request_id if isinstance(request_id, int) else None,
                    )
        except RemoteMCPError:
            raise
        except httpx.HTTPError as exc:
            raise RemoteMCPError(
                code=-1,
                message=f"transport: {type(exc).__name__}: {exc}",
            ) from exc

    async def _rpc(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        request_id = self._next_id()
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        data = await self._post_message(body, timeout=timeout)
        if data is None:
            raise RemoteMCPError(
                code=-32603,
                message="Vendor returned no JSON-RPC response.",
            )
        if data.get("id") != request_id:
            raise RemoteMCPError(
                code=-32600,
                message="Vendor returned a mismatched JSON-RPC response id.",
            )

        if "error" in data and data["error"]:
            err = data["error"]
            raise RemoteMCPError(
                code=err.get("code", -32603),
                message=err.get("message", "Unknown JSON-RPC error"),
                data=err.get("data"),
            )
        return data.get("result", {})

    async def _send_notification(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self._post_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            }
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    async def initialize(self) -> Dict[str, Any]:
        """Complete the MCP handshake and initialized notification once."""
        if self._initialized:
            return self._initialize_result

        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": self.client_name,
                    "version": self.client_version,
                },
            },
        )
        negotiated = result.get("protocolVersion")
        if isinstance(negotiated, str) and negotiated.strip():
            self._protocol_version = negotiated.strip()
        self._initialized = True
        try:
            await self._send_notification("notifications/initialized")
        except Exception:
            self._initialized = False
            raise
        self._initialize_result = result
        return result

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.initialize()

    # ── Tool surface ─────────────────────────────────────────────────

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Discover the vendor's tool catalog. Returns the raw MCP
        ``tools/list`` payload — list of ``{name, description, inputSchema}``.
        """
        await self._ensure_initialized()
        tools: List[Dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(_MAX_LIST_TOOLS_PAGES):
            result = await self._rpc(
                "tools/list",
                {"cursor": cursor} if cursor is not None else {},
                timeout=_LIST_TOOLS_TIMEOUT,
            )
            tools.extend(item for item in (result.get("tools") or []) if isinstance(item, dict))
            next_cursor = result.get("nextCursor")
            if next_cursor is None or not str(next_cursor).strip():
                return tools
            cursor = str(next_cursor)
            if cursor in seen_cursors:
                raise RemoteMCPError(code=-32603, message="Vendor MCP tools/list repeated a pagination cursor.")
            seen_cursors.add(cursor)
        raise RemoteMCPError(
            code=-32603,
            message=f"Vendor MCP tools/list exceeded {_MAX_LIST_TOOLS_PAGES} pages.",
        )

    async def call_tool(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Invoke a single tool. Returns the standard MCP envelope
        ``{content: [{type:"text", text: ...}], isError: bool}`` —
        matches what in-process wrappers return so the dispatcher can
        treat them uniformly."""
        await self._ensure_initialized()
        return await self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        )


# ── Cache layer for tools/list ─────────────────────────────────────────────


# tool catalogues don't change minute-by-minute; cache the discovery
# call so each agent invocation doesn't re-hit the vendor.
_DEFAULT_TOOLS_CACHE_TTL_SEC = 10 * 60


@dataclass
class _CachedTools:
    tools: List[Dict[str, Any]]
    fetched_at: float


_tools_cache: Dict[str, _CachedTools] = {}


def _cache_key(endpoint: str, access_token: str) -> str:
    """Keep tool discovery account-scoped without retaining OAuth material."""

    token_digest = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    return f"{endpoint.rstrip('/')}::{token_digest}"


async def list_tools_cached(
    endpoint: str,
    access_token: str,
    *,
    ttl_sec: int = _DEFAULT_TOOLS_CACHE_TTL_SEC,
    force_refresh: bool = False,
    token_in: str = "header",
) -> List[Dict[str, Any]]:
    """In-process cache of ``tools/list`` per ``(endpoint, token)``.

    A bigger Redis-backed cache lives in ``mcp_servers.tools_cached``
    (populated by a periodic job) — this in-process layer just avoids
    re-hitting the vendor across rapid back-to-back agent runs in the
    same process.
    """
    key = _cache_key(endpoint, access_token)
    cached = _tools_cache.get(key)
    if cached and not force_refresh and (time.time() - cached.fetched_at) < ttl_sec:
        return cached.tools

    client = RemoteMCPClient(endpoint, access_token, token_in=token_in)
    tools = await client.list_tools()
    _tools_cache[key] = _CachedTools(tools=tools, fetched_at=time.time())
    return tools


def invalidate_tools_cache(endpoint: Optional[str] = None) -> None:
    """Drop the in-process cache. Pass ``endpoint`` to scope; no-arg
    clears everything. Called when an OAuth reconnect happens or a
    vendor reports schema drift."""
    if endpoint is None:
        _tools_cache.clear()
        return
    for key in list(_tools_cache):
        if key.startswith(f"{endpoint.rstrip('/')}::"):
            _tools_cache.pop(key, None)


__all__ = [
    "RemoteMCPClient",
    "RemoteMCPError",
    "RemoteMCPResponseFactory",
    "RemoteMCPResponseMediaType",
    "list_tools_cached",
    "invalidate_tools_cache",
]
