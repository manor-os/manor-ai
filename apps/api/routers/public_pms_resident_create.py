"""Browser-only consent and confirmation; no model endpoint can submit a repair."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.routers import public_pms_resident as base
from apps.api.routers.public_chat import PrivateChatResponseRoute
from packages.core.database import get_db
from packages.core.services import webchat_resident as read
from packages.core.services import webchat_resident_create as create

router = APIRouter(prefix="/api/v1/public/chat", tags=["resident-webchat"], route_class=PrivateChatResponseRoute)


class EditBody(base.SessionBody):
    action: Literal["edit", "cancel"]
    revision: int = Field(gt=0, strict=True)
    fields: create.DraftFields | None = None

    @model_validator(mode="after")
    def exact_action_fields(self):
        if (self.action == "edit") != (self.fields is not None):
            raise ValueError("Editing requires fields; cancellation does not accept fields")
        return self


class ConfirmBody(base.SessionBody):
    revision: int = Field(gt=0, strict=True)
    confirmation_nonce: str = Field(min_length=1, max_length=128, strict=True)


async def _context(db, token, request, session_id):
    cc, _, contact, _, user = await base._context(db, token, request, session_id)
    scope = await read.require_private_access(
        db, contact=contact, cc=cc, session_id=session_id, request=request, manor_user_id=user.id if user else None
    )
    if scope is None:
        raise read.authentication_required()
    binding = await db.get(read.WebchatResidentBinding, scope.binding_id)
    proof = request.cookies.get(read.cookie_name(binding.conversation_id))
    read.require_proof(binding, proof)
    return binding, proof


@router.post("/{token}/resident/create/start")
async def start(token: str, body: base.SessionBody, request: Request, db: AsyncSession = Depends(get_db)):
    base._require_origin(request)
    try:
        admission = await base._start_limiter.check(f"resident-create-start:{token}:{base.client_ip(request)}", 10, 300)
        if not admission.allowed:
            raise HTTPException(429, "Too many verification attempts")
        binding, proof = await _context(db, token, request, body.session_id)
        return {
            "authorize_url": await create.begin_authorization(
                db, binding, proof=proof, callback_uri=base.callback_url(request)
            )
        }
    except read.ResidentAccessError as exc:
        raise base.http_error(exc) from exc


@router.get("/{token}/resident/maintenance/drafts")
async def drafts(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    session_id = base.session_query(request, allow=("skip", "limit"))
    try:
        skip, limit = int(request.query_params.get("skip", "0")), int(request.query_params.get("limit", "20"))
        if not 0 <= skip <= 10000 or not 1 <= limit <= 50:
            raise ValueError("pagination")
    except ValueError as exc:
        raise HTTPException(422, "Invalid pagination") from exc
    try:
        binding, proof = await _context(db, token, request, session_id)
        return await create.collection(db, binding, proof=proof, skip=skip, limit=limit)
    except read.ResidentAccessError as exc:
        raise base.http_error(exc) from exc


@router.patch("/{token}/resident/maintenance/drafts/{draft_id}")
async def edit(token: str, draft_id: str, body: EditBody, request: Request, db: AsyncSession = Depends(get_db)):
    base._require_origin(request)
    try:
        binding, proof = await _context(db, token, request, body.session_id)
        draft = await create.edit_draft(
            db, binding, draft_id, revision=body.revision, fields=body.fields, cancel=body.action == "cancel"
        )
        await create.validate_grant(db, binding)
        return create.project_draft(draft, proof=proof)
    except read.ResidentAccessError as exc:
        raise base.http_error(exc) from exc


@router.post("/{token}/resident/maintenance/drafts/{draft_id}/confirm")
async def confirm(
    token: str,
    draft_id: str,
    body: ConfirmBody,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    base._require_origin(request)
    try:
        binding, proof = await _context(db, token, request, body.session_id)
        draft = await create.submit_draft(
            db, binding, draft_id, revision=body.revision, nonce=body.confirmation_nonce, proof=proof
        )
        response.status_code = 202 if draft.state in {"confirmed", "submitting", "outcome_unknown"} else 200
        return create.project_draft(draft, proof=proof)
    except read.ResidentAccessError as exc:
        raise base.http_error(exc) from exc


@router.post("/{token}/resident/maintenance/drafts/{draft_id}/recover")
async def recover(
    token: str,
    draft_id: str,
    body: base.SessionBody,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    base._require_origin(request)
    try:
        binding, proof = await _context(db, token, request, body.session_id)
        draft = await create.submit_draft(db, binding, draft_id, proof=proof, recover=True)
        response.status_code = 202 if draft.state in {"confirmed", "submitting", "outcome_unknown"} else 200
        return create.project_draft(draft, proof=proof)
    except read.ResidentAccessError as exc:
        raise base.http_error(exc) from exc
