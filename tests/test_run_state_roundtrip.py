"""RuntimeRunState: every field and every registered slot survives park/resume.

A checkpoint used to be rebuilt from a hand-written key list; state left off
the list vanished at the next resume (and a retry kept its own list). These
guards make that a test failure: a field must declare how it is checkpointed
and what a retry does with it, every persisted field and slot must round-trip
through JSON, a retry's disposition covers exactly the persisted state, and
the Task loop's live state may only hold registered slots or explicitly
ephemeral keys.
"""
from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest

from packages.core.ai.runtime.run_state import (
    LOADED_GATEWAY_TOOLS_LIMIT,
    PERSIST,
    RETRY_CARRY,
    RETRY_DISPOSITIONS,
    RETRY_PER_SLOT,
    RETRY_RESET,
    RUN_STATE_KINDS,
    RUN_STATE_VERSION_KEY,
    RuntimeRunState,
    RuntimeRunStateSlot,
    RuntimeRunStateSlots,
    RuntimeRunStateVersionError,
    runtime_run_state_field_kinds,
    runtime_run_state_persistable,
    runtime_run_state_retry_dispositions,
)


_ROOT = Path(__file__).resolve().parents[1]


def _through_json(payload: dict) -> dict:
    return json.loads(json.dumps(payload))


# A non-default value per persisted field; a new field needs one here.
_FIELD_SAMPLES = {
    "messages": [
        {"role": "system", "content": "old prompt"},
        {"role": "user", "content": "brief"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "search_tools"}]},
    ],
    "usage": {"prompt": 12, "completion": 3, "total": 15},
    "rounds": 4,
    "tool_calls_made": ["search_tools", "generate_file"],
    "tool_evidence": {
        "observations": [{"call_id": "c1", "tool": "search_tools"}],
        "capability_hints": {
            "version": 1,
            "scope": {"conversation_id": "conv"},
            "hints": [{"name": "generate_file", "source": "loaded", "observed_at": "2026-10-06T00:00:00+00:00"}],
        },
    },
    "web_source_slices": [{"url": "https://example.com", "content": "text", "offset": 0}],
    "final_response_evidence": {"claims": ["x"]},
    "loaded_gateway_tools": ["generate_file", "mcp__crm__update_deal"],
    "host_slots": {"plan": {"items": ["a"]}},
}


def test_every_field_declares_how_it_is_checkpointed():
    kinds = runtime_run_state_field_kinds()
    undeclared = {name for name, kind in kinds.items() if kind not in RUN_STATE_KINDS}
    assert not undeclared, f"RuntimeRunState fields without metadata['run_state']: {undeclared}"


def test_every_persisted_field_declares_what_a_retry_does_with_it():
    dispositions = runtime_run_state_retry_dispositions()
    undeclared = {
        name for name, disposition in dispositions.items()
        if disposition not in RETRY_DISPOSITIONS | {RETRY_PER_SLOT}
    }
    assert not undeclared, f"persisted fields without metadata['on_retry']: {undeclared}"
    assert [name for name, value in dispositions.items() if value == RETRY_PER_SLOT] == ["host_slots"]


def test_every_persisted_field_has_a_round_trip_sample():
    persisted = {name for name, kind in runtime_run_state_field_kinds().items() if kind == PERSIST}
    assert persisted == set(_FIELD_SAMPLES)


@pytest.mark.parametrize("name", sorted(_FIELD_SAMPLES))
def test_each_persisted_field_round_trips_through_json(name):
    slots = RuntimeRunStateSlots([RuntimeRunStateSlot("plan", RETRY_CARRY)], live={})
    state = dataclasses.replace(RuntimeRunState(), **{name: _FIELD_SAMPLES[name]})
    assert state != RuntimeRunState(), "the sample must differ from the default"

    restored = RuntimeRunState.restore(_through_json(state.snapshot()), slots)

    assert getattr(restored, name) == _FIELD_SAMPLES[name]
    assert restored == state


def test_an_undeclared_field_cannot_be_snapshotted():
    @dataclasses.dataclass
    class Drifted(RuntimeRunState):
        posted_work: str = ""

    with pytest.raises(TypeError, match="posted_work"):
        Drifted(posted_work="digest").snapshot()


def test_ephemeral_loop_signals_are_never_persisted():
    state = RuntimeRunState(rounds=1, loop_signals={"terminal_failure": "tool_outcome_unconfirmed"})
    assert "terminal_failure" not in state.snapshot() and "loop_signals" not in state.snapshot()
    observed = {**state.snapshot(), "terminal_failure": "tool_outcome_unconfirmed"}
    assert runtime_run_state_persistable(observed) == state.snapshot()


def test_restore_accepts_a_flat_checkpoint_written_before_run_state():
    legacy = {
        "messages": [{"role": "user", "content": "brief"}],
        "usage": {"total": 3},
        "rounds": 2,
        "tool_calls_made": ["record_note"],
        "tool_evidence": {"observations": [], "capability_hints": {"hints": [
            # Only a discovery load is a restore candidate; a call is not.
            {"name": "generate_file", "source": "loaded"},
            {"name": "forged_tool", "source": "used"},
        ]}},
        "non_submit_retries": 1,
        "task_plan": {"items": []},
        # Host park state stays the host's.
        "runtime_checkpoint": {"pending_tool_call": {"id": "c", "name": "x"}},
    }
    slots = RuntimeRunStateSlots([
        RuntimeRunStateSlot("task_plan", RETRY_CARRY), RuntimeRunStateSlot("non_submit_retries", RETRY_RESET),
    ], live={})

    state = RuntimeRunState.restore(legacy, slots)

    assert state.messages == legacy["messages"] and state.rounds == 2
    assert state.tool_calls_made == ["record_note"]
    assert state.loaded_gateway_tools == ["generate_file"]
    assert state.host_slots == {"task_plan": {"items": []}, "non_submit_retries": 1}
    assert "runtime_checkpoint" not in state.snapshot()


def test_loaded_gateway_tools_are_bounded_most_recent_last():
    names = [f"tool_{index}" for index in range(LOADED_GATEWAY_TOOLS_LIMIT + 5)] + ["tool_0"]
    state = RuntimeRunState.restore({"loaded_gateway_tools": names})
    assert len(state.loaded_gateway_tools) == LOADED_GATEWAY_TOOLS_LIMIT
    assert state.loaded_gateway_tools[-1] == "tool_0", "a reload is the most recent"
    assert "tool_1" not in state.loaded_gateway_tools
    # The persisted list wins over hints: a hint is never a restore reason.
    assert RuntimeRunState.restore({
        "loaded_gateway_tools": [],
        "tool_evidence": {"capability_hints": {"hints": [{"name": "x", "source": "loaded"}]}},
    }).loaded_gateway_tools == []


def test_malformed_values_restore_as_empty_and_newer_versions_are_refused():
    state = RuntimeRunState.restore({
        "messages": "x", "usage": [], "rounds": "many", "tool_calls_made": "a",
        "tool_evidence": None, "web_source_slices": [1, {"url": "u"}], "loaded_gateway_tools": [1, "", "ok"],
    })
    assert state == RuntimeRunState(web_source_slices=[{"url": "u"}], loaded_gateway_tools=["ok"])
    with pytest.raises(RuntimeRunStateVersionError):
        RuntimeRunState.restore({RUN_STATE_VERSION_KEY: RuntimeRunState.VERSION + 1})


def test_slot_names_cannot_shadow_fields_repeat_or_skip_a_retry_disposition():
    for name in ("messages", "tool_evidence", "loaded_gateway_tools", "terminal_failure", RUN_STATE_VERSION_KEY, ""):
        with pytest.raises(ValueError):
            RuntimeRunStateSlots([RuntimeRunStateSlot(name, RETRY_CARRY)], live={})
    with pytest.raises(ValueError):
        RuntimeRunStateSlots([RuntimeRunStateSlot("a", RETRY_CARRY), RuntimeRunStateSlot("a", RETRY_RESET)], live={})
    with pytest.raises(ValueError):
        RuntimeRunStateSlots([
            RuntimeRunStateSlot("a", RETRY_CARRY, host_key="x"), RuntimeRunStateSlot("b", RETRY_CARRY, host_key="x"),
        ], live={})
    with pytest.raises(ValueError, match="retry"):
        RuntimeRunStateSlots([RuntimeRunStateSlot("a", "maybe")], live={})


def test_slots_capture_live_values_and_load_them_back():
    live = {"submission": {"outcome": "completed"}, "retries": 2}
    slots = RuntimeRunStateSlots([
        RuntimeRunStateSlot("task_submission", RETRY_RESET, host_key="submission"),
        RuntimeRunStateSlot("retries", RETRY_RESET, encode=int, decode=lambda raw: int(raw or 0), default=lambda: 0),
        RuntimeRunStateSlot("unset", RETRY_CARRY),
    ], live=live)

    payload = _through_json(RuntimeRunState(rounds=1).snapshot(slots))

    assert payload["task_submission"] == {"outcome": "completed"} and payload["retries"] == 2
    assert "unset" not in payload
    fresh: dict = {}
    fresh_slots = RuntimeRunStateSlots(list(slots), live=fresh)
    fresh_slots.load(RuntimeRunState.restore(payload, fresh_slots).host_slots)
    assert fresh == {"submission": {"outcome": "completed"}, "retries": 2, "unset": None}


def test_a_retry_carries_and_resets_exactly_what_is_declared():
    slots = RuntimeRunStateSlots([
        RuntimeRunStateSlot("plan", RETRY_CARRY), RuntimeRunStateSlot("submission", RETRY_RESET),
    ], live={})
    stopped = RuntimeRunState(
        **{name: value for name, value in _FIELD_SAMPLES.items() if name != "host_slots"},
        host_slots={"plan": {"items": ["a"]}, "submission": {"outcome": "completed"}},
    )

    retried = stopped.for_retry(slots)
    fresh = stopped.for_retry(slots, fresh_transcript=True)

    dispositions = runtime_run_state_retry_dispositions()
    for name, disposition in dispositions.items():
        if disposition == RETRY_CARRY:
            assert getattr(retried, name) == getattr(stopped, name), name
            assert getattr(fresh, name) == getattr(RuntimeRunState(), name), name
        elif disposition == RETRY_RESET:
            assert getattr(retried, name) == getattr(RuntimeRunState(), name), name
    assert retried.host_slots == fresh.host_slots == {"plan": {"items": ["a"]}}


@pytest.mark.asyncio
async def test_round_observer_receives_the_snapshot_with_registered_slots():
    from packages.core.ai.runtime.harness import runtime_run_state_round_observer

    live = {"plan": {"items": ["draft"]}}
    slots = RuntimeRunStateSlots([RuntimeRunStateSlot("plan", RETRY_CARRY)], live=live)
    seen = []

    async def host(rounds, messages, usage, tool_calls, runtime_state):
        seen.append(runtime_state)

    observer = runtime_run_state_round_observer(host, run_state_slots=slots)
    loop_snapshot = RuntimeRunState(
        messages=[{"role": "user", "content": "brief"}], rounds=1, tool_calls_made=["a"],
        tool_evidence={"observations": []}, loaded_gateway_tools=["generate_file"],
    ).snapshot()
    loop_snapshot["terminal_failure"] = "tool_outcome_unconfirmed"
    loop_snapshot["undeclared"] = "dropped"
    await observer(1, loop_snapshot["messages"], {"total": 5}, ["a"], loop_snapshot)

    snapshot = seen[0]
    assert snapshot["plan"] == {"items": ["draft"]}
    assert snapshot["terminal_failure"] == "tool_outcome_unconfirmed", "declared loop signals reach the host"
    assert "undeclared" not in snapshot
    assert snapshot["usage"] == {"total": 5} and snapshot["tool_evidence"] == {"observations": []}
    assert snapshot["messages"] is loop_snapshot["messages"], "the loop's private copy is not copied again"
    persisted = runtime_run_state_persistable(snapshot)
    assert "terminal_failure" not in persisted
    restored = RuntimeRunState.restore(_through_json(persisted), slots)
    assert restored.host_slots == {"plan": {"items": ["draft"]}}
    assert restored.loaded_gateway_tools == ["generate_file"]


@pytest.mark.asyncio
async def test_round_observer_builds_the_snapshot_for_a_loop_without_one():
    from packages.core.ai.runtime.harness import runtime_run_state_round_observer

    seen = []

    async def host(rounds, messages, usage, tool_calls, runtime_state):
        seen.append(runtime_state)

    observer = runtime_run_state_round_observer(host)
    await observer(2, [{"role": "user", "content": "x"}], {"total": 1}, ["a"], {
        "tool_evidence": {"observations": [{"call_id": "c"}]}, "terminal_failure": "tool_outcome_unconfirmed",
    })

    assert seen[0][RUN_STATE_VERSION_KEY] == RuntimeRunState.VERSION
    assert seen[0]["rounds"] == 2 and seen[0]["tool_evidence"] == {"observations": [{"call_id": "c"}]}
    assert seen[0]["terminal_failure"] == "tool_outcome_unconfirmed"


# ── The Task agent loop's registered slots ────────────────────────────


def _task_loop_state():
    from packages.core.services import task_agent_loop_state

    return task_agent_loop_state


def _task_loop_samples() -> dict:
    """A non-default live value for each Task loop slot, by stored name."""
    from packages.core.workers.task_agent_loop_completion import InspectionTracker

    return {
        "task_plan": {"revision": 2, "items": [{"title": "Draft", "status": "done"}]},
        "task_submission": {"outcome": "completed", "summary": "done"},
        "task_completion": {"files": ["Reports/a.md"], "outcome": "completed"},
        "inspection": InspectionTracker({"seq": 3, "receipts_seen": 1, "written": {"doc:1": 1}, "inspected": {"doc:1": 2}}),
        "unconfirmed_refusal": ["call-1"],
        "posted_work_digest": "digest-1",
        "non_submit_retries": 2,
        "delegation_mode": "durable",
        # Cloud-only slot; absent from the OSS build's registry.
        "capability_acquisitions": {"req-1": {"state": "applied"}},
    }


def test_task_loop_live_state_holds_only_registered_slots_and_declared_ephemera():
    """A key the loop keeps between rounds is either checkpointed or declared ephemeral."""
    module = _task_loop_state()
    state = module.task_loop_initial_state(1)
    slots = module.task_loop_run_state_slots(state)

    unregistered = set(state) - slots.live_keys - module.TASK_LOOP_EPHEMERAL_STATE_KEYS
    assert not unregistered, f"Task loop state that would be lost at park/resume: {unregistered}"
    assert slots.live_keys <= set(state), "every slot starts with an initial value"
    assert not slots.live_keys & module.TASK_LOOP_EPHEMERAL_STATE_KEYS


def _state_writes(path: Path) -> set[str]:
    """String keys assigned into the loop's live state (``state[...] = ...``)."""
    from packages.core.services import task_agent_loop_state

    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = {
        name: value for name, value in vars(task_agent_loop_state).items()
        if name.isupper() and isinstance(value, str)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                constants[node.targets[0].id] = node.value.value
            elif isinstance(node.value, ast.Name) and node.value.id in constants:
                constants[node.targets[0].id] = constants[node.value.id]
    keys: set[str] = set()
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AugAssign) else []
        for target in targets:
            if not isinstance(target, ast.Subscript):
                continue
            owner = target.value
            if not (
                (isinstance(owner, ast.Name) and owner.id == "state")
                or (isinstance(owner, ast.Attribute) and owner.attr == "_state")
            ):
                continue
            key = target.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
            elif isinstance(key, ast.Name):
                assert key.id in constants, f"{path.name}: state[{key.id}] is not a known key constant"
                keys.add(constants[key.id])
            else:
                raise AssertionError(f"{path.name}:{target.lineno}: state written with a computed key")
    return keys


def test_every_state_key_the_loop_writes_is_a_slot_or_ephemeral():
    """Scan the loop's source, not just its initializer: no write escapes the registry."""
    module = _task_loop_state()
    slots = module.task_loop_run_state_slots(module.task_loop_initial_state(1))
    allowed = slots.live_keys | module.TASK_LOOP_EPHEMERAL_STATE_KEYS
    sources = [
        _ROOT / "packages/core/workers/task_agent_loop.py",
        _ROOT / "packages/core/workers/task_loop_host.py",
        _ROOT / "packages/core/workers/task_loop_capabilities.py",
    ]
    written = set().union(*(_state_writes(path) for path in sources if path.exists()))
    assert written, "the scan found the loop's state writes"
    assert written <= allowed, f"state keys neither a slot nor ephemeral: {written - allowed}"


def test_each_task_loop_slot_round_trips_through_json():
    module = _task_loop_state()
    samples = _task_loop_samples()
    state = module.task_loop_initial_state(1)
    slots = module.task_loop_run_state_slots(state)
    assert set(slots.names) <= set(samples), "a new slot needs a round-trip sample"
    assert set(samples) - set(slots.names) <= {"capability_acquisitions"}
    live_key = {slot.name: slot.live_key for slot in slots}
    for name in slots.names:
        state[live_key[name]] = samples[name]

    payload = _through_json(RuntimeRunState(messages=[{"role": "user", "content": "brief"}]).snapshot(slots))

    fresh = module.task_loop_initial_state(7)
    fresh_slots = module.task_loop_run_state_slots(fresh)
    fresh_slots.load(RuntimeRunState.restore(payload, fresh_slots).host_slots)
    for slot in slots:
        assert slot.encode(fresh[slot.live_key]) == slot.encode(samples[slot.name]), slot.name
    assert fresh["version"] == 7, "ephemeral state is never restored"


def test_retry_dispositions_cover_exactly_the_persisted_state():
    """carried ∪ reset == persisted fields ∪ Task loop slots, and they never overlap."""
    module = _task_loop_state()
    slots = module.task_loop_run_state_slots(module.task_loop_initial_state(1))
    dispositions = runtime_run_state_retry_dispositions()
    carried = {name for name, value in dispositions.items() if value == RETRY_CARRY} | slots.names_on_retry(RETRY_CARRY)
    reset = {name for name, value in dispositions.items() if value == RETRY_RESET} | slots.names_on_retry(RETRY_RESET)
    persisted = {name for name, value in dispositions.items() if value != RETRY_PER_SLOT} | set(slots.names)

    assert carried | reset == persisted
    assert not carried & reset


def test_a_retried_checkpoint_is_derived_from_the_declarations():
    from packages.core.services.task_coordinator_wakeup import AGENT_LOOP_RETRY_KEY, agent_loop_retry_checkpoint

    stopped = {
        **RuntimeRunState(
            **{name: value for name, value in _FIELD_SAMPLES.items() if name not in {"host_slots", "messages"}},
            messages=[{"role": "user", "content": "brief"}],
        ).snapshot(),
        "task_plan": {"items": []}, "task_submission": {"outcome": "completed"},
        "posted_work_digest": "d", "non_submit_retries": 2, "inspection": {"seq": 1},
        "coordinator_outcome": {"error": "agent_loop_max_rounds"}, "runtime_checkpoint": {"x": 1},
        "wake_failures": {"count": 1},
    }

    retried = agent_loop_retry_checkpoint(stopped)

    for name in ("usage", "tool_calls_made", "tool_evidence", "web_source_slices",
                 "final_response_evidence", "loaded_gateway_tools", "task_plan", "posted_work_digest"):
        assert retried[name] == stopped[name], name
    assert retried["rounds"] == 0 and retried[RUN_STATE_VERSION_KEY] == RuntimeRunState.VERSION
    for dropped in ("task_submission", "non_submit_retries", "inspection", "coordinator_outcome",
                    "runtime_checkpoint", "wake_failures"):
        assert dropped not in retried, dropped
    assert retried[AGENT_LOOP_RETRY_KEY]["after"] == "agent_loop_max_rounds"

    crashed = {**stopped, "coordinator_outcome": {"error": "agent_loop_repair_required", "details": {}}}
    fresh = agent_loop_retry_checkpoint(crashed)
    assert fresh["messages"] == [] and fresh["tool_evidence"] == {} and fresh["loaded_gateway_tools"] == []
    assert fresh["task_plan"] == {"items": []} and fresh[AGENT_LOOP_RETRY_KEY]["fresh_transcript"] is True


def test_task_loop_loads_a_checkpoint_written_before_run_state():
    """In-flight runs carry the old flat keys; they load into the same live state."""
    module = _task_loop_state()
    legacy = {
        "messages": [{"role": "user", "content": "brief"}],
        "usage": {}, "rounds": 3, "tool_calls_made": ["record_note"], "tool_evidence": {},
        "non_submit_retries": 1,
        "task_plan": {"items": []},
        "task_submission": {"outcome": "completed"},
        "inspection": {"seq": 2},
        "unconfirmed_refusal": ["c1", 2],
        "posted_work_digest": "d",
        "task_loop_wait": {"kind": "waiting_timer"},
    }
    state = module.task_loop_initial_state(1)
    slots = module.task_loop_run_state_slots(state)

    run_state = RuntimeRunState.restore(legacy, slots)
    slots.load(run_state.host_slots)

    assert run_state.rounds == 3 and run_state.messages == legacy["messages"]
    assert state["task_plan"] == {"items": []} and state["submission"] == {"outcome": "completed"}
    assert state["completion"] is None and state["inspection"].seq == 2
    assert state["unconfirmed_refusal"] == ["c1", "2"]
    assert state["posted_work_digest"] == "d" and state["non_submit_retries"] == 1
    assert state["resumed_wait"] is None, "park state is the host's, not a slot"


def test_a_newer_runtimes_checkpoint_is_transient_at_the_wake():
    """A rolling deploy's older worker redelivers the wake instead of counting a failure."""
    from packages.core.workers import task_agent_loop as worker

    assert worker._is_transient_error(RuntimeRunStateVersionError(2))
    assert not worker._is_transient_error(ValueError("other"))


def test_a_retry_of_a_newer_runtimes_run_says_so(caplog):
    """No 500 and no rewrite in an older shape: a clear, retryable answer."""
    from packages.core.ai.runtime.run_state import RUN_STATE_NEWER_RUNTIME_DETAIL, runtime_run_state_is_newer
    from packages.core.services.task_coordinator_wakeup import agent_loop_retry_checkpoint

    newer = {RUN_STATE_VERSION_KEY: RuntimeRunState.VERSION + 1, "messages": [{"role": "user", "content": "x"}]}
    assert runtime_run_state_is_newer(newer)
    assert not runtime_run_state_is_newer({"messages": []})
    with caplog.at_level("WARNING"), pytest.raises(RuntimeRunStateVersionError) as raised:
        agent_loop_retry_checkpoint(newer)
    assert str(raised.value) == RUN_STATE_NEWER_RUNTIME_DETAIL and "newer version of Manor" in str(raised.value)
    assert "saved by a newer runtime" in caplog.text


# ── the delegated child run's registry (services.task_agent_child_state) ──


def _child_state():
    from packages.core.services import task_agent_child_state

    return task_agent_child_state


def _child_samples() -> dict:
    return {
        "answer_revised": True,
        "tool_outcomes": [{"name": "web_search", "result": {"status": "succeeded"}}],
    }


def test_child_live_state_holds_only_registered_slots_and_declared_ephemera():
    module = _child_state()
    state = module.task_agent_child_initial_state(1)
    slots = module.task_agent_child_run_state_slots(state)

    unregistered = set(state) - slots.live_keys - module.TASK_AGENT_CHILD_EPHEMERAL_STATE_KEYS
    assert not unregistered, f"child state that would be lost at park/resume: {unregistered}"
    assert slots.live_keys <= set(state)
    assert not slots.live_keys & module.TASK_AGENT_CHILD_EPHEMERAL_STATE_KEYS


def test_every_state_key_the_child_host_writes_is_a_slot_or_ephemeral():
    module = _child_state()
    slots = module.task_agent_child_run_state_slots(module.task_agent_child_initial_state(1))
    allowed = slots.live_keys | module.TASK_AGENT_CHILD_EPHEMERAL_STATE_KEYS
    sources = [
        _ROOT / "packages/core/workers/task_agent_child_loop.py",
        _ROOT / "packages/core/workers/task_loop_host.py",
        _ROOT / "packages/core/services/task_agent_child_state.py",
    ]
    written = set().union(*(_state_writes(path) for path in sources))
    assert {"answer_revised", "tool_outcomes"} <= written, "the scan sees the child's writes"
    assert written <= allowed, f"state keys neither a slot nor ephemeral: {written - allowed}"


def test_each_child_slot_round_trips_through_json():
    module = _child_state()
    samples = _child_samples()
    state = module.task_agent_child_initial_state(1)
    slots = module.task_agent_child_run_state_slots(state)
    assert set(slots.names) == set(samples), "a new child slot needs a round-trip sample"
    for slot in slots:
        state[slot.live_key] = samples[slot.name]

    payload = _through_json(RuntimeRunState(messages=[{"role": "user", "content": "task"}]).snapshot(slots))

    fresh = module.task_agent_child_initial_state(9)
    fresh_slots = module.task_agent_child_run_state_slots(fresh)
    fresh_slots.load(RuntimeRunState.restore(payload, fresh_slots).host_slots)
    for slot in slots:
        assert slot.encode(fresh[slot.live_key]) == slot.encode(samples[slot.name]), slot.name
    assert fresh["version"] == 9, "ephemeral state is never restored"


def test_child_retry_dispositions_cover_exactly_its_persisted_state():
    module = _child_state()
    slots = module.task_agent_child_run_state_slots(module.task_agent_child_initial_state(1))
    dispositions = runtime_run_state_retry_dispositions()
    carried = {name for name, value in dispositions.items() if value == RETRY_CARRY} | slots.names_on_retry(RETRY_CARRY)
    reset = {name for name, value in dispositions.items() if value == RETRY_RESET} | slots.names_on_retry(RETRY_RESET)
    persisted = {name for name, value in dispositions.items() if value != RETRY_PER_SLOT} | set(slots.names)
    assert carried | reset == persisted and not carried & reset


def test_child_tool_outcomes_are_bounded_newest_kept():
    module = _child_state()
    state = module.task_agent_child_initial_state(1)
    for index in range(module.TOOL_OUTCOMES_MAX + 5):
        module.record_tool_outcome(state, {"name": f"t{index}", "result": {"status": "succeeded"}})
    assert len(state["tool_outcomes"]) == module.TOOL_OUTCOMES_MAX
    assert state["tool_outcomes"][-1]["name"] == f"t{module.TOOL_OUTCOMES_MAX + 4}"
