"""Explicit, short-lived resident reads. Public conversation IDs confer no authority."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from packages.core.credentials import Requester, get_credential_service
from packages.core.models.webchat_resident import WebchatResidentBinding
from packages.core.models.user import OAuthAccount, User
from packages.core.models.document import Channel
from packages.core.models.channel import ChannelConfig, ChannelContact
from packages.core.models.task import Conversation
from packages.core.services.manor_integration_oauth import (
    manor_oauth_account_matches, manor_oauth_endpoints,
)

SCOPE = "pms:maintenance:self:read"
CALLBACK_PATH = "/api/v1/public/pms-resident/callback"
PRIVATE_MARKER = "resident_private"
TOOL_NAME = "resident_maintenance_status"


class ResidentAccessError(ValueError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def unavailable():
    return ResidentAccessError(503, "resident_unavailable", "Resident verification is temporarily unavailable.")


def authentication_required():
    return ResidentAccessError(401, "resident_auth_required", "Verify your resident identity to access this private conversation.")


class ResidentReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: int | None = Field(default=None, gt=0)
    status: Literal["open", "in_progress", "completed", "cancelled"] | None = None
    skip: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def detail_has_no_list_selectors(self):
        if self.request_id is not None and self.model_fields_set & {"status", "skip", "limit"}:
            raise ValueError("A detail request cannot also specify list filters")
        return self


class ResidentMaintenanceRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: int = Field(gt=0)
    title: str
    description: str
    category: str
    priority: str
    status: str
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None


def validate_resident_identity(data: dict, *, expected_entity: int, expected_subject: str | None = None) -> dict:
    if not isinstance(data, dict) or data.get("scope") != SCOPE:
        raise authentication_required()
    ttl = data.get("expires_in")
    resident = data.get("resident")
    if type(ttl) is not int or not 0 < ttl <= 300 or not isinstance(resident, dict):
        raise authentication_required()
    tenant, entity, subject = resident.get("tenant_id"), resident.get("entity_id"), resident.get("sub")
    if (type(tenant) is not int or tenant <= 0 or type(entity) is not int or entity != expected_entity
            or not isinstance(subject, str) or not subject.strip() or len(subject) > 255
            or any(ord(ch) < 32 for ch in subject)):
        raise ResidentAccessError(403, "resident_identity_mismatch", "Resident identity does not match this chat.")
    if expected_subject is not None and not hmac.compare_digest(subject, expected_subject):
        raise ResidentAccessError(403, "resident_identity_mismatch", "This private conversation belongs to a different resident. Start a new chat.")
    return {"sub": subject, "tenant_id": tenant, "entity_id": entity}


def now():
    return datetime.now(timezone.utc)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def cookie_name(conversation_id: str) -> str:
    return "manor_resident_" + conversation_id


def require_proof(row, proof: str | None):
    if not proof or not hmac.compare_digest(digest(proof), row.browser_proof_hash):
        raise authentication_required()


def _credentials(row) -> dict:
    try:
        return get_credential_service().lease_webchat_resident(
            row, requester=Requester(kind="service", id=row.id), reason="webchat.resident.read",
        )
    except Exception as exc:
        raise unavailable() from exc


def _store(row, data):
    try:
        get_credential_service().store_webchat_resident(row, data)
    except Exception as exc:
        raise unavailable() from exc


async def _provider_request(issuer: str, path: str, *, token=None, form=None, config=None):
    # Only a deployment-controlled current issuer; never send old grants to a new origin.
    try:
        if issuer != manor_oauth_endpoints("manor_pms")["issuer"]:
            raise unavailable()
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            if form is not None:
                response = await client.post(issuer + "/api/resident-webchat/" + path,
                    data=form, auth=(config.client_id, config.client_secret),
                    headers={"Accept": "application/json"})
            else:
                response = await client.get(issuer + "/api/resident-webchat/" + path,
                    headers={"Authorization": "Bearer " + str(token), "Accept": "application/json"})
        if response.status_code in {401, 403}:
            raise authentication_required()
        if response.status_code == 404:
            raise ResidentAccessError(404, "resident_not_found", "Maintenance request not found.")
        if response.status_code != 200:
            raise unavailable()
        data = response.json()
        if not isinstance(data, dict):
            raise unavailable()
        return data
    except (httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, ResidentAccessError):
            raise
        raise unavailable() from exc


async def _oauth_config(db):
    from packages.core.services.oauth_provider_config import resolve_oauth_config
    config = await resolve_oauth_config(db, "manor_pms")
    if not config or not config.client_id or not config.client_secret:
        raise unavailable()
    return config


async def publish_target(db, value: dict, *, user, cc, binding) -> dict:
    """Accept only owner input; authority metadata comes from live provider userinfo."""
    if not isinstance(value, dict) or set(value) - {"enabled", "integration_account_id"} or type(value.get("enabled")) is not bool:
        raise ResidentAccessError(422, "resident_invalid_target", "Choose a PMS connection for this channel.")
    if not value["enabled"]:
        return {"enabled": False}
    if cc.owner_user_id is None and cc.channel_type == "webchat":
        # An external collaborator never takes over a customer webchat; the ones
        # they create pass to the customer when the collaboration ends.
        if getattr(user, "_collaborator_workspace_ids", None) is not None:
            raise ResidentAccessError(403, "resident_invalid_target", "The channel owner must publish the resident connection.")
        cc.owner_user_id = user.id
    if cc.owner_user_id != user.id:
        raise ResidentAccessError(403, "resident_invalid_target", "The channel owner must publish the resident connection.")
    account = await db.get(OAuthAccount, value.get("integration_account_id"))
    if (not account or account.provider != "manor_pms" or account.user_id != user.id
            or not manor_oauth_account_matches(account, entity_id=cc.entity_id)):
        raise ResidentAccessError(403, "resident_invalid_target", "Choose your available PMS OAuth connection.")
    from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens
    from packages.core.services.manor_integration_oauth import resolve_manor_oauth_identity
    tokens = lease_oauth_account_tokens(account, requester_id=user.id, reason="webchat.resident.publish")
    subject, profile = await resolve_manor_oauth_identity(
        "manor_pms", tokens.get("access_token", ""), user_id=user.id, entity_id=cc.entity_id,
    )
    remote = profile.get("manor_oauth", {}).get("remote_entity_id")
    if subject != account.provider_user_id or type(remote) is not int or remote == 0:
        raise ResidentAccessError(403, "resident_invalid_target", "PMS organization could not be verified.")
    previous = ((binding.config or {}).get("resident_pms") or {}) if binding is not None else {}
    keep_version = (previous.get("enabled") is True and previous.get("integration_account_id") == account.id
        and previous.get("published_by") == user.id and previous.get("pms_entity_id") == remote
        and previous.get("account_subject") == subject
        and previous.get("issuer") == manor_oauth_endpoints("manor_pms")["issuer"])
    return {"enabled": True, "integration_account_id": account.id, "published_by": user.id,
            "issuer": manor_oauth_endpoints("manor_pms")["issuer"], "pms_entity_id": remote,
            "account_subject": subject, "version": previous.get("version") if keep_version else secrets.token_hex(16), "display_name": "PMS resident services"}


async def current_target(db, cc, binding, *, verify_remote=True) -> dict:
    from packages.core.services.channel_bindings import channel_workspace_is_routable
    if cc is None or binding is None:
        raise authentication_required()
    target = (binding.config or {}).get("resident_pms", {})
    if not target.get("enabled") or type(target.get("pms_entity_id")) is not int:
        raise authentication_required()
    if (cc.status != "active" or binding.status != "active" or binding.entity_id != cc.entity_id
            or not await channel_workspace_is_routable(db, binding.workspace_id, entity_id=cc.entity_id)):
        raise authentication_required()
    account = await db.get(OAuthAccount, target.get("integration_account_id"), populate_existing=True)
    owner = await db.get(User, cc.owner_user_id, populate_existing=True) if cc.owner_user_id else None
    if (not owner or owner.status != "active" or not account or account.provider != "manor_pms"
            or account.user_id != cc.owner_user_id or account.user_id != target.get("published_by")
            or account.provider_user_id != target.get("account_subject")
            or target.get("issuer") != manor_oauth_endpoints("manor_pms")["issuer"]
            or not manor_oauth_account_matches(account, entity_id=cc.entity_id)):
        raise authentication_required()
    from packages.core.services.actor_authorization import AuthenticatedUserCredential, resolve_current_user_actor
    from packages.core.services.workspace_access import user_can_manage_workspace
    actor = await resolve_current_user_actor(db, AuthenticatedUserCredential(
        user_id=owner.id, entity_id=cc.entity_id, token_version=owner.token_version,
    ))
    if actor is None or not await user_can_manage_workspace(
        db, workspace_id=binding.workspace_id, user_id=owner.id, entity_role=actor.display_role,
    ):
        raise authentication_required()
    if not verify_remote:
        return target
    # A provider-side revocation must also invalidate publication, even before
    # the owner's next health click has updated local OAuth metadata.
    from packages.core.services.oauth_account_credentials import lease_oauth_account_tokens
    from packages.core.services.manor_integration_oauth import resolve_manor_oauth_identity
    try:
        tokens = lease_oauth_account_tokens(account, requester_id=owner.id, reason="webchat.resident.target")
        subject, profile = await resolve_manor_oauth_identity(
            "manor_pms", tokens.get("access_token", ""), user_id=owner.id, entity_id=cc.entity_id,
        )
    except Exception as exc:
        raise unavailable() from exc
    if subject != target.get("account_subject") or profile.get("manor_oauth", {}).get("remote_entity_id") != target["pms_entity_id"]:
        raise authentication_required()
    return target


async def find_binding(db, *, contact_id: str, lock=False):
    query = select(WebchatResidentBinding).where(WebchatResidentBinding.channel_contact_id == contact_id)
    if lock:
        query = query.with_for_update()
    return await db.scalar(query.execution_options(populate_existing=True))


async def _local_binding_context(db, row):
    cc = await db.get(ChannelConfig, row.channel_config_id, populate_existing=True)
    binding = await db.get(Channel, row.channel_binding_id, populate_existing=True)
    contact = await db.get(ChannelContact, row.channel_contact_id, populate_existing=True)
    conversation = await db.get(Conversation, row.conversation_id, populate_existing=True)
    if (not cc or not binding or not contact or contact.status != "active"
            or contact.role != "external" or contact.entity_id != row.entity_id
            or contact.source_id != row.session_id or contact.channel_config_id != row.channel_config_id
            or not conversation or conversation.entity_id != row.entity_id
            or conversation.workspace_id != row.workspace_id
            or binding.workspace_id != row.workspace_id):
        raise authentication_required()
    claimed = contact.user_id or (contact.profile or {}).get("verified_customer_user_id")
    if claimed != row.manor_user_id:
        raise authentication_required()
    if row.manor_user_id:
        visitor = await db.get(User, row.manor_user_id, populate_existing=True)
        if not visitor or visitor.status != "active" or visitor.deleted_at is not None:
            raise authentication_required()
    return cc, binding, contact, conversation


def _target_matches(row, target):
    return (target.get("version") == row.target_version and target.get("issuer") == row.issuer
        and target.get("integration_account_id") == row.target_account_id
        and target.get("pms_entity_id") == row.pms_entity_id)


async def validate_binding(db, row, *, expected_generation=None):
    generation = row.generation if expected_generation is None else expected_generation
    def active():
        if (row.status != "active" or not row.subject or not row.expires_at
                or row.expires_at <= now() or row.generation != generation):
            raise authentication_required()
    active()
    cc, binding, _, _ = await _local_binding_context(db, row)
    target = await current_target(db, cc, binding)
    if not _target_matches(row, target):
        raise authentication_required()
    payload = await _provider_request(row.issuer, "identity", token=_credentials(row).get("access_token"))
    resident = validate_resident_identity(payload, expected_entity=row.pms_entity_id, expected_subject=row.subject)
    if resident["tenant_id"] != row.tenant_id:
        raise authentication_required()
    # HTTP may outlive a local disconnect, ownership change or channel disable.
    await db.refresh(row)
    active()
    cc, binding, _, _ = await _local_binding_context(db, row)
    if not _target_matches(row, await current_target(db, cc, binding, verify_remote=False)):
        raise authentication_required()
    return row


@dataclass(frozen=True)
class ResidentRuntimeScope:
    binding_id: str
    generation: int
    entity_id: str
    conversation_id: str
    contact_id: str
    session_id: str


_runtime_scope: ContextVar[ResidentRuntimeScope | None] = ContextVar("resident_webchat_scope", default=None)


def current_runtime_scope():
    return _runtime_scope.get()


async def private_contact_binding(db, contact):
    row = await find_binding(db, contact_id=contact.id)
    return row, bool((row and row.subject) or (contact.profile or {}).get(PRIVATE_MARKER))


async def require_private_access(db, *, contact, cc, session_id, request, manor_user_id=None):
    row, private = await private_contact_binding(db, contact)
    if not private:
        return None
    if (not row or row.entity_id != cc.entity_id or row.session_id != session_id or request is None
            or row.manor_user_id != manor_user_id):
        raise authentication_required()
    require_proof(row, request.cookies.get(cookie_name(row.conversation_id)))
    await validate_binding(db, row)
    scope = ResidentRuntimeScope(row.id, row.generation, row.entity_id, row.conversation_id, contact.id, session_id)
    request.state.resident_scope = scope
    return scope


async def run_with_scope(scope, awaitable):
    reset = _runtime_scope.set(scope)
    try:
        return await awaitable
    finally:
        _runtime_scope.reset(reset)


async def read_maintenance(db, row, args: ResidentReadRequest):
    generation = row.generation
    await validate_binding(db, row, expected_generation=generation)
    if args.request_id is not None:
        path = "maintenance/" + str(args.request_id)
    else:
        params = {"skip": args.skip, "limit": args.limit}
        if args.status:
            params["status"] = args.status
        path = "maintenance?" + urlencode(params)
    data = await _provider_request(row.issuer, path, token=_credentials(row).get("access_token"))
    await validate_binding(db, row, expected_generation=generation)
    def safe(item):
        try:
            return ResidentMaintenanceRecord.model_validate(item).model_dump()
        except ValueError as exc:
            raise unavailable() from exc
    if args.request_id is not None:
        value = safe(data)
        if value["id"] != args.request_id:
            raise unavailable()
        return value
    if not isinstance(data.get("items"), list) or len(data["items"]) > args.limit:
        raise unavailable()
    return {"items": [safe(item) for item in data["items"]], "skip": args.skip, "limit": args.limit}


async def begin_authorization(db, *, cc, binding, contact, conversation, session_id, user_id, proof, callback_uri):
    import base64
    await db.refresh(contact, with_for_update=True)
    target = await current_target(db, cc, binding)
    config = await _oauth_config(db)
    row = await find_binding(db, contact_id=contact.id, lock=True)
    if row:
        # Scope is immutable after the first verified private response.
        if row.subject:
            require_proof(row, proof)
            if (row.conversation_id != conversation.id or row.target_version != target["version"]
                    or row.target_account_id != target["integration_account_id"] or row.manor_user_id != user_id):
                raise authentication_required()
        proof = proof if proof and digest(proof) == row.browser_proof_hash else secrets.token_urlsafe(32)
    else:
        from packages.core.models.base import generate_ulid
        proof = secrets.token_urlsafe(32)
        row = WebchatResidentBinding(
            id=generate_ulid(), entity_id=cc.entity_id, workspace_id=binding.workspace_id,
            channel_binding_id=binding.id, channel_config_id=cc.id, channel_contact_id=contact.id,
            conversation_id=conversation.id, session_id=session_id, manor_user_id=user_id,
            target_account_id=target["integration_account_id"], target_version=target["version"],
            issuer=target["issuer"], pms_entity_id=target["pms_entity_id"], browser_proof_hash=digest(proof),
            status="pending", generation=0, credential_scheme=get_credential_service().backend,
        )
        db.add(row)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    payload = _credentials(row) if row.credential_ref else {}
    payload.update(code_verifier=verifier, callback_uri=callback_uri)
    row.browser_proof_hash = digest(proof)
    row.state_hash = digest(state)
    row.state_expires_at = now() + timedelta(seconds=120)
    row.generation += 1
    _store(row, payload)
    await db.commit()
    auth = urlsplit(manor_oauth_endpoints("manor_pms")["authorize_url"])
    auth_url = urlunsplit((auth.scheme, auth.netloc, "/resident-webchat/authorize", "", ""))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return row, proof, auth_url + "?" + urlencode({"client_id": config.client_id,
        "redirect_uri": callback_uri, "response_type": "code", "scope": SCOPE,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


def _token_material(tokens):
    return list(dict.fromkeys(value for value in
        [tokens.get("access_token"), *tokens.get("pending_revocations", [])] if value))


async def _revoke_material(db, row, values):
    config = await _oauth_config(db)
    remaining = []
    for value in values:
        try:
            await _provider_request(row.issuer, "revoke", form={"token": value}, config=config)
        except ResidentAccessError:
            remaining.append(value)
    return remaining


async def revoke_tokens(db, row, tokens: dict) -> bool:
    remaining = await _revoke_material(db, row, _token_material(tokens))
    _store(row, {"pending_revocations": remaining})
    return not remaining


async def complete_authorization(db, *, state, proof, callback_uri, code=None, error=None):
    row = await db.scalar(select(WebchatResidentBinding).where(
        WebchatResidentBinding.state_hash == digest(state),
    ).with_for_update().execution_options(populate_existing=True))
    if not row or not row.state_expires_at or row.state_expires_at <= now():
        raise authentication_required()
    require_proof(row, proof(row.conversation_id))
    payload = _credentials(row)
    if payload.get("callback_uri") != callback_uri:
        raise authentication_required()
    row.state_hash = None
    row.state_expires_at = None
    acquired_tokens = []
    try:
        cc, binding, _, _ = await _local_binding_context(db, row)
        if not _target_matches(row, await current_target(db, cc, binding)):
            raise authentication_required()
        if error:
            if error != "access_denied" or code:
                raise authentication_required()
            payload.pop("code_verifier", None)
            payload.pop("callback_uri", None)
            _store(row, payload)
            await db.commit()
            return row, "cancelled"
        if not code:
            raise authentication_required()
        config = await _oauth_config(db)
        response = await _provider_request(row.issuer, "token", config=config, form={
            "grant_type": "authorization_code", "code": code, "redirect_uri": callback_uri,
            "code_verifier": payload.get("code_verifier", ""),
        })
        token = response.get("access_token")
        if not isinstance(token, str) or not token.startswith("pms_res_"):
            raise unavailable()
        # Keep every acquired grant recoverable until replacement is committed.
        acquired_tokens = list(dict.fromkeys([token, *_token_material(payload)]))
        _store(row, {"pending_revocations": acquired_tokens})
        if not isinstance(response.get("token_type"), str) or response["token_type"].lower() != "bearer":
            raise unavailable()
        resident = validate_resident_identity(response, expected_entity=row.pms_entity_id, expected_subject=row.subject)
        live = await _provider_request(row.issuer, "identity", token=token)
        live_resident = validate_resident_identity(live, expected_entity=row.pms_entity_id, expected_subject=resident["sub"])
        if live_resident != resident:
            raise authentication_required()
        old_remaining = await _revoke_material(db, row, [value for value in _token_material(payload) if value != token])
        acquired_tokens = [token, *old_remaining]
        _store(row, {"pending_revocations": acquired_tokens})
        if old_remaining:
            raise ResidentAccessError(502, "resident_revoke_pending", "Previous verification could not be retired. Disconnect and retry.")
        cc, binding, contact, conversation = await _local_binding_context(db, row)
        if not _target_matches(row, await current_target(db, cc, binding)):
            raise authentication_required()
        # Refresh local bindings once more after the owner's remote health check.
        cc, binding, contact, conversation = await _local_binding_context(db, row)
        if not _target_matches(row, await current_target(db, cc, binding, verify_remote=False)):
            raise authentication_required()
        _store(row, {"access_token": token})
        row.subject, row.tenant_id = resident["sub"], resident["tenant_id"]
        row.expires_at = now() + timedelta(seconds=min(response["expires_in"], live["expires_in"]))
        row.status = "active"
        contact.profile = {**(contact.profile or {}), PRIVATE_MARKER: True}
        conversation.meta = {**(conversation.meta or {}), PRIVATE_MARKER: True}
        await db.commit()
        return row, "connected"
    except Exception as exc:
        if acquired_tokens:
            row.status = "revoked"
            # Also runs when either Vault store fails. A failed revoke is kept
            # encrypted for an owner/browser retry whenever storage is writable.
            try:
                remaining = await _revoke_material(db, row, acquired_tokens)
            except ResidentAccessError:
                remaining = acquired_tokens
            try:
                _store(row, {"pending_revocations": remaining})
            except ResidentAccessError:
                pass
        await db.commit()  # Consume rejected callbacks once; never activate them.
        failure = exc if isinstance(exc, ResidentAccessError) else unavailable()
        failure.callback_channel_config_id = row.channel_config_id
        raise failure from exc


async def disconnect(db, row):
    await db.refresh(row, with_for_update=True)
    from packages.core.services import webchat_resident_create
    create_grant = await webchat_resident_create.disable_grant(db, row)
    row.status = "revoked"
    row.generation += 1
    row.state_hash = None
    row.state_expires_at = None
    disabled_generation = row.generation
    await db.commit()  # Local authority ends even if remote revocation fails.
    # A newer authorization may win between commits. Never overwrite its
    # encrypted material with cleanup from the old generation.
    await db.refresh(row, with_for_update=True)
    if row.generation != disabled_generation or row.status != "revoked":
        await db.rollback()
        raise ResidentAccessError(502, "resident_revoke_pending", "Authorization changed during cleanup. Retry disconnect.")
    create_grant = await webchat_resident_create.grant_for(db, row.id, lock=True)
    try:
        confirmed = await revoke_tokens(db, row, _credentials(row))
    except ResidentAccessError:
        confirmed = False
    create_confirmed = await webchat_resident_create.revoke_grant(db, create_grant)
    await db.commit()  # Keep both locks until both cleanup results are durable.
    if not confirmed or not create_confirmed:
        raise ResidentAccessError(502, "resident_revoke_pending", "Disconnected locally; provider revocation is unconfirmed. Retry disconnect.")


async def guard_stream(scope, stream):
    """Private text is released only after fresh authorization, never into a reset chat."""
    if scope is None:
        async for part in stream:
            yield part
        return
    from packages.core.database import async_session
    reset = _runtime_scope.set(scope)
    try:
        # The model may contain private context even before selecting a tool.
        # Buffer this private answer, then recheck authority before publication.
        chunks = []
        async for part in stream:
            chunks.append(part)
        async with async_session() as db:
            row = await db.get(WebchatResidentBinding, scope.binding_id)
            if not row:
                raise authentication_required()
            await validate_binding(db, row, expected_generation=scope.generation)
        for part in chunks:
            yield part
    except ResidentAccessError:
        from packages.core.services.sse_events import format_sse
        yield format_sse("stream_end", {"status": "error", "stop_reason": "error",
            "error": "Resident verification expired. Verify again to continue."})
    finally:
        _runtime_scope.reset(reset)


async def authorize_private_persistence(db, conversation_id):
    scope = current_runtime_scope()
    if scope is None:
        return
    if conversation_id != scope.conversation_id:
        raise authentication_required()
    row = await db.scalar(select(WebchatResidentBinding).where(
        WebchatResidentBinding.id == scope.binding_id,
    ).with_for_update().execution_options(populate_existing=True))
    if not row:
        raise authentication_required()
    await validate_binding(db, row, expected_generation=scope.generation)


async def authorize_publication(db, scope):
    if scope is None:
        return
    row = await db.get(WebchatResidentBinding, scope.binding_id, populate_existing=True)
    if not row:
        raise authentication_required()
    await validate_binding(db, row, expected_generation=scope.generation)
