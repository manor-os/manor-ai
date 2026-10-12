"""A hard-block denial names the rule that caused it and hands back the fix.

Prod task FQ2881 (2026-09-14): the chat agent reported "实时规则仍硬性禁止
social_post.publish" and stopped. Every denial surface carried only the glob
pattern that matched; the rule that produced it — the thing the operator can
change — was never named, and no surface offered the one-line fix (retype the
rule to approval_required). This pins that both the chat-tool denial and the
plan-step denial now carry ``blocking_rules`` and ``fix``, and that a plan-step
denial parks the task for a human instead of replanning into the same wall.
"""
from __future__ import annotations

import json

import pytest

from tests.approval_settings_test_utils import initialize_test_approval_owners
from packages.core.governance.approval_projection import compile_workspace_rules, replace_workspace_rules

from packages.core.governance.hard_block_provenance import (
    hard_block_denial_context,
    hard_block_fix,
    hard_block_rules_for,
    soften_rule_patch,
)

_DRAFT_ONLY = {
    "rule_key": "draft_only_social",
    "description": "社交帖子只出草稿",
    "rule_type": "draft_only",
    "approval_selectors": [{"kind": "action", "action_key": "social_post.publish"}],
}
_APPROVAL = {
    "rule_key": "ask_before_posting",
    "description": "发帖前先问我",
    "rule_type": "approval_required",
    "approval_selectors": [{"kind": "action", "action_key": "social_post.publish"}],
}
_CATEGORY_DENY = {
    "rule_key": "no_publishing",
    "description": "禁止发布内容",
    "rule_type": "deny",
    "approval_selectors": [{"kind": "category", "category": "publishing"}],
}


# ── the pure walk ─────────────────────────────────────────────────────


def test_finds_the_hard_block_rule_by_action_and_skips_approval_rules():
    found = hard_block_rules_for(
        [_APPROVAL, _DRAFT_ONLY],
        action_key="social_post.publish",
        capability_id="external.social",
        matched_rule="social_post.publish",
    )
    assert [r["rule_key"] for r in found] == ["draft_only_social"]
    assert found[0]["rule_type"] == "draft_only"
    assert found[0]["description"] == "社交帖子只出草稿"
    assert found[0]["kind"] == "action" and found[0]["pattern"] == "social_post.publish"


def test_category_rule_names_the_source_for_each_matching_publication():
    for action in ("social_post.publish", "content.publish"):
        found = hard_block_rules_for([_CATEGORY_DENY], action_key=action)
        assert [r["rule_key"] for r in found] == ["no_publishing"]
        assert found[0]["kind"] == "category"
        assert found[0]["pattern"] == "publishing"
    assert hard_block_rules_for([_CATEGORY_DENY], action_key="data.read") == []


@pytest.mark.parametrize("legacy", [
    {"action_patterns": ["social_post.*"]},
    {"capability_patterns": ["external.*"]},
])
def test_legacy_glob_or_capability_source_requires_explicit_migration(legacy):
    with pytest.raises(ValueError):
        compile_workspace_rules([{"rule_key": "legacy", "rule_type": "deny", **legacy}])


def test_no_owning_rule_yields_an_empty_list():
    assert hard_block_rules_for([_APPROVAL], action_key="social_post.publish") == []
    assert hard_block_rules_for(None, action_key="social_post.publish") == []


# ── the fix ───────────────────────────────────────────────────────────


def test_fix_is_the_exact_rule_update_patch_plus_the_rules_page():
    blocking = hard_block_rules_for([_DRAFT_ONLY], action_key="social_post.publish")
    fix = hard_block_fix("ws_1", blocking)
    assert fix["rules_page"] == "/workspaces/ws_1?tab=rules"
    assert fix["workspace_operation"]["action"] == "create_draft"
    assert fix["workspace_operation"]["patches"] == [soften_rule_patch("draft_only_social")]
    assert soften_rule_patch("k") == {
        "op": "rule.update",
        "payload": {"rule_key": "k", "rule": {"rule_type": "approval_required"}},
    }


def test_policy_only_block_still_points_at_the_rules_page():
    context = hard_block_denial_context(
        [], workspace_id="ws_1", action_key="social_post.publish",
        capability_id=None, matched_rule="social_post.publish",
    )
    assert context["blocking_rules"] == []
    assert context["fix"]["rules_page"] == "/workspaces/ws_1?tab=rules"
    assert "workspace_operation" not in context["fix"]
    assert "no rule owns" in context["message"]


def test_message_names_the_rule_and_the_soften_path():
    context = hard_block_denial_context(
        [_DRAFT_ONLY], workspace_id="ws_1", action_key="social_post.publish",
        capability_id="external.social", matched_rule="social_post.publish",
    )
    assert "'draft_only_social'" in context["message"]
    assert "approval_required" in context["message"]
    assert "rules page" in context["message"]


# ── the plan-step denial ──────────────────────────────────────────────


async def _gated_step(db, *, rules):
    """A workspace subagent step parked in front of the dispatcher gate.

    Self-contained on purpose: importing another test module's fixture
    imports that module a second time and breaks its own suite when it is
    collected afterwards.
    """
    from decimal import Decimal

    from packages.core.governance import get_policy, update_policy
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.user import User, UserMembership
    from packages.core.models.worker import SubscriptionWorker, Worker
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    agent_id = generate_ulid()
    subscription_id = generate_ulid()
    actor_id = generate_ulid()
    worker = Worker(
        id=generate_ulid(), entity_id=entity_id, kind="internal", display_name="Internal worker",
        capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
        monthly_spent_usd=Decimal("0"), auto_pause_on_budget=True, status="active",
    )
    db.add_all([
        User(id=actor_id, entity_id=entity_id, email=f"{actor_id}@example.com",
             password_hash="test-only", role="owner", status="active"),
        UserMembership(user_id=actor_id, entity_id=entity_id, role="owner", status="active"),
        Workspace(id=workspace_id, entity_id=entity_id, name="Gated workspace", status="active",
                  operating_model={"rules": rules}),
        worker,
        Agent(id=agent_id, entity_id=entity_id, name="Content Agent",
              slug=f"content-{agent_id[:8].lower()}", status="active"),
        AgentSubscription(id=subscription_id, entity_id=entity_id, agent_id=agent_id,
                          workspace_id=workspace_id, name="Content", service_key="content", status="active"),
        SubscriptionWorker(subscription_id=subscription_id, worker_id=worker.id, priority=100, is_preferred=True),
        ExecutionPlan(id=plan_id, entity_id=entity_id, workspace_id=workspace_id, task_id=None,
                      status="running", execution_mode="live", approval_required=False, plan_dag={"steps": []}),
        ExecutionStep(id=step_id, plan_id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
                      step_key="publish_post", kind="subagent", service_key="content",
                      action_key="social_post.publish", capability_id="external.social", params={"prompt": "Publish the approved post."},
                      depends_on=[], step_status="pending", risk_level="medium", requires_approval=True,
                      attempt_count=0, max_attempts=1),
    ])
    await db.flush()
    workspace = await db.get(Workspace, workspace_id)
    await initialize_test_approval_owners(db, workspace, user_id=actor_id)
    policy = replace_workspace_rules(await get_policy(db, workspace_id), rules)
    await update_policy(db, entity_id=entity_id, workspace_id=workspace_id,
                        policy=policy, changed_by=actor_id)
    await db.commit()
    return {"entity_id": entity_id, "step_id": step_id, "worker": worker}


@pytest.mark.asyncio
async def test_plan_step_denial_names_the_rule_and_parks_for_a_human(db_session, monkeypatch):
    from sqlalchemy import select

    from packages.core.dispatcher.service import Dispatcher
    from packages.core.models.execution import ExecutionStep
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.plans.executor import _human_required_failure_issue

    cards: list[dict] = []

    async def fake_card(**kwargs):
        cards.append(kwargs)

    monkeypatch.setattr("packages.core.governance.service.post_hitl_card", fake_card)
    s = await _gated_step(
        db_session, rules=[dict(_DRAFT_ONLY)],
    )

    leases = await Dispatcher().checkout_steps_for_worker(db_session, s["worker"], max_n=1)
    step = await db_session.get(ExecutionStep, s["step_id"])

    assert leases == []
    assert step.step_status == "failed"
    assert step.error["type"] == "GovernancePolicy"
    assert [r["rule_key"] for r in step.error["blocking_rules"]] == ["draft_only_social"]
    assert step.error["fix"]["workspace_operation"]["patches"] == [soften_rule_patch("draft_only_social")]
    assert "'draft_only_social'" in step.error["message"]
    # Hard blocks stay hard: still no approvable card, no open request.
    open_requests = list((await db_session.execute(
        select(HitlRequest).where(HitlRequest.entity_id == s["entity_id"], HitlRequest.status == "pending")
    )).scalars().all())
    assert cards == [] and open_requests == []
    # A policy denial is not a pending question or approval request.
    assert step.error["failure"]["requires_human"] is True
    assert step.error["failure"]["retryable"] is False
    issue = _human_required_failure_issue([step])
    assert issue is None


def test_unsupported_file_operation_requires_human_does_not_park_the_plan():
    from types import SimpleNamespace

    from packages.core.constants.execution import ExecutionStepStatus
    from packages.core.plans.executor import _human_required_failure_issue

    step = SimpleNamespace(
        step_key="patch_document",
        step_status=ExecutionStepStatus.FAILED,
        error={
            "type": "ToolError",
            "message": "unsupported_operation_for_file_type",
            "failure": {
                "requires_human": True,
                "reason": "unsupported_operation_for_file_type",
                "blockers": ["text.set"],
            },
        },
        result=None,
    )
    assert _human_required_failure_issue([step]) is None


def test_model_file_not_found_flag_is_not_a_human_request():
    from types import SimpleNamespace

    from packages.core.constants.execution import ExecutionStepStatus
    from packages.core.plans.executor import _human_required_failure_issue

    step = SimpleNamespace(
        step_key="patch_document",
        step_status=ExecutionStepStatus.FAILED,
        error={
            "type": "ToolError",
            "message": "File not found",
            "failure": {
                "requires_human": True,
                "reason": "File not found",
                "blockers": ["documents/missing.md"],
            },
        },
        result=None,
    )
    issue = _human_required_failure_issue([step])
    assert issue is None


# ── the chat-tool denial ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_tool_denial_carries_blocking_rules_and_fix(db_session, monkeypatch):
    from packages.core import database
    from packages.core.ai.runtime.approval_service import guard_runtime_tool_action
    from packages.core.governance import get_policy, update_policy
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import User, UserMembership
    from packages.core.models.workspace import Workspace

    class _SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(database, "async_session", lambda: _SessionContext())

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    user_id = generate_ulid()
    db_session.add(User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.com", password_hash="x", role="owner"))
    db_session.add(UserMembership(user_id=user_id, entity_id=entity_id, role="owner", status="active"))
    await db_session.flush()
    deny_rule = {
        "rule_key": "no_agent_tasks",
        "description": "agent 不得自行创建任务",
        "rule_type": "deny",
        "approval_selectors": [{"kind": "action", "action_key": "workspace.task.create"}],
    }
    db_session.add(Workspace(
        id=workspace_id, entity_id=entity_id, name="Denial provenance",
        operating_model={"rules": [deny_rule]},
    ))
    await db_session.flush()
    workspace = await db_session.get(Workspace, workspace_id)
    await initialize_test_approval_owners(db_session, workspace, user_id=user_id)
    policy = replace_workspace_rules(await get_policy(db_session, workspace_id), [deny_rule])
    await update_policy(
        db_session, entity_id=entity_id, workspace_id=workspace_id,
        policy=policy, changed_by=user_id, change_summary="deny task creation",
    )
    await db_session.commit()

    blocked = await guard_runtime_tool_action(
        name="workspace_agent",
        arguments={"action": "create_task", "params": {"title": "Prepare lease options"}},
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
        conversation_id=None,
        operation_id=f"test:hard-block:{workspace_id}:create-task",
    )

    assert blocked is not None
    payload = json.loads(blocked)
    assert payload["error"] == "blocked_by_governance"
    assert payload["matched_rule"] == compile_workspace_rules([deny_rule])[0].id
    assert [r["rule_key"] for r in payload["blocking_rules"]] == ["no_agent_tasks"]
    assert payload["fix"]["workspace_operation"]["patches"] == [soften_rule_patch("no_agent_tasks")]
    assert payload["fix"]["rules_page"] == f"/workspaces/{workspace_id}?tab=rules"
    assert "'no_agent_tasks'" in payload["message"]


def test_model_guidance_tells_the_agent_to_offer_the_fix():
    from packages.core.ai.runtime.prompt_guidance import runtime_tool_usage_guidance
    import inspect

    source = inspect.getsource(runtime_tool_usage_guidance)
    assert "blocking_rules" in source
    assert "never only report that" in source
