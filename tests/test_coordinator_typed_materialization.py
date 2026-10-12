from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.constants.execution import (
    ACTION_EFFECT_GENERATION_PARAM_KEY,
    COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY,
    COORDINATOR_CHILD_ITEM_PARAM_KEY,
    COORDINATOR_CHILD_PARAM_KEY,
    COORDINATOR_CHILD_RELEASED_PARAM_KEY,
    COORDINATOR_PARENT_STEP_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
    COORDINATOR_WAITING_CHILD_PARAM_KEY,
    ExecutionStepStatus,
)
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.plans.coordinator import (
    TASK_COORDINATOR_PLAN_VERSION,
    TASK_COORDINATOR_PLAN_VERSION_KEY,
    TASK_COORDINATOR_RUN_ID_KEY,
    coordinator_child_idempotency_key,
    coordinator_child_step_key,
    coordinator_plan_from,
)
from packages.core.plans.schema import Plan, PlanStep
from packages.core.plans.service import create_plan_from_dag, materialize_plan_steps
from packages.core.services.task_execution import (
    CoordinatorSelectionDriftError,
    CoordinatorRuntimeSelection,
    TaskPlanServiceBinding,
    resolve_task_execution,
)
from packages.core.services.task_coordinator_wakeup import (
    claim_task_coordinator_wake,
    ensure_task_coordinator_run,
    stage_task_coordinator_wake,
)


ACTION_INPUT_SCHEMA = {
    "type": "object",
    "required": ["channel", "body"],
    "additionalProperties": False,
    "properties": {
        "channel": {"type": "string"},
        "body": {"type": "string"},
    },
}
ACTION_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["message_id"],
    "additionalProperties": False,
    "properties": {"message_id": {"type": "string"}},
}


def _task_contract(**overrides):
    fields = {
        "title": "Ship the launch update",
        "description": "Research, review, wait, then publish.",
        "details": {},
        "owner_service_key": "writer",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _coordinator_selection() -> CoordinatorRuntimeSelection:
    bindings = tuple(
        TaskPlanServiceBinding(
            subscription_id=f"subscription-{service}",
            service_key=service,
            agent_id=f"agent-{service}",
        )
        for service in ("writer", "researcher")
    )
    return CoordinatorRuntimeSelection(root=bindings[0], bindings=bindings)


def _fold(plan: Plan) -> Plan:
    return coordinator_plan_from(
        plan,
        _task_contract(),
        coordinator=_coordinator_selection(),
    )


def _mixed_source_plan() -> Plan:
    return Plan(steps=[
        PlanStep(
            key="research",
            kind="subagent",
            execution_contract={"delivery": "inline", "effects": "read_only"},
            service_key="researcher",
            output_shape="TextResult",
            params={"prompt": "Research the launch."},
        ),
        PlanStep(
            key="review",
            kind="human",
            params={"prompt": "Approve the researched launch update."},
            depends_on=["research"],
        ),
        PlanStep(
            key="cooldown",
            kind="sleep",
            params={"seconds": 30},
            depends_on=["review"],
        ),
        PlanStep(
            key="publish",
            kind="action",
            service_key="writer",
            provider="slack",
            action_key="send_message",
            integration_id="integration-1",
            params={
                "channel": "launches",
                "body": "${{ steps.research.result.text }}",
            },
            expected_input_schema=ACTION_INPUT_SCHEMA,
            expected_output_schema=ACTION_OUTPUT_SCHEMA,
            depends_on=["cooldown"],
            risk_level="high",
            requires_approval=True,
        ),
    ])


async def _claim_v3_task(db, task, *, selection=None):
    selection = selection or (await resolve_task_execution(db, task)).coordinator
    assert selection is not None
    await ensure_task_coordinator_run(db, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(
        db, task_id=task.id, reason="v3_materialization_test",
    )
    assert wake is not None
    claim, reason = await claim_task_coordinator_wake(
        db,
        task_id=task.id,
        event_id=wake.event_id,
        wake_generation=wake.wake_generation,
        claim_token="v3-materialization-test-owner",
        selection=selection,
        lease_seconds=60,
    )
    assert reason == "claimed" and claim is not None
    await db.commit()
    return claim


@pytest.mark.asyncio
async def test_task_coordinator_v3_materializes_source_dag_behind_release_gate(db_session):
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="V3", status="active",
    )
    writer_agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Writer", status="active", config={},
    )
    researcher_agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Researcher", status="active", config={},
    )
    writer = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=writer_agent.id, service_key="writer", status="active",
    )
    researcher = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=researcher_agent.id, service_key="researcher", status="active",
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Launch", description="Prepare and publish.", status="in_progress",
        details={}, owner_service_key="writer", delegate_service_keys=["researcher"],
    )
    db_session.add_all([workspace, writer_agent, researcher_agent, writer, researcher, task])
    await db_session.commit()

    selection = (await resolve_task_execution(db_session, task)).coordinator
    assert selection is not None
    run = await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="initial_plan",
    )
    assert wake is not None
    claim, reason = await claim_task_coordinator_wake(
        db_session, task_id=task.id, event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token="v3-plan-owner",
        selection=selection, lease_seconds=60,
    )
    assert reason == "claimed" and claim is not None

    source = _mixed_source_plan()
    plan_row = await create_plan_from_dag(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace.id,
        task_id=task.id,
        agent_subscription_id=None,
        plan=source,
        task_coordinator_claim=claim,
        expected_selection=selection,
    )
    rows = list((await db_session.execute(
        select(ExecutionStep).where(ExecutionStep.plan_id == plan_row.id)
    )).scalars().all())
    by_key = {row.step_key: row for row in rows}
    assert len(rows) == len(source.steps)
    assert set(by_key) == {step.key for step in source.steps}
    assert Plan.model_validate(plan_row.plan_dag).model_dump(mode="json") == source.model_dump(mode="json")
    assert plan_row.dispatcher_state == {
        TASK_COORDINATOR_PLAN_VERSION_KEY: TASK_COORDINATOR_PLAN_VERSION,
        TASK_COORDINATOR_RUN_ID_KEY: run.id,
    }
    assert run.current_plan_id == plan_row.id
    assert run.phase == "waiting_plan"
    assert all(row.step_status == ExecutionStepStatus.PAUSED.value for row in rows)
    assert by_key["publish"].params == source.step_by_key("publish").params
    assert by_key["research"].params["_execution_contract"] == {
        "delivery": "inline", "effects": "read_only",
    }
    assert by_key["publish"].depends_on == ["cooldown"]
    assert (by_key["research"].resolved_subscription_id, by_key["research"].resolved_agent_id) == (
        researcher.id, researcher_agent.id,
    )
    assert (by_key["publish"].resolved_subscription_id, by_key["publish"].resolved_agent_id) == (
        writer.id, writer_agent.id,
    )
    assert by_key["review"].resolved_subscription_id is None
    assert by_key["cooldown"].resolved_subscription_id is None


@pytest.mark.asyncio
async def test_task_coordinator_v3_rejects_stale_claim_without_plan_rows(db_session):
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="V3", status="active",
    )
    agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Writer", status="active", config={},
    )
    writer = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=agent.id, service_key="writer", status="active",
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Draft", description="Draft the update.", status="in_progress",
        details={}, owner_service_key="writer", delegate_service_keys=[],
    )
    db_session.add_all([workspace, agent, writer, task])
    await db_session.commit()
    selection = (await resolve_task_execution(db_session, task)).coordinator
    assert selection is not None
    await ensure_task_coordinator_run(db_session, task=task, selection=selection)
    wake = await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="initial_plan",
    )
    assert wake is not None
    claim, reason = await claim_task_coordinator_wake(
        db_session, task_id=task.id, event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token="stale-owner",
        selection=selection, lease_seconds=60,
    )
    assert reason == "claimed" and claim is not None
    await stage_task_coordinator_wake(
        db_session, task_id=task.id, reason="supersede_plan",
    )

    with pytest.raises(ValueError, match="claim or factory selection is stale"):
        await create_plan_from_dag(
            db_session,
            entity_id=entity_id,
            workspace_id=workspace.id,
            task_id=task.id,
            agent_subscription_id=None,
            plan=Plan(steps=[PlanStep(
                key="draft", kind="subagent", service_key="writer",
                output_shape="TextResult", params={"prompt": "Draft the update."},
            )]),
            task_coordinator_claim=claim,
            expected_selection=selection,
        )
    assert list((await db_session.execute(
        select(ExecutionPlan).where(ExecutionPlan.task_id == task.id)
    )).scalars().all()) == []


@pytest.mark.asyncio
async def test_generated_coordinator_materializes_paused_typed_children(db_session):
    source = _mixed_source_plan()
    folded = _fold(source)
    plan_row = ExecutionPlan(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        plan_dag=folded.model_dump(mode="json"),
        status="draft",
        approval_required=False,
        execution_mode="live",
    )
    db_session.add(plan_row)
    await db_session.flush()

    rows = await materialize_plan_steps(db_session, plan_row.id, folded)

    assert len(rows) == 1 + len(source.steps)
    root = rows[0]
    assert root.step_key == "coordinate"
    assert root.step_status == ExecutionStepStatus.PENDING.value
    assert root.params[COORDINATOR_WAITING_CHILD_PARAM_KEY] is None

    children = {
        row.params[COORDINATOR_CHILD_ITEM_PARAM_KEY]: row
        for row in rows[1:]
    }
    assert list(children) == ["research", "review", "cooldown", "publish"]
    assert [children[key].kind for key in children] == [
        "subagent", "human", "sleep", "action",
    ]
    for item_key, child in children.items():
        assert child.step_key == coordinator_child_step_key(root.step_key, item_key)
        assert child.step_status == ExecutionStepStatus.PAUSED.value
        assert child.params[COORDINATOR_CHILD_PARAM_KEY] is True
        assert child.params[COORDINATOR_TYPED_CHILD_PARAM_KEY] is True
        assert child.params[COORDINATOR_CHILD_RELEASED_PARAM_KEY] is False
        assert child.params[COORDINATOR_PARENT_STEP_PARAM_KEY] == root.step_key
        assert child.params[COORDINATOR_CHILD_IDEMPOTENCY_PARAM_KEY] == (
            coordinator_child_idempotency_key(
                plan_row.id,
                root.step_key,
                item_key,
            )
        )
        assert child.params[ACTION_EFFECT_GENERATION_PARAM_KEY] == 0

    assert children["review"].depends_on == [children["research"].step_key]
    assert children["cooldown"].depends_on == [children["review"].step_key]
    assert children["publish"].depends_on == [children["cooldown"].step_key]
    assert children["review"].service_key is None
    assert children["cooldown"].service_key is None

    action = children["publish"]
    source_action = source.step_by_key("publish")
    assert action.service_key == source_action.service_key
    assert action.provider == source_action.provider
    assert action.action_key == source_action.action_key
    assert action.capability_id == source_action.capability_id
    assert action.integration_id == source_action.integration_id
    assert action.expected_input_schema == ACTION_INPUT_SCHEMA
    assert action.expected_output_schema == ACTION_OUTPUT_SCHEMA
    assert action.requires_approval is True
    assert action.risk_level == "high"
    assert "body" not in action.params, "dormant children must not expose unresolved args"
    assert action.params["source_contract"]["params"]["body"] == (
        "${{ steps.research.result.text }}"
    )
    assert source_action.params == {
        "channel": "launches",
        "body": "${{ steps.research.result.text }}",
    }, "materialization must not mutate the source Plan"


@pytest.mark.asyncio
async def test_generated_coordinator_allowlist_uses_active_task_subscriptions(db_session):
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="Coordinator", status="active",
    )
    agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Writer", status="active", config={},
    )
    writer = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=agent.id, service_key="writer", status="active",
    )
    inactive_researcher = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=agent.id, service_key="researcher", status="inactive",
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Research", description="Research the launch.", status="in_progress",
        details={}, owner_service_key="writer", delegate_service_keys=[],
    )
    db_session.add_all([workspace, agent, writer, inactive_researcher, task])
    await db_session.commit()

    source = Plan(steps=[
        PlanStep(
            key="research",
            kind="subagent",
            service_key="researcher",
            output_shape="TextResult",
            params={"prompt": "Research the launch."},
        ),
    ])
    claim = await _claim_v3_task(db_session, task)

    with pytest.raises(ValueError, match="exceed the active Task subscription scope"):
        await create_plan_from_dag(
            db_session,
            entity_id=entity_id,
            workspace_id=workspace.id,
            task_id=task.id,
            agent_subscription_id=writer.id,
            plan=source,
            task_coordinator_claim=claim,
        )


@pytest.mark.asyncio
async def test_trusted_generated_coordinator_cannot_replace_factory_root(db_session):
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="Coordinator", status="active",
    )
    agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Writer", status="active", config={},
    )
    writer = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=agent.id, service_key="writer", status="active",
    )
    researcher = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=agent.id, service_key="researcher", status="active",
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Research", description="Research the launch.", status="in_progress",
        details={}, owner_service_key="writer", delegate_service_keys=["researcher"],
    )
    db_session.add_all([workspace, agent, writer, researcher, task])
    await db_session.commit()

    forged_selection = CoordinatorRuntimeSelection(
        root=TaskPlanServiceBinding(
            subscription_id=researcher.id,
            service_key="researcher",
            agent_id=agent.id,
        ),
        bindings=(
            TaskPlanServiceBinding(
                subscription_id=researcher.id,
                service_key="researcher",
                agent_id=agent.id,
            ),
            TaskPlanServiceBinding(
                subscription_id=writer.id,
                service_key="writer",
                agent_id=agent.id,
            ),
        ),
    )
    source = Plan(steps=[
        PlanStep(
            key="research",
            kind="subagent",
            service_key="researcher",
            output_shape="TextResult",
            params={"prompt": "Research the launch."},
        ),
    ])
    claim = await _claim_v3_task(db_session, task)

    with pytest.raises(CoordinatorSelectionDriftError, match="coordinator selection changed while planning"):
        await create_plan_from_dag(
            db_session,
            entity_id=entity_id,
            workspace_id=workspace.id,
            task_id=task.id,
            agent_subscription_id=None,
            plan=source,
            expected_selection=forged_selection,
            task_coordinator_claim=claim,
        )


@pytest.mark.asyncio
async def test_typed_children_pin_exact_subscription_and_agent(db_session):
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="Coordinator", status="active",
    )
    writer_agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Writer", status="active", config={},
    )
    researcher_agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Researcher", status="active", config={},
    )
    writer = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=writer_agent.id, service_key="writer", status="active",
    )
    researcher = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=researcher_agent.id, service_key="researcher", status="active",
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Launch", description="Prepare and publish.", status="in_progress",
        details={}, owner_service_key="writer", delegate_service_keys=["researcher"],
    )
    db_session.add_all([
        workspace, writer_agent, researcher_agent, writer, researcher, task,
    ])
    await db_session.commit()
    expected_selection = (
        await resolve_task_execution(db_session, task)
    ).coordinator
    assert expected_selection is not None

    source = _mixed_source_plan()
    claim = await _claim_v3_task(db_session, task, selection=expected_selection)
    plan_row = await create_plan_from_dag(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace.id,
        task_id=task.id,
        agent_subscription_id=None,
        plan=source,
        expected_selection=expected_selection,
        task_coordinator_claim=claim,
    )
    rows = list((await db_session.execute(
        select(ExecutionStep)
        .where(ExecutionStep.plan_id == plan_row.id)
        .order_by(ExecutionStep.step_key)
    )).scalars().all())
    by_item = {row.step_key: row for row in rows}

    assert plan_row.agent_subscription_id == writer.id
    assert set(by_item) == {step.key for step in source.steps}
    assert (
        by_item["research"].resolved_subscription_id,
        by_item["research"].resolved_agent_id,
    ) == (researcher.id, researcher_agent.id)
    assert (
        by_item["publish"].resolved_subscription_id,
        by_item["publish"].resolved_agent_id,
    ) == (writer.id, writer_agent.id)
    assert by_item["review"].resolved_subscription_id is None
    assert by_item["cooldown"].resolved_subscription_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift",
    ["deactivate", "replacement", "owner", "delegates"],
)
async def test_expected_selection_rejects_runtime_drift_before_plan_rows(
    db_session,
    drift,
):
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="Fence", status="active",
    )
    writer_agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Writer", status="active", config={},
    )
    researcher_agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Researcher", status="active", config={},
    )
    writer = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=writer_agent.id, service_key="writer", status="active",
    )
    researcher = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=researcher_agent.id, service_key="researcher", status="active",
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Fence selection", description="Keep one exact runtime.",
        status="in_progress", details={}, owner_service_key="writer",
        delegate_service_keys=["researcher"],
    )
    db_session.add_all([
        workspace, writer_agent, researcher_agent, writer, researcher, task,
    ])
    await db_session.commit()

    expected_selection = (
        await resolve_task_execution(db_session, task)
    ).coordinator
    assert expected_selection is not None
    claim = await _claim_v3_task(db_session, task, selection=expected_selection)

    if drift == "deactivate":
        writer.status = "inactive"
    elif drift == "replacement":
        writer.status = "inactive"
        db_session.add(AgentSubscription(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace.id,
            agent_id=researcher_agent.id,
            service_key="writer",
            status="active",
        ))
    elif drift == "owner":
        task.owner_service_key = "researcher"
        task.delegate_service_keys = ["writer"]
    else:
        task.delegate_service_keys = []
    await db_session.commit()

    source = Plan(steps=[
        PlanStep(
            key="draft",
            kind="subagent",
            execution_contract={"delivery": "inline", "effects": "read_only"},
            service_key="writer",
            output_shape="TextResult",
            params={"prompt": "Draft."},
        ),
    ])
    with pytest.raises(
        CoordinatorSelectionDriftError,
        match="coordinator selection changed while planning",
    ):
        await create_plan_from_dag(
            db_session,
            entity_id=entity_id,
            workspace_id=workspace.id,
            task_id=task.id,
            agent_subscription_id=expected_selection.root.subscription_id,
            plan=source,
            expected_selection=expected_selection,
            task_coordinator_claim=claim,
        )

    assert list((await db_session.execute(
        select(ExecutionPlan).where(ExecutionPlan.task_id == task.id)
    )).scalars().all()) == []
    assert list((await db_session.execute(
        select(ExecutionStep).join(
            ExecutionPlan,
            ExecutionPlan.id == ExecutionStep.plan_id,
        ).where(ExecutionPlan.task_id == task.id)
    )).scalars().all()) == []


@pytest.mark.asyncio
async def test_raw_task_plan_uses_factory_root_and_persists_v3_source_dag(db_session):
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="Coordinator", status="active",
    )
    writer_agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Writer", status="active", config={},
    )
    researcher_agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Researcher", status="active", config={},
    )
    writer = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=writer_agent.id, service_key="writer", status="active",
    )
    researcher = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=researcher_agent.id, service_key="researcher", status="active",
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Draft", description="Draft the launch note.", status="in_progress",
        details={}, owner_service_key=None, delegate_service_keys=[],
    )
    db_session.add_all([
        workspace, writer_agent, researcher_agent, writer, researcher, task,
    ])
    await db_session.commit()

    # The authored DAG has only a writer step. The ownerless Task's root is
    # selected by the factory's stable service scope, never from that step.
    source = Plan(steps=[
        PlanStep(
            key="draft",
            kind="subagent",
            service_key="writer",
            output_shape="TextResult",
            params={"prompt": "Draft the launch note."},
            execution_contract={"delivery": "inline", "effects": "read_only"},
        ),
    ])
    claim = await _claim_v3_task(db_session, task)
    plan_row = await create_plan_from_dag(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace.id,
        task_id=task.id,
        agent_subscription_id=None,
        plan=source,
        task_coordinator_claim=claim,
    )

    persisted = Plan.model_validate(plan_row.plan_dag)
    assert [step.key for step in persisted.steps] == ["draft"]
    assert persisted.steps[0].service_key == "writer"
    assert plan_row.agent_subscription_id == researcher.id
    rows = list((await db_session.execute(
        select(ExecutionStep)
        .where(ExecutionStep.plan_id == plan_row.id)
        .order_by(ExecutionStep.step_key)
    )).scalars().all())
    assert len(rows) == 1
    assert rows[0].service_key == "writer"
    assert rows[0].resolved_subscription_id == writer.id


@pytest.mark.asyncio
async def test_non_coordinated_task_runtime_cannot_persist_a_plan(db_session):
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="Approval", status="active",
    )
    agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Writer", status="active", config={},
    )
    subscription = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=agent.id, service_key="writer", status="active",
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Approve", description="Approve the launch.", status="in_progress",
        task_type="approval", details={}, owner_service_key="writer",
    )
    db_session.add_all([workspace, agent, subscription, task])
    await db_session.commit()

    source = Plan(steps=[
        PlanStep(
            key="draft",
            kind="subagent",
            service_key="writer",
            output_shape="TextResult",
            params={"prompt": "Draft."},
        ),
    ])
    with pytest.raises(ValueError, match="human_decision.*cannot create a new Plan"):
        await create_plan_from_dag(
            db_session,
            entity_id=entity_id,
            workspace_id=workspace.id,
            task_id=task.id,
            agent_subscription_id=subscription.id,
            plan=source,
            task_coordinator_claim=SimpleNamespace(),
        )

    plans = list((await db_session.execute(
        select(ExecutionPlan).where(ExecutionPlan.task_id == task.id)
    )).scalars().all())
    assert plans == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "waiting_on_customer", "completed"])
async def test_workspace_task_must_be_in_progress_to_persist_plan(
    db_session,
    status,
):
    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(), entity_id=entity_id, name="Lifecycle", status="active",
    )
    agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Writer", status="active", config={},
    )
    subscription = AgentSubscription(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        agent_id=agent.id, service_key="writer", status="active",
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title="Lifecycle gate", description="Draft a note.", status=status,
        task_type="general", details={}, owner_service_key="writer",
        owner_subscription_id=subscription.id,
    )
    db_session.add_all([workspace, agent, subscription, task])
    await db_session.commit()

    source = Plan(steps=[
        PlanStep(
            key="draft",
            kind="subagent",
            service_key="writer",
            output_shape="TextResult",
            params={"prompt": "Draft."},
        ),
    ])
    with pytest.raises(
        ValueError,
        match=rf"Task status '{status}' cannot create a new Plan",
    ):
        await create_plan_from_dag(
            db_session,
            entity_id=entity_id,
            workspace_id=workspace.id,
            task_id=task.id,
            agent_subscription_id=subscription.id,
            plan=source,
            task_coordinator_claim=SimpleNamespace(),
        )

    plans = list((await db_session.execute(
        select(ExecutionPlan).where(ExecutionPlan.task_id == task.id)
    )).scalars().all())
    assert plans == []


@pytest.mark.asyncio
async def test_entity_level_legacy_task_plan_remains_unfolded(db_session):
    entity_id = generate_ulid()
    agent = Agent(
        id=generate_ulid(), entity_id=entity_id, name="Personal", status="active", config={},
    )
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=None,
        agent_id=agent.id, title="Personal draft", description="Draft a note.",
        status="in_progress", details={},
    )
    db_session.add_all([agent, task])
    await db_session.commit()

    source = Plan(steps=[
        PlanStep(
            key="draft",
            kind="subagent",
            service_key="writer",
            output_shape="TextResult",
            params={"prompt": "Draft."},
        ),
    ])
    plan_row = await create_plan_from_dag(
        db_session,
        entity_id=entity_id,
        workspace_id=None,
        task_id=task.id,
        agent_subscription_id=None,
        plan=source,
    )

    persisted = Plan.model_validate(plan_row.plan_dag)
    assert [step.key for step in persisted.steps] == ["draft"]
