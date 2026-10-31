"""OAuth 2.0 provider endpoints — Manor acts as IdP for downstream apps (PMS, etc.).

Endpoints
---------
GET  /api/v1/oauth/clients/{client_id}
    Public — returns minimal info (name, description) about a registered client app so
    the consent screen can render. Does not leak client_secret_hash or full URI list.

POST /api/v1/oauth/authorize
    Authenticated (user JWT in Authorization header).
    Body: { client_id, redirect_uri, scope, state, code_challenge?, code_challenge_method? }
    Returns: { redirect_to: "<redirect_uri>?code=<code>&state=<state>" }
    Frontend then sets `window.location = redirect_to`.

POST /api/v1/oauth/token
    No auth header. Standard OAuth 2.0 form-encoded or JSON body.
    Body: { grant_type=authorization_code, code, client_id, client_secret?, redirect_uri, code_verifier? }
       or { grant_type=refresh_token, refresh_token, client_id }   (public clients)
    Returns: { access_token, token_type, expires_in, scope, user, refresh_token? }

POST /api/v1/oauth/register
    RFC 7591 dynamic registration of a public PKCE client (native MCP clients).
    Unauthenticated and always rate-limited per client IP.

GET    /api/v1/oauth/connections
DELETE /api/v1/oauth/connections/{id}
    Authenticated. The current user's live sign-ins from public clients (one per
    refresh-token family) and revocation of one of them.

GET /.well-known/oauth-authorization-server   (RFC 8414, see oauth_metadata.py)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services import oauth_provider_service as svc
from apps.api.deps import get_current_user
from apps.api.middleware.rate_limit import RateLimiter, client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/oauth", tags=["oauth-provider"])


# ── Schemas ──

class ClientInfo(BaseModel):
    client_id: str
    name: str
    description: Optional[str] = None
    redirect_uris: List[str]
    allowed_scopes: List[str]
    require_pkce: bool = False
    require_verified_email: bool = False
    # False for self-registered clients: their name is unverified.
    verified: bool = True
    # Answer for the ``redirect_uri`` query parameter, when one was sent.
    redirect_uri_allowed: Optional[bool] = None


class AuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str
    redirect_uri: str
    scope: Optional[str] = ""  # space-separated OAuth scopes
    state: Optional[str] = None
    code_challenge: Optional[StrictStr] = None
    code_challenge_method: Optional[StrictStr] = None


class AuthorizeResponse(BaseModel):
    redirect_to: str
    code: str
    state: Optional[str] = None


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant_type: str = Field(..., description="'authorization_code' or 'refresh_token'")
    client_id: str
    client_secret: Optional[str] = None
    code: Optional[str] = None
    redirect_uri: Optional[str] = None
    code_verifier: Optional[StrictStr] = None
    refresh_token: Optional[str] = None
    # RFC 8707 resource indicator. The audience is fixed per client, so this is
    # accepted for compatibility and not used to widen it.
    resource: Optional[str] = None
    scope: Optional[str] = None


class TokenUserInfo(BaseModel):
    id: str
    email: str
    email_verified: bool = False
    name: Optional[str] = None
    entity_id: str
    role: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str = ""
    user: TokenUserInfo
    refresh_token: Optional[str] = None


def _unique_parameters(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_parameter")
        result[key] = value
    return result


async def _protocol_body(request: Request, model):
    """Never silently choose the first/last duplicate OAuth parameter."""
    try:
        _unique_parameters(request.query_params.multi_items())
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/x-www-form-urlencoded" in content_type:
            data = _unique_parameters((await request.form()).multi_items())
        else:
            data = json.loads(await request.body(), object_pairs_hook=_unique_parameters)
        if not isinstance(data, dict):
            raise ValueError("invalid_body")
        return model.model_validate(data)
    except (ValueError, TypeError):
        # ValidationError includes input values, which can contain client secrets
        # and verifiers. Return only the stable OAuth error, never that detail.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_request",
                            headers={"Cache-Control": "no-store"}) from None


async def _authorize_body(request: Request) -> AuthorizeRequest:
    return await _protocol_body(request, AuthorizeRequest)


# ── Public client info ──

@router.get("/clients/{client_id}", response_model=ClientInfo)
async def get_client_info(
    client_id: str,
    redirect_uri: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    client = await svc.get_client_by_id(db, client_id)
    if not client or not client.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown client")
    return ClientInfo(
        client_id=client.client_id,
        name=client.name,
        description=client.description,
        redirect_uris=client.redirect_uris or [],
        allowed_scopes=client.allowed_scopes or [],
        require_pkce=bool(client.require_pkce),
        require_verified_email=bool(client.require_verified_email),
        verified=client.registration_source != svc.REGISTRATION_SOURCE_DYNAMIC,
        redirect_uri_allowed=(
            None if redirect_uri is None else svc.validate_redirect_uri(client, redirect_uri)
        ),
    )


# ── /register (RFC 7591) ──

# Registration is unauthenticated, so it is always limited per IP — independent
# of the global RATE_LIMIT_ENABLED switch. A coding agent registers once per
# install; retries and a few machines behind one NAT stay well under this.
REGISTER_RATE_LIMIT = int(os.getenv("MANOR_OAUTH_REGISTER_RATE_LIMIT", "20"))
REGISTER_RATE_WINDOW_SECONDS = int(os.getenv("MANOR_OAUTH_REGISTER_RATE_WINDOW_SECONDS", "3600"))
_register_limiter = RateLimiter(redis_enabled=True)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_client(request: Request, db: AsyncSession = Depends(get_db)):
    """Self-register a public PKCE client. Its tokens only reach the MCP endpoint."""
    decision = await _register_limiter.check(
        f"oauth-register:{client_ip(request)}", REGISTER_RATE_LIMIT, REGISTER_RATE_WINDOW_SECONDS,
    )
    if not decision.allowed:
        retry_after = str(decision.retry_after or REGISTER_RATE_WINDOW_SECONDS)
        return _oauth_error("too_many_requests", status.HTTP_429_TOO_MANY_REQUESTS,
                            "too many client registrations from this address",
                            headers={"Retry-After": retry_after})
    try:
        metadata = json.loads(await request.body())
    except ValueError:
        metadata = None
    if not isinstance(metadata, dict):
        return _oauth_error("invalid_client_metadata", status.HTTP_400_BAD_REQUEST, "body must be a JSON object")
    try:
        client = await svc.register_dynamic_client(db, metadata)
    except svc.ClientRegistrationError as e:
        return _oauth_error(e.code, status.HTTP_400_BAD_REQUEST, e.description)
    await db.commit()
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        headers={"Cache-Control": "no-store"},
        content={
            "client_id": client.client_id,
            "client_id_issued_at": int(client.created_at.timestamp()) if client.created_at else None,
            "client_name": client.name,
            "redirect_uris": client.redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )


def _oauth_error(
    code: str, status_code: int, description: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    # ``detail`` keeps the historical FastAPI error shape for existing clients.
    body = {"error": code, "detail": code}
    if description:
        body["error_description"] = description
    return JSONResponse(status_code=status_code, content=body,
                        headers={"Cache-Control": "no-store", **(headers or {})})


# ── Connected clients (the current user's sign-ins from public clients) ──

class ConnectedClient(BaseModel):
    id: str  # refresh-token family
    client_id: str
    name: str
    verified: bool
    entity_id: str
    connected_at: datetime
    last_used_at: datetime
    expires_at: datetime


@router.get("/connections", response_model=List[ConnectedClient])
async def list_connections(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Coding agents (and other public clients) that can still refresh as this user."""
    return [ConnectedClient(**row) for row in await svc.list_connected_clients(db, user)]


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_connection(
    connection_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke one connection. Other connections and the browser session are untouched."""
    if not await svc.revoke_connected_client(db, user, connection_id):
        await db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown connection")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── /authorize ──

@router.post("/authorize", response_model=AuthorizeResponse)
async def authorize(
    body: AuthorizeRequest = Depends(_authorize_body),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mint an authorization code for the authenticated user, bound to the requesting client."""
    client = await svc.get_client_by_id(db, body.client_id)
    if not client or not client.active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown_or_inactive_client")

    if not svc.validate_redirect_uri(client, body.redirect_uri):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "redirect_uri_not_whitelisted")

    # Validate requested scopes against the client's allowed scopes (if requested at all)
    requested_scopes = [s for s in (body.scope or "").split() if s]
    allowed = set(client.allowed_scopes or [])
    if allowed:
        for s in requested_scopes:
            if s not in allowed:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"scope_not_allowed:{s}")
    if not requested_scopes:
        requested_scopes = list(client.allowed_scopes or [])

    try:
        code = await svc.create_authorization_code(
            db,
            client=client,
            user=user,
            redirect_uri=body.redirect_uri,
            scope=requested_scopes,
            state=body.state,
            code_challenge=body.code_challenge,
            code_challenge_method=body.code_challenge_method,
        )
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    await db.commit()

    qs = {"code": code.code}
    if body.state:
        qs["state"] = body.state
    sep = "&" if "?" in body.redirect_uri else "?"
    redirect_to = f"{body.redirect_uri}{sep}{urlencode(qs)}"

    return AuthorizeResponse(redirect_to=redirect_to, code=code.code, state=body.state)


# ── /token ──

@router.post("/token", response_model=TokenResponse)
async def token(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OAuth 2.0 token endpoint. Accepts JSON body OR application/x-www-form-urlencoded."""
    body = await _protocol_body(request, TokenRequest)

    try:
        if body.grant_type == "authorization_code":
            if not body.code or not body.redirect_uri:
                return _oauth_error("invalid_request", status.HTTP_400_BAD_REQUEST)
            result = await svc.exchange_code_for_token(
                db,
                code=body.code,
                client_id=body.client_id,
                client_secret=body.client_secret,
                redirect_uri=body.redirect_uri,
                code_verifier=body.code_verifier,
            )
        elif body.grant_type == "refresh_token":
            if not body.refresh_token:
                return _oauth_error("invalid_request", status.HTTP_400_BAD_REQUEST)
            result = await svc.refresh_access_token(
                db,
                refresh_token=body.refresh_token,
                client_id=body.client_id,
                client_secret=body.client_secret,
            )
        else:
            return _oauth_error("unsupported_grant_type", status.HTTP_400_BAD_REQUEST)
        await db.commit()
    except svc.RefreshTokenReuse as e:
        await db.commit()  # persist the family revocation
        return _oauth_error(str(e), status.HTTP_400_BAD_REQUEST)
    except ValueError as e:
        await db.rollback()
        return _oauth_error(str(e), status.HTTP_400_BAD_REQUEST)

    response = TokenResponse(
        access_token=result["access_token"],
        token_type=result["token_type"],
        expires_in=result["expires_in"],
        scope=result["scope"],
        user=TokenUserInfo(**result["user"]),
        refresh_token=result.get("refresh_token"),
    )
    content = response.model_dump()
    if content["refresh_token"] is None:
        del content["refresh_token"]
    return JSONResponse(
        content=content,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
