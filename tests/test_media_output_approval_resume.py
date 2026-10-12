"""Persisted media approvals reach the real filesystem and Knowledge boundary."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from test_generated_media_authorization_contract import PNG, _approve, _authorized, _save
from test_runtime_guard_unified_store import _SessionContext, _fixture, _runtime_envelope

from packages.core import database
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.approval_service import (
    _runtime_render_context,
    consume_runtime_approval,
    guard_runtime_tool_action,
)
from packages.core.ai.runtime.media_outputs import runtime_media_output_approval_scope, runtime_media_output_policy_resources
from packages.core.ai.tools import extended_tools
from packages.core.config import get_settings
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.workspace import Workspace
from packages.core.services.runtime_authorization import AuthorizationRule, PermissionDecision
from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder
from packages.core.services.generated_file_refs import entity_fs_open_url


pytestmark = pytest.mark.oss_smoke


async def _persist_grant(db, ids, request, receipt):
    action = classify_runtime_tool(
        request.tool_name, request.arguments,
        entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        workspace_file_scope=request.workspace_file_scope,
    ).action
    with runtime_media_output_approval_scope(receipt.media_output_plan):
        context = await _runtime_render_context(
            db, **ids, tool_name=request.tool_name,
            arguments=request.arguments, action=action,
        )
    from packages.core.governance.approvals import _consent_requirements
    from packages.core.governance.service import check_step_policy

    with runtime_media_output_approval_scope(receipt.media_output_plan):
        decision = await check_step_policy(
            db, entity_id=ids["entity_id"], user_id=ids["user_id"], workspace_id=ids["workspace_id"],
            kind=action.kind, action_key=action.action_key, capability_id=action.capability_id,
            resource_kind=action.resource_kind, resource_id=action.resource_id,
            resource_ids=runtime_media_output_policy_resources(), risk_level=action.risk_level,
            operation=action.operation,
        )
    assert decision.pause_for_hitl
    context = {**context, "operation_id": f"media:{ids['conversation_id']}",
               "approval_requirements": _consent_requirements(decision)}
    row = HitlRequest(
        id=generate_ulid(), entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        action_key=action.action_key, capability_id=action.capability_id,
        resource_id=action.resource_id, resource_kind="file", risk_level="medium",
        origin_kind="tool_call", origin_conversation_id=ids["conversation_id"],
        status="granted", dedup_key=f"operation:{context['operation_id']}", context=context,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _consume(db, ids, row, request, receipt):
    action = classify_runtime_tool(
        request.tool_name, request.arguments,
        entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        workspace_file_scope=request.workspace_file_scope,
    ).action
    with runtime_media_output_approval_scope(receipt.media_output_plan):
        return await consume_runtime_approval(
            db, entity_id=ids["entity_id"], user_id=ids["user_id"],
            conversation_id=ids["conversation_id"], hitl_id=row.id,
            tool_name=request.tool_name, arguments=request.arguments, action=action,
            permission_decision=PermissionDecision.allow(AuthorizationRule.HITL_REQUEST_OWNER),
        )


async def _guard_media(ids, request, receipt, *, new_intent=None):
    classification = classify_runtime_tool(
        request.tool_name, request.arguments,
        entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        workspace_file_scope=request.workspace_file_scope,
    )
    operation_id = f"media:{ids['conversation_id']}"
    if new_intent is not None:
        operation_id += f":intent:{new_intent}"
    with runtime_media_output_approval_scope(receipt.media_output_plan):
        return await guard_runtime_tool_action(
            name=request.tool_name, arguments=dict(request.arguments), **ids,
            runtime_envelope=_runtime_envelope(ids, name=request.tool_name),
            classification=classification, operation_id=operation_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_change", ["none", "collision", "legacy"])
async def test_persisted_media_approval_resume_and_knowledge_projection(
    db_session, monkeypatch, tmp_path, resume_change,
):
    from packages.core.services import knowledge_sync

    # Projection schedules committed event delivery in a child task. Preserve
    # production's independent sessions instead of sharing the approval session.
    sessions = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", sessions)
    monkeypatch.setattr(database, "create_worker_session", lambda: sessions)
    monkeypatch.setattr(knowledge_sync, "async_session", sessions)
    monkeypatch.setattr(extended_tools, "_bill_media", AsyncMock())
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")

    ids = await _fixture(db_session, hitl_capability="file.write")
    workspace = await db_session.get(Workspace, ids["workspace_id"])
    folder = await ensure_workspace_artifact_folder(db_session, workspace)
    await db_session.commit()
    requested = "production/夏季_宣传图"
    scope = {
        "entity_id": ids["entity_id"], "workspace_id": ids["workspace_id"],
        "user_id": ids["user_id"], "task_id": None,
    }
    request, receipt = await _approve(requested, **scope)
    row = await _persist_grant(db_session, ids, request, receipt)
    assert row.context["media_output_plan"] == receipt.media_output_plan.to_dict()
    expected = f"Workspaces/_by_id/{folder.id}/production/夏季-宣传图.png"
    entity_root = tmp_path / ids["entity_id"]
    target = entity_root / expected

    if resume_change == "collision":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"concurrent artifact")
    elif resume_change == "legacy":
        row.context = {key: value for key, value in row.context.items() if key != "media_output_plan"}
        await db_session.commit()

    resumed_request, resumed_receipt = await _approve(requested, **scope)
    outcome = await _consume(db_session, ids, row, resumed_request, resumed_receipt)
    await db_session.commit()
    await db_session.refresh(row)
    if resume_change != "none":
        payload = json.loads(outcome)
        assert row.status == "expired"
        assert row.consumed_at is None
        assert payload["error"] == "blocked_by_governance"
        assert payload["matched_rule"] == (
            "approval.operation_changed" if resume_change == "collision" else "approval.operation_terminal"
        )
        assert payload["status"] == "expired" and payload["action_executed"] is False
        assert payload["approval_token"] == row.id
        assert "__hitl__" not in payload and "hitl" not in payload
        replay = json.loads(await _consume(db_session, ids, row, resumed_request, resumed_receipt))
        assert replay["error"] == "approval_not_granted" and replay["status"] == "expired"
        await db_session.commit()
        existing = (await db_session.scalars(select(HitlRequest.id).where(
            HitlRequest.entity_id == ids["entity_id"],
            HitlRequest.origin_conversation_id == ids["conversation_id"],
        ))).all()
        assert existing == [row.id]
        fresh = json.loads(await _guard_media(
            ids, resumed_request, resumed_receipt, new_intent="review-updated-image",
        ))
        assert fresh["__hitl__"] is True and fresh["approval_token"] != row.id
        replacement = await db_session.get(HitlRequest, fresh["approval_token"])
        assert replacement is not None and replacement.status == "pending"
        assert replacement.context["operation_id"] != row.context["operation_id"]
        assert replacement.dedup_key == f"operation:{replacement.context['operation_id']}"
        assert replacement.context["media_output_plan"] == resumed_receipt.media_output_plan.to_dict()
        all_ids = (await db_session.scalars(select(HitlRequest.id).where(
            HitlRequest.entity_id == ids["entity_id"],
            HitlRequest.origin_conversation_id == ids["conversation_id"],
        ))).all()
        assert set(all_ids) == {row.id, replacement.id}
        await db_session.refresh(row)
        assert row.status == "expired" and row.consumed_at is None
        if resume_change == "collision":
            assert target.read_bytes() == b"concurrent artifact"
        else:
            assert not target.exists()
        documents = (await db_session.execute(select(Document).where(
            Document.entity_id == ids["entity_id"],
        ))).scalars().all()
        assert documents == []
        assert not (entity_root / expected.replace(".png", "-2.png")).exists()
        return

    assert outcome is None and row.status == "consumed"
    with _authorized(resumed_request, resumed_receipt):
        image_url = await _save(
            requested, **scope, conversation_id=ids["conversation_id"], save_to_knowledge=True,
        )
    assert image_url == entity_fs_open_url(ids['entity_id'], expected)
    assert target.read_bytes() == PNG
    documents = (await db_session.execute(select(Document).where(
        Document.entity_id == ids["entity_id"], Document.fs_path == expected,
    ))).scalars().all()
    assert len(documents) == 1
    document = documents[0]
    assert document.id and document.folder_id
    assert document.name == "夏季-宣传图.png"
    assert document.source == "ai_generated"
    assert document.mime_type == "image/png"
    assert document.file_size == len(PNG)
    assert document.visibility == "workspace"
    assert document.metadata_["origin"]["workspace_id"] == ids["workspace_id"]
    assert document.metadata_["origin"]["conversation_id"] == ids["conversation_id"]
    assert document.metadata_["artifact"]["role"] == "final"


@pytest.mark.asyncio
async def test_tokenless_guard_rechecks_plan_when_only_secondary_format_collides(
    db_session, monkeypatch, tmp_path,
):
    from packages.core.governance.approvals import grant_approval

    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    ids = await _fixture(db_session, hitl_capability="file.write")
    workspace = await db_session.get(Workspace, ids["workspace_id"])
    await ensure_workspace_artifact_folder(db_session, workspace)
    await db_session.commit()
    scope = {
        "entity_id": ids["entity_id"], "workspace_id": ids["workspace_id"],
        "user_id": ids["user_id"], "task_id": None,
    }
    request, receipt = await _approve("hero.png", **scope)
    first = json.loads(await _guard_media(ids, request, receipt))
    row = await db_session.get(HitlRequest, first["approval_token"])
    await grant_approval(
        db_session, row, by_user_id=ids["user_id"], via="test", authority_prechecked=True,
    )
    await db_session.commit()

    jpg = receipt.media_output_plan.target_for_extension(".jpg")
    collision = tmp_path / ids["entity_id"] / jpg.rel_path
    collision.parent.mkdir(parents=True, exist_ok=True)
    collision.write_bytes(b"concurrent jpeg")
    resumed_request, resumed_receipt = await _approve("hero.png", **scope)
    assert resumed_request.arguments == request.arguments
    assert resumed_receipt.media_output_plan.targets[0] == receipt.media_output_plan.targets[0]
    assert resumed_receipt.media_output_plan.target_for_extension(".jpg").rel_path != jpg.rel_path
    assert "approval_token" not in resumed_request.arguments
    blocked = json.loads(await _guard_media(ids, resumed_request, resumed_receipt))
    await db_session.refresh(row)
    assert row.status == "expired" and row.consumed_at is None
    assert row.resolved_reason == "approval_requirements_changed"
    assert blocked["__hitl__"] is True and blocked["approval_token"] != row.id
    replacement = await db_session.get(HitlRequest, blocked["approval_token"])
    assert replacement.status == "pending"
    # This tokenless pre-consumption check discovers an additional target's
    # requirement; it replaces the live grant within the same operation.
    assert replacement.context["operation_id"] == row.context["operation_id"]
    assert replacement.dedup_key == row.dedup_key
    assert replacement.context["media_output_plan"] == resumed_receipt.media_output_plan.to_dict()
    assert collision.read_bytes() == b"concurrent jpeg"
    assert not (tmp_path / ids["entity_id"] / receipt.media_output_plan.targets[0].rel_path).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("rule_scope, outcome", [
    ("logical_webp", "deny"),
    ("physical_webp", "deny"),
    ("logical_directory", "deny"),
    ("requested_name", "deny"),
    ("logical_webp", "hitl"),
    ("physical_webp", "hitl"),
    ("logical_directory", "hitl"),
])
async def test_media_governance_checks_every_format_and_exact_path_scope(
    db_session, monkeypatch, tmp_path, rule_scope, outcome,
):
    from tests.approval_settings_test_utils import set_workspace_test_approval

    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    ids = await _fixture(db_session)
    workspace = await db_session.get(Workspace, ids["workspace_id"])
    folder = await ensure_workspace_artifact_folder(db_session, workspace)
    requested_name = "images/Hero_Asset.PNG"
    request, receipt = await _approve(
        requested_name, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        user_id=ids["user_id"], task_id=None,
    )
    with runtime_media_output_approval_scope(receipt.media_output_plan):
        resources = list(runtime_media_output_policy_resources())
    webp = receipt.media_output_plan.target_for_extension(".webp").rel_path
    base = f"Workspaces/_by_id/{folder.id}/"
    selected = {
        "logical_webp": [webp.removeprefix(base)],
        "physical_webp": [webp],
        "logical_directory": resources,
        "requested_name": [requested_name],
    }[rule_scope]
    assert set(selected) <= set(resources)
    saved = await set_workspace_test_approval(
        db_session, workspace_id=ids["workspace_id"], entity_id=ids["entity_id"],
        actor_id=ids["user_id"], mode="automatic", user_rules=[{
            "effect": "deny" if outcome == "deny" else "ask",
            "selector": {"kind": "resource", "action_key": "workspace.file.create",
                "resource_kind": "file", "resource_ids": selected, "account_id": None},
        }],
    )
    rule = saved["policy"]["rules"][0]["id"]
    await db_session.commit()
    result = json.loads(await _guard_media(ids, request, receipt))
    if outcome == "deny":
        assert result["error"] == "blocked_by_governance"
        assert result["matched_rule"] == rule
    else:
        assert result["__hitl__"] is True
        row = await db_session.get(HitlRequest, result["approval_token"])
        assert row.status == "pending" and row.matched_rule == rule
        requests = (await db_session.execute(select(HitlRequest).where(
            HitlRequest.entity_id == ids["entity_id"],
        ))).scalars().all()
        assert len(requests) == 1
    assert not list((tmp_path / ids["entity_id"]).rglob("*.png"))
