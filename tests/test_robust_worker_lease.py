"""``_execute_claimed_lease`` robustness (Section W1/W2/W3/W4).

The dispatcher is faked here on purpose: these tests pin what the worker
REPORTS (checkpoint, error shape, outcome) and how it behaves when its lease
is pulled out from under it, independently of the dispatcher's own
implementation of those keyword arguments. Raw tool output is diagnostic and
must not be promoted to effect evidence; the real-dispatcher suite separately
pins the authoritative pre-I/O intent path.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from packages.core.constants.execution import WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA
from packages.core.contracts.task_output import SubmittedStepFailure
from packages.core.database import async_session
from packages.core.dispatcher import LeaseNotActive
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import WorkLease, Worker
from packages.core.workers import internal


# ── fixtures / helpers ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_heartbeat_engine():
    internal._reset_lease_heartbeat_engine()
    yield
    internal._reset_lease_heartbeat_engine()


class _FakeDispatcher:
    """Records every lease transition the worker reports."""

    def __init__(self, *, raise_on: str | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        self.raise_on = raise_on

    def _record(self, method: str, lease_id: str, kwargs: dict):
        self.calls.append((method, lease_id, kwargs))
        if self.raise_on == method:
            raise LeaseNotActive(f"lease {lease_id} is 'cancelled', not active")

    async def fail_lease(self, db, lease_id, **kwargs):
        self._record("fail_lease", lease_id, kwargs)
        lease = await db.get(WorkLease, lease_id)
        assert lease is not None
        return SimpleNamespace(status="failed", step_id=lease.step_id)

    async def complete_lease(self, db, lease_id, **kwargs):
        self._record("complete_lease", lease_id, kwargs)
        return SimpleNamespace(status="completed")

    async def lease_needs_human(self, db, lease_id, **kwargs):
        self._record("lease_needs_human", lease_id, kwargs)
        return SimpleNamespace(status="needs_human")

    def only(self, method: str) -> dict:
        assert [c[0] for c in self.calls] == [method], self.calls
        return self.calls[0][2]


@pytest.fixture
def dispatcher(monkeypatch) -> _FakeDispatcher:
    fake = _FakeDispatcher()
    monkeypatch.setattr(internal, "Dispatcher", lambda: fake)
    return fake


async def _seed_lease(db, *, params: dict | None = None, lease_seconds: float = 300) -> dict:
    entity_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
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
            params=params or {"prompt": "draft"}, depends_on=[], step_status="running",
            risk_level="low", attempt_count=1, max_attempts=3, current_lease_id=lease_id,
            started_at=now,
        ),
        WorkLease(
            id=lease_id, step_id=step_id, plan_id=plan_id, entity_id=entity_id,
            worker_id=worker_id, status="active", lease_until=now + timedelta(seconds=lease_seconds),
        ),
    ])
    await db.commit()
    return {"entity_id": entity_id, "plan_id": plan_id, "step_id": step_id, "lease_id": lease_id}


_TOOL_MESSAGES = [
    {"role": "assistant", "tool_calls": [{"id": "t1", "function": {"name": "mcp__twitter_x__create_tweet"}}]},
    {"role": "tool", "tool_call_id": "t1", "content": '{"tweet_id": "42", "tweet_url": "https://x.com/s/42"}'},
]
_CHECKPOINT = {
    "schema_version": WORKER_TRANSCRIPT_CHECKPOINT_SCHEMA,
    "messages": [{"role": "user", "content": "draft"}],
    "rounds": 1, "usage": {}, "tool_calls_made": [], "saved_at": "2026-09-19T00:00:00+00:00",
}


def _body(exc: BaseException | None = None, *, result: dict | None = None, capture: bool = False):
    async def body(snapshot):
        if capture:
            snapshot.setdefault("_captured_tool_messages", []).extend(_TOOL_MESSAGES)
        if exc is not None:
            raise exc
        return result if result is not None else {"result": {"text": "ok"}}
    return body


# ── W1 + W2: what a failure reports ───────────────────────────────────


@pytest.mark.asyncio
async def test_generic_failure_reports_captured_evidence_and_the_transcript(
    db_session,
    dispatcher,
    monkeypatch,
):
    seeded = await _seed_lease(db_session)
    exc = RuntimeError("tool blew up")
    exc.retry_checkpoint = _CHECKPOINT
    monkeypatch.setattr(internal, "_execute_by_kind", _body(exc, capture=True))

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "failed"
    kwargs = dispatcher.only("fail_lease")
    assert kwargs["error"] == {"type": "RuntimeError", "message": "tool blew up"}
    assert kwargs.get("evidence_refs") is None
    assert kwargs["runtime_checkpoint"] == _CHECKPOINT


@pytest.mark.asyncio
async def test_failure_without_evidence_or_transcript_reports_neither(db_session, dispatcher, monkeypatch):
    seeded = await _seed_lease(db_session)
    monkeypatch.setattr(internal, "_execute_by_kind", _body(RuntimeError("plain")))

    await internal.execute_lease_inproc(seeded["lease_id"])

    kwargs = dispatcher.only("fail_lease")
    assert kwargs.get("evidence_refs") is None
    assert kwargs.get("runtime_checkpoint") is None


@pytest.mark.asyncio
async def test_deadline_failure_reports_captured_evidence(db_session, dispatcher, monkeypatch):
    seeded = await _seed_lease(db_session, params={"prompt": "draft", "max_runtime_seconds": 0.1})

    async def slow(snapshot):
        snapshot.setdefault("_captured_tool_messages", []).extend(_TOOL_MESSAGES)
        await asyncio.sleep(30)

    monkeypatch.setattr(internal, "_execute_by_kind", slow)

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["error"]["type"] == "StepDeadlineExceeded"
    kwargs = dispatcher.only("fail_lease")
    assert kwargs["error"]["type"] == "StepDeadlineExceeded"
    assert kwargs.get("evidence_refs") is None


@pytest.mark.asyncio
async def test_submitted_failure_reports_its_transcript_and_keeps_retry_semantics(db_session, dispatcher, monkeypatch):
    seeded = await _seed_lease(db_session)
    exc = SubmittedStepFailure(
        summary="blocked",
        failure={"reason": "no creds", "retryable": True, "requires_human": False, "blockers": []},
    )
    exc.retry_checkpoint = _CHECKPOINT
    monkeypatch.setattr(internal, "_execute_by_kind", _body(exc, capture=True))

    await internal.execute_lease_inproc(seeded["lease_id"])

    kwargs = dispatcher.only("fail_lease")
    assert kwargs["error"]["type"] == "StepResultFailed"
    assert kwargs["will_retry"] is None
    assert kwargs["runtime_checkpoint"] == _CHECKPOINT
    assert kwargs.get("evidence_refs") is None


@pytest.mark.asyncio
async def test_rate_limit_and_credit_paths_report_captured_evidence(
    db_session,
    dispatcher,
    monkeypatch,
):
    from packages.core.ai.llm_client import CreditExhaustedError, LLMRateLimited

    seeded = await _seed_lease(db_session)
    monkeypatch.setattr(internal, "_execute_by_kind", _body(LLMRateLimited(7, "slow down"), capture=True))
    outcome = await internal.execute_lease_inproc(seeded["lease_id"])
    assert outcome["outcome"] == "retry"
    assert dispatcher.only("fail_lease").get("evidence_refs") is None

    dispatcher.calls.clear()
    seeded = await _seed_lease(db_session)
    monkeypatch.setattr(internal, "_execute_by_kind", _body(CreditExhaustedError("broke"), capture=True))
    outcome = await internal.execute_lease_inproc(seeded["lease_id"])
    assert outcome["outcome"] == "failed"
    kwargs = dispatcher.only("fail_lease")
    assert kwargs["will_retry"] is False
    assert kwargs.get("evidence_refs") is None


@pytest.mark.asyncio
async def test_fresh_mcp_approval_path_reports_captured_evidence(db_session, dispatcher, monkeypatch):
    seeded = await _seed_lease(db_session)
    exc = internal._FreshMCPApprovalRequired(provider="stripe", action_key="refund", effect="write")
    monkeypatch.setattr(internal, "_execute_by_kind", _body(exc, capture=True))

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "approval_required"
    kwargs = dispatcher.only("fail_lease")
    assert kwargs["will_retry"] is True
    assert kwargs.get("evidence_refs") is None


# ── W3: contract failure ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_contract_failure_maps_to_output_schema_error(db_session, dispatcher, monkeypatch):
    seeded = await _seed_lease(db_session)
    errors = [{"path": "$['leads']", "message": "[] is too short"}]
    exc = internal.StepContractFailure(errors)
    exc.retry_checkpoint = _CHECKPOINT
    monkeypatch.setattr(internal, "_execute_by_kind", _body(exc, capture=True))

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "failed"
    assert outcome["error"]["type"] == "OutputSchemaError"
    kwargs = dispatcher.only("fail_lease")
    assert kwargs["error"]["type"] == "OutputSchemaError"
    assert kwargs["error"]["errors"] == errors
    assert "[] is too short" in kwargs["error"]["message"]
    assert kwargs.get("will_retry") is None
    assert kwargs["runtime_checkpoint"] == _CHECKPOINT
    assert kwargs.get("evidence_refs") is None


# ── W2: human pauses and the success path ─────────────────────────────


@pytest.mark.asyncio
async def test_pending_action_pause_parks_the_transcript(db_session, dispatcher, monkeypatch):
    seeded = await _seed_lease(db_session)
    exc = internal._NeedsHumanInput(prompt="approve?", pending_action={"kind": "needs_confirmation", "prompt": "approve?"})
    exc.retry_checkpoint = _CHECKPOINT
    monkeypatch.setattr(internal, "_execute_by_kind", _body(exc))

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "needs_human"
    kwargs = dispatcher.only("lease_needs_human")
    assert kwargs["runtime_checkpoint"] == _CHECKPOINT
    assert kwargs["pending_action"]["kind"] == "needs_confirmation"


@pytest.mark.asyncio
async def test_request_input_checkpoint_wins_over_the_transcript(db_session, dispatcher, monkeypatch):
    seeded = await _seed_lease(db_session)
    request_input = {"schema_version": 4, "messages": [], "pending_tool_call": {"id": "c", "name": "request_input"}}
    exc = internal._NeedsHumanInput(prompt="which?", runtime_checkpoint=request_input)
    exc.retry_checkpoint = _CHECKPOINT
    monkeypatch.setattr(internal, "_execute_by_kind", _body(exc))

    await internal.execute_lease_inproc(seeded["lease_id"])

    assert dispatcher.only("lease_needs_human")["runtime_checkpoint"] == request_input


@pytest.mark.asyncio
async def test_plain_human_pause_without_any_checkpoint_keeps_the_plain_contract(db_session, dispatcher, monkeypatch):
    seeded = await _seed_lease(db_session)
    monkeypatch.setattr(internal, "_execute_by_kind", _body(internal._NeedsHumanInput(prompt="sign in")))

    await internal.execute_lease_inproc(seeded["lease_id"])

    assert dispatcher.only("lease_needs_human").get("runtime_checkpoint") is None


@pytest.mark.asyncio
async def test_success_passes_the_transcript_to_complete_lease(db_session, dispatcher, monkeypatch):
    seeded = await _seed_lease(db_session)
    monkeypatch.setattr(internal, "_execute_by_kind", _body(result={
        "result": {"text": "done"}, "cost": {"usd": 0.1}, "retry_checkpoint": _CHECKPOINT,
    }))

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome["outcome"] == "completed"
    kwargs = dispatcher.only("complete_lease")
    assert kwargs["result"] == {"text": "done"}
    assert kwargs["runtime_checkpoint"] == _CHECKPOINT


# ── W4: a pulled lease never reaches the Celery retry ─────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["fail_lease", "complete_lease", "lease_needs_human"])
async def test_lease_not_active_from_any_report_is_absorbed(db_session, monkeypatch, method):
    fake = _FakeDispatcher(raise_on=method)
    monkeypatch.setattr(internal, "Dispatcher", lambda: fake)
    seeded = await _seed_lease(db_session)
    body = {
        "fail_lease": _body(RuntimeError("boom")),
        "complete_lease": _body(result={"result": {"text": "ok"}}),
        "lease_needs_human": _body(internal._NeedsHumanInput(prompt="?")),
    }[method]
    monkeypatch.setattr(internal, "_execute_by_kind", body)

    outcome = await internal.execute_lease_inproc(seeded["lease_id"])

    assert outcome == {"lease_id": seeded["lease_id"], "outcome": "lease_not_active"}
    assert [c[0] for c in fake.calls] == [method]


@pytest.mark.asyncio
async def test_lease_revoked_mid_step_cancels_the_body_without_reporting(db_session, dispatcher, monkeypatch):
    seeded = await _seed_lease(db_session, params={"prompt": "draft", "max_runtime_seconds": 30})
    monkeypatch.setattr(internal, "LEASE_HEARTBEAT_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(internal, "LEASE_HEARTBEAT_EXTEND_SECONDS", 5)
    cancelled: list[bool] = []

    async def revoked_body(snapshot):
        # The operator cancels the plan while the step is running: the
        # dispatcher flips the lease, and the heartbeat is what notices.
        try:
            async with async_session() as db:
                lease = await db.get(WorkLease, snapshot["lease_id"])
                lease.status = "cancelled"
                await db.commit()
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return {"result": {"text": "never"}}

    monkeypatch.setattr(internal, "_execute_by_kind", revoked_body)

    outcome = await asyncio.wait_for(internal.execute_lease_inproc(seeded["lease_id"]), timeout=10)

    assert outcome == {"lease_id": seeded["lease_id"], "outcome": "lease_revoked"}
    assert cancelled == [True]
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_an_outer_cancellation_is_not_mistaken_for_a_revoked_lease(db_session, dispatcher, monkeypatch):
    seeded = await _seed_lease(db_session, params={"prompt": "draft", "max_runtime_seconds": 30})
    monkeypatch.setattr(internal, "LEASE_HEARTBEAT_INTERVAL_SECONDS", 0.1)
    started = asyncio.Event()

    async def slow(snapshot):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(internal, "_execute_by_kind", slow)
    task = asyncio.create_task(internal._execute_claimed_lease(seeded["lease_id"]))
    await started.wait()
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert dispatcher.calls == []


# ── W4: heartbeat unit semantics ──────────────────────────────────────


def test_heartbeat_reports_an_inactive_lease_and_fires_on_inactive_once(monkeypatch):
    extends: list[str] = []

    def fake_extend(lease_id, *, extra_seconds, execution_claim_id=None):
        extends.append(lease_id)
        return False

    monkeypatch.setattr(internal, "extend_active_lease_sync", fake_extend)
    fired: list[int] = []
    heartbeat = internal.LeaseHeartbeat(
        "lease-1", interval_seconds=0.02, extend_seconds=5,
        on_inactive=lambda: fired.append(1),
    )
    assert heartbeat.lease_inactive is False
    heartbeat.start()
    heartbeat.thread.join(timeout=2)

    assert heartbeat.thread.is_alive() is False
    assert heartbeat.lease_inactive is True
    assert fired == [1]
    assert extends == ["lease-1"]


def test_heartbeat_survives_an_on_inactive_callback_that_raises(monkeypatch):
    monkeypatch.setattr(
        internal,
        "extend_active_lease_sync",
        lambda lease_id, *, extra_seconds, execution_claim_id=None: False,
    )

    def explode():
        raise RuntimeError("loop is gone")

    heartbeat = internal.LeaseHeartbeat("lease-2", interval_seconds=0.02, extend_seconds=5, on_inactive=explode)
    heartbeat.start()
    heartbeat.thread.join(timeout=2)

    assert heartbeat.thread.is_alive() is False
    assert heartbeat.lease_inactive is True


def test_start_lease_heartbeat_forwards_on_inactive(monkeypatch):
    monkeypatch.setattr(internal, "LEASE_HEARTBEAT_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(internal, "extend_active_lease_sync", lambda lease_id, *, extra_seconds: True)
    callback = lambda: None  # noqa: E731
    heartbeat = internal._start_lease_heartbeat("lease-3", on_inactive=callback)
    try:
        assert heartbeat is not None
        assert heartbeat._on_inactive is callback
    finally:
        heartbeat.stop(join_timeout=1)
