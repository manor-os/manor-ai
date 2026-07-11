"""Free-form agent steps (llm/subagent) get Planner-*guessed* output schemas
that often don't match the real, open-ended output (e.g. a "research and draft
posts" step annotated as {text:string} that returns {posts:[...]}). Such a
mismatch must NOT dead-fail the step (3 retries → stranded plan); it is
advisory — accept the real output. Structured kinds keep hard validation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from packages.core.dispatcher.service import Dispatcher
from packages.core.dispatcher.validation import output_schema_is_advisory
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import WorkLease, Worker


def test_output_schema_advisory_only_for_freeform_kinds():
    assert output_schema_is_advisory("llm") is True
    assert output_schema_is_advisory("subagent") is True
    assert output_schema_is_advisory("action") is False
    assert output_schema_is_advisory("code") is False
    assert output_schema_is_advisory("human") is False
    assert output_schema_is_advisory(None) is False


# Planner guessed {text:string}; the agent actually produced {posts:[...]} —
# the exact mismatch seen in production for `research_and_draft`.
_TEXT_SCHEMA = {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}}
_POSTS_RESULT = {"posts": [{"label": "Post 1", "draft": "hello", "char_count": 5}]}


async def _setup_step(db, *, kind: str, attempt_count: int = 1):
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
            step_key="research_and_draft",
            kind=kind,
            params={"prompt": "research and draft posts"},
            depends_on=[],
            step_status="running",
            attempt_count=attempt_count,
            max_attempts=3,
            expected_output_schema=_TEXT_SCHEMA,
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
async def test_subagent_output_schema_mismatch_is_accepted(client) -> None:
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        step_id, lease_id = await _setup_step(db, kind="subagent")
        await Dispatcher().complete_lease(db, lease_id, result=_POSTS_RESULT)
        await db.flush()

        step = await db.get(ExecutionStep, step_id)
        lease = await db.get(WorkLease, lease_id)
        # advisory: step completes with the real output, not OutputSchemaError
        assert step.step_status == "done"
        assert step.error is None
        assert step.result == _POSTS_RESULT
        assert lease.status == "completed"


@pytest.mark.asyncio
async def test_structured_kind_output_schema_mismatch_still_fails(client) -> None:
    import packages.core.database as dbmod

    async with dbmod.async_session() as db:
        # final attempt so the hard failure is terminal, not a retry
        step_id, lease_id = await _setup_step(db, kind="code", attempt_count=3)
        await Dispatcher().complete_lease(db, lease_id, result=_POSTS_RESULT)
        await db.flush()

        step = await db.get(ExecutionStep, step_id)
        lease = await db.get(WorkLease, lease_id)
        # structured kind keeps the hard contract → not accepted
        assert step.step_status != "done"
        assert step.error and step.error.get("type") == "OutputSchemaError"
        assert lease.status == "failed"
