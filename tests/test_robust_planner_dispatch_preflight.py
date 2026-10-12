"""P2 — a Plan is persisted only when every service step can actually be leased.

The dispatcher used to discover "no subscription", "no bound worker" and
"agent may not run this action" one lease at a time, minutes after the plan
was accepted.  ``plan_dispatch_preflight`` answers the same questions from
the same tables at planning time, so an undispatchable plan is rejected as a
``CapabilityError`` before a single credit is spent.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.task import Task
from packages.core.models.worker import SubscriptionWorker, Worker
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.plans import planner as planner_mod
from packages.core.plans.dispatchability import plan_dispatch_preflight
from packages.core.plans.schema import Plan, PlanStep
from packages.core.services.task_coordinator_wakeup import TaskCoordinatorClaim
from packages.core.workers.registry import INTERNAL_WORKER_KIND


async def _scenario(db, *, service_keys=("writer",), worker_kinds=None, title="Preflight"):
    """Workspace + Agent + one active subscription per service key.

    ``worker_kinds`` seeds a pre-existing internal worker with exactly those
    ``supported_kinds`` (a narrow legacy row); ``None`` leaves the entity
    without any worker so the preflight has to bootstrap one.
    """
    entity_id = generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name=title, status="active")
    agent = Agent(id=generate_ulid(), entity_id=entity_id, name="Writer", status="active", config={})
    subscriptions = [
        AgentSubscription(
            id=generate_ulid(), entity_id=entity_id, agent_id=agent.id,
            workspace_id=workspace.id, service_key=key, status="active",
        )
        for key in service_keys
    ]
    task = Task(
        id=generate_ulid(), entity_id=entity_id, workspace_id=workspace.id,
        title=title, status="in_progress", details={},
        owner_service_key=service_keys[0], delegate_service_keys=list(service_keys[1:]),
    )
    rows = [workspace, agent, *subscriptions, task]
    worker = None
    if worker_kinds is not None:
        worker = Worker(
            id=generate_ulid(), entity_id=entity_id, kind=INTERNAL_WORKER_KIND,
            display_name="Narrow internal", status="active",
            capabilities={"supported_kinds": list(worker_kinds), "max_risk_level": "high"},
            monthly_spent_usd=Decimal("0"), auto_pause_on_budget=True,
        )
        rows.append(worker)
    db.add_all(rows)
    await db.flush()
    return SimpleNamespace(
        entity_id=entity_id, workspace=workspace, agent=agent,
        subscriptions=subscriptions, subscription=subscriptions[0], task=task, worker=worker,
    )


def _agent_step(key, kind="subagent", service_key="writer", **overrides):
    fields = {
        "key": key, "kind": kind, "service_key": service_key,
        "output_shape": "TextResult", "params": {"prompt": f"Do {key}."},
        "execution_contract": {"delivery": "inline", "effects": "read_only"},
    }
    fields.update(overrides)
    return PlanStep(**fields)


def _planning_claim(task_id):
    return TaskCoordinatorClaim(
        run_id="preflight-run", task_id=task_id, event_id="preflight-event",
        wake_generation="preflight-generation", claim_token="preflight-claim",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        state_version=1,
    )


async def _internal_workers(db, entity_id):
    return list((await db.execute(
        select(Worker).where(Worker.entity_id == entity_id, Worker.kind == INTERNAL_WORKER_KIND)
    )).scalars().all())


class _UntouchableDB:
    def __getattr__(self, name):  # pragma: no cover - only hit on regression
        raise AssertionError(f"preflight touched the database via {name!r} for a non-workspace task")


@pytest.mark.asyncio
async def test_non_workspace_task_has_nothing_to_preflight():
    task = SimpleNamespace(entity_id="ent", workspace_id=None)
    plan = Plan(steps=[_agent_step("draft")])

    assert await plan_dispatch_preflight(_UntouchableDB(), plan=plan, task=task) == []


@pytest.mark.asyncio
async def test_preflight_bootstraps_the_internal_worker_and_binds_the_subscription(db_session):
    scenario = await _scenario(db_session)
    plan = Plan(steps=[
        _agent_step("research", kind="llm"),
        _agent_step("draft", depends_on=["research"]),
    ])

    assert await plan_dispatch_preflight(db_session, plan=plan, task=scenario.task) == []

    workers = await _internal_workers(db_session, scenario.entity_id)
    assert len(workers) == 1 and workers[0].status == "active"
    binding = (await db_session.execute(
        select(SubscriptionWorker).where(
            SubscriptionWorker.subscription_id == scenario.subscription.id,
            SubscriptionWorker.worker_id == workers[0].id,
        )
    )).scalar_one_or_none()
    assert binding is not None

    # Idempotent: a second pass neither duplicates the worker nor complains.
    assert await plan_dispatch_preflight(db_session, plan=plan, task=scenario.task) == []
    assert len(await _internal_workers(db_session, scenario.entity_id)) == 1


@pytest.mark.asyncio
async def test_steps_without_a_service_are_ignored(db_session):
    scenario = await _scenario(db_session)
    plan = Plan(steps=[
        PlanStep(key="wait", kind="sleep", params={"seconds": 0}),
        PlanStep(key="confirm", kind="human", params={"prompt": "Confirm."}, depends_on=["wait"]),
    ])

    assert await plan_dispatch_preflight(db_session, plan=plan, task=scenario.task) == []


@pytest.mark.asyncio
async def test_missing_subscription_is_reported_per_step(db_session):
    scenario = await _scenario(db_session)
    plan = Plan(steps=[
        _agent_step("draft"),
        _agent_step("design", service_key="designer", depends_on=["draft"]),
        _agent_step("review", kind="llm", service_key="designer", depends_on=["design"]),
    ])

    errors = await plan_dispatch_preflight(db_session, plan=plan, task=scenario.task)

    assert errors == [
        "step design: service 'designer' has no active subscription in this workspace",
        "step review: service 'designer' has no active subscription in this workspace",
    ]


@pytest.mark.asyncio
async def test_inactive_subscription_does_not_count(db_session):
    scenario = await _scenario(db_session)
    scenario.subscription.status = "paused"
    await db_session.flush()
    plan = Plan(steps=[_agent_step("draft")])

    assert await plan_dispatch_preflight(db_session, plan=plan, task=scenario.task) == [
        "step draft: service 'writer' has no active subscription in this workspace",
    ]


@pytest.mark.asyncio
async def test_subscription_from_another_workspace_does_not_count(db_session):
    scenario = await _scenario(db_session)
    other_workspace = Workspace(id=generate_ulid(), entity_id=scenario.entity_id, name="Other", status="active")
    db_session.add_all([
        other_workspace,
        AgentSubscription(
            id=generate_ulid(), entity_id=scenario.entity_id, agent_id=scenario.agent.id,
            workspace_id=other_workspace.id, service_key="designer", status="active",
        ),
    ])
    await db_session.flush()
    plan = Plan(steps=[_agent_step("design", service_key="designer")])

    assert await plan_dispatch_preflight(db_session, plan=plan, task=scenario.task) == [
        "step design: service 'designer' has no active subscription in this workspace",
    ]


@pytest.mark.asyncio
async def test_worker_that_cannot_run_the_kind_is_reported(db_session):
    scenario = await _scenario(db_session, worker_kinds=["subagent"])
    plan = Plan(steps=[
        _agent_step("draft"),
        _agent_step("polish", kind="llm", depends_on=["draft"]),
    ])

    errors = await plan_dispatch_preflight(db_session, plan=plan, task=scenario.task)

    assert errors == ["step polish: no active worker is bound to service 'writer' for kind 'llm'"]
    # The narrow row is reused, never shadowed by a second internal worker.
    assert len(await _internal_workers(db_session, scenario.entity_id)) == 1


@pytest.mark.asyncio
async def test_paused_external_worker_does_not_satisfy_the_kind(db_session):
    scenario = await _scenario(db_session, worker_kinds=["subagent"])
    external = Worker(
        id=generate_ulid(), entity_id=scenario.entity_id, kind="custom_http",
        display_name="Paused runner", status="paused",
        capabilities={"supported_kinds": ["llm", "subagent"], "max_risk_level": "high"},
        monthly_spent_usd=Decimal("0"), auto_pause_on_budget=True,
    )
    db_session.add_all([
        external,
        SubscriptionWorker(subscription_id=scenario.subscription.id, worker_id=external.id, priority=1),
    ])
    await db_session.flush()
    plan = Plan(steps=[_agent_step("polish", kind="llm")])

    assert await plan_dispatch_preflight(db_session, plan=plan, task=scenario.task) == [
        "step polish: no active worker is bound to service 'writer' for kind 'llm'",
    ]

    external.status = "active"
    await db_session.flush()
    assert await plan_dispatch_preflight(db_session, plan=plan, task=scenario.task) == []


@pytest.mark.asyncio
async def test_action_binding_is_checked_with_the_shared_predicate(db_session):
    scenario = await _scenario(db_session)
    server = MCPServer(
        id=generate_ulid(), server_key=f"preflight_{generate_ulid().lower()}",
        name="Preflight provider", transport="http",
        endpoint="https://example.invalid/mcp", auth_type="none", status="active",
    )
    db_session.add(server)
    await db_session.flush()
    plan = Plan(steps=[PlanStep(
        key="notify", kind="action", service_key="writer",
        provider=server.server_key, action_key="send_message", params={"to": "ops"},
    )])

    errors = await plan_dispatch_preflight(db_session, plan=plan, task=scenario.task)
    assert errors == [
        f"step notify: agent {scenario.agent.id} is not bound to MCP provider {server.server_key!r}",
    ]

    binding = AgentMCPBinding(
        id=generate_ulid(), agent_id=scenario.agent.id, mcp_server_id=server.id,
        allowed_tools=["list_messages"], status="active",
    )
    db_session.add(binding)
    await db_session.flush()
    errors = await plan_dispatch_preflight(db_session, plan=plan, task=scenario.task)
    assert errors == [
        f"step notify: agent {scenario.agent.id} is not allowed to execute "
        f"{server.server_key}.send_message",
    ]

    binding.allowed_tools = ["list_messages", "send_message"]
    await db_session.flush()
    assert await plan_dispatch_preflight(db_session, plan=plan, task=scenario.task) == []


# ── plan_task integration ─────────────────────────────────────────────


@contextlib.asynccontextmanager
async def _noop_billing(*_args, **_kwargs):
    yield


def _patch_plan_task(monkeypatch, scenario, generated: Plan) -> dict:
    """Keep the real admission locks + preflight; stub the provider and persistence."""
    async def _context(_db, _task):
        return planner_mod._Context(
            workspace=None,
            subscriptions=list(scenario.subscriptions),
            agents_by_id={},
            allowed_service_keys={sub.service_key for sub in scenario.subscriptions},
            provider_actions={},
        )

    async def _generate(_task, _context):
        return generated

    persisted: dict = {}

    async def _persist(db, **kwargs):
        persisted["plan"] = kwargs["plan"]
        return SimpleNamespace(id="plan-persisted", status="draft")

    monkeypatch.setattr(planner_mod, "_gather_context", _context)
    monkeypatch.setattr(planner_mod, "_generate_plan", _generate)
    monkeypatch.setattr(planner_mod, "runtime_planner_llm_billing_context", _noop_billing)
    monkeypatch.setattr(planner_mod, "create_plan_from_dag", _persist)
    return persisted


@pytest.mark.asyncio
async def test_plan_task_refuses_an_undispatchable_plan(db_session, monkeypatch):
    scenario = await _scenario(db_session, worker_kinds=["subagent"])
    await db_session.commit()
    persisted = _patch_plan_task(monkeypatch, scenario, Plan(steps=[_agent_step("draft", kind="llm")]))

    with pytest.raises(
        planner_mod.CapabilityError,
        match=r"plan is not dispatchable: step draft: no active worker is bound to service 'writer' for kind 'llm'",
    ):
        await planner_mod.plan_task(
            db_session, scenario.task.id, execution_mode="live",
            task_coordinator_claim=_planning_claim(scenario.task.id),
        )

    assert persisted == {}


@pytest.mark.asyncio
async def test_task_capability_gap_stops_before_billing_and_provider(db_session, monkeypatch):
    scenario = await _scenario(db_session)
    scenario.task.details = {
        "runtime_context": {"required_capabilities": ["file.write"]},
    }
    await db_session.commit()
    persisted = _patch_plan_task(
        monkeypatch, scenario, Plan(steps=[_agent_step("draft")]),
    )

    async def unexpected_billing():
        pytest.fail("Task capability gap must stop before billing")

    async def unexpected_generate(*_args, **_kwargs):
        pytest.fail("Task capability gap must stop before provider")

    monkeypatch.setattr(planner_mod, "_generate_plan", unexpected_generate)

    with pytest.raises(planner_mod.TaskCapabilityGapError) as exc:
        await planner_mod.plan_task(
            db_session,
            scenario.task.id,
            execution_mode="live",
            before_provider=unexpected_billing,
            task_coordinator_claim=_planning_claim(scenario.task.id),
        )

    assert exc.value.missing_capabilities == ("file.write",)
    assert persisted == {}


@pytest.mark.asyncio
async def test_plan_task_persists_once_every_step_can_be_leased(db_session, monkeypatch):
    scenario = await _scenario(db_session)
    await db_session.commit()
    persisted = _patch_plan_task(monkeypatch, scenario, Plan(steps=[
        _agent_step("research", kind="llm"),
        _agent_step("draft", depends_on=["research"]),
    ]))

    await planner_mod.plan_task(
        db_session, scenario.task.id, execution_mode="live",
        task_coordinator_claim=_planning_claim(scenario.task.id),
    )

    assert [step.key for step in persisted["plan"].steps] == ["research", "draft"]
    workers = await _internal_workers(db_session, scenario.entity_id)
    assert [worker.status for worker in workers] == ["active"]
