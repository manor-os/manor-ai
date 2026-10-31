"""Default policy end to end through the real runtime gate (R2-1, R2-3, R2-5).

A Task agent loop's ordinary run — plan, discover, search, read, delegate,
complete — mints no approval card under the account default (risk_based).
Under confirm_changes, and even under a migrated "ask for every category"
policy, loop controls, discovery and delegation still mint none: only the
leaf work they cause is judged. An explicit action rule the user wrote for
delegation still applies (owner decision Q1).
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from packages.core import database
from packages.core.ai.runtime.approval_service import guard_runtime_tool_action
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.principals import RuntimePrincipal
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.constants.runtime_principal import RuntimePrincipalKind
from packages.core.governance.approval_policy_service import account_defaults
from packages.core.models.base import generate_ulid
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Task
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.workspace_runtime import ensure_workspace_task_conversation
from tests.approval_settings_test_utils import (
    action_rule, initialize_test_approval_owners, set_workspace_test_approval,
)

PLAN = {"plan": [{"step": "Draft the brief", "status": "in_progress"}],
        "acceptance_criteria": ["A brief exists"]}
LOOP_CALLS = [
    ("update_task_plan", PLAN),
    ("search_tools", {"query": "summarize documents"}),
    ("workspace_search", {"query": "launch brief"}),
    ("read_file", {"path": "brief.md"}),
    ("delegate_to_agent", {"agent": "Writer", "task": "Draft the launch brief."}),
    ("call_tool", {"name": "generate_file", "rejected_reason": "generate_file is not loaded"}),
    ("wait_until", {"until": "2026-10-07T09:00:00Z", "reason": "embargo"}),
    ("complete_task", {"outcome": "completed", "summary": "Brief drafted."}),
]
CONTROL_AND_DELEGATION = {"update_task_plan", "search_tools", "delegate_to_agent", "call_tool",
                          "wait_until", "complete_task"}
EVERY_CATEGORY = ("read", "files", "communication", "publishing", "deletion", "purchases",
                  "automation", "workspace", "unknown")


class _Session:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_exc):
        return False


async def _scope(db, *, mode=None, rules=()):
    entity_id, user_id, workspace_id, agent_id = (generate_ulid() for _ in range(4))
    db.add(Entity(id=entity_id, name="Effect contract", settings={"approval_defaults": account_defaults()}))
    db.add(User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.test",
                password_hash="x", role="owner", status="active", display_name="Ana"))
    db.add(UserMembership(user_id=user_id, entity_id=entity_id, role="owner", status="active", is_primary=True))
    db.add(Workspace(id=workspace_id, entity_id=entity_id, name="Launch", status="active", operating_model={}))
    db.add(Agent(id=agent_id, entity_id=entity_id, name="Planner", status="active"))
    db.add(AgentSubscription(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id,
                             agent_id=agent_id, name="Planner", service_key="planner", status="active"))
    await db.flush()
    task = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace_id, creator_id=user_id,
                title="Write the launch brief", agent_id=agent_id, task_type="general",
                status="in_progress", details={})
    db.add(task)
    await db.flush()
    thread = await ensure_workspace_task_conversation(
        db, entity_id=entity_id, workspace_id=workspace_id, task_id=task.id, title=task.title,
    )
    task.conversation_id = thread.id
    await db.flush()
    workspace = await db.get(Workspace, workspace_id)
    await initialize_test_approval_owners(db, workspace, user_id=user_id)
    if mode is not None or rules:
        await set_workspace_test_approval(db, entity_id=entity_id, workspace_id=workspace_id,
                                          actor_id=user_id, mode=mode, user_rules=list(rules))
    await db.commit()
    envelope = RuntimeEnvelope(
        surface=ChatSurface.SCHEDULED_AGENT_RUN,
        profile=RuntimeProfile.BACKGROUND_WORKER,
        principal=RuntimePrincipal(
            kind=RuntimePrincipalKind.WORKSPACE_MEMBER, entity_id=entity_id,
            actor_user_id=user_id, execution_user_id=user_id, workspace_id=workspace_id,
        ),
        entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
        conversation_id=thread.id, task_id=task.id,
        allowed_tool_names=tuple(name for name, _ in LOOP_CALLS),
    )
    return task, thread, envelope


async def _run_loop_calls(db, monkeypatch, *, mode=None, rules=()):
    monkeypatch.setattr(database, "async_session", lambda: _Session(db))
    task, thread, envelope = await _scope(db, mode=mode, rules=rules)
    results = {}
    for index, (name, args) in enumerate(LOOP_CALLS):
        results[name] = await guard_runtime_tool_action(
            name=name, arguments=dict(args), entity_id=envelope.entity_id, user_id=envelope.user_id,
            workspace_id=envelope.workspace_id, conversation_id=thread.id, task_id=task.id,
            runtime_envelope=envelope, operation_id=f"runtime:{task.id}:call:{index}",
        )
    cards = await db.scalar(select(func.count()).select_from(HitlRequest).where(
        HitlRequest.entity_id == envelope.entity_id))
    return results, cards


def _blocked(result):
    """``None`` when the gate let the call run, else the gate's error code."""
    return None if result is None else json.loads(result).get("error")


@pytest.mark.asyncio
async def test_default_risk_based_task_loop_mints_no_card(db_session, monkeypatch):
    results, cards = await _run_loop_calls(db_session, monkeypatch)

    assert {name: _blocked(result) for name, result in results.items()} == {
        name: None for name, _ in LOOP_CALLS
    }
    assert cards == 0


@pytest.mark.asyncio
async def test_confirm_changes_never_gates_controls_discovery_or_delegation(db_session, monkeypatch):
    results, _cards = await _run_loop_calls(db_session, monkeypatch, mode="confirm_changes")

    for name in CONTROL_AND_DELEGATION:
        assert _blocked(results[name]) is None, name
    asks = await db_session.scalar(select(func.count()).select_from(HitlRequest).where(
        HitlRequest.action_key.in_([
            "workspace.service.delegate", "runtime.control.update_task_plan",
            "runtime.control.complete_task", "runtime.control.call_tool",
            "runtime.control.wait_until", "tool.search_tools.read",
        ])))
    assert asks == 0


@pytest.mark.asyncio
async def test_migrated_ask_everything_policy_only_gates_the_leaf_reads(db_session, monkeypatch):
    rules = [{"effect": "ask", "selector": {"kind": "category", "category": category}}
             for category in EVERY_CATEGORY]
    results, cards = await _run_loop_calls(db_session, monkeypatch, mode="confirm_changes", rules=rules)

    for name in CONTROL_AND_DELEGATION:
        assert _blocked(results[name]) is None, name
    # The user's own read: ask still governs the leaf reads (the user is the authority).
    assert _blocked(results["workspace_search"]) == "approval_required"
    assert _blocked(results["read_file"]) == "approval_required"
    assert cards == 2


@pytest.mark.asyncio
async def test_an_explicit_delegation_rule_still_applies(db_session, monkeypatch):
    results, cards = await _run_loop_calls(
        db_session, monkeypatch, rules=[action_rule("workspace.service.delegate", effect="ask")],
    )

    assert _blocked(results["delegate_to_agent"]) == "approval_required"
    assert cards == 1
    request = (await db_session.execute(select(HitlRequest).where(
        HitlRequest.action_key == "workspace.service.delegate"))).scalar_one()
    assert request.status == "pending"


@pytest.mark.asyncio
async def test_an_explicit_delegation_deny_still_blocks(db_session, monkeypatch):
    results, cards = await _run_loop_calls(
        db_session, monkeypatch, rules=[action_rule("workspace.service.delegate", effect="deny")],
    )

    assert _blocked(results["delegate_to_agent"]) == "blocked_by_governance"
    assert cards == 0
