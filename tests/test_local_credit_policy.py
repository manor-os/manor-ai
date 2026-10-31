from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from auth_helpers import register_user_and_get_token


@pytest.mark.parametrize(
    ("deployment_mode", "environment", "enabled"),
    [
        ("oss", "local", False),
        ("cloud", "local", False),
        ("cloud", "development", False),
        ("cloud", "staging", True),
        ("cloud", "production", True),
    ],
)
def test_ai_credit_limit_policy_matches_deployment_boundary(
    monkeypatch: pytest.MonkeyPatch,
    deployment_mode: str,
    environment: str,
    enabled: bool,
) -> None:
    from packages.core.constants.plans import ai_credit_limits_enabled

    monkeypatch.setenv("DEPLOYMENT_MODE", deployment_mode)
    monkeypatch.setenv("MANOR_ENV", environment)

    assert ai_credit_limits_enabled() is enabled


@pytest.mark.asyncio
async def test_local_unlimited_workspace_budget_gate_still_enforces_operator_cap(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    from packages.core.budget.enforcement import check_workspace_budget
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Workspace

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("MANOR_ENV", "local")

    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Local capped Workspace",
        monthly_budget_usd=Decimal("1"),
        monthly_spent_usd=Decimal("2"),
        auto_pause_on_budget=True,
    )
    db_session.add(workspace)
    await db_session.flush()

    allowed, reason = await check_workspace_budget(db_session, workspace.id)
    assert allowed is False
    assert "workspace over budget" in reason


def test_worker_budget_gate_is_independent_of_tenant_credit_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.core.budget.enforcement import check_worker_budget

    worker = SimpleNamespace(
        monthly_spent_usd=Decimal("5"),
        monthly_budget_usd=Decimal("1"),
        auto_pause_on_budget=True,
    )
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("MANOR_ENV", "local")
    allowed, reason = check_worker_budget(worker)
    assert allowed is False
    assert "worker over budget" in reason

    monkeypatch.setenv("MANOR_ENV", "production")
    allowed, reason = check_worker_budget(worker)
    assert allowed is False
    assert "worker over budget" in reason


@pytest.mark.asyncio
async def test_local_ai_credit_gate_is_unlimited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.core.services.plan_gate import check

    class NoDatabaseAccess:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("local AI credit bypass must not query billing data")

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("MANOR_ENV", "local")

    result = await check(NoDatabaseAccess(), "entity-local", "ai_budget_usd")

    assert result.allowed is True
    assert result.limit is None


@pytest.mark.asyncio
async def test_local_usage_summary_tracks_usage_without_a_balance(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.billing import CreditUsageLog
    from packages.core.models.user import Entity
    from packages.core.services.plan_enforcement import get_usage_summary

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("MANOR_ENV", "local")
    entity = Entity(
        id=generate_ulid(),
        name="Local unlimited usage",
        plan_id="plan_free",
        settings={
            "ai_usage_usd": 999.0,
            "ai_provider_cost_usd": 750.0,
            "total_credits": 100,
            "used_credits": 100,
        },
    )
    db_session.add(entity)
    db_session.add(CreditUsageLog(
        entity_id=entity.id,
        total_credit=42,
        total_tokens=4200,
        cost_usd=0.042,
    ))
    await db_session.commit()

    summary = await get_usage_summary(db_session, entity.id)

    assert summary["ai_credits_unlimited"] is True
    assert summary["ai_budget_usd"] is None
    assert summary["credits_total"] is None
    assert summary["credits_remaining"] is None
    assert summary["credits_used"] == 42
    assert summary["ai_usage_usd"] == 999.0
    await db_session.refresh(entity)
    assert entity.settings["ai_usage_usd"] == 999.0


@pytest.mark.asyncio
async def test_local_unlimited_workspace_usage_honors_custom_budget_alerts(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    from packages.core.budget import aggregation
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Workspace

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("MANOR_ENV", "local")
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=generate_ulid(),
        name="Local unlimited Workspace",
        monthly_budget_usd=Decimal("1"),
        monthly_spent_usd=Decimal("1"),
        auto_pause_on_budget=True,
        budget_alert_state="normal",
    )
    db_session.add(workspace)
    await db_session.flush()

    alerts: list[str] = []

    async def capture_alert(_db, _workspace, _spent, state):
        alerts.append(state)

    monkeypatch.setattr(aggregation, "_post_budget_alert", capture_alert)
    result = await aggregation.accumulate_workspace_ai_cost(
        db_session,
        workspace_id=workspace.id,
        cost_usd=0.5,
    )

    assert result["workspace_total"] == 1.5
    assert result["workspace_pct"] == 1.5
    assert result["alert_emitted"] == "critical_100"
    assert alerts == ["critical_100"]
    assert workspace.budget_alert_state == "critical_100"


@pytest.mark.asyncio
async def test_worker_llm_step_cost_is_attributed_to_plan_without_double_counting_budget(
    db_session,
) -> None:
    from datetime import datetime, timedelta, timezone

    from packages.core.budget import aggregation
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.worker import Worker, WorkLease
    from packages.core.models.workspace import Workspace

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="LLM budget accounting",
        # Simulate the provider-call settlement that already happened through
        # record_llm_usage before Dispatcher completes the worker step.
        monthly_spent_usd=Decimal("0.70"),
    )
    worker = Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind="internal",
        display_name="Internal worker",
        capabilities={"supported_kinds": ["llm"]},
        monthly_spent_usd=Decimal("0"),
        status="active",
    )
    plan = ExecutionPlan(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace.id,
        status="running",
        execution_mode="live",
        approval_required=False,
        plan_dag={"steps": []},
        cost_tracking={},
    )
    step = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan.id,
        entity_id=entity_id,
        workspace_id=workspace.id,
        step_key="draft",
        kind="llm",
        params={"prompt": "draft"},
        depends_on=[],
        step_status="done",
        cost={
            "usd": 0.70,
            "llm_usd": 0.70,
            "budget_usd": 0,
            "llm_tokens_input": 100,
            "llm_tokens_output": 25,
        },
    )
    lease = WorkLease(
        id=generate_ulid(),
        step_id=step.id,
        plan_id=plan.id,
        entity_id=entity_id,
        workspace_id=workspace.id,
        worker_id=worker.id,
        status="completed",
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add_all([workspace, worker, plan, step, lease])
    await db_session.flush()

    result = await aggregation.accumulate_step_cost(
        db_session,
        step,
        lease,
        notify=False,
    )

    assert result["usd"] == 0.70
    assert result["budget_usd"] == 0
    assert plan.cost_tracking["usd"] == 0.70
    assert plan.cost_tracking["llm_tokens_input"] == 100
    assert workspace.monthly_spent_usd == Decimal("0.70")
    assert worker.monthly_spent_usd == Decimal("0")


@pytest.mark.asyncio
async def test_zero_priced_worker_step_still_rolls_up_plan_token_counters(
    db_session,
) -> None:
    from packages.core.budget import aggregation
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep

    entity_id = generate_ulid()
    plan = ExecutionPlan(
        id=generate_ulid(),
        entity_id=entity_id,
        status="running",
        execution_mode="live",
        approval_required=False,
        plan_dag={"steps": []},
        cost_tracking={},
    )
    step = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan.id,
        entity_id=entity_id,
        step_key="free-model",
        kind="llm",
        params={"prompt": "draft"},
        depends_on=[],
        step_status="done",
        cost={
            "usd": 0,
            "budget_usd": 0,
            "llm_tokens_input": 40,
            "llm_tokens_output": 10,
        },
    )
    db_session.add_all([plan, step])
    await db_session.flush()

    result = await aggregation.accumulate_step_cost(
        db_session,
        step,
        notify=False,
    )

    assert result["usd"] == 0
    assert plan.cost_tracking["usd"] == 0
    assert plan.cost_tracking["llm_tokens_input"] == 40
    assert plan.cost_tracking["llm_tokens_output"] == 10


@pytest.mark.asyncio
async def test_action_step_attributes_total_to_plan_but_only_action_to_budget(
    db_session,
) -> None:
    from datetime import datetime, timedelta, timezone

    from packages.core.budget import aggregation
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.worker import Worker, WorkLease
    from packages.core.models.workspace import Workspace

    entity_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Action accounting",
        # The LLM portion has already entered the provider usage ledger.
        monthly_spent_usd=Decimal("0.05"),
    )
    worker = Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind="internal",
        display_name="Internal worker",
        capabilities={"supported_kinds": ["action"]},
        monthly_spent_usd=Decimal("0"),
        status="active",
    )
    plan = ExecutionPlan(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace.id,
        status="running",
        execution_mode="live",
        approval_required=False,
        plan_dag={"steps": []},
        cost_tracking={},
    )
    step = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan.id,
        entity_id=entity_id,
        workspace_id=workspace.id,
        step_key="publish",
        kind="action",
        params={},
        depends_on=[],
        step_status="done",
        cost={
            "usd": 0.30,
            "llm_usd": 0.05,
            "budget_usd": 0.25,
            "llm_tokens_input": 100,
            "llm_tokens_output": 20,
        },
    )
    lease = WorkLease(
        id=generate_ulid(),
        step_id=step.id,
        plan_id=plan.id,
        entity_id=entity_id,
        workspace_id=workspace.id,
        worker_id=worker.id,
        status="completed",
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add_all([workspace, worker, plan, step, lease])
    await db_session.flush()

    result = await aggregation.accumulate_step_cost(
        db_session,
        step,
        lease,
        notify=False,
    )

    assert result["usd"] == pytest.approx(0.30)
    assert result["budget_usd"] == pytest.approx(0.25)
    assert plan.cost_tracking["usd"] == pytest.approx(0.30)
    assert workspace.monthly_spent_usd == Decimal("0.30")
    assert worker.monthly_spent_usd == Decimal("0.25")


@pytest.mark.asyncio
async def test_local_legacy_cost_meter_never_applies_a_plan_budget(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import Entity
    from packages.core.services.plan_enforcement import record_ai_cost

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("MANOR_ENV", "local")
    entity = Entity(
        id=generate_ulid(),
        name="Local unlimited metering",
        plan_id="plan_free",
        settings={"ai_usage_usd": 999.0},
    )
    db_session.add(entity)
    await db_session.commit()

    result = await record_ai_cost(db_session, entity.id, 1.0, model="test-model")

    assert result["allowed"] is True
    assert result["budget"] is None
    assert result["total_usage"] > 999.0


