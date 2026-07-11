"""In-app Nango Connect flow.

Goal: end-user clicks "Connect Twitter" inside manor-os and is taken
through the OAuth dance without ever knowing Nango exists. Nango runs
in our docker stack; the operator (admin) only has to configure the
Integration in Nango admin once per platform; thereafter every
manor-os user can self-connect.

Flow:

  1. Frontend → POST /api/v1/integrations/nango/connect-session
       Backend reads the entity's Nango secret_key from the
       Integration(provider='nango') row, calls Nango's
       /connect/sessions endpoint, returns the short-lived session
       token.

  2. Frontend opens https://{nango_public_url}/connect?token=... in a
     popup using Nango's Connect UI. Nango walks the user through
     OAuth and writes a new Connection on success.

  3. Frontend → POST /api/v1/integrations/nango/connections/sync
       Backend re-fetches the entity's connections from Nango and
       upserts them as ``Integration(provider=<platform>)`` rows so
       agents can discover them via the standard integration channel.

The Nango "tenancy" model: every connection has an ``end_user.id``
which we set to the entity_id, so a single Nango server can serve all
manor-os entities cleanly.
"""
from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.mcp.nango import _NANGO_BASE
from packages.core.credentials import get_credential_service
from packages.core.database import get_db
from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration
from packages.core.models.user import User
from packages.core.services.provider_keys import canonical_provider_key, provider_key_aliases
from apps.api.deps import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/integrations/nango",
    tags=["integrations-nango"],
)


# ── Schemas ─────────────────────────────────────────────────────────────────

class StartConnectRequest(BaseModel):
    """Optional restrictions on which platforms the connect-session
    can authenticate. Empty means "all integrations Nango has configured."""
    provider_config_keys: Optional[list[str]] = None


class StartConnectResponse(BaseModel):
    session_token: str
    nango_connect_url: str
    """Full URL the frontend should open in a popup. Combines
    Nango's Connect UI base + the session token."""


class SyncConnectionsResponse(BaseModel):
    upserted: int
    providers: list[str]


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _load_nango_secret(db: AsyncSession, entity_id: str) -> str:
    """Resolve the Nango admin secret_key.

    Env-first (``NANGO_SECRET_KEY``) for the standard self-hosted deploy,
    falls back to per-entity ``Integration(provider='nango')`` row for
    legacy / multi-tenant setups.
    """
    from packages.core.ai.mcp.nango import get_nango_secret

    secret = await get_nango_secret(db, entity_id)
    if not secret:
        raise HTTPException(
            400,
            "Nango is not configured. Set the NANGO_SECRET_KEY environment "
            "variable (paste the admin secret_key from http://localhost:3003 "
            "→ Settings), or add an Integration with provider='nango'.",
        )
    return secret


def _nango_public_base() -> str:
    """Public Nango URL for browser redirects/popups.

    ``NANGO_BASE_URL`` is the Docker-internal service URL used by the API
    container. Browsers need the public hostname instead.
    """
    return (
        os.environ.get("NANGO_PUBLIC_URL")
        or os.environ.get("NANGO_SERVER_URL")
        or _NANGO_BASE
    ).rstrip("/")


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/connect-session", response_model=StartConnectResponse)
async def start_connect_session(
    req: StartConnectRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Build the Nango OAuth popup URL.

    Nango 0.36 supports a direct ``/oauth/connect/<provider>`` URL with
    ``public_key`` + ``connection_id`` query params — no Connect Session
    API required. The popup completes OAuth inside Nango, stores the
    Connection under the given connection_id, then we mirror it into
    our Integration table via ``/connections/sync``.
    """
    # Need both NANGO_SECRET_KEY (validates Nango is set up) and
    # NANGO_PUBLIC_KEY (parameter the popup URL needs).
    await _load_nango_secret(db, user.entity_id)
    public_key = os.environ.get("NANGO_PUBLIC_KEY", "").strip()
    if not public_key:
        raise HTTPException(
            500,
            "NANGO_PUBLIC_KEY is not configured. Copy from Nango admin "
            "(http://localhost:3003 → Settings → Public Key) into .env.",
        )

    keys = req.provider_config_keys or []
    if len(keys) != 1:
        raise HTTPException(
            400,
            "Direct Connect requires exactly one provider_config_key. "
            "(Nango 0.36 doesn't support multi-platform popups; loop "
            "from the frontend per platform.)",
        )
    provider_config_key = keys[0]

    # New Nango connects should create distinct accounts so providers
    # like LinkedIn/Gmail can support multi-account cards. The user id
    # is embedded for auditability; agents still resolve by entity.
    connection_id = (
        f"{user.entity_id}--{user.id}--{provider_config_key}--{generate_ulid()}"
    )

    query = urlencode({
        "public_key": public_key,
        "connection_id": connection_id,
    })
    nango_url = f"{_nango_public_base()}/oauth/connect/{provider_config_key}?{query}"
    return StartConnectResponse(
        session_token="",  # legacy field, unused in 0.36 flow
        nango_connect_url=nango_url,
    )


@router.post("/connections/sync", response_model=SyncConnectionsResponse)
async def sync_connections(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pull every Connection Nango has for this entity and mirror it
    into our ``integrations`` table so the rest of the platform (agent
    pickers, MCP runtime, billing scopes) treats them like any other
    entity-scope integration."""
    secret = await _load_nango_secret(db, user.entity_id)

    try:
        async with httpx.AsyncClient(timeout=30.0) as cx:
            r = await cx.get(
                f"{_NANGO_BASE}/connection",
                params={"end_user_id": user.entity_id},
                headers={"Authorization": f"Bearer {secret}"},
            )
            r.raise_for_status()
            body = r.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            502,
            f"Nango list connections failed: {exc.response.status_code} {exc.response.text[:200]}",
        )

    raw_conns = body.get("connections") or (body if isinstance(body, list) else [])

    cs = get_credential_service()
    upserted = 0
    providers: list[str] = []

    for c in raw_conns:
        provider_config_key = c.get("provider_config_key") or c.get("provider")
        connection_id = c.get("connection_id")
        if not provider_config_key or not connection_id:
            continue
        provider_key = canonical_provider_key(provider_config_key)
        if not str(connection_id).startswith(f"{user.entity_id}--"):
            logger.debug(
                "Skipping Nango connection outside current entity",
                extra={
                    "entity_id": user.entity_id,
                    "provider": provider_config_key,
                    "connection_id": connection_id,
                },
            )
            continue

        candidates = (await db.execute(
            select(Integration).where(
                Integration.entity_id == user.entity_id,
                Integration.provider.in_(provider_key_aliases(provider_key)),
                Integration.status == "active",
            )
        )).scalars().all()
        existing = next(
            (
                row for row in candidates
                if ((row.config or {}).get("nango") or {}).get("connection_id") == connection_id
            ),
            None,
        )
        if existing is None:
            existing = Integration(
                id=generate_ulid(),
                entity_id=user.entity_id,
                provider=provider_key,
                status="active",
                config={},
                credentials={},
            )
            db.add(existing)
        else:
            existing.provider = provider_key

        # Store the Nango connection id + the platform key in config
        # so resolution code can tell "this Integration is backed by
        # Nango" vs "this is a hand-rolled credential".
        existing_config = dict(existing.config or {})
        existing_config["nango"] = {
            "connection_id": connection_id,
            "provider_config_key": provider_config_key,
            "synced_at": c.get("created_at") or c.get("updated_at"),
            "connected_by_user_id": user.id,
        }
        if provider_key == "linkedin":
            profile = await _fetch_linkedin_profile_via_nango(
                secret=secret,
                provider_config_key=provider_key,
                connection_id=connection_id,
            )
            if profile:
                existing_config["profile"] = profile
        existing.config = existing_config
        existing.status = "active"

        # Cache an indirection ref on credential_ref so the runtime
        # knows to fetch via Nango when leasing. The actual access
        # tokens stay inside Nango — manor-os never holds them.
        cs.store_integration(
            existing,
            {
                "via": "nango",
                "connection_id": connection_id,
                "provider_config_key": provider_config_key,
            },
        )

        providers.append(provider_key)
        upserted += 1

    await db.commit()
    return SyncConnectionsResponse(upserted=upserted, providers=sorted(set(providers)))


async def _fetch_linkedin_profile_via_nango(
    *, secret: str, provider_config_key: str, connection_id: str,
) -> dict | None:
    """Best-effort profile fetch for account labels on the Integrations card."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as cx:
            r = await cx.get(
                f"{_NANGO_BASE}/proxy/v2/userinfo",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Provider-Config-Key": provider_config_key,
                    "Connection-Id": connection_id,
                },
            )
            if not r.is_success:
                return None
            data = r.json()
    except Exception:  # noqa: BLE001
        logger.debug("Could not fetch LinkedIn profile via Nango", exc_info=True)
        return None

    name = data.get("name") or "LinkedIn account"
    email = data.get("email")
    return {
        "sub": data.get("sub"),
        "name": name,
        "email": email,
        "picture": data.get("picture"),
        "display_name": f"{name} <{email}>" if email else name,
    }
