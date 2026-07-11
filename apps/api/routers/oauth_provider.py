"""OAuth 2.0 provider endpoints — Manor acts as IdP for downstream apps (PMS, etc.).

Endpoints
---------
GET  /api/v1/oauth/clients/{client_id}
    Public — returns minimal info (name, description) about a registered client app so
    the consent screen can render. Does not leak client_secret_hash or full URI list.

POST /api/v1/oauth/authorize
    Authenticated (user JWT in Authorization header).
    Body: { client_id, redirect_uri, scope, state }
    Returns: { redirect_to: "<redirect_uri>?code=<code>&state=<state>" }
    Frontend then sets `window.location = redirect_to`.

POST /api/v1/oauth/token
    No auth header. Standard OAuth 2.0 form-encoded or JSON body.
    Body: { grant_type=authorization_code, code, client_id, client_secret, redirect_uri }
    Returns: { access_token, token_type, expires_in, scope, user }
"""
from __future__ import annotations

import logging
from typing import List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services import oauth_provider_service as svc
from apps.api.deps import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/oauth", tags=["oauth-provider"])


# ── Schemas ──

class ClientInfo(BaseModel):
    client_id: str
    name: str
    description: Optional[str] = None
    redirect_uris: List[str]
    allowed_scopes: List[str]


class AuthorizeRequest(BaseModel):
    client_id: str
    redirect_uri: str
    scope: Optional[str] = ""  # space-separated OAuth scopes
    state: Optional[str] = None


class AuthorizeResponse(BaseModel):
    redirect_to: str
    code: str
    state: Optional[str] = None


class TokenRequest(BaseModel):
    grant_type: str = Field(..., description="must be 'authorization_code'")
    code: str
    client_id: str
    client_secret: str
    redirect_uri: str


class TokenUserInfo(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    entity_id: str
    role: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str = ""
    user: TokenUserInfo


# ── Public client info ──

@router.get("/clients/{client_id}", response_model=ClientInfo)
async def get_client_info(
    client_id: str,
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
    )


# ── /authorize ──

@router.post("/authorize", response_model=AuthorizeResponse)
async def authorize(
    body: AuthorizeRequest,
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
        )
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))

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
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        data = dict(form)
    elif "application/json" in content_type:
        data = await request.json()
    else:
        # Try both
        try:
            data = await request.json()
        except Exception:
            form = await request.form()
            data = dict(form)

    try:
        body = TokenRequest(**data)
    except Exception as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid_request:{e}")

    if body.grant_type != "authorization_code":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unsupported_grant_type")

    try:
        result = await svc.exchange_code_for_token(
            db,
            code=body.code,
            client_id=body.client_id,
            client_secret=body.client_secret,
            redirect_uri=body.redirect_uri,
        )
        await db.commit()
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))

    return TokenResponse(
        access_token=result["access_token"],
        token_type=result["token_type"],
        expires_in=result["expires_in"],
        scope=result["scope"],
        user=TokenUserInfo(**result["user"]),
    )
