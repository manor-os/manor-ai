"""Admin-only OAuth client management.

Hidden from the regular UI nav. URL: /__admin/oauth on the frontend,
endpoints under /api/v1/admin/oauth-clients here. Requires the caller's
role to be ``admin`` or ``owner`` — non-admins get 403, no leakage.

Backed by ``packages.core.services.oauth_provider_config``:
  - GET   list  → reads current state per provider (client_id + source,
                  never the secret plaintext)
  - PUT   set   → save_oauth_config (encrypts via Vault, marks source="ui")
  - DELETE      → clears credential_ref + the UI override flag so the
                  next env bootstrap (or restart) takes over again
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.database import get_db
from packages.core.models.mcp import MCPServer
from packages.core.models.user import User
from packages.core.permissions import user_is_effective_entity_admin
from packages.core.services.oauth_provider_config import (
    _PROVIDER_OAUTH_META,
    is_google_provider,
    oauth_client_secret_required,
    resolve_oauth_config,
    save_oauth_config,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin/oauth-clients", tags=["admin-oauth"])


async def _require_admin(db: AsyncSession, user: User) -> None:
    if not await user_is_effective_entity_admin(db, user):
        raise HTTPException(403, "Admin role required")


class OAuthClientStatus(BaseModel):
    server_key: str
    name: str
    client_id: Optional[str]
    has_secret: bool
    client_secret_required: bool = True
    source: str               # "env" | "ui" | "none"
    scopes: Optional[str]
    configured: bool          # client_id and any required secret resolved
    client_id_env_var: str    # for the admin's reference
    client_secret_env_var: str
    redirect_uri: str         # exact callback to whitelist in the provider console


class UpdateOAuthClientRequest(BaseModel):
    client_id: str
    client_secret: str = ""
    scopes: Optional[str] = None


@router.get("", response_model=list[OAuthClientStatus])
async def list_oauth_clients(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[OAuthClientStatus]:
    await _require_admin(db, user)

    app_url = os.getenv("APP_URL", "http://localhost:3010").rstrip("/")
    out: list[OAuthClientStatus] = []
    for server_key, meta in _PROVIDER_OAUTH_META.items():
        server = (await db.execute(
            select(MCPServer).where(MCPServer.server_key == server_key)
        )).scalar_one_or_none()

        cfg = await resolve_oauth_config(db, server_key)
        client_id = cfg.client_id if cfg else None
        configured = cfg is not None
        source = "none"
        scopes = meta["scopes"]
        has_secret = False
        if server:
            dc = server.default_config if isinstance(server.default_config, dict) else {}
            source = dc.get("_oauth_source") or ("db" if dc.get("oauth_client_id") else "none")
            if not is_google_provider(server_key):
                scopes = dc.get("oauth_scopes") or scopes
            has_secret = bool(server.credential_ref) or bool(dc.get("oauth_client_secret"))

        # If neither DB nor env had secret_present, ``configured`` is
        # already false. We just expose has_secret for the UI to render
        # "secret set" vs "secret missing" correctly.
        out.append(OAuthClientStatus(
            server_key=server_key,
            name=(server.name if server else server_key),
            client_id=client_id,
            has_secret=has_secret,
            client_secret_required=oauth_client_secret_required(server_key),
            source=source,
            scopes=scopes,
            configured=configured,
            client_id_env_var=meta["client_id_env"],
            client_secret_env_var=meta["client_secret_env"],
            redirect_uri=f"{app_url}{cfg.redirect_path}" if cfg
            else f"{app_url}/api/v1/integrations/oauth/{server_key}/callback",
        ))
    return out


@router.put("/{server_key}")
async def update_oauth_client(
    server_key: str,
    body: UpdateOAuthClientRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    await _require_admin(db, user)
    if server_key not in _PROVIDER_OAUTH_META:
        raise HTTPException(404, f"Unknown OAuth provider: {server_key}")
    if not body.client_id.strip() or (
        oauth_client_secret_required(server_key) and not body.client_secret.strip()
    ):
        raise HTTPException(400, "Required OAuth client credentials are missing")

    saved = await save_oauth_config(
        db, server_key,
        client_id=body.client_id.strip(),
        client_secret=body.client_secret.strip(),
        scopes=(body.scopes or None),
    )
    if not saved:
        raise HTTPException(404, f"MCPServer row missing for {server_key}")
    await db.commit()
    logger.info("OAuth client updated via admin UI: %s by %s", server_key, user.id)
    return {"ok": True}


class OAuthClientHealth(BaseModel):
    server_key: str
    ok: bool
    status_code: Optional[int] = None
    detail: str = ""


@router.get("/{server_key}/health", response_model=OAuthClientHealth)
async def check_oauth_client_health(
    server_key: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OAuthClientHealth:
    """Probe the provider's authorize_url with the configured client_id.

    A 200/302/400 response means the provider recognized the client_id;
    401/403 = wrong client_id. Network failure = unreachable. We don't
    actually complete the OAuth flow — just verify the entry point is
    valid for our credentials.
    """
    await _require_admin(db, user)
    if server_key not in _PROVIDER_OAUTH_META:
        raise HTTPException(404, f"Unknown OAuth provider: {server_key}")

    cfg = await resolve_oauth_config(db, server_key)
    if not cfg:
        return OAuthClientHealth(
            server_key=server_key, ok=False,
            detail="No client_id/secret configured",
        )

    import httpx
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": "https://example.com/cb",
        "response_type": "code",
        "scope": cfg.scopes.split()[0] if cfg.scopes else "",
        "state": "healthcheck",
    }
    if server_key == "robinhood":
        import base64
        import hashlib
        import secrets
        from packages.core.services.oauth_provider_config import apply_authorize_param_conventions

        params["redirect_uri"] = os.getenv("APP_URL", "http://localhost:3010").rstrip("/") + cfg.redirect_path
        params["state"] = secrets.token_urlsafe(24)
        params["code_challenge"] = base64.urlsafe_b64encode(
            hashlib.sha256(secrets.token_urlsafe(64).encode()).digest()
        ).rstrip(b"=").decode()
        params["code_challenge_method"] = "S256"
        params = apply_authorize_param_conventions(cfg, params)
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as cx:
            resp = await cx.get(
                cfg.authorize_url,
                params=params,
            )
        # 200 = login screen rendered; 302 = redirect to login
        # (typical); 400 might come back if redirect_uri doesn't match
        # an allow-list — still proves the client_id is recognized.
        # 401/403 = bad client_id; 404 = provider misconfigured.
        ok = resp.status_code in (200, 302, 303, 307, 400)
        if server_key == "robinhood":
            ok = resp.status_code in (200, 302, 303, 307)
        return OAuthClientHealth(
            server_key=server_key,
            ok=ok,
            status_code=resp.status_code,
            detail=(
                "OAuth entry reachable; client registration and user consent are not verified by this probe."
                if ok and server_key == "robinhood"
                else ("OK" if ok else f"Provider returned HTTP {resp.status_code}")
            ),
        )
    except httpx.RequestError as exc:
        return OAuthClientHealth(
            server_key=server_key, ok=False,
            detail=f"Provider unreachable: {exc}",
        )


@router.delete("/{server_key}")
async def reset_oauth_client(
    server_key: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Clear the UI override on this provider so env bootstrap
    re-seeds it on next restart."""
    await _require_admin(db, user)
    if server_key not in _PROVIDER_OAUTH_META:
        raise HTTPException(404, f"Unknown OAuth provider: {server_key}")

    server = (await db.execute(
        select(MCPServer).where(MCPServer.server_key == server_key)
    )).scalar_one_or_none()
    if not server:
        raise HTTPException(404, f"MCPServer row missing for {server_key}")

    cfg = dict(server.default_config or {})
    cfg.pop("oauth_client_id", None)
    cfg.pop("oauth_client_secret", None)
    cfg.pop("oauth_scopes", None)
    cfg.pop("_oauth_source", None)
    cfg.pop("_credential_context", None)
    server.default_config = cfg
    server.credential_ref = None
    server.credential_scheme = None
    await db.commit()
    logger.info("OAuth client cleared via admin UI: %s by %s", server_key, user.id)
    return {"ok": True}
