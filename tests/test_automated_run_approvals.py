"""Unattended origins validate identity but use the current approval policy."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime import approval_service
from packages.core.governance import (
    automated_run_always_allowed, check_step_policy, update_policy,
)
from packages.core.governance.approval_policy import ApprovalPolicyOverride, ApprovalRule
from packages.core.governance.approval_policy_service import (
    account_defaults, initialize_workspace_approval, local_defaults,
)
from packages.core.governance.policy import PolicyDecision, PolicyError, WorkspacePolicy, policy_from_dict, policy_to_dict
from packages.core.models.base import generate_ulid


def test_unattended_bool_is_retired_and_modes_round_trip():
    for value in (True, False, "true"):
        with pytest.raises(PolicyError, match="automated_runs_always_allow"):
            policy_from_dict({"automated_runs_always_allow": value})
    policy = WorkspacePolicy(approval=ApprovalPolicyOverride(mode="confirm_changes"))
    assert policy_from_dict(policy_to_dict(policy)) == policy


async def _running_workflow(db, *, with_workspace=True, mode="confirm_changes"):
    from packages.core.models.task import Conversation
    from packages.core.models.user import Entity, User, UserMembership
    from packages.core.models.workflow import WorkflowRun
    from packages.core.models.workspace import Workspace

    entity_id, user_id, conversation_id = (generate_ulid() for _ in range(3))
    defaults = account_defaults()
    defaults["policy"]["mode"] = mode
    db.add(Entity(id=entity_id, name="Approval tests", settings={"approval_defaults": defaults}))
    db.add(User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.test", password_hash="none", role="owner",
                preferences={"approval_policy": {entity_id: local_defaults(entity_id)}}))
    db.add(UserMembership(user_id=user_id, entity_id=entity_id, role="owner", status="active", is_primary=True))
    workspace_id = None
    if with_workspace:
        workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Scheduled work", status="active", operating_model={})
        db.add(workspace)
        await db.flush()
        await initialize_workspace_approval(db, workspace)
        workspace_id = workspace.id
    db.add(Conversation(id=conversation_id, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id))
    run = WorkflowRun(id=generate_ulid(), workflow_id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
        started_by=user_id, status="running", current_step_id="execute", variables={}, step_results={}, trigger_data={},
        execution_snapshot={}, execution_trace=[], lineage_is_legacy=False)
    run.lineage_root_run_id = run.id
    db.add(run)
    await db.commit()
    return {"entity_id": entity_id, "user_id": user_id, "workspace_id": workspace_id}, run


@pytest.mark.asyncio
@pytest.mark.parametrize("with_workspace", [True, False])
async def test_running_workflow_is_identity_not_blanket_consent(db_session, with_workspace):
    from packages.core.governance.service import workflow_run_is_executable

    ids, run = await _running_workflow(db_session, with_workspace=with_workspace)
    assert await workflow_run_is_executable(db_session, **ids, workflow_run_id=run.id)
    assert not await automated_run_always_allowed(db_session, **ids, workflow_run_id=run.id)
    decision = await check_step_policy(db_session, **ids, kind="action", action_key="social_post.publish", risk_level="medium")
    assert decision.pause_for_hitl
    run.status = "completed"
    await db_session.flush()
    assert not await workflow_run_is_executable(db_session, **ids, workflow_run_id=run.id)
    assert not await workflow_run_is_executable(db_session, **{**ids, "user_id": "other"}, workflow_run_id=run.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_workspace", [True, False])
async def test_workflow_actions_observe_changed_root_and_keep_explicit_denial(db_session, with_workspace):
    from sqlalchemy import select
    from packages.core.models.user import Entity

    ids, _ = await _running_workflow(db_session, with_workspace=with_workspace, mode="automatic")
    before = await check_step_policy(db_session, **ids, kind="action", action_key="social_post.publish", risk_level="medium")
    assert before.allowed
    entity = (await db_session.execute(select(Entity).where(Entity.id == ids["entity_id"]))).scalar_one()
    defaults = {"revision": 2, "policy": {"mode": "automatic", "rules": [ApprovalRule(id="no-publish", effect="deny",
        selector={"kind": "category", "category": "publishing"}).model_dump()]}}
    entity.settings = {"approval_defaults": defaults}
    await db_session.flush()
    after = await check_step_policy(db_session, **ids, kind="action", action_key="social_post.publish", risk_level="medium")
    assert not after.allowed and not after.pause_for_hitl
    assert after.matched_rule == "no-publish"


@pytest.mark.asyncio
async def test_hard_policy_update_preserves_live_local_mode(db_session):
    ids, _ = await _running_workflow(db_session)
    await update_policy(db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        policy=WorkspacePolicy(approval=ApprovalPolicyOverride(mode="automatic")), changed_by=ids["user_id"])
    row = await update_policy(db_session, entity_id=ids["entity_id"], workspace_id=ids["workspace_id"],
        policy=WorkspacePolicy(max_risk_level="medium"), changed_by=ids["user_id"])
    assert row.policy["approval"]["policy"]["mode"] == "automatic"
    assert row.policy["max_risk_level"] == "medium"
    assert "automated_runs_always_allow" not in row.policy


@pytest.mark.asyncio
async def test_scheduled_origin_requires_matching_job_run_and_lifecycle(db_session):
    from packages.core.governance.service import scheduled_automation_origin_is_valid
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun

    ids, _ = await _running_workflow(db_session)
    job = ScheduledJob(job_id=generate_ulid(), entity_id=ids["entity_id"], workspace_id=ids["workspace_id"], execution_type="agent", enabled=True)
    run = ScheduledJobRun(id=generate_ulid(), job_id=job.job_id, status="running", trigger_type="cron")
    db_session.add_all([job, run])
    await db_session.flush()
    kwargs = {"entity_id": ids["entity_id"], "workspace_id": ids["workspace_id"], "scheduled_job_id": job.job_id, "scheduled_run_id": run.id, "execution_type": "agent"}
    assert await scheduled_automation_origin_is_valid(db_session, **kwargs)
    assert not await automated_run_always_allowed(db_session, **ids, task_id="scheduled-task")
    assert not await scheduled_automation_origin_is_valid(db_session, **{**kwargs, "scheduled_run_id": "missing"})
    assert not await scheduled_automation_origin_is_valid(db_session, **{**kwargs, "entity_id": "other"})
    run.status = "completed"
    await db_session.flush()
    assert not await scheduled_automation_origin_is_valid(db_session, **kwargs)


@pytest.mark.asyncio
async def test_blueprint_export_never_carries_unattended_consent(monkeypatch):
    from packages.core.blueprints import exporter

    monkeypatch.setattr(exporter, "get_policy", AsyncMock(return_value=WorkspacePolicy(approval=ApprovalPolicyOverride(mode="automatic"))))
    assert await exporter._export_governance(None, "workspace") is None


def _provider_gate(mode, category):
    return json.dumps({"ok": False, "status": "approval_required", "approval_required": True,
        "provider": "chrome", "approvalId": "provider-operation", "expires_at": "2099-01-01T00:00:00Z",
        "confirmation_mode": mode, "policy_category": category, "target_label": "Publish",
        "retry_action": {"name": "mcp__chrome__click_element", "arguments": {"ref": "publish", "tabId": 9}}})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,category", [
    ("handoff_required", "password_change"), ("always_action_time", "captcha"),
    ("preapproval_allowed", "sensitive_data_transmission"), ("preapproval_allowed", "browser_permission"),
])
@pytest.mark.parametrize("surface", ["workspace_chat", "workflow_agent_step"])
async def test_provider_human_requirements_survive_any_mode(monkeypatch, mode, category, surface):
    from packages.core.governance import approvals, service

    @asynccontextmanager
    async def session():
        yield None

    monkeypatch.setattr("packages.core.database.async_session", session)
    monkeypatch.setattr(approvals, "find_requests_by_dedup", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "check_step_policy", AsyncMock(return_value=PolicyDecision(True)))
    standing = AsyncMock(return_value=True)
    monkeypatch.setattr(approval_service, "_standing_grant", standing)
    execute = AsyncMock()
    original = _provider_gate(mode, category)
    result = await approval_service.runtime_auto_confirm_provider_approval(tool_name="mcp__chrome__click_element",
        arguments={"ref": "publish", "tabId": 9}, result=original, execute=execute,
        entity_id="entity", user_id="user", workspace_id="workspace", workflow_run_id="workflow", runtime_surface=surface)
    assert result == original
    execute.assert_not_awaited()
    standing.assert_not_awaited()
