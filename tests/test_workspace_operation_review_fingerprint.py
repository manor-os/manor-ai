"""Approval applies only the operation version in the durable clicked card."""
from __future__ import annotations

import json
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.ai.runtime.workspace_operation_actions import (
    _operation_review_payload,
    runtime_workspace_add_rule_action,
    runtime_workspace_operation_action,
    runtime_workspace_resolve_hitl_action,
)
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User
from packages.core.models.workspace import Workspace, WorkspaceOperationDraft
from packages.core.services.hitl_requests import (
    hitl_requests_from_data,
    workspace_operation_pending_action_from_data,
)
from packages.core.services.workspace_operation_service import (
    resolve_workspace_operation_review_message,
)


async def _seed(client, db, *, legacy=False, patches=None):
    username = f"review_{generate_ulid().lower()}"
    registered = await client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@test.com",
        "password": "pass123", "entity_name": "Review Test",
    })
    assert registered.status_code == 200, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    user = (await db.execute(select(User).where(User.email == f"{username}@test.com"))).scalar_one()
    ws = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Review version"})).json()
    response = await client.post(f"/api/v1/workspaces/{ws['id']}/operation/drafts", headers=headers, json={
        "patches": patches or [{"op": "integration.remove", "payload": {"provider": "slack"}}],
    })
    assert response.status_code == 200, response.text
    draft = response.json()
    review = _operation_review_payload(draft, prompt="Review integration changes", content={})
    if legacy:
        review["operation"].pop("review_fingerprint")
        review["hitl"]["id"] = review["approval_token"] = draft["id"]
    conv_id, message_id = generate_ulid(), generate_ulid()
    db.add(Conversation(id=conv_id, entity_id=user.entity_id, user_id=user.id,
                        workspace_id=ws["id"], channel="workspace", scope="workspace_main"))
    card = Message(
        id=message_id, conversation_id=conv_id, role="assistant", content="Review changes",
        author_kind="agent", message_kind="hitl_request",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        pending_action=workspace_operation_pending_action_from_data(review),
        meta={"hitl_requests": hitl_requests_from_data(review)},
    )
    db.add(card)
    db.add(Message(conversation_id=conv_id, role="user", content="Approve these changes",
                   author_kind="user", message_kind="text", meta={"author_user_id": user.id}))
    context = dict(headers=headers, workspace_id=ws["id"], entity_id=user.entity_id,
                   user_id=user.id, conversation_id=conv_id, message_id=message_id,
                   draft_id=draft["id"], hitl_id=review["approval_token"])
    await db.commit()
    return context


async def _resolve(surface, client, db, ctx):
    if surface == "workspace":
        return await client.post(
            f"/api/v1/workspaces/{ctx['workspace_id']}/chat/messages/{ctx['message_id']}/resolve",
            headers=ctx["headers"], json={"choice": "approve"},
        )
    kwargs = {key: ctx[key] for key in ("entity_id", "user_id", "conversation_id")}
    if surface == "runtime":
        return json.loads(await runtime_workspace_resolve_hitl_action(
            **kwargs, workspace_id=ctx["workspace_id"],
            params={"message_id": ctx["message_id"], "hitl_id": ctx["hitl_id"], "action": "approve"},
        ))
    from packages.core.services.chat_approvals import resolve_chat_approval_turn

    result, saved_text, _save, _metadata = await resolve_chat_approval_turn(
        db, **kwargs, message=json.dumps({"hitl_id": ctx["hitl_id"], "action": "approve"}),
    )
    if result and result.startswith("[Workspace operation not applied]"):
        assert saved_text == "Requested approval; the workspace operation was not applied."
    await db.commit()
    return result


async def _assert_pending(db, ctx):
    db.expire_all()
    card = await db.get(Message, ctx["message_id"])
    draft = await db.get(WorkspaceOperationDraft, ctx["draft_id"])
    workspace = await db.get(Workspace, ctx["workspace_id"])
    assert card.resolved_at is None
    assert not card.meta["hitl_requests"][0].get("resolved")
    assert draft.status == "open"
    assert workspace.operation_revision == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["workspace", "runtime", "personal"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_stale_or_unversioned_review_cannot_apply_or_close_card(client, db_session, surface, legacy):
    ctx = await _seed(client, db_session, legacy=legacy)
    patched = await client.patch(
        f"/api/v1/workspaces/{ctx['workspace_id']}/operation/drafts/{ctx['draft_id']}",
        headers=ctx["headers"], json={"patches": [{"op": "integration.remove", "payload": {"provider": "github"}}]},
    )
    assert patched.status_code == 200, patched.text
    # A newer card must never replace the older card the user clicked.
    latest = _operation_review_payload(patched.json(), prompt="Review more changes", content={})
    assert latest["approval_token"] != ctx["hitl_id"]
    db_session.add(Message(
        conversation_id=ctx["conversation_id"], role="assistant", content="Updated review",
        author_kind="agent", message_kind="hitl_request",
        pending_action=workspace_operation_pending_action_from_data(latest),
        meta={"hitl_requests": hitl_requests_from_data(latest)},
    ))
    await db_session.commit()
    result = await _resolve(surface, client, db_session, ctx)
    if surface == "workspace":
        assert result.status_code == 409, result.text
    elif surface == "runtime":
        assert result["resolved"] is False, result
    else:
        assert result.startswith("[Workspace operation not applied]"), result
    await _assert_pending(db_session, ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["workspace", "runtime", "personal"])
async def test_unchanged_review_applies_exact_contents(client, db_session, surface):
    ctx = await _seed(client, db_session)
    result = await _resolve(surface, client, db_session, ctx)
    if surface == "workspace":
        assert result.status_code == 200, result.text
    elif surface == "runtime":
        assert result["resolved"] is True, result
    else:
        assert result.startswith("[Workspace operation approved]"), result
    db_session.expire_all()
    workspace = await db_session.get(Workspace, ctx["workspace_id"])
    assert workspace.operation_revision == 1
    assert workspace.operating_model["integration_scope"]["disabled_providers"] == ["slack"]
    assert (await db_session.get(WorkspaceOperationDraft, ctx["draft_id"])).status == "applied"
    card = await db_session.get(Message, ctx["message_id"])
    assert card.resolved_at is not None
    assert card.meta["hitl_requests"][0]["resolved"] is True


@pytest.mark.asyncio
async def test_model_confirmation_cannot_apply_a_draft_without_its_durable_review(client, db_session):
    ctx = await _seed(client, db_session)
    await db_session.delete(await db_session.get(Message, ctx["message_id"]))
    await db_session.commit()
    result = json.loads(await runtime_workspace_operation_action(
        entity_id=ctx["entity_id"], workspace_id=ctx["workspace_id"],
        user_id=ctx["user_id"], conversation_id=ctx["conversation_id"],
        params={"action": "apply_draft", "draft_id": ctx["draft_id"], "user_confirmation": True},
    ))
    assert result["resolved"] is False, result
    db_session.expire_all()
    assert (await db_session.get(WorkspaceOperationDraft, ctx["draft_id"])).status == "open"


@pytest.mark.asyncio
async def test_model_add_rule_confirmation_still_returns_a_review(client, db_session):
    ctx = await _seed(client, db_session)
    result = json.loads(await runtime_workspace_add_rule_action(
        entity_id=ctx["entity_id"], workspace_id=ctx["workspace_id"],
        user_id=ctx["user_id"], conversation_id=ctx["conversation_id"],
        params={"description": "Explain results clearly", "apply_immediately": True},
    ))
    assert result["__hitl__"] is True, result
    db_session.expire_all()
    assert (await db_session.get(Workspace, ctx["workspace_id"])).operation_revision == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["workspace", "runtime", "personal"])
async def test_workspace_disappearing_before_apply_does_not_approve_card(client, db_session, monkeypatch, surface):
    from packages.core.services import workspace_operation_service as operations

    ctx = await _seed(client, db_session)
    original = operations.apply_operation_draft

    async def delete_before_apply(db, *args, **kwargs):
        workspace = await db.get(Workspace, ctx["workspace_id"])
        workspace.deleted_at = datetime.now(timezone.utc)
        await db.flush()
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(operations, "apply_operation_draft", delete_before_apply)
    result = await _resolve(surface, client, db_session, ctx)
    if surface == "workspace":
        assert result.status_code == 409, result.text
    elif surface == "runtime":
        assert result["resolved"] is False, result
        assert result["reason"] == "not_found"
    else:
        assert result.startswith("[Workspace operation not applied]"), result
    await _assert_pending(db_session, ctx)


@pytest.mark.asyncio
async def test_apply_winning_before_discard_retains_approved_receipt(client, db_session, monkeypatch):
    from packages.core.services import workspace_operation_service as operations

    ctx = await _seed(client, db_session)
    original = operations.discard_operation_draft

    async def apply_before_discard(db, *args, **kwargs):
        await operations.apply_operation_draft(db, *args, **kwargs, user_confirmation=True)
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(operations, "discard_operation_draft", apply_before_discard)
    result = await resolve_workspace_operation_review_message(
        db_session, conversation_id=ctx["conversation_id"], entity_id=ctx["entity_id"],
        user_id=ctx["user_id"], hitl_id=ctx["hitl_id"], action="reject",
    )
    await db_session.commit()
    assert "already applied" in result, result
    db_session.expire_all()
    card = await db_session.get(Message, ctx["message_id"])
    assert card.resolution["choice"] == "approve"
    assert card.meta["hitl_requests"][0]["resolution"] == "approve"
    assert (await db_session.get(WorkspaceOperationDraft, ctx["draft_id"])).status == "applied"


@pytest.mark.asyncio
async def test_runtime_bare_draft_id_cannot_select_between_review_versions(client, db_session):
    ctx = await _seed(client, db_session)
    draft = await client.patch(
        f"/api/v1/workspaces/{ctx['workspace_id']}/operation/drafts/{ctx['draft_id']}",
        headers=ctx["headers"], json={"patches": [{"op": "integration.remove", "payload": {"provider": "github"}}]},
    )
    assert draft.status_code == 200, draft.text
    latest = _operation_review_payload(draft.json(), prompt="Updated review", content={})
    db_session.add(Message(
        conversation_id=ctx["conversation_id"], role="assistant", content="Updated review",
        author_kind="agent", message_kind="hitl_request",
        pending_action=workspace_operation_pending_action_from_data(latest),
    ))
    await db_session.commit()
    result = json.loads(await runtime_workspace_resolve_hitl_action(
        entity_id=ctx["entity_id"], workspace_id=ctx["workspace_id"],
        conversation_id=ctx["conversation_id"], user_id=ctx["user_id"],
        params={"hitl_id": ctx["draft_id"], "action": "approve"},
    ))
    assert result["resolved"] is False
    assert result["reason"] == "ambiguous_review"
    await _assert_pending(db_session, ctx)


@pytest.mark.asyncio
async def test_personal_and_workspace_approval_share_lock_order(client, db_session, monkeypatch):
    from packages.core.database import async_session
    from packages.core.services import workspace_operation_service as operations

    ctx = await _seed(client, db_session)
    original = operations.apply_operation_draft
    applied = asyncio.Event()
    ui_entered = asyncio.Event()

    async def interleaved_apply(db, *args, **kwargs):
        if asyncio.current_task().get_name() == "personal-review":
            result = await original(db, *args, **kwargs)
            applied.set()
            await asyncio.wait_for(ui_entered.wait(), timeout=5)
            return result
        ui_entered.set()
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(operations, "apply_operation_draft", interleaved_apply)

    async def personal():
        async with async_session() as db:
            return await _resolve("personal", client, db, ctx)

    first = asyncio.create_task(personal(), name="personal-review")
    second = None
    try:
        await asyncio.wait_for(applied.wait(), timeout=5)
        second = asyncio.create_task(_resolve("workspace", client, db_session, ctx), name="workspace-review")
        personal_result, ui_result = await asyncio.wait_for(asyncio.gather(first, second), timeout=8)
        assert personal_result.startswith("[Workspace operation approved]")
        assert ui_result.status_code in {200, 409}, ui_result.text
    finally:
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    db_session.expire_all()
    assert (await db_session.get(Workspace, ctx["workspace_id"])).operation_revision == 1
    assert (await db_session.get(Message, ctx["message_id"])).resolution["choice"] == "approve"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["workspace", "runtime", "personal"])
async def test_new_live_hard_block_requires_review_of_changed_effects(client, db_session, surface):
    ctx = await _seed(client, db_session, patches=[{
        "op": "rules.replace", "payload": {"rules": [{
            "rule_key": "review_posts", "description": "Review every post",
            "rule_type": "approval_required", "approval_selectors": [{"kind": "action", "action_key": "social_post.publish"}],
        }]},
    }])
    settings_url = f"/api/v1/approval-settings/workspaces/{ctx['workspace_id']}"
    current = await client.get(settings_url, headers=ctx["headers"])
    assert current.status_code == 200, current.text
    response = await client.put(settings_url, headers=ctx["headers"], json={
        "expected_version": current.json()["version"],
        "draft": {"mode": None, "user_rules": [{"effect": "deny",
            "selector": {"kind": "action", "action_key": "payments.refund"}}]},
    })
    assert response.status_code == 200, response.text
    result = await _resolve(surface, client, db_session, ctx)
    if surface == "workspace":
        assert result.status_code == 409, result.text
    elif surface == "runtime":
        assert result["resolved"] is False, result
        assert result["reason"] == "conflict"
    else:
        assert result.startswith("[Workspace operation not applied]"), result
    await _assert_pending(db_session, ctx)
    response = await client.get(settings_url, headers=ctx["headers"])
    assert response.status_code == 200, response.text
    assert [(rule["effect"], rule["selector"]) for rule in response.json()["policy"]["rules"]] == [
        ("deny", {"kind": "action", "action_key": "payments.refund"}),
    ]


@pytest.mark.asyncio
async def test_replaced_message_card_cannot_be_marked_approved_by_an_older_click(client, db_session, monkeypatch):
    from packages.core.database import async_session
    from packages.core.services import workspace_operation_service as operations

    ctx = await _seed(client, db_session)
    original = operations.apply_operation_draft

    async def replace_card_after_apply(db, *args, **kwargs):
        result = await original(db, *args, **kwargs)
        # A separate streaming transaction replaces the card while apply
        # holds only Workspace/draft locks. The original click must roll back.
        async with async_session() as other:
            card = await other.get(Message, ctx["message_id"])
            action = dict(card.pending_action)
            action["approval_token"] = "replacement-review"
            card.pending_action = action
            await other.commit()
        return result

    monkeypatch.setattr(operations, "apply_operation_draft", replace_card_after_apply)
    result = await _resolve("workspace", client, db_session, ctx)
    assert result.status_code == 409, result.text
    await _assert_pending(db_session, ctx)
    card = await db_session.get(Message, ctx["message_id"])
    assert card.pending_action["approval_token"] == "replacement-review"
