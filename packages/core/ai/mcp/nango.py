"""Nango MCP adapter — self-hosted OAuth + API multiplexer for 200+
SaaS platforms.

Why this exists: same motivation as the (paid) Composio adapter, but
free / open-source / self-hosted. Nango handles the OAuth dance + token
refresh; this adapter exposes its proxy API as a single MCP server so
agents can call any connected provider without per-platform code.

Three tools:

  * ``nango_list_providers``    — list integrations configured in
                                   this Nango instance (the operator
                                   set them up in Nango admin).

  * ``nango_list_connections``  — list active connections an entity
                                   has across all providers.

  * ``nango_proxy``             — make an authenticated HTTP call to a
                                   provider via Nango. Nango injects
                                   OAuth headers + refreshes tokens
                                   automatically.

The bearer_token at this layer is **manor-os's Nango secret key**,
read from the entity's Integration(provider='nango') row at call time.
Connection IDs (per-end-user) are stored alongside in
``Integration.config.connections``.

Compared to Composio (which has a curated tool catalog like
TWITTER_CREATE_TWEET): Nango doesn't ship platform-specific actions,
so the agent has to know the underlying API endpoint shape (e.g.
POST https://api.x.com/2/tweets). This is the price of self-hosted
freedom; in practice agents already know mainstream API shapes from
training data.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


# Default to the docker-compose service name; overridable via env so a
# prod deployment can point at a hosted Nango or a different host.
_NANGO_BASE = os.environ.get("NANGO_BASE_URL", "http://nango-server:3003").rstrip("/")
_TIMEOUT = 30.0
_MAX_PAYLOAD_CHARS = 12_000


async def get_nango_secret(db: Any = None, entity_id: str | None = None) -> Optional[str]:
    """Resolve the Nango admin secret_key.

    Priority:
      1. ``NANGO_SECRET_KEY`` env var — the normal case for self-hosted
         and SaaS deployments where Nango is one shared instance.
      2. Per-entity ``Integration(provider='nango').credentials.secret_key``
         row — legacy / multi-tenant escape hatch.

    Returns None if neither is configured. Callers should treat None as
    "Nango not available; skip the feature gracefully."
    """
    env_secret = os.environ.get("NANGO_SECRET_KEY")
    if env_secret:
        return env_secret.strip() or None

    if db is None or not entity_id:
        return None

    try:
        from sqlalchemy import select
        from packages.core.credentials import Requester, get_credential_service
        from packages.core.models.document import Integration

        row = (await db.execute(
            select(Integration).where(
                Integration.entity_id == entity_id,
                Integration.provider == "nango",
                Integration.status == "active",
            )
        )).scalar_one_or_none()
        if row is None:
            return None

        creds = get_credential_service().lease_integration(
            row,
            requester=Requester(kind="agent", id=entity_id),
            reason="nango_secret_lookup",
        )
        return (creds or {}).get("secret_key") or None
    except Exception:  # noqa: BLE001
        logger.exception("get_nango_secret: DB fallback failed")
        return None


# ── MCP protocol ────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "nango_list_providers",
            "description": (
                "List integrations available on this Nango server "
                "(Twitter, Slack, Notion, Stripe, Linear, …). Use to "
                "see what the operator has configured before proposing "
                "a connect / proxy call."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "nango_list_connections",
            "description": (
                "List active OAuth connections this Nango instance "
                "holds for the current entity (one connection per "
                "provider × end-user)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "provider_config_key": {"type": "string", "description": "Optional — filter by integration id."},
                },
            },
        },
        {
            "name": "nango_proxy",
            "description": (
                "Make an authenticated HTTP request to a provider via "
                "Nango. Nango injects OAuth Bearer / API-key headers "
                "and refreshes tokens automatically. You supply the "
                "raw endpoint path + method + body following the "
                "provider's API docs."
            ),
            "parameters": {
                "type": "object",
                "required": ["provider_config_key", "connection_id", "method", "endpoint"],
                "properties": {
                    "provider_config_key": {"type": "string", "description": "The integration id, e.g. 'twitter', 'slack'."},
                    "connection_id": {"type": "string", "description": "Per-entity connection id (returned from nango_list_connections)."},
                    "method": {"enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                    "endpoint": {"type": "string", "description": "Path or full URL of the provider's API endpoint."},
                    "params": {"type": "object", "description": "Query string parameters."},
                    "headers": {"type": "object", "description": "Extra request headers (auth headers added by Nango)."},
                    "data": {"type": "object", "description": "Request body for POST/PUT/PATCH."},
                },
            },
        },
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    """``bearer_token`` here is the manor-os entity's Nango secret_key
    (stored in Integration(provider='nango').credentials.secret_key)."""
    if not bearer_token:
        return _error(
            "Nango not connected. Add an Integration with provider='nango' "
            "and your Nango admin secret_key (visit http://localhost:3003 "
            "if self-hosting, or your hosted Nango dashboard's Settings)."
        )

    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"Unknown nango tool: {name}")

    try:
        result = await handler(arguments, bearer_token)
        return _content(result)
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        return _error(f"Nango HTTP {exc.response.status_code}: {body}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Nango tool %s crashed", name)
        return _error(f"Nango call failed: {exc}")


# ── Handlers ────────────────────────────────────────────────────────────────

async def _list_providers(args: Dict[str, Any], secret_key: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.get(
            f"{_NANGO_BASE}/config",
            headers={"Authorization": f"Bearer {secret_key}"},
        )
        r.raise_for_status()
        body = r.json()

    providers = []
    for cfg in (body.get("configs") or body if isinstance(body, list) else []):
        providers.append({
            "provider_config_key": cfg.get("unique_key") or cfg.get("provider_config_key"),
            "provider": cfg.get("provider"),
            "auth_mode": cfg.get("auth_mode") or cfg.get("oauth_type"),
        })
    return {"count": len(providers), "providers": providers}


async def _list_connections(args: Dict[str, Any], secret_key: str) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if pck := args.get("provider_config_key"):
        params["provider_config_key"] = pck

    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.get(
            f"{_NANGO_BASE}/connection",
            params=params,
            headers={"Authorization": f"Bearer {secret_key}"},
        )
        r.raise_for_status()
        body = r.json()

    conns = []
    for c in (body.get("connections") or body if isinstance(body, list) else []):
        conns.append({
            "connection_id": c.get("connection_id"),
            "provider_config_key": c.get("provider_config_key"),
            "provider": c.get("provider"),
            "created_at": c.get("created_at"),
        })
    return {"count": len(conns), "connections": conns}


async def _proxy(args: Dict[str, Any], secret_key: str) -> Dict[str, Any]:
    pck = (args.get("provider_config_key") or "").strip()
    conn = (args.get("connection_id") or "").strip()
    method = (args.get("method") or "GET").upper()
    endpoint = (args.get("endpoint") or "").strip()
    if not (pck and conn and endpoint):
        return {"error": "provider_config_key, connection_id, endpoint are required"}

    headers = {
        "Authorization": f"Bearer {secret_key}",
        "Provider-Config-Key": pck,
        "Connection-Id": conn,
    }
    extra_headers = args.get("headers") or {}
    if extra_headers:
        # Nango forwards headers on the underlying request, but we have
        # to namespace them so they don't collide with auth headers.
        for k, v in extra_headers.items():
            headers[f"Nango-Proxy-{k}"] = str(v)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.request(
            method,
            f"{_NANGO_BASE}/proxy/{endpoint.lstrip('/')}",
            params=args.get("params") or None,
            json=args.get("data") if method in ("POST", "PUT", "PATCH") else None,
            headers=headers,
        )

    # Try JSON, fall back to text. Truncate huge bodies before returning.
    try:
        body = r.json()
        raw = json.dumps(body, ensure_ascii=False, default=str)
    except Exception:
        body = r.text
        raw = str(body)

    if len(raw) > _MAX_PAYLOAD_CHARS:
        return {
            "status_code": r.status_code,
            "truncated": True,
            "preview": raw[:_MAX_PAYLOAD_CHARS],
            "full_length": len(raw),
        }
    return {"status_code": r.status_code, "body": body}


_HANDLERS = {
    "nango_list_providers": _list_providers,
    "nango_list_connections": _list_connections,
    "nango_proxy": _proxy,
}


# ── MCP envelope helpers ────────────────────────────────────────────────────

def _content(payload: Any) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}],
        "isError": False,
    }


def _error(message: str) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


# ── Server-side helper for the OAuth connect flow router ───────────────────
#
# Used by ``apps/api/routers/nango_oauth.py`` (the in-app Connect flow)
# rather than the agent — kept here so the Nango-specific endpoint
# knowledge lives in one place.

async def create_connect_session(
    secret_key: str,
    *,
    end_user_id: str,
    provider_config_keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create a Nango Connect Session and return the session token the
    frontend opens in a popup. Nango handles the OAuth dance + writes
    a Connection on success.

    Docs: POST /connect/sessions
    """
    payload: Dict[str, Any] = {
        "end_user": {"id": end_user_id},
    }
    if provider_config_keys:
        payload["allowed_integrations"] = list(provider_config_keys)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.post(
            f"{_NANGO_BASE}/connect/sessions",
            headers={
                "Authorization": f"Bearer {secret_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        r.raise_for_status()
        return r.json()
