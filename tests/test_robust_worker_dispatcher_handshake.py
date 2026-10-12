"""Worker -> real Dispatcher handshake (Section W2/W1 meets Section D3).

The fake-dispatcher suite (``test_robust_worker_lease.py``) pins what the
worker REPORTS. This file pins that the real dispatcher HONOURS it: a parked
transcript survives a retry, a durable pre-I/O external-effect intent blocks
one, and a transcript handed to ``complete_lease`` reaches the step when the
dispatcher's own validation sends it back for another attempt.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from packages.core.ai.runtime.approvals import RuntimeApprovalAction
from packages.core.ai.runtime.tool_effect_classification import (
    RuntimeToolClassification,
)
from packages.core.constants.execution import WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA
from packages.core.contracts.task_output import plan_output_contract_schema
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.worker import WorkLease, Worker
from packages.core.plans.wakeup import (
    PLAN_CYCLE_OUTBOX_EVENT,
    plan_cycle_outbox_dedupe_key,
)
from packages.core.workers import internal


@pytest.fixture(autouse=True)
def _fresh_heartbeat_engine():
    internal._reset_lease_heartbeat_engine()
    yield
    internal._reset_lease_heartbeat_engine()


_CHECKPOINT = {
    "schema_version": WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA,
    "messages": [{"role": "user", "content": "draft"}, {"role": "assistant", "content": "half done"}],
    "rounds": 1, "usage": {}, "tool_calls_made": [], "saved_at": "2026-09-19T00:00:00+00:00",
}
_PUBLISH_MESSAGES = [
    {"role": "assistant", "tool_calls": [{"id": "t1", "function": {"name": "mcp__twitter_x__create_tweet"}}]},
    {"role": "tool", "tool_call_id": "t1", "content": '{"tweet_id": "42", "tweet_url": "https://x.com/s/42"}'},
]


async def _seed_lease(db, *, schema: dict | None = None) -> dict:
    entity_id, plan_id, step_id, lease_id, worker_id = (generate_ulid() for _ in range(5))
    now = datetime.now(timezone.utc)
    db.add_all([
        Worker(
            id=worker_id, entity_id=entity_id, kind="internal", display_name="Internal worker",
            capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
            monthly_spent_usd=Decimal("0"), auto_pause_on_budget=True, status="active",
        ),
        ExecutionPlan(
            id=plan_id, entity_id=entity_id, status="running", execution_mode="live",
            approval_required=False, plan_dag={"steps": []},
        ),
        ExecutionStep(
            id=step_id, plan_id=plan_id, entity_id=entity_id, step_key="draft", kind="subagent",
            params={"prompt": "draft"}, expected_output_schema=schema, depends_on=[],
            step_status="running", risk_level="low", attempt_count=1, max_attempts=3,
            current_lease_id=lease_id, started_at=now,
        ),
        WorkLease(
            id=lease_id, step_id=step_id, plan_id=plan_id, entity_id=entity_id,
            worker_id=worker_id, status="active", lease_until=now + timedelta(seconds=300),
        ),
    ])
    await db.commit()
    return {"plan_id": plan_id, "step_id": step_id, "lease_id": lease_id}


async def _step(db, seeded) -> ExecutionStep:
    db.expire_all()
    return await db.get(ExecutionStep, seeded["step_id"])


async def _wake(db, seeded) -> RuntimeOutboxEvent:
    return (await db.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == PLAN_CYCLE_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == seeded["plan_id"],
            RuntimeOutboxEvent.dedupe_key
            == plan_cycle_outbox_dedupe_key(seeded["plan_id"]),
            RuntimeOutboxEvent.delivered_at.is_(None),
        )
    )).scalar_one()


@pytest.mark.asyncio
async def test_transcript_from_a_failed_attempt_is_parked_for_the_retry(db_session, monkeypatch):
    seeded = await _seed_lease(db_session)
    exc = RuntimeError("tool blew up")
    exc.retry_checkpoint = _CHECKPOINT

    async def body(snapshot):
        raise exc

    monkeypatch.setattr(internal, "_execute_by_kind", body)

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "failed"
    step = await _step(db_session, seeded)
    assert step.step_status == "pending", "attempts remain, so the step retries"
    assert step.runtime_checkpoint == _CHECKPOINT
    wake = await _wake(db_session, seeded)
    assert wake.payload["reason"] == "lease_failed"
    assert wake.payload["wake_generation"]
    # ...and the next attempt's snapshot would continue it, not restart.
    assert internal._resume_transcript_for_retry({"runtime_checkpoint": step.runtime_checkpoint}) == _CHECKPOINT["messages"]


@pytest.mark.asyncio
async def test_captured_external_effect_blocks_the_retry(db_session, monkeypatch):
    seeded = await _seed_lease(db_session)

    async def body(snapshot):
        await internal._persist_worker_tool_effect_intent(
            snapshot,
            "mcp__twitter_x__create_tweet",
            {"text": "hello"},
            "t1",
            RuntimeToolClassification.action_call(RuntimeApprovalAction(
                kind="action",
                action_key="twitter_x.create_tweet",
                risk_level="high",
                title="Publish tweet",
                operation="publish",
            )),
        )
        # Raw tool output is diagnostic only.  Even if it claims success, the
        # retry gate is driven by the server-persisted pre-I/O intent above.
        snapshot.setdefault("_captured_tool_messages", []).extend(_PUBLISH_MESSAGES)
        raise RuntimeError("schema check failed after the tweet went out")

    monkeypatch.setattr(internal, "_execute_by_kind", body)

    await internal.execute_lease_inproc(seeded["lease_id"])

    step = await _step(db_session, seeded)
    assert len(step.evidence_refs) == 1
    intent = step.evidence_refs[0]
    assert intent["kind"] == "tool_effect"
    assert intent["effect"] == "pending"
    assert intent["fields"]["tool"] == "mcp__twitter_x__create_tweet"
    assert intent["fields"]["tool_call_id"] == "t1"
    assert intent["fields"]["effect_id"]
    assert "tweet_id" not in intent["fields"]
    assert step.step_status != "pending", "a published tweet must not be republished by a retry"
    assert step.error["failure"]["requires_human"] is True
    wake = await _wake(db_session, seeded)
    assert wake.payload["reason"] == "lease_failed"
    assert wake.payload["wake_generation"]


@pytest.mark.asyncio
async def test_transcript_handed_to_complete_lease_survives_the_dispatcher_rejection(db_session, monkeypatch):
    schema = plan_output_contract_schema(
        {"type": "object", "required": ["leads"], "additionalProperties": False,
         "properties": {"leads": {"type": "array", "minItems": 2}}},
        source="plan.expected_output_schema",
    )
    seeded = await _seed_lease(db_session, schema=schema)

    async def body(snapshot):
        return {"result": {"leads": []}, "cost": {"usd": 0}, "retry_checkpoint": _CHECKPOINT}

    monkeypatch.setattr(internal, "_execute_by_kind", body)

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "failed"
    step = await _step(db_session, seeded)
    assert step.error["type"] == "OutputSchemaError"
    assert step.step_status == "pending"
    assert step.runtime_checkpoint == _CHECKPOINT
    wake = await _wake(db_session, seeded)
    assert wake.payload["reason"] == "lease_completed"
    assert wake.payload["wake_generation"]
