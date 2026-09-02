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

import hashlib
import hmac
import json
import logging
import os
from typing import Any, Optional
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.mcp.nango import _NANGO_BASE
from packages.core.credentials import get_credential_service
from packages.core.database import get_db
from packages.core.external_api_versions import META_GRAPH
from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration
from packages.core.models.user import User
from packages.core.permissions import Permission, check_effective_user_permission
from packages.core.services.integration_account_service import (
    IntegrationAccountKind,
    lock_runtime_integration_account_scope,
    normalize_runtime_integration_account_defaults,
)
from packages.core.services.provider_keys import canonical_provider_key, provider_key_aliases
from apps.api.deps import get_current_user
from packages.core.services.whatsapp_business_config import (
    load_whatsapp_business_config,
)
from packages.core.services.whatsapp_business_provisioning import (
    delete_nango_connection,
    provision_whatsapp_business_number,
)

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
    replace_integration_id: Optional[str] = None


class StartConnectResponse(BaseModel):
    session_token: str
    nango_connect_url: str
    connection_id: str
    provider_config_key: str
    """Full URL the frontend should open in a popup. Combines
    Nango's Connect UI base + the session token."""


class SyncConnectionsRequest(BaseModel):
    expected_connection_id: Optional[str] = None
    expected_provider_config_key: Optional[str] = None
    replace_integration_id: Optional[str] = None
    whatsapp_waba_id: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None


class SyncConnectionsResponse(BaseModel):
    upserted: int
    providers: list[str]
    integration_id: str | None = None
    readiness_code: str | None = None
    provisioning_pending: bool = False
    provisioning_detail: str | None = None


async def _sync_whatsapp_reconnect(
    *,
    req: SyncConnectionsRequest,
    prepared_connection: tuple,
    secret: str,
    user: User,
    db: AsyncSession,
    credential_service,
) -> SyncConnectionsResponse:
    from apps.api.routers.integrations import (
        WhatsAppPhoneAlreadyConnected,
        _apply_whatsapp_reconnect_result,
        _classify_whatsapp_provisioning_result,
        _clear_whatsapp_retirement_state,
        _stage_whatsapp_reconnect,
    )

    (
        raw_connection,
        provider_config_key,
        connection_id,
        _provider_key,
        _profile,
        phone_number_id,
        waba_id,
        display_name,
    ) = prepared_connection
    integration_id = str(req.replace_integration_id or "")
    try:
        await _stage_whatsapp_reconnect(
            db,
            entity_id=user.entity_id,
            owner_user_id=user.id,
            integration_id=integration_id,
            provider_config_key=provider_config_key,
            connection_id=connection_id,
            waba_id=waba_id or "",
            phone_number_id=phone_number_id or "",
            display_name=display_name,
            synced_at=(
                raw_connection.get("created_at") or raw_connection.get("updated_at")
            ),
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc

    try:
        deployment = load_whatsapp_business_config()
        result = await provision_whatsapp_business_number(
            nango_secret=secret,
            provider_config_key=provider_config_key,
            connection_id=connection_id,
            phone_number_id=phone_number_id or "",
            waba_id=waba_id or "",
            expected_app_id=deployment.app_id,
        )
    except (httpx.HTTPError, RuntimeError):
        provisioning_status = "failed"
        readiness_code = "provider_unavailable"
        detail = "WhatsApp Business provider setup is temporarily unavailable."
        retryable = True
    else:
        (
            provisioning_status,
            readiness_code,
            detail,
            retryable,
        ) = _classify_whatsapp_provisioning_result(result)

    try:
        activated, retirement = await _apply_whatsapp_reconnect_result(
            db,
            entity_id=user.entity_id,
            owner_user_id=user.id,
            integration_id=integration_id,
            connection_id=connection_id,
            provisioning_status=provisioning_status,
            readiness_code=readiness_code,
            detail=detail,
            retryable=retryable,
            credential_service=credential_service,
        )
        await db.commit()
    except WhatsAppPhoneAlreadyConnected as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc

    if retirement is not None:
        old_provider_config_key, old_connection_id = retirement
        try:
            await delete_nango_connection(
                nango_secret=secret,
                provider_config_key=old_provider_config_key,
                connection_id=old_connection_id,
            )
        except Exception:
            try:
                from packages.core.tasks.channel_tasks import (
                    retire_nango_connection_task,
                )

                retire_nango_connection_task.delay(
                    entity_id=user.entity_id,
                    integration_id=integration_id,
                )
            except Exception:
                logger.warning(
                    "Could not enqueue Nango retirement for integration=%s",
                    integration_id,
                )
        else:
            await _clear_whatsapp_retirement_state(
                db,
                entity_id=user.entity_id,
                integration_id=integration_id,
                connection_id=old_connection_id,
            )
            await db.commit()

    if retryable and not activated:
        try:
            from packages.core.tasks.channel_tasks import (
                register_integration_webhooks_task,
            )

            register_integration_webhooks_task.delay(
                entity_id=user.entity_id,
                integration_id=integration_id,
            )
        except Exception:
            logger.warning(
                "Could not enqueue WhatsApp reconnect provisioning retry for integration=%s",
                integration_id,
            )

    return SyncConnectionsResponse(
        upserted=1 if activated else 0,
        providers=["whatsapp"],
        integration_id=integration_id,
        readiness_code=readiness_code,
        provisioning_pending=not activated,
        provisioning_detail=detail if not activated else None,
    )


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


def _connect_hmac_key() -> str:
    key = os.environ.get("NANGO_CONNECT_HMAC_KEY", "").strip()
    if len(key) != 64 or any(char not in "0123456789abcdefABCDEF" for char in key):
        raise HTTPException(
            500,
            "NANGO_CONNECT_HMAC_KEY must be a 64-character hexadecimal value.",
        )
    return key


def _nango_items(payload: Any) -> list[dict[str, Any]]:
    """Normalize a Graph collection response without retaining credentials."""
    raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


async def _nango_proxy_json(
    client: httpx.AsyncClient,
    *,
    secret: str,
    provider_config_key: str,
    connection_id: str,
    path: str,
    params: dict[str, str],
) -> dict[str, Any]:
    """Read one provider resource through Nango's authenticated proxy."""
    response = await client.get(
        f"{_NANGO_BASE}/proxy/{path.lstrip('/')}",
        params=params,
        headers={
            "Authorization": f"Bearer {secret}",
            "Provider-Config-Key": provider_config_key,
            "Connection-Id": connection_id,
            "Nango-Proxy-Accept": "application/json",
            # Nango streams provider responses by default. Meta Graph returns
            # Brotli, which the API image does not decode. Ask the provider
            # for an identity response and ask Nango to decompress as a
            # fallback before forwarding it.
            "Decompress": "true",
            "Nango-Proxy-Accept-Encoding": "identity",
        },
    )
    if response.status_code in (400, 404):
        return {}
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


async def _discover_whatsapp_phones(
    client: httpx.AsyncClient,
    *,
    secret: str,
    provider_config_key: str,
    connection_id: str,
    waba_id: str,
) -> list[dict[str, Any]]:
    payload = await _nango_proxy_json(
        client,
        secret=secret,
        provider_config_key=provider_config_key,
        connection_id=connection_id,
        path=f"{META_GRAPH.value}/{quote(waba_id, safe='')}/phone_numbers",
        params={"fields": "id,display_phone_number,verified_name"},
    )
    return _nango_items(payload)


async def _verify_whatsapp_resource_via_nango(
    *,
    secret: str,
    provider_config_key: str,
    connection_id: str,
    waba_id: str,
    phone_number_id: str,
) -> dict[str, str | None] | None:
    """Verify that Embedded Signup's phone belongs to its reported WABA."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        phones = await _discover_whatsapp_phones(
            client,
            secret=secret,
            provider_config_key=provider_config_key,
            connection_id=connection_id,
            waba_id=waba_id,
        )
    phone = next(
        (
            item
            for item in phones
            if str(item.get("id") or item.get("phone_number_id") or "").strip()
            == phone_number_id
        ),
        None,
    )
    if phone is None:
        return None
    return {
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
        "display_name": str(
            phone.get("verified_name")
            or phone.get("display_phone_number")
            or "WhatsApp account"
        ),
    }


async def _subscribe_whatsapp_waba_via_nango(
    *,
    secret: str,
    provider_config_key: str,
    connection_id: str,
    waba_id: str,
) -> None:
    """Subscribe Manor's Meta App without copying the connection token."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            (
                f"{_NANGO_BASE}/proxy/{META_GRAPH.value}/"
                f"{quote(waba_id, safe='')}/subscribed_apps"
            ),
            headers={
                "Authorization": f"Bearer {secret}",
                "Provider-Config-Key": provider_config_key,
                "Connection-Id": connection_id,
                "Nango-Proxy-Accept": "application/json",
            },
            json={},
        )
        response.raise_for_status()

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
    if req.replace_integration_id:
        await check_effective_user_permission(
            db,
            user,
            Permission.INTEGRATIONS_MANAGE,
        )

    hmac_key = _connect_hmac_key()

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

    if req.replace_integration_id:
        replacement = (await db.execute(
            select(Integration).where(
                Integration.id == req.replace_integration_id,
                Integration.entity_id == user.entity_id,
                Integration.owner_user_id == user.id,
                Integration.status == "active",
            )
        )).scalar_one_or_none()
        if replacement is None:
            raise HTTPException(404, "Integration selected for reconnect was not found")
        nango_config = (replacement.config or {}).get("nango")
        if not isinstance(nango_config, dict) or not nango_config.get("connection_id"):
            raise HTTPException(400, "Only an active Nango-backed Integration can be reconnected")
        requested_provider = canonical_provider_key(provider_config_key)
        existing_provider = canonical_provider_key(replacement.provider)
        existing_nango_provider = canonical_provider_key(
            nango_config.get("provider_config_key") or replacement.provider
        )
        if requested_provider not in {existing_provider, existing_nango_provider}:
            raise HTTPException(400, "Reconnect provider does not match the selected Integration")

    # New Nango connects create distinct accounts so providers such as
    # LinkedIn/Gmail support multi-account cards. The user id is embedded for
    # auditability; agents resolve through the owner/share access boundary.
    connection_id = (
        f"{user.entity_id}--{user.id}--{provider_config_key}--{generate_ulid()}"
    )

    query_params = {
        "public_key": public_key,
        "connection_id": connection_id,
        "hmac": hmac.new(
            hmac_key.encode("ascii"),
            f"{provider_config_key}:{connection_id}".encode("ascii"),
            hashlib.sha256,
        ).hexdigest(),
    }
    if canonical_provider_key(provider_config_key) == "whatsapp":
        try:
            config_id = load_whatsapp_business_config().embedded_signup_config_id
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc
        query_params.update({
            "authorization_params[config_id]": config_id,
            "authorization_params[response_type]": "code",
            "authorization_params[override_default_response_type]": "true",
            "authorization_params[extras]": json.dumps(
                {
                    "setup": {},
                    "featureType": "",
                    "sessionInfoVersion": "3",
                },
                separators=(",", ":"),
            ),
        })
    query = urlencode(query_params)
    nango_url = f"{_nango_public_base()}/oauth/connect/{provider_config_key}?{query}"
    return StartConnectResponse(
        session_token="",  # legacy field, unused in 0.36 flow
        nango_connect_url=nango_url,
        connection_id=connection_id,
        provider_config_key=provider_config_key,
    )


@router.post("/connections/sync", response_model=SyncConnectionsResponse)
async def sync_connections(
    req: SyncConnectionsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pull the current user's Connections and mirror them
    into our ``integrations`` table so the rest of the platform (agent
    pickers, MCP runtime, billing scopes) treats them like any other
    user-owned integration."""
    if req.replace_integration_id:
        await check_effective_user_permission(
            db,
            user,
            Permission.INTEGRATIONS_MANAGE,
        )

    if req.expected_connection_id and not req.expected_provider_config_key:
        raise HTTPException(
            400,
            "expected_connection_id requires expected_provider_config_key",
        )
    if req.expected_provider_config_key and not req.expected_connection_id:
        raise HTTPException(
            400,
            "expected_provider_config_key requires expected_connection_id",
        )
    if req.replace_integration_id and not req.expected_connection_id:
        raise HTTPException(
            400,
            "replace_integration_id requires expected_connection_id",
        )

    embedded_waba_id = str(req.whatsapp_waba_id or "").strip()
    embedded_phone_number_id = str(req.whatsapp_phone_number_id or "").strip()
    if bool(embedded_waba_id) != bool(embedded_phone_number_id):
        raise HTTPException(
            422,
            "WhatsApp Embedded Signup requires both waba_id and phone_number_id.",
        )
    if embedded_waba_id and (
        not req.expected_connection_id
        or canonical_provider_key(req.expected_provider_config_key or "") != "whatsapp"
    ):
        raise HTTPException(
            400,
            "WhatsApp Embedded Signup ids require an exact WhatsApp connection.",
        )

    exact_sync = bool(req.expected_connection_id)
    if not exact_sync:
        await check_effective_user_permission(
            db,
            user,
            Permission.INTEGRATIONS_MANAGE,
        )

    secret = await _load_nango_secret(db, user.entity_id)

    try:
        async with httpx.AsyncClient(timeout=30.0) as cx:
            if exact_sync:
                r = await cx.get(
                    f"{_NANGO_BASE}/connection/{quote(req.expected_connection_id or '', safe='')}",
                    params={"provider_config_key": req.expected_provider_config_key},
                    headers={"Authorization": f"Bearer {secret}"},
                )
                if r.status_code == 404:
                    raw_conns: list[dict] = []
                else:
                    r.raise_for_status()
                    connection = r.json()
                    if not isinstance(connection, dict):
                        raise HTTPException(502, "Nango connection response was not an object")
                    returned_connection_id = connection.get("connection_id")
                    returned_provider_key = (
                        connection.get("provider_config_key") or connection.get("provider")
                    )
                    if (
                        returned_connection_id != req.expected_connection_id
                        or returned_provider_key != req.expected_provider_config_key
                    ):
                        raise HTTPException(
                            502,
                            "Nango connection response did not match the requested connection",
                        )
                    raw_conns = [connection]
            else:
                r = await cx.get(
                    f"{_NANGO_BASE}/connection",
                    headers={"Authorization": f"Bearer {secret}"},
                )
                r.raise_for_status()
                body = r.json()
                raw_conns = body.get("connections") or (
                    body if isinstance(body, list) else []
                )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            502,
            f"Nango connections request failed: {exc.response.status_code} {exc.response.text[:200]}",
        )

    reconnect_mode = bool(exact_sync and req.replace_integration_id)
    if not raw_conns:
        return SyncConnectionsResponse(upserted=0, providers=[])

    replacement: Integration | None = None
    replaced_connection_id: str | None = None

    superseded_nango_connection_ids: set[str] = set()
    if not reconnect_mode:
        existing_nango_rows = (await db.execute(
            select(Integration).where(
                Integration.entity_id == user.entity_id,
                Integration.owner_user_id == user.id,
                Integration.status == "active",
            )
        )).scalars().all()
        for row in existing_nango_rows:
            nango_config = (row.config or {}).get("nango")
            if not isinstance(nango_config, dict):
                continue
            superseded_ids = nango_config.get("superseded_connection_ids")
            if isinstance(superseded_ids, list):
                superseded_nango_connection_ids.update(
                    item for item in superseded_ids if isinstance(item, str)
                )

    prepared_connections = []
    for c in raw_conns:
        provider_config_key = c.get("provider_config_key") or c.get("provider")
        connection_id = c.get("connection_id")
        if not provider_config_key or not connection_id:
            continue
        provider_config_key = str(provider_config_key)
        connection_id = str(connection_id)
        provider_key = canonical_provider_key(provider_config_key)
        if not connection_id.startswith(f"{user.entity_id}--{user.id}--"):
            logger.debug(
                "Skipping Nango connection not owned by current user",
                extra={
                    "entity_id": user.entity_id,
                    "provider": provider_config_key,
                    "connection_id": connection_id,
                },
            )
            continue
        if connection_id in superseded_nango_connection_ids:
            continue

        profile = None
        whatsapp_phone_number_id = None
        whatsapp_waba_id = None
        whatsapp_display_name = None
        if provider_key == "whatsapp":
            # Nango connection metadata is trusted for this non-secret
            # routing key. Do not copy access tokens or webhook secrets into
            # Manor; those remain runtime-resolved from Nango.
            for source in (
                c,
                c.get("metadata") if isinstance(c, dict) else None,
                c.get("profile") if isinstance(c, dict) else None,
                c.get("credentials") if isinstance(c, dict) else None,
            ):
                if isinstance(source, dict):
                    candidate = str(
                        source.get("phone_number_id") or source.get("phone_id") or ""
                    ).strip()
                    if candidate and not whatsapp_phone_number_id:
                        whatsapp_phone_number_id = candidate
                    waba_candidate = str(
                        source.get("waba_id")
                        or source.get("whatsapp_business_account_id")
                        or ""
                    ).strip()
                    if waba_candidate and not whatsapp_waba_id:
                        whatsapp_waba_id = waba_candidate
                    display_candidate = str(
                        source.get("display_name")
                        or source.get("verified_name")
                        or source.get("display_phone_number")
                        or ""
                    ).strip()
                    if display_candidate and not whatsapp_display_name:
                        whatsapp_display_name = display_candidate
            if embedded_waba_id and embedded_phone_number_id:
                whatsapp_waba_id = embedded_waba_id
                whatsapp_phone_number_id = embedded_phone_number_id
            if not whatsapp_waba_id or not whatsapp_phone_number_id:
                if not exact_sync:
                    continue
                raise HTTPException(
                    422,
                    "WhatsApp Embedded Signup did not return a WABA and phone number.",
                )
            try:
                verified_resource = await _verify_whatsapp_resource_via_nango(
                    secret=secret,
                    provider_config_key=provider_config_key,
                    connection_id=connection_id,
                    waba_id=whatsapp_waba_id,
                    phone_number_id=whatsapp_phone_number_id,
                )
            except (httpx.HTTPError, ValueError) as exc:
                raise HTTPException(
                    502,
                    "Could not verify the WhatsApp Business phone number through Nango.",
                ) from exc
            if verified_resource is None:
                raise HTTPException(
                    422,
                    "The selected WhatsApp phone number does not belong to the authorized WABA.",
                )
            whatsapp_phone_number_id = verified_resource["phone_number_id"]
            whatsapp_waba_id = verified_resource["waba_id"]
            whatsapp_display_name = (
                verified_resource.get("display_name") or whatsapp_display_name
            )
        if provider_key == "linkedin":
            profile = await _fetch_linkedin_profile_via_nango(
                secret=secret,
                provider_config_key=provider_key,
                connection_id=connection_id,
            )
        prepared_connections.append(
            (
                c,
                provider_config_key,
                connection_id,
                provider_key,
                profile,
                whatsapp_phone_number_id,
                whatsapp_waba_id,
                whatsapp_display_name,
            )
        )
    prepared_connections.sort(key=lambda prepared: (prepared[3], prepared[2]))

    cs = get_credential_service()
    if (
        reconnect_mode
        and len(prepared_connections) == 1
        and prepared_connections[0][3] == "whatsapp"
    ):
        return await _sync_whatsapp_reconnect(
            req=req,
            prepared_connection=prepared_connections[0],
            secret=secret,
            user=user,
            db=db,
            credential_service=cs,
        )

    upserted = 0
    providers: list[str] = []
    teams_integration_ids: set[str] = set()
    outlook_integration_ids: set[str] = set()
    whatsapp_provisioning_targets: list[tuple[str, str, str, str, str]] = []
    synced_integration_ids: list[str] = []

    for (
        c,
        provider_config_key,
        connection_id,
        provider_key,
        profile,
        whatsapp_phone_number_id,
        whatsapp_waba_id,
        whatsapp_display_name,
    ) in prepared_connections:
        await lock_runtime_integration_account_scope(
            db,
            kind=IntegrationAccountKind.INTEGRATION,
            user_id=user.id,
            entity_id=user.entity_id,
            provider=provider_key,
        )
        if reconnect_mode:
            replacement = (await db.execute(
                select(Integration).where(
                    Integration.id == req.replace_integration_id,
                    Integration.entity_id == user.entity_id,
                    Integration.owner_user_id == user.id,
                    Integration.status == "active",
                ).with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if replacement is None:
                raise HTTPException(404, "Integration selected for reconnect was not found")
            replacement_nango = (replacement.config or {}).get("nango") or {}
            if not isinstance(replacement_nango, dict) or not replacement_nango.get(
                "connection_id"
            ):
                raise HTTPException(
                    400,
                    "Only an active Nango-backed Integration can be reconnected",
                )
            replaced_connection_id = str(replacement_nango["connection_id"])
            replacement_provider = canonical_provider_key(replacement.provider)
            replacement_nango_provider = canonical_provider_key(
                replacement_nango.get("provider_config_key") or replacement.provider
            )
            if provider_key not in {replacement_provider, replacement_nango_provider}:
                raise HTTPException(400, "Reconnect provider does not match the selected Integration")
            existing = replacement
        else:
            candidates = (await db.execute(
                select(Integration).where(
                    Integration.entity_id == user.entity_id,
                    Integration.owner_user_id == user.id,
                    Integration.provider.in_(provider_key_aliases(provider_key)),
                    Integration.status == "active",
                ).order_by(Integration.id).with_for_update().execution_options(
                    populate_existing=True
                )
            )).scalars().all()
            all_superseded_ids: set[str] = set()
            for row in candidates:
                candidate_nango = (row.config or {}).get("nango")
                if not isinstance(candidate_nango, dict):
                    continue
                superseded_ids = candidate_nango.get("superseded_connection_ids")
                if isinstance(superseded_ids, list):
                    all_superseded_ids.update(
                        value for value in superseded_ids if isinstance(value, str)
                    )
            if connection_id in all_superseded_ids:
                continue
            existing = next(
                (
                    row
                    for row in candidates
                    if isinstance((row.config or {}).get("nango"), dict)
                    and (row.config or {})["nango"].get("connection_id")
                    == connection_id
                ),
                None,
            )
        if existing is None:
            existing = Integration(
                id=generate_ulid(),
                entity_id=user.entity_id,
                owner_user_id=user.id,
                created_by_user_id=user.id,
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
        previous_nango = existing_config.get("nango")
        previous_superseded_ids = (
            previous_nango.get("superseded_connection_ids", [])
            if isinstance(previous_nango, dict)
            else []
        )
        superseded_connection_ids: list[str] = []
        for superseded_id in previous_superseded_ids:
            if (
                isinstance(superseded_id, str)
                and superseded_id != connection_id
                and superseded_id not in superseded_connection_ids
            ):
                superseded_connection_ids.append(superseded_id)
        if (
            reconnect_mode
            and replaced_connection_id
            and replaced_connection_id != connection_id
            and replaced_connection_id not in superseded_connection_ids
        ):
            superseded_connection_ids.append(replaced_connection_id)

        nango_config = {
            "connection_id": connection_id,
            "provider_config_key": provider_config_key,
            "synced_at": c.get("created_at") or c.get("updated_at"),
            "connected_by_user_id": user.id,
        }
        if superseded_connection_ids:
            nango_config["superseded_connection_ids"] = superseded_connection_ids
        existing_config["nango"] = nango_config
        if provider_key == "whatsapp":
            existing_config["whatsapp"] = {
                "waba_id": whatsapp_waba_id,
                "phone_number_id": whatsapp_phone_number_id,
                "display_name": whatsapp_display_name,
                "provisioning_status": "pending",
                "readiness_code": "provider_unavailable",
                "subscription_status": "pending",
            }
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

        # Nango owns the OAuth connection, while provider webhooks still
        # enter Manor directly. Materialize the same user-owned ChannelConfig
        # used by Slack/Discord so inbound and outbound share one contract.
        if provider_key in {"whatsapp", "ms_teams", "outlook"}:
            from apps.api.routers.integrations import (
                WhatsAppPhoneAlreadyConnected,
                _sync_channel_config_if_needed,
            )

            try:
                await _sync_channel_config_if_needed(
                    db,
                    entity_id=user.entity_id,
                    owner_user_id=user.id,
                    provider=provider_key,
                    integration_id=existing.id,
                    whatsapp_phone_number_id=whatsapp_phone_number_id,
                    whatsapp_provisioning_pending=provider_key == "whatsapp",
                )
            except WhatsAppPhoneAlreadyConnected as exc:
                await db.rollback()
                raise HTTPException(409, str(exc)) from exc
            except Exception:
                await db.rollback()
                raise
        if provider_key == "ms_teams":
            teams_integration_ids.add(existing.id)
        if provider_key == "outlook":
            outlook_integration_ids.add(existing.id)
        if provider_key == "whatsapp" and whatsapp_waba_id:
            whatsapp_provisioning_targets.append((
                existing.id,
                provider_config_key,
                connection_id,
                whatsapp_waba_id,
                whatsapp_phone_number_id or "",
            ))

        synced_integration_ids.append(existing.id)
        providers.append(provider_key)
        upserted += 1

    await db.flush()
    for provider_key in sorted(set(providers)):
        await normalize_runtime_integration_account_defaults(
            db,
            kind=IntegrationAccountKind.INTEGRATION,
            user_id=user.id,
            entity_id=user.entity_id,
            provider=provider_key,
        )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    whatsapp_provisioning_failures: list[str] = []
    whatsapp_pending_ids: list[str] = []
    whatsapp_readiness_codes: dict[str, str] = {}
    whatsapp_details: dict[str, str] = {}
    if whatsapp_provisioning_targets:
        from apps.api.routers.integrations import (
            _classify_whatsapp_provisioning_result,
            _set_whatsapp_provisioning_state,
        )

        for (
            integration_id,
            provider_config_key,
            connection_id,
            waba_id,
            phone_number_id,
        ) in whatsapp_provisioning_targets:
            try:
                deployment = load_whatsapp_business_config()
                result = await provision_whatsapp_business_number(
                    nango_secret=secret,
                    provider_config_key=provider_config_key,
                    connection_id=connection_id,
                    phone_number_id=phone_number_id,
                    waba_id=waba_id,
                    expected_app_id=deployment.app_id,
                )
            except (httpx.HTTPError, RuntimeError):
                provisioning_status = "failed"
                readiness_code = "provider_unavailable"
                detail = "WhatsApp Business provider setup is temporarily unavailable."
                retryable = True
            else:
                (
                    provisioning_status,
                    readiness_code,
                    detail,
                    retryable,
                ) = _classify_whatsapp_provisioning_result(result)

            await _set_whatsapp_provisioning_state(
                db,
                integration_id=integration_id,
                provisioning_status=provisioning_status,
                readiness_code=readiness_code,
                detail=detail,
            )
            whatsapp_readiness_codes[integration_id] = readiness_code
            whatsapp_details[integration_id] = detail
            if readiness_code != "ready":
                whatsapp_pending_ids.append(integration_id)
            if retryable:
                whatsapp_provisioning_failures.append(integration_id)
        await db.commit()

    for integration_id in whatsapp_provisioning_failures:
        try:
            from packages.core.tasks.channel_tasks import register_integration_webhooks_task

            register_integration_webhooks_task.delay(
                entity_id=user.entity_id,
                integration_id=integration_id,
            )
        except Exception:
            logger.warning(
                "Could not enqueue WhatsApp webhook registration retry for integration=%s",
                integration_id,
                exc_info=True,
            )
    # Teams and Outlook require a Graph change subscription before they can receive a
    # message. Queue registration after the Nango pointer and ChannelConfig
    # are durable; Graph/Nango availability must not block this sync response.
    for integration_id in sorted(teams_integration_ids | outlook_integration_ids):
        try:
            from packages.core.tasks.channel_tasks import register_integration_webhooks_task

            register_integration_webhooks_task.delay(
                entity_id=user.entity_id,
                integration_id=integration_id,
            )
        except Exception:
            logger.warning(
                "Could not enqueue Teams webhook registration for integration=%s",
                integration_id,
                exc_info=True,
            )
    synced_integration_id = (
        synced_integration_ids[0] if len(synced_integration_ids) == 1 else None
    )
    return SyncConnectionsResponse(
        upserted=upserted,
        providers=sorted(set(providers)),
        integration_id=synced_integration_id,
        readiness_code=(
            whatsapp_readiness_codes.get(synced_integration_id or "")
        ),
        provisioning_pending=bool(whatsapp_pending_ids),
        provisioning_detail=(
            whatsapp_details.get(synced_integration_id or "")
            if whatsapp_pending_ids else None
        ),
    )


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
