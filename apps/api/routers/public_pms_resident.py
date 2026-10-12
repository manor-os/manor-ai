"""Resident read verification uses dedicated tokens, never management OAuth grants."""
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.routers.public_chat import PrivateChatResponseRoute
from apps.api.deps import get_current_user
from apps.api.middleware.rate_limit import RateLimiter, client_ip
from packages.core.database import get_db
from packages.core.models.user import OAuthAccount, User
from packages.core.services import webchat_resident as resident

router = APIRouter(prefix="/api/v1/public", tags=["resident-webchat"], route_class=PrivateChatResponseRoute)
owner_router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])
_start_limiter = RateLimiter()
_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


class SessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=128)


def http_error(exc):
    return HTTPException(exc.status, {"code": exc.code, "message": exc.message}, headers=_HEADERS)


def callback_url(request):
    from packages.core.config import get_settings
    from packages.core.services.manor_integration_oauth import _deployment_url
    value = (get_settings().APP_URL or "").strip()
    if not value:
        raise HTTPException(503, "Configure the public application URL before resident verification")
    try:
        return _deployment_url(value, origin=True) + resident.CALLBACK_PATH
    except ValueError as exc:
        raise HTTPException(503, "Resident callback configuration is unavailable") from exc


def _require_origin(request):
    expected = urlsplit(callback_url(request))
    origin = request.headers.get("origin", "")
    if origin != urlunsplit((expected.scheme, expected.netloc, "", "", "")):
        raise HTTPException(403, {"code": "resident_invalid_origin", "message": "This request must come from the chat page."})


async def _context(db, token, request, session_id):
    from apps.api.routers import public_chat
    cc, binding = await public_chat._resolve_channel_by_token(db, token)
    user = await public_chat._require_chat_access(cc, token, request, db, binding)
    conv = await public_chat.find_public_webchat_conversation_by_session(
        db, entity_id=cc.entity_id, channel_config_id=cc.id, session_id=session_id,
    )
    if conv is None:
        raise HTTPException(404, "Chat session not found")
    contact = await public_chat._ensure_session_contact_for_user(
        db, cc=cc, session_id=session_id, user=user, conversation=conv,
        request=request, resident_check=False,
    )
    return cc, binding, contact, conv, user


def session_query(request, *, allow=()):
    pairs = list(request.query_params.multi_items())
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)) or set(keys) - {"session_id", *allow}:
        raise HTTPException(400, {"code": "resident_invalid_request", "message": "Invalid query parameters."})
    session_id = request.query_params.get("session_id", "")
    if not session_id or len(session_id) > 128:
        raise HTTPException(422, "session_id is required")
    return session_id


@owner_router.get("/{workspace_id}/resident-pms/accounts")
async def target_accounts(workspace_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from apps.api.routers.workspaces import _require_workspace_manage
    await _require_workspace_manage(db, workspace_id, user)
    accounts = (await db.scalars(select(OAuthAccount).where(
        OAuthAccount.user_id == user.id, OAuthAccount.provider == "manor_pms",
    ))).all()
    return {"accounts": [{"id": account.id, "display_name": (account.profile or {}).get("display_name") or "PMS"}
                         for account in accounts if resident.manor_oauth_account_matches(account, entity_id=user.entity_id)]}


@router.post("/chat/{token}/resident/start")
async def start(token: str, body: SessionBody, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    _require_origin(request)
    admission = await _start_limiter.check(f"resident-start:{token}:{client_ip(request)}", 10, 300)
    if not admission.allowed:
        raise HTTPException(429, "Too many verification attempts", headers={**_HEADERS, "Retry-After": str(admission.retry_after or 300)})
    cc, binding, contact, conv, user = await _context(db, token, request, body.session_id)
    try:
        row, proof, url = await resident.begin_authorization(
            db, cc=cc, binding=binding, contact=contact, conversation=conv,
            session_id=body.session_id, user_id=user.id if user else None,
            proof=request.cookies.get(resident.cookie_name(conv.id)), callback_uri=callback_url(request),
        )
    except resident.ResidentAccessError as exc:
        raise http_error(exc) from exc
    response.set_cookie(resident.cookie_name(row.conversation_id), proof, httponly=True,
        secure=urlsplit(callback_url(request)).scheme == "https", samesite="lax", path="/", max_age=86400)
    response.headers.update(_HEADERS)
    return {"authorize_url": url}


@router.get("/pms-resident/callback")
async def callback(request: Request, db: AsyncSession = Depends(get_db)):
    pairs = list(request.query_params.multi_items())
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)) or set(keys) - {"code", "state", "error"}:
        raise HTTPException(400, "Invalid resident callback")
    state = request.query_params.get("state", "")
    if not 16 <= len(state) <= 256:
        raise HTTPException(400, "Invalid resident callback")
    from packages.core.services import webchat_resident_create
    from packages.core.models.webchat_resident_create import ResidentCreateGrant
    is_create = await db.scalar(select(ResidentCreateGrant.id).where(ResidentCreateGrant.state_hash == resident.digest(state))) is not None
    callback_service = webchat_resident_create if is_create else resident
    try:
        row, outcome = await callback_service.complete_authorization(
            db, state=state, callback_uri=callback_url(request),
            proof=lambda conv: request.cookies.get(resident.cookie_name(conv)),
            code=request.query_params.get("code"), error=request.query_params.get("error"),
        )
    except resident.ResidentAccessError as exc:
        channel_id = getattr(exc, "callback_channel_config_id", None)
        if not channel_id:
            raise http_error(exc) from exc
        outcome = "error"
    else:
        channel_id = row.channel_config_id
    from packages.core.models.channel import ChannelConfig
    cc = await db.get(ChannelConfig, channel_id)
    entry = (cc.config or {}).get("public_token") if cc else None
    if not entry:
        raise HTTPException(404, "Chat not found")
    from apps.api.web_base import public_web_base
    parameter = "resident_create" if is_create else "resident"
    return RedirectResponse(public_web_base(request) + "/chat/public/" + entry + "?" + parameter + "=" + outcome,
        status_code=303, headers=_HEADERS)


@router.get("/chat/{token}/resident")
async def status(token: str, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    session_id = session_query(request)
    cc, binding, contact, conv, user = await _context(db, token, request, session_id)
    response.headers.update(_HEADERS)
    try:
        scope = await resident.require_private_access(db, cc=cc, contact=contact, session_id=session_id, request=request, manor_user_id=user.id if user else None)
        if scope is None:
            return {"verified": False, "expires_at": None}
        row = await db.get(resident.WebchatResidentBinding, scope.binding_id)
        return {"verified": True, "expires_at": row.expires_at}
    except resident.ResidentAccessError as exc:
        raise http_error(exc) from exc


async def _read(token, request, response, db, request_id=None):
    session_id = session_query(request, allow=("skip", "limit", "status") if request_id is None else ())
    cc, binding, contact, conv, user = await _context(db, token, request, session_id)
    try:
        scope = await resident.require_private_access(db, cc=cc, contact=contact, session_id=session_id, request=request, manor_user_id=user.id if user else None)
        if not scope:
            raise resident.authentication_required()
        values = {key: value for key, value in request.query_params.items() if key != "session_id"}
        for field in ("skip", "limit"):
            if field in values:
                values[field] = int(values[field])
        if request_id is not None:
            values["request_id"] = request_id
        args = resident.ResidentReadRequest.model_validate(values)
        row = await db.get(resident.WebchatResidentBinding, scope.binding_id)
        result = await resident.read_maintenance(db, row, args)
        response.headers.update(_HEADERS)
        return result
    except (ValueError, ValidationError) as exc:
        if isinstance(exc, resident.ResidentAccessError):
            raise http_error(exc) from exc
        raise HTTPException(422, {"code": "resident_invalid_request", "message": "Invalid maintenance query."}) from exc


@router.get("/chat/{token}/resident/maintenance")
async def maintenance_list(token: str, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    return await _read(token, request, response, db)


@router.get("/chat/{token}/resident/maintenance/{request_id}")
async def maintenance_detail(token: str, request_id: int, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    return await _read(token, request, response, db, request_id)


@router.delete("/chat/{token}/resident", status_code=204)
async def disconnect(token: str, body: SessionBody, request: Request, db: AsyncSession = Depends(get_db)):
    _require_origin(request)
    cc, binding, contact, conv, user = await _context(db, token, request, body.session_id)
    row = await resident.find_binding(db, contact_id=contact.id, lock=True)
    response = Response(status_code=204, headers=_HEADERS)
    response.delete_cookie(resident.cookie_name(conv.id), path="/")
    if row:
        try:
            resident.require_proof(row, request.cookies.get(resident.cookie_name(conv.id)))
            await resident.disconnect(db, row)
        except resident.ResidentAccessError as exc:
            raise http_error(exc) from exc
    return response
