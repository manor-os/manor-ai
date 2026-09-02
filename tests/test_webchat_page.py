"""Public page data and the existing Workspace/channel authorization boundary."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from apps.api.routers import public_chat, workspaces
from packages.core.contracts.webchat_page import WebchatPage, public_webchat_page
from packages.core.database import get_db
from packages.core.services import document_access


def page():
    return {"version": 1, "modules": [
        {"id": "brand", "type": "brand", "side": "left", "name": "Acme", "website": "https://example.com", "logo_url": ""},
        {"id": "faq", "type": "faq", "side": "right", "title": "FAQ", "items": [{"question": "When?", "answer": "Contact us."}]},
    ]}


def connected_page():
    return {"version": 1, "modules": [
        {"id": "profile", "type": "workspace_content", "side": "left", "title": "About", "source": "profile", "resource_id": "workspace"},
        {"id": "action", "type": "workspace_action", "side": "right", "title": "Request a tour", "description": "Send a request.", "binding_id": "tour-flow", "fields": ["Name", "Date"], "submit_label": "Send"},
    ]}


@pytest.mark.parametrize("address", ["javascript:alert(1)", "data:image/svg+xml,test", "//example.com", "https://user:pass@example.com", "https://example.com\\@evil.test", "https://example.com\n", "https://example.com:bad"])
def test_page_rejects_unsafe_urls(address):
    value = page()
    value["modules"][0]["website"] = address
    with pytest.raises(ValidationError):
        WebchatPage.model_validate(value)
    assert public_webchat_page(value) is None


@pytest.mark.parametrize("invalid", [
    {"version": 2, "modules": []},
    {"version": 1, "modules": [{"id": "html", "side": "left", "type": "html", "html": "<script>alert(1)</script>"}]},
    {"version": 1, "modules": [{"id": "chat", "side": "left", "type": "chat"}]},
    {"version": 1, "modules": [{"id": "../id", "side": "left", "type": "text"}]},
    {"version": 1, "modules": [{"id": "text", "side": "left", "type": "text", "body": "x" * 4001}]},
])
def test_invalid_or_executable_modules_fail_closed(invalid):
    assert public_webchat_page(invalid) is None
    with pytest.raises(ValidationError):
        workspaces.WorkspaceChannelUpdateRequest(config={"public_page": invalid})


def test_duplicate_ids_limits_and_private_fields_are_rejected():
    value = page()
    for modules in [value["modules"] * 2, [{"id": str(i), "type": "text", "side": "left"} for i in range(13)], [{**value["modules"][0], "internal_prompt": "private"}]]:
        with pytest.raises(ValidationError):
            WebchatPage.model_validate({"version": 1, "modules": modules})


def test_public_action_submission_has_a_durable_unique_index():
    from packages.core.models.workflow import WorkflowRun

    index = next(
        item
        for item in WorkflowRun.__table__.indexes
        if item.name == "uq_workflow_runs_webchat_submission"
    )
    assert index.unique is True
    assert [column.name for column in index.columns] == [
        "binding_id",
        "webchat_session_id",
        "webchat_module_id",
        "webchat_submission_id",
    ]


@pytest.mark.parametrize("fields", [
    ["Name", "Name"],
    ["Name", "name"],
    ["API token"],
    ["Password"],
    [" Name"],
    ["fields"],
    ["FIELDS"],
    ["trigger"],
    ["webchat_session_id"],
])
def test_public_action_rejects_duplicate_or_sensitive_fields(fields):
    value = connected_page()
    value["modules"][1]["fields"] = fields
    with pytest.raises(ValidationError):
        WebchatPage.model_validate(value)


async def test_public_document_visibility_batch_uses_one_preloaded_policy_snapshot(monkeypatch):
    documents = [
        SimpleNamespace(id="visible", entity_id="entity", folder_id="folder", quarantine_status=None),
        SimpleNamespace(id="private", entity_id="entity", folder_id="folder", quarantine_status=None),
    ]

    class Context:
        preload_documents = AsyncMock()

        @staticmethod
        def document_owned_by_deleted_workspace(document):
            return False

        @staticmethod
        def effective_document_policy(document):
            return ("internal", "private" if document.id == "private" else "workspace", True)

    context = Context()
    load = AsyncMock(return_value=context)
    links = AsyncMock(return_value={"visible": {"workspace"}, "private": {"workspace"}})
    monkeypatch.setattr(document_access.DocumentAccessContext, "load", load)
    monkeypatch.setattr(document_access, "document_workspace_ids_batched", links)
    result = await document_access.public_agent_visible_document_ids_batched(
        AsyncMock(),
        documents,
        entity_id="entity",
        workspace_id="workspace",
    )
    assert result == {"visible"}
    load.assert_awaited_once()
    context.preload_documents.assert_awaited_once()
    links.assert_awaited_once()


@pytest.mark.parametrize("model", [workspaces.WorkspaceChannelRequest, workspaces.WorkspaceChannelUpdateRequest])
def test_channel_writes_normalize_public_page_and_allow_explicit_clear(model):
    request = model(config={"public_page": page(), "unrelated": "preserve"})
    assert request.config["public_page"] == page()
    assert request.config["unrelated"] == "preserve"
    assert model(config={"public_page": None}).config == {"public_page": None}


@pytest.mark.parametrize("shared", [False, True])
async def test_page_only_update_preserves_routing_language_and_other_settings(monkeypatch, shared):
    db = AsyncMock()
    user = SimpleNamespace(id="manager", entity_id="entity")
    binding = SimpleNamespace(id="binding", type="webchat", user_id=None, agent_id="agent", agent_subscription_id="subscription", config={"channel_config_id": "account", "language": "zh", "login_required": True, "purpose": "unchanged"})
    account = SimpleNamespace(id="account", owner_user_id=None, workspace_id=None if shared else "workspace", channel_type="webchat", config={"language": "es", "secret": "not-public"})
    original_account_config = deepcopy(account.config)
    db.execute.side_effect = [SimpleNamespace(scalar_one_or_none=lambda value=value: value) for value in (binding, account)]
    monkeypatch.setattr(workspaces, "_require_workspace_manage", AsyncMock())
    monkeypatch.setattr("packages.core.services.workspace_service.record_activity", AsyncMock())
    await workspaces.update_workspace_channel("workspace", "binding", workspaces.WorkspaceChannelUpdateRequest(config={"public_page": page()}), user, db)
    assert binding.config["public_page"] == page()
    assert binding.config["language"] == "zh"
    assert binding.config["login_required"] is True
    assert binding.config["purpose"] == "unchanged"
    assert binding.agent_id == "agent"
    assert binding.agent_subscription_id == "subscription"
    assert account.config == original_account_config
    db.commit.assert_awaited_once()


async def test_page_update_persists_only_the_authoritative_review_projection(monkeypatch):
    db = AsyncMock()
    user = SimpleNamespace(id="manager", entity_id="entity")
    binding = SimpleNamespace(
        id="binding",
        type="webchat",
        user_id=None,
        agent_id="agent",
        agent_subscription_id="subscription",
        config={"channel_config_id": "account"},
    )
    account = SimpleNamespace(
        id="account",
        owner_user_id=None,
        workspace_id="workspace",
        channel_type="webchat",
        config={},
    )
    db.execute.side_effect = [
        SimpleNamespace(scalar_one_or_none=lambda value=value: value)
        for value in (binding, account)
    ]
    resolver = AsyncMock(return_value=WebchatPage.model_validate(page()))
    monkeypatch.setattr(workspaces, "_require_workspace_manage", AsyncMock())
    monkeypatch.setattr(
        "packages.core.services.webchat_page.resolve_workspace_webchat_page",
        resolver,
    )
    monkeypatch.setattr("packages.core.services.workspace_service.record_activity", AsyncMock())
    await workspaces.update_workspace_channel(
        "workspace",
        "binding",
        workspaces.WorkspaceChannelUpdateRequest(config={"public_page": connected_page()}),
        user,
        db,
    )
    assert binding.config["public_page"] == page()
    resolver.assert_awaited_once()


async def test_page_update_cannot_bypass_workspace_management(monkeypatch):
    db = AsyncMock()
    monkeypatch.setattr(workspaces, "lock_workspace_access_boundary", AsyncMock(return_value=SimpleNamespace(deleted_at=None)))
    monkeypatch.setattr(workspaces, "user_can_manage_workspace", AsyncMock(return_value=False))
    monkeypatch.setattr(
        workspaces.ResourcePermissionGate,
        "authorize_workspace_read",
        AsyncMock(return_value=SimpleNamespace()),
    )
    with pytest.raises(HTTPException) as error:
        await workspaces.update_workspace_channel("other-workspace", "binding", workspaces.WorkspaceChannelUpdateRequest(config={"public_page": page()}), SimpleNamespace(id="viewer", entity_id="entity", role="member"), db)
    assert error.value.status_code == 403
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_workspace_resource_catalog_only_returns_public_projection(monkeypatch):
    db = AsyncMock()
    user = SimpleNamespace(id="viewer", entity_id="entity", role="member")
    workspace = SimpleNamespace(
        name="Acme",
        identity_label=None,
        description="Public description",
        cover_image_url="",
        category="Services",
        address=None,
    )
    document = SimpleNamespace(id="document", entity_id="entity", name="Guide", metadata_={"public_summary": "Visitor information", "secret": "hidden"})
    binding = SimpleNamespace(id="flow-binding", name="Request a tour")
    workflow = SimpleNamespace(name="Tour flow", description="Public action", steps=[{"secret": "hidden"}])
    db.get.return_value = SimpleNamespace(logo_url="https://example.com/logo.png")
    db.scalars.return_value = SimpleNamespace(unique=lambda: SimpleNamespace(all=lambda: [document]))
    db.execute.return_value = SimpleNamespace(all=lambda: [(binding, workflow)])
    monkeypatch.setattr(workspaces, "_require_workspace_manage", AsyncMock(return_value=workspace))
    monkeypatch.setattr("packages.core.services.document_access.public_agent_visible_document_ids_batched", AsyncMock(return_value={"document"}))
    result = await workspaces.list_webchat_workspace_resources("workspace", user, db)
    assert result == {
        "brand": {"name": "Acme", "logo_url": "https://example.com/logo.png", "website": ""},
        "profile": {"id": "workspace", "name": "Acme", "body": "Public description", "image_url": "", "items": ["Services"]},
        "documents": [{"id": "document", "name": "Guide", "body": ""}],
        "documents_next_cursor": None,
        "actions": [{"id": "flow-binding", "name": "Request a tour", "description": "Public action"}],
    }
    assert "hidden" not in str(result)
    assert "SELECT DISTINCT" in str(db.scalars.await_args.args[0])


async def test_workspace_action_picker_values_fit_the_page_contract(monkeypatch):
    db = AsyncMock()
    user = SimpleNamespace(id="viewer", entity_id="entity", role="member")
    workspace = SimpleNamespace(
        name="Acme",
        identity_label=None,
        description="",
        cover_image_url="",
        category=None,
        address=None,
    )
    binding = SimpleNamespace(id="flow-binding", name="N" * 255)
    workflow = SimpleNamespace(name="Flow", description="D" * 1500)
    db.scalars.return_value = SimpleNamespace(unique=lambda: SimpleNamespace(all=lambda: []))
    db.execute.return_value = SimpleNamespace(all=lambda: [(binding, workflow)])
    monkeypatch.setattr(workspaces, "_require_workspace_manage", AsyncMock(return_value=workspace))
    result = await workspaces.list_webchat_workspace_resources("workspace", user, db)
    assert len(result["actions"][0]["name"]) == 160
    assert len(result["actions"][0]["description"]) == 1000


async def test_workspace_resource_catalog_limits_documents_after_visibility(monkeypatch):
    db = AsyncMock()
    user = SimpleNamespace(id="viewer", entity_id="entity", role="member")
    workspace = SimpleNamespace(
        name="Acme",
        identity_label=None,
        description="",
        cover_image_url="",
        category=None,
        address=None,
    )
    private_documents = [
        SimpleNamespace(id=f"private-{index}", entity_id="entity", name=f"A {index:03d}")
        for index in range(200)
    ]
    public_document = SimpleNamespace(id="public", entity_id="entity", name="Z Public")
    db.scalars.side_effect = [
        SimpleNamespace(unique=lambda: SimpleNamespace(all=lambda: [*private_documents, public_document])),
        SimpleNamespace(unique=lambda: SimpleNamespace(all=lambda: [public_document])),
    ]
    db.execute.return_value = SimpleNamespace(all=lambda: [])
    visibility = AsyncMock(side_effect=[set(), {"public"}])
    monkeypatch.setattr(
        workspaces,
        "_require_workspace_manage",
        AsyncMock(return_value=workspace),
    )
    monkeypatch.setattr(
        "packages.core.services.document_access.public_agent_visible_document_ids_batched",
        visibility,
    )

    result = await workspaces.list_webchat_workspace_resources("workspace", user, db)

    assert result["documents"] == [{"id": "public", "name": "Z Public", "body": ""}]
    assert result["documents_next_cursor"] is None
    assert visibility.await_count == 2


async def test_workspace_resource_catalog_bounds_private_document_scanning(monkeypatch):
    db = AsyncMock()
    user = SimpleNamespace(id="viewer", entity_id="entity", role="member")
    workspace = SimpleNamespace(
        name="Acme",
        identity_label=None,
        description="",
        cover_image_url="",
        category=None,
        address=None,
    )
    candidates = [
        SimpleNamespace(id=f"private-{index}", entity_id="entity", name=f"Private {index:04d}")
        for index in range(workspaces._WEBCHAT_DOCUMENT_SCAN_BATCH + 1)
    ]
    db.scalars.side_effect = [
        SimpleNamespace(unique=lambda: SimpleNamespace(all=lambda: candidates))
        for _ in range(
            workspaces._WEBCHAT_DOCUMENT_SCAN_LIMIT
            // workspaces._WEBCHAT_DOCUMENT_SCAN_BATCH
        )
    ]
    db.execute.return_value = SimpleNamespace(all=lambda: [])
    visibility = AsyncMock(return_value=set())
    monkeypatch.setattr(workspaces, "_require_workspace_manage", AsyncMock(return_value=workspace))
    monkeypatch.setattr(
        "packages.core.services.document_access.public_agent_visible_document_ids_batched",
        visibility,
    )

    result = await workspaces.list_webchat_workspace_resources("workspace", user, db)

    assert result["documents"] == []
    assert result["documents_next_cursor"] == workspaces._WEBCHAT_DOCUMENT_SCAN_LIMIT
    assert db.scalars.await_count == 5
    assert visibility.await_count == 5


async def test_workspace_review_uses_the_shared_public_projection(monkeypatch):
    db = AsyncMock()
    user = SimpleNamespace(id="viewer", entity_id="entity", role="member")
    candidate = WebchatPage.model_validate(connected_page())
    projected = WebchatPage.model_validate(page())
    resolver = AsyncMock(return_value=projected)
    require_manage = AsyncMock()
    monkeypatch.setattr(workspaces, "_require_workspace_manage", require_manage)
    monkeypatch.setattr(
        "packages.core.services.webchat_page.resolve_workspace_webchat_page",
        resolver,
    )
    result = await workspaces.review_webchat_workspace_page(
        "workspace",
        candidate,
        user,
        db,
    )
    assert result == projected
    assert resolver.await_args.kwargs == {
        "value": candidate,
        "entity_id": "entity",
        "workspace_id": "workspace",
    }
    require_manage.assert_awaited_once_with(db, "workspace", user)


async def test_read_only_review_resolves_only_the_saved_channel_page(monkeypatch):
    db = AsyncMock()
    user = SimpleNamespace(id="viewer", entity_id="entity", role="viewer")
    binding = SimpleNamespace(
        id="binding",
        entity_id="entity",
        workspace_id="workspace",
        type="webchat",
        status="active",
        config={"channel_config_id": "channel", "public_page": connected_page()},
    )
    channel = SimpleNamespace(
        id="channel",
        entity_id="entity",
        workspace_id="workspace",
        channel_type="webchat",
        status="active",
        config={},
    )
    db.scalar.side_effect = [binding, channel]
    projected = WebchatPage.model_validate(page())
    resolver = AsyncMock(return_value=projected)
    require_read = AsyncMock()
    monkeypatch.setattr(workspaces, "_require_workspace_read", require_read)
    monkeypatch.setattr(
        "packages.core.services.webchat_page.resolve_workspace_webchat_page",
        resolver,
    )
    result = await workspaces.review_saved_webchat_workspace_page(
        "workspace", "binding", user, db,
    )
    assert result == projected
    require_read.assert_awaited_once_with(db, "workspace", user)
    assert resolver.await_args.kwargs == {
        "value": connected_page(),
        "entity_id": "entity",
        "workspace_id": "workspace",
    }


@pytest.mark.parametrize("value", [page(), None, {"version": 1, "modules": [{"type": "html", "html": "secret"}]}])
async def test_public_info_projects_only_validated_page_and_binding_override(monkeypatch, value):
    db = AsyncMock()
    account = SimpleNamespace(name="Chat", config={"public_page": page(), "private_notes": "do-not-expose", "api_key": "do-not-expose"})
    binding = SimpleNamespace(agent_id=None, agent_subscription_id=None, workspace_id=None, entity_id="entity", config={"public_page": value})
    monkeypatch.setattr(public_chat, "_resolve_channel_by_token", AsyncMock(return_value=(account, binding)))
    app = FastAPI()
    app.include_router(public_chat.router)
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/public/chat/token")
    assert response.status_code == 200
    assert response.json()["public_page"] == (page() if value == page() else None)
    assert "do-not-expose" not in response.text


async def test_public_info_resolves_workspace_profile_without_persisting_preview(monkeypatch):
    db = AsyncMock()
    account = SimpleNamespace(name="Chat", config={})
    binding = SimpleNamespace(
        agent_id=None,
        agent_subscription_id=None,
        workspace_id="workspace",
        entity_id="entity",
        config={"public_page": connected_page()},
    )
    workspace = SimpleNamespace(
        name="Acme",
        identity_label="Acme Public",
        description="Public description",
        cover_image_url="https://example.com/cover.png",
        category="Services",
        address="100 Main St",
    )
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: "Acme")
    db.scalar.return_value = workspace
    db.scalars.return_value = SimpleNamespace(all=lambda: ["tour-flow"])
    monkeypatch.setattr(public_chat, "_resolve_channel_by_token", AsyncMock(return_value=(account, binding)))
    app = FastAPI()
    app.include_router(public_chat.router)
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/public/chat/token")
    assert response.status_code == 200
    assert response.json()["workspace_name"] == "Acme Public"
    resolved = response.json()["public_page"]["modules"][0]["resolved"]
    assert resolved == {
        "name": "Acme Public",
        "body": "Public description",
        "image_url": "https://example.com/cover.png",
        "items": ["Services", "100 Main St"],
    }
    normalized = workspaces.WorkspaceChannelUpdateRequest(
        config={"public_page": response.json()["public_page"]},
    )
    assert "resolved" not in normalized.config["public_page"]["modules"][0]


async def test_revoked_document_is_removed_from_the_public_page(monkeypatch):
    db = AsyncMock()
    stored = {"version": 1, "modules": [{
        "id": "guide", "type": "workspace_content", "side": "left", "title": "Guide", "source": "document", "resource_id": "document",
    }]}
    binding = SimpleNamespace(workspace_id="workspace", entity_id="entity")
    document = SimpleNamespace(id="document", entity_id="entity", is_trashed=False)
    db.scalars.return_value = SimpleNamespace(all=lambda: [document])
    monkeypatch.setattr("packages.core.services.document_access.public_agent_visible_document_ids_batched", AsyncMock(return_value=set()))
    resolved = await public_chat._resolve_public_page(db, value=stored, binding=binding)
    assert resolved is not None
    assert resolved.modules == []


async def test_public_document_projection_uses_the_same_chunk_body_as_review(monkeypatch):
    db = AsyncMock()
    stored = {"version": 1, "modules": [{
        "id": "guide", "type": "workspace_content", "side": "left", "title": "Guide", "source": "document", "resource_id": "document",
    }]}
    binding = SimpleNamespace(workspace_id="workspace", entity_id="entity")
    document = SimpleNamespace(id="document", entity_id="entity", is_trashed=False, name="Guide", metadata_={})
    db.scalars.side_effect = [
        SimpleNamespace(all=lambda: [document]),
        SimpleNamespace(all=lambda: ["First public section", "Second public section"]),
    ]
    monkeypatch.setattr("packages.core.services.document_access.public_agent_visible_document_ids_batched", AsyncMock(return_value={"document"}))
    resolved = await public_chat._resolve_public_page(db, value=stored, binding=binding)
    assert resolved is not None
    assert resolved.modules[0].resolved.body == "First public section\n\nSecond public section"


async def test_inactive_workspace_action_is_removed_from_the_public_page():
    db = AsyncMock()
    binding = SimpleNamespace(workspace_id="workspace", entity_id="entity")
    db.scalar.return_value = SimpleNamespace(
        name="Acme",
        identity_label=None,
        description="Public description",
        cover_image_url="",
        category=None,
        address=None,
    )
    db.scalars.return_value = SimpleNamespace(all=lambda: [])
    resolved = await public_chat._resolve_public_page(db, value=connected_page(), binding=binding)
    assert resolved is not None
    assert [module.id for module in resolved.modules] == ["profile"]


async def test_public_action_requires_a_published_module_and_matching_session(monkeypatch):
    db = AsyncMock()
    account = SimpleNamespace(id="channel", entity_id="entity", name="Chat", config={})
    binding = SimpleNamespace(
        workspace_id="workspace",
        entity_id="entity",
        config={"public_page": connected_page()},
    )
    conversation = SimpleNamespace(id="conversation", meta={})
    workflow_binding = SimpleNamespace(id="tour-flow")
    run = SimpleNamespace(id="run", status="running", error=None)
    db.scalar.side_effect = [workflow_binding, None, workflow_binding, run]
    monkeypatch.setattr(public_chat, "_resolve_channel_by_token", AsyncMock(return_value=(account, binding)))
    monkeypatch.setattr(public_chat, "_require_chat_access", AsyncMock(return_value=None))
    monkeypatch.setattr(public_chat, "find_public_webchat_conversation_by_session", AsyncMock(return_value=conversation))
    monkeypatch.setattr(public_chat, "_ensure_session_contact_for_user", AsyncMock())
    start = AsyncMock(return_value=run)
    limiter = SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(allowed=True, retry_after=0)))
    monkeypatch.setattr(public_chat, "_public_action_limiter", limiter)
    monkeypatch.setattr("packages.core.services.workflow_service.start_workflow_from_binding", start)
    monkeypatch.setattr("packages.core.ai.workflow_runner.WorkflowRunner.enqueue", lambda run_id: True)
    app = FastAPI()
    app.include_router(public_chat.router)
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        payload = {"session_id": "visitor", "submission_id": "submission-123456", "values": {"Name": "Alex", "Date": "Friday"}}
        missing = await client.post("/api/v1/public/chat/token/actions/missing", json={**payload, "values": {}})
        wrong_fields = await client.post("/api/v1/public/chat/token/actions/action", json={**payload, "values": {"Name": "Alex"}})
        response = await client.post("/api/v1/public/chat/token/actions/action", json=payload)
        duplicate = await client.post("/api/v1/public/chat/token/actions/action", json=payload)
    assert missing.status_code == 404
    assert wrong_fields.status_code == 422
    assert response.status_code == 202
    assert response.json() == {"accepted": True, "queued": True}
    assert duplicate.status_code == 202
    assert duplicate.json() == {"accepted": True, "queued": True, "duplicate": True}
    assert start.await_args.kwargs["execution_workspace_id"] == "workspace"
    assert start.await_args.kwargs["trigger_data"]["fields"] == {"Name": "Alex", "Date": "Friday"}
    assert start.await_args.kwargs["trigger_data"]["Name"] == "Alex"
    assert start.await_args.kwargs["trigger_data"]["webchat_submission_id"] == "submission-123456"
    assert limiter.check.await_count == 4
    assert [call.args[0].split(":", 1)[0] for call in limiter.check.await_args_list] == [
        "webchat-action-attempt",
        "webchat-action-create",
        "webchat-action-attempt",
        "webchat-action-retry",
    ]
    creation_key = limiter.check.await_args_list[1].args[0]
    assert creation_key.startswith("webchat-action-create:token:")
    assert "visitor" not in creation_key
    assert limiter.check.await_args_list[3].args[0].startswith(
        "webchat-action-retry:token:submission-123456:"
    )
    assert run.webchat_session_id == "visitor"
    assert run.webchat_module_id == "action"
    assert run.webchat_submission_id == "submission-123456"
    start.assert_awaited_once()
    assert db.commit.await_count == 2


async def test_public_action_attempt_limit_runs_before_session_and_workflow_queries(monkeypatch):
    db = AsyncMock()
    account = SimpleNamespace(id="channel", entity_id="entity", name="Chat", config={})
    binding = SimpleNamespace(
        workspace_id="workspace",
        entity_id="entity",
        config={"public_page": connected_page()},
    )
    monkeypatch.setattr(
        public_chat,
        "_resolve_channel_by_token",
        AsyncMock(return_value=(account, binding)),
    )
    monkeypatch.setattr(
        public_chat,
        "_require_chat_access",
        AsyncMock(return_value=None),
    )
    find_conversation = AsyncMock()
    monkeypatch.setattr(
        public_chat,
        "find_public_webchat_conversation_by_session",
        find_conversation,
    )
    monkeypatch.setattr(
        public_chat,
        "_public_action_limiter",
        SimpleNamespace(
            check=AsyncMock(return_value=SimpleNamespace(allowed=False, retry_after=17))
        ),
    )
    app = FastAPI()
    app.include_router(public_chat.router)
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/public/chat/token/actions/action",
            json={
                "session_id": "visitor",
                "submission_id": "submission-123456",
                "values": {"Name": "Alex", "Date": "Friday"},
            },
        )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "17"
    find_conversation.assert_not_awaited()
    db.scalar.assert_not_awaited()


async def test_public_action_queue_failure_is_retryable_with_the_same_submission(monkeypatch):
    db = AsyncMock()
    account = SimpleNamespace(id="channel-retry", entity_id="entity", name="Chat", config={})
    binding = SimpleNamespace(
        workspace_id="workspace",
        entity_id="entity",
        config={"public_page": connected_page()},
    )
    conversation = SimpleNamespace(id="conversation", meta={})
    workflow_binding = SimpleNamespace(id="tour-flow")
    run = SimpleNamespace(id="run-retry", status="running", error=None)
    db.scalar.side_effect = [workflow_binding, None, workflow_binding, run]
    monkeypatch.setattr(public_chat, "_resolve_channel_by_token", AsyncMock(return_value=(account, binding)))
    monkeypatch.setattr(public_chat, "_require_chat_access", AsyncMock(return_value=None))
    monkeypatch.setattr(public_chat, "find_public_webchat_conversation_by_session", AsyncMock(return_value=conversation))
    monkeypatch.setattr(public_chat, "_ensure_session_contact_for_user", AsyncMock())
    start = AsyncMock(return_value=run)
    limiter = SimpleNamespace(check=AsyncMock(return_value=SimpleNamespace(allowed=True, retry_after=0)))
    monkeypatch.setattr(public_chat, "_public_action_limiter", limiter)
    monkeypatch.setattr("packages.core.services.workflow_service.start_workflow_from_binding", start)
    outcomes = iter([False, True])
    monkeypatch.setattr("packages.core.ai.workflow_runner.WorkflowRunner.enqueue", lambda run_id: next(outcomes))
    app = FastAPI()
    app.include_router(public_chat.router)
    app.dependency_overrides[get_db] = lambda: db
    payload = {"session_id": "visitor-retry", "submission_id": "submission-retry-123", "values": {"Name": "Alex", "Date": "Friday"}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        failed = await client.post("/api/v1/public/chat/token-retry/actions/action", json=payload)
        retried = await client.post("/api/v1/public/chat/token-retry/actions/action", json=payload)
    assert failed.status_code == 503
    assert retried.status_code == 202
    assert retried.json() == {"accepted": True, "queued": True, "duplicate": True}
    assert run.status == "pending"
    assert run.error is None
    assert [call.args[0].split(":", 1)[0] for call in limiter.check.await_args_list] == [
        "webchat-action-attempt",
        "webchat-action-create",
        "webchat-action-attempt",
        "webchat-action-retry",
    ]
    assert "visitor-retry" not in limiter.check.await_args_list[1].args[0]
    assert "submission-retry-123" in limiter.check.await_args_list[3].args[0]
    start.assert_awaited_once()
    assert db.commit.await_count == 2
