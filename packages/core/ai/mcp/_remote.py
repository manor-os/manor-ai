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

Streaming + SSE notification channels are not implemented — Manor's
agent loop does request/response, not push.

Auth
────
``access_token`` is the OAuth access_token Manor obtained through
its standard ``/oauth/{server_key}/start`` flow. The vendor's MCP
server validates it on every request via the ``Authorization``
header (or, on a few vendors, a query param — pass ``token_in='query'``
to switch).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 30.0
_LIST_TOOLS_TIMEOUT = 15.0
_PROTOCOL_VERSION = "2024-11-05"   # MCP spec version Manor speaks


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
        token_in: str = "header",   # "header" | "query"
        client_name: str = "manor-ai",
        client_version: str = "1.0.0",
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.access_token = access_token
        self.timeout = timeout
        self.token_in = token_in
        self.client_name = client_name
        self.client_version = client_version
        self._request_seq = 0

    def _next_id(self) -> int:
        self._request_seq += 1
        return self._request_seq

    def _headers(self) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.token_in == "header" and self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    def _params(self, base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p = dict(base or {})
        if self.token_in == "query" and self.access_token:
            p["access_token"] = self.access_token
        return p

    # ── JSON-RPC core ────────────────────────────────────────────────

    async def _rpc(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as cx:
                r = await cx.post(
                    self.endpoint,
                    json=body,
                    headers=self._headers(),
                    params=self._params() or None,
                )
        except httpx.HTTPError as exc:
            raise RemoteMCPError(
                code=-1,
                message=f"transport: {type(exc).__name__}: {exc}",
            ) from exc

        if r.status_code == 401:
            raise RemoteMCPError(
                code=-32001,
                message="OAuth token rejected by vendor MCP server. Reconnect.",
            )
        if r.status_code >= 400 and r.status_code != 200:
            raise RemoteMCPError(
                code=r.status_code,
                message=f"HTTP {r.status_code}: {r.text[:300]}",
            )

        try:
            data = r.json()
        except Exception:
            raise RemoteMCPError(
                code=-32700,
                message=f"Vendor returned non-JSON: {r.text[:200]}",
            )

        if "error" in data and data["error"]:
            err = data["error"]
            raise RemoteMCPError(
                code=err.get("code", -32603),
                message=err.get("message", "Unknown JSON-RPC error"),
                data=err.get("data"),
            )
        return data.get("result", {})

    # ── Lifecycle ────────────────────────────────────────────────────

    async def initialize(self) -> Dict[str, Any]:
        """MCP handshake — call once before tools/list. Some servers
        require it; others tolerate skipping. We always send it so we
        match the protocol spec.
        """
        return await self._rpc(
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

    # ── Tool surface ─────────────────────────────────────────────────

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Discover the vendor's tool catalog. Returns the raw MCP
        ``tools/list`` payload — list of ``{name, description, inputSchema}``.
        """
        result = await self._rpc(
            "tools/list", {}, timeout=_LIST_TOOLS_TIMEOUT,
        )
        return list(result.get("tools") or [])

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


def _cache_key(endpoint: str, token_prefix: str) -> str:
    return f"{endpoint}::{token_prefix}"


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
    key = _cache_key(endpoint, access_token[:16])
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
    "list_tools_cached",
    "invalidate_tools_cache",
]
