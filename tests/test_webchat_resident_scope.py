"""Resident Webchat scope: no model or external provider is used by these tests."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest


def identity(**changes):
    payload = {
        "scope": "pms:maintenance:self:read",
        "expires_in": 240,
        "resident": {"sub": "pms-resident:-1:42", "tenant_id": 42, "entity_id": -1},
    }
    payload.update(changes)
    return payload


@pytest.mark.parametrize(
    "resident",
    [
        {"sub": "pms-resident:-2:42", "tenant_id": 42, "entity_id": -2},
        {"sub": "pms-resident:-1:42", "tenant_id": True, "entity_id": -1},
        {"sub": "pms-resident:-1:42", "tenant_id": "42", "entity_id": -1},
        {"sub": "pms-resident:-1:42", "tenant_id": 42, "entity_id": "-1"},
        {"sub": "", "tenant_id": 42, "entity_id": -1},
    ],
)
def test_untrusted_or_wrong_resident_identity_is_rejected(resident):
    from packages.core.services.webchat_resident import validate_resident_identity, ResidentAccessError

    with pytest.raises(ResidentAccessError):
        validate_resident_identity(identity(resident=resident), expected_entity=-1)


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": "pms:read pms:write"},
        {"expires_in": 301},
        {"expires_in": 0},
        {"expires_in": True},
        {"expires_in": "240"},
    ],
)
def test_wrong_scope_or_lifetime_is_rejected(changes):
    from packages.core.services.webchat_resident import validate_resident_identity, ResidentAccessError

    with pytest.raises(ResidentAccessError):
        validate_resident_identity(identity(**changes), expected_entity=-1)


def test_same_resident_is_required_for_private_history_reauthorization():
    from packages.core.services.webchat_resident import validate_resident_identity, ResidentAccessError

    assert validate_resident_identity(identity(), expected_entity=-1)["tenant_id"] == 42
    with pytest.raises(ResidentAccessError):
        validate_resident_identity(identity(), expected_entity=-1, expected_subject="pms-resident:-1:43")


def test_token_is_separate_encrypted_credential_not_oauth_account():
    from packages.core.models.webchat_resident import WebchatResidentBinding
    from packages.core.credentials import CredentialService

    assert WebchatResidentBinding.__tablename__ != "oauth_accounts"
    assert "access_token" not in WebchatResidentBinding.__table__.columns
    assert "credential_ref" in WebchatResidentBinding.__table__.columns
    assert hasattr(CredentialService, "store_webchat_resident")
    assert hasattr(CredentialService, "lease_webchat_resident")


@pytest.mark.parametrize(
    "args",
    [
        {"tenant_id": 43},
        {"entity_id": -2},
        {"integration_account_id": "other"},
        {"url": "http://attacker.test"},
        {"request_id": 1, "status": "open"},
        {"limit": 51},
        {"skip": -1},
    ],
)
def test_narrow_read_does_not_accept_authority_or_bad_pagination(args):
    from packages.core.services.webchat_resident import ResidentReadRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResidentReadRequest.model_validate(args)


def test_runtime_tool_is_registered_but_has_no_authority_selectors():
    from packages.core.ai.tools.resident_maintenance_tools import get_tools

    schema, handler = get_tools()[0]
    assert schema["function"]["name"] == "resident_maintenance_status"
    fields = set(schema["function"]["parameters"]["properties"])
    assert fields == {"request_id", "status", "skip", "limit"}
    assert schema["function"]["parameters"]["additionalProperties"] is False


async def test_runtime_tool_refuses_without_request_local_browser_scope():
    from packages.core.ai.tools.resident_maintenance_tools import get_tools

    _, handler = get_tools()[0]
    result = await handler(entity_id="business", request_id=1)
    import json

    assert json.loads(result)["error"] == "resident_auth_required"


@pytest.fixture
async def resident_case(db_session, monkeypatch):
    from cryptography.fernet import Fernet
    from packages.core.credentials import CredentialService
    from packages.core.credentials.dev_provider import DevKeyProvider
    from packages.core.models.base import generate_ulid
    from packages.core.models.channel import ChannelConfig, ChannelContact
    from packages.core.models.document import Channel
    from packages.core.models.task import Conversation
    from packages.core.models.user import User, Entity
    from packages.core.governance.approval_policy_service import account_defaults, initialize_workspace_approval
    from packages.core.models.workspace import Workspace
    from packages.core.services import webchat_resident as svc

    db = db_session
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from packages.core import database

    monkeypatch.setattr(database, "async_session", async_sessionmaker(db.bind, expire_on_commit=False))
    eid, wid, uid, ccid, bid, cid, convid = [generate_ulid() for _ in range(7)]
    user = User(
        id=uid, entity_id=eid, email=f"{uid}@example.test", password_hash="unused", role="owner", status="active"
    )
    ws = Workspace(id=wid, entity_id=eid, name="Resident test")
    cc = ChannelConfig(
        id=ccid,
        entity_id=eid,
        workspace_id=wid,
        owner_user_id=uid,
        channel_type="webchat",
        provider="webchat",
        status="active",
        config={"public_token": "entry"},
    )
    target = {
        "enabled": True,
        "integration_account_id": generate_ulid(),
        "published_by": uid,
        "issuer": "https://pms.example.test",
        "pms_entity_id": -1,
        "account_subject": "owner",
        "version": "one",
    }
    binding = Channel(
        id=bid,
        entity_id=eid,
        workspace_id=wid,
        user_id=uid,
        type="webchat",
        status="active",
        config={"channel_config_id": ccid, "resident_pms": target},
    )
    contact = ChannelContact(
        id=cid,
        entity_id=eid,
        channel_config_id=ccid,
        channel_type="webchat",
        source_id="session",
        profile={},
        status="active",
        role="external",
    )
    conv = Conversation(id=convid, entity_id=eid, workspace_id=wid, title="Resident test", meta={})
    db.add_all([Entity(id=eid, name="Resident test", settings={"approval_defaults": account_defaults()}), user, ws, cc, binding, contact, conv])
    await db.flush()
    await initialize_workspace_approval(db, ws)
    await db.commit()
    monkeypatch.setenv("DEV_CREDENTIAL_KEY", Fernet.generate_key().decode())
    creds = CredentialService(DevKeyProvider(key=Fernet.generate_key().decode()))
    monkeypatch.setattr(svc, "get_credential_service", lambda: creds)
    from unittest.mock import AsyncMock

    real_target = svc.current_target
    target_mock = AsyncMock(return_value=target)
    monkeypatch.setattr(svc, "current_target", target_mock)
    monkeypatch.setattr(
        svc, "_oauth_config", AsyncMock(return_value=SimpleNamespace(client_id="client", client_secret="test-only"))
    )
    monkeypatch.setattr(
        svc,
        "manor_oauth_endpoints",
        lambda provider: {"issuer": target["issuer"], "authorize_url": "https://pms.example.test/oauth/authorize"},
    )
    calls = []

    async def provider(issuer, path, **kwargs):
        calls.append((path, kwargs))
        if path == "token":
            return {**identity(), "access_token": "pms_res_fixture", "token_type": "Bearer"}
        if path == "identity":
            return identity()
        if path == "revoke":
            return {}
        record = {
            "id": 1,
            "title": "Own repair",
            "description": "Synthetic",
            "category": "plumbing",
            "priority": "medium",
            "status": "open",
            "created_at": None,
            "updated_at": None,
            "completed_at": None,
            "tenant_email": "must not leave provider",
        }
        return record if path == "maintenance/1" else {"items": [record]}

    provider_mock = AsyncMock(side_effect=provider)
    monkeypatch.setattr(svc, "_provider_request", provider_mock)
    return SimpleNamespace(
        db=db,
        svc=svc,
        cc=cc,
        binding=binding,
        contact=contact,
        conv=conv,
        user=user,
        ws=ws,
        target=target,
        provider=provider_mock,
        calls=calls,
        creds=creds,
        target_mock=target_mock,
        real_target=real_target,
    )


async def authorize(case):
    from urllib.parse import urlsplit, parse_qs

    row, proof, url = await case.svc.begin_authorization(
        case.db,
        cc=case.cc,
        binding=case.binding,
        contact=case.contact,
        conversation=case.conv,
        session_id="session",
        user_id=None,
        proof=None,
        callback_uri="https://manor.example.test/callback",
    )
    state = parse_qs(urlsplit(url).query)["state"][0]
    row, outcome = await case.svc.complete_authorization(
        case.db, state=state, proof=lambda _: proof, callback_uri="https://manor.example.test/callback", code="one-code"
    )
    assert outcome == "connected"
    return row, proof, state


async def test_authorization_encrypts_token_and_marks_history_private(resident_case):
    c = resident_case
    row, proof, state = await authorize(c)
    assert row.subject == "pms-resident:-1:42" and row.tenant_id == 42 and row.pms_entity_id == -1
    assert row.credential_ref and "pms_res_fixture" not in row.credential_ref
    assert row.state_hash is None
    assert c.contact.profile["resident_private"] is True and c.conv.meta["resident_private"] is True
    assert row.browser_proof_hash != proof
    from packages.core.credentials import Requester

    assert (
        c.creds.lease_webchat_resident(row, requester=Requester(kind="test", id="test"), reason="test")["access_token"]
        == "pms_res_fixture"
    )
    row.conversation_id = "wrong-context"
    with pytest.raises(Exception):
        c.creds.lease_webchat_resident(row, requester=Requester(kind="test", id="test"), reason="test")


async def test_callback_replay_never_exchanges_twice(resident_case):
    c = resident_case
    row, proof, state = await authorize(c)
    with pytest.raises(c.svc.ResidentAccessError):
        await c.svc.complete_authorization(
            c.db,
            state=state,
            proof=lambda _: proof,
            callback_uri="https://manor.example.test/callback",
            code="one-code",
        )
    assert len([x for x in c.calls if x[0] == "token"]) == 1


@pytest.mark.parametrize("bad", ["wrong-proof", "missing-proof", "wrong-callback"])
async def test_callback_requires_same_browser_and_callback(resident_case, bad):
    from urllib.parse import urlsplit, parse_qs

    c = resident_case
    row, proof, url = await c.svc.begin_authorization(
        c.db,
        cc=c.cc,
        binding=c.binding,
        contact=c.contact,
        conversation=c.conv,
        session_id="session",
        user_id=None,
        proof=None,
        callback_uri="https://manor.example.test/callback",
    )
    state = parse_qs(urlsplit(url).query)["state"][0]
    with pytest.raises(c.svc.ResidentAccessError):
        await c.svc.complete_authorization(
            c.db,
            state=state,
            proof=lambda _: None if bad == "missing-proof" else "other" if bad == "wrong-proof" else proof,
            callback_uri="https://evil.example.test/callback"
            if bad == "wrong-callback"
            else "https://manor.example.test/callback",
            code="one-code",
        )
    assert not any(x[0] == "token" for x in c.calls)


async def test_cancel_consumes_state_without_token_or_private_history(resident_case):
    from urllib.parse import urlsplit, parse_qs

    c = resident_case
    row, proof, url = await c.svc.begin_authorization(
        c.db,
        cc=c.cc,
        binding=c.binding,
        contact=c.contact,
        conversation=c.conv,
        session_id="session",
        user_id=None,
        proof=None,
        callback_uri="https://manor.example.test/callback",
    )
    state = parse_qs(urlsplit(url).query)["state"][0]
    _, outcome = await c.svc.complete_authorization(
        c.db,
        state=state,
        proof=lambda _: proof,
        callback_uri="https://manor.example.test/callback",
        error="access_denied",
    )
    assert outcome == "cancelled" and row.state_hash is None and not c.contact.profile.get("resident_private")
    assert not any(x[0] == "token" for x in c.calls)


@pytest.mark.parametrize(
    "failure", ["expired", "revoked", "parent_logout", "generation", "owner_target_change", "blocked_contact"]
)
async def test_private_access_revalidates_every_lifecycle_boundary(resident_case, failure):
    c = resident_case
    row, proof, state = await authorize(c)
    generation = row.generation
    if failure == "expired":
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    if failure == "revoked":
        row.status = "revoked"
    if failure == "generation":
        row.generation += 1
    if failure == "owner_target_change":
        c.target["version"] = "different"
    if failure == "blocked_contact":
        c.contact.status = "blocked"
    if failure == "parent_logout":
        c.provider.side_effect = c.svc.authentication_required()
    await c.db.commit()
    with pytest.raises(c.svc.ResidentAccessError):
        await c.svc.validate_binding(c.db, row, expected_generation=generation)


async def test_private_history_requires_cookie_even_with_known_session_id(resident_case):
    c = resident_case
    row, proof, state = await authorize(c)
    request = SimpleNamespace(cookies={}, state=SimpleNamespace())
    with pytest.raises(c.svc.ResidentAccessError):
        await c.svc.require_private_access(c.db, contact=c.contact, cc=c.cc, session_id="session", request=request)
    request.cookies[c.svc.cookie_name(c.conv.id)] = proof
    assert await c.svc.require_private_access(c.db, contact=c.contact, cc=c.cc, session_id="session", request=request)


async def test_disconnected_tombstone_never_restores_anonymous_history(resident_case):
    c = resident_case
    row, proof, state = await authorize(c)
    await c.svc.disconnect(c.db, row)
    assert row.subject and row.status == "revoked" and c.contact.profile["resident_private"]
    with pytest.raises(c.svc.ResidentAccessError):
        await c.svc.require_private_access(
            c.db,
            contact=c.contact,
            cc=c.cc,
            session_id="session",
            request=SimpleNamespace(cookies={c.svc.cookie_name(c.conv.id): proof}, state=SimpleNamespace()),
        )
    await c.svc.disconnect(c.db, row)  # expired/revoked is safely retryable


async def test_foreign_resident_cannot_reauthorize_same_private_conversation(resident_case):
    from urllib.parse import urlsplit, parse_qs

    c = resident_case
    row, proof, state = await authorize(c)
    _, _, url = await c.svc.begin_authorization(
        c.db,
        cc=c.cc,
        binding=c.binding,
        contact=c.contact,
        conversation=c.conv,
        session_id="session",
        user_id=None,
        proof=proof,
        callback_uri="https://manor.example.test/callback",
    )
    old_side = c.provider.side_effect

    async def other(issuer, path, **kwargs):
        data = await old_side(issuer, path, **kwargs)
        if path in {"identity", "token"}:
            data["resident"] = {"sub": "pms-resident:-1:43", "tenant_id": 43, "entity_id": -1}
        return data

    c.provider.side_effect = other
    with pytest.raises(c.svc.ResidentAccessError) as error:
        await c.svc.complete_authorization(
            c.db,
            state=parse_qs(urlsplit(url).query)["state"][0],
            proof=lambda _: proof,
            callback_uri="https://manor.example.test/callback",
            code="other-code",
        )
    assert error.value.code == "resident_identity_mismatch" and row.subject == "pms-resident:-1:42"
    assert any(x[0] == "revoke" for x in c.calls)


async def test_safe_record_projection_drops_private_fields_and_rejects_objects(resident_case):
    c = resident_case
    row, proof, state = await authorize(c)
    result = await c.svc.read_maintenance(c.db, row, c.svc.ResidentReadRequest(request_id=1))
    assert "tenant_email" not in result and result["title"] == "Own repair"
    old_side = c.provider.side_effect

    async def malformed(issuer, path, **kwargs):
        data = await old_side(issuer, path, **kwargs)
        if path == "maintenance/1":
            data["title"] = {"nested_private": "no"}
        return data

    c.provider.side_effect = malformed
    with pytest.raises(c.svc.ResidentAccessError):
        await c.svc.read_maintenance(c.db, row, c.svc.ResidentReadRequest(request_id=1))


async def test_persistence_fence_rejects_late_private_answer(resident_case):
    c = resident_case
    row, proof, state = await authorize(c)
    scope = c.svc.ResidentRuntimeScope(
        row.id, row.generation, row.entity_id, row.conversation_id, row.channel_contact_id, row.session_id
    )
    row.generation += 1
    await c.db.commit()
    from packages.core.services.conversation_messages import add_message

    with pytest.raises(c.svc.ResidentAccessError):
        await c.svc.run_with_scope(scope, add_message(c.db, c.conv.id, role="assistant", content="must not persist"))
    from packages.core.models.task import Message
    from sqlalchemy import select

    assert await c.db.scalar(select(Message.id).where(Message.conversation_id == c.conv.id)) is None


async def test_workspace_purge_removes_private_grant_and_only_own_rows(resident_case):
    c = resident_case
    row, proof, state = await authorize(c)
    from packages.core.services.entity_service import purge_workspace
    from packages.core.models.webchat_resident import WebchatResidentBinding

    c.ws.deleted_at = datetime.now(timezone.utc) - timedelta(days=31)
    await c.db.commit()
    assert await purge_workspace(c.db, c.ws.id)
    assert await c.db.get(WebchatResidentBinding, row.id) is None


def test_blueprint_export_removes_private_binding_metadata():
    from packages.core.blueprints.exporter import _without_nonportable_references

    data = {
        "name": "public",
        "resident_pms": {"integration_account_id": "private-account"},
        "nested": {"resident_private": True, "resident_binding_id": "private-binding", "safe": "ok"},
    }
    assert _without_nonportable_references(data) == {"name": "public", "nested": {"safe": "ok"}}


@pytest.mark.parametrize("boundary", ["read_response", "identity_response"])
async def test_late_disconnect_discards_remote_result(resident_case, boundary):
    c = resident_case
    row, proof, _ = await authorize(c)
    original = c.provider.side_effect

    async def delayed(issuer, path, **kwargs):
        result = await original(issuer, path, **kwargs)
        if (boundary == "read_response" and path.startswith("maintenance")) or (
            boundary == "identity_response" and path == "identity"
        ):
            row.generation += 1
            row.status = "revoked"
            await c.db.commit()
        return result

    c.provider.side_effect = delayed
    with pytest.raises(c.svc.ResidentAccessError):
        await c.svc.read_maintenance(c.db, row, c.svc.ResidentReadRequest())


@pytest.mark.parametrize("store_number", [1, 2, 3])
async def test_callback_vault_failure_revokes_new_token(resident_case, monkeypatch, store_number):
    from urllib.parse import parse_qs, urlsplit

    c = resident_case
    row, proof, url = await c.svc.begin_authorization(
        c.db,
        cc=c.cc,
        binding=c.binding,
        contact=c.contact,
        conversation=c.conv,
        session_id="session",
        user_id=None,
        proof=None,
        callback_uri="https://manor.example.test/callback",
    )
    original = c.svc._store
    calls = 0

    def failing_store(row, payload):
        nonlocal calls
        calls += 1
        if calls == store_number:
            raise c.svc.unavailable()
        original(row, payload)

    monkeypatch.setattr(c.svc, "_store", failing_store)
    with pytest.raises(c.svc.ResidentAccessError):
        await c.svc.complete_authorization(
            c.db,
            state=parse_qs(urlsplit(url).query)["state"][0],
            proof=lambda _: proof,
            callback_uri="https://manor.example.test/callback",
            code="one-code",
        )
    assert row.status == "revoked" and row.state_hash is None
    assert any(path == "revoke" and data["form"]["token"] == "pms_res_fixture" for path, data in c.calls)
    assert not c.contact.profile.get("resident_private")


async def test_callback_failed_revoke_preserves_encrypted_cleanup_for_retry(resident_case):
    from urllib.parse import parse_qs, urlsplit

    c = resident_case
    row, proof, url = await c.svc.begin_authorization(
        c.db,
        cc=c.cc,
        binding=c.binding,
        contact=c.contact,
        conversation=c.conv,
        session_id="session",
        user_id=None,
        proof=None,
        callback_uri="https://manor.example.test/callback",
    )
    original = c.provider.side_effect

    async def failing(issuer, path, **kwargs):
        if path in {"identity", "revoke"}:
            raise c.svc.unavailable()
        return await original(issuer, path, **kwargs)

    c.provider.side_effect = failing
    with pytest.raises(c.svc.ResidentAccessError):
        await c.svc.complete_authorization(
            c.db,
            state=parse_qs(urlsplit(url).query)["state"][0],
            proof=lambda _: proof,
            callback_uri="https://manor.example.test/callback",
            code="one-code",
        )
    assert row.status == "revoked"
    assert c.svc._credentials(row) == {"pending_revocations": ["pms_res_fixture"]}
    c.provider.side_effect = original
    await c.svc.disconnect(c.db, row)
    assert c.svc._credentials(row) == {"pending_revocations": []}


async def test_real_toolpool_executes_only_verified_resident_scope(resident_case):
    import json
    from packages.core.ai.tool_pool import ToolPool
    from packages.core.ai.tools.resident_maintenance_tools import get_tools
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import resolve_runtime_principal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface

    c = resident_case
    row, proof, _ = await authorize(c)
    scope = c.svc.ResidentRuntimeScope(
        row.id, row.generation, row.entity_id, row.conversation_id, row.channel_contact_id, row.session_id
    )
    principal = resolve_runtime_principal(
        surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
        entity_id=row.entity_id,
        channel_context={
            "channel_type": "webchat",
            "source_id": row.session_id,
            "channel_contact_id": row.channel_contact_id,
        },
    )
    envelope = RuntimeEnvelope(
        surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
        principal=principal,
        profile=RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
        entity_id=row.entity_id,
        workspace_id=row.workspace_id,
        conversation_id=row.conversation_id,
        allowed_tool_names=(c.svc.TOOL_NAME,),
    )
    pool = ToolPool()
    schema, handler = get_tools()[0]
    pool.register(c.svc.TOOL_NAME, schema, handler, discoverable=False)
    result = await c.svc.run_with_scope(
        scope,
        pool.execute(
            c.svc.TOOL_NAME,
            {"request_id": 1, "_runtime_run_id_from_context": "resident-read",
             "_runtime_tool_call_id_from_context": "verified-request"},
            entity_id=row.entity_id,
            workspace_id=row.workspace_id,
            conversation_id=row.conversation_id,
            runtime_envelope=envelope,
        ),
    )
    assert "error" not in json.loads(result), result
    assert json.loads(result)["title"] == "Own repair"
    result = await pool.execute(
        c.svc.TOOL_NAME,
        {"request_id": 1},
        entity_id=row.entity_id,
        workspace_id=row.workspace_id,
        conversation_id=row.conversation_id,
        runtime_envelope=envelope,
    )
    assert json.loads(result)["error"] == "resident_auth_required"


@pytest.mark.parametrize(
    "mutation", ["membership_revoked", "owner_changed", "channel_disabled", "workspace_trashed", "oauth_disconnected"]
)
async def test_target_rechecks_current_owner_authority(resident_case, monkeypatch, mutation):
    from packages.core.models.user import OAuthAccount, UserMembership
    from packages.core.services import manor_integration_oauth as oauth
    from packages.core.services import oauth_account_credentials
    from unittest.mock import AsyncMock

    c = resident_case
    # Restore the real target resolver; only the external provider is simulated.
    monkeypatch.setattr(c.svc, "current_target", c.real_target)
    monkeypatch.setattr(oauth, "manor_oauth_endpoints", lambda _: {"issuer": c.target["issuer"]})
    monkeypatch.setattr(oauth, "manor_mcp_endpoint", lambda _: c.target["issuer"] + "/api/mcp")
    monkeypatch.setattr(
        oauth_account_credentials, "lease_oauth_account_tokens", lambda *args, **kw: {"access_token": "test-only"}
    )
    monkeypatch.setattr(
        oauth,
        "resolve_manor_oauth_identity",
        AsyncMock(return_value=("owner", {"manor_oauth": {"remote_entity_id": -1}})),
    )
    account = OAuthAccount(
        id=c.target["integration_account_id"],
        user_id=c.user.id,
        provider="manor_pms",
        provider_user_id="owner",
        profile={
            "manor_oauth": {
                "version": 1,
                "user_id": c.user.id,
                "entity_id": c.user.entity_id,
                "subject": "owner",
                "issuer": c.target["issuer"],
                "mcp_url": c.target["issuer"] + "/api/mcp",
            }
        },
    )
    c.db.add(account)
    await c.db.commit()
    assert await c.svc.current_target(c.db, c.cc, c.binding)
    if mutation == "membership_revoked":
        c.db.add(UserMembership(user_id=c.user.id, entity_id=c.user.entity_id, role="owner", status="inactive"))
    elif mutation == "owner_changed":
        c.cc.owner_user_id = None
    elif mutation == "channel_disabled":
        c.cc.status = "disabled"
    elif mutation == "workspace_trashed":
        c.ws.deleted_at = datetime.now(timezone.utc)
    else:
        account.profile = {**account.profile, "oauth_disconnect": {"pending": True}}
    await c.db.commit()
    with pytest.raises(c.svc.ResidentAccessError):
        await c.svc.current_target(c.db, c.cc, c.binding)


@pytest.fixture
async def resident_http(resident_case, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from unittest.mock import AsyncMock
    from apps.api.routers import public_chat, public_pms_resident
    from packages.core.database import get_db

    c = resident_case
    row, proof, state = await authorize(c)
    app = FastAPI()
    app.include_router(public_chat.router)
    app.include_router(public_pms_resident.router)

    async def db():
        yield c.db

    app.dependency_overrides[get_db] = db
    monkeypatch.setattr(public_chat, "_resolve_channel_by_token", AsyncMock(return_value=(c.cc, c.binding)))
    monkeypatch.setattr(public_chat, "_require_chat_access", AsyncMock(return_value=None))
    monkeypatch.setattr(public_chat, "find_public_webchat_conversation_by_session", AsyncMock(return_value=c.conv))
    monkeypatch.setattr(public_chat, "find_channel_session_contact", AsyncMock(return_value=c.contact))
    monkeypatch.setattr(public_chat, "find_claimed_webchat_contact_for_user", AsyncMock(return_value=None))
    monkeypatch.setattr(public_chat, "enforce_public_audio_budget", AsyncMock())
    history = AsyncMock(return_value=[{"id": "reply", "role": "assistant", "content": "Own private repair"}])
    monkeypatch.setattr(public_chat, "list_public_webchat_messages", history)
    monkeypatch.setattr(public_pms_resident, "callback_url", lambda _: "https://manor.example.test/callback")
    monkeypatch.setattr(
        public_pms_resident._start_limiter, "check", AsyncMock(return_value=SimpleNamespace(allowed=True))
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://manor.example.test") as client:
        yield SimpleNamespace(c=c, row=row, proof=proof, state=state, client=client, history=history)


@pytest.mark.parametrize(
    "endpoint",
    [
        "messages",
        "session",
        "message",
        "message/stream",
        "audio/speech",
        "audio/transcribe",
        "resident",
        "resident/maintenance",
    ],
)
async def test_http_all_private_entrypoints_reject_known_session_without_proof(resident_http, endpoint):
    h = resident_http
    if endpoint in {"messages", "resident", "resident/maintenance"}:
        response = await h.client.get("/api/v1/public/chat/entry/" + endpoint, params={"session_id": "session"})
    elif endpoint == "audio/transcribe":
        response = await h.client.post(
            "/api/v1/public/chat/entry/" + endpoint,
            data={"session_id": "session"},
            files={"file": ("audio.wav", b"fake", "audio/wav")},
        )
    elif endpoint == "message/stream":
        response = await h.client.post(
            "/api/v1/public/chat/entry/" + endpoint, data={"session_id": "session", "message": "private status"}
        )
    else:
        body = {"session_id": "session"}
        if endpoint == "message":
            body["text"] = "private status"
        if endpoint == "audio/speech":
            body["message_id"] = "reply"
        response = await h.client.post("/api/v1/public/chat/entry/" + endpoint, json=body)
    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "resident_auth_required"
    assert "no-store" in response.headers["cache-control"]
    h.history.assert_not_awaited()


async def test_http_tombstone_survives_removed_contact_marker(resident_http):
    h = resident_http
    h.c.contact.profile = {}
    h.c.conv.meta = {}
    await h.c.db.commit()
    response = await h.client.get("/api/v1/public/chat/entry/messages", params={"session_id": "session"})
    assert response.status_code == 401
    h.history.assert_not_awaited()
    h.client.cookies.set(h.c.svc.cookie_name(h.c.conv.id), h.proof)
    response = await h.client.get("/api/v1/public/chat/entry/messages", params={"session_id": "session"})
    assert response.status_code == 200 and response.json()["messages"][0]["content"] == "Own private repair"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"


async def test_http_expired_disconnect_and_cross_site_request(resident_http):
    h = resident_http
    h.row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await h.c.db.commit()
    h.client.cookies.set(h.c.svc.cookie_name(h.c.conv.id), h.proof)
    response = await h.client.request(
        "DELETE",
        "/api/v1/public/chat/entry/resident",
        json={"session_id": "session"},
        headers={"Origin": "https://evil.example.test"},
    )
    assert response.status_code == 403 and h.row.status == "active"
    response = await h.client.request(
        "DELETE",
        "/api/v1/public/chat/entry/resident",
        json={"session_id": "session"},
        headers={"Origin": "https://manor.example.test"},
    )
    assert response.status_code == 204
    assert h.row.status == "revoked" and h.row.subject
    response = await h.client.get("/api/v1/public/chat/entry/messages", params={"session_id": "session"})
    assert response.status_code == 401


async def test_http_callback_provider_failure_returns_to_fixed_chat_without_code(resident_http):
    from urllib.parse import urlsplit, parse_qs

    h = resident_http
    row, proof, url = await h.c.svc.begin_authorization(
        h.c.db,
        cc=h.c.cc,
        binding=h.c.binding,
        contact=h.c.contact,
        conversation=h.c.conv,
        session_id="session",
        user_id=None,
        proof=h.proof,
        callback_uri="https://manor.example.test/callback",
    )
    h.client.cookies.set(h.c.svc.cookie_name(h.c.conv.id), proof)
    h.c.provider.side_effect = h.c.svc.unavailable()
    response = await h.client.get(
        "/api/v1/public/pms-resident/callback",
        params={"state": parse_qs(urlsplit(url).query)["state"][0], "code": "synthetic-code"},
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"].endswith("/chat/public/entry?resident=error")
    assert "synthetic-code" not in response.headers["location"]
    assert row.state_hash is None
    response = await h.client.get(
        "/api/v1/public/pms-resident/callback", params={"state": "invalid-state-123456", "code": "synthetic-code"}
    )
    assert response.status_code == 401 and "location" not in response.headers


@pytest.mark.parametrize(
    "query",
    [
        "session_id=session&tenant_id=43",
        "session_id=session&limit=1&limit=50",
        "session_id=session&url=https://evil.example.test",
    ],
)
async def test_http_rejects_selectors_and_duplicate_query(resident_http, query):
    h = resident_http
    h.client.cookies.set(h.c.svc.cookie_name(h.c.conv.id), h.proof)
    response = await h.client.get("/api/v1/public/chat/entry/resident/maintenance?" + query)
    assert response.status_code == 400
    assert "no-store" in response.headers["cache-control"]


async def test_http_unknown_session_is_reported_as_missing_not_unavailable(resident_http, monkeypatch):
    from unittest.mock import AsyncMock
    from apps.api.routers import public_chat

    monkeypatch.setattr(public_chat, "find_public_webchat_conversation_by_session", AsyncMock(return_value=None))
    # The widget starts a fresh session on this code instead of retrying forever.
    response = await resident_http.client.get("/api/v1/public/chat/entry/resident?session_id=never-created")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "chat_session_not_found"
    assert "no-store" in response.headers["cache-control"]


async def test_scoped_sse_does_not_release_a_late_private_answer(resident_case):
    c = resident_case
    row, proof, _ = await authorize(c)
    scope = c.svc.ResidentRuntimeScope(
        row.id, row.generation, row.entity_id, row.conversation_id, row.channel_contact_id, row.session_id
    )

    async def stream():
        yield 'event: text_delta\ndata: {"content":"private answer"}\n\n'
        row.status = "revoked"
        row.generation += 1
        await c.db.commit()
        yield 'event: stream_end\ndata: {"persisted":true}\n\n'

    result = "".join([chunk async for chunk in c.svc.guard_stream(scope, stream())])
    assert "private answer" not in result
    assert "Verify again" in result
    assert '"persisted":false' not in result and '"persisted": false' not in result
    assert c.svc.current_runtime_scope() is None


async def test_concurrent_callback_consumes_code_once(resident_case):
    import asyncio
    from urllib.parse import urlsplit, parse_qs
    from sqlalchemy.ext.asyncio import async_sessionmaker

    c = resident_case
    row, proof, url = await c.svc.begin_authorization(
        c.db,
        cc=c.cc,
        binding=c.binding,
        contact=c.contact,
        conversation=c.conv,
        session_id="session",
        user_id=None,
        proof=None,
        callback_uri="https://manor.example.test/callback",
    )
    state = parse_qs(urlsplit(url).query)["state"][0]
    factory = async_sessionmaker(c.db.bind, expire_on_commit=False)
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.provider.side_effect
    token_calls = 0

    async def delayed(issuer, path, **kwargs):
        nonlocal token_calls
        if path == "token":
            token_calls += 1
            entered.set()
            await release.wait()
        return await original(issuer, path, **kwargs)

    c.provider.side_effect = delayed

    async def attempt():
        async with factory() as db:
            return await c.svc.complete_authorization(
                db, state=state, proof=lambda _: proof, callback_uri="https://manor.example.test/callback", code="once"
            )

    first = asyncio.create_task(attempt())
    await asyncio.wait_for(entered.wait(), 3)
    second = asyncio.create_task(attempt())
    await asyncio.sleep(0.05)
    assert not second.done() and token_calls == 1
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 5)
    assert sum(isinstance(value, tuple) and value[1] == "connected" for value in results) == 1
    assert sum(isinstance(value, c.svc.ResidentAccessError) for value in results) == 1
    assert token_calls == 1


async def test_stream_placeholder_cannot_be_filled_after_disconnect(resident_case):
    from packages.core.services.conversation_messages import (
        create_assistant_stream_placeholder,
        save_or_update_assistant_stream_message,
    )

    c = resident_case
    row, proof, _ = await authorize(c)
    placeholder = await create_assistant_stream_placeholder(
        c.db, c.conv.id, entity_id=c.cc.entity_id, workspace_id=c.ws.id, agent_id=None
    )
    await c.db.commit()
    original_content = placeholder.content
    scope = c.svc.ResidentRuntimeScope(
        row.id, row.generation, row.entity_id, row.conversation_id, row.channel_contact_id, row.session_id
    )
    await c.svc.disconnect(c.db, row)
    result = await c.svc.run_with_scope(
        scope,
        save_or_update_assistant_stream_message(
            conversation_id=c.conv.id,
            entity_id=c.cc.entity_id,
            workspace_id=c.ws.id,
            agent_id=None,
            content="private late answer",
            message_id=placeholder.id,
        ),
    )
    assert result is None
    await c.db.refresh(placeholder)
    assert placeholder.content == original_content


@pytest.mark.parametrize("bad_field", ["channel_contact_id", "source_id", "conversation_id"])
async def test_runtime_envelope_cannot_rebind_private_browser_scope(resident_case, bad_field):
    import json
    from packages.core.ai.tools.resident_maintenance_tools import get_tools
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import resolve_runtime_principal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface

    c = resident_case
    row, proof, _ = await authorize(c)
    scope = c.svc.ResidentRuntimeScope(
        row.id, row.generation, row.entity_id, row.conversation_id, row.channel_contact_id, row.session_id
    )
    channel = {"channel_type": "webchat", "source_id": row.session_id, "channel_contact_id": row.channel_contact_id}
    conversation = row.conversation_id
    if bad_field == "conversation_id":
        conversation = "other"
    else:
        channel[bad_field] = "other"
    principal = resolve_runtime_principal(
        surface=ChatSurface.PUBLIC_CUSTOMER_CHAT, entity_id=row.entity_id, channel_context=channel
    )
    envelope = RuntimeEnvelope(
        surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
        principal=principal,
        profile=RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
        entity_id=row.entity_id,
        conversation_id=conversation,
    )
    _, handler = get_tools()[0]
    result = await c.svc.run_with_scope(
        scope, handler(entity_id=row.entity_id, request_id=1, _runtime_envelope_from_context=envelope)
    )
    assert json.loads(result)["error"] == "resident_auth_required"


async def test_public_scope_exposes_narrow_tool_only_during_verified_turn(resident_case):
    from packages.core.ai.runtime.tool_surface import runtime_public_agent_tool_surface
    from packages.core.ai.runtime.surfaces import ChatSurface

    c = resident_case
    row, proof, _ = await authorize(c)
    scope = c.svc.ResidentRuntimeScope(
        row.id, row.generation, row.entity_id, row.conversation_id, row.channel_contact_id, row.session_id
    )
    _, names = runtime_public_agent_tool_surface(surface=ChatSurface.PUBLIC_CUSTOMER_CHAT, bound_tool_names=set())
    assert c.svc.TOOL_NAME not in names

    async def scoped():
        schemas, names = runtime_public_agent_tool_surface(
            surface=ChatSurface.PUBLIC_CUSTOMER_CHAT, bound_tool_names=set()
        )
        assert c.svc.TOOL_NAME in names
        assert any(schema["function"]["name"] == c.svc.TOOL_NAME for schema in schemas)

    await c.svc.run_with_scope(scope, scoped())
