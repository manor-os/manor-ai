"""Single Meta Graph API client used by every caller in Manor.

Three places in Manor talk to ``graph.facebook.com``:

  * ``packages/core/ai/mcp/facebook.py``           — Pages + Messenger MCP tools
  * ``packages/core/services/channels/facebook_adapter.py`` — webhook callbacks
  * ``packages/core/services/integration_health.py`` — WhatsApp probes

Before this module each had its own private ``_get``/``_post`` helper
plus a near-identical error parser. This client centralizes:

  * the API version pin (sourced from ``external_api_versions.META_GRAPH``)
  * httpx client setup + timeouts
  * the Graph error envelope (code / error_subcode / message)
  * a typed exception (``MetaGraphError``) so callers can either
    re-raise or convert to user-facing text once.

Auth model: every call takes ``token`` (User Access Token, Page Access
Token, or Cloud-API system user token — the client doesn't care which).
The ``form`` vs ``json`` body distinction matters because Pages/comments
take ``application/x-www-form-urlencoded`` while Messenger Send-API and
WhatsApp Cloud insist on JSON. ``post(json_body=True)`` switches.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from packages.core.external_api_versions import META_GRAPH

logger = logging.getLogger(__name__)


_BASE = "https://graph.facebook.com"
_DEFAULT_TIMEOUT = 30.0


class MetaGraphError(RuntimeError):
    """Raised on any non-2xx Graph response or any embedded ``error``
    object in the JSON body. ``code`` / ``subcode`` mirror Meta's docs:
    https://developers.facebook.com/docs/graph-api/guides/error-handling.
    """

    def __init__(
        self,
        code: int,
        message: str,
        *,
        subcode: Optional[int] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.code = code
        self.subcode = subcode
        self.message = message
        self.body = body or {}
        suffix = f"/{subcode}" if subcode else ""
        super().__init__(f"Facebook Graph error {code}{suffix}: {message}")


class MetaGraphClient:
    """Thin async wrapper over Meta's Graph API.

    One client instance per module / per caller is fine — there's no
    per-process state. The version is pulled from the central pin so a
    bump in ``external_api_versions.META_GRAPH`` propagates here
    automatically.
    """

    def __init__(
        self,
        *,
        version: Optional[str] = None,
        base: str = _BASE,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self.version = version or META_GRAPH.value
        self.base = f"{base.rstrip('/')}/{self.version}"
        self.timeout = timeout

    # ── HTTP verbs ─────────────────────────────────────────────────────

    async def get(
        self,
        path: str,
        *,
        token: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        p = dict(params or {})
        p["access_token"] = token
        async with httpx.AsyncClient(timeout=self.timeout) as cx:
            r = await cx.get(self._url(path), params=p)
        return self._handle(r)

    async def post(
        self,
        path: str,
        body: Dict[str, Any],
        *,
        token: str,
        json_body: bool = False,
    ) -> Dict[str, Any]:
        url = self._url(path)
        async with httpx.AsyncClient(timeout=self.timeout) as cx:
            if json_body:
                # Messenger Send API + WhatsApp Cloud /messages need
                # the access_token on the query string, body stays
                # pure JSON (Meta rejects mixed forms).
                r = await cx.post(url, params={"access_token": token}, json=body)
            else:
                p = dict(body)
                p["access_token"] = token
                r = await cx.post(url, data=p)
        return self._handle(r)

    async def delete(
        self,
        path: str,
        *,
        token: str,
    ) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as cx:
            r = await cx.delete(
                self._url(path),
                params={"access_token": token},
            )
        return self._handle(r)

    # ── Internals ──────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}" if not path.startswith("http") else path

    @staticmethod
    def _handle(r: httpx.Response) -> Dict[str, Any]:
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:500]}
        if r.status_code >= 400 or "error" in data:
            err = data.get("error") or {}
            raise MetaGraphError(
                code=err.get("code") or r.status_code,
                subcode=err.get("error_subcode"),
                message=err.get("message") or str(data)[:300],
                body=data,
            )
        return data


# Module-level singleton — every caller can just import this.
graph = MetaGraphClient()


__all__ = ["MetaGraphClient", "MetaGraphError", "graph"]
