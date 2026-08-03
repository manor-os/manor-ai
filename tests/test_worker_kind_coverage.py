"""Guard: every step kind the dispatcher may lease has a worker handler.

Regression origin: ``registry.DEFAULT_INTERNAL_CAPABILITIES`` advertised
``human`` from 2026-04-26, but ``InternalWorker._execute_by_kind`` only
learned to run it on 2026-06-11. The dispatcher matches leases on
``supported_kinds``, so every human step leased in that window died with
``NotImplementedError: InternalWorker doesn't handle kind='human'``,
burned all 3 attempts, and left its dependents skipped.

These tests fail if an advertised kind loses its handler, if a schema kind
goes unrouted, or if a kind is left pending with no worker able to take it.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from packages.core.plans.schema import StepKind
from packages.core.workers.internal import (
    _KIND_HANDLERS,
    INTERNAL_WORKER_SUPPORTED_KINDS,
    _execute_by_kind,
    _NeedsHumanInput,
)
from packages.core.workers.registry import (
    DEFAULT_INTERNAL_CAPABILITIES,
    _internal_supported_kinds,
)

SCHEMA_KINDS = set(StepKind.__args__)

# Kinds PlanExecutor resolves itself and never leaves for the dispatcher.
INLINE_KINDS = {"sleep", "human"}
# Kinds deliberately unimplemented; the executor must fail them loudly.
OUT_OF_SCOPE_KINDS = {"parallel_fanout", "gather", "code"}


def _executor_source() -> str:
    return Path("packages/core/plans/executor.py").read_text(encoding="utf-8")


def _executor_kind_tuple(anchor: str) -> set[str]:
    """Extract the kind tuple of the executor branch whose body has ``anchor``."""
    source = _executor_source()
    branches = list(re.finditer(r"elif step\.kind in \(([^)]*)\):", source))
    assert branches, "no 'elif step.kind in (...)' branches found in executor"
    for index, branch in enumerate(branches):
        end = (
            branches[index + 1].start()
            if index + 1 < len(branches)
            else len(source)
        )
        body = source[branch.end():end]
        if anchor in body:
            return set(re.findall(r'"([a-z_]+)"', branch.group(1)))
    raise AssertionError(f"could not locate executor branch containing {anchor!r}")


def test_advertised_kinds_match_implemented_handlers():
    """The advertised set must be exactly what the worker can run."""
    assert set(DEFAULT_INTERNAL_CAPABILITIES["supported_kinds"]) == set(
        INTERNAL_WORKER_SUPPORTED_KINDS
    )
    assert set(_internal_supported_kinds()) == set(_KIND_HANDLERS)


def test_every_advertised_kind_has_a_callable_handler():
    for kind in DEFAULT_INTERNAL_CAPABILITIES["supported_kinds"]:
        handler = _KIND_HANDLERS.get(kind)
        assert handler is not None, (
            f"kind {kind!r} is advertised to the dispatcher but has no handler; "
            "the dispatcher will lease it and the worker will raise "
            "NotImplementedError"
        )
        assert inspect.iscoroutinefunction(handler)


def test_every_schema_kind_is_accounted_for():
    """No plan-legal kind may fall through unrouted."""
    routed = set(_KIND_HANDLERS) | OUT_OF_SCOPE_KINDS
    unaccounted = SCHEMA_KINDS - routed
    assert not unaccounted, (
        f"step kinds {sorted(unaccounted)} are legal in PlanStep but neither "
        "implemented nor explicitly out of scope"
    )


def test_dispatched_kinds_are_all_advertised():
    """A kind left pending for the dispatcher must be leasable by a worker.

    ``code`` used to sit in this branch while no worker advertised it, so
    such plans hung pending forever instead of reporting an error.
    """
    dispatched = _executor_kind_tuple("Dispatcher will pick this up")
    assert dispatched, "executor dispatch branch not found"
    not_leasable = dispatched - set(DEFAULT_INTERNAL_CAPABILITIES["supported_kinds"])
    assert not not_leasable, (
        f"executor leaves {sorted(not_leasable)} pending for the dispatcher, but "
        "no internal worker advertises those kinds — the steps would never run"
    )


def test_out_of_scope_kinds_fail_loudly_in_executor():
    failed_loudly = _executor_kind_tuple("not in Demo A v0 scope")
    for kind in OUT_OF_SCOPE_KINDS:
        assert kind in failed_loudly, (
            f"kind {kind!r} is unimplemented but the executor does not fail it "
            "explicitly — the plan would stall instead of surfacing an error"
        )


@pytest.mark.asyncio
async def test_human_kind_pauses_instead_of_raising_not_implemented():
    """The exact 2026-06-05 failure: a leased human step must not blow up."""
    snapshot = {
        "kind": "human",
        "params": {"prompt": "Connect your video publishing channel"},
    }
    with pytest.raises(_NeedsHumanInput) as excinfo:
        await _execute_by_kind(snapshot)
    assert "Connect your video publishing channel" in excinfo.value.prompt


@pytest.mark.asyncio
async def test_human_kind_completes_once_a_response_is_supplied():
    snapshot = {
        "kind": "human",
        "params": {"human_input_response": {"channel_platform": "youtube"}},
    }
    envelope = await _execute_by_kind(snapshot)
    assert envelope["result"] == {"channel_platform": "youtube"}


@pytest.mark.asyncio
async def test_sleep_kind_is_runnable_when_leased():
    envelope = await _execute_by_kind({"kind": "sleep", "params": {"seconds": 5}})
    assert envelope["result"] == {"slept": 5}


@pytest.mark.asyncio
async def test_unknown_kind_still_raises_a_named_error():
    with pytest.raises(NotImplementedError, match="kind='mystery'"):
        await _execute_by_kind({"kind": "mystery", "params": {}})
