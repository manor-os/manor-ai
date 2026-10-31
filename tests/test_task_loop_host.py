"""TaskLoopHost: the Task agent loop's durable host, addressed by spec + key.

The Task agent loop module never touches its run row's durable-run writes or
claim checks directly: every checkpoint, stage, settle, release, claim check,
heartbeat and fence goes through its ``TaskLoopHost``. Only an explicit
allowlist of module definitions (the root run's writes, the host factory, the
per-kind claim helpers the host is built with, and the root terminal write)
may name them, and no code in the module touches ``host.writes`` or assigns
the run's expected ``state["version"]``. An AST guard keeps it that way
(mutation-checked below). The host's own run writes go through its ``writes``
object only, so a different run kind drives the same host by passing its own
spec and key.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.ai.runtime.run_state import RuntimeRunState
from packages.core.models.base import generate_ulid
from packages.core.models.task_coordinator import (
    TaskCoordinatorEngine,
    TaskCoordinatorPhase,
    TaskCoordinatorRun,
)
from packages.core.services import durable_run
from packages.core.services.task_agent_loop_state import task_loop_initial_state
from packages.core.services.task_coordinator_wakeup import TASK_COORDINATOR_RUN_SPEC
from packages.core.workers import task_agent_loop as worker
from packages.core.workers import task_loop_host
from packages.core.workers.task_loop_host import DurableRunWrites, TaskLoopHost


REPO_ROOT = Path(__file__).resolve().parents[1]
LOOP_SOURCE = REPO_ROOT / "packages/core/workers/task_agent_loop.py"
HOST_SOURCE = REPO_ROOT / "packages/core/workers/task_loop_host.py"

# The durable-run protocol's run writes and claim checks
# (services/durable_run.py) and the Task coordinator wrappers around them.
_PROTOCOL_CALLS = frozenset({
    "stage_run_wake", "claim_run_wake", "renew_run_claim", "checkpoint_run",
    "settle_run_wake", "release_run_claim", "holds_run_claim",
    "record_run_wake_failure", "clear_run_claim_and_wake", "reopen_run",
    "close_run_wake",
    "stage_task_coordinator_wake", "claim_task_coordinator_wake",
    "renew_task_coordinator_claim", "checkpoint_task_coordinator_run",
    "settle_task_coordinator_wake", "release_task_coordinator_claim",
})
# What only the host may do for the loop: its claim checks, heartbeat,
# model fence and crash counter (the per-kind admission stays the loop's).
_HOST_ONLY = _PROTOCOL_CALLS | frozenset({
    "_claim_held", "_claim_is_current", "_heartbeat", "heartbeat",
    "fence_before_model", "_record_wake_failure",
})


def _referenced(node: ast.AST, names: frozenset[str]) -> list[str]:
    """Every name or attribute in ``names`` used anywhere under ``node``.

    References, not only calls: passing a writer as a callback is the same
    bypass as calling it.
    """

    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in names:
            found.append(f"{child.id}:{child.lineno}")
        elif isinstance(child, ast.Attribute) and child.attr in names:
            found.append(f"{child.attr}:{child.lineno}")
    return found


def _class(tree: ast.AST, name: str) -> ast.ClassDef:
    matches = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == name]
    assert len(matches) == 1, f"expected one class {name}"
    return matches[0]


# The task_agent_loop.py definitions that may name a host-only writer or
# claim check: the root run's writes and host factory, the per-kind claim
# helpers the host is built with (and the Task-thread bind that checks the
# claim under the Task lock), and the root terminal write.
_LOOP_ALLOWED = frozenset({
    "_TaskCoordinatorRunWrites", "_task_loop_host", "_claim_held",
    "_claim_is_current", "_heartbeat", "_ensure_task_conversation",
    "_write_terminal",
})


def _host_writes_access(node: ast.AST) -> list[str]:
    """Any ``<x>.writes`` use: the loop reaches the row through host methods."""

    return [
        f"writes:{child.lineno}" for child in ast.walk(node)
        if isinstance(child, ast.Attribute) and child.attr == "writes"
    ]


def _version_assignments(node: ast.AST) -> list[str]:
    """``state["version"] = ...``: only the host advances the expected version."""

    found = []
    for child in ast.walk(node):
        targets = (
            child.targets if isinstance(child, ast.Assign)
            else [child.target] if isinstance(child, (ast.AugAssign, ast.AnnAssign))
            else []
        )
        for target in targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "version"
                and (
                    (isinstance(target.value, ast.Name) and target.value.id == "state")
                    or (isinstance(target.value, ast.Attribute) and target.value.attr in {"state", "_state"})
                )
            ):
                found.append(f"state[version]:{child.lineno}")
    return found


def _loop_bypasses(source: str) -> list[str]:
    tree = ast.parse(source)
    found = _host_writes_access(tree) + _version_assignments(tree)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name in _LOOP_ALLOWED
        ):
            continue
        found += _referenced(node, _HOST_ONLY)
    return found


def _host_bypasses(source: str) -> list[str]:
    """Run writes TaskLoopHost makes without going through ``self.writes``."""

    return _referenced(_class(ast.parse(source), "TaskLoopHost"), _PROTOCOL_CALLS)


def test_the_loop_reaches_its_run_row_only_through_the_host():
    source = LOOP_SOURCE.read_text(encoding="utf-8")
    assert _loop_bypasses(source) == []
    defined = {
        node.name for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert _LOOP_ALLOWED <= defined, f"stale allowlist entries: {_LOOP_ALLOWED - defined}"


def test_the_host_writes_its_run_row_only_through_its_writes():
    assert _host_bypasses(HOST_SOURCE.read_text(encoding="utf-8")) == []


_ANCHOR = "    host.start()\n"


@pytest.mark.parametrize("statement", [
    "    await release_task_coordinator_claim(db, task_id=task_id, claim_token='t', claim_generation='g')\n",
    "    await durable_run.checkpoint_run(db, TASK_COORDINATOR_RUN_SPEC, key=task_id)\n",
    "    callback = stage_task_coordinator_wake\n",
    "    await _claim_is_current(claim)\n",
    "    asyncio.create_task(_heartbeat(claim, asyncio.Event()))\n",
    "    await task_run_host.fence_before_model(claim, None, None)\n",
    "    async def _nested(db):\n        return await _claim_held(db, claim)\n",
    "    await host.writes.release(None, claim_token='t', claim_generation='g')\n",
    "    state[\"version\"] = claim.state_version + 1\n",
])
def test_loop_guard_catches_a_direct_write_or_claim_check(statement):
    source = LOOP_SOURCE.read_text(encoding="utf-8")
    assert source.count(_ANCHOR) == 1
    mutated = source.replace(_ANCHOR, _ANCHOR + statement)
    assert _loop_bypasses(mutated), statement


@pytest.mark.parametrize("helper", [
    "async def _late_release(db, claim):\n"
    "    await release_task_coordinator_claim(db, task_id=claim.task_id,"
    " claim_token=claim.claim_token, claim_generation=claim.wake_generation)\n",
    "async def _peek(db, claim):\n"
    "    return await durable_run.holds_run_claim(db, TASK_COORDINATOR_RUN_SPEC, key=claim.task_id)\n",
    "def _bump(state):\n    state[\"version\"] += 1\n",
    "class _Sneaky:\n    async def go(self, host, db):\n        await host.writes.settle(db)\n",
    "_CALLBACK = checkpoint_task_coordinator_run\n",
])
def test_loop_guard_covers_every_definition_in_the_module(helper):
    source = LOOP_SOURCE.read_text(encoding="utf-8")
    assert _loop_bypasses(source + "\n\n" + helper), helper


@pytest.mark.parametrize("statement", [
    "        await durable_run.settle_run_wake(None, self.spec, key=self.key)\n",
    "        await release_task_coordinator_claim(None)\n",
])
def test_host_guard_catches_a_write_around_its_writes(statement):
    source = HOST_SOURCE.read_text(encoding="utf-8")
    anchor = '        """Stop the heartbeat and release this owner\'s claim (never a successor\'s)."""\n'
    assert source.count(anchor) == 1
    mutated = source.replace(anchor, anchor + statement)
    assert _host_bypasses(mutated), statement


def test_the_host_counts_failures_under_the_loops_key():
    assert task_loop_host.WAKE_FAILURES_KEY == worker._WAKE_FAILURES_KEY
    assert task_loop_host.RUNTIME_CHECKPOINT_KEY == worker.RUNTIME_CHECKPOINT_KEY


# --- the same host over either writes -----------------------------------------


def _generic_writes(key: str) -> DurableRunWrites:
    return DurableRunWrites(TASK_COORDINATOR_RUN_SPEC, key)


def _root_writes(key: str) -> DurableRunWrites:
    return worker._TaskCoordinatorRunWrites(key)


_WRITES = [
    pytest.param(_generic_writes, id="spec_writes"),
    pytest.param(_root_writes, id="task_coordinator_wrappers"),
]


async def _claimed_run(db) -> tuple[str, durable_run.DurableRunClaim]:
    run = TaskCoordinatorRun(
        id=generate_ulid(),
        task_id=generate_ulid(),
        entity_id=generate_ulid(),
        workspace_id=generate_ulid(),
        root_subscription_id=generate_ulid(),
        root_agent_id=generate_ulid(),
        selection_fingerprint="task-loop-host",
        lifecycle_generation="lifecycle-1",
        engine=TaskCoordinatorEngine.AGENT_LOOP.value,
        phase=TaskCoordinatorPhase.READY.value,
        state_version=0,
    )
    db.add(run)
    await db.flush()
    wake = await durable_run.stage_run_wake(db, TASK_COORDINATOR_RUN_SPEC, key=run.task_id, reason="test")
    claim, reason = await durable_run.claim_run_wake(
        db, TASK_COORDINATOR_RUN_SPEC,
        key=run.task_id,
        event_id=wake.event_id,
        wake_generation=wake.wake_generation,
        claim_token=generate_ulid(),
        lease_seconds=120.0,
    )
    assert reason == "claimed"
    await db.commit()
    return run.task_id, claim


def _host(db_session, writes: DurableRunWrites, claim, *, slice_seconds: float = 3600.0) -> TaskLoopHost:
    async def heartbeat(_claim, lost: asyncio.Event) -> None:
        await lost.wait()

    return TaskLoopHost(
        claim=claim,
        writes=writes,
        task_id=writes.key,
        state=task_loop_initial_state(claim.state_version),
        session_factory=async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False),
        is_current=AsyncMock(return_value=True),
        heartbeat=heartbeat,
        slice_seconds=lambda: slice_seconds,
        label="Task agent loop",
    )


_ROW_FIELDS = ("phase", "checkpoint", "state_version", "claim_token", "claim_generation", "wake_generation")


async def _row(db, key: str) -> SimpleNamespace:
    """The run's committed fields (read before the rollback expires the row)."""

    row = await durable_run.lock_run(db, TASK_COORDINATOR_RUN_SPEC, key)
    fields = SimpleNamespace(**{name: getattr(row, name) for name in _ROW_FIELDS})
    await db.rollback()
    return fields


def _round_state(text: str) -> dict:
    return RuntimeRunState(messages=[{"role": "user", "content": text}]).snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("make_writes", _WRITES)
async def test_round_checkpoint_then_restage_hands_the_run_to_its_next_wake(db_session, make_writes):
    key, claim = await _claimed_run(db_session)
    host = _host(db_session, make_writes(key), claim, slice_seconds=0)
    host.start()
    try:
        await host.checkpoint(1, [], {}, [], _round_state("round one"))
        row = await _row(db_session, key)
        assert row.phase == TaskCoordinatorPhase.RUNNING.value
        assert row.checkpoint["messages"][0]["content"] == "round one"
        assert row.state_version == host._state["version"]
        # A zero slice yields at the first round boundary.
        assert host._state["yield"] is True and host.cancel_requested()

        assert await host.restage(
            reason_code="task_agent_loop_yield", delay_seconds=0, checkpoint=_round_state("final"),
        )
        row = await _row(db_session, key)
        assert row.checkpoint["messages"][0]["content"] == "final"
        assert row.claim_token is None and row.claim_generation is None
        assert row.wake_generation and row.wake_generation != claim.wake_generation
        assert row.state_version == host._state["version"] + 2  # stage, then release

        # The released owner can no longer checkpoint: the loop is stopped.
        with pytest.raises(RuntimeError, match="lost its claim"):
            await host.checkpoint(2, [], {}, [], _round_state("stale"))
        assert host.lost.is_set()
    finally:
        await host.close()
    assert (await _row(db_session, key)).wake_generation == row.wake_generation


@pytest.mark.asyncio
@pytest.mark.parametrize("make_writes", _WRITES)
async def test_crashes_count_per_wake_and_close_releases_only_this_owner(db_session, make_writes):
    key, claim = await _claimed_run(db_session)
    host = _host(db_session, make_writes(key), claim)
    host.start()
    try:
        assert (await host.record_wake_failure(ValueError("one")))[0] == 1
        count, checkpoint = await host.record_wake_failure(ValueError("two"))
        assert count == 2
        assert checkpoint[task_loop_host.WAKE_FAILURES_KEY]["wake_generation"] == claim.wake_generation
        async with host.session_factory() as db:
            assert await host.holds_claim(db)
            await db.rollback()
    finally:
        await host.close()
    row = await _row(db_session, key)
    assert row.claim_token is None
    assert row.wake_generation == claim.wake_generation, "close never settles the wake"
    # A superseded owner counts nothing.
    assert await host.record_wake_failure(ValueError("late")) == (0, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("make_writes", _WRITES)
async def test_a_failed_fence_stops_provider_io(db_session, make_writes):
    key, claim = await _claimed_run(db_session)
    host = _host(db_session, make_writes(key), claim)
    host._is_current = AsyncMock(return_value=False)
    with pytest.raises(RuntimeError, match="claim changed before model I/O"):
        await host.before_model()
    assert host.lost.is_set() and host.cancel_requested()
    await host.close()
