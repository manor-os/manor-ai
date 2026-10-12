"""Capability repair is reviewed, pinned and resumes the existing Task."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.models.user import Entity, User, UserMembership
from packages.core.governance.approval_policy_service import account_defaults
from tests.approval_settings_test_utils import initialize_test_approval_owners, action_rule, set_workspace_test_approval
from packages.core.models.workspace import Agent, AgentSubscription, Workspace, WorkspaceOperationDraft
from packages.core.services import task_capability_recovery as recovery
from packages.core.services import workspace_operation_service as operations
from packages.core.services.task_execution import resolve_task_execution, task_execution_intent_generation
from packages.core.services.task_retry_service import TaskRetryError, prepare_task_retry

pytestmark = pytest.mark.oss_regression


async def _seed(db):
    entity_id = generate_ulid()
    user = User(id=generate_ulid(), entity_id=entity_id, role="owner", status="active",
                email=f"recovery-{generate_ulid()}@example.com", password_hash="test")
    agent = Agent(id=generate_ulid(), entity_id=entity_id, name="Discovery", status="active")
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Recovery",
                          status="active", operating_model={"services": [{"key": "research", "name": "Research"}]})
    sub = AgentSubscription(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
                            service_key="research", agent_id=agent.id, status="active")
    task = Task(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
                title="Find reference material", status="blocked", creator_id=user.id,
                status_changed_at=datetime.now(timezone.utc), task_type="general",
                owner_service_key=sub.service_key, owner_subscription_id=sub.id,
                delegate_service_keys=[], details={"runtime_context": {"required_capabilities": ["web.safe_search"]}})
    db.add_all([Entity(id=entity_id, name="Recovery account", settings={"approval_defaults": account_defaults()}),
                UserMembership(entity_id=entity_id, user_id=user.id, role="owner", status="active"),
                user, agent, workspace, sub, task])
    await db.flush()
    await initialize_test_approval_owners(db, workspace, user_id=user.id)
    execution = await resolve_task_execution(db, task)
    run = TaskCoordinatorRun(id=generate_ulid(), task_id=task.id, entity_id=entity_id,
        workspace_id=workspace.id, root_subscription_id=sub.id, root_agent_id=agent.id,
        selection_fingerprint=execution.coordinator.stable_fingerprint(),
        lifecycle_generation=task_execution_intent_generation(task), phase="waiting_human")
    db.add(run)
    await db.flush()
    return SimpleNamespace(task=task, run=run, user=user, agent=agent, workspace=workspace, sub=sub)


async def _record(db, fixture):
    blocker, message = await recovery.record_capability_blocker(
        db, task=fixture.task, run=fixture.run, missing_capabilities=["web.safe_search"],
    )
    fixture.task.details = {**fixture.task.details, recovery.BLOCKER_KEY: dict(blocker)}
    await db.flush()
    return blocker, message


@pytest.mark.asyncio
async def test_gap_creates_one_scoped_review_not_a_new_task_or_grant(db_session):
    fixture = await _seed(db_session)
    blocker, message = await _record(db_session, fixture)
    assert blocker["kind"] == "tool_missing"
    assert message.pending_action["kind"] == "workspace_operation_review"
    assert "continue" in message.pending_action["payload"]["why"]
    draft = await db_session.get(WorkspaceOperationDraft, blocker["operation_draft_id"])
    binding = draft.patches[0]["payload"]["bindings"][0]
    assert binding["owner_scope"] == "agent"
    assert binding["agent_subscription_id"] == fixture.sub.id
    assert "capability_bindings" not in fixture.workspace.operating_model
    again, duplicate = await _record(db_session, fixture)
    assert again["operation_draft_id"] == draft.id
    assert duplicate is None
    assert (await db_session.scalar(select(func.count()).select_from(Task).where(Task.workspace_id == fixture.workspace.id))) == 1
    assert (await db_session.scalar(select(func.count()).select_from(WorkspaceOperationDraft).where(WorkspaceOperationDraft.workspace_id == fixture.workspace.id))) == 1


@pytest.mark.asyncio
async def test_unresolved_gap_rejects_retry_before_dispatch(db_session):
    fixture = await _seed(db_session)
    # Simulation cannot enter the live worker's acquisition path.
    fixture.workspace.settings = {"sandbox": True}
    await _record(db_session, fixture)
    with pytest.raises(TaskRetryError, match="needs configuration"):
        await prepare_task_retry(db_session, task=fixture.task, user_id=fixture.user.id, user_label="Owner")
    assert fixture.task.status == "blocked"
    assert not await db_session.scalar(select(RuntimeOutboxEvent.id).where(RuntimeOutboxEvent.aggregate_id == fixture.task.id))




@pytest.mark.asyncio
async def test_review_apply_resumes_same_task_with_durable_intent(db_session):
    fixture = await _seed(db_session)
    blocker, _ = await _record(db_session, fixture)
    draft = await db_session.get(WorkspaceOperationDraft, blocker["operation_draft_id"])
    await db_session.refresh(fixture.task)
    assert recovery.current_capability_blocker(fixture.task), fixture.task.details
    stored = recovery.current_capability_blocker(fixture.task)
    assert stored["operation_draft_id"] == draft.id
    for key in ("generation", "fingerprint", "selection_fingerprint", "run_id"):
        assert stored[key] == draft.current_state[recovery.RECOVERY_KEY][key], key
    await operations.apply_operation_draft(
        db_session, draft.id, fixture.task.entity_id, fixture.workspace.id,
        user_id=fixture.user.id, user_confirmation=True,
        expected_review_fingerprint=operations.operation_draft_review_fingerprint(draft),
    )
    assert fixture.task.status == "in_progress"
    assert recovery.BLOCKER_KEY not in fixture.task.details
    assert draft.status == "applied"
    events = list((await db_session.execute(select(RuntimeOutboxEvent).where(
        RuntimeOutboxEvent.aggregate_id == fixture.task.id,
    ))).scalars().all())
    assert len(events) == 1
    assert events[0].event_type == "runtime.dispatch_task_execution"
    assert events[0].delivered_at is None
    assert (await db_session.scalar(select(func.count()).select_from(Task).where(Task.workspace_id == fixture.workspace.id))) == 1
    with pytest.raises(operations.OperationConflictError):
        await operations.apply_operation_draft(
            db_session, draft.id, fixture.task.entity_id, fixture.workspace.id,
            user_id=fixture.user.id, user_confirmation=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["generation", "subscription", "claim", "plan", "fingerprint"])
async def test_stale_review_cannot_modify_configuration_or_resume(db_session, drift):
    fixture = await _seed(db_session)
    blocker, _ = await _record(db_session, fixture)
    draft = await db_session.get(WorkspaceOperationDraft, blocker["operation_draft_id"])
    fingerprint = operations.operation_draft_review_fingerprint(draft)
    if drift == "generation":
        fixture.task.status_changed_at = datetime.now(timezone.utc)
    elif drift == "subscription":
        fixture.sub.status = "inactive"
    elif drift == "claim":
        fixture.run.claim_token = "live"
        fixture.run.claim_generation = generate_ulid()
        fixture.run.claim_expires_at = datetime.now(timezone.utc)
    elif drift == "plan":
        fixture.run.current_plan_id = generate_ulid()
    else:
        fingerprint = "different"
    await db_session.flush()
    with pytest.raises(operations.OperationConflictError):
        await operations.apply_operation_draft(
            db_session, draft.id, fixture.task.entity_id, fixture.workspace.id,
            user_id=fixture.user.id, user_confirmation=True, expected_review_fingerprint=fingerprint,
        )
    assert fixture.task.status == "blocked"
    assert not fixture.workspace.operating_model.get("capability_bindings")


@pytest.mark.asyncio
async def test_unknown_capability_does_not_invent_configuration_or_approval(db_session):
    fixture = await _seed(db_session)
    blocker, message = await recovery.record_capability_blocker(
        db_session, task=fixture.task, run=fixture.run, missing_capabilities=["unknown.future_capability"],
    )
    assert blocker["kind"] == "capability_unavailable"
    assert "operation_draft_id" not in blocker
    assert message is None


@pytest.mark.asyncio
async def test_revoked_requester_keeps_blocker_without_proposing_or_retrying(db_session):
    fixture = await _seed(db_session)
    fixture.user.status = "inactive"
    await db_session.flush()
    blocker, message = await _record(db_session, fixture)
    assert blocker["kind"] == "capability_unavailable"
    assert "operation_draft_id" not in blocker
    assert message is None
    with pytest.raises(TaskRetryError, match="requester no longer has access"):
        await recovery.validate_capability_retry(
            db_session, task=fixture.task,
            execution=await resolve_task_execution(db_session, fixture.task),
        )
    assert fixture.task.status == "blocked"
    assert not await db_session.scalar(select(RuntimeOutboxEvent.id).where(
        RuntimeOutboxEvent.aggregate_id == fixture.task.id,
    ))


@pytest.mark.asyncio
async def test_hard_denial_is_not_proposed_as_missing_tool(db_session, monkeypatch):
    fixture = await _seed(db_session)
    await set_workspace_test_approval(db_session, workspace_id=fixture.workspace.id,
        entity_id=fixture.user.entity_id, actor_id=fixture.user.id,
        user_rules=[action_rule("tool.web_search.read", effect="deny")])
    blocker, message = await _record(db_session, fixture)
    assert blocker["kind"] == "policy_denied"
    assert message is None
    assert "operation_draft_id" not in blocker


def test_strategist_keeps_configuration_wait_on_original_task():
    from packages.core.strategist.service import _suppress_capability_recovery_duplicates

    task = Task(id="task", entity_id="entity", workspace_id="workspace", status="blocked",
                task_type="general", owner_service_key="research", owner_subscription_id="sub",
                delegate_service_keys=[], details={})
    task.details = {recovery.BLOCKER_KEY: {"generation": task_execution_intent_generation(task),
        "missing_capabilities": ["web.safe_search"]}}
    proposal = SimpleNamespace(notes=None, tasks=[
        SimpleNamespace(task_key="recover", owner_service_key="research", required_capabilities=["web.safe_search"], delegate_service_keys=[], depends_on_task_keys=[]),
        SimpleNamespace(task_key="dependent", owner_service_key="analysis", required_capabilities=[], delegate_service_keys=[], depends_on_task_keys=["recover"]),
        SimpleNamespace(task_key="independent", owner_service_key="research", required_capabilities=["file.write"], delegate_service_keys=[], depends_on_task_keys=[]),
    ])
    _suppress_capability_recovery_duplicates(proposal, SimpleNamespace(recent_tasks=[task], service_capabilities={}))
    assert [item.task_key for item in proposal.tasks] == ["independent"]
