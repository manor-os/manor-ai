"""In-process calls from MCP tools into Manor's own REST API.

A tool never re-implements a route: it sends the same request a browser would,
through the full ASGI app (middleware, dependencies, permission checks, commit
handling), as the caller. The MCP access token is audience-bound and rejected by
the REST API, so each call carries a short-lived API token minted for the same
user, team and token version; it never leaves the process.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx
from starlette.types import ASGIApp

from packages.core.services.auth_service import create_access_token

_INTERNAL_TOKEN_MINUTES = 5
# Tool calls such as Blueprint installs or synchronous workflow runs can be slow.
_TIMEOUT_SECONDS = 600.0


class ManorApiError(Exception):
    """A REST call failed; ``detail`` is the route's own error body."""

    def __init__(self, status_code: int, detail: Any):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.detail = detail


class ManorApi:
    def __init__(self, app: ASGIApp, *, claims: dict, client: Optional[tuple[str, int]] = None):
        self._app = app
        self._claims = claims
        self._client = client or ("127.0.0.1", 0)

    def _token(self) -> str:
        claims = self._claims
        return create_access_token(
            str(claims["sub"]),
            str(claims["entity_id"]),
            str(claims.get("role") or ""),
            token_version=int(claims.get("token_version") or 0),
            expires_minutes=_INTERNAL_TOKEN_MINUTES,
            mfa_authenticated="mfa" in (claims.get("amr") or []),
            # The REST route sees an agent acting for the user, with what the
            # user granted it (a token from before scopes means just "mcp").
            scopes=list(claims.get("scp") or ["mcp"]),
            actor=f"mcp:{claims.get('azp') or 'client'}",
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[dict] = None,
        files: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> httpx.Response:
        transport = httpx.ASGITransport(app=self._app, client=self._client)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://manor-mcp.internal", timeout=_TIMEOUT_SECONDS,
        ) as http:
            return await http.request(
                method,
                path,
                json=json_body,
                params={k: v for k, v in (params or {}).items() if v is not None},
                files=files,
                headers={**(headers or {}), "Authorization": f"Bearer {self._token()}"},
            )

    @staticmethod
    def _body(response: httpx.Response) -> Any:
        try:
            return response.json() if response.content else None
        except (ValueError, json.JSONDecodeError):
            return response.text

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            body = self._body(response)
            if isinstance(body, dict) and "detail" in body:
                body = body["detail"]
            raise ManorApiError(response.status_code, body)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[dict] = None,
        files: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> Any:
        response = await self._send(
            method, path, json_body=json_body, params=params, files=files, headers=headers,
        )
        self._raise_for_status(response)
        return self._body(response)

    async def download(
        self, path: str, *, method: str = "GET", json_body: Any = None, **params: Any,
    ) -> tuple[bytes, str]:
        """A route's raw response bytes and media type (for file downloads)."""
        response = await self._send(method, path, json_body=json_body, params=params)
        self._raise_for_status(response)
        content_type = response.headers.get("content-type") or "application/octet-stream"
        return response.content, content_type.split(";")[0].strip()

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return await self.request("POST", path, json_body=body, **kwargs)

    async def put(self, path: str, body: Any = None) -> Any:
        return await self.request("PUT", path, json_body=body)

    async def patch(self, path: str, body: Any = None) -> Any:
        return await self.request("PATCH", path, json_body=body)
