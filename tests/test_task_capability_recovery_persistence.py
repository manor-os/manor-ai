"""JSONB recovery receipts survive implicit flushes between awaited reads."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm.attributes import set_committed_value

from packages.core.models.task import Task
from packages.core.services import task_capability_recovery as recovery


@pytest.fixture
def recovery_fixture(monkeypatch):
    from packages.core import governance
    from packages.core.ai.runtime import workspace_operation_actions
    from packages.core.services import hitl_requests, task_requester_identity
    from packages.core.services import workspace_operation_service as operations
    from packages.core.services.agent_effective_capabilities import AgentEffectiveCapabilityFactory
    from packages.core.workspace_chat import service as chat

    task = Task(
        id="task", entity_id="entity", workspace_id="workspace", status="blocked",
        title="Research", details={},
    )
    run = SimpleNamespace(id="run", current_plan_id=None, selection_fingerprint="selection")
    root = SimpleNamespace(agent_id="agent", subscription_id="subscription", service_key="research")
    coordinator = SimpleNamespace(root=root, stable_fingerprint=lambda: "selection")
    agent = SimpleNamespace(id="agent", name="Researcher")
    draft = SimpleNamespace(id="draft", current_state={}, validation={}, diff={"changed_keys": ["capability_bindings"]})
    committed = []
    policy = SimpleNamespace(allowed=True, pause_for_hitl=False)

    def autoflush():
        committed.append(deepcopy(task.details))
        # A flush marks the current ORM value committed. Deliberately do not
        # replace its nested objects: that is the aliasing bug under test.
        set_committed_value(task, "details", task.details)

    async def resolve(*_args, **_kwargs):
        autoflush()
        return SimpleNamespace(coordinator=coordinator)

    async def load_policy(*_args, **_kwargs):
        autoflush()
        return policy

    async def create_draft(*_args, **_kwargs):
        autoflush()
        return draft

    monkeypatch.setattr(recovery, "task_execution_intent_generation", lambda _task: "generation")
    monkeypatch.setattr(recovery, "resolve_task_execution", resolve)
    monkeypatch.setattr(recovery, "task_plan_subscriptions", AsyncMock(return_value=[SimpleNamespace(id="subscription")]))
    monkeypatch.setattr(governance, "check_step_policy", load_policy)
    monkeypatch.setattr(task_requester_identity, "resolve_task_execution_user_id", AsyncMock(return_value="user"))
    monkeypatch.setattr(AgentEffectiveCapabilityFactory, "create", AsyncMock(return_value={"research": SimpleNamespace(available=True)}))
    monkeypatch.setattr(operations, "create_operation_draft", create_draft)
    monkeypatch.setattr(operations, "draft_to_dict", lambda _draft: {})
    monkeypatch.setattr(workspace_operation_actions, "_operation_review_payload", lambda *_args, **_kwargs: {"hitl": {"id": "draft"}})
    monkeypatch.setattr(hitl_requests, "workspace_operation_pending_action_from_data", lambda _payload: {})
    monkeypatch.setattr(chat, "post_message", AsyncMock(return_value=SimpleNamespace(id="message")))
    db = SimpleNamespace(
        get=AsyncMock(return_value=agent),
        scalar=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))),
    )
    return SimpleNamespace(db=db, task=task, run=run, draft=draft, committed=committed, policy=policy)


@pytest.mark.asyncio
async def test_receipt_assignment_stays_dirty_after_intermediate_autoflush(recovery_fixture):
    state = recovery_fixture
    blocker, message = await recovery.record_capability_blocker(
        state.db, task=state.task, run=state.run, missing_capabilities=["web.safe_search"],
    )
    assert message is not None
    assert blocker["operation_draft_id"] == "draft"
    assert state.task.details[recovery.BLOCKER_KEY] == blocker
    assert state.task.details[recovery.BLOCKER_KEY] is not blocker
    assert "operation_draft_id" not in state.committed[-1][recovery.BLOCKER_KEY]
    assert inspect(state.task).attrs.details.history.has_changes()
    assert state.draft.current_state[recovery.RECOVERY_KEY]["fingerprint"] == blocker["fingerprint"]


@pytest.mark.asyncio
async def test_concurrent_stopped_task_recorders_create_one_durable_review(db_session):
    import asyncio

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.models.task_coordinator import TaskCoordinatorRun
    from packages.core.models.workspace import WorkspaceOperationDraft
    from packages.core.services.workspace_access import lock_workspace_access_boundary
    from test_task_capability_recovery import _seed

    fixture = await _seed(db_session)
    await db_session.commit()
    task_id, run_id = fixture.task.id, fixture.run.id
    workspace_id, entity_id = fixture.workspace.id, fixture.workspace.entity_id
    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    gate = asyncio.Event()

    async def record():
        async with sessions() as db:
            await gate.wait()
            await lock_workspace_access_boundary(db, workspace_id=workspace_id, entity_id=entity_id)
            task = await db.scalar(select(Task).where(Task.id == task_id).with_for_update().execution_options(populate_existing=True))
            run = await db.scalar(select(TaskCoordinatorRun).where(TaskCoordinatorRun.id == run_id).with_for_update().execution_options(populate_existing=True))
            blocker, message = await recovery.record_capability_blocker(
                db, task=task, run=run, missing_capabilities=["web.safe_search"],
            )
            await db.commit()
            return blocker["operation_draft_id"], message.id if message is not None else None

    pending = [asyncio.create_task(record()), asyncio.create_task(record())]
    gate.set()
    outcomes = await asyncio.wait_for(asyncio.gather(*pending), timeout=20)
    assert len({draft_id for draft_id, _ in outcomes}) == 1
    assert len([message_id for _, message_id in outcomes if message_id]) == 1
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(WorkspaceOperationDraft).where(
            WorkspaceOperationDraft.workspace_id == workspace_id,
        )) == 1
        task = await db.get(Task, task_id)
        assert task.details[recovery.BLOCKER_KEY]["operation_draft_id"] == outcomes[0][0]


@pytest.mark.asyncio
async def test_new_hard_deny_rolls_back_apply_even_when_chat_caller_commits(db_session):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from packages.core.models.governance import GovernancePolicy
    from packages.core.governance.approval_policy import ApprovalRule
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.models.workspace import Workspace, WorkspaceOperationDraft
    from packages.core.services import workspace_operation_service as operations
    from test_task_capability_recovery import _record, _seed

    fixture = await _seed(db_session)
    blocker, message = await _record(db_session, fixture)
    draft = await db_session.get(WorkspaceOperationDraft, blocker["operation_draft_id"])
    fingerprint = operations.operation_draft_review_fingerprint(draft)
    task_id, workspace_id, draft_id = fixture.task.id, fixture.workspace.id, draft.id
    entity_id, user_id, conversation_id = fixture.task.entity_id, fixture.user.id, message.conversation_id
    await db_session.commit()

    # Direct DB update deliberately leaves any pre-review policy cache stale.
    # The recovery preflight must consult current durable hard denials.
    policy = await db_session.get(GovernancePolicy, workspace_id)
    assert policy is not None
    value = deepcopy(policy.policy)
    value["approval"]["policy"]["rules"].extend(ApprovalRule(id=f"new-{tool}-deny", effect="deny",
        selector={"kind": "action", "action_key": f"tool.{tool}.read"}).model_dump()
        for tool in ("web_search", "web_fetch", "browse_web"))
    policy.policy = value
    policy.revision += 1
    await db_session.commit()

    # Refresh the reviewed diff so this reaches continuation preflight rather
    # than stopping earlier at the independent stale-fingerprint guard.
    refreshed = await operations.refresh_operation_draft_review(
        db_session, draft_id, entity_id, workspace_id,
    )
    fingerprint = operations.operation_draft_review_fingerprint(refreshed)
    await db_session.commit()

    result = await operations.resolve_workspace_operation_review(
        db_session, conversation_id=conversation_id, entity_id=entity_id,
        user_id=user_id, hitl_id=draft_id, action="approve", workspace_id=workspace_id,
        expected_review_fingerprint=fingerprint,
    )
    assert result["status"] == "conflict"
    assert "rules prohibit" in result["message"]
    # Personal Chat returns a failure explanation and then persists its turn.
    # That outer commit must not retain a half-applied configuration grant.
    await db_session.commit()
    sessions = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as db:
        workspace = await db.get(Workspace, workspace_id)
        task = await db.get(Task, task_id)
        persisted_draft = await db.get(WorkspaceOperationDraft, draft_id)
        assert not workspace.operating_model.get("capability_bindings")
        assert workspace.operation_revision == 0
        assert task.status == "blocked"
        assert recovery.current_capability_blocker(task)
        assert persisted_draft.status == "open"
        assert not await db.scalar(select(RuntimeOutboxEvent.id).where(RuntimeOutboxEvent.aggregate_id == task_id))


@pytest.mark.asyncio
async def test_policy_denial_assignment_survives_policy_lookup_autoflush(recovery_fixture):
    state = recovery_fixture
    state.policy.allowed = False
    blocker, message = await recovery.record_capability_blocker(
        state.db, task=state.task, run=state.run, missing_capabilities=["web.safe_search"],
    )
    assert message is None
    assert blocker["kind"] == "policy_denied"
    assert state.committed[-1][recovery.BLOCKER_KEY]["kind"] == "capability_unavailable"
    assert inspect(state.task).attrs.details.history.has_changes()


@pytest.mark.asyncio
async def test_early_unavailable_return_persists_resolved_affected_agent(recovery_fixture):
    state = recovery_fixture
    blocker, message = await recovery.record_capability_blocker(
        state.db, task=state.task, run=state.run, missing_capabilities=["unknown.capability"],
    )
    assert message is None
    assert blocker["affected_agents"][0]["subscription_id"] == "subscription"
    assert state.task.details[recovery.BLOCKER_KEY]["affected_agents"] == blocker["affected_agents"]
    assert state.committed[-1][recovery.BLOCKER_KEY]["affected_agents"] == []
    assert inspect(state.task).attrs.details.history.has_changes()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["record", "retry"])
async def test_capability_preflight_uses_runtime_canonical_read_policy(recovery_fixture, monkeypatch, phase):
    from packages.core import governance
    from packages.core.governance.approval_facts import action_facts
    from packages.core.governance.approval_policy import ApprovalScope, evaluate_policy
    from packages.core.governance.approval_policy_service import account_defaults, build_policy_chain
    from packages.core.governance.policy import PolicyDecision
    from packages.core.services.agent_effective_capabilities import AgentEffectiveCapabilityFactory
    from packages.core.services.task_retry_service import TaskRetryError

    state = recovery_fixture
    tools = ("web_search", "web_fetch", "browse_web")
    root = account_defaults()
    root["policy"]["rules"] = [{"id": f"deny-{tool}", "effect": "deny",
        "selector": {"kind": "action", "action_key": f"tool.{tool}.read"}} for tool in tools]
    chain = build_policy_chain(scope=ApprovalScope(kind="account", id="entity", entity_id="entity"), root=root)
    seen = []

    async def evaluate_current(_db, **kwargs):
        seen.append(kwargs)
        result = evaluate_policy(chain, action_facts(
            action_key=kwargs["action_key"], capability_id=kwargs["capability_id"],
            risk_level=kwargs["risk_level"], impact=kwargs.get("impact"),
            operation=kwargs.get("operation"),
        ))
        return PolicyDecision(result.outcome == "allow", result.outcome == "ask")

    monkeypatch.setattr(governance, "check_step_policy", evaluate_current)
    monkeypatch.setattr(recovery, "task_plan_subscriptions", AsyncMock(
        return_value=[SimpleNamespace(id="subscription", agent_id="agent")]))
    if phase == "record":
        blocker, message = await recovery.record_capability_blocker(
            state.db, task=state.task, run=state.run, missing_capabilities=["web.safe_search"],
        )
        assert blocker["kind"] == "policy_denied"
        assert message is None
    else:
        state.task.details = {recovery.BLOCKER_KEY: {"generation": "generation",
            "selection_fingerprint": "selection", "missing_capabilities": ["web.safe_search"]}}
        monkeypatch.setattr(AgentEffectiveCapabilityFactory, "create", AsyncMock(return_value={
            "research": SimpleNamespace(capability_ids={"web.safe_search"}, tool_names=set(tools)),
        }))
        execution = await recovery.resolve_task_execution(state.db, state.task)
        with pytest.raises(TaskRetryError, match="rules prohibit"):
            await recovery.validate_capability_retry(state.db, task=state.task, execution=execution)
    assert {call["action_key"] for call in seen} == {f"tool.{tool}.read" for tool in tools}
    assert all(call["impact"] == "read" for call in seen)
