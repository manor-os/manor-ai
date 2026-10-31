"""The durable run-row protocol, once per run kind (``DurableRunSpec``).

Every invariant here is a property of the protocol, not of one run kind:
each spec added to ``_CASES`` gets the whole suite. A repo-wide AST guard
keeps ``services/durable_run.py`` the only writer of a run row's claim and
wake fields.
"""
from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task_coordinator import (
    TaskCoordinatorEngine,
    TaskCoordinatorPhase,
    TaskCoordinatorRun,
)
from packages.core.services import durable_run
from packages.core.services.durable_run import (
    DURABLE_RUN_MAX_WAKE_DELAY,
    DurableRunSpec,
)
from packages.core.models.task_agent_child import TaskAgentChildRun
from packages.core.services.task_agent_child_wakeup import TASK_AGENT_CHILD_RUN_SPEC
from packages.core.services.task_coordinator_wakeup import TASK_COORDINATOR_RUN_SPEC


REPO_ROOT = Path(__file__).resolve().parents[1]
LEASE = 120.0


@dataclass(frozen=True)
class RunCase:
    spec: DurableRunSpec
    # Inserts one fresh, non-terminal run row and returns its key.
    make_run: Callable[[Any], Awaitable[str]]
    ready_phase: str
    parked_phase: str


async def _task_coordinator_run(db) -> str:
    run = TaskCoordinatorRun(
        id=generate_ulid(),
        task_id=generate_ulid(),
        entity_id=generate_ulid(),
        workspace_id=generate_ulid(),
        root_subscription_id=generate_ulid(),
        root_agent_id=generate_ulid(),
        selection_fingerprint="durable-run-protocol",
        lifecycle_generation="lifecycle-1",
        engine=TaskCoordinatorEngine.AGENT_LOOP.value,
        phase=TaskCoordinatorPhase.READY.value,
        state_version=0,
    )
    db.add(run)
    await db.flush()
    return run.task_id


async def _task_agent_child_run(db) -> str:
    root_run_id = generate_ulid()
    child = TaskAgentChildRun(
        id=generate_ulid(),
        task_id=generate_ulid(),
        entity_id=generate_ulid(),
        workspace_id=generate_ulid(),
        root_run_id=root_run_id,
        parent_ref=root_run_id,
        parent_tool_call_id="call-1",
        item_index=0,
        batch_size=1,
        depth=1,
        agent_subscription_id=generate_ulid(),
        agent_id=generate_ulid(),
        prompt="Draft the note.",
        answer_max_chars=4000,
        lifecycle_generation="lifecycle-1",
        phase=TaskCoordinatorPhase.READY.value,
        state_version=0,
    )
    db.add(child)
    await db.flush()
    return child.id


_CASES = [
    pytest.param(
        RunCase(
            spec=TASK_COORDINATOR_RUN_SPEC,
            make_run=_task_coordinator_run,
            ready_phase=TaskCoordinatorPhase.READY.value,
            parked_phase=TaskCoordinatorPhase.WAITING_HUMAN.value,
        ),
        id="task_coordinator",
    ),
    pytest.param(
        RunCase(
            spec=TASK_AGENT_CHILD_RUN_SPEC,
            make_run=_task_agent_child_run,
            ready_phase=TaskCoordinatorPhase.READY.value,
            parked_phase=TaskCoordinatorPhase.WAITING_HUMAN.value,
        ),
        id="task_agent_child",
    ),
]


@pytest.fixture(params=_CASES)
def case(request) -> RunCase:
    return request.param


async def _run(db, case: RunCase, key: str):
    return await durable_run.lock_run(db, case.spec, key)


async def _event(db, case: RunCase, key: str) -> RuntimeOutboxEvent:
    return (await db.execute(
        select(RuntimeOutboxEvent)
        .where(RuntimeOutboxEvent.dedupe_key == case.spec.dedupe_key(key))
        .execution_options(populate_existing=True)
    )).scalar_one()


async def _claim(db, case: RunCase, key: str, wake, token: str | None = None, **kwargs):
    return await durable_run.claim_run_wake(
        db, case.spec,
        key=key,
        event_id=wake.event_id,
        wake_generation=wake.wake_generation,
        claim_token=token or generate_ulid(),
        lease_seconds=LEASE,
        **kwargs,
    )


async def _staged_and_claimed(db, case: RunCase):
    key = await case.make_run(db)
    wake = await durable_run.stage_run_wake(db, case.spec, key=key, reason="test")
    claim, reason = await _claim(db, case, key, wake)
    assert reason == "claimed"
    return key, wake, claim


# --- spec shape ---------------------------------------------------------------


def test_spec_payload_round_trips_the_key(case):
    payload = case.spec.payload("key-1", "gen-1", "why")
    assert payload["version"] == case.spec.outbox_version
    assert payload[case.spec.payload_key] == "key-1"
    assert payload["wake_generation"] == "gen-1"
    assert case.spec.terminal_phases <= case.spec.phases
    assert case.ready_phase in case.spec.phases - case.spec.terminal_phases
    assert case.spec.dedupe_key("a") != case.spec.dedupe_key("b")


# --- staging ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_replaces_the_single_wake_with_a_fresh_generation(db_session, case):
    key = await case.make_run(db_session)
    first = await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="first")
    second = await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="second")
    assert first.wake_generation != second.wake_generation
    # One stable outbox row per run, now carrying the newer generation.
    assert second.event_id == first.event_id
    event = await _event(db_session, case, key)
    assert event.payload["wake_generation"] == second.wake_generation
    assert event.payload["reason"] == "second"
    assert event.delivered_at is None
    assert event.last_error == case.spec.wake_pending_error
    run = await _run(db_session, case, key)
    assert run.wake_generation == second.wake_generation
    assert run.state_version == 2


@pytest.mark.asyncio
async def test_stage_if_pending_keeps_the_current_wake(db_session, case):
    key = await case.make_run(db_session)
    first = await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="first")
    again = await durable_run.stage_run_wake(
        db_session, case.spec, key=key, reason="again", if_pending=True,
    )
    assert again.wake_generation == first.wake_generation
    assert (await _run(db_session, case, key)).state_version == 1


@pytest.mark.asyncio
async def test_terminal_run_is_never_woken(db_session, case):
    key = await case.make_run(db_session)
    run = await _run(db_session, case, key)
    run.phase = sorted(case.spec.terminal_phases)[0]
    assert await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="late") is None
    assert await durable_run.stage_run_wake(
        db_session, case.spec, key=generate_ulid(), reason="missing",
    ) is None


@pytest.mark.asyncio
async def test_stage_rejects_a_stale_state_version(db_session, case):
    key = await case.make_run(db_session)
    with pytest.raises(ValueError, match="state version changed"):
        await durable_run.stage_run_wake(
            db_session, case.spec, key=key, reason="cas", expected_state_version=7,
        )


@pytest.mark.asyncio
async def test_stale_owner_cannot_replace_a_successor_wake(db_session, case):
    key, wake, claim = await _staged_and_claimed(db_session, case)
    # Someone else stages the next wake while the owner still runs.
    await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="child_settled")
    with pytest.raises(ValueError, match="wake generation was lost"):
        await durable_run.stage_run_wake(
            db_session, case.spec, key=key, reason="owner_restage",
            claim_token=claim.claim_token, claim_generation=claim.wake_generation,
        )
    with pytest.raises(ValueError, match="supplied together"):
        await durable_run.stage_run_wake(
            db_session, case.spec, key=key, reason="half", claim_token=claim.claim_token,
        )


@pytest.mark.parametrize(
    ("delay_seconds", "available_offset", "expected_offset"),
    [
        (0, None, timedelta(0)),
        (-5, None, timedelta(0)),
        (math.nan, None, timedelta(0)),
        ("not a number", None, timedelta(0)),
        (3600, None, timedelta(hours=1)),
        (10 ** 12, None, DURABLE_RUN_MAX_WAKE_DELAY),
        (math.inf, None, DURABLE_RUN_MAX_WAKE_DELAY),
        (0, timedelta(days=365 * 5), DURABLE_RUN_MAX_WAKE_DELAY),
        (0, timedelta(days=-3), timedelta(0)),
        (10 ** 12, timedelta(hours=2), timedelta(hours=2)),
    ],
)
def test_wake_delay_is_bounded_to_ninety_days(delay_seconds, available_offset, expected_offset):
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    available_at = None if available_offset is None else now + available_offset
    assert durable_run.bounded_wake_time(
        now, delay_seconds=delay_seconds, available_at=available_at,
    ) == now + expected_offset
    assert DURABLE_RUN_MAX_WAKE_DELAY == timedelta(days=90)


@pytest.mark.asyncio
async def test_staged_wake_time_is_capped(db_session, case):
    key = await case.make_run(db_session)
    before = datetime.now(timezone.utc)
    wake = await durable_run.stage_run_wake(
        db_session, case.spec, key=key, reason="far", delay_seconds=math.inf,
    )
    assert before + DURABLE_RUN_MAX_WAKE_DELAY <= wake.available_at
    assert wake.available_at <= datetime.now(timezone.utc) + DURABLE_RUN_MAX_WAKE_DELAY


# --- claim --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_takes_only_the_current_generation(db_session, case):
    key = await case.make_run(db_session)
    stale = await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="old")
    current = await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="new")
    claim, reason = await _claim(db_session, case, key, stale)
    assert (claim, reason) == (None, "stale_generation")
    run = await _run(db_session, case, key)
    assert run.claim_token is None
    claim, reason = await _claim(db_session, case, key, current)
    assert reason == "claimed"
    assert claim.key == key and claim.wake_generation == current.wake_generation
    assert claim.state_version == run.state_version


@pytest.mark.asyncio
async def test_claim_refuses_a_delivered_or_foreign_event(db_session, case):
    key = await case.make_run(db_session)
    other = await case.make_run(db_session)
    await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="mine")
    foreign = await durable_run.stage_run_wake(db_session, case.spec, key=other, reason="theirs")
    claim, reason = await durable_run.claim_run_wake(
        db_session, case.spec, key=key, event_id=foreign.event_id,
        wake_generation=foreign.wake_generation, claim_token=generate_ulid(),
        lease_seconds=LEASE,
    )
    assert (claim, reason) == (None, "stale_generation")
    claim, reason = await durable_run.claim_run_wake(
        db_session, case.spec, key=key, event_id=generate_ulid(),
        wake_generation=foreign.wake_generation, claim_token=generate_ulid(),
        lease_seconds=LEASE,
    )
    assert (claim, reason) == (None, "missing_or_delivered_event")


@pytest.mark.asyncio
async def test_new_wake_never_steals_a_live_claim(db_session, case):
    key, _wake, owner = await _staged_and_claimed(db_session, case)
    successor = await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="next")
    run = await _run(db_session, case, key)
    # Staging left the owner's claim intact.
    assert (run.claim_token, run.claim_generation) == (owner.claim_token, owner.wake_generation)
    claim, reason = await _claim(db_session, case, key, successor)
    assert (claim, reason) == (None, "claim_held")
    # Once the owner's lease lapses the successor may claim.
    run.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    claim, reason = await _claim(db_session, case, key, successor)
    assert reason == "claimed"
    assert claim.claim_token != owner.claim_token


@pytest.mark.asyncio
async def test_owner_may_reclaim_its_own_generation(db_session, case):
    key = await case.make_run(db_session)
    wake = await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="t")
    token = generate_ulid()
    first, _ = await _claim(db_session, case, key, wake, token)
    second, reason = await _claim(db_session, case, key, wake, token)
    assert reason == "claimed"
    assert second.state_version == first.state_version + 1


@pytest.mark.asyncio
async def test_claim_admission_and_terminal_refusals(db_session, case):
    key = await case.make_run(db_session)
    wake = await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="t")
    seen = []

    def refuse(run):
        seen.append(run)
        return "stale_lifecycle"

    claim, reason = await _claim(db_session, case, key, wake, admit=refuse)
    assert (claim, reason) == (None, "stale_lifecycle")
    assert len(seen) == 1 and seen[0].claim_token is None
    run = await _run(db_session, case, key)
    run.phase = sorted(case.spec.terminal_phases)[0]
    claim, reason = await _claim(db_session, case, key, wake, admit=lambda _run: None)
    assert (claim, reason) == (None, "terminal_run")
    claim, reason = await durable_run.claim_run_wake(
        db_session, case.spec, key=generate_ulid(), event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token=generate_ulid(),
        lease_seconds=LEASE,
    )
    assert (claim, reason) == (None, "missing_run")
    with pytest.raises(ValueError, match="positive lease"):
        await durable_run.claim_run_wake(
            db_session, case.spec, key=key, event_id=wake.event_id,
            wake_generation=wake.wake_generation, claim_token=generate_ulid(),
            lease_seconds=0,
        )


# --- renew / checkpoint -------------------------------------------------------


@pytest.mark.asyncio
async def test_renew_extends_only_the_exact_owner(db_session, case):
    key, _wake, claim = await _staged_and_claimed(db_session, case)
    run = await _run(db_session, case, key)
    before = run.claim_expires_at
    assert not await durable_run.renew_run_claim(
        db_session, case.spec, key=key, claim_token=generate_ulid(),
        claim_generation=claim.wake_generation, lease_seconds=LEASE * 10,
    )
    assert run.claim_expires_at == before
    assert await durable_run.renew_run_claim(
        db_session, case.spec, key=key, claim_token=claim.claim_token,
        claim_generation=claim.wake_generation, lease_seconds=LEASE * 10,
    )
    assert run.claim_expires_at > before
    run.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    # An expired claim is gone, even for its former owner.
    assert not await durable_run.renew_run_claim(
        db_session, case.spec, key=key, claim_token=claim.claim_token,
        claim_generation=claim.wake_generation, lease_seconds=LEASE,
    )


@pytest.mark.asyncio
async def test_checkpoint_is_a_compare_and_swap(db_session, case):
    key, _wake, claim = await _staged_and_claimed(db_session, case)
    version = await durable_run.checkpoint_run(
        db_session, case.spec, key=key,
        claim_token=claim.claim_token, claim_generation=claim.wake_generation,
        expected_state_version=claim.state_version,
        checkpoint={"round": 1}, phase=case.ready_phase,
    )
    assert version == claim.state_version + 1
    # Replaying the same expected version fails and writes nothing.
    assert await durable_run.checkpoint_run(
        db_session, case.spec, key=key,
        claim_token=claim.claim_token, claim_generation=claim.wake_generation,
        expected_state_version=claim.state_version, checkpoint={"round": "stale"},
    ) is None
    # So does a stranger with the right version.
    assert await durable_run.checkpoint_run(
        db_session, case.spec, key=key,
        claim_token=generate_ulid(), claim_generation=claim.wake_generation,
        expected_state_version=version, checkpoint={"round": "stranger"},
    ) is None
    run = await _run(db_session, case, key)
    assert run.checkpoint == {"round": 1}
    assert run.state_version == version


@pytest.mark.asyncio
async def test_checkpoint_validates_shape_and_runs_apply_after_the_cas(db_session, case):
    key, _wake, claim = await _staged_and_claimed(db_session, case)
    common = dict(
        key=key, claim_token=claim.claim_token, claim_generation=claim.wake_generation,
    )
    with pytest.raises(ValueError, match="JSON object or null"):
        await durable_run.checkpoint_run(
            db_session, case.spec, expected_state_version=claim.state_version,
            checkpoint=["not", "a", "dict"], **common,
        )
    with pytest.raises(ValueError, match="phase"):
        await durable_run.checkpoint_run(
            db_session, case.spec, expected_state_version=claim.state_version,
            phase="no_such_phase", **common,
        )
    applied = []
    assert await durable_run.checkpoint_run(
        db_session, case.spec, expected_state_version=claim.state_version - 1,
        apply=applied.append, **common,
    ) is None
    assert applied == []
    assert await durable_run.checkpoint_run(
        db_session, case.spec, expected_state_version=claim.state_version,
        apply=applied.append, **common,
    ) == claim.state_version + 1
    assert len(applied) == 1


# --- settle (ACK) / release ---------------------------------------------------


@pytest.mark.asyncio
async def test_settle_acks_only_the_owned_generation(db_session, case):
    key, wake, owner = await _staged_and_claimed(db_session, case)
    successor = await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="next")
    # The old owner cannot ACK: the row now carries the successor generation.
    assert not await durable_run.settle_run_wake(
        db_session, case.spec, key=key, event_id=wake.event_id,
        wake_generation=owner.wake_generation, claim_token=owner.claim_token,
    )
    event = await _event(db_session, case, key)
    assert event.delivered_at is None
    assert event.payload["wake_generation"] == successor.wake_generation
    # Nor can it ACK the successor generation it never claimed.
    assert not await durable_run.settle_run_wake(
        db_session, case.spec, key=key, event_id=wake.event_id,
        wake_generation=successor.wake_generation, claim_token=owner.claim_token,
    )
    assert await durable_run.release_run_claim(
        db_session, case.spec, key=key,
        claim_token=owner.claim_token, claim_generation=owner.wake_generation,
    )
    claim, reason = await _claim(db_session, case, key, successor)
    assert reason == "claimed"
    assert await durable_run.settle_run_wake(
        db_session, case.spec, key=key, event_id=successor.event_id,
        wake_generation=claim.wake_generation, claim_token=claim.claim_token,
    )
    event = await _event(db_session, case, key)
    assert event.delivered_at is not None and event.last_error is None
    assert (await _run(db_session, case, key)).wake_generation is None
    # An ACK is once only.
    assert not await durable_run.settle_run_wake(
        db_session, case.spec, key=key, event_id=successor.event_id,
        wake_generation=claim.wake_generation, claim_token=claim.claim_token,
    )


@pytest.mark.asyncio
async def test_release_drops_only_its_own_claim(db_session, case):
    key, _wake, claim = await _staged_and_claimed(db_session, case)
    assert not await durable_run.release_run_claim(
        db_session, case.spec, key=key,
        claim_token=generate_ulid(), claim_generation=claim.wake_generation,
    )
    assert not await durable_run.release_run_claim(
        db_session, case.spec, key=key,
        claim_token=claim.claim_token, claim_generation=generate_ulid(),
    )
    run = await _run(db_session, case, key)
    assert run.claim_token == claim.claim_token
    version = run.state_version
    assert await durable_run.release_run_claim(
        db_session, case.spec, key=key,
        claim_token=claim.claim_token, claim_generation=claim.wake_generation,
    )
    assert (run.claim_token, run.claim_generation, run.claim_expires_at) == (None, None, None)
    assert run.state_version == version + 1
    # The wake itself is untouched; a released owner never ACKed it.
    assert run.wake_generation == claim.wake_generation


@pytest.mark.asyncio
async def test_release_never_clears_a_successor_claim(db_session, case):
    key, _wake, owner = await _staged_and_claimed(db_session, case)
    run = await _run(db_session, case, key)
    run.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    successor = await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="next")
    claim, _ = await _claim(db_session, case, key, successor)
    assert not await durable_run.release_run_claim(
        db_session, case.spec, key=key,
        claim_token=owner.claim_token, claim_generation=owner.wake_generation,
    )
    assert run.claim_token == claim.claim_token


# --- lifecycle helpers --------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_run_claim_and_wake_fences_every_prior_owner(db_session, case):
    key, wake, claim = await _staged_and_claimed(db_session, case)
    run = await _run(db_session, case, key)
    version = run.state_version
    durable_run.clear_run_claim_and_wake(run)
    assert (run.claim_token, run.claim_generation, run.claim_expires_at, run.wake_generation) == (
        None, None, None, None,
    )
    assert run.state_version == version + 1
    assert await durable_run.checkpoint_run(
        db_session, case.spec, key=key, claim_token=claim.claim_token,
        claim_generation=claim.wake_generation, expected_state_version=run.state_version,
        checkpoint={},
    ) is None
    assert not await durable_run.settle_run_wake(
        db_session, case.spec, key=key, event_id=wake.event_id,
        wake_generation=wake.wake_generation, claim_token=claim.claim_token,
    )
    assert await durable_run.close_run_wake(
        db_session, case.spec, key=key, last_error="permanent:closed",
    )
    event = await _event(db_session, case, key)
    assert event.delivered_at is not None and event.last_error == "permanent:closed"
    assert not await durable_run.close_run_wake(
        db_session, case.spec, key=key, last_error="permanent:closed",
    )


@pytest.mark.asyncio
async def test_reopen_run_restages_and_fences_the_parked_generation(db_session, case):
    key, parked_wake, claim = await _staged_and_claimed(db_session, case)
    run = await _run(db_session, case, key)
    run.phase = case.parked_phase
    version = run.state_version
    wake = await durable_run.reopen_run(
        db_session, case.spec, run, phase=case.ready_phase, reason="answered",
    )
    assert wake.wake_generation != parked_wake.wake_generation
    assert run.phase == case.ready_phase
    assert run.claim_token is None
    # Reopening adds exactly the stage's version bump.
    assert run.state_version == version + 1
    claim2, reason = await _claim(db_session, case, key, parked_wake)
    assert (claim2, reason) == (None, "stale_generation")
    with pytest.raises(ValueError, match="cannot reopen"):
        await durable_run.reopen_run(
            db_session, case.spec, run,
            phase=sorted(case.spec.terminal_phases)[0], reason="nope",
        )


@pytest.mark.asyncio
async def test_wake_failures_count_per_generation_for_the_owner_only(db_session, case):
    key, _wake, claim = await _staged_and_claimed(db_session, case)
    stranger = await durable_run.record_run_wake_failure(
        db_session, case.spec, key=key, claim_token=generate_ulid(),
        claim_generation=claim.wake_generation, error=RuntimeError("x"),
    )
    assert stranger == (0, {})
    for expected in (1, 2):
        count, checkpoint = await durable_run.record_run_wake_failure(
            db_session, case.spec, key=key, claim_token=claim.claim_token,
            claim_generation=claim.wake_generation, error=RuntimeError("boom"),
        )
        assert count == expected
    failures = checkpoint[durable_run.DEFAULT_WAKE_FAILURES_KEY]
    assert failures == {
        "wake_generation": claim.wake_generation, "count": 2,
        "exception": "RuntimeError", "error": "boom",
    }
    assert (await _run(db_session, case, key)).checkpoint == checkpoint
    # A different generation's counter starts over.
    run = await _run(db_session, case, key)
    run.checkpoint = {
        durable_run.DEFAULT_WAKE_FAILURES_KEY: {"wake_generation": "older", "count": 2},
    }
    count, _ = await durable_run.record_run_wake_failure(
        db_session, case.spec, key=key, claim_token=claim.claim_token,
        claim_generation=claim.wake_generation, error=ValueError("again"),
    )
    assert count == 1

@pytest.mark.asyncio
async def test_checkpoint_apply_raising_rejects_the_transition(db_session, case):
    key, _wake, claim = await _staged_and_claimed(db_session, case)
    await db_session.commit()

    def reject(_run):
        raise ValueError("foreign plan")

    with pytest.raises(ValueError, match="foreign plan"):
        await durable_run.checkpoint_run(
            db_session, case.spec, key=key,
            claim_token=claim.claim_token, claim_generation=claim.wake_generation,
            expected_state_version=claim.state_version,
            checkpoint={"round": "rejected"}, phase=case.parked_phase, apply=reject,
        )
    # The caller's transaction rolls back: nothing of the transition stays.
    await db_session.rollback()
    run = await _run(db_session, case, key)
    assert run.checkpoint is None
    assert run.phase == case.ready_phase
    assert run.state_version == claim.state_version
    # And the owner can still checkpoint at the same version.
    assert await durable_run.checkpoint_run(
        db_session, case.spec, key=key,
        claim_token=claim.claim_token, claim_generation=claim.wake_generation,
        expected_state_version=claim.state_version, checkpoint={"round": 1},
    ) == claim.state_version + 1
    await db_session.commit()


@pytest.mark.asyncio
async def test_holds_run_claim_only_for_the_owner_of_the_current_wake(db_session, case):
    key, _wake, claim = await _staged_and_claimed(db_session, case)

    async def holds(token=claim.claim_token, generation=claim.wake_generation, run_key=key):
        return await durable_run.holds_run_claim(
            db_session, case.spec, key=run_key, claim_token=token, claim_generation=generation,
        )

    assert await holds()
    assert not await holds(token=generate_ulid())
    assert not await holds(generation=generate_ulid())
    assert not await holds(run_key=generate_ulid())
    # A newer wake leaves the claim live but no longer on the current wake.
    await durable_run.stage_run_wake(db_session, case.spec, key=key, reason="next")
    assert not await holds()
    run = await _run(db_session, case, key)
    run.wake_generation = claim.wake_generation  # test-only: back on the owned wake
    assert await holds()
    run.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not await holds()


def test_claim_is_live_needs_a_token_and_an_unexpired_lease():
    from types import SimpleNamespace

    now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    later = now + timedelta(minutes=1)
    earlier = now - timedelta(minutes=1)

    def run(token, expires):
        return SimpleNamespace(claim_token=token, claim_expires_at=expires)

    assert durable_run.claim_is_live(run("t", later), now=now)
    assert not durable_run.claim_is_live(run("t", earlier), now=now)
    assert not durable_run.claim_is_live(run("t", now), now=now)
    assert not durable_run.claim_is_live(run(None, later), now=now)
    assert not durable_run.claim_is_live(run("", later), now=now)
    assert not durable_run.claim_is_live(run("t", None), now=now)
    # A naive timestamp is read as UTC.
    assert durable_run.claim_is_live(run("t", later.replace(tzinfo=None)), now=now)
    assert not durable_run.claim_is_live(run("t", earlier.replace(tzinfo=None)), now=now)
    # Without ``now`` the wall clock decides.
    assert durable_run.claim_is_live(run("t", datetime.now(timezone.utc) + timedelta(minutes=5)))
    assert not durable_run.claim_is_live(run("t", datetime.now(timezone.utc) - timedelta(minutes=5)))


# --- guard: only durable_run.py writes claim/wake fields ----------------------

# No other table has these columns: a write anywhere is a run write.
_RUN_ONLY_FIELDS = frozenset({"claim_generation", "wake_generation"})
# Shared with other claim tables (blueprint checkout refunds, Apify
# collections, initiative deliveries, notification schedules): flagged only in
# a module that can reach a durable run.
_SHARED_FIELDS = frozenset({"claim_token", "claim_expires_at"})
_PROTOCOL_FIELDS = _RUN_ONLY_FIELDS | _SHARED_FIELDS
_PROTOCOL_MODULE = "packages/core/services/durable_run.py"
# Importing from these modules reaches run rows and their specs.
_RUN_MODULES = frozenset({"durable_run", "task_coordinator_wakeup", "task_agent_child_wakeup"})
_SCANNED_ROOTS = ("packages", "apps")
_SQL_SET = re.compile(
    r"\bSET\b[^;]*?\b(claim_token|claim_generation|claim_expires_at|wake_generation)\b\s*=",
    re.IGNORECASE | re.DOTALL,
)


def _python_sources() -> list[Path]:
    paths: list[Path] = []
    for root in _SCANNED_ROOTS:
        for path in (REPO_ROOT / root).rglob("*.py"):
            parts = set(path.relative_to(REPO_ROOT).parts)
            if parts & {"node_modules", "tests", "__pycache__", ".venv"}:
                continue
            paths.append(path)
    return paths


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _name_of(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    return getattr(node, "attr", "") or ""


def _durable_run_specs(sources: dict[str, ast.AST]) -> tuple[set[str], set[str]]:
    """(models, spec names): what every ``DurableRunSpec(...)`` declares, repo-wide.

    The model may be passed by keyword or as the first positional argument;
    a spec name is any name the call's result is bound to.
    """

    models: set[str] = set()
    spec_names: set[str] = set()
    for tree in sources.values():
        for node in ast.walk(tree):
            value = getattr(node, "value", None)
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and isinstance(value, ast.Call) and _call_name(value) == "DurableRunSpec"
            ):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                spec_names.update(_name_of(target) for target in targets if _name_of(target))
            if not isinstance(node, ast.Call) or _call_name(node) != "DurableRunSpec":
                continue
            if node.args:
                models.add(_name_of(node.args[0]))
            for keyword in node.keywords:
                if keyword.arg == "model":
                    models.add(_name_of(keyword.value))
    models.discard("")
    return models, spec_names


def _reaches_a_run(tree: ast.AST, models: set[str], spec_names: set[str]) -> bool:
    """The module names a run model or spec, or imports the run modules."""

    armed = models | spec_names
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in armed:
            return True
        if isinstance(node, ast.Attribute) and node.attr in armed | _RUN_MODULES:
            return True
        if isinstance(node, ast.ImportFrom):
            module = (node.module or "").rsplit(".", 1)[-1]
            if module in _RUN_MODULES:
                return True
            if any((alias.name in armed | _RUN_MODULES) for alias in node.names):
                return True
        if isinstance(node, ast.Import) and any(
            alias.name.rsplit(".", 1)[-1] in _RUN_MODULES for alias in node.names
        ):
            return True
    return False


def _mapping_fields(node: ast.AST | None) -> list[str]:
    """Field names a dict literal / ``dict(...)`` / list of them would write."""

    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [field for element in node.elts for field in _mapping_fields(element)]
    if isinstance(node, ast.Dict):
        fields = []
        for key, value in zip(node.keys, node.values):
            if key is None:  # {**{...}}
                fields.extend(_mapping_fields(value))
            elif isinstance(key, ast.Constant) and key.value in _PROTOCOL_FIELDS:
                fields.append(key.value)
        return fields
    if isinstance(node, ast.Call) and _call_name(node) == "dict":
        fields = [keyword.arg for keyword in node.keywords if keyword.arg in _PROTOCOL_FIELDS]
        for keyword in node.keywords:
            if keyword.arg is None:
                fields.extend(_mapping_fields(keyword.value))
        for arg in node.args:
            fields.extend(_mapping_fields(arg))
        return fields
    return []


def _is_statement(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_name(node) in {"update", "insert"}


def _protocol_writes(
    tree: ast.AST, models: set[str], spec_names: set[str] = frozenset(),
) -> list[tuple[int, str]]:
    """Writes of a protocol field.

    ``claim_generation`` / ``wake_generation`` writes are flagged anywhere;
    ``claim_token`` / ``claim_expires_at`` only in a module that reaches a
    run (see ``_reaches_a_run``), since other claim tables share them.
    Covers attribute assignment (plain, chained, augmented, annotated,
    tuple), ``setattr``, constructor and ``values()`` keywords, dicts passed
    to ``values()`` positionally or as ``**``, ``set_={...}`` / ``dict(...)``,
    bulk ``execute(update(Model), [{...}])`` and SQL ``SET field =`` strings.
    """

    raw: list[tuple[int, str]] = []

    def target_hit(target: ast.AST) -> None:
        if isinstance(target, ast.Attribute) and target.attr in _PROTOCOL_FIELDS:
            raw.append((target.lineno, target.attr))
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                target_hit(element)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                target_hit(target)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            target_hit(node.target)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            raw.extend((node.lineno, match.group(1)) for match in _SQL_SET.finditer(node.value))
        elif isinstance(node, ast.Call):
            func_name = _call_name(node)
            if (
                func_name == "setattr" and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _PROTOCOL_FIELDS
            ):
                raw.append((node.lineno, node.args[1].value))
            if func_name in models or func_name in {"values", "on_conflict_do_update"}:
                for keyword in node.keywords:
                    if keyword.arg in _PROTOCOL_FIELDS:
                        raw.append((node.lineno, keyword.arg))
                    elif keyword.arg in {None, "set_"}:
                        raw.extend((node.lineno, field) for field in _mapping_fields(keyword.value))
                if func_name == "values":
                    for arg in node.args:
                        raw.extend((node.lineno, field) for field in _mapping_fields(arg))
            if func_name == "execute" and node.args and _is_statement(node.args[0]):
                for arg in node.args[1:2]:
                    raw.extend((node.lineno, field) for field in _mapping_fields(arg))
    if not raw:
        return []
    reaches = _reaches_a_run(tree, models, spec_names)
    return [(line, field) for line, field in raw if field in _RUN_ONLY_FIELDS or reaches]


def _parsed_sources() -> dict[str, ast.AST]:
    parsed: dict[str, ast.AST] = {}
    for path in _python_sources():
        try:
            parsed[str(path.relative_to(REPO_ROOT))] = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
    return parsed


def test_only_the_protocol_module_writes_run_claim_and_wake_fields():
    sources = _parsed_sources()
    models, spec_names = _durable_run_specs(sources)
    assert {"TaskCoordinatorRun", "TaskAgentChildRun"} <= models
    assert {"TASK_COORDINATOR_RUN_SPEC", "TASK_AGENT_CHILD_RUN_SPEC"} <= spec_names
    violations = {
        path: hits
        for path, tree in sources.items()
        if path != _PROTOCOL_MODULE
        for hits in [_protocol_writes(tree, models, spec_names)]
        if hits
    }
    assert violations == {}, (
        "Only services/durable_run.py may write a durable run's claim/wake fields; "
        "use its helpers (clear_run_claim_and_wake, reopen_run, stage_run_wake, ...). "
        f"Found: {violations}"
    )
    # The scanner itself still sees the protocol's own writes.
    own = _protocol_writes(sources[_PROTOCOL_MODULE], models, spec_names | {"DurableRunSpec"})
    assert {field for _line, field in own} == set(_PROTOCOL_FIELDS)


_MODEL_IMPORT = "from packages.core.models.task_coordinator import TaskCoordinatorRun\n"


@pytest.mark.parametrize(
    "source",
    [
        # Modules that name the run model.
        *(
            _MODEL_IMPORT + snippet
            for snippet in (
                "run.claim_token = None",
                "run.claim_token = run.claim_expires_at = None",
                "run.claim_expires_at += delta",
                "setattr(run, 'claim_token', None)",
                "TaskCoordinatorRun(task_id='t', claim_token='x')",
                "update(TaskCoordinatorRun).values(claim_expires_at=None)",
                "stmt.on_conflict_do_update(index_elements=[], set_={'claim_token': None})",
                # Dicts handed to values(), positionally or unpacked.
                "update(TaskCoordinatorRun).values({'claim_token': None})",
                "update(TaskCoordinatorRun).values(**{'claim_expires_at': None})",
                "update(TaskCoordinatorRun).values(dict(claim_token=None))",
                "stmt.on_conflict_do_update(index_elements=[], set_=dict(claim_token=None))",
                "stmt.on_conflict_do_update(index_elements=[], set_={**{'claim_token': None}})",
                # Bulk ORM update / insert with parameter dicts.
                "db.execute(update(TaskCoordinatorRun), [{'id': 'r', 'claim_token': None}])",
                "db.execute(insert(TaskCoordinatorRun), {'id': 'r', 'claim_expires_at': None})",
                # Raw SQL.
                "db.execute(text('UPDATE task_coordinator_runs SET claim_token = NULL'))",
                "db.execute(text('''UPDATE task_coordinator_runs\n SET phase = 'x',\n claim_expires_at = NULL'''))",
            )
        ),
        # Modules that reach a run without naming its model.
        "from packages.core.services.durable_run import lock_run\nrun.claim_token = None",
        "from packages.core.services import durable_run\nrow.claim_expires_at = None",
        "import packages.core.services.durable_run\nrow.claim_token = None",
        "from packages.core.services.task_coordinator_wakeup import stage_task_coordinator_wake\n"
        "run.claim_expires_at = None",
        "spec = TASK_COORDINATOR_RUN_SPEC\nrun.claim_token = None",
        "spec = wakeup.TASK_COORDINATOR_RUN_SPEC\nrun.claim_token = None",
        # Run-only fields: flagged in any module, no import needed.
        "row.wake_generation = None",
        "row.claim_generation: str = 'g'",
        "row.claim_generation, other = None, 1",
        "setattr(row, 'wake_generation', None)",
        "stmt.values(wake_generation=None)",
        "stmt.values({'claim_generation': None})",
        "db.execute(update(Anything), [{'wake_generation': None}])",
        "db.execute(text('update t set phase = 1, wake_generation = null'))",
    ],
)
def test_guard_catches_every_write_shape(source):
    assert _protocol_writes(
        ast.parse(source), {"TaskCoordinatorRun"}, {"TASK_COORDINATOR_RUN_SPEC"},
    ), source


def test_guard_finds_specs_declared_positionally_or_by_keyword():
    tree = ast.parse(
        "A_SPEC = DurableRunSpec(ChildRun, key_column=ChildRun.id)\n"
        "B_SPEC: DurableRunSpec = durable_run.DurableRunSpec(model=models.OtherRun)\n"
    )
    models, spec_names = _durable_run_specs({"x.py": tree})
    assert models == {"ChildRun", "OtherRun"}
    assert spec_names == {"A_SPEC", "B_SPEC"}


def test_guard_ignores_other_claim_tables_reads_and_payload_keys():
    other_table = (
        "job.claim_token = None\njob.claim_expires_at = None\n"
        "update(RefundJob).values(claim_token=None)\n"
        "text('ON CONFLICT DO UPDATE SET claim_token = EXCLUDED.claim_token')\n"
    )
    assert _protocol_writes(ast.parse(other_table), {"TaskCoordinatorRun"}) == []
    reads = (
        _MODEL_IMPORT
        + "ok = run.claim_token == token and run.wake_generation is None\n"
        "payload = {'wake_generation': generation, 'claim_token': token}\n"
        "claim = SimpleNamespace(run_id=run.id, wake_generation=None, claim_token=None)\n"
        "where = 'wake_generation is null'\n"
    )
    assert _protocol_writes(ast.parse(reads), {"TaskCoordinatorRun"}) == []
