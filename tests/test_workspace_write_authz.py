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

import asyncio
import json
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import update

import packages.core.database as db_module
from packages.core.models.workspace import Workspace, WorkspaceStaff
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
    assert standing.status_code == 403, standing.text
    assert "manage_standing_grants" in standing.json()["detail"]

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
async def test_workspace_plan_approval_requires_approve_tasks_authority(
    client: AsyncClient,
):
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.base import generate_ulid

    owner_headers = await _auth(client, "wsplanapproval_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    ws_id = await _make_workspace(entity_id, "Plan approval authority", owner_id)
    contributor = await _create_entity_user(
        entity_id,
        "wsplanapproval_contributor",
        role="member",
    )
    await _add_member(ws_id, contributor["id"], "contributor")
    plan_id = generate_ulid()
    async with db_module.async_session() as db:
        db.add(ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            workspace_id=ws_id,
            status="pending_approval",
            execution_mode="live",
            approval_required=True,
            plan_dag={"steps": []},
        ))
        await db.commit()

    response = await client.post(
        f"/api/v1/plans/{plan_id}/approve",
        headers=contributor["headers"],
    )

    assert response.status_code == 403, response.text
    async with db_module.async_session() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        assert plan is not None and plan.status == "pending_approval"
        assert plan.approval_required is True


@pytest.mark.asyncio
async def test_workspace_plan_detail_steps_and_cancel_enforce_workspace_access(
    client: AsyncClient,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep

    owner_headers = await _auth(client, "wsplanaccess_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    ws_id = await _make_workspace(entity_id, "Private plan access", owner_id)
    outsider = await _create_entity_user(
        entity_id,
        "wsplanaccess_outsider",
        role="member",
    )
    viewer = await _create_entity_user(
        entity_id,
        "wsplanaccess_viewer",
        role="member",
    )
    await _add_member(ws_id, viewer["id"], "viewer")

    plan_id, step_id = generate_ulid(), generate_ulid()
    async with db_module.async_session() as db:
        db.add(ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            workspace_id=ws_id,
            status="draft",
            execution_mode="live",
            approval_required=False,
            plan_dag={"steps": []},
        ))
        db.add(ExecutionStep(
            id=step_id,
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=ws_id,
            step_key="private_step",
            kind="human",
            params={"prompt": "Wait"},
            depends_on=[],
            step_status="pending",
        ))
        await db.commit()

    assert (
        await client.get(
            f"/api/v1/plans/{plan_id}",
            headers=outsider["headers"],
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/plans/{plan_id}/steps",
            headers=outsider["headers"],
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/plans/{plan_id}/cancel",
            headers=outsider["headers"],
        )
    ).status_code == 403

    assert (
        await client.get(
            f"/api/v1/plans/{plan_id}",
            headers=viewer["headers"],
        )
    ).status_code == 200
    assert (
        await client.get(
            f"/api/v1/plans/{plan_id}/steps",
            headers=viewer["headers"],
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/plans/{plan_id}/cancel",
            headers=viewer["headers"],
        )
    ).status_code == 403

    async with db_module.async_session() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        step = await db.get(ExecutionStep, step_id)
        assert plan is not None and plan.status == "draft"
        assert step is not None and step.step_status == "pending"


@pytest.mark.asyncio
async def test_plan_creation_authorizes_task_before_planner_or_persistence(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from sqlalchemy import select

    from packages.core.models.execution import ExecutionPlan

    owner_headers = await _auth(client, "wsplancreate_owner")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    ws_id = await _make_workspace(entity_id, "Private plan creation", owner_id)
    outsider = await _create_entity_user(
        entity_id,
        "wsplancreate_outsider",
        role="member",
    )
    async with db_module.async_session() as db:
        task = await create_task(
            db,
            entity_id,
            title="Do not plan for outsider",
            workspace_id=ws_id,
        )
        await db.commit()
        task_id = task.id

    planner_calls = []

    async def unexpected_planner(*args, **kwargs):
        planner_calls.append((args, kwargs))
        raise AssertionError("authorization must run before Planner/provider work")

    monkeypatch.setattr(
        "apps.api.routers.plans.plan_task_and_commit",
        unexpected_planner,
    )
    from_task = await client.post(
        f"/api/v1/plans/from-task/{task_id}",
        headers=outsider["headers"],
        json={"execution_mode": "live"},
    )
    assert from_task.status_code == 403, from_task.text

    manual = await client.post(
        "/api/v1/plans",
        headers=outsider["headers"],
        json={
            "task_id": task_id,
            "workspace_id": ws_id,
            "plan": {
                "steps": [{
                    "key": "wait_for_owner",
                    "kind": "human",
                    "params": {"prompt": "Wait for the owner"},
                }],
            },
        },
    )
    assert manual.status_code == 403, manual.text
    assert planner_calls == []

    async with db_module.async_session() as db:
        plans = list((await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.task_id == task_id)
        )).scalars())
        assert plans == []


@pytest.mark.asyncio
async def test_manual_plan_rejects_cross_workspace_task_and_subscription_refs(
    client: AsyncClient,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import AgentSubscription

    headers = await _auth(client, "wsplanref_owner")
    me = await _me(client, headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    source_ws = await _make_workspace(entity_id, "Plan ref source", owner_id)
    target_ws = await _make_workspace(entity_id, "Plan ref target", owner_id)
    async with db_module.async_session() as db:
        task = await create_task(
            db,
            entity_id,
            title="Source workspace task",
            workspace_id=source_ws,
        )
        subscription = AgentSubscription(
            entity_id=entity_id,
            agent_id=generate_ulid(),
            workspace_id=source_ws,
            status="active",
        )
        db.add(subscription)
        await db.commit()
        task_id = task.id
        subscription_id = subscription.id

    base_payload = {
        "workspace_id": target_ws,
        "plan": {
            "steps": [{
                "key": "wait_for_scope",
                "kind": "human",
                "params": {"prompt": "Wait"},
            }],
        },
    }
    task_mismatch = await client.post(
        "/api/v1/plans",
        headers=headers,
        json={**base_payload, "task_id": task_id},
    )
    assert task_mismatch.status_code == 400, task_mismatch.text

    subscription_mismatch = await client.post(
        "/api/v1/plans",
        headers=headers,
        json={
            **base_payload,
            "agent_subscription_id": subscription_id,
        },
    )
    assert subscription_mismatch.status_code == 400, subscription_mismatch.text


@pytest.mark.asyncio
async def test_manual_plan_serializes_with_agent_unsubscribe(client: AsyncClient):
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.workspace import AgentSubscription
    from packages.core.services.agent_service import unsubscribe_agent

    headers = await _auth(client, "planunsubscriberace")
    me = await _me(client, headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    workspace_id = await _make_workspace(
        entity_id,
        "Plan unsubscribe race",
        owner_id,
    )
    async with db_module.async_session() as db:
        subscription = AgentSubscription(
            entity_id=entity_id,
            agent_id=generate_ulid(),
            workspace_id=workspace_id,
            status="active",
        )
        db.add(subscription)
        await db.commit()
        subscription_id = subscription.id

    async with db_module.async_session() as cancelling_db:
        assert await unsubscribe_agent(
            cancelling_db,
            subscription_id,
            entity_id,
        ) is True
        request_task = asyncio.create_task(client.post(
            "/api/v1/plans",
            headers=headers,
            json={
                "workspace_id": workspace_id,
                "agent_subscription_id": subscription_id,
                "plan": {
                    "steps": [{
                        "key": "wait_for_scope",
                        "kind": "human",
                        "params": {"prompt": "Wait"},
                    }],
                },
            },
        ))
        await asyncio.sleep(0.1)
        assert request_task.done() is False
        await cancelling_db.commit()

    response = await asyncio.wait_for(request_task, timeout=5)
    assert response.status_code == 400, response.text
    assert "not active" in response.text
    async with db_module.async_session() as db:
        plans = list((await db.execute(
            select(ExecutionPlan).where(
                ExecutionPlan.agent_subscription_id == subscription_id,
            )
        )).scalars())
        assert plans == []


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["from_task", "manual"])
async def test_plan_creation_serializes_with_workspace_delete(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
):
    from sqlalchemy import select

    from packages.core.models.execution import ExecutionPlan
    from packages.core.services.entity_service import soft_delete_workspace

    headers = await _auth(client, f"plandeleterace_{surface}")
    me = await _me(client, headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    workspace_id = await _make_workspace(
        entity_id,
        f"Plan delete race {surface}",
        owner_id,
    )
    async with db_module.async_session() as db:
        task = await create_task(
            db,
            entity_id,
            title="Must not cross Workspace deletion",
            workspace_id=workspace_id,
        )
        await db.commit()
        task_id = task.id

    planner_calls: list[str] = []

    async def unexpected_planner(*_args, **_kwargs):
        planner_calls.append(task_id)
        raise AssertionError("Workspace deletion must win before Planner work")

    monkeypatch.setattr(
        "apps.api.routers.plans.plan_task_and_commit",
        unexpected_planner,
    )

    async with db_module.async_session() as deleting_db:
        assert await soft_delete_workspace(
            deleting_db,
            workspace_id,
            entity_id,
        ) is True
        if surface == "from_task":
            request_task = asyncio.create_task(client.post(
                f"/api/v1/plans/from-task/{task_id}",
                headers=headers,
                json={"execution_mode": "live"},
            ))
        else:
            request_task = asyncio.create_task(client.post(
                "/api/v1/plans",
                headers=headers,
                json={
                    "task_id": task_id,
                    "workspace_id": workspace_id,
                    "plan": {
                        "steps": [{
                            "key": "wait_for_scope",
                            "kind": "human",
                            "params": {"prompt": "Wait"},
                        }],
                    },
                },
            ))

        await asyncio.sleep(0.1)
        assert request_task.done() is False
        await deleting_db.commit()

    response = await asyncio.wait_for(request_task, timeout=5)
    assert response.status_code == 403, response.text
    assert planner_calls == []
    async with db_module.async_session() as db:
        plans = list((await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.task_id == task_id)
        )).scalars())
        assert plans == []


@pytest.mark.asyncio
@pytest.mark.parametrize("with_task", [False, True])
async def test_plan_cancel_serializes_with_workspace_delete(
    client: AsyncClient,
    with_task: bool,
):
    from packages.core.models.execution import ExecutionPlan
    from packages.core.services.entity_service import soft_delete_workspace

    headers = await _auth(
        client,
        f"plancanceldeleterace_{'task' if with_task else 'standalone'}",
    )
    me = await _me(client, headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    workspace_id = await _make_workspace(
        entity_id,
        f"Plan cancel delete race {with_task}",
        owner_id,
    )
    async with db_module.async_session() as db:
        task = None
        if with_task:
            task = await create_task(
                db,
                entity_id,
                title="Do not cancel after Workspace deletion",
                workspace_id=workspace_id,
            )
        plan = ExecutionPlan(
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task.id if task else None,
            status="draft",
            execution_mode="live",
            approval_required=False,
            plan_dag={"steps": []},
        )
        db.add(plan)
        await db.commit()
        plan_id = plan.id

    async with db_module.async_session() as deleting_db:
        assert await soft_delete_workspace(
            deleting_db,
            workspace_id,
            entity_id,
        ) is True
        request_task = asyncio.create_task(client.post(
            f"/api/v1/plans/{plan_id}/cancel",
            headers=headers,
        ))

        await asyncio.sleep(0.1)
        assert request_task.done() is False
        await deleting_db.commit()

    response = await asyncio.wait_for(request_task, timeout=5)
    assert response.status_code == 403, response.text
    async with db_module.async_session() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        assert plan is not None and plan.status == "draft"


@pytest.mark.asyncio
async def test_background_planner_revalidates_task_after_workspace_lock(
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Task
    from packages.core.plans import planner as planner_module
    from packages.core.services import workspace_access

    entity_id = generate_ulid()
    async with db_module.async_session() as db:
        workspace = Workspace(
            entity_id=entity_id,
            name="Background Planner Task race",
            status="active",
        )
        db.add(workspace)
        await db.flush()
        task = await create_task(
            db,
            entity_id,
            title="Cancel before Planner admission",
            workspace_id=workspace.id,
        )
        await db.commit()
        task_id = task.id

    lock_requested = asyncio.Event()
    continue_planning = asyncio.Event()
    real_lock = workspace_access.lock_workspace_access_boundary

    async def delayed_workspace_lock(*args, **kwargs):
        lock_requested.set()
        await continue_planning.wait()
        return await real_lock(*args, **kwargs)

    async def unexpected_context(*_args, **_kwargs):
        raise AssertionError("terminal Task must not reach Planner context assembly")

    monkeypatch.setattr(
        workspace_access,
        "lock_workspace_access_boundary",
        delayed_workspace_lock,
    )
    monkeypatch.setattr(planner_module, "_gather_context", unexpected_context)

    async def plan_in_background():
        async with db_module.async_session() as db:
            return await planner_module.plan_task(db, task_id)

    planning = asyncio.create_task(plan_in_background())
    await asyncio.wait_for(lock_requested.wait(), timeout=5)
    async with db_module.async_session() as db:
        await db.execute(
            update(Task).where(Task.id == task_id).values(status="cancelled")
        )
        await db.commit()
    continue_planning.set()

    with pytest.raises(planner_module.PlannerError, match="cancelled|not active"):
        await asyncio.wait_for(planning, timeout=5)


@pytest.mark.asyncio
async def test_background_planner_releases_lifecycle_lock_during_generation(
    monkeypatch: pytest.MonkeyPatch,
):
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan
    from packages.core.plans import planner as planner_module
    from packages.core.plans.schema import Plan, PlanStep

    entity_id = generate_ulid()
    async with db_module.async_session() as db:
        workspace = Workspace(
            entity_id=entity_id,
            name="Planner releases lifecycle lock",
            status="active",
        )
        db.add(workspace)
        await db.flush()
        task = await create_task(
            db,
            entity_id,
            title="Generate without holding lifecycle locks",
            workspace_id=workspace.id,
        )
        task.status = "in_progress"
        await db.commit()
        workspace_id = workspace.id
        task_id = task.id

    provider_started = asyncio.Event()
    release_provider = asyncio.Event()
    planning_session = None

    async def delayed_generate(*_args, **_kwargs):
        assert planning_session is not None
        assert planning_session.in_transaction() is False
        provider_started.set()
        await release_provider.wait()
        return Plan(steps=[PlanStep(
            key="wait_for_operator",
            kind="human",
            params={"prompt": "Wait for operator confirmation."},
        )])

    monkeypatch.setattr(planner_module, "_generate_plan", delayed_generate)

    async def plan_in_background():
        nonlocal planning_session
        async with db_module.async_session() as db:
            planning_session = db
            return await planner_module.plan_task(db, task_id)

    planning = asyncio.create_task(plan_in_background())
    await asyncio.wait_for(provider_started.wait(), timeout=5)
    try:
        async with db_module.async_session() as deleting_db:
            deleted = await asyncio.wait_for(deleting_db.execute(
                update(Workspace)
                .where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                )
                .values(deleted_at=datetime.now(UTC))
            ), timeout=2)
            assert deleted.rowcount == 1
            # A Planner that still holds the Workspace row lock would make
            # this commit wait until provider generation is released.
            await asyncio.wait_for(deleting_db.commit(), timeout=2)
    finally:
        release_provider.set()

    with pytest.raises(planner_module.PlannerError, match="not active|cancelled"):
        await asyncio.wait_for(planning, timeout=5)

    async with db_module.async_session() as db:
        plans = list((await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.task_id == task_id)
        )).scalars())
        assert plans == []


@pytest.mark.asyncio
async def test_background_planner_rejects_task_inputs_changed_during_generation(
    monkeypatch: pytest.MonkeyPatch,
):
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.task import Task
    from packages.core.plans import planner as planner_module
    from packages.core.plans.schema import Plan, PlanStep

    entity_id = generate_ulid()
    async with db_module.async_session() as db:
        workspace = Workspace(
            entity_id=entity_id,
            name="Planner Task input race",
            status="active",
        )
        db.add(workspace)
        await db.flush()
        task = await create_task(
            db,
            entity_id,
            title="Original planning request",
            workspace_id=workspace.id,
        )
        task.status = "in_progress"
        await db.commit()
        task_id = task.id

    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def delayed_generate(*_args, **_kwargs):
        provider_started.set()
        await release_provider.wait()
        return Plan(steps=[PlanStep(
            key="wait_for_operator",
            kind="human",
            params={"prompt": "Wait for operator confirmation."},
        )])

    monkeypatch.setattr(planner_module, "_generate_plan", delayed_generate)

    async def plan_in_background():
        async with db_module.async_session() as db:
            return await planner_module.plan_task(db, task_id)

    planning = asyncio.create_task(plan_in_background())
    await asyncio.wait_for(provider_started.wait(), timeout=5)
    try:
        async with db_module.async_session() as editing_db:
            await editing_db.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    title="Revised planning request",
                    details={
                        "_replan_context": {
                            "approval_constraints": [{"step_key": "review"}],
                            "prior_plan_id": "newer-plan-lineage",
                        }
                    },
                )
            )
            await editing_db.commit()
    finally:
        release_provider.set()

    with pytest.raises(planner_module.PlannerError, match="inputs changed"):
        await asyncio.wait_for(planning, timeout=5)

    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        plans = list((await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.task_id == task_id)
        )).scalars())
        assert task is not None
        assert task.title == "Revised planning request"
        assert task.details["_replan_context"]["prior_plan_id"] == (
            "newer-plan-lineage"
        )
        assert plans == []


@pytest.mark.asyncio
async def test_task_plan_persistence_allows_only_one_active_plan():
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan
    from packages.core.plans.schema import Plan, PlanStep
    from packages.core.plans.service import (
        ActiveTaskPlanError,
        create_plan_from_dag,
    )

    entity_id = generate_ulid()
    async with db_module.async_session() as setup_db:
        workspace = Workspace(
            entity_id=entity_id,
            name="Single active Task Plan",
            status="active",
        )
        setup_db.add(workspace)
        await setup_db.flush()
        task = await create_task(
            setup_db,
            entity_id,
            title="Persist exactly one active Plan",
            workspace_id=workspace.id,
        )
        task.status = "in_progress"
        await setup_db.commit()
        workspace_id = workspace.id
        task_id = task.id

    def human_plan() -> Plan:
        return Plan(steps=[PlanStep(
            key="operator_step",
            kind="human",
            params={"prompt": "Confirm the result."},
        )])

    second_started = asyncio.Event()

    async def create_second_plan():
        async with db_module.async_session() as second_db:
            second_started.set()
            return await create_plan_from_dag(
                second_db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                agent_subscription_id=None,
                plan=human_plan(),
            )

    async with db_module.async_session() as first_db:
        first_plan = await create_plan_from_dag(
            first_db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            agent_subscription_id=None,
            plan=human_plan(),
        )
        second = asyncio.create_task(create_second_plan())
        await asyncio.wait_for(second_started.wait(), timeout=5)
        await asyncio.sleep(0.1)
        assert second.done() is False
        first_plan_id = first_plan.id
        await first_db.commit()

    with pytest.raises(ActiveTaskPlanError) as exc_info:
        await asyncio.wait_for(second, timeout=5)
    assert exc_info.value.plan_id == first_plan_id

    async with db_module.async_session() as db:
        plans = list((await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.task_id == task_id)
        )).scalars())
        assert [plan.id for plan in plans] == [first_plan_id]
        plans[0].status = "replanned"
        await db.commit()

        replacement = await create_plan_from_dag(
            db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            agent_subscription_id=None,
            plan=human_plan(),
        )
        await db.commit()
        replacement_id = replacement.id

    async with db_module.async_session() as db:
        plans = list((await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.task_id == task_id)
        )).scalars())
        assert {plan.id: plan.status for plan in plans} == {
            first_plan_id: "replanned",
            replacement_id: "draft",
        }


@pytest.mark.asyncio
async def test_planner_reuses_active_plan_before_provider_work(monkeypatch):
    from packages.core.models.base import generate_ulid
    from packages.core.plans import planner as planner_module
    from packages.core.plans.schema import Plan, PlanStep
    from packages.core.plans.service import (
        ActiveTaskPlanError,
        create_plan_from_dag,
    )

    entity_id = generate_ulid()
    async with db_module.async_session() as db:
        workspace = Workspace(
            entity_id=entity_id,
            name="No duplicate Planner spend",
            status="active",
        )
        db.add(workspace)
        await db.flush()
        task = await create_task(
            db,
            entity_id,
            title="Reuse the committed Plan",
            workspace_id=workspace.id,
        )
        task.status = "in_progress"
        plan = await create_plan_from_dag(
            db,
            entity_id=entity_id,
            workspace_id=workspace.id,
            task_id=task.id,
            agent_subscription_id=None,
            plan=Plan(steps=[PlanStep(
                key="operator_step",
                kind="human",
                params={"prompt": "Confirm the result."},
            )]),
        )
        await db.commit()
        task_id = task.id
        plan_id = plan.id

    async def _unexpected_generate(*_args, **_kwargs):
        raise AssertionError("an active Plan must prevent another provider call")

    async def _unexpected_preflight():
        raise AssertionError("an active Plan must not re-enter the credit gate")

    monkeypatch.setattr(planner_module, "_generate_plan", _unexpected_generate)

    async with db_module.async_session() as db:
        with pytest.raises(ActiveTaskPlanError) as exc_info:
            await planner_module.plan_task_and_commit(
                db,
                task_id,
                before_provider=_unexpected_preflight,
            )

    assert exc_info.value.plan_id == plan_id


@pytest.mark.asyncio
async def test_api_and_worker_planning_share_one_billable_claim(monkeypatch):
    from contextlib import asynccontextmanager

    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan
    from packages.core.plans import planner as planner_module
    from packages.core.plans.schema import Plan, PlanStep

    entity_id = generate_ulid()
    async with db_module.async_session() as setup_db:
        workspace = Workspace(
            entity_id=entity_id,
            name="Single-flight Task planning",
            status="active",
        )
        setup_db.add(workspace)
        await setup_db.flush()
        task = await create_task(
            setup_db,
            entity_id,
            title="Bill the Planner once",
            workspace_id=workspace.id,
        )
        task.status = "in_progress"
        await setup_db.commit()
        task_id = task.id

    provider_started = asyncio.Event()
    release_provider = asyncio.Event()
    provider_calls = 0

    async def _delayed_generate(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        provider_started.set()
        await release_provider.wait()
        return Plan(steps=[PlanStep(
            key="operator_step",
            kind="human",
            params={"prompt": "Confirm the result."},
        )])

    @asynccontextmanager
    async def _no_billing(*_args, **_kwargs):
        yield

    monkeypatch.setattr(planner_module, "_generate_plan", _delayed_generate)
    monkeypatch.setattr(
        planner_module,
        "runtime_planner_llm_billing_context",
        _no_billing,
    )

    async def _first_planner():
        async with db_module.async_session() as db:
            return await planner_module.plan_task_and_commit(db, task_id)

    first = asyncio.create_task(_first_planner())
    await asyncio.wait_for(provider_started.wait(), timeout=5)
    try:
        async with db_module.async_session() as duplicate_db:
            with pytest.raises(planner_module.TaskPlanClaimHeldError):
                await asyncio.wait_for(
                    planner_module.plan_task_and_commit(duplicate_db, task_id),
                    timeout=5,
                )
    finally:
        release_provider.set()

    first_plan = await asyncio.wait_for(first, timeout=5)
    assert provider_calls == 1

    async with db_module.async_session() as db:
        plans = list((await db.execute(
            select(ExecutionPlan).where(ExecutionPlan.task_id == task_id)
        )).scalars())
        assert [plan.id for plan in plans] == [first_plan.id]


@pytest.mark.asyncio
async def test_planning_failure_cannot_overwrite_a_concurrent_plan_commit():
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.task import Task
    from packages.core.plans.schema import Plan, PlanStep
    from packages.core.plans.service import create_plan_from_dag
    from packages.core.tasks.ai_tasks import _apply_task_planning_failure

    entity_id = generate_ulid()
    async with db_module.async_session() as setup_db:
        workspace = Workspace(
            entity_id=entity_id,
            name="Planning failure serialization",
            status="active",
        )
        setup_db.add(workspace)
        await setup_db.flush()
        task = await create_task(
            setup_db,
            entity_id,
            title="Keep the committed Plan authoritative",
            workspace_id=workspace.id,
        )
        task.status = "in_progress"
        await setup_db.commit()
        workspace_id = workspace.id
        task_id = task.id

    failure_started = asyncio.Event()

    async def mark_failed():
        async with db_module.async_session() as failure_db:
            failure_started.set()
            result = await _apply_task_planning_failure(
                failure_db,
                task_id,
                "stale planner failed",
                error_type="PlannerError",
            )
            await failure_db.commit()
            return result

    async with db_module.async_session() as planning_db:
        plan = await create_plan_from_dag(
            planning_db,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            agent_subscription_id=None,
            plan=Plan(steps=[PlanStep(
                key="operator_step",
                kind="human",
                params={"prompt": "Confirm the result."},
            )]),
        )
        failure = asyncio.create_task(mark_failed())
        await asyncio.wait_for(failure_started.wait(), timeout=5)
        await asyncio.sleep(0.1)
        assert failure.done() is False
        plan_id = plan.id
        await planning_db.commit()

    assert await asyncio.wait_for(failure, timeout=5) is None

    async with db_module.async_session() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        assert task.status == "in_progress"
        assert task.actual_output is None
        persisted_plan = await db.get(ExecutionPlan, plan_id)
        assert persisted_plan is not None
        assert persisted_plan.status == "draft"


@pytest.mark.asyncio
async def test_plan_api_returns_client_error_for_capability_rejection(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.plans.planner import CapabilityError

    headers = await _auth(client, "plan_capability_rejection")
    me = await _me(client, headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    workspace_id = await _make_workspace(
        entity_id,
        "Plan capability rejection",
        owner_id,
    )
    async with db_module.async_session() as db:
        task = await create_task(
            db,
            entity_id,
            title="Reject unavailable capability",
            workspace_id=workspace_id,
        )
        task.status = "in_progress"
        await db.commit()
        task_id = task.id

    async def reject_capability(*_args, **_kwargs):
        raise CapabilityError("skill is no longer callable")

    monkeypatch.setattr(
        "apps.api.routers.plans.plan_task_and_commit",
        reject_capability,
    )

    response = await client.post(
        f"/api/v1/plans/from-task/{task_id}",
        headers=headers,
        json={"execution_mode": "live"},
    )

    assert response.status_code == 400, response.text
    assert "capability validation failed" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_type", "task_status"),
    [
        ("interactive", "pending"),
        ("general", "created"),
        ("general", "proposed"),
        ("general", "pending"),
        ("general", "scheduled"),
        ("general", "waiting_on_customer"),
        ("general", "on_hold"),
        ("general", "blocked"),
        ("general", "completed"),
        ("general", "cancelled"),
        ("general", "failed"),
    ],
)
async def test_plan_entries_reject_task_session_and_non_runnable_tasks(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    task_type: str,
    task_status: str,
):
    from packages.core.models.task import Task

    headers = await _auth(client, f"planentry_{task_type}_{task_status}")
    me = await _me(client, headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    workspace_id = await _make_workspace(
        entity_id,
        f"Plan entry {task_type} {task_status}",
        owner_id,
    )
    async with db_module.async_session() as db:
        task = Task(
            entity_id=entity_id,
            title="Unsupported Plan entry",
            workspace_id=workspace_id,
            task_type=task_type,
            status=task_status,
            priority=3,
            details={},
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    planner_calls: list[str] = []

    async def unexpected_planner(*_args, **_kwargs):
        planner_calls.append(task_id)
        raise AssertionError("unsupported Task must not reach Planner")

    monkeypatch.setattr(
        "apps.api.routers.plans.plan_task_and_commit",
        unexpected_planner,
    )
    from_task = await client.post(
        f"/api/v1/plans/from-task/{task_id}",
        headers=headers,
        json={"execution_mode": "live"},
    )
    manual = await client.post(
        "/api/v1/plans",
        headers=headers,
        json={
            "task_id": task_id,
            "workspace_id": workspace_id,
            "plan": {
                "steps": [{
                    "key": "wait_for_scope",
                    "kind": "human",
                    "params": {"prompt": "Wait"},
                }],
            },
        },
    )

    assert from_task.status_code == 409, from_task.text
    assert manual.status_code == 409, manual.text
    assert planner_calls == []


@pytest.mark.asyncio
async def test_manual_task_plan_rejects_service_outside_owner_delegate_scope(
    client: AsyncClient,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import AgentSubscription

    headers = await _auth(client, "manual_plan_delegate_scope")
    me = await _me(client, headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    workspace_id = await _make_workspace(entity_id, "Manual Plan delegates", owner_id)
    async with db_module.async_session() as db:
        task = await create_task(
            db,
            entity_id,
            title="Content-owned work",
            workspace_id=workspace_id,
            owner_service_key="content",
        )
        task.status = "in_progress"
        finance = AgentSubscription(
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=generate_ulid(),
            service_key="finance",
            status="active",
        )
        db.add(finance)
        await db.commit()
        task_id, finance_id = task.id, finance.id

    response = await client.post(
        "/api/v1/plans",
        headers=headers,
        json={
            "task_id": task_id,
            "workspace_id": workspace_id,
            "agent_subscription_id": finance_id,
            "plan": {
                "steps": [{
                    "key": "analyze_finance",
                    "kind": "llm",
                    "service_key": "finance",
                    "params": {"prompt": "Analyze the numbers"},
                    "output_shape": "TextResult",
                }],
            },
        },
    )

    assert response.status_code == 400, response.text
    assert "owner/delegate" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["step", "subscription"])
async def test_manual_task_plan_rejects_service_when_assignment_scope_is_empty(
    client: AsyncClient,
    reference: str,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import AgentSubscription

    headers = await _auth(client, f"manual_plan_empty_scope_{reference}")
    me = await _me(client, headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    workspace_id = await _make_workspace(entity_id, "Empty Task assignment", owner_id)
    async with db_module.async_session() as db:
        task = await create_task(
            db,
            entity_id,
            title="Human-owned unassigned work",
            workspace_id=workspace_id,
        )
        task.status = "in_progress"
        finance = AgentSubscription(
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=generate_ulid(),
            service_key="finance",
            status="active",
        )
        db.add(finance)
        await db.commit()
        task_id, finance_id = task.id, finance.id

    payload = {
        "task_id": task_id,
        "workspace_id": workspace_id,
        "plan": {
            "steps": [{
                "key": "perform_work",
                "kind": "human" if reference == "subscription" else "llm",
                "service_key": None if reference == "subscription" else "finance",
                "params": {"prompt": "Perform the work"},
                **({} if reference == "subscription" else {"output_shape": "TextResult"}),
            }],
        },
    }
    if reference == "subscription":
        payload["agent_subscription_id"] = finance_id

    response = await client.post("/api/v1/plans", headers=headers, json=payload)

    assert response.status_code == 400, response.text
    assert "owner/delegate" in response.text


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
@pytest.mark.parametrize("retry_surface", ["plan", "step"])
async def test_workspace_viewer_cannot_retry_plan_execution(
    client: AsyncClient,
    retry_surface: str,
):
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.base import generate_ulid

    owner_headers = await _auth(client, f"planretry_owner_{retry_surface}")
    me = await _me(client, owner_headers)
    entity_id, owner_id = me["entity_id"], (me.get("user_id") or me.get("id"))
    ws_id = await _make_workspace(entity_id, "Private retry", owner_id)
    viewer = await _create_entity_user(
        entity_id,
        f"planretry_viewer_{retry_surface}",
        role="member",
    )
    await _add_member(ws_id, viewer["id"], "viewer")

    plan_id, step_id = generate_ulid(), generate_ulid()
    async with db_module.async_session() as db:
        db.add(ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            workspace_id=ws_id,
            status="failed",
            execution_mode="live",
            approval_required=False,
            plan_dag={"steps": []},
        ))
        db.add(ExecutionStep(
            id=step_id,
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=ws_id,
            step_key="failed_step",
            kind="llm",
            params={},
            depends_on=[],
            step_status="failed",
            error={"type": "ProviderError"},
        ))
        await db.commit()

    endpoint = (
        f"/api/v1/plans/{plan_id}/retry-failed-steps"
        if retry_surface == "plan"
        else f"/api/v1/plans/steps/{step_id}/retry"
    )
    response = await client.post(endpoint, headers=viewer["headers"], json={})

    assert response.status_code == 403, response.text
    async with db_module.async_session() as db:
        plan = await db.get(ExecutionPlan, plan_id)
        step = await db.get(ExecutionStep, step_id)
        assert plan is not None and plan.status == "failed"
        assert step is not None and step.step_status == "failed"


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
