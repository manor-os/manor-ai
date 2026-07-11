"""Shared HTTP + envelope helpers for in-process MCP modules.

Every MCP wrapper in this directory follows the same shape:

  1. Build an ``httpx.AsyncClient(timeout=…)``
  2. Inject ``Authorization: Bearer <token>``
  3. Send the request
  4. Map 401/403/4xx to a friendly user-facing message
  5. Wrap success in the MCP "content envelope"
     (``{"content": [{"type":"text","text": json.dumps(...)}],
        "isError": False}``).

The boilerplate is ~30-40 LOC per file. This module collapses it to:

  ```python
  from packages.core.ai.mcp._http import McpHttpClient, mcp_ok, mcp_err

  _client = McpHttpClient("https://api.example.com/v1")

  async def call_tool(name, args, bearer_token):
      try:
          data = await _client.get(f"/things/{args['id']}", token=bearer_token)
          return mcp_ok(data)
      except Exception as exc:
          return mcp_err(str(exc))
  ```

Errors raised as ``McpHttpError(status, body)`` so wrappers can catch
typed failures (e.g. retry on 429) and let the rest bubble.

Why one file shared instead of per-vendor base classes:
  * Each vendor still owns its tool schemas + dispatch. This module is
    purely the HTTP + envelope plumbing — the parts that genuinely
    don't differ.
  * Inheritance forces structural decisions (token in header? in
    query? auth scheme?) that vary per-vendor. A helper class with
    ctor knobs is cleaner than a hierarchy.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 30.0
_MCP_MAX_PAYLOAD_CHARS = 12_000


# ── Errors ──────────────────────────────────────────────────────────────────


class McpHttpError(RuntimeError):
    """Raised on any non-2xx response. ``status`` is the HTTP code;
    ``body`` is the parsed JSON (or ``{"raw": "..."}`` if the body
    wasn't JSON). Wrappers may catch this to retry or to translate into
    domain-specific error text."""

    def __init__(self, status: int, body: Dict[str, Any], message: Optional[str] = None) -> None:
        self.status = status
        self.body = body
        self.message = message or self._extract_message(body) or f"HTTP {status}"
        super().__init__(f"HTTP {status}: {self.message}")

    @staticmethod
    def _extract_message(body: Dict[str, Any]) -> Optional[str]:
        if not isinstance(body, dict):
            return None
        for key in ("error_description", "error_message", "message", "errmsg", "error_msg"):
            v = body.get(key)
            if isinstance(v, str) and v:
                return v
        err = body.get("error")
        if isinstance(err, dict):
            for key in ("message", "description"):
                v = err.get(key)
                if isinstance(v, str) and v:
                    return v
        elif isinstance(err, str):
            return err
        return None


# ── Client ──────────────────────────────────────────────────────────────────


_BEARER = "bearer"
_QUERY_TOKEN = "query_token"  # token goes on ?access_token=...
_NONE = "none"

# Type alias for the auth-injection policy. Wrappers pass a string at
# ctor time; we apply it on every request.
AuthScheme = str


class McpHttpClient:
    """Async HTTP client tuned for MCP wrappers.

    One instance per provider:

      ``GitHub  = McpHttpClient(base="https://api.github.com",
                                 default_headers={"Accept": "...",
                                                  "X-GitHub-Api-Version": ...})``

    Auth: ``auth="bearer"`` (default) sends ``Authorization: Bearer
    {token}``; ``auth="query_token"`` adds ``?access_token=...`` (Meta-
    style); ``auth="none"`` for unauthenticated calls.
    """

    def __init__(
        self,
        base: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        default_headers: Optional[Dict[str, str]] = None,
        auth: AuthScheme = _BEARER,
    ) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.default_headers = dict(default_headers or {})
        self.auth = auth

    # ── verbs ─────────────────────────────────────────────────────────

    async def get(
        self,
        path: str,
        *,
        token: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        return await self._request("GET", path, token=token, params=params, headers=headers)

    async def post(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        token: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        form: bool = False,
    ) -> Dict[str, Any]:
        return await self._request(
            "POST", path,
            json_body=body if not form else None,
            form_body=body if form else None,
            token=token, params=params, headers=headers,
        )

    async def patch(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        token: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        return await self._request("PATCH", path, json_body=body, token=token, headers=headers)

    async def delete(
        self,
        path: str,
        *,
        token: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self._request("DELETE", path, token=token, params=params)

    # ── core ──────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        form_body: Optional[Dict[str, Any]] = None,
        token: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        url = self._url(path)
        merged_headers = {**self.default_headers, **(headers or {})}
        merged_params = dict(params or {})

        if token:
            if self.auth == _BEARER:
                merged_headers.setdefault("Authorization", f"Bearer {token}")
            elif self.auth == _QUERY_TOKEN:
                merged_params.setdefault("access_token", token)

        async with httpx.AsyncClient(timeout=self.timeout) as cx:
            r = await cx.request(
                method, url,
                params=merged_params or None,
                headers=merged_headers,
                json=json_body,
                data=form_body,
            )
        return self._handle(r)

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base}/{path.lstrip('/')}"

    @staticmethod
    def _handle(r: httpx.Response) -> Dict[str, Any]:
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:500]}
        if r.status_code >= 400:
            raise McpHttpError(r.status_code, data if isinstance(data, dict) else {"raw": data})
        # Some endpoints (DELETE, 204) return empty/non-dict bodies; pass
        # them through unchanged so callers see exactly what's there.
        return data if isinstance(data, dict) else {"data": data}


# ── MCP envelope helpers ────────────────────────────────────────────────────


def mcp_ok(data: Any, *, indent: int = 2) -> Dict[str, Any]:
    """Standard MCP success envelope. ``data`` is JSON-serialised
    (with truncation safety) and shipped as the ``text`` content
    block."""
    if isinstance(data, str):
        text = data
    else:
        try:
            text = json.dumps(data, ensure_ascii=False, indent=indent, default=str)
        except Exception:
            text = str(data)
    if len(text) > _MCP_MAX_PAYLOAD_CHARS:
        text = text[:_MCP_MAX_PAYLOAD_CHARS] + "\n… (truncated)"
    return {"content": [{"type": "text", "text": text}], "isError": False}


def mcp_err(message: str) -> Dict[str, Any]:
    """Standard MCP error envelope. The agent sees ``isError: True``
    and the human-readable message in the content block."""
    return {"content": [{"type": "text", "text": message}], "isError": True}


def mcp_safe_call(fn: Callable, *args, error_prefix: str = "") -> Dict[str, Any]:
    """Decorator-ish wrapper for very small ``call_tool`` functions:
    catches ``McpHttpError`` + ``Exception`` and returns ``mcp_err``.
    Most wrappers will inline a try/except for finer control; this is
    here for the simplest cases.
    """
    try:
        return mcp_ok(fn(*args))
    except McpHttpError as exc:
        return mcp_err(f"{error_prefix}{exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("MCP call failed")
        return mcp_err(f"{error_prefix}{type(exc).__name__}: {exc}")


__all__ = [
    "McpHttpClient",
    "McpHttpError",
    "mcp_ok",
    "mcp_err",
    "mcp_safe_call",
]
