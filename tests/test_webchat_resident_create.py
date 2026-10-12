"""Create delegation is separate; a model can draft but cannot confirm PMS writes."""

import pytest
from pydantic import ValidationError
from test_webchat_resident_scope import resident_case as resident_case, authorize


@pytest.mark.parametrize(
    "extra",
    [
        {"tenant_id": 2},
        {"entity_id": 3},
        {"property_id": 4},
        {"unit_id": 5},
        {"assigned_to": 6},
        {"status": "completed"},
        {"confirmed": True},
        {"confirmation_nonce": "not-model-input"},
        {"location_ref": "loc_" + "a" * 64},
    ],
)
def test_draft_tool_rejects_authority_and_confirmation_fields(extra):
    from packages.core.services.webchat_resident_create import DraftContent

    with pytest.raises(ValidationError):
        DraftContent.model_validate({"title": "Leaking tap", "description": "Synthetic leak", **extra})


@pytest.mark.parametrize(
    "fields",
    [
        {"title": ""},
        {"description": ""},
        {"title": "a" * 256},
        {"description": "a" * 5001},
        {"priority": "manager"},
        {"category": "billing"},
        {"title": {"private": "nested"}},
    ],
)
def test_draft_content_is_strict_bounded_and_business_only(fields):
    from packages.core.services.webchat_resident_create import DraftContent

    with pytest.raises(ValidationError):
        DraftContent.model_validate({"title": "Tap", "description": "Leaks", **fields})


def test_create_grant_and_draft_are_separate_runtime_state():
    from packages.core.models.webchat_resident_create import ResidentCreateGrant, ResidentMaintenanceDraft
    from packages.core.models.webchat_resident import WebchatResidentBinding

    assert ResidentCreateGrant.__tablename__ != WebchatResidentBinding.__tablename__
    assert "access_token" not in ResidentCreateGrant.__table__.columns
    assert "credential_ref" in ResidentCreateGrant.__table__.columns
    assert "submission_key" in ResidentMaintenanceDraft.__table__.columns
    assert "confirmation_nonce" not in ResidentMaintenanceDraft.__table__.columns


async def test_model_draft_tool_has_no_confirmation_handler():
    import json
    from packages.core.ai.tools.resident_maintenance_tools import get_tools

    tools = {schema["function"]["name"]: (schema, handler) for schema, handler in get_tools()}
    assert "resident_maintenance_draft" in tools
    assert not any("confirm" in name or "submit" in name for name in tools)
    result = await tools["resident_maintenance_draft"][1](entity_id="entity", title="Tap", description="Leaks")
    assert json.loads(result)["error"] == "resident_auth_required"


@pytest.fixture
async def create_case(resident_case, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from packages.core.services import webchat_resident_create as svc

    c = resident_case
    row, proof, _ = await authorize(c)
    original = c.provider.side_effect

    async def provider(issuer, path, **kw):
        data = await original(issuer, path, **kw)
        if path == "token" and kw.get("form", {}).get("code", "").startswith("create-"):
            return {**data, "access_token": "pms_rcreate_fixture", "scope": svc.SCOPE}
        if path == "identity" and str(kw.get("token", "")).startswith("pms_rcreate_"):
            return {**data, "scope": svc.SCOPE}
        return data

    c.provider.side_effect = provider
    loc = {"location_ref": "loc_" + "a" * 64, "label": "Synthetic hall · 101"}
    requests = []
    committed = {}

    async def resources(grant, path, **kw):
        requests.append((path, kw))
        if path == "maintenance/locations":
            return {"items": [loc]}
        if path.startswith("maintenance/submissions/"):
            key = path.split("/")[-1]
            if key not in committed:
                raise c.svc.ResidentAccessError(404, "resident_not_found", "Not found")
            return {"state": "committed", "request": committed[key]}
        assert path == "maintenance"
        key, payload = kw["submission_key"], kw["payload"]
        replayed = key in committed
        if not replayed:
            committed[key] = {
                "id": len(committed) + 1,
                **{k: v for k, v in payload.items() if k != "location_ref"},
                "status": "open",
                "created_at": None,
                "updated_at": None,
                "completed_at": None,
            }
        return {"request": committed[key], "replayed": replayed}

    resource_mock = AsyncMock(side_effect=resources)
    monkeypatch.setattr(svc, "_request", resource_mock)
    return SimpleNamespace(
        c=c,
        db=c.db,
        binding=row,
        proof=proof,
        svc=svc,
        loc=loc,
        resources=resource_mock,
        calls=requests,
        committed=committed,
    )


async def enable_create(x):
    from urllib.parse import parse_qs, urlsplit

    url = await x.svc.begin_authorization(
        x.db, x.binding, proof=x.proof, callback_uri="https://manor.example.test/callback"
    )
    state = parse_qs(urlsplit(url).query)["state"][0]
    binding, result = await x.svc.complete_authorization(
        x.db,
        state=state,
        proof=lambda _: x.proof,
        callback_uri="https://manor.example.test/callback",
        code="create-good",
    )
    assert result == "connected" and binding.id == x.binding.id
    return await x.svc.grant_for(x.db, x.binding.id)


async def make_draft(x):
    return await x.svc.make_draft(x.db, x.binding, x.svc.DraftContent(title="Tap", description="Synthetic leak"))


async def test_read_grant_alone_cannot_draft_or_post(create_case):
    x = create_case
    with pytest.raises(x.c.svc.ResidentAccessError) as error:
        await make_draft(x)
    assert error.value.code == "resident_create_auth_required"
    assert x.calls == [] and not x.committed


async def test_create_consent_does_not_replace_read_grant(create_case):
    x = create_case
    before = (x.binding.credential_ref, x.binding.status, x.binding.generation)
    grant = await enable_create(x)
    assert (x.binding.credential_ref, x.binding.status, x.binding.generation) == before
    assert x.svc._tokens(grant)["access_token"].startswith("pms_rcreate_")
    assert x.c.svc._credentials(x.binding)["access_token"].startswith("pms_res_")
    assert "pms_rcreate_" not in grant.credential_ref
    assert not x.committed


async def test_create_cancel_keeps_read_and_performs_no_token_exchange(create_case):
    from urllib.parse import parse_qs, urlsplit

    x = create_case
    before = x.binding.credential_ref
    url = await x.svc.begin_authorization(
        x.db, x.binding, proof=x.proof, callback_uri="https://manor.example.test/callback"
    )
    binding, outcome = await x.svc.complete_authorization(
        x.db,
        state=parse_qs(urlsplit(url).query)["state"][0],
        proof=lambda _: x.proof,
        callback_uri="https://manor.example.test/callback",
        error="access_denied",
    )
    assert outcome == "cancelled" and binding.credential_ref == before
    assert not x.calls


async def test_draft_edit_cancel_and_listing_have_zero_pms_writes(create_case):
    x = create_case
    await enable_create(x)
    draft = await make_draft(x)
    assert draft.fields["location_ref"] == x.loc["location_ref"]
    assert "confirmation_nonce" not in x.svc.project_draft(draft)
    first_nonce = x.svc.project_draft(draft, proof=x.proof)["confirmation_nonce"]
    updated = await x.svc.edit_draft(
        x.db,
        x.binding,
        draft.id,
        revision=1,
        fields=x.svc.DraftFields(**{**draft.fields, "description": "Changed description"}),
    )
    assert updated.revision == 2 and x.svc.confirmation_nonce(updated, x.proof) != first_nonce
    result = await x.svc.collection(x.db, x.binding, proof=x.proof)
    assert result["create_access"]["verified"] and len(result["items"]) == 1
    await x.svc.edit_draft(x.db, x.binding, draft.id, revision=2, cancel=True)
    assert draft.state == "cancelled" and not x.committed
    assert not any(kw.get("payload") for _, kw in x.calls)


@pytest.mark.parametrize("bad", ["nonce", "revision", "proof", "foreign_draft", "recover_unconfirmed"])
async def test_confirmation_requires_exact_browser_revision_and_existing_draft(create_case, bad):
    x = create_case
    await enable_create(x)
    draft = await make_draft(x)
    with pytest.raises(x.c.svc.ResidentAccessError):
        await x.svc.submit_draft(
            x.db,
            x.binding,
            "foreign" if bad == "foreign_draft" else draft.id,
            revision=2 if bad == "revision" else 1,
            nonce="other" if bad == "nonce" else x.svc.confirmation_nonce(draft, x.proof),
            proof="other" if bad == "proof" else x.proof,
            recover=bad == "recover_unconfirmed",
        )
    assert not x.committed


async def test_confirm_once_then_retry_keeps_one_frozen_key(create_case):
    x = create_case
    await enable_create(x)
    draft = await make_draft(x)
    key = draft.submission_key
    nonce = x.svc.confirmation_nonce(draft, x.proof)
    result = await x.svc.submit_draft(x.db, x.binding, draft.id, revision=1, nonce=nonce, proof=x.proof)
    assert result.state == "submitted" and result.request["id"] == 1
    result = await x.svc.submit_draft(x.db, x.binding, draft.id, revision=1, nonce=nonce, proof=x.proof)
    assert result.submission_key == key and len(x.committed) == 1
    assert len([1 for path, kw in x.calls if path == "maintenance" and kw.get("payload")]) == 1
    with pytest.raises(x.c.svc.ResidentAccessError):
        await x.svc.edit_draft(x.db, x.binding, draft.id, revision=1, fields=x.svc.DraftFields(**draft.fields))


async def test_timeout_after_commit_recovers_receipt_without_second_post(create_case):
    x = create_case
    await enable_create(x)
    draft = await make_draft(x)
    original = x.resources.side_effect

    async def lost(grant, path, **kw):
        result = await original(grant, path, **kw)
        if path == "maintenance":
            raise x.svc.unavailable()
        return result

    x.resources.side_effect = lost
    result = await x.svc.submit_draft(
        x.db, x.binding, draft.id, revision=1, nonce=x.svc.confirmation_nonce(draft, x.proof), proof=x.proof
    )
    assert result.state == "outcome_unknown" and len(x.committed) == 1 and result.request is None
    x.resources.side_effect = original
    result = await x.svc.submit_draft(x.db, x.binding, draft.id, proof=x.proof, recover=True)
    assert result.state == "submitted" and result.request["id"] == 1
    assert len([1 for path, kw in x.calls if path == "maintenance" and kw.get("payload")]) == 1


async def test_receipt404_replays_same_key_not_new_submission(create_case):
    x = create_case
    await enable_create(x)
    draft = await make_draft(x)
    original = x.resources.side_effect

    async def lost(grant, path, **kw):
        if path == "maintenance":
            raise x.svc.unavailable()
        return await original(grant, path, **kw)

    x.resources.side_effect = lost
    await x.svc.submit_draft(
        x.db, x.binding, draft.id, revision=1, nonce=x.svc.confirmation_nonce(draft, x.proof), proof=x.proof
    )
    key = draft.submission_key
    assert draft.state == "outcome_unknown" and not x.committed
    x.resources.side_effect = original
    result = await x.svc.submit_draft(x.db, x.binding, draft.id, proof=x.proof, recover=True)
    assert result.state == "submitted" and list(x.committed) == [key]
    assert any(path == "maintenance/submissions/" + key for path, _ in x.calls)


async def test_concurrent_confirm_has_one_external_post(create_case):
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from packages.core.models.webchat_resident import WebchatResidentBinding

    x = create_case
    await enable_create(x)
    draft = await make_draft(x)
    nonce = x.svc.confirmation_nonce(draft, x.proof)
    entered, release = asyncio.Event(), asyncio.Event()
    original = x.resources.side_effect

    async def slow(grant, path, **kw):
        if path == "maintenance":
            entered.set()
            await release.wait()
        return await original(grant, path, **kw)

    x.resources.side_effect = slow
    factory = async_sessionmaker(x.db.bind, expire_on_commit=False)

    async def confirm():
        async with factory() as db:
            binding = await db.get(WebchatResidentBinding, x.binding.id)
            return await x.svc.submit_draft(db, binding, draft.id, revision=1, nonce=nonce, proof=x.proof)

    first = asyncio.create_task(confirm())
    await asyncio.wait_for(entered.wait(), 5)
    second = asyncio.create_task(confirm())
    await asyncio.sleep(0.05)
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert all(value.state in {"submitted", "submitting"} for value in results)
    assert len(x.committed) == 1
    assert len([1 for path, kw in x.calls if path == "maintenance" and kw.get("payload")]) == 1


async def test_read_disconnect_disables_and_revokes_both_grants(create_case):
    x = create_case
    grant = await enable_create(x)
    await make_draft(x)
    await x.c.svc.disconnect(x.db, x.binding)
    await x.db.refresh(grant)
    assert grant.status == "revoked" and x.binding.status == "revoked"
    revoked = [kw["form"]["token"] for path, kw in x.c.calls if path == "revoke"]
    assert "pms_res_fixture" in revoked and "pms_rcreate_fixture" in revoked
    assert not x.committed


async def test_real_toolpool_can_only_save_local_draft(create_case):
    import json
    from packages.core.ai.tool_pool import ToolPool
    from packages.core.ai.tools.resident_maintenance_tools import get_tools
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import resolve_runtime_principal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface

    x = create_case
    await enable_create(x)
    row = x.binding
    scope = x.c.svc.ResidentRuntimeScope(
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
        allowed_tool_names=(x.svc.TOOL_NAME,),
    )
    pool = ToolPool()
    for schema, handler in get_tools():
        pool.register(schema["function"]["name"], schema, handler, discoverable=False)
    result = json.loads(
        await x.c.svc.run_with_scope(
            scope,
            pool.execute(
                x.svc.TOOL_NAME,
                {"title": "Tap", "description": "Synthetic leak",
                 "_runtime_run_id_from_context": "test:resident-draft",
                 "_runtime_tool_call_id_from_context": "create-draft"},
                entity_id=row.entity_id,
                workspace_id=row.workspace_id,
                conversation_id=row.conversation_id,
                runtime_envelope=envelope,
            ),
        )
    )
    assert result.get("state") == "draft", result
    assert result["request"] is None
    assert "confirmation_nonce" not in result and "submission_key" not in result
    assert not x.committed


@pytest.mark.parametrize(
    "fault", ["expired", "revoked", "wrong_scope", "wrong_resident", "read_expired", "target_changed"]
)
async def test_create_authority_changes_prevent_confirm_before_pms_write(create_case, fault):
    from datetime import timedelta

    x = create_case
    grant = await enable_create(x)
    draft = await make_draft(x)
    if fault == "expired":
        grant.expires_at = x.c.svc.now() - timedelta(seconds=1)
    if fault == "revoked":
        grant.status = "revoked"
    if fault == "read_expired":
        x.binding.expires_at = x.c.svc.now() - timedelta(seconds=1)
    if fault == "target_changed":
        x.c.target["version"] = "different"
    if fault in {"wrong_scope", "wrong_resident"}:
        original = x.c.provider.side_effect

        async def changed(issuer, path, **kw):
            result = await original(issuer, path, **kw)
            if path == "identity" and str(kw.get("token", "")).startswith("pms_rcreate_"):
                if fault == "wrong_scope":
                    result["scope"] = x.c.svc.SCOPE
                else:
                    result["resident"] = {**result["resident"], "sub": "other", "tenant_id": 999}
            return result

        x.c.provider.side_effect = changed
    await x.db.commit()
    with pytest.raises(x.c.svc.ResidentAccessError):
        await x.svc.submit_draft(
            x.db, x.binding, draft.id, revision=1, nonce=x.svc.confirmation_nonce(draft, x.proof), proof=x.proof
        )
    assert not x.committed


async def test_grant_expiry_while_waiting_for_confirmation_lock_prevents_post(create_case):
    import asyncio
    from datetime import timedelta
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from packages.core.models.webchat_resident import WebchatResidentBinding

    x = create_case
    grant = await enable_create(x)
    draft = await make_draft(x)
    factory = async_sessionmaker(x.db.bind, expire_on_commit=False)
    async with factory() as locker:
        await locker.get(WebchatResidentBinding, x.binding.id, with_for_update=True)

        async def confirm():
            async with factory() as db:
                binding = await db.get(WebchatResidentBinding, x.binding.id)
                return await x.svc.submit_draft(
                    db, binding, draft.id, revision=1, nonce=x.svc.confirmation_nonce(draft, x.proof), proof=x.proof
                )

        task = asyncio.create_task(confirm())
        await asyncio.sleep(0.05)
        assert not task.done()
        grant.expires_at = x.c.svc.now() - timedelta(seconds=1)
        await x.db.commit()
        await locker.commit()
        with pytest.raises(x.c.svc.ResidentAccessError):
            await asyncio.wait_for(task, 5)
    assert not x.committed


async def test_edit_wins_race_old_confirmation_cannot_submit_new_payload(create_case):
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from packages.core.models.webchat_resident import WebchatResidentBinding

    x = create_case
    await enable_create(x)
    draft = await make_draft(x)
    old_nonce = x.svc.confirmation_nonce(draft, x.proof)
    factory = async_sessionmaker(x.db.bind, expire_on_commit=False)
    async with factory() as edit_db:
        binding = await edit_db.get(WebchatResidentBinding, x.binding.id, with_for_update=True)

        async def confirm():
            async with factory() as db:
                target = await db.get(WebchatResidentBinding, x.binding.id)
                return await x.svc.submit_draft(db, target, draft.id, revision=1, nonce=old_nonce, proof=x.proof)

        task = asyncio.create_task(confirm())
        await asyncio.sleep(0.05)
        await x.svc.edit_draft(
            edit_db,
            binding,
            draft.id,
            revision=1,
            fields=x.svc.DraftFields(**{**draft.fields, "description": "Edited revision"}),
        )
        with pytest.raises(x.c.svc.ResidentAccessError) as failure:
            await asyncio.wait_for(task, 5)
        assert failure.value.code == "resident_draft_conflict"
    assert not x.committed


async def test_create_formal_migrations_insert_defaults_and_cascade(db_session):
    import importlib
    import uuid
    from sqlalchemy import text, select, func
    from sqlalchemy.ext.asyncio import AsyncSession
    from alembic.operations import Operations
    from alembic.migration import MigrationContext
    from packages.core.models.webchat_resident import WebchatResidentBinding
    from packages.core.models.webchat_resident_create import ResidentCreateGrant, ResidentMaintenanceDraft

    schema = "resident_migration_" + uuid.uuid4().hex
    async with db_session.bind.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
        try:
            await conn.execute(text(f"SET LOCAL search_path TO {schema}"))

            def migrate(sync_conn):
                with Operations.context(MigrationContext.configure(sync_conn)):
                    importlib.import_module(
                        "packages.core.migrations.versions.20260917_05_webchat_resident_binding"
                    ).upgrade()
                    importlib.import_module("packages.core.migrations.versions.20260917_06_resident_create").upgrade()

            await conn.run_sync(migrate)
            async with AsyncSession(bind=conn, expire_on_commit=False) as db:
                binding = WebchatResidentBinding(
                    entity_id="e",
                    workspace_id="w",
                    channel_binding_id="c",
                    channel_config_id="cc",
                    channel_contact_id="contact",
                    conversation_id="conv",
                    session_id="s",
                    target_account_id="a",
                    target_version="v",
                    issuer="https://pms.example.test",
                    pms_entity_id=-1,
                    browser_proof_hash="a" * 64,
                    credential_scheme="dev_fernet",
                    status="pending",
                    generation=1,
                )
                db.add(binding)
                await db.flush()
                grant = ResidentCreateGrant(
                    binding_id=binding.id, issuer=binding.issuer, credential_scheme="dev_fernet", read_generation=1
                )
                draft = ResidentMaintenanceDraft(
                    binding_id=binding.id, fields={"title": "Synthetic"}, payload_hash="a" * 64, submission_key="b" * 32
                )
                db.add_all([grant, draft])
                await db.flush()
                assert grant.updated_at is not None and draft.updated_at is not None
                await db.delete(binding)
                await db.flush()
                assert await db.scalar(select(func.count()).select_from(ResidentCreateGrant)) == 0
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(ResidentMaintenanceDraft)
                        .where(ResidentMaintenanceDraft.binding_id == binding.id)
                    )
                    == 0
                )
        finally:
            await conn.execute(text("SET LOCAL search_path TO public"))
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))


@pytest.fixture
async def create_http(create_case, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from unittest.mock import AsyncMock
    from apps.api.routers import public_chat, public_pms_resident, public_pms_resident_create
    from packages.core.database import get_db

    x = create_case
    app = FastAPI()
    app.include_router(public_pms_resident_create.router)
    app.include_router(public_pms_resident.router)

    async def db():
        yield x.db

    app.dependency_overrides[get_db] = db
    c = x.c
    monkeypatch.setattr(public_chat, "_resolve_channel_by_token", AsyncMock(return_value=(c.cc, c.binding)))
    monkeypatch.setattr(public_chat, "_require_chat_access", AsyncMock(return_value=None))
    monkeypatch.setattr(public_chat, "find_public_webchat_conversation_by_session", AsyncMock(return_value=c.conv))
    monkeypatch.setattr(public_chat, "find_channel_session_contact", AsyncMock(return_value=c.contact))
    monkeypatch.setattr(public_chat, "find_claimed_webchat_contact_for_user", AsyncMock(return_value=None))
    monkeypatch.setattr(public_pms_resident, "callback_url", lambda _: "https://manor.example.test/callback")
    monkeypatch.setattr(
        public_pms_resident._start_limiter, "check", AsyncMock(return_value=SimpleNamespace(allowed=True))
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://manor.example.test",
        headers={"Origin": "https://manor.example.test"},
    ) as client:
        client.cookies.set(c.svc.cookie_name(c.conv.id), x.proof)
        yield SimpleNamespace(x=x, client=client, base="/api/v1/public/chat/entry/resident")


async def test_http_create_consent_draft_edit_confirm_and_recover(create_http):
    from urllib.parse import parse_qs, urlsplit

    h = create_http
    x = h.x
    result = await h.client.get(h.base + "/maintenance/drafts", params={"session_id": "session"})
    assert result.status_code == 200 and result.json()["create_access"]["verified"] is False
    started = await h.client.post(h.base + "/create/start", json={"session_id": "session"})
    assert started.status_code == 200
    query = parse_qs(urlsplit(started.json()["authorize_url"]).query)
    assert query["scope"] == [x.svc.SCOPE]
    callback = await h.client.get(
        "/api/v1/public/pms-resident/callback", params={"state": query["state"][0], "code": "create-good"}
    )
    assert callback.status_code == 303 and callback.headers["location"].endswith("?resident_create=connected")
    replay = await h.client.get(
        "/api/v1/public/pms-resident/callback", params={"state": query["state"][0], "code": "create-good"}
    )
    assert replay.status_code == 401
    draft = await x.svc.make_draft(x.db, x.binding, x.svc.DraftContent(title="Tap", description="Leaks"))
    result = await h.client.get(h.base + "/maintenance/drafts", params={"session_id": "session"})
    item = result.json()["items"][0]
    assert result.status_code == 200 and item["confirmation_nonce"]
    assert "submission_key" not in item and "no-store" in result.headers["cache-control"]
    edited = await h.client.patch(
        h.base + "/maintenance/drafts/" + draft.id,
        json={
            "session_id": "session",
            "action": "edit",
            "revision": 1,
            "fields": {**item["fields"], "title": "New title"},
        },
    )
    assert edited.status_code == 200 and edited.json()["revision"] == 2
    assert not [call for call in x.calls if call[0] == "maintenance"]
    body = {"session_id": "session", "revision": 2, "confirmation_nonce": edited.json()["confirmation_nonce"]}
    submitted = await h.client.post(h.base + "/maintenance/drafts/" + draft.id + "/confirm", json=body)
    assert submitted.status_code == 200 and submitted.json()["state"] == "submitted"
    recovered = await h.client.post(
        h.base + "/maintenance/drafts/" + draft.id + "/recover", json={"session_id": "session"}
    )
    assert recovered.status_code == 200 and recovered.json()["request"] == submitted.json()["request"]
    assert len([call for call in x.calls if call[0] == "maintenance"]) == 1


@pytest.mark.parametrize(
    "boundary", ["missing_proof", "wrong_origin", "wrong_session", "selector", "nonce", "revision"]
)
async def test_http_create_confirmation_boundary_zero_writes(create_http, boundary):
    h = create_http
    x = h.x
    await enable_create(x)
    draft = await x.svc.make_draft(x.db, x.binding, x.svc.DraftContent(title="Tap", description="Leaks"))
    body = {"session_id": "session", "revision": 1, "confirmation_nonce": x.svc.confirmation_nonce(draft, x.proof)}
    headers = {}
    if boundary == "missing_proof":
        h.client.cookies.clear()
    elif boundary == "wrong_origin":
        headers["Origin"] = "https://evil.example.test"
    elif boundary == "wrong_session":
        body["session_id"] = "another-session"
    elif boundary == "selector":
        body["tenant_id"] = 43
    elif boundary == "nonce":
        body["confirmation_nonce"] = "invalid"
    else:
        body["revision"] = 2
    response = await h.client.post(h.base + "/maintenance/drafts/" + draft.id + "/confirm", json=body, headers=headers)
    assert response.status_code in {401, 403, 409, 422}, response.text
    assert "no-store" in response.headers["cache-control"]
    assert not [call for call in x.calls if call[0] == "maintenance"]


async def test_expired_create_does_not_revoke_read_and_get_never_recovers(create_http):
    from datetime import timedelta

    h = create_http
    x = h.x
    grant = await enable_create(x)
    grant.expires_at = x.c.svc.now() - timedelta(seconds=1)
    await x.db.commit()
    response = await h.client.get(h.base + "/maintenance/drafts", params={"session_id": "session"})
    assert response.status_code == 200 and response.json()["create_access"]["verified"] is False
    await x.c.svc.validate_binding(x.db, x.binding)
    assert not [call for call in x.calls if call[0] == "maintenance"]


async def test_stale_runtime_generation_cannot_persist_new_draft(create_case):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import select, func
    from packages.core.models.webchat_resident_create import ResidentMaintenanceDraft

    x = create_case
    await enable_create(x)
    original_generation = x.binding.generation
    x.binding.generation += 1
    await x.db.commit()
    async with async_sessionmaker(x.db.bind, expire_on_commit=False)() as db:
        row = await db.get(type(x.binding), x.binding.id)
        with pytest.raises(x.c.svc.ResidentAccessError):
            await x.svc.make_draft(
                db,
                row,
                x.svc.DraftContent(title="Late answer", description="Old generation"),
                expected_read_generation=original_generation,
            )
    assert (
        await x.db.scalar(
            select(func.count())
            .select_from(ResidentMaintenanceDraft)
            .where(ResidentMaintenanceDraft.binding_id == x.binding.id)
        )
        == 0
    )


async def test_purge_cascades_create_credentials_and_private_drafts(create_case):
    from datetime import timedelta
    from packages.core.services.entity_service import purge_workspace
    from packages.core.models.webchat_resident_create import ResidentCreateGrant, ResidentMaintenanceDraft

    x = create_case
    grant = await enable_create(x)
    draft = await x.svc.make_draft(x.db, x.binding, x.svc.DraftContent(title="Tap", description="Leaks"))
    grant_id, draft_id = grant.id, draft.id
    x.c.ws.deleted_at = x.c.svc.now() - timedelta(days=31)
    await x.db.commit()
    assert await purge_workspace(x.db, x.c.ws.id)
    from sqlalchemy import select

    assert await x.db.scalar(select(ResidentCreateGrant.id).where(ResidentCreateGrant.id == grant_id)) is None
    assert await x.db.scalar(select(ResidentMaintenanceDraft.id).where(ResidentMaintenanceDraft.id == draft_id)) is None


def test_blueprint_cannot_export_resident_create_runtime_or_confirmation():
    from packages.core.blueprints.exporter import _without_nonportable_references

    assert _without_nonportable_references(
        {
            "name": "Public FAQ",
            "resident_create_grant": {"credential_ref": "private"},
            "nested": {
                "resident_create_grant_id": "private",
                "resident_maintenance_drafts": [{"title": "private"}],
                "confirmation_nonce": "private",
                "submission_key": "private",
                "confirmed_payload": {"title": "private"},
                "safe": 1,
            },
        }
    ) == {"name": "Public FAQ", "nested": {"safe": 1}}


@pytest.mark.parametrize(
    "mutation", ["scope_missing", "entity", "conversation", "surface", "tool", "action", "access", "unbound"]
)
async def test_external_control_exception_is_exact_and_request_scoped(create_case, mutation):
    from dataclasses import replace
    from packages.core.services.runtime_authorization.strategies import ExternalRuntimeAuthorizationStrategy
    from packages.core.services.runtime_authorization.domain import (
        RuntimeAuthorizationRequest,
        RuntimeAuthorizationAccess,
        AuthorizationSubject,
    )
    from packages.core.services.runtime_authorization.mappers import RegistryEntityPermissionMapper

    x = create_case
    row = x.binding
    scope = x.c.svc.ResidentRuntimeScope(
        row.id, row.generation, row.entity_id, row.conversation_id, row.channel_contact_id, row.session_id
    )
    request = RuntimeAuthorizationRequest.create(
        entity_id=row.entity_id,
        user_id=None,
        workspace_id=row.workspace_id,
        conversation_id=row.conversation_id,
        action_key="runtime.control.resident_maintenance_draft",
        capability_id=None,
        access=RuntimeAuthorizationAccess.CONTROL,
        tool_name=x.svc.TOOL_NAME,
        bound_tool_names=[x.svc.TOOL_NAME],
        runtime_surface="public_customer_chat",
    )
    if mutation == "entity":
        request = replace(request, entity_id="other")
    if mutation == "conversation":
        request = replace(request, conversation_id="other")
    if mutation == "surface":
        request = replace(request, runtime_surface="workspace_chat")
    if mutation == "tool":
        request = replace(request, tool_name="sandbox_destroy", bound_tool_names=frozenset({"sandbox_destroy"}))
    if mutation == "action":
        request = replace(request, subject=AuthorizationSubject.create("runtime.control.submit_result", None))
    if mutation == "access":
        request = replace(request, access=RuntimeAuthorizationAccess.ACTION)
    if mutation == "unbound":
        request = replace(request, bound_tool_names=frozenset())
    strategy = ExternalRuntimeAuthorizationStrategy(RegistryEntityPermissionMapper())
    operation = strategy.authorize(x.db, request)
    result = await operation if mutation == "scope_missing" else await x.c.svc.run_with_scope(scope, operation)
    assert not result.allowed


async def test_multiple_locations_require_browser_selection_and_foreign_ref_denied(create_case):
    x = create_case
    await enable_create(x)
    original = x.resources.side_effect
    other = {"location_ref": "loc_" + "b" * 64, "label": "Synthetic hall · 102"}

    async def resources(grant, path, **kw):
        if path == "maintenance/locations":
            return {"items": [x.loc, other]}
        return await original(grant, path, **kw)

    x.resources.side_effect = resources
    draft = await x.svc.make_draft(x.db, x.binding, x.svc.DraftContent(title="Tap", description="Leaks"))
    assert (
        draft.fields["location_ref"] is None and x.svc.project_draft(draft, proof=x.proof)["confirmation_nonce"] is None
    )
    with pytest.raises(x.c.svc.ResidentAccessError):
        await x.svc.submit_draft(
            x.db, x.binding, draft.id, revision=1, nonce=x.svc.confirmation_nonce(draft, x.proof), proof=x.proof
        )
    with pytest.raises(x.c.svc.ResidentAccessError):
        await x.svc.edit_draft(
            x.db,
            x.binding,
            draft.id,
            revision=1,
            fields=x.svc.DraftFields(**{**draft.fields, "location_ref": "loc_" + "c" * 64}),
        )
    edited = await x.svc.edit_draft(
        x.db,
        x.binding,
        draft.id,
        revision=1,
        fields=x.svc.DraftFields(**{**draft.fields, "location_ref": other["location_ref"]}),
    )
    assert edited.location_label == other["label"] and edited.revision == 2
    assert not [call for call in x.calls if call[0] == "maintenance"]


async def test_receipt_404_after_lost_commit_never_changes_key_or_duplicates(create_case):
    x = create_case
    await enable_create(x)
    draft = await x.svc.make_draft(x.db, x.binding, x.svc.DraftContent(title="Tap", description="Leaks"))
    original = x.resources.side_effect

    async def timeout_after_commit(grant, path, **kw):
        result = await original(grant, path, **kw)
        if path == "maintenance":
            raise x.svc.unavailable()
        return result

    x.resources.side_effect = timeout_after_commit
    await x.svc.submit_draft(
        x.db, x.binding, draft.id, revision=1, nonce=x.svc.confirmation_nonce(draft, x.proof), proof=x.proof
    )
    assert draft.state == "outcome_unknown" and len(x.committed) == 1

    async def hidden_receipt(grant, path, **kw):
        if path.startswith("maintenance/submissions/"):
            raise x.c.svc.ResidentAccessError(404, "resident_not_found", "Not found")
        return await original(grant, path, **kw)

    x.resources.side_effect = hidden_receipt
    await x.svc.submit_draft(x.db, x.binding, draft.id, proof=x.proof, recover=True)
    attempts = [kw for path, kw in x.calls if path == "maintenance"]
    assert draft.state == "submitted" and len(x.committed) == 1 and len(attempts) == 2
    assert attempts[0] == attempts[1]


@pytest.mark.parametrize("invalid", ["read_scope", "foreign_subject", "vault_failure"])
async def test_bad_create_callback_retires_new_token_preserves_read(create_case, invalid, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    x = create_case
    before_read = x.c.svc._credentials(x.binding)["access_token"]
    url = await x.svc.begin_authorization(
        x.db, x.binding, proof=x.proof, callback_uri="https://manor.example.test/callback"
    )
    original = x.c.provider.side_effect

    async def provider(issuer, path, **kw):
        result = await original(issuer, path, **kw)
        if path == "token":
            if invalid == "read_scope":
                return {**result, "scope": x.c.svc.SCOPE}
            if invalid == "foreign_subject":
                return {**result, "resident": {"sub": "pms-resident:-1:43", "entity_id": -1, "tenant_id": 43}}
        return result

    x.c.provider.side_effect = provider
    if invalid == "vault_failure":
        monkeypatch.setattr(x.svc, "_store", lambda *_: (_ for _ in ()).throw(x.svc.unavailable()))
    with pytest.raises(x.c.svc.ResidentAccessError):
        await x.svc.complete_authorization(
            x.db,
            state=parse_qs(urlsplit(url).query)["state"][0],
            proof=lambda _: x.proof,
            callback_uri="https://manor.example.test/callback",
            code="create-good",
        )
    assert x.c.svc._credentials(x.binding)["access_token"] == before_read
    await x.c.svc.validate_binding(x.db, x.binding)
    grant = await x.svc.grant_for(x.db, x.binding.id)
    assert grant.status != "active" and grant.state_hash is None
    assert any(
        path == "revoke" and kw.get("form", {}).get("token", "").startswith("pms_rcreate_") for path, kw in x.c.calls
    )
    assert not [call for call in x.calls if call[0] == "maintenance"]


@pytest.mark.parametrize(
    "mutation", ["scope_missing", "contact", "session", "conversation", "surface", "actor", "confirmed"]
)
async def test_draft_handler_rejects_forged_runtime_or_model_confirmation(create_case, mutation):
    import json
    from dataclasses import replace
    from packages.core.ai.tools.resident_maintenance_tools import get_tools
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import resolve_runtime_principal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.constants.runtime_principal import RuntimePrincipalKind
    from sqlalchemy import select, func
    from packages.core.models.webchat_resident_create import ResidentMaintenanceDraft

    x = create_case
    await enable_create(x)
    row = x.binding
    scope = x.c.svc.ResidentRuntimeScope(
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
    if mutation == "contact":
        principal = replace(principal, metadata={"channel_contact_id": "other"})
    if mutation == "session":
        principal = replace(principal, external_sender_id="other")
    if mutation == "actor":
        principal = replace(principal, kind=RuntimePrincipalKind.OWNER)
    envelope = RuntimeEnvelope(
        surface=ChatSurface.WORKSPACE_CHAT if mutation == "surface" else ChatSurface.PUBLIC_CUSTOMER_CHAT,
        principal=principal,
        profile=RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
        entity_id=row.entity_id,
        workspace_id=row.workspace_id,
        conversation_id="other" if mutation == "conversation" else row.conversation_id,
        allowed_tool_names=(x.svc.TOOL_NAME,),
    )
    kwargs = {"title": "Tap", "description": "Leaks", "_runtime_envelope_from_context": envelope}
    if mutation == "confirmed":
        kwargs["confirmed"] = True
    operation = get_tools()[1][1](entity_id=row.entity_id, **kwargs)
    result = json.loads(
        await operation if mutation == "scope_missing" else await x.c.svc.run_with_scope(scope, operation)
    )
    assert "error" in result
    assert (
        await x.db.scalar(
            select(func.count())
            .select_from(ResidentMaintenanceDraft)
            .where(ResidentMaintenanceDraft.binding_id == row.id)
        )
        == 0
    )
    assert not [call for call in x.calls if call[0] == "maintenance"]


async def test_waiting_old_model_turn_cannot_draft_after_reauthorization(create_case):
    import asyncio
    from sqlalchemy import select, func
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from packages.core.models.webchat_resident_create import ResidentMaintenanceDraft

    x = create_case
    await enable_create(x)
    factory = async_sessionmaker(x.db.bind, expire_on_commit=False)
    old_generation = x.binding.generation
    await x.db.commit()
    started = asyncio.Event()
    async with factory() as blocker, factory() as worker:
        old_row = await worker.get(type(x.binding), x.binding.id)
        locked = await blocker.get(type(x.binding), x.binding.id, with_for_update=True)

        async def delayed_draft():
            started.set()
            return await x.svc.make_draft(
                worker,
                old_row,
                x.svc.DraftContent(title="Old turn", description="Waited during consent renewal"),
                expected_read_generation=old_generation,
            )

        task = asyncio.create_task(delayed_draft())
        await started.wait()
        await asyncio.sleep(0.05)
        assert not task.done()
        locked.generation += 1
        await blocker.commit()
        with pytest.raises(x.c.svc.ResidentAccessError):
            await asyncio.wait_for(task, 5)
    assert (
        await x.db.scalar(
            select(func.count())
            .select_from(ResidentMaintenanceDraft)
            .where(ResidentMaintenanceDraft.binding_id == x.binding.id)
        )
        == 0
    )
    assert not [call for call in x.calls if call[0] == "maintenance"]


async def test_disconnect_cleanup_serializes_concurrent_read_reauthorization(create_case):
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker

    x = create_case
    await enable_create(x)
    await x.db.commit()
    factory = async_sessionmaker(x.db.bind, expire_on_commit=False)
    entered, release = asyncio.Event(), asyncio.Event()
    original = x.c.provider.side_effect

    async def slow_revoke(issuer, path, **kwargs):
        if path == "revoke" and not entered.is_set():
            entered.set()
            await release.wait()
        return await original(issuer, path, **kwargs)

    x.c.provider.side_effect = slow_revoke
    async with factory() as closing, factory() as renewing:
        close_row = await closing.get(type(x.binding), x.binding.id)
        closing_task = asyncio.create_task(x.c.svc.disconnect(closing, close_row))
        await asyncio.wait_for(entered.wait(), 5)

        async def renew():
            return await x.c.svc.begin_authorization(
                renewing,
                cc=await renewing.get(type(x.c.cc), x.c.cc.id),
                binding=await renewing.get(type(x.c.binding), x.c.binding.id),
                contact=await renewing.get(type(x.c.contact), x.c.contact.id),
                conversation=await renewing.get(type(x.c.conv), x.c.conv.id),
                session_id="session",
                user_id=None,
                proof=x.proof,
                callback_uri="https://manor.example.test/callback",
            )

        renewal = asyncio.create_task(renew())
        await asyncio.sleep(0.05)
        was_waiting = not renewal.done()
        release.set()
        await asyncio.wait_for(closing_task, 5)
        row, _, _ = await asyncio.wait_for(renewal, 5)
        await renewing.refresh(row)
        verifier_preserved = bool(x.c.svc._credentials(row).get("code_verifier"))
    assert was_waiting, "renewal must wait until old read/create cleanup finishes"
    assert verifier_preserved, "old cleanup must not overwrite newly issued authorization state"
