"""OAuth 2.0 provider endpoints — Manor acts as IdP for downstream apps (PMS, etc.).

Endpoints
---------
GET  /api/v1/oauth/clients/{client_id}
    Public — returns minimal info (name, description) about a registered client app so
    the consent screen can render. Does not leak client_secret_hash or full URI list.
    A client_id that is an https URL is a Client ID Metadata Document: it is
    fetched (per-IP rate-limited) when no fresh copy is cached.

POST /api/v1/oauth/authorize
    Authenticated (user JWT in Authorization header).
    Body: { client_id, redirect_uri, scope, state, code_challenge?, code_challenge_method?, resource? }
    Returns: { redirect_to: "<redirect_uri>?code=<code>&state=<state>&iss=<issuer>" }
    Frontend then sets `window.location = redirect_to`.

POST /api/v1/oauth/token
    Standard OAuth 2.0 form-encoded or JSON body. A confidential client
    authenticates with client_secret in the body or HTTP Basic, not both.
    Body: { grant_type=authorization_code, code, client_id, client_secret?, redirect_uri, code_verifier?, resource? }
       or { grant_type=refresh_token, refresh_token, client_id, client_secret?, resource? }
    Returns: { access_token, token_type, expires_in, scope, user, refresh_token }

POST /api/v1/oauth/revoke
    RFC 7009 revocation of a refresh token (and its family), client-authenticated
    like /token.

POST /api/v1/oauth/register
    RFC 7591 dynamic registration of a public PKCE client (native MCP clients).
    Unauthenticated and always rate-limited per client IP.

GET    /api/v1/oauth/connections
DELETE /api/v1/oauth/connections/{id}
    Authenticated. The current user's live sign-ins (one per refresh-token
    family) and personal access tokens, and revocation of one of them.

POST /api/v1/oauth/personal-tokens
    Authenticated. Create a personal access token for the MCP endpoint; the
    token is in this response only.

GET /.well-known/oauth-authorization-server   (RFC 8414, see oauth_metadata.py)
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from datetime import datetime
from typing import List, Optional
from urllib.parse import unquote_plus, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.database_runtime import release_read_transaction
from packages.core.models.user import User
from packages.core.services import oauth_client_metadata as cimd
from packages.core.services import oauth_provider_service as svc
from packages.core.services import personal_access_tokens as pats
from apps.api.deps import get_current_user
from apps.api.middleware.rate_limit import RateLimiter, client_ip
from apps.api.routers.oauth_metadata import issuer, resource_allowed

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
    # Sent back as RFC 9207 ``iss`` with every authorization response, errors included.
    issuer: str = ""
    # An MCP client: the user may grant read-only access (``mcp:read``) instead.
    read_only_available: bool = False
    # A Client ID Metadata Document client: the host that serves its document,
    # which is its identity (its name is whatever that document says).
    client_host: Optional[str] = None


class AuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str
    redirect_uri: str
    scope: Optional[str] = ""  # space-separated OAuth scopes
    state: Optional[str] = None
    code_challenge: Optional[StrictStr] = None
    code_challenge_method: Optional[StrictStr] = None
    resource: Optional[StrictStr] = None  # RFC 8707 resource indicator


class AuthorizeResponse(BaseModel):
    redirect_to: str
    code: str
    state: Optional[str] = None


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant_type: str = Field(..., description="'authorization_code' or 'refresh_token'")
    # Optional only when HTTP Basic carries it.
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    code: Optional[str] = None
    redirect_uri: Optional[str] = None
    code_verifier: Optional[StrictStr] = None
    refresh_token: Optional[str] = None
    # RFC 8707 resource indicator. The audience is fixed per client, so this
    # may only name that audience's resource; it never widens it.
    resource: Optional[str] = None
    scope: Optional[str] = None


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str
    token_type_hint: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


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


# ── Client lookup, including Client ID Metadata Documents ──

# Each metadata document fetch is an outbound request on an unauthenticated
# caller's behalf, so fetches are limited per IP like registrations. A cached,
# fresh document costs nothing and is not counted.
CLIENT_METADATA_RATE_LIMIT = int(os.getenv("MANOR_OAUTH_CLIENT_METADATA_RATE_LIMIT", "30"))
CLIENT_METADATA_RATE_WINDOW_SECONDS = int(os.getenv("MANOR_OAUTH_CLIENT_METADATA_RATE_WINDOW_SECONDS", "3600"))


async def _client_for_consent(request: Request, db: AsyncSession, client_id: str):
    """The client a consent request names, fetching its metadata document when stale.

    Only the consent page and /authorize call this; /token reads the cached row.
    """
    client = await svc.get_client_by_id(db, client_id)
    if not cimd.is_metadata_document_client_id(client_id) or (
        client is not None and client.registration_source != svc.REGISTRATION_SOURCE_METADATA_DOCUMENT
    ):
        return client
    if client is not None and cimd.is_fresh(client):
        return client
    decision = await _register_limiter.check(
        f"oauth-client-metadata:{client_ip(request)}", CLIENT_METADATA_RATE_LIMIT,
        CLIENT_METADATA_RATE_WINDOW_SECONDS,
    )
    if not decision.allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too_many_requests",
                            headers={"Retry-After": str(decision.retry_after or CLIENT_METADATA_RATE_WINDOW_SECONDS)})
    # The fetch can take seconds; hold no transaction (or DB permit) across it.
    await release_read_transaction(db)
    try:
        client = await cimd.refresh_metadata_document_client(db, client_id, client)
    except cimd.MetadataDocumentError as e:
        await db.rollback()
        logger.info("OAuth client metadata document %s refused: %s", cimd.client_host(client_id), e.description)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{e.code}: {e.description}") from None
    await db.commit()
    return client


# ── Public client info ──

@router.get("/clients/{client_id:path}", response_model=ClientInfo)
async def get_client_info(
    client_id: str,
    request: Request,
    redirect_uri: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    client = await _client_for_consent(request, db, client_id)
    if not client or not client.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown client")
    metadata_document = client.registration_source == svc.REGISTRATION_SOURCE_METADATA_DOCUMENT
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
        issuer=issuer(request),
        read_only_available=client.token_audience == svc.MCP_TOKEN_AUDIENCE,
        client_host=cimd.client_host(client.client_id) if metadata_document else None,
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


# ── Connections: the current user's live OAuth sign-ins and access tokens ──

class ConnectedClient(BaseModel):
    # "oauth": an OAuth client's sign-in (``id`` is its refresh-token family);
    # "personal_token": a personal access token the user created.
    kind: str
    id: str
    client_id: Optional[str] = None
    name: str
    verified: bool
    entity_id: str
    connected_at: datetime
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    # Personal access tokens only.
    scope: Optional[str] = None
    token_prefix: Optional[str] = None


@router.get("/connections", response_model=List[ConnectedClient])
async def list_connections(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Agents and apps that can act as this user: OAuth clients that can still refresh, and access tokens."""
    connections = [ConnectedClient(kind="oauth", **row) for row in await svc.list_connected_clients(db, user)]
    connections += [
        ConnectedClient(kind="personal_token", verified=True, connected_at=row.pop("created_at"), **row)
        for row in await pats.list_personal_access_tokens(db, user)
    ]
    return connections


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_connection(
    connection_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke one connection or token. Others and the browser session are untouched."""
    if not (await svc.revoke_connected_client(db, user, connection_id)
            or await pats.revoke_personal_access_token(db, user, connection_id)):
        await db.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown connection")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class PersonalTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=100)
    scope: str = svc.MCP_SCOPE
    # 30, 90 or 365 days, or null for a token that never expires.
    expires_in_days: Optional[int] = pats.DEFAULT_EXPIRY_DAYS


class PersonalTokenResponse(ConnectedClient):
    # The token itself; shown once and never retrievable again.
    token: str


@router.post("/personal-tokens", response_model=PersonalTokenResponse, status_code=status.HTTP_201_CREATED)
async def create_personal_token(
    body: PersonalTokenRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an access token for the MCP endpoint, acting as this user in the current team."""
    try:
        row, token = await pats.create_personal_access_token(
            db, user, name=body.name, scope=body.scope, expires_in_days=body.expires_in_days,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    await db.commit()
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        headers={"Cache-Control": "no-store"},
        content=PersonalTokenResponse(
            kind="personal_token", id=row.id, name=row.name, verified=True, entity_id=row.entity_id,
            connected_at=row.created_at, expires_at=row.expires_at, scope=row.scope,
            token_prefix=pats.TOKEN_PREFIX + row.token_prefix, token=token,
        ).model_dump(mode="json"),
    )


# ── /authorize ──

@router.post("/authorize", response_model=AuthorizeResponse)
async def authorize(
    request: Request,
    body: AuthorizeRequest = Depends(_authorize_body),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mint an authorization code for the authenticated user, bound to the requesting client."""
    client = await _client_for_consent(request, db, body.client_id)
    if not client or not client.active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown_or_inactive_client")

    if not svc.validate_redirect_uri(client, body.redirect_uri):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "redirect_uri_not_whitelisted")
    if not resource_allowed(request, body.resource, client.token_audience):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_target")

    # Every grant gets a refresh token, so offline_access needs no permission.
    requested_scopes = [s for s in (body.scope or "").split() if s and s != svc.OFFLINE_ACCESS_SCOPE]
    if client.token_audience == svc.MCP_TOKEN_AUDIENCE:
        allowed = set(client.allowed_scopes or svc.MCP_SCOPES) & set(svc.MCP_SCOPES)
        for requested in requested_scopes:
            if requested not in allowed:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"scope_not_allowed:{requested}")
        requested_scopes = svc.granted_mcp_scopes(requested_scopes)
    else:
        # Validate requested scopes against the client's allowed scopes (if requested at all)
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
    qs["iss"] = issuer(request)  # RFC 9207: lets the client detect mix-up attacks
    sep = "&" if "?" in body.redirect_uri else "?"
    redirect_to = f"{body.redirect_uri}{sep}{urlencode(qs)}"

    return AuthorizeResponse(redirect_to=redirect_to, code=code.code, state=body.state)


# ── /token ──

class _ClientAuthError(Exception):
    pass


def _client_credentials(request: Request, body_id: Optional[str], body_secret: Optional[str]) -> tuple[str, Optional[str], bool]:
    """(client_id, client_secret, via_basic) from HTTP Basic (RFC 6749 §2.3.1) or the body.

    A client uses one method: Basic plus a body secret, or a Basic id that differs
    from the body's, is refused rather than resolved in either's favour.
    """
    scheme, _, encoded = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "basic":
        if not body_id:
            raise _ClientAuthError("invalid_request")
        return body_id, body_secret, False
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise _ClientAuthError("invalid_request") from None
    raw_id, sep, raw_secret = decoded.partition(":")
    client_id, client_secret = unquote_plus(raw_id), unquote_plus(raw_secret)
    if not sep or not client_id or body_secret is not None or (body_id and body_id != client_id):
        raise _ClientAuthError("invalid_request")
    return client_id, client_secret, True


def _invalid_client(via_basic: bool) -> JSONResponse:
    # A client that authenticated with Basic is told so with a 401 challenge (RFC 6749 §5.2).
    if via_basic:
        return _oauth_error("invalid_client", status.HTTP_401_UNAUTHORIZED,
                            headers={"WWW-Authenticate": 'Basic realm="manor"'})
    return _oauth_error("invalid_client", status.HTTP_400_BAD_REQUEST)


@router.post("/token", response_model=TokenResponse)
async def token(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OAuth 2.0 token endpoint. Accepts JSON body OR application/x-www-form-urlencoded."""
    body = await _protocol_body(request, TokenRequest)
    try:
        client_id, client_secret, via_basic = _client_credentials(request, body.client_id, body.client_secret)
    except _ClientAuthError as e:
        return _oauth_error(str(e), status.HTTP_400_BAD_REQUEST)

    if body.resource is not None:
        client = await svc.get_client_by_id(db, client_id)
        if client is not None and not resource_allowed(request, body.resource, client.token_audience):
            return _oauth_error("invalid_target", status.HTTP_400_BAD_REQUEST)

    try:
        if body.grant_type == "authorization_code":
            if not body.code or not body.redirect_uri:
                return _oauth_error("invalid_request", status.HTTP_400_BAD_REQUEST)
            result = await svc.exchange_code_for_token(
                db,
                code=body.code,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=body.redirect_uri,
                code_verifier=body.code_verifier,
            )
        elif body.grant_type == "refresh_token":
            if not body.refresh_token:
                return _oauth_error("invalid_request", status.HTTP_400_BAD_REQUEST)
            result = await svc.refresh_access_token(
                db,
                refresh_token=body.refresh_token,
                client_id=client_id,
                client_secret=client_secret,
            )
        else:
            return _oauth_error("unsupported_grant_type", status.HTTP_400_BAD_REQUEST)
        await db.commit()
    except svc.RefreshTokenReuse as e:
        await db.commit()  # persist the family revocation
        return _oauth_error(str(e), status.HTTP_400_BAD_REQUEST)
    except ValueError as e:
        await db.rollback()
        if str(e) == "invalid_client":
            return _invalid_client(via_basic)
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


# ── /revoke (RFC 7009) ──

@router.post("/revoke")
async def revoke(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Revoke a refresh token's family, e.g. when the user disconnects in the client.

    Access tokens are self-contained JWTs that expire within the hour; they
    cannot be revoked one by one, which RFC 7009 reports as unsupported_token_type.
    """
    body = await _protocol_body(request, RevokeRequest)
    try:
        client_id, client_secret, via_basic = _client_credentials(request, body.client_id, body.client_secret)
    except _ClientAuthError as e:
        return _oauth_error(str(e), status.HTTP_400_BAD_REQUEST)
    client = await svc.verify_client_credentials(db, client_id, client_secret)
    if client is None:
        return _invalid_client(via_basic)
    if body.token.count(".") == 2:  # JWT shape; refresh tokens are opaque base64url
        return _oauth_error("unsupported_token_type", status.HTTP_400_BAD_REQUEST)
    await svc.revoke_refresh_token(db, token=body.token, client=client)
    await db.commit()
    return Response(status_code=status.HTTP_200_OK, headers={"Cache-Control": "no-store"})
