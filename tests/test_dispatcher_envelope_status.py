"""A StepResult envelope's ``status`` is the success/failure control signal.

PR #296 made the envelope the fixed output contract for llm/subagent steps and
designed it so an *inferred* status is never ``succeeded`` — but nothing ever
read it back. Staging showed the consequence: a step rendered ``Done`` while its
stored result was ``{"text": "", "status": "failed", ...}``, the dependent step
ran on that empty input, and the retry budget was never touched (attempts 1/3).

``complete_lease`` must therefore route a failed envelope to ``fail_lease`` —
and *only* for steps whose declared schema IS the envelope, so a custom/action
schema that happens to carry a ``status`` field is untouched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from packages.core.contracts.envelope import step_result_envelope_schema
from packages.core.dispatcher.service import Dispatcher
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import WorkLease, Worker

# The staging payload, verbatim, after coercion by build_step_result_envelope.
_FAILED_ENVELOPE = {"text": "", "status": "failed", "summary": "no structured summary provided"}
_PARTIAL_ENVELOPE = {"status": "partial", "summary": "half a report", "outputs": {"text": "half"}}
_SUCCEEDED_ENVELOPE = {"status": "succeeded", "summary": "wrote the report", "outputs": {"text": "body"}}

# A plain action schema that legitimately carries a `status` payload field —
# e.g. a publish receipt whose provider reported status="failed" as DATA.
_ACTION_SCHEMA = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}, "detail": {"type": "string"}},
}


async def _setup_step(
    db,
    *,
    kind: str = "llm",
    schema: dict | None = None,
    attempt_count: int = 1,
    max_attempts: int = 3,
):
    entity_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
    db.add(
        Worker(
            id=worker_id,
            entity_id=entity_id,
            kind="internal",
            display_name="Internal worker",
            capabilities={"supported_kinds": [kind], "max_risk_level": "high"},
            monthly_spent_usd=Decimal("0"),
            auto_pause_on_budget=True,
            status="active",
        )
    )
    db.add(
        ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            status="running",
            execution_mode="live",
            approval_required=False,
            plan_dag={"steps": []},
        )
    )
    db.add(
        ExecutionStep(
            id=step_id,
            plan_id=plan_id,
            entity_id=entity_id,
            step_key="draft_report",
            kind=kind,
            params={"prompt": "draft the report"},
            depends_on=[],
            step_status="running",
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            expected_output_schema=schema if schema is not None else step_result_envelope_schema(),
            current_lease_id=lease_id,
        )
    )
    db.add(
        WorkLease(
            id=lease_id,
            step_id=step_id,
            plan_id=plan_id,
            entity_id=entity_id,
            worker_id=worker_id,
            lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
            status="active",
        )
    )
    await db.flush()
    return step_id, lease_id


@pytest.mark.asyncio
async def test_failed_envelope_does_not_mark_step_done(client) -> None:
    """The staging bug: an empty result stored as `done` with retries untouched."""
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        step_id, lease_id = await _setup_step(db, attempt_count=1)
        await Dispatcher().complete_lease(db, lease_id, result=_FAILED_ENVELOPE)
        await db.flush()

        step = await db.get(ExecutionStep, step_id)
        lease = await db.get(WorkLease, lease_id)
        assert step.step_status != "done"
        # attempts remain → back in the queue for the normal retry policy
        assert step.step_status == "pending"
        assert step.error and step.error.get("type") == "StepResultFailed"
        assert "no structured summary provided" in (step.error.get("message") or "")
        assert lease.status == "failed"
        # the envelope survives for the operator to inspect
        assert lease.result == _FAILED_ENVELOPE


@pytest.mark.asyncio
async def test_failed_envelope_on_last_attempt_fails_the_step(client) -> None:
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        step_id, lease_id = await _setup_step(db, attempt_count=3, max_attempts=3)
        await Dispatcher().complete_lease(db, lease_id, result=_FAILED_ENVELOPE)
        await db.flush()

        step = await db.get(ExecutionStep, step_id)
        assert step.step_status in ("failed", "waiting_human")
        assert step.step_status != "done"
        assert step.error.get("type") == "StepResultFailed"


@pytest.mark.asyncio
async def test_failed_envelope_carries_the_failure_block(client) -> None:
    import packages.core.database as dbmod

    envelope = {
        "status": "failed",
        "summary": "could not reach the source",
        "failure": {"reason": "upstream 503", "retryable": True},
    }
    async with dbmod.async_session() as db:
        step_id, lease_id = await _setup_step(db)
        await Dispatcher().complete_lease(db, lease_id, result=envelope)
        await db.flush()

        step = await db.get(ExecutionStep, step_id)
        assert step.error.get("message") == "could not reach the source"
        assert step.error.get("failure") == {"reason": "upstream 503", "retryable": True}


@pytest.mark.asyncio
async def test_partial_envelope_is_a_success(client) -> None:
    """Partial output is usable output — the step stays done."""
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        step_id, lease_id = await _setup_step(db)
        await Dispatcher().complete_lease(db, lease_id, result=_PARTIAL_ENVELOPE)
        await db.flush()

        step = await db.get(ExecutionStep, step_id)
        assert step.step_status == "done"
        assert step.error is None
        assert step.result == _PARTIAL_ENVELOPE


@pytest.mark.asyncio
async def test_succeeded_envelope_is_a_success(client) -> None:
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        step_id, lease_id = await _setup_step(db)
        await Dispatcher().complete_lease(db, lease_id, result=_SUCCEEDED_ENVELOPE)
        await db.flush()

        step = await db.get(ExecutionStep, step_id)
        assert step.step_status == "done"
        assert step.result == _SUCCEEDED_ENVELOPE


@pytest.mark.asyncio
async def test_non_envelope_step_with_status_failed_payload_still_completes(client) -> None:
    """The false-positive guard: the gate is the SCHEMA, not a `status` key.

    An action step's schema can declare `status` as ordinary payload data. Such
    a result must not be hijacked into a step failure.
    """
    import packages.core.database as dbmod

    payload = {"status": "failed", "detail": "provider marked the job failed"}
    async with dbmod.async_session() as db:
        step_id, lease_id = await _setup_step(db, kind="action", schema=_ACTION_SCHEMA)
        await Dispatcher().complete_lease(db, lease_id, result=payload)
        await db.flush()

        step = await db.get(ExecutionStep, step_id)
        lease = await db.get(WorkLease, lease_id)
        assert step.step_status == "done"
        assert step.error is None
        assert step.result == payload
        assert lease.status == "completed"


@pytest.mark.asyncio
async def test_empty_envelope_step_does_not_release_its_dependent(client) -> None:
    """End-to-end: step 1 returns an empty envelope, step 2 must not run on it.

    This is the staging failure in miniature — the dependent step used to be
    dispatched with the empty output of a step that had produced nothing.
    """
    import packages.core.database as dbmod
    from sqlalchemy import select

    entity_id = generate_ulid()
    plan_id = generate_ulid()
    worker_id = generate_ulid()
    step1_id, step2_id = generate_ulid(), generate_ulid()

    async with dbmod.async_session() as db:
        db.add(
            Worker(
                id=worker_id,
                entity_id=entity_id,
                kind="internal",
                display_name="Internal worker",
                capabilities={"supported_kinds": ["llm"], "max_risk_level": "high"},
                monthly_spent_usd=Decimal("0"),
                auto_pause_on_budget=True,
                status="active",
            )
        )
        db.add(
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                status="running",
                execution_mode="live",
                approval_required=False,
                plan_dag={"steps": []},
            )
        )
        for step_id, key, deps in ((step1_id, "research", []), (step2_id, "write_up", ["research"])):
            db.add(
                ExecutionStep(
                    id=step_id,
                    plan_id=plan_id,
                    entity_id=entity_id,
                    step_key=key,
                    kind="llm",
                    params={"prompt": key},
                    depends_on=deps,
                    step_status="pending",
                    attempt_count=0,
                    max_attempts=3,
                    expected_output_schema=step_result_envelope_schema(),
                )
            )
        await db.commit()

    dispatcher = Dispatcher()
    async with dbmod.async_session() as db:
        worker = await db.get(Worker, worker_id)
        leased = await dispatcher.checkout_steps_for_worker(db, worker, max_n=5, plan_id=plan_id)
        assert [s.step_key for _, s in leased] == ["research"]
        lease_id = leased[0][0].id
        await db.commit()

    async with dbmod.async_session() as db:
        await dispatcher.complete_lease(db, lease_id, result=_FAILED_ENVELOPE)
        await db.commit()

    async with dbmod.async_session() as db:
        step1 = await db.get(ExecutionStep, step1_id)
        step2 = await db.get(ExecutionStep, step2_id)
        assert step1.step_status != "done"
        assert step1.error and step1.error.get("type") == "StepResultFailed"
        # the failing attempt was actually charged to the retry budget
        assert step1.attempt_count == 1
        assert step2.step_status == "pending"

        worker = await db.get(Worker, worker_id)
        leased = await dispatcher.checkout_steps_for_worker(db, worker, max_n=5, plan_id=plan_id)
        assert "write_up" not in [s.step_key for _, s in leased]
        await db.rollback()

    async with dbmod.async_session() as db:
        step2_leases = (
            await db.execute(select(WorkLease).where(WorkLease.step_id == step2_id))
        ).scalars().all()
        assert step2_leases == []


@pytest.mark.asyncio
async def test_llm_step_without_envelope_schema_is_unaffected(client) -> None:
    """A surviving planner-authored custom schema is not an envelope contract."""
    import packages.core.database as dbmod

    schema = {"type": "object", "properties": {"status": {"type": "string"}}}
    payload = {"status": "failed", "text": "the model chose to report status=failed"}
    async with dbmod.async_session() as db:
        step_id, lease_id = await _setup_step(db, kind="llm", schema=schema)
        await Dispatcher().complete_lease(db, lease_id, result=payload)
        await db.flush()

        step = await db.get(ExecutionStep, step_id)
        assert step.step_status == "done"
