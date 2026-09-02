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
        ("cloud", "local-k8s", False),
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
    monkeypatch.setenv("MANOR_ENV", "local-k8s")

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
    monkeypatch.setenv("MANOR_ENV", "local-k8s")
    allowed, reason = check_worker_budget(worker)
    assert allowed is False
    assert "worker over budget" in reason

    monkeypatch.setenv("MANOR_ENV", "production")
    allowed, reason = check_worker_budget(worker)
    assert allowed is False
    assert "worker over budget" in reason


@pytest.mark.asyncio
async def test_local_k8s_ai_credit_gate_is_unlimited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from packages.core.services.plan_gate import check

    class NoDatabaseAccess:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("local AI credit bypass must not query billing data")

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("MANOR_ENV", "local-k8s")

    result = await check(NoDatabaseAccess(), "entity-local", "ai_budget_usd")

    assert result.allowed is True
    assert result.limit is None


@pytest.mark.asyncio
async def test_local_k8s_usage_summary_tracks_usage_without_a_balance(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.billing import CreditUsageLog
    from packages.core.models.user import Entity
    from packages.core.services.plan_enforcement import get_usage_summary

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("MANOR_ENV", "local-k8s")
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
    monkeypatch.setenv("MANOR_ENV", "local-k8s")
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

    async def capture_alert(_workspace, _spent, state):
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
async def test_local_k8s_legacy_cost_meter_never_applies_a_plan_budget(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import Entity
    from packages.core.services.plan_enforcement import record_ai_cost

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("MANOR_ENV", "local-k8s")
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


