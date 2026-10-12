"""Workspace Workflows obey the same approval policy as Chat and Task actions."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import select

from packages.core import database
from packages.core.ai.runtime.approval_service import guard_runtime_tool_action
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.governance import check_step_policy, update_policy
from packages.core.governance.approval_policy import ApprovalPolicyOverride, ApprovalRule
from packages.core.governance.approvals import ApprovalOrigin, ApprovalSubject, resolve_approval
from packages.core.governance.policy import WorkspacePolicy
from packages.core.models.hitl_request import HitlRequest
from packages.core.services.runtime_authorization import AuthorizationRule, PermissionDecision
from tests.test_automated_run_approvals import _running_workflow
from tests.test_runtime_guard_unified_store import _runtime_envelope, _SessionContext


async def _workspace_rule(db, ids, effect="ask", *, mode="confirm_changes", **constraints):
    await update_policy(db, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], policy=WorkspacePolicy(
        approval=ApprovalPolicyOverride(mode=mode, rules=[ApprovalRule(
            id="file-rule", effect=effect, selector={"kind": "category", "category": "files"},
        )] if effect else []), **constraints), changed_by=ids["user_id"])
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["automatic", "confirm_changes", "risk_based"])
@pytest.mark.parametrize("effect", [None, "allow", "ask", "deny"])
async def test_workflow_and_chat_obey_the_same_mode_and_rules(db_session, mode, effect):
    ids, run = await _running_workflow(db_session)
    await _workspace_rule(db_session, ids, effect, mode=mode)
    kwargs = {**ids, "kind": "action", "action_key": "workspace.file.create", "risk_level": "medium", "impact": "reversible"}
    workflow = await check_step_policy(db_session, **kwargs, workflow_run_id=run.id)
    chat = await check_step_policy(db_session, **kwargs)
    expected = effect or ("ask" if mode == "confirm_changes" else "allow")
    for decision in (workflow, chat):
        assert decision.allowed == (expected == "allow")
        assert decision.pause_for_hitl == (expected == "ask")
    assert workflow.matched_rule == chat.matched_rule
    assert workflow.explanation == chat.explanation


@pytest.mark.asyncio
async def test_running_workflow_rechecks_changed_policy(db_session):
    ids, run = await _running_workflow(db_session, mode="automatic")
    kwargs = {**ids, "workflow_run_id": run.id, "kind": "action",
              "action_key": "workspace.file.create", "risk_level": "medium", "impact": "reversible"}
    assert (await check_step_policy(db_session, **kwargs)).allowed
    await _workspace_rule(db_session, ids, "ask", mode="automatic")
    assert (await check_step_policy(db_session, **kwargs)).pause_for_hitl
    await _workspace_rule(db_session, ids, "deny", mode="automatic")
    denied = await check_step_policy(db_session, **kwargs)
    assert not denied.allowed and not denied.pause_for_hitl


@pytest.mark.asyncio
@pytest.mark.parametrize("effect,constraints,spent", [
    ("deny", {}, {}), ("ask", {"max_risk_level": "low"}, {}),
    ("ask", {"budget_caps_per_kind": {"action": 1}}, {"action": 1}),
])
async def test_workflow_preserves_hard_constraints(db_session, effect, constraints, spent):
    ids, run = await _running_workflow(db_session)
    await _workspace_rule(db_session, ids, effect, **constraints)
    result = await check_step_policy(db_session, **ids, workflow_run_id=run.id,
        kind="action", action_key="workspace.file.create", risk_level="medium", spent_credits_per_kind=spent)
    assert not result.allowed and not result.pause_for_hitl


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["automatic", "confirm_changes", "risk_based"])
@pytest.mark.parametrize("effect", [None, "allow", "ask", "deny"])
@pytest.mark.parametrize("name,args", [
    ("mcp__chrome__upload", {"files": ["video.mp4"]}),
    ("mcp__chrome__click_element", {"ref": "upload", "target": {
        "role": "button", "label": "Upload", "activation": "form_submit",
    }}),
    ("mcp__chrome__confirm_action", {"approvalId": "upload", "policy_category": "file_upload"}),
])
async def test_known_chrome_effects_obey_current_policy_on_every_surface(db_session, name, args, mode, effect):
    from packages.core.ai.runtime.approval_classifier import (
        classify_runtime_tool, runtime_policy_action_for_classification,
    )

    ids, run = await _running_workflow(db_session, mode=mode)
    action, impact = runtime_policy_action_for_classification(classify_runtime_tool(name, args))
    if effect:
        await update_policy(db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
            changed_by=ids["user_id"], policy=WorkspacePolicy(approval=ApprovalPolicyOverride(
                mode=mode, rules=[ApprovalRule(id="chrome-rule", effect=effect,
                    selector={"kind": "action", "action_key": action.action_key})])))
        await db_session.commit()
    kwargs = {**ids, "kind": action.kind, "action_key": action.action_key,
        "capability_id": action.capability_id, "resource_kind": action.resource_kind,
        "operation": action.operation, "risk_level": action.risk_level, "impact": impact}
    workflow = await check_step_policy(db_session, **kwargs, workflow_run_id=run.id)
    chat = await check_step_policy(db_session, **kwargs)
    expected = effect or ("allow" if mode == "automatic" else "ask")
    for decision in (workflow, chat):
        assert decision.allowed == (expected == "allow")
        assert decision.pause_for_hitl == (expected == "ask")
    assert workflow.matched_rule == chat.matched_rule
    assert workflow.explanation == chat.explanation


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["automatic", "confirm_changes", "risk_based"])
async def test_unknown_workflow_impact_obeys_the_selected_mode(db_session, mode):
    ids, run = await _running_workflow(db_session, mode=mode)
    result = await check_step_policy(db_session, **ids, workflow_run_id=run.id,
        kind="action", action_key="unclassified.action", risk_level="medium")
    assert result.allowed == (mode == "automatic")
    assert result.pause_for_hitl == (mode != "automatic")
    assert result.matched_rule == "approval.unknown_impact"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("mcp__chrome__unclassified_future_action", {}),
    ("mcp__chrome__confirm_action", {}),
    ("mcp__chrome__confirm_action", {"policy_category": "unclassified_future_category"}),
    ("mcp__chrome__confirm_action", {"confirmation_mode": "handoff_required", "policy_category": "password_change"}),
    ("mcp__chrome__confirm_action", {"confirmation_mode": "always_action_time", "policy_category": "captcha"}),
    ("mcp__chrome__confirm_action", {"confirmation_mode": "preapproval_allowed", "policy_category": "browser_permission", "preapproved": True}),
    ("mcp__chrome__confirm_action", {"confirmation_mode": "preapproval_allowed", "policy_category": "sensitive_data_transmission", "preapproved": True}),
    ("mcp__chrome__confirm_action", {"confirmation_mode": "preapproval_allowed", "policy_category": "login"}),
    ("mcp__chrome__confirm_action", {"confirmation_mode": "preapproval_allowed", "policy_category": "mfa"}),
])
async def test_risk_based_workflow_guard_blocks_unresolved_chrome_actions(
    db_session, client, name, args,
):
    from packages.core.ai.runtime.approval_classifier import (
        classify_runtime_tool, runtime_policy_action_for_classification,
    )

    classification = classify_runtime_tool(name, args)
    if name == "mcp__chrome__unclassified_future_action":
        assert classification.action is None
    else:
        _, impact = runtime_policy_action_for_classification(classification)
        assert impact is None  # A dynamic Chrome category is not a known external effect.
    ids, run = await _running_workflow(db_session, mode="risk_based")
    envelope = replace(_runtime_envelope(ids, name=name), surface=ChatSurface.WORKFLOW_AGENT_STEP)
    result = await guard_runtime_tool_action(
        **ids, name=name, arguments={**args, "_workflow_run_id_from_context": run.id},
        conversation_id=None, runtime_envelope=envelope,
        operation_id=f"workflow:{run.id}:unknown-chrome-action",
    )
    assert result is not None, (name, args)
    payload = json.loads(result)
    if name == "mcp__chrome__unclassified_future_action":
        assert payload["error"] == "chrome_unsupported_action", result
        assert payload["status"] == "blocked"
        assert payload["action_executed"] is False
    else:
        assert payload["error"] in {
            "blocked_by_authorization_configuration", "approval_required",
        }, result
    assert not (await db_session.execute(select(HitlRequest).where(
        HitlRequest.entity_id == ids["entity_id"],
    ))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["cancelled", "paused", "completed", "wrong_user", "wrong_run"])
async def test_invalid_workflow_origin_cannot_execute(db_session, case):
    ids, run = await _running_workflow(db_session)
    run_id = run.id
    if case in {"cancelled", "paused", "completed"}:
        run.status = case
        await db_session.commit()
    elif case == "wrong_user":
        ids["user_id"] = "another-user"
    else:
        run_id = "missing-run"
    result = await check_step_policy(db_session, **ids, workflow_run_id=run_id,
        kind="action", action_key="workspace.file.create", risk_level="medium")
    assert not result.allowed and not result.pause_for_hitl
    assert result.matched_rule == "workflow.origin_not_executable"


@pytest.mark.asyncio
async def test_guard_uses_workflow_origin_without_conversation_and_rechecks_denial(db_session, monkeypatch):
    ids, run = await _running_workflow(db_session)
    await _workspace_rule(db_session, ids, "allow")
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    envelope = replace(_runtime_envelope(ids, name="generate_file"), surface=ChatSurface.WORKFLOW_AGENT_STEP)
    args = {"kind": "document", "name": "release-plan.md", "file_type": "md", "content": "Plan",
            "_workflow_run_id_from_context": run.id}
    kwargs = {**ids, "conversation_id": None, "name": "generate_file", "arguments": args,
              "runtime_envelope": envelope, "operation_id": f"workflow:{run.id}:write"}
    assert await guard_runtime_tool_action(**kwargs) is None
    assert (await db_session.execute(select(HitlRequest).where(HitlRequest.entity_id == ids["entity_id"]))).scalars().all() == []
    # Both surfaces now respect an ask rule, even with a running Workflow ID.
    await _workspace_rule(db_session, ids, "ask")
    workflow_result = await guard_runtime_tool_action(**kwargs)
    assert json.loads(workflow_result)["error"] == "approval_required"
    chat_result = await guard_runtime_tool_action(**{**kwargs, "runtime_envelope": replace(envelope, surface=ChatSurface.WORKSPACE_CHAT)})
    assert json.loads(chat_result)["error"] == "approval_required"
    await _workspace_rule(db_session, ids, "deny")
    result = await guard_runtime_tool_action(**kwargs)
    assert json.loads(result)["error"] == "blocked_by_governance"


@pytest.mark.asyncio
async def test_explicit_workflow_review_still_requires_a_human(db_session):
    ids, run = await _running_workflow(db_session)
    result = await resolve_approval(db_session,
        subject=ApprovalSubject(**ids, action_key="workspace.file.create", impact="reversible", requires_approval=True),
        origin=ApprovalOrigin(kind="tool_call", workflow_run_id=run.id, operation_id=f"review:{run.id}"),
        permission_decision=PermissionDecision.allow(AuthorizationRule.DISPATCHER_WORKER))
    assert result.outcome == "needs_human"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,mode,rule,expected_calls", [
    ("running", "automatic", None, 2),
    ("running", "confirm_changes", None, 0),
    ("running", "automatic", "ask", 0),
    ("running", "automatic", "deny", 0),
    ("cancelled", "automatic", None, 0),
])
async def test_workflow_provider_confirmation_obeys_policy(db_session, monkeypatch, status, mode, rule, expected_calls):
    from unittest.mock import AsyncMock
    from packages.core.ai.runtime.approval_service import runtime_auto_confirm_provider_approval
    from tests.test_automated_run_approvals import _provider_gate

    ids, run = await _running_workflow(db_session, mode=mode)
    if rule:
        await update_policy(db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
            changed_by=ids["user_id"], policy=WorkspacePolicy(approval=ApprovalPolicyOverride(
                mode=mode, rules=[ApprovalRule(id="provider-rule", effect=rule,
                    selector={"kind": "action", "action_key": "external_message.send"})])))
    run.status = status
    await db_session.commit()
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    result = json.loads(_provider_gate("preapproval_allowed", "external_message"))
    result["action_key"] = "external_message.send"
    execute = AsyncMock(side_effect=[json.dumps({"approvalToken": "approved-token"}), json.dumps({"ok": True})])
    output = await runtime_auto_confirm_provider_approval(
        **ids, workflow_run_id=run.id, runtime_surface=ChatSurface.WORKFLOW_AGENT_STEP,
        tool_name="mcp__chrome__click_element", arguments={"ref": "send", "tabId": 9},
        result=json.dumps(result), execute=execute)
    assert execute.await_count == expected_calls
    if expected_calls:
        assert json.loads(output) == {"ok": True}
        assert execute.await_args_list[1].args[1]["approvalToken"] == "approved-token"


@pytest.mark.asyncio
async def test_standalone_workflow_does_not_change_its_consent_mode(db_session):
    ids, run = await _running_workflow(db_session, with_workspace=False)
    result = await check_step_policy(db_session, **ids, workflow_run_id=run.id,
        kind="action", action_key="workspace.file.create", risk_level="medium")
    assert result.pause_for_hitl


async def _legacy_ask_workspace(db):
    ids, run = await _running_workflow(db)
    await update_policy(db, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        changed_by=ids["user_id"], policy=WorkspacePolicy(approval=ApprovalPolicyOverride(
            mode=None, rules=[ApprovalRule(id="legacy-" + category, effect="ask",
                selector={"kind": "category", "category": category}) for category in (
                    "read", "files", "deletion", "communication", "publishing",
                    "purchases", "automation", "workspace", "unknown")]
        )))
    await db.commit()
    return ids, run


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("read_file", {"path": "plan.md"}),
    ("workspace_search", {"query": "release plan"}),
    ("probe_media", {"path": "master.wav"}),
    ("generate_file", {"kind": "document", "name": "release-plan.md", "file_type": "md", "content": "# Plan"}),
])
async def test_saved_ask_rules_cannot_be_bypassed_without_conversation(db_session, client, name, args):
    ids, run = await _legacy_ask_workspace(db_session)
    envelope = replace(_runtime_envelope(ids, name=name), surface=ChatSurface.WORKFLOW_AGENT_STEP)
    kwargs = {**ids, "conversation_id": None, "name": name,
        "arguments": {**args, "_workflow_run_id_from_context": run.id},
        "runtime_envelope": envelope, "operation_id": f"workflow:{run.id}:{name}"}
    output = await guard_runtime_tool_action(**kwargs)
    assert json.loads(output)["error"] == "approval_required"
    # No conversation is available to present consent: report the blocker,
    # never run the operation or fabricate an approval request.
    assert not (await db_session.execute(select(HitlRequest).where(HitlRequest.entity_id == ids["entity_id"]))).scalars().all()
    chat = await guard_runtime_tool_action(**{**kwargs, "runtime_envelope": replace(envelope, surface=ChatSurface.WORKSPACE_CHAT)})
    assert json.loads(chat)["error"] == "approval_required"


@pytest.mark.asyncio
async def test_policy_allowed_workflow_creates_a_real_document_without_conversation(db_session, client, tmp_path, monkeypatch):
    from packages.core.ai.runtime.tool_execution import runtime_execute_registered_tool
    from packages.core.ai.tools.generate_file.tool import _generate_file_handler
    from packages.core.config import get_settings
    from packages.core.models.document import Document
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")
    ids, run = await _running_workflow(db_session, mode="automatic")
    await ensure_workspace_artifact_folder(db_session, await db_session.get(Workspace, ids["workspace_id"]))
    await db_session.commit()
    envelope = replace(_runtime_envelope(ids, name="generate_file"), surface=ChatSurface.WORKFLOW_AGENT_STEP)
    args = {"kind": "document", "name": "release-plan.md", "file_type": "md", "content": "# Verified release plan",
        "_workflow_run_id_from_context": run.id, "_workflow_lineage_root_run_id_from_context": run.id,
        "_runtime_run_id_from_context": "workflow:" + run.id, "_runtime_tool_call_id_from_context": "save-plan"}
    output = await runtime_execute_registered_tool(tool_name="generate_file", arguments=args,
        handler_resolver=lambda _: _generate_file_handler, runtime_envelope=envelope)
    result = json.loads(output)
    assert not result.get("error"), result
    await db_session.rollback()
    docs = list((await db_session.execute(select(Document).where(Document.entity_id == ids["entity_id"]))).scalars())
    saved = [document for document in docs if document.name.endswith("release-plan.md")]
    assert len(saved) == 1, result
    assert (tmp_path / ids["entity_id"] / saved[0].fs_path).read_text() == "# Verified release plan"
    assert saved[0].id in output
    assert not (await db_session.execute(select(HitlRequest).where(HitlRequest.entity_id == ids["entity_id"]))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("rule_type", ["approval_required", "deny"])
async def test_workflow_keeps_parent_task_restrictions(db_session, rule_type):
    from packages.core.constants.agents import MANOR_AGENT_ID
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Task

    ids, run = await _running_workflow(db_session, mode="automatic")
    conversation_id = await db_session.scalar(select(Conversation.id).where(
        Conversation.workspace_id == ids["workspace_id"]))
    task = Task(id=generate_ulid(), entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        conversation_id=conversation_id, agent_id=MANOR_AGENT_ID, title="Policy test",
        details={"runtime_context": {"rules": [{"rule_key": "task-file-rule",
            "rule_type": rule_type, "description": "Review file changes in this Task.",
            "approval_selectors": [{"kind": "action", "action_key": "workspace.file.create"}]}]}})
    db_session.add(task)
    await db_session.flush()
    decision = await check_step_policy(db_session, **ids, workflow_run_id=run.id,
        task_id=task.id, kind="action", action_key="workspace.file.create",
        risk_level="medium", impact="reversible")
    assert not decision.allowed
    assert decision.pause_for_hitl == (rule_type == "approval_required")
    assert decision.matched_rule == "task-file-rule"
