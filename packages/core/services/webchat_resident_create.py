"""Resident-owned local drafts; only explicit browser confirmation writes to PMS."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from typing import Annotated, Literal
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select

from packages.core.credentials import Requester
from packages.core.models.base import generate_ulid
from packages.core.models.webchat_resident_create import ResidentCreateGrant, ResidentMaintenanceDraft
from packages.core.services import webchat_resident as read

SCOPE = "pms:maintenance:self:create"
TOOL_NAME = "resident_maintenance_draft"
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
LocationRef = Annotated[str, StringConstraints(pattern=r"^loc_[a-f0-9]{64}$")]


class DraftContent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: Title
    description: Description
    category: Literal["plumbing", "electrical", "hvac", "appliance", "other"] = "other"
    priority: Literal["low", "medium", "high", "urgent"] = "medium"


class DraftFields(DraftContent):
    location_ref: LocationRef | None = None


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    location_ref: LocationRef
    label: Annotated[str, StringConstraints(min_length=1, max_length=500)]


def auth_required():
    return read.ResidentAccessError(
        401, "resident_create_auth_required", "Authorize resident maintenance creation to continue."
    )


def unavailable():
    return read.ResidentAccessError(
        503, "resident_create_unavailable", "Maintenance creation is temporarily unavailable."
    )


def conflict():
    return read.ResidentAccessError(
        409, "resident_draft_conflict", "This draft changed or is already confirmed. Reload its current state."
    )


def _store(grant, payload):
    try:
        read.get_credential_service().store_resident_create(grant, payload)
    except Exception as exc:
        raise unavailable() from exc


def _tokens(grant):
    try:
        return read.get_credential_service().lease_resident_create(
            grant, requester=Requester(kind="service", id=grant.id), reason="webchat.resident.create"
        )
    except Exception as exc:
        raise unavailable() from exc


def _identity(payload, binding):
    if not isinstance(payload, dict) or payload.get("scope") != SCOPE:
        raise auth_required()
    resident = read.validate_resident_identity(
        {**payload, "scope": read.SCOPE}, expected_entity=binding.pms_entity_id, expected_subject=binding.subject
    )
    if resident["tenant_id"] != binding.tenant_id:
        raise auth_required()
    return resident


async def grant_for(db, binding_id, *, lock=False):
    q = select(ResidentCreateGrant).where(ResidentCreateGrant.binding_id == binding_id)
    if lock:
        q = q.with_for_update()
    return await db.scalar(q.execution_options(populate_existing=True))


async def validate_grant(db, binding, *, expected_read_generation=None):
    await read.validate_binding(db, binding, expected_generation=expected_read_generation)
    grant = await grant_for(db, binding.id)
    if (
        not grant
        or grant.status != "active"
        or not grant.expires_at
        or grant.expires_at <= read.now()
        or grant.issuer != binding.issuer
    ):
        raise auth_required()
    generation = grant.generation
    try:
        payload = await read._provider_request(grant.issuer, "identity", token=_tokens(grant).get("access_token"))
    except read.ResidentAccessError as exc:
        raise auth_required() if exc.status in {401, 403} else unavailable() from exc
    _identity(payload, binding)
    await db.refresh(grant)
    if (
        grant.status != "active"
        or grant.generation != generation
        or not grant.expires_at
        or grant.expires_at <= read.now()
    ):
        raise auth_required()
    await read.validate_binding(db, binding, expected_generation=expected_read_generation)
    await db.refresh(grant)
    if (
        grant.status != "active"
        or grant.generation != generation
        or not grant.expires_at
        or grant.expires_at <= read.now()
    ):
        raise auth_required()
    return grant


async def begin_authorization(db, binding, *, proof, callback_uri):
    await db.refresh(binding, with_for_update=True)
    read.require_proof(binding, proof)
    await read.validate_binding(db, binding)
    grant = await grant_for(db, binding.id, lock=True)
    if grant is None:
        grant = ResidentCreateGrant(
            id=generate_ulid(),
            binding_id=binding.id,
            issuer=binding.issuer,
            credential_scheme=read.get_credential_service().backend,
            read_generation=binding.generation,
            generation=0,
            status="pending",
        )
        db.add(grant)
    payload = _tokens(grant) if grant.credential_ref else {}
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    payload.update(code_verifier=verifier, callback_uri=callback_uri)
    grant.state_hash, grant.state_expires_at = read.digest(state), read.now() + timedelta(seconds=120)
    grant.read_generation = binding.generation
    grant.generation += 1
    _store(grant, payload)
    config = await read._oauth_config(db)
    await db.commit()
    auth = urlsplit(read.manor_oauth_endpoints("manor_pms")["authorize_url"])
    url = urlunsplit((auth.scheme, auth.netloc, "/resident-webchat/authorize", "", ""))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return (
        url
        + "?"
        + urlencode(
            {
                "client_id": config.client_id,
                "redirect_uri": callback_uri,
                "response_type": "code",
                "scope": SCOPE,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )


async def complete_authorization(db, *, state, proof, callback_uri, code=None, error=None):
    # Lock the same read boundary first as confirmation/disconnect, then grant.
    binding_id = await db.scalar(
        select(ResidentCreateGrant.binding_id).where(ResidentCreateGrant.state_hash == read.digest(state))
    )
    if binding_id is None:
        raise read.authentication_required()
    binding = await db.get(read.WebchatResidentBinding, binding_id, with_for_update=True, populate_existing=True)
    grant = await grant_for(db, binding_id, lock=True)
    if (
        not binding
        or not grant
        or grant.state_hash != read.digest(state)
        or not grant.state_expires_at
        or grant.state_expires_at <= read.now()
    ):
        raise read.authentication_required()
    read.require_proof(binding, proof(binding.conversation_id))
    payload = _tokens(grant)
    if payload.get("callback_uri") != callback_uri:
        raise read.authentication_required()
    grant.state_hash, grant.state_expires_at = None, None
    acquired = []
    try:
        await read.validate_binding(db, binding, expected_generation=grant.read_generation)
        if error:
            if error != "access_denied" or code:
                raise auth_required()
            payload.pop("code_verifier", None)
            payload.pop("callback_uri", None)
            _store(grant, payload)
            await db.commit()
            return binding, "cancelled"
        if not code:
            raise auth_required()
        response = await read._provider_request(
            grant.issuer,
            "token",
            config=await read._oauth_config(db),
            form={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": callback_uri,
                "code_verifier": payload.get("code_verifier", ""),
            },
        )
        token = response.get("access_token")
        if not isinstance(token, str) or not token.startswith("pms_rcreate_"):
            raise unavailable()
        acquired = list(dict.fromkeys([token, *read._token_material(payload)]))
        _store(grant, {"pending_revocations": acquired})
        if not isinstance(response.get("token_type"), str) or response["token_type"].lower() != "bearer":
            raise unavailable()
        _identity(response, binding)
        live = await read._provider_request(grant.issuer, "identity", token=token)
        _identity(live, binding)
        remaining = await read._revoke_material(
            db, grant, [value for value in read._token_material(payload) if value != token]
        )
        acquired = [token, *remaining]
        _store(grant, {"pending_revocations": acquired})
        if remaining:
            raise read.ResidentAccessError(
                502, "resident_revoke_pending", "Previous creation consent could not be retired. Disconnect and retry."
            )
        await read.validate_binding(db, binding, expected_generation=grant.read_generation)
        _store(grant, {"access_token": token})
        grant.status = "active"
        grant.expires_at = read.now() + timedelta(seconds=min(response["expires_in"], live["expires_in"]))
        await db.commit()
        return binding, "connected"
    except Exception as exc:
        if acquired:
            grant.status = "revoked"
            try:
                remaining = await read._revoke_material(db, grant, acquired)
            except read.ResidentAccessError:
                remaining = acquired
            try:
                _store(grant, {"pending_revocations": remaining})
            except read.ResidentAccessError:
                pass
        await db.commit()
        failure = exc if isinstance(exc, read.ResidentAccessError) else unavailable()
        failure.callback_channel_config_id = binding.channel_config_id
        raise failure from exc


async def disable_grant(db, binding):
    grant = await grant_for(db, binding.id, lock=True)
    if grant:
        grant.status, grant.state_hash, grant.state_expires_at = "revoked", None, None
        grant.generation += 1
    return grant


async def revoke_grant(db, grant):
    if grant is None:
        return True
    try:
        remaining = await read._revoke_material(db, grant, read._token_material(_tokens(grant)))
        _store(grant, {"pending_revocations": remaining})
        # The disconnect caller holds both read/create locks until one commit.
        return not remaining
    except read.ResidentAccessError:
        return False


async def _request(grant, path, *, payload=None, submission_key=None):
    if grant.issuer != read.manor_oauth_endpoints("manor_pms")["issuer"]:
        raise unavailable()
    try:
        headers = {"Authorization": "Bearer " + _tokens(grant)["access_token"], "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            url = grant.issuer + "/api/resident-webchat/" + path
            if payload is None:
                response = await client.get(url, headers=headers)
            else:
                response = await client.post(url, headers={**headers, "Idempotency-Key": submission_key}, json=payload)
        if response.status_code in {401, 403}:
            raise auth_required()
        if response.status_code == 404:
            raise read.ResidentAccessError(404, "resident_not_found", "Resident resource not found.")
        if response.status_code not in {200, 201}:
            raise unavailable()
        data = response.json()
        if not isinstance(data, dict):
            raise unavailable()
        return data
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        if isinstance(exc, read.ResidentAccessError):
            raise
        raise unavailable() from exc


async def locations(db, binding):
    generation = binding.generation
    grant = await validate_grant(db, binding, expected_read_generation=generation)
    payload = await _request(grant, "maintenance/locations")
    try:
        if not isinstance(payload.get("items"), list) or len(payload["items"]) > 100:
            raise unavailable()
        choices = [Location.model_validate(value).model_dump() for value in payload["items"]]
        if len({value["location_ref"] for value in choices}) != len(choices):
            raise unavailable()
    except ValueError as exc:
        raise unavailable() from exc
    await validate_grant(db, binding, expected_read_generation=generation)
    return choices


def payload_hash(fields):
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def confirmation_nonce(draft, proof):
    message = f"resident-confirm-v1:{draft.binding_id}:{draft.id}:{draft.revision}:{draft.payload_hash}"
    return hmac.new(proof.encode(), message.encode(), hashlib.sha256).hexdigest()


def project_draft(draft, *, proof=None):
    return {
        "draft_id": draft.id,
        "revision": draft.revision,
        "state": draft.state,
        "fields": draft.fields,
        "location_label": draft.location_label,
        **(
            {
                "confirmation_nonce": confirmation_nonce(draft, proof)
                if draft.state == "draft" and draft.fields.get("location_ref")
                else None
            }
            if proof
            else {}
        ),
        "request": draft.request if draft.state == "submitted" else None,
        "error": draft.error,
        "updated_at": draft.updated_at,
    }


async def make_draft(db, binding, content: DraftContent, *, source_call_id=None, expected_read_generation=None):
    generation = binding.generation if expected_read_generation is None else expected_read_generation
    await db.refresh(binding, with_for_update=True)
    await validate_grant(db, binding, expected_read_generation=generation)
    if source_call_id:
        existing = await db.scalar(
            select(ResidentMaintenanceDraft).where(
                ResidentMaintenanceDraft.binding_id == binding.id,
                ResidentMaintenanceDraft.source_call_id == source_call_id,
            )
        )
        if existing:
            return existing
    choices = await locations(db, binding)
    fields = content.model_dump()
    fields["location_ref"] = choices[0]["location_ref"] if len(choices) == 1 else None
    draft = ResidentMaintenanceDraft(
        id=generate_ulid(),
        binding_id=binding.id,
        source_call_id=source_call_id,
        fields=fields,
        location_label=choices[0]["label"] if len(choices) == 1 else None,
        payload_hash=payload_hash(fields),
        submission_key=secrets.token_hex(24),
        state="draft",
        revision=1,
    )
    await validate_grant(db, binding, expected_read_generation=generation)
    db.add(draft)
    await db.commit()
    await db.refresh(draft)
    return draft


async def _draft(db, binding, draft_id, *, lock=True):
    query = select(ResidentMaintenanceDraft).where(
        ResidentMaintenanceDraft.id == draft_id, ResidentMaintenanceDraft.binding_id == binding.id
    )
    if lock:
        query = query.with_for_update()
    row = await db.scalar(query.execution_options(populate_existing=True))
    if row is None:
        raise read.ResidentAccessError(404, "resident_draft_not_found", "Draft not found.")
    return row


async def edit_draft(db, binding, draft_id, *, revision, fields=None, cancel=False):
    await db.refresh(binding, with_for_update=True)
    await validate_grant(db, binding)
    draft = await _draft(db, binding, draft_id)
    if draft.revision != revision or draft.state != "draft":
        raise conflict()
    if cancel:
        draft.state = "cancelled"
    else:
        choices = await locations(db, binding)
        selected = next((choice for choice in choices if choice["location_ref"] == fields.location_ref), None)
        if fields.location_ref and selected is None:
            raise read.ResidentAccessError(404, "resident_not_found", "Resident location not found.")
        draft.fields = fields.model_dump()
        draft.location_label = selected["label"] if selected else None
        draft.payload_hash = payload_hash(draft.fields)
        draft.revision += 1
    await db.commit()
    await db.refresh(draft)
    return draft


async def collection(db, binding, *, proof, skip=0, limit=20):
    try:
        grant = await validate_grant(db, binding)
    except read.ResidentAccessError as exc:
        if exc.code != "resident_create_auth_required":
            raise
        return {
            "create_access": {"verified": False, "expires_at": None},
            "locations": [],
            "items": [],
            "skip": skip,
            "limit": limit,
            "has_more": False,
        }
    choices = await locations(db, binding)
    drafts = list(
        (
            await db.scalars(
                select(ResidentMaintenanceDraft)
                .where(ResidentMaintenanceDraft.binding_id == binding.id)
                .order_by(ResidentMaintenanceDraft.created_at.desc(), ResidentMaintenanceDraft.id.desc())
                .offset(skip)
                .limit(limit + 1)
            )
        ).all()
    )
    await validate_grant(db, binding)
    return {
        "create_access": {"verified": True, "expires_at": grant.expires_at},
        "locations": choices,
        "items": [project_draft(draft, proof=proof) for draft in drafts[:limit]],
        "skip": skip,
        "limit": limit,
        "has_more": len(drafts) > limit,
    }


async def submit_draft(db, binding, draft_id, *, revision=None, nonce=None, proof, recover=False):
    await db.refresh(binding, with_for_update=True)
    generation = binding.generation
    await validate_grant(db, binding, expected_read_generation=generation)
    read.require_proof(binding, proof)
    draft = await _draft(db, binding, draft_id)
    if recover:
        if draft.confirmed_at is None:
            raise conflict()
    elif draft.revision != revision or not nonce or not hmac.compare_digest(nonce, confirmation_nonce(draft, proof)):
        raise conflict()
    if draft.state == "submitted":
        return draft
    if draft.state in {"cancelled", "rejected"}:
        raise conflict()
    if draft.attempt_id and draft.attempt_expires_at and draft.attempt_expires_at > read.now():
        return draft
    if draft.state == "draft":
        if recover or not draft.fields.get("location_ref"):
            raise conflict()
        choices = await locations(db, binding)
        if not any(choice["location_ref"] == draft.fields["location_ref"] for choice in choices):
            raise read.ResidentAccessError(404, "resident_not_found", "Resident location not found.")
        draft.confirmed_payload = DraftFields.model_validate(draft.fields).model_dump()
        draft.confirmed_at = read.now()
    elif draft.confirmed_at is None or draft.confirmed_payload is None:
        raise conflict()
    attempt = secrets.token_hex(16)
    draft.attempt_id, draft.attempt_expires_at = attempt, read.now() + timedelta(seconds=30)
    draft.state, draft.error = "submitting", None
    payload, key = dict(draft.confirmed_payload), draft.submission_key
    await db.commit()  # Freeze intent/key before any external write.
    receipt = None
    failure = None
    try:
        await db.refresh(binding, with_for_update=True)
        await grant_for(db, binding.id, lock=True)
        grant = await validate_grant(db, binding, expected_read_generation=generation)
        if recover:
            try:
                result = await _request(grant, "maintenance/submissions/" + key)
                if result.get("state") != "committed":
                    raise unavailable()
                receipt = read.ResidentMaintenanceRecord.model_validate(result.get("request")).model_dump()
            except read.ResidentAccessError as exc:
                if exc.status != 404:
                    raise
                # 404 may race an earlier in-flight commit. Only replay this
                # exact immutable key/payload; never infer that no row exists.
        if receipt is None:
            grant = await validate_grant(db, binding, expected_read_generation=generation)
            result = await _request(grant, "maintenance", payload=payload, submission_key=key)
            if type(result.get("replayed")) is not bool:
                raise unavailable()
            receipt = read.ResidentMaintenanceRecord.model_validate(result.get("request")).model_dump()
    except (read.ResidentAccessError, ValueError) as exc:
        failure = exc if isinstance(exc, read.ResidentAccessError) else unavailable()
    draft = await _draft(db, binding, draft_id)
    if draft.attempt_id == attempt:
        draft.attempt_id, draft.attempt_expires_at = None, None
        if receipt:
            draft.state, draft.request, draft.error = "submitted", receipt, None
        else:
            draft.state = "outcome_unknown"
            draft.error = {
                "code": "resident_outcome_unknown",
                "message": "The submission result is not yet known. Check and recover this same submission.",
            }
        await db.commit()
        await db.refresh(draft)
    await validate_grant(
        db, binding, expected_read_generation=generation
    )  # Publication fence, including late successful writes.
    if failure and failure.code in {"resident_auth_required", "resident_create_auth_required"}:
        raise failure
    return draft
