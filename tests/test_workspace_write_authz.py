"""Regression: workspace write authorization.

Two things are covered:

1. Cross-workspace WRITE hole — `POST /tasks`, `PUT /tasks/{id}`,
   `DELETE /tasks/{id}` only scoped by entity_id, so a member could create
   tasks into — and modify/delete tasks inside — a members_only workspace they
   cannot even see.
2. Workspace role semantics — a `viewer` membership is read-only;
   `contributor` / `editor` / `owner` may write. Previously every workspace
   role behaved identically.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import update

import packages.core.database as db_module
from packages.core.models.task import Task
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    Workspace,
    WorkspaceStaff,
)
from packages.core.services.task_service import create_task
from tests.test_document_permissions import _auth, _create_entity_user


async def _me(client: AsyncClient, headers: dict) -> dict:
    r = await client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _make_workspace(entity_id: str, name: str, owner_user_id: str) -> str:
    async with db_module.async_session() as db:
        ws = Workspace(
            entity_id=entity_id, name=name,
            settings={"access_mode": "members_only"},
        )
        db.add(ws)
        await db.flush()
        from packages.core.governance.approval_policy_service import initialize_workspace_approval
        await initialize_workspace_approval(db, ws)
        ws_id = ws.id
        db.add(WorkspaceStaff(
            workspace_id=ws_id, staff_id=None, user_id=owner_user_id,
            role="owner", status="active",
        ))
        await db.commit()
    return ws_id


async def _add_member(ws_id: str, user_id: str, role: str) -> None:
    async with db_module.async_session() as db:
        db.add(WorkspaceStaff(
            workspace_id=ws_id, staff_id=None, user_id=user_id,
            role=role, status="active",
        ))
        await db.commit()


async def _add_workspace_plan_runtime(
    db,
    *,
    entity_id: str,
    workspace_id: str,
    service_key: str = "planner",
) -> AgentSubscription:
    """Install the minimal active service required by the Workspace Task factory."""

    agent = Agent(
        entity_id=entity_id,
        name=f"{service_key} agent",
        status="active",
        config={},
    )
    db.add(agent)
    await db.flush()
    subscription = AgentSubscription(
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent.id,
        service_key=service_key,
        status="active",
    )
    db.add(subscription)
    await db.flush()
    return subscription


@pytest.mark.asyncio
async def test_only_workspace_managers_can_update_governance(
    client: AsyncClient,
):
    entity_admin_headers = await _auth(client, "governance_authz_admin")
    admin = await _me(client, entity_admin_headers)
    entity_id = admin["entity_id"]
    admin_id = admin.get("user_id") or admin.get("id")
    ws_id = await _make_workspace(entity_id, "Governance authz", admin_id)
    viewer = await _create_entity_user(
        entity_id,
        "governance_authz_viewer",
        role="member",
    )
    workspace_owner = await _create_entity_user(
        entity_id,
        "governance_authz_workspace_owner",
        role="member",
    )
    await _add_member(ws_id, viewer["id"], "viewer")
    await _add_member(ws_id, workspace_owner["id"], "owner")

    current = (await client.get(
        f"/api/v1/workspaces/{ws_id}/governance",
        headers=viewer["headers"],
    )).json()
    policy = {key: current["policy"][key] for key in ("max_risk_level", "budget_caps_per_kind")}
    policy["max_risk_level"] = "medium"

    forbidden = await client.put(
        f"/api/v1/workspaces/{ws_id}/governance",
        headers=viewer["headers"],
        json={"policy": policy, "change_summary": "viewer write"},
    )
    assert forbidden.status_code == 403, forbidden.text

    allowed = await client.put(
        f"/api/v1/workspaces/{ws_id}/governance",
        headers=workspace_owner["headers"],
        json={"policy": policy, "change_summary": "workspace owner write"},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["policy"]["max_risk_level"] == "medium"


@pytest.mark.asyncio
async def test_external_reply_decision_requires_authority_and_valid_intent(
    client: AsyncClient,
):
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.models.task import Conversation, Message

    owner_headers = await _auth(client, "external_reply_decision_owner")
    owner = await _me(client, owner_headers)
    entity_id = owner["entity_id"]
    owner_id = owner.get("user_id") or owner.get("id")
    ws_id = await _make_workspace(entity_id, "External reply authority", owner_id)
    contributor = await _create_entity_user(
        entity_id,
        "external_reply_decision_contributor",
        role="member",
    )
    await _add_member(ws_id, contributor["id"], "contributor")
    editor = await _create_entity_user(
        entity_id,
        "external_reply_decision_editor",
        role="member",
    )
    await _add_member(ws_id, editor["id"], "editor")

    async with db_module.async_session() as db:
        conversation = Conversation(
            entity_id=entity_id,
            workspace_id=ws_id,
            scope="workspace_main",
        )
        db.add(conversation)
        await db.flush()
        message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="Approve this external reply?",
            pending_action={
                "kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL.value,
            },
        )
        db.add(message)
        await db.commit()
        message_id = message.id

    rejected = await client.post(
        f"/api/v1/workspaces/{ws_id}/chat/messages/{message_id}/resolve",
        headers=contributor["headers"],
        json={"choice": "reject"},
    )
    assert rejected.status_code == 403, rejected.text

    invalid = await client.post(
        f"/api/v1/workspaces/{ws_id}/chat/messages/{message_id}/resolve",
        headers=owner_headers,
        json={"choice": "not-a-decision"},
    )
    assert invalid.status_code == 400, invalid.text

    from packages.core.models.participant import ParticipantProfile

    async with db_module.async_session() as db:
        db.add(ParticipantProfile(
            entity_id=entity_id,
            workspace_id=ws_id,
            user_id=editor["id"],
            authority={
                "approve_external_publish": True,
                "manage_standing_grants": False,
            },
        ))
        await db.commit()

    standing = await client.post(
        f"/api/v1/workspaces/{ws_id}/chat/messages/{message_id}/resolve",
        headers=editor["headers"],
        json={"choice": "always_approve"},
    )
    assert standing.status_code == 410, standing.text
    assert "Execution & approvals settings" in standing.json()["detail"]

    async with db_module.async_session() as db:
        message = await db.get(Message, message_id)
        assert message is not None and message.resolved_at is None


@pytest.mark.asyncio
async def test_workspace_reply_approval_surfaces_whatsapp_template_requirement(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.models.task import Conversation, Message
    from packages.core.services import channel_outbound_delivery as delivery

    owner_headers = await _auth(client, "whatsapp_template_required_owner")
    owner = await _me(client, owner_headers)
    entity_id = owner["entity_id"]
    owner_id = owner.get("user_id") or owner.get("id")
    ws_id = await _make_workspace(entity_id, "WhatsApp template required", owner_id)

    pending_action = {
        "kind": PendingActionKind.EXTERNAL_MESSAGE_APPROVAL.value,
        "channel_config_id": "config-1",
        "channel_type": "whatsapp",
        "channel_conversation_id": "channel-conversation-1",
        "chat_id": "15550001111",
        "reply_text": "Late approved reply",
        "channel_binding_id": "binding-1",
        "channel_contact_id": "contact-1",
        "workspace_id": ws_id,
        "route_snapshot": {
            "version": 1,
            "config_workspace_id": ws_id,
            "binding_workspace_id": ws_id,
            "runtime_workspace_id": ws_id,
        },
    }
    async with db_module.async_session() as db:
        conversation = Conversation(
            entity_id=entity_id,
            workspace_id=ws_id,
            scope="workspace_main",
        )
        db.add(conversation)
        await db.flush()
        message = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="Approve this WhatsApp reply?",
            pending_action=pending_action,
        )
        db.add(message)
        await db.commit()
        message_id = message.id

    async def claim(*_args, **_kwargs):
        return {
            "acquired": True,
            "reason": "claimed",
            "claim_id": "claim-1",
            "pending_action": pending_action,
        }

    async def reject_freeform(*_args, **_kwargs):
        raise delivery.ApprovedExternalReplyDeliveryError(
            "whatsapp_template_required: approved template required",
            reason_code="whatsapp_template_required",
        )

    released: list[str] = []

    async def release(_db, *, message_id, **_kwargs):
        released.append(message_id)

    monkeypatch.setattr(delivery, "claim_external_reply_approval", claim)
    monkeypatch.setattr(delivery, "deliver_approved_external_reply", reject_freeform)
    monkeypatch.setattr(delivery, "release_external_reply_approval_claim", release)

    response = await client.post(
        f"/api/v1/workspaces/{ws_id}/chat/messages/{message_id}/resolve",
        headers=owner_headers,
        json={"choice": "approve"},
    )

    assert response.status_code == 409, response.text
    assert "approved WhatsApp template" in response.json()["detail"]
    assert released == [message_id]
    async with db_module.async_session() as db:
        message = await db.get(Message, message_id)
        assert message is not None and message.resolved_at is None


@pytest.mark.asyncio
async def test_workspace_owner_cannot_overwrite_a_shared_blueprint_skill(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.blueprints.freshness import (
        BLUEPRINT_ID_KEY,
        BLUEPRINT_SETTINGS_KEY,
        CONTENT_FINGERPRINT_KEY,
        blueprint_content_fingerprint,
    )
    from packages.core.models.skill import Skill

    admin_headers = await _auth(client, "blueprint_scope_admin")
    admin = await _me(client, admin_headers)
    entity_id = admin["entity_id"]
    workspace_owner = await _create_entity_user(
        entity_id,
        "blueprint_scope_workspace_owner",
        role="member",
    )
    payload = {
        "manifest": {
            "slug": "shared-scope-blueprint",
            "blueprint_version": "1.1",
            "name": "Shared Scope Blueprint",
        },
        "embedded": {
            "skills": [{
                "slug": "shared-scope-skill",
                "name": "Shared Scope Skill",
                "system_prompt": "Blueprint instructions",
                "tools": [],
                "input_schema": {},
                "output_format": "text",
                "config": {},
                "status": "active",
            }],
        },
        "recipe": {"workflows": []},
        "contract": {},
        "policy": {},
    }
    installed_payload = json.loads(json.dumps(payload))
    installed_payload["embedded"]["skills"][0]["system_prompt"] = "Workspace instructions"

    async with db_module.async_session() as db:
        workspace = Workspace(
            entity_id=entity_id,
            name="Blueprint Scope",
            settings={
                "access_mode": "members_only",
                BLUEPRINT_SETTINGS_KEY: {
                    BLUEPRINT_ID_KEY: "builtin:shared-scope-blueprint",
                    "blueprint_slug": "shared-scope-blueprint",
                    CONTENT_FINGERPRINT_KEY: blueprint_content_fingerprint(
                        installed_payload
                    ),
                },
            },
        )
        db.add(workspace)
        await db.flush()
        db.add(WorkspaceStaff(
            workspace_id=workspace.id,
            staff_id=None,
            user_id=workspace_owner["id"],
            role="owner",
            status="active",
        ))
        skill = Skill(
            entity_id=entity_id,
            workspace_id=None,
            owner_user_id=None,
            name="Shared Scope Skill",
            slug="shared-scope-skill",
            system_prompt="Workspace instructions",
            tools=[],
            input_schema={},
            output_format="text",
            config={},
            status="active",
            revision=4,
        )
        db.add(skill)
        await db.commit()
        workspace_id = workspace.id
        skill_id = skill.id

    async def resolved_payloads(_db, workspaces):
        return {
            candidate.id: (
                payload,
                "1.0.1",
                "builtin:shared-scope-blueprint",
            )
            for candidate in workspaces
        }

    monkeypatch.setattr(
        "apps.api.routers.workspaces._blueprint_payloads_for",
        resolved_payloads,
    )
    preview = await client.get(
        f"/api/v1/workspaces/{workspace_id}/blueprint/upgrade",
        headers=workspace_owner["headers"],
    )
    assert preview.status_code == 200, preview.text
    conflict = next(
        item for item in preview.json()["items"]
        if item["slug"] == "shared-scope-skill"
    )

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/blueprint/upgrade/v2",
        headers=workspace_owner["headers"],
        json={
            "expected_blueprint_fingerprint": preview.json()["blueprint_fingerprint"],
            "conflict_resolutions": [{
                "kind": conflict["kind"],
                "slug": conflict["slug"],
                "resolution": "use_blueprint",
                "expected_revision": conflict["revision"],
            }],
        },
    )

    assert response.status_code == 403, response.text
    async with db_module.async_session() as db:
        persisted = await db.get(Skill, skill_id)
        assert persisted is not None
        assert persisted.system_prompt == "Workspace instructions"

    admin_response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/blueprint/upgrade/v2",
        headers=admin_headers,
        json={
            "expected_blueprint_fingerprint": preview.json()["blueprint_fingerprint"],
            "conflict_resolutions": [{
                "kind": conflict["kind"],
                "slug": conflict["slug"],
                "resolution": "use_blueprint",
                "expected_revision": conflict["revision"],
            }],
        },
    )

    assert admin_response.status_code == 200, admin_response.text
    async with db_module.async_session() as db:
        persisted = await db.get(Skill, skill_id)
        assert persisted is not None
        assert persisted.system_prompt == "Blueprint instructions"


@pytest.mark.asyncio
async def test_workspace_task_agent_assignment_requires_active_workspace_subscription(db_session):
    from packages.core.models.workspace import AgentSubscription
    from packages.core.services.task_service import ensure_workspace_agent_assignment

    with pytest.raises(ValueError, match="not subscribed"):
        await ensure_workspace_agent_assignment(
            db_session,
            entity_id="entity-a",
            workspace_id="workspace-a",
            agent_id="agent-a",
        )

    db_session.add(AgentSubscription(
        entity_id="entity-a",
        workspace_id="workspace-a",
        agent_id="agent-a",
        service_key="content",
        status="active",
    ))
    await db_session.flush()
    await ensure_workspace_agent_assignment(
        db_session,
        entity_id="entity-a",
        workspace_id="workspace-a",
        agent_id="agent-a",
    )


@pytest.mark.asyncio
async def test_non_member_cannot_write_into_workspace(client: AsyncClient):
    owner_headers = await _auth(client, "wswrite_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))

    ws_id = await _make_workspace(entity_id, "Private Ops", owner_id)
    async with db_module.async_session() as db:
        t = await create_task(db, entity_id, title="ws task", workspace_id=ws_id)
        await db.commit()
        task_id = t.id

    outsider = await _create_entity_user(entity_id, "wswrite_outsider", role="member")

    # Cannot CREATE a task into a workspace they can't access.
    r = await client.post(
        "/api/v1/tasks", headers=outsider["headers"],
        json={"title": "injected", "workspace_id": ws_id},
    )
    assert r.status_code == 403, f"create into foreign workspace allowed: {r.status_code}"

    # Cannot UPDATE a task inside it.
    r = await client.put(
        f"/api/v1/tasks/{task_id}", headers=outsider["headers"],
        json={"title": "hijacked"},
    )
    assert r.status_code == 403, f"update of foreign-workspace task allowed: {r.status_code}"

    # Cannot DELETE it.
    r = await client.delete(f"/api/v1/tasks/{task_id}", headers=outsider["headers"])
    assert r.status_code == 403, f"delete of foreign-workspace task allowed: {r.status_code}"

    # The task is untouched.
    r = await client.get(f"/api/v1/tasks/{task_id}", headers=owner_headers)
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "ws task"

    # Entity-level tasks (no workspace) are still creatable by any member.
    r = await client.post(
        "/api/v1/tasks", headers=outsider["headers"], json={"title": "entity task"},
    )
    assert r.status_code == 201, f"entity-level create wrongly blocked: {r.text}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/v1/bulk/tasks/status", {"status": "in_progress"}),
        ("/api/v1/bulk/tasks/update", {"updates": {"priority": 5}}),
    ],
)
async def test_bulk_task_mutation_requires_write_access_for_every_workspace(
    client: AsyncClient,
    path: str,
    payload: dict,
):
    owner_headers = await _auth(client, f"bulk_ws_owner_{path.rsplit('/', 1)[-1]}")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    ws_id = await _make_workspace(entity_id, "Private bulk ops", owner_id)
    async with db_module.async_session() as db:
        task = await create_task(
            db,
            entity_id,
            title="Protected bulk task",
            workspace_id=ws_id,
        )
        await db.commit()
        task_id = task.id

    outsider = await _create_entity_user(
        entity_id,
        f"bulk_ws_outsider_{path.rsplit('/', 1)[-1]}",
        role="member",
    )
    response = await client.post(
        path,
        headers=outsider["headers"],
        json={"task_ids": [task_id], **payload},
    )

    assert response.status_code == 403, response.text
    async with db_module.async_session() as db:
        unchanged = await db.get(Task, task_id)
        assert unchanged is not None
        assert unchanged.status == "pending"
        assert unchanged.priority != 5


@pytest.mark.asyncio
async def test_workspace_promotion_requires_read_and_manage_access(client: AsyncClient):
    owner_headers = await _auth(client, "wspromote_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    ws_id = await _make_workspace(entity_id, "Private simulation", owner_id)
    async with db_module.async_session() as db:
        workspace = await db.get(Workspace, ws_id)
        assert workspace is not None
        workspace.settings = {**dict(workspace.settings or {}), "sandbox": True}
        await db.commit()

    outsider = await _create_entity_user(entity_id, "wspromote_outsider", role="member")

    preflight = await client.get(
        f"/api/v1/workspaces/{ws_id}/promote/preflight",
        headers=outsider["headers"],
    )
    assert preflight.status_code == 404, preflight.text

    report = await client.get(
        f"/api/v1/workspaces/{ws_id}/simulation-report",
        headers=outsider["headers"],
    )
    assert report.status_code == 404, report.text

    promote = await client.post(
        f"/api/v1/workspaces/{ws_id}/promote",
        headers=outsider["headers"],
    )
    assert promote.status_code == 403, promote.text

    owner_preflight = await client.get(
        f"/api/v1/workspaces/{ws_id}/promote/preflight",
        headers=owner_headers,
    )
    assert owner_preflight.status_code == 200, owner_preflight.text


@pytest.mark.asyncio
async def test_promotion_preflight_reports_missing_legacy_setup_contract(
    client: AsyncClient,
):
    owner_headers = await _auth(client, "wspromote_legacy_contract")
    me = await _me(client, owner_headers)
    workspace_id = await _make_workspace(me["entity_id"], "Legacy simulation", me.get("user_id") or me.get("id"))
    async with db_module.async_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.settings = {
            "sandbox": True,
            "_blueprint": {"install_mode": "simulate"},
        }
        await db.commit()

    preflight = await client.get(
        f"/api/v1/workspaces/{workspace_id}/promote/preflight",
        headers=owner_headers,
    )

    assert preflight.status_code == 200, preflight.text
    assert any(
        item["payload"].get("todo_kind") == "live_setup_contract"
        or item.get("kind") == "blueprint_install_todo"
        for item in preflight.json()
    )


@pytest.mark.asyncio
async def test_viewer_is_read_only_but_contributor_can_write(client: AsyncClient):
    owner_headers = await _auth(client, "wsrole_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    ws_id = await _make_workspace(entity_id, "Role Semantics", owner_id)

    viewer = await _create_entity_user(entity_id, "wsrole_viewer", role="member")
    contributor = await _create_entity_user(entity_id, "wsrole_contrib", role="member")
    await _add_member(ws_id, viewer["id"], "viewer")
    await _add_member(ws_id, contributor["id"], "contributor")

    # Both can READ the workspace's tasks (they are members).
    for who in (viewer, contributor):
        r = await client.get(
            f"/api/v1/tasks?workspace_id={ws_id}", headers=who["headers"]
        )
        assert r.status_code == 200, f"member denied read: {r.text}"

    # viewer: read-only — cannot create.
    r = await client.post(
        "/api/v1/tasks", headers=viewer["headers"],
        json={"title": "viewer attempt", "workspace_id": ws_id},
    )
    assert r.status_code == 403, f"viewer was allowed to write: {r.status_code}"

    # contributor: may create.
    r = await client.post(
        "/api/v1/tasks", headers=contributor["headers"],
        json={"title": "contributor task", "workspace_id": ws_id},
    )
    assert r.status_code == 201, f"contributor wrongly blocked: {r.text}"
    created_id = r.json()["id"]

    # viewer cannot modify what contributor created.
    r = await client.put(
        f"/api/v1/tasks/{created_id}", headers=viewer["headers"],
        json={"title": "viewer edit"},
    )
    assert r.status_code == 403, f"viewer was allowed to update: {r.status_code}"

    # contributor can.
    r = await client.put(
        f"/api/v1/tasks/{created_id}", headers=contributor["headers"],
        json={"title": "contributor edit"},
    )
    assert r.status_code == 200, f"contributor wrongly blocked on update: {r.text}"


@pytest.mark.asyncio
async def test_workspace_task_approval_requires_approve_tasks_authority(
    client: AsyncClient,
):
    from sqlalchemy import select

    from packages.core.constants.pending_actions import PendingActionKind
    from packages.core.models.task import Conversation, Message, Task

    owner_headers = await _auth(client, "wsapproval_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    ws_id = await _make_workspace(entity_id, "Approval authority", owner_id)
    contributor = await _create_entity_user(
        entity_id,
        "wsapproval_contributor",
        role="member",
    )
    await _add_member(ws_id, contributor["id"], "contributor")

    async with db_module.async_session() as db:
        task = await create_task(
            db,
            entity_id,
            title="Approve the launch",
            task_type="approval",
            workspace_id=ws_id,
        )
        await db.commit()
        task_id = task.id
        card = (await db.execute(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(
                Conversation.workspace_id == ws_id,
                Message.resolved_at.is_(None),
            )
        )).scalars().one()
        assert (card.pending_action or {}).get("kind") == PendingActionKind.TASK_APPROVAL.value
        card_id = card.id

    direct = await client.post(
        f"/api/v1/tasks/{task_id}/approval",
        headers=contributor["headers"],
        json={"choice": "approve"},
    )
    chat = await client.post(
        f"/api/v1/workspaces/{ws_id}/chat/messages/{card_id}/resolve",
        headers=contributor["headers"],
        json={"choice": "approve"},
    )

    assert direct.status_code == 403, direct.text
    assert chat.status_code == 403, chat.text
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        card = await db.get(Message, card_id)
        assert task is not None and task.status == "pending"
        assert card is not None and card.resolved_at is None


@pytest.mark.asyncio
async def test_workspace_viewer_cannot_resolve_task_recovery_card(
    client: AsyncClient,
):
    from packages.core.models.task import Message, Task
    from packages.core.services.task_chat_hitl import ensure_task_recovery_hitl

    owner_headers = await _auth(client, "wsrecovery_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    ws_id = await _make_workspace(entity_id, "Recovery authority", owner_id)
    viewer = await _create_entity_user(entity_id, "wsrecovery_viewer", role="member")
    await _add_member(ws_id, viewer["id"], "viewer")

    async with db_module.async_session() as db:
        task = await create_task(
            db,
            entity_id,
            title="Recover the failed run",
            workspace_id=ws_id,
        )
        task.status = "waiting_on_customer"
        card = await ensure_task_recovery_hitl(
            db,
            task,
            plan_id=None,
            prompt="The run stopped.",
            issue="Retry or cancel the task.",
        )
        await db.commit()
        assert card is not None
        task_id, card_id = task.id, card.id

    response = await client.post(
        f"/api/v1/workspaces/{ws_id}/chat/messages/{card_id}/resolve",
        headers=viewer["headers"],
        json={"choice": "cancel"},
    )

    assert response.status_code == 403, response.text
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        card = await db.get(Message, card_id)
        assert task is not None and task.status == "waiting_on_customer"
        assert card is not None and card.resolved_at is None


@pytest.mark.asyncio
async def test_workspace_viewer_cannot_submit_task_hitl_response(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.models.task import Task

    owner_headers = await _auth(client, "taskhitl_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    ws_id = await _make_workspace(entity_id, "Private HITL", owner_id)
    viewer = await _create_entity_user(entity_id, "taskhitl_viewer", role="member")
    await _add_member(ws_id, viewer["id"], "viewer")

    async with db_module.async_session() as db:
        task = await create_task(
            db,
            entity_id,
            title="Waiting for operator input",
            workspace_id=ws_id,
        )
        task.status = "waiting_on_customer"
        await db.commit()
        task_id = task.id

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "packages.core.tasks.ai_tasks.run_agent_task.delay",
        lambda task_id, agent_id: dispatched.append((task_id, agent_id)),
    )
    response = await client.post(
        f"/api/v1/tasks/{task_id}/hitl-response",
        headers=viewer["headers"],
        json={"response": "Continue"},
    )

    assert response.status_code == 403, response.text
    assert dispatched == []
    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        assert task is not None and task.status == "waiting_on_customer"


@pytest.mark.asyncio
async def test_workspace_workflow_binding_read_write_scope_preserves_entity_bindings(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Workspace bindings follow Workspace read/write roles while entity-level
    binding mutations still require edit access to the workflow definition.
    """
    from tests.test_workflows import _simple_steps

    owner_headers = await _auth(client, "wsbinding_scope_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    ws_id = await _make_workspace(entity_id, "Binding Scope", owner_id)

    workflow = await client.post(
        "/api/v1/workflows",
        headers=owner_headers,
        json={"name": "Scoped binding workflow", "steps": _simple_steps(["step"])},
    )
    assert workflow.status_code == 201, workflow.text
    workflow_id = workflow.json()["id"]
    scoped = await client.post(
        "/api/v1/workflows/bindings",
        headers=owner_headers,
        json={
            "workflow_id": workflow_id,
            "workspace_id": ws_id,
            "name": "Private binding",
            "trigger_type": "manual",
        },
    )
    assert scoped.status_code == 201, scoped.text
    binding_id = scoped.json()["id"]

    entity_binding = await client.post(
        "/api/v1/workflows/bindings",
        headers=owner_headers,
        json={"workflow_id": workflow_id, "name": "Entity binding", "trigger_type": "manual"},
    )
    assert entity_binding.status_code == 201, entity_binding.text

    other_ws_id = await _make_workspace(entity_id, "Other Binding Scope", owner_id)
    foreign = await client.post(
        "/api/v1/workflows/bindings",
        headers=owner_headers,
        json={
            "workflow_id": workflow_id,
            "workspace_id": other_ws_id,
            "name": "Foreign binding",
            "trigger_type": "manual",
        },
    )
    assert foreign.status_code == 201, foreign.text
    foreign_binding_id = foreign.json()["id"]

    viewer = await _create_entity_user(entity_id, "wsbinding_scope_viewer", role="member")
    await _add_member(ws_id, viewer["id"], "viewer")

    listed = await client.get(
        "/api/v1/workflows/bindings", headers=viewer["headers"], params={"workspace_id": ws_id}
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [binding_id]

    global_list = await client.get("/api/v1/workflows/bindings", headers=viewer["headers"])
    assert global_list.status_code == 200, global_list.text
    global_ids = {item["id"] for item in global_list.json()}
    assert binding_id in global_ids
    assert foreign_binding_id not in global_ids

    denied_create = await client.post(
        "/api/v1/workflows/bindings",
        headers=viewer["headers"],
        json={"workflow_id": workflow_id, "workspace_id": ws_id, "trigger_type": "event"},
    )
    assert denied_create.status_code == 403, denied_create.text

    denied_update = await client.put(
        f"/api/v1/workflows/bindings/{binding_id}",
        headers=viewer["headers"],
        json={"name": "Viewer changed"},
    )
    assert denied_update.status_code == 403, denied_update.text

    monkeypatch.setattr(
        "packages.core.ai.workflow_runner.WorkflowRunner.enqueue",
        staticmethod(lambda *_args, **_kwargs: None),
    )
    denied_run = await client.post(
        f"/api/v1/workflows/bindings/{binding_id}/run",
        headers=viewer["headers"],
        json={"execute": False},
    )
    assert denied_run.status_code == 403, denied_run.text

    denied_delete = await client.delete(
        f"/api/v1/workflows/bindings/{binding_id}", headers=viewer["headers"]
    )
    assert denied_delete.status_code == 403, denied_delete.text

    denied_entity_create = await client.post(
        "/api/v1/workflows/bindings",
        headers=viewer["headers"],
        json={"workflow_id": workflow_id, "trigger_type": "event"},
    )
    assert denied_entity_create.status_code == 403, denied_entity_create.text


@pytest.mark.asyncio
async def test_deleted_workspace_binding_is_not_reclassified_as_entity_level(
    client: AsyncClient,
):
    """A real Workspace id stays scoped after soft deletion.

    Bindings use ``workspace_id`` without a foreign key, so a deleted
    Workspace must not be mistaken for a historical entity-level label.
    """
    from tests.test_workflows import _simple_steps

    owner_headers = await _auth(client, "deleted_binding_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    workspace_id = await _make_workspace(entity_id, "Deleted Binding", owner_id)
    workflow = await client.post(
        "/api/v1/workflows",
        headers=owner_headers,
        json={"name": "Deleted binding flow", "steps": _simple_steps(["step"])},
    )
    assert workflow.status_code == 201, workflow.text
    binding = await client.post(
        "/api/v1/workflows/bindings",
        headers=owner_headers,
        json={
            "workflow_id": workflow.json()["id"],
            "workspace_id": workspace_id,
            "trigger_type": "manual",
        },
    )
    assert binding.status_code == 201, binding.text
    binding_id = binding.json()["id"]

    deleted = await client.delete(
        f"/api/v1/workspaces/{workspace_id}", headers=owner_headers
    )
    assert deleted.status_code == 204, deleted.text

    all_bindings = await client.get("/api/v1/workflows/bindings", headers=owner_headers)
    assert all_bindings.status_code == 200, all_bindings.text
    assert binding_id not in {item["id"] for item in all_bindings.json()}

    scoped_bindings = await client.get(
        "/api/v1/workflows/bindings",
        headers=owner_headers,
        params={"workspace_id": workspace_id},
    )
    assert scoped_bindings.status_code == 404, scoped_bindings.text


@pytest.mark.asyncio
async def test_workspace_workflow_import_requires_write_access_for_binding(
    client: AsyncClient,
):
    """Importing a binding is the same Workspace write as creating one."""
    owner_headers = await _auth(client, "binding_import_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    workspace_id = await _make_workspace(entity_id, "Import Scope", owner_id)
    viewer = await _create_entity_user(entity_id, "binding_import_viewer", role="member")
    await _add_member(workspace_id, viewer["id"], "viewer")

    imported = await client.post(
        "/api/v1/workflows/import",
        headers=viewer["headers"],
        json={
            "content": json.dumps({
                "name": "Imported scoped flow",
                "nodes": [
                    {"name": "Webhook", "type": "n8n-nodes-base.webhook", "parameters": {}},
                    {"name": "HTTP", "type": "n8n-nodes-base.httpRequest", "parameters": {}},
                ],
                "connections": {
                    "Webhook": {"main": [[{"node": "HTTP", "type": "main", "index": 0}]]},
                },
            }),
            "workspace_id": workspace_id,
            "create_binding": True,
        },
    )
    assert imported.status_code == 403, imported.text


@pytest.mark.asyncio
async def test_soft_deleted_workspace_rejects_owner_binding_writes(
    client: AsyncClient,
):
    """Soft deletion stops new Workspace work even for entity admins."""
    from tests.test_workflows import _simple_steps

    owner_headers = await _auth(client, "deleted_owner_write")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    workspace_id = await _make_workspace(entity_id, "Deleted Owner Write", owner_id)

    async with db_module.async_session() as db:
        await db.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id, Workspace.entity_id == entity_id)
            .values(deleted_at=datetime.now(UTC))
        )
        await db.commit()

    workflow = await client.post(
        "/api/v1/workflows",
        headers=owner_headers,
        json={"name": "Deleted owner flow", "steps": _simple_steps(["step"])},
    )
    assert workflow.status_code == 201, workflow.text
    binding = await client.post(
        "/api/v1/workflows/bindings",
        headers=owner_headers,
        json={
            "workflow_id": workflow.json()["id"],
            "workspace_id": workspace_id,
            "trigger_type": "manual",
        },
    )
    assert binding.status_code == 403, binding.text


@pytest.mark.asyncio
async def test_binding_rejects_workspace_id_from_another_entity(
    client: AsyncClient,
):
    """A persisted Workspace id from another Entity is never an opaque label."""
    from tests.test_workflows import _simple_steps

    foreign_headers = await _auth(client, "foreign_binding_workspace_owner")
    foreign_me = await _me(client, foreign_headers)
    foreign_workspace_id = await _make_workspace(
        foreign_me["entity_id"],
        "Foreign binding target",
        foreign_me.get("user_id") or foreign_me.get("id"),
    )

    local_headers = await _auth(client, "local_binding_workspace_owner")
    workflow = await client.post(
        "/api/v1/workflows",
        headers=local_headers,
        json={"name": "Cross entity flow", "steps": _simple_steps(["step"])},
    )
    assert workflow.status_code == 201, workflow.text
    binding = await client.post(
        "/api/v1/workflows/bindings",
        headers=local_headers,
        json={
            "workflow_id": workflow.json()["id"],
            "workspace_id": foreign_workspace_id,
            "trigger_type": "manual",
        },
    )
    assert binding.status_code == 403, binding.text


@pytest.mark.asyncio
async def test_non_member_cannot_read_task_detail_logs_or_attachment(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    from packages.core.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    owner_headers = await _auth(client, "wsdetail_owner")
    me = await _me(client, owner_headers)
    ws_id = await _make_workspace(me["entity_id"], "Private Detail", me.get("user_id") or me.get("id"))

    created = await client.post(
        "/api/v1/tasks",
        headers=owner_headers,
        json={"title": "private task", "workspace_id": ws_id},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    uploaded = await client.post(
        f"/api/v1/tasks/{task_id}/attachments",
        headers=owner_headers,
        files={"file": ("private.txt", b"private attachment", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    filename = uploaded.json()["filename"]

    outsider = await _create_entity_user(me["entity_id"], "wsdetail_outsider", role="member")
    for path in (
        f"/api/v1/tasks/{task_id}",
        f"/api/v1/tasks/{task_id}/history",
        f"/api/v1/tasks/{task_id}/logs",
        f"/api/v1/tasks/{task_id}/attachments/{filename}",
    ):
        response = await client.get(path, headers=outsider["headers"])
        assert response.status_code == 404, f"private task path leaked: {path} {response.status_code}"

    response = await client.post(
        f"/api/v1/tasks/{task_id}/logs",
        headers=outsider["headers"],
        json={"content": "leak", "log_type": "comment"},
    )
    assert response.status_code == 403

    response = await client.post(
        f"/api/v1/tasks/{task_id}/attachments",
        headers=outsider["headers"],
        files={"file": ("blocked.txt", b"blocked", "text/plain")},
    )
    assert response.status_code == 403
