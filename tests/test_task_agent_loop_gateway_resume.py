"""R2-6: a tool search_tools loaded stays callable after the Task loop parks.

The loop runs through the real Runtime harness, the real agentic loop, the
real Task wake/claim/checkpoint storage and (for the approval park) the real
approval service. Each wake builds a fresh agent context and envelope, as in
production. Faked: the model round, the discovery backend, and the registry
executor for the discovered tool, which authorizes exactly as the runtime
policy does (the envelope's allowlist plus its discovered grants).

Before RuntimeRunState the harness never got the loaded tools back on resume:
``call_tool(generate_file)`` was refused with "'generate_file' is not loaded
for this request" after every park. The loaded set is recorded from real
discovery results, with its own bound, so neither a long run nor a forged
call can change what a resume re-authorizes.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import test_task_agent_loop as base
import test_task_agent_loop_hitl as hitl
from packages.core.ai.runtime import harness as runtime_harness
from packages.core.ai.runtime import tool_registry
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.policies import check_runtime_tool_policy
from packages.core.ai.runtime.tool_gateway import call_tool_schema
from packages.core.models.task import Task
from packages.core.models.task_coordinator import TaskCoordinatorRun
from packages.core.workers import task_agent_loop as worker


# A declared tool, so the outcome of a refused call does not depend on how
# the effect contract classifies an unknown one.
CHART = "generate_file"
CHART_SCHEMA = {
    "type": "function",
    "function": {
        "name": CHART,
        "description": "Create a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"}, "name": {"type": "string"}, "content": {"type": "string"},
            },
            "required": ["kind"],
        },
    },
}


def _baseline_tool(name: str) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": "Record a step.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
    }}
SEARCH_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_tools",
        "description": "Find and load tools.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}


def _install_gateway_run(monkeypatch, host, *, baseline: tuple[str, ...] = (), on_tool=None):
    """Real harness, a fresh envelope per wake, faked discovery and file tool."""

    envelopes: list[RuntimeEnvelope] = []
    discoveries: list[dict] = []
    chart_runs: list[dict] = []
    loop_calls: list[dict] = []
    template = host.envelope

    async def build_context(_db, **kwargs):
        envelope = RuntimeEnvelope(
            surface=template.surface, profile=template.profile, principal=template.principal,
            entity_id=template.entity_id, user_id=template.user_id, agent_id=template.agent_id,
            workspace_id=template.workspace_id, conversation_id=kwargs["conversation_id"],
            task_id=template.task_id,
            tool_names=("search_tools", "call_tool", hitl.TWEET, *baseline),
            allowed_tool_names=("search_tools", "call_tool", hitl.TWEET, *baseline),
            blocked_tool_names=("delegate_to_agent", "acquire_capabilities"),
        )
        envelopes.append(envelope)
        return SimpleNamespace(
            tools=[SEARCH_TOOLS_SCHEMA, call_tool_schema(), hitl.TWEET_SCHEMA, *map(_baseline_tool, baseline)],
            allowed_tool_names={"search_tools", "call_tool", hitl.TWEET, *baseline},
            runtime_envelope=envelope, system_prompt="AGENT PROMPT", tool_profile=None,
            model=None, llm_metadata={},
        )

    async def discover(*, entity_id, user_id, arguments):
        envelope = arguments["_runtime_envelope_from_context"]
        discoveries.append({"mode": arguments.get("mode"), "envelope": envelope,
                            "exact_names": arguments.get("exact_names")})
        # As the real discovery: loading grants the tool on this run's envelope.
        envelope.discovered_tool_grants.grant([CHART])
        return json.dumps({
            "mode": arguments.get("mode") or "search",
            "loaded_tools": [CHART],
            "matches": [{"name": CHART, "schema": CHART_SCHEMA}],
        })

    real_schema_for_actor = tool_registry.runtime_tool_schema_for_actor

    async def schema_for_actor(name, **kwargs):
        return CHART_SCHEMA if name == CHART else await real_schema_for_actor(name, **kwargs)

    async def execute_tool(name, arguments, *, runtime_envelope=None, entity_id=None, user_id=None, **_kwargs):
        if name == "search_tools":
            return await discover(entity_id=entity_id, user_id=user_id, arguments={
                **arguments, "_runtime_envelope_from_context": runtime_envelope,
            })
        if name == hitl.TWEET:
            return await host.extra_tools[hitl.TWEET](arguments)
        if name == "call_tool":
            return json.dumps({"error": "gateway_call_rejected", "message": arguments.get("rejected_reason")})
        decision = check_runtime_tool_policy(envelope=runtime_envelope, tool_name=name, arguments=arguments)
        if not decision.allowed:
            return decision.to_tool_result()
        if name == CHART:
            chart_runs.append({"envelope": runtime_envelope, "name": arguments.get("name")})
            index = len(chart_runs)
            return json.dumps({"ok": True, "document": {
                "document_id": f"doc-{index}", "fs_path": f"Reports/launch-{index}.md",
            }})
        if name in baseline:
            if on_tool is not None:
                on_tool(name, arguments)
            return json.dumps({"ok": True, "recorded": arguments.get("text")})
        raise AssertionError(f"unexpected tool {name}")

    real_harness = runtime_harness.runtime_execute_agentic_loop

    async def recorded_harness(**kwargs):
        loop_calls.append(kwargs)
        return await real_harness(**kwargs)

    host.context.side_effect = build_context
    monkeypatch.setattr(worker, "build_agent_context", host.context)
    monkeypatch.setattr(worker, "runtime_execute_agentic_loop", recorded_harness)
    monkeypatch.setattr(tool_registry, "runtime_discover_tools", discover)
    monkeypatch.setattr(tool_registry, "runtime_tool_schema_for_actor", schema_for_actor)
    monkeypatch.setattr(tool_registry, "runtime_execute_tool", execute_tool)
    return SimpleNamespace(
        envelopes=envelopes, discoveries=discoveries, chart_runs=chart_runs, loop_calls=loop_calls,
    )


def _chart_called(messages, _tools, call_id: str = "chart"):
    result = json.loads(hitl._result_of(messages, call_id))
    assert result.get("ok") is True and result["document"]["document_id"], result
    return "", [base._call("done", "complete_task", base.COMPLETE_ARGS)]


def _call_chart(call_id: str = "chart", name: str = "launch.md") -> tuple:
    return ("", [base._call(call_id, "call_tool", {
        "name": CHART, "arguments": {"kind": "md", "name": name, "content": "We launch on 2026-10-10."},
    })])


_SEARCH = ("", [base._call("search", "search_tools", {"query": "create a file"})])
_CALL_CHART = _call_chart()


async def _park_on_approval(db_session, monkeypatch, host, wake):
    executed = hitl._with_tweet(host)
    gateway = _install_gateway_run(monkeypatch, host)
    seen = base._model(monkeypatch, [
        _SEARCH,
        ("", [base._call("post", hitl.TWEET, {"text": "We launch on 2026-10-10"})]),
        _CALL_CHART,
        _chart_called,
    ])
    first = await hitl._run(db_session, host.task_id, wake)
    assert first == {"status": "waiting_human", "task_id": host.task_id, "kind": "waiting_approval"}, first
    run = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    await hitl._decide(db_session, host, run.checkpoint["task_loop_wait"]["hitl_request_id"], "approve")
    second = await hitl._run(db_session, host.task_id)
    assert len(executed) == 1, "the approved call ran once on resume"
    return gateway, seen, second


async def _park_on_wait_until(db_session, monkeypatch, host, wake):
    from packages.core.workers import task_agent_loop_waits as waits

    gateway = _install_gateway_run(monkeypatch, host)
    seen = base._model(monkeypatch, [
        _SEARCH,
        ("", [base._call("wait", "wait_until", {"seconds": 120, "reason": "the render queue"})]),
        _CALL_CHART,
        _chart_called,
    ])
    first = await hitl._run(db_session, host.task_id, wake)
    assert first["status"] == "waiting_timer", first
    monkeypatch.setattr(waits, "utcnow", lambda: datetime.now(timezone.utc) + timedelta(minutes=5))
    second, _row = await base._deliver(db_session, host.task_id)
    return gateway, seen, second


async def _yield_at_the_time_slice(db_session, monkeypatch, host, wake):
    gateway = _install_gateway_run(monkeypatch, host)
    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 0)
    seen = base._model(monkeypatch, [_SEARCH, _CALL_CHART, _chart_called])
    first = await hitl._run(db_session, host.task_id, wake)
    assert first == {"status": "yielded", "task_id": host.task_id}, first
    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 3600)
    second, _row = await base._deliver(db_session, host.task_id)
    return gateway, seen, second


@pytest.mark.asyncio
@pytest.mark.parametrize("park", [_park_on_approval, _park_on_wait_until, _yield_at_the_time_slice],
                         ids=["approval", "wait_until", "time_slice_yield"])
async def test_a_tool_loaded_before_a_park_is_callable_after_resume(db_session, monkeypatch, park):
    host, wake = await hitl._start(db_session, monkeypatch)

    gateway, seen, second = await park(db_session, monkeypatch, host, wake)

    assert second == {"status": "completed", "task_id": host.task_id}, second
    # The chart ran once, on the resumed wake's own (fresh) envelope.
    assert len(gateway.chart_runs) == 1
    assert len(gateway.envelopes) == 2
    resumed_envelope = gateway.envelopes[-1]
    assert gateway.chart_runs[0]["envelope"].discovered_tool_grants is resumed_envelope.discovered_tool_grants
    # The grant was rebuilt by exact discovery under that envelope, never
    # replayed from the checkpoint (which holds the name only).
    first_search, restore = gateway.discoveries
    assert first_search["envelope"].discovered_tool_grants is gateway.envelopes[0].discovered_tool_grants
    assert gateway.envelopes[0].discovered_tool_grants is not resumed_envelope.discovered_tool_grants
    assert restore["mode"] == "exact" and restore["exact_names"] == [CHART]
    assert restore["envelope"].discovered_tool_grants is resumed_envelope.discovered_tool_grants
    resumed = gateway.loop_calls[-1]
    assert resumed.get("run_state") is not None or resumed.get("runtime_checkpoint") is not None
    assert resumed["run_state_slots"] is not None
    # The model never saw a "not loaded" refusal.
    assert all("is not loaded for this request" not in str(m.get("content")) for m in seen[-1])
    assert (await base._fresh(db_session, Task, host.task_id)).status == "completed"


@pytest.mark.asyncio
async def test_a_tool_the_current_scope_revoked_is_not_restored(db_session, monkeypatch):
    """Restoring re-authorizes: exact discovery that refuses the name leaves it unloaded."""

    host, wake = await hitl._start(db_session, monkeypatch)
    gateway = _install_gateway_run(monkeypatch, host)
    granting = tool_registry.runtime_discover_tools

    async def revoked_after_first_wake(*, entity_id, user_id, arguments):
        if arguments.get("mode") == "exact":
            gateway.discoveries.append({"mode": "exact", "envelope": arguments["_runtime_envelope_from_context"],
                                        "exact_names": arguments.get("exact_names")})
            return json.dumps({"mode": "exact", "loaded_tools": [], "matches": []})
        return await granting(entity_id=entity_id, user_id=user_id, arguments=arguments)

    monkeypatch.setattr(tool_registry, "runtime_discover_tools", revoked_after_first_wake)
    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 0)

    def refused(messages, _tools):
        result = hitl._result_of(messages, "chart")
        assert "is not loaded for this request" in result, result
        return "", [base._call("done", "complete_task", base.COMPLETE_ARGS)]

    base._model(monkeypatch, [_SEARCH, _CALL_CHART, refused])
    assert (await hitl._run(db_session, host.task_id, wake))["status"] == "yielded"
    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 3600)
    second, row = await base._deliver(db_session, host.task_id)

    assert second == {"status": "completed", "task_id": host.task_id}, second
    assert gateway.chart_runs == []
    assert [item["mode"] for item in gateway.discoveries] == [None, "exact"]
    assert not gateway.envelopes[-1].discovered_tool_grants.allows(CHART)
    # The scope refused it: the run forgets the name.
    assert row.checkpoint["loaded_gateway_tools"] == []


@pytest.mark.asyncio
async def test_a_failed_restore_keeps_the_name_for_the_next_resume(db_session, monkeypatch):
    """Discovery that throws at one wake is not a refusal: the next wake restores the tool."""

    host, wake = await hitl._start(db_session, monkeypatch)
    gateway = _install_gateway_run(monkeypatch, host)
    granting = tool_registry.runtime_discover_tools
    outage = {"on": False}

    async def flaky(*, entity_id, user_id, arguments):
        if outage["on"] and arguments.get("mode") == "exact":
            raise ConnectionError("provider registry unavailable")
        return await granting(entity_id=entity_id, user_id=user_id, arguments=arguments)

    monkeypatch.setattr(tool_registry, "runtime_discover_tools", flaky)
    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 0)

    def refused_while_down(messages, _tools):
        assert "is not loaded for this request" in hitl._result_of(messages, "chart")
        return _call_chart("chart-2", "launch-2.md")

    base._model(monkeypatch, [
        _SEARCH,
        _CALL_CHART,
        refused_while_down,
        lambda messages, tools: _chart_called(messages, tools, "chart-2"),
    ])
    assert (await hitl._run(db_session, host.task_id, wake))["status"] == "yielded"
    outage["on"] = True
    outcome, row = await base._deliver(db_session, host.task_id)
    assert outcome["status"] == "yielded", outcome
    assert gateway.chart_runs == []
    assert row.checkpoint["loaded_gateway_tools"] == [CHART], "a failed restore keeps the name"

    outage["on"] = False
    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 3600)
    done, _row = await base._deliver(db_session, host.task_id)

    assert done == {"status": "completed", "task_id": host.task_id}, done
    assert len(gateway.chart_runs) == 1
    assert gateway.chart_runs[0]["envelope"].discovered_tool_grants is gateway.envelopes[-1].discovered_tool_grants


@pytest.mark.asyncio
async def test_a_time_slice_yield_keeps_every_web_source_of_the_run(db_session, monkeypatch):
    """Round snapshots bound web text; the yield persists the run's full final state."""
    import hashlib

    from packages.core.ai.runtime.artifacts import RUNTIME_ROUND_WEB_SOURCE_CHARS, runtime_record_web_source_slice

    pages = {url: (url[-1] * (RUNTIME_ROUND_WEB_SOURCE_CHARS * 3 // 4)) for url in (
        "https://example.com/a", "https://example.com/b",
    )}

    def on_tool(name, _arguments):
        for url, page in pages.items():
            runtime_record_web_source_slice(
                url=url, content=page, offset=0,
                source_sha256=hashlib.sha256(page.encode()).hexdigest(), complete_source=True,
            )

    host, wake = await hitl._start(db_session, monkeypatch)
    _install_gateway_run(monkeypatch, host, baseline=("note_fetch",), on_tool=on_tool)
    monkeypatch.setattr(worker, "TASK_AGENT_LOOP_SLICE_SECONDS", 0)
    base._model(monkeypatch, [("", [base._call("fetch", "note_fetch", {"text": "pages"})])])

    assert (await hitl._run(db_session, host.task_id, wake))["status"] == "yielded"

    row = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert [item["url"] for item in row.checkpoint["web_source_slices"]] == list(pages)


@pytest.mark.asyncio
async def test_a_loaded_tool_survives_a_long_run_of_other_calls_before_the_park(db_session, monkeypatch):
    """Chat's 12 capability hints rank used tools first; the loaded set does not.

    Load the tool, call it through call_tool, then call 13 other tools: the
    tool is still restored after the park, and once more after a second park.
    """
    from packages.core.workers import task_agent_loop_waits as waits

    baseline = tuple(f"note_{index}" for index in range(13))
    host, wake = await hitl._start(db_session, monkeypatch)
    gateway = _install_gateway_run(monkeypatch, host, baseline=baseline)

    def notes(names):
        return ("", [base._call(f"call-{name}", name, {"text": name}) for name in names])

    def first_call_ran(messages, _tools):
        assert json.loads(hitl._result_of(messages, "chart"))["ok"] is True
        return notes(baseline[:5])

    seen = base._model(monkeypatch, [
        _SEARCH,
        _CALL_CHART,
        first_call_ran,
        notes(baseline[5:10]),
        notes(baseline[10:]),
        ("", [base._call("wait", "wait_until", {"seconds": 120, "reason": "the render queue"})]),
        _call_chart("chart-2", "launch-2.md"),
        ("", [base._call("wait-2", "wait_until", {"seconds": 120, "reason": "the review"})]),
        _call_chart("chart-3", "launch-3.md"),
        lambda messages, tools: _chart_called(messages, tools, "chart-3"),
    ])
    assert (await hitl._run(db_session, host.task_id, wake))["status"] == "waiting_timer"
    row = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert row.checkpoint["runtime_checkpoint"]["loaded_gateway_tools"] == [CHART]
    hints = row.checkpoint["tool_evidence"]["capability_hints"]["hints"]
    assert CHART not in [hint["name"] for hint in hints], "the hint window alone would have lost it"

    clock = {"now": datetime.now(timezone.utc) + timedelta(minutes=5)}
    monkeypatch.setattr(waits, "utcnow", lambda: clock["now"])
    assert (await base._deliver(db_session, host.task_id))[0]["status"] == "waiting_timer"
    clock["now"] += timedelta(minutes=5)
    done, row = await base._deliver(db_session, host.task_id)

    assert done == {"status": "completed", "task_id": host.task_id}, done
    assert len(gateway.chart_runs) == 3
    assert [item["mode"] for item in gateway.discoveries] == [None, "exact", "exact"]
    assert all("is not loaded for this request" not in str(m.get("content")) for m in seen[-1])


@pytest.mark.asyncio
async def test_a_continuation_resumes_with_the_runs_web_sources_and_loaded_tools(db_session, monkeypatch):
    """The final run state comes from the harness: nothing a resume needs is rebuilt by hand."""
    import hashlib

    from packages.core.ai.runtime.artifacts import (
        runtime_current_web_source_slices,
        runtime_record_web_source_slice,
    )

    page = "Launch day is 2026-10-10."
    seen_sources: list[list[str]] = []

    def on_tool(name, _arguments):
        if name == "note_fetch":
            runtime_record_web_source_slice(
                url="https://example.com/launch", content=page, offset=0,
                source_sha256=hashlib.sha256(page.encode()).hexdigest(), complete_source=True,
            )
        else:
            seen_sources.append([item.url for item in runtime_current_web_source_slices()])

    host, wake = await hitl._start(db_session, monkeypatch)
    gateway = _install_gateway_run(monkeypatch, host, baseline=("note_fetch", "note_check"), on_tool=on_tool)
    seen = base._model(monkeypatch, [
        _SEARCH,
        ("", [base._call("fetch", "note_fetch", {"text": "page"})]),
        # Ends without complete_task: the host continues the transcript.
        ("The note is ready.", []),
        ("", [base._call("check", "note_check", {"text": "sources"})]),
        _CALL_CHART,
        _chart_called,
    ])
    assert (await hitl._run(db_session, host.task_id, wake))["status"] == "retry_scheduled"
    row = await base._fresh(db_session, TaskCoordinatorRun, host.run_id)
    assert [item["url"] for item in row.checkpoint["web_source_slices"]] == ["https://example.com/launch"]
    assert row.checkpoint["loaded_gateway_tools"] == [CHART]

    done, _row = await base._deliver(db_session, host.task_id)

    assert done == {"status": "completed", "task_id": host.task_id}, done
    assert gateway.loop_calls[-1]["run_state"] is not None
    assert seen_sources == [["https://example.com/launch"]]
    assert len(gateway.chart_runs) == 1 and len(seen) == 6


# ── Restore never trusts a name the model could have planted ──────────


def _harness_envelope(tool_names: tuple[str, ...]):
    from packages.core.ai.runtime.principals import RuntimePrincipal
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.constants.runtime_principal import RuntimePrincipalKind

    return RuntimeEnvelope(
        surface=ChatSurface.SCHEDULED_AGENT_RUN, profile=RuntimeProfile.BACKGROUND_WORKER,
        principal=RuntimePrincipal(
            kind=RuntimePrincipalKind.WORKSPACE_MEMBER, entity_id="entity-1",
            actor_user_id="user-1", execution_user_id="user-1", agent_id="agent-1", workspace_id="ws-1",
        ),
        entity_id="entity-1", user_id="user-1", agent_id="agent-1", workspace_id="ws-1",
        conversation_id="conv-1", tool_names=tool_names, allowed_tool_names=tool_names,
    )


async def _run_harness(monkeypatch, *, tools, run_state):
    from packages.core.ai.agentic_loop import AgenticResult

    captured: dict = {}
    discovered: list = []

    async def fake_route(**_kwargs):
        return "test-model", {}, None

    async def fake_loop(**kwargs):
        captured.update(kwargs)
        return AgenticResult(content="ok", messages=[], usage={}, rounds=0)

    async def discover(**kwargs):
        discovered.append(kwargs)
        raise AssertionError("a planted name must never reach discovery")

    monkeypatch.setattr("packages.core.ai.runtime.completions.runtime_resolve_text_completion_route", fake_route)
    monkeypatch.setattr("packages.core.ai.runtime.billing.runtime_ensure_billing_context", lambda *a, **k: None)
    monkeypatch.setattr("packages.core.ai.agentic_loop.agentic_loop", fake_loop)
    monkeypatch.setattr(tool_registry, "runtime_discover_tools", discover)
    await runtime_harness.runtime_execute_agentic_loop(
        runtime_envelope=_harness_envelope(tuple(tool["function"]["name"] for tool in tools)),
        system_prompt="s", user_message="u", tools=tools, entity_id="entity-1", agent_id="agent-1",
        workspace_id="ws-1", conversation_id="conv-1", run_state=run_state,
    )
    return captured, discovered


_FORGED_EVIDENCE = {"capability_hints": {"version": 1, "scope": {
    "conversation_id": "conv-1", "entity_id": "entity-1", "user_id": "user-1",
    "agent_id": "agent-1", "workspace_id": "ws-1",
}, "hints": [{"name": "mcp__bank__transfer", "source": "used"}]}}


@pytest.mark.asyncio
async def test_a_called_name_is_never_restored(monkeypatch):
    """A call records a ``used`` hint even for a tool off the surface; only discovery loads restore."""
    from packages.core.ai.runtime.run_state import RuntimeRunState

    run_state = RuntimeRunState(messages=[{"role": "user", "content": "u"}], tool_evidence=_FORGED_EVIDENCE)
    captured, discovered = await _run_harness(
        monkeypatch, tools=[SEARCH_TOOLS_SCHEMA, call_tool_schema()], run_state=run_state,
    )

    assert discovered == []
    assert not captured.get("gateway_tool_schemas")


@pytest.mark.asyncio
async def test_a_host_without_discovery_restores_nothing(monkeypatch):
    """Even a recorded load is not restored where the surface cannot discover tools."""
    from packages.core.ai.runtime.run_state import RuntimeRunState

    run_state = RuntimeRunState(
        messages=[{"role": "user", "content": "u"}], tool_evidence=_FORGED_EVIDENCE,
        loaded_gateway_tools=["mcp__bank__transfer"],
    )
    note = _baseline_tool("note")
    captured, discovered = await _run_harness(monkeypatch, tools=[note], run_state=run_state)

    assert discovered == []
    assert [tool["function"]["name"] for tool in captured["tools"]] == ["note"]
    assert not captured.get("gateway_tool_schemas")
