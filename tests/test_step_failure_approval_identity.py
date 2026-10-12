"""Failed attempts get recovery decisions without replaying their spent grants."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import BigInteger, Integer, JSON, MetaData, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from packages.core.constants.approvals import ApprovalOutcome
from packages.core.dispatcher.service import Dispatcher, _failed_step_review_operation_id
from packages.core.governance import approvals
from packages.core.governance.approval_policy import ApprovalScope
from packages.core.governance.approval_policy_service import read_approval_settings
from packages.core.governance.policy import PolicyDecision
from packages.core.models.audit import AuditLog
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.staff import Department, Staff, StaffRole
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.worker import WorkLease, Worker, WorkerActivityLog
from packages.core.plans.step_reset import renew_step_approval_intent
from packages.core.services.runtime_authorization.domain import PermissionDecision
from tests.approval_settings_test_utils import action_rule, initialize_test_account_approval


@pytest.fixture
async def failure_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = MetaData()
    for model in (Entity, User, UserMembership, Department, StaffRole, Staff, AuditLog, HitlRequest, WorkLease, Worker, WorkerActivityLog, ExecutionPlan, ExecutionStep):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, (JSONB, ARRAY)):
                column.type = JSON()
            elif column.primary_key and isinstance(column.type, BigInteger):
                column.type = Integer()
        for index in table.indexes:
            where = index.dialect_options["postgresql"].get("where")
            if where is not None:
                index.dialect_options["sqlite"]["where"] = where
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_account_fixture_uses_real_owner_permissions_and_cas(failure_db):
    actor = await initialize_test_account_approval(failure_db, entity_id="e",
        user_rules=[action_rule("code.run")])
    snapshot = await read_approval_settings(failure_db,
        ApprovalScope(kind="account", id="e", entity_id="e"), actor)
    assert snapshot["policy"]["mode"] == "risk_based"
    assert snapshot["policy"]["rules"][0]["selector"]["action_key"] == "code.run"
    assert snapshot["revision"] == 2
    assert len((await failure_db.execute(select(AuditLog))).scalars().all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["consumed", "expired"])
async def test_failed_review_identity_is_stable_scoped_and_never_restores_receipt(failure_db, status):
    step = SimpleNamespace(id="s", plan_id="p", entity_id="e", workspace_id="w",
                           action_key="message.send", approval_operation_revision=0)
    request = HitlRequest(id="r", entity_id="e", workspace_id="w", origin_kind="step",
        origin_step_id="s", origin_plan_id="p", action_key="message.send", status=status,
        dedup_key="operation:step:p:s", context={"operation_id": "step:p:s"})
    lease = WorkLease(id="l", step_id="s", plan_id="p", entity_id="e", worker_id="worker",
        status="failed", lease_until=datetime.now(timezone.utc))
    failure_db.add_all([request, lease])
    await failure_db.flush()
    assert await _failed_step_review_operation_id(failure_db, step) == "step:p:s:failure:l"
    assert await _failed_step_review_operation_id(failure_db, step) == "step:p:s:failure:l"
    assert request.status == status
    for field in ("entity_id", "workspace_id", "plan_id", "id"):
        other = SimpleNamespace(**vars(step))
        setattr(other, field, "other")
        assert await _failed_step_review_operation_id(failure_db, other) is None
    renew_step_approval_intent(step)
    assert await _failed_step_review_operation_id(failure_db, step) is None
    assert request.status == status


@pytest.mark.asyncio
@pytest.mark.parametrize("hitl_type,explicit,expected", [
    ("error", False, ApprovalOutcome.ALLOW),
    ("error", True, ApprovalOutcome.NEEDS_HUMAN),
    ("review", False, ApprovalOutcome.NEEDS_HUMAN),
])
async def test_error_rendering_does_not_force_safe_retry_but_explicit_review_does(
    monkeypatch, hitl_type, explicit, expected,
):
    monkeypatch.setattr(approvals, "_policy_decision", AsyncMock(return_value=PolicyDecision(True)))
    monkeypatch.setattr(approvals, "_find_operation_request", AsyncMock(return_value=None))
    mint = AsyncMock(return_value=SimpleNamespace(id="review", reason="review needed", matched_rule="step.requires_approval"))
    monkeypatch.setattr(approvals, "_create_pending_request", mint)
    result = await approvals.resolve_approval(None,
        subject=approvals.ApprovalSubject(entity_id="e", workspace_id="w", action_key="code.run"),
        origin=approvals.ApprovalOrigin(kind="step", step_id="s", operation_id="step:p:s:failure:l"),
        permission_decision=PermissionDecision.allow(), hitl_type=hitl_type,
        require_explicit_approval=explicit,
        payload={"what_happened": "failed", "why": "offline", "action_to_take": "Retry"})
    assert result.outcome == expected
    assert mint.await_count == (expected == ApprovalOutcome.NEEDS_HUMAN)


@pytest.mark.asyncio
async def test_granted_error_decision_cannot_execute_a_spent_operation(monkeypatch):
    request = SimpleNamespace(status="granted", hitl_type="error", action_key="code.run",
                              resource_id=None, context={})
    monkeypatch.setattr(approvals, "_policy_decision", AsyncMock(return_value=PolicyDecision(True)))
    monkeypatch.setattr(approvals, "_find_operation_request", AsyncMock(return_value=request))
    result = await approvals.resolve_approval(None,
        subject=approvals.ApprovalSubject(entity_id="e", workspace_id="w", action_key="code.run"),
        origin=approvals.ApprovalOrigin(kind="step", step_id="s", operation_id="step:p:s:failure:l"),
        permission_decision=PermissionDecision.allow(), hitl_type="error", require_explicit_approval=True)
    assert result.outcome == ApprovalOutcome.DENY
    assert result.matched_rule == "approval.operation_terminal"
    assert request.status == "granted"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["step:s", "operation:step:p:s"])
async def test_transient_failure_keeps_legacy_and_current_consumed_grants_terminal(failure_db, monkeypatch, key):
    import packages.core.dispatcher.service as dispatcher_module

    # Only outward notifications and broker delivery are suppressed. Lease
    # fencing, retry accounting and all persisted approval rows remain real.
    monkeypatch.setattr(dispatcher_module, "_safe_chat_step_failed", AsyncMock())
    monkeypatch.setattr(dispatcher_module, "_safe_task_log", AsyncMock())
    monkeypatch.setattr("packages.core.plans.wakeup.stage_plan_cycle_wake", AsyncMock())
    step = ExecutionStep(id="s", plan_id="p", entity_id="e", step_key="work", kind="llm",
        params={}, step_status="running", attempt_count=1, max_attempts=3, current_lease_id="l")
    lease = WorkLease(id="l", step_id="s", plan_id="p", entity_id="e", worker_id="worker",
        status="active", lease_until=datetime.now(timezone.utc))
    request = HitlRequest(id="r", entity_id="e", origin_kind="step", origin_step_id="s",
        origin_plan_id="p", dedup_key=key, status="consumed", consumed_at=datetime.now(timezone.utc),
        decided_by_user_id="operator", resolved_reason="approved")
    failure_db.add_all([step, lease, request, ExecutionPlan(id="p", entity_id="e", status="running",
        plan_dag={}, dispatcher_state={"_response_locale": "en"})])
    await failure_db.flush()
    await Dispatcher().fail_lease(failure_db, "l", error={
        "type": "LLMRateLimited", "message": "LLM rate limited; retry after 10s"})
    await failure_db.refresh(request)
    assert lease.status == "failed" and step.step_status == "pending"
    assert step.attempt_count == 0 and step.error["transient_retry"]["count"] == 1
    assert step.approval_operation_revision == 0
    assert request.status == "consumed" and request.consumed_at is not None
    assert request.decided_by_user_id == "operator" and request.resolved_reason == "approved"
