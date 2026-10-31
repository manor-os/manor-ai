"""Partial transcript on cancel (Section B3).

A run cancelled by its deadline (or torn down by an exception raised inside
the loop) used to report no transcript at all: the checkpoint was only built
AFTER the loop returned. The agentic loop now fires ``on_round_end`` at the
end of every round, and the runtime harness forwards it with the cumulative
round state so a caller can keep the latest snapshot.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime import harness


# ── the agentic loop hook ─────────────────────────────────────────────


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}


def _scripted_rounds(*rounds):
    """Fake the model: each entry is ``(content, tool_calls)``."""
    script = list(rounds)

    async def fake_round(*_args, **_kwargs):
        content, tool_calls = script.pop(0)
        return content, tool_calls, {}

    return fake_round


@pytest.mark.asyncio
async def test_on_round_end_fires_after_tool_results_and_before_the_final_return(monkeypatch):
    seen: list[tuple[int, list[dict]]] = []

    def hook(rounds: int, messages: list[dict]) -> None:
        seen.append((rounds, [dict(m) for m in messages]))

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", _scripted_rounds(
        ("", [{"id": "c1", "name": "web_search", "arguments": {"q": "x"}},
              {"id": "c2", "name": "web_search", "arguments": {"q": "y"}}]),
        ("all done", []),
    ))

    async def tool_executor(name, arguments):
        return json.dumps({"ok": True, "q": arguments["q"]})

    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="do the work", tools=[_tool("web_search")],
        tool_executor=tool_executor, max_rounds=5, on_round_end=hook,
    )

    assert result.stop_reason == "completed"
    assert [rounds for rounds, _ in seen] == [1, 2]
    first_round = seen[0][1]
    # Both tool results of the round are already in the transcript.
    assert [m["role"] for m in first_round] == ["system", "user", "assistant", "tool", "tool"]
    assert {m["tool_call_id"] for m in first_round if m["role"] == "tool"} == {"c1", "c2"}
    # The final assistant message is included in the last call.
    assert seen[1][1][-1] == {"role": "assistant", "content": "all done"}
    assert len(seen[1][1]) == len(result.messages)


@pytest.mark.asyncio
async def test_on_round_end_may_be_awaitable(monkeypatch):
    seen: list[int] = []

    async def hook(rounds: int, messages: list[dict]) -> None:
        await asyncio.sleep(0)
        seen.append(rounds)

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", _scripted_rounds(("done", [])))

    async def tool_executor(name, arguments):
        return "{}"

    await loop_module.agentic_loop(
        system_prompt="s", user_message="u", tools=[_tool("t")], tool_executor=tool_executor,
        max_rounds=3, on_round_end=hook,
    )

    assert seen == [1]


@pytest.mark.asyncio
async def test_transcript_seed_keeps_round_usage_and_tool_counts_cumulative(monkeypatch):
    observed = []

    async def fake_round(*_args, **_kwargs):
        return "done", [], {"prompt_tokens": 2, "completion_tokens": 1}

    def checkpoint(rounds, messages, usage, tool_calls_made):
        observed.append((rounds, messages, usage, tool_calls_made))

    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        fake_round,
    )

    result = await loop_module.agentic_loop(
        system_prompt="s",
        user_message="continue",
        tools=[_tool("t")],
        tool_executor=lambda *_args, **_kwargs: None,
        max_rounds=3,
        initial_messages=[
            {"role": "user", "content": "original"},
            {"role": "assistant", "content": "worked"},
        ],
        initial_rounds=2,
        initial_usage={"prompt_tokens": 10, "completion_tokens": 4},
        initial_tool_calls_made=["web_search"],
        on_round_end=checkpoint,
    )

    assert result.rounds == 3
    assert result.usage["prompt_tokens"] == 12
    assert result.usage["completion_tokens"] == 5
    assert result.tool_calls_made == ["web_search"]
    assert observed[-1][0] == 3
    assert observed[-1][2]["prompt_tokens"] == 12
    assert observed[-1][3] == ["web_search"]


@pytest.mark.asyncio
async def test_resume_after_saved_max_round_summary_does_not_buy_another_call(monkeypatch):
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("a saved final summary must be reused")

    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        unexpected,
    )
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_final_completion",
        unexpected,
    )

    result = await loop_module.agentic_loop(
        system_prompt="s",
        user_message="continue",
        tools=[_tool("t")],
        tool_executor=unexpected,
        max_rounds=3,
        initial_messages=[
            {"role": "user", "content": "original"},
            {"role": "assistant", "content": "saved final summary"},
        ],
        initial_rounds=4,
        initial_usage={"prompt_tokens": 10, "completion_tokens": 4},
    )

    assert result.stop_reason == "max_rounds"
    assert result.content == "saved final summary"
    assert result.rounds == 4


@pytest.mark.asyncio
async def test_exact_forced_resume_runs_at_the_saved_round_limit(monkeypatch):
    async def unexpected_model_call(*_args, **_kwargs):
        raise AssertionError("a terminal forced continuation needs no model call")

    executed: list[tuple[str, dict]] = []

    async def tool_executor(name, arguments):
        public_arguments = dict(arguments)
        assert public_arguments.pop("_runtime_tool_call_id_from_context")
        executed.append((name, public_arguments))
        return json.dumps({"ok": True})

    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        unexpected_model_call,
    )
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_final_completion",
        unexpected_model_call,
    )

    result = await loop_module.agentic_loop(
        system_prompt="s",
        user_message="continue approved child",
        tools=[_tool("resume_exact")],
        tool_executor=tool_executor,
        max_rounds=3,
        initial_messages=[
            {"role": "user", "content": "original"},
            {"role": "assistant", "content": "waiting for approval"},
        ],
        initial_rounds=3,
        forced_tool_calls=[{"name": "resume_exact", "arguments": {"child": "c1"}}],
        terminal_tool_result_policy={
            "terminal_tool_results": [{
                "tool_names": ["resume_exact"],
                "stop_reason": "submit_result",
                "notice": "approved child completed",
            }],
        },
    )

    assert executed == [("resume_exact", {"child": "c1"})]
    assert result.stop_reason == "submit_result"
    assert result.rounds == 4


@pytest.mark.asyncio
async def test_forced_resume_cannot_cross_an_already_saved_final_summary(monkeypatch):
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("a finalized transcript must not execute again")

    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        unexpected,
    )
    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_final_completion",
        unexpected,
    )

    result = await loop_module.agentic_loop(
        system_prompt="s",
        user_message="continue",
        tools=[_tool("resume_exact")],
        tool_executor=unexpected,
        max_rounds=3,
        initial_messages=[
            {"role": "user", "content": "original"},
            {"role": "assistant", "content": "saved final summary"},
        ],
        initial_rounds=4,
        forced_tool_calls=[{"name": "resume_exact", "arguments": {}}],
    )

    assert result.stop_reason == "max_rounds"
    assert result.content == "saved final summary"
    assert result.rounds == 4


@pytest.mark.asyncio
async def test_a_failing_on_round_end_never_breaks_the_loop(monkeypatch, caplog):
    def hook(rounds: int, messages: list[dict]) -> None:
        raise RuntimeError("observer bug")

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", _scripted_rounds(
        ("", [{"id": "c1", "name": "t", "arguments": {}}]),
        ("done", []),
    ))

    async def tool_executor(name, arguments):
        return "{}"

    result = await loop_module.agentic_loop(
        system_prompt="s", user_message="u", tools=[_tool("t")], tool_executor=tool_executor,
        max_rounds=3, on_round_end=hook,
    )

    assert result.stop_reason == "completed" and result.content == "done"


@pytest.mark.asyncio
async def test_a_strict_round_checkpoint_failure_stops_before_another_round(monkeypatch):
    model_calls = 0

    async def fake_round(*_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        return "", [{"id": "c1", "name": "t", "arguments": {}}], {}

    async def tool_executor(_name, _arguments):
        return json.dumps({"ok": True, "id": "external-effect-1"})

    async def failing_checkpoint(_rounds, _messages):
        raise RuntimeError("checkpoint database unavailable")

    monkeypatch.setattr(
        loop_module,
        "runtime_execute_agentic_round_tool_completion",
        fake_round,
    )

    with pytest.raises(RuntimeError, match="checkpoint database unavailable"):
        await loop_module.agentic_loop(
            system_prompt="s",
            user_message="u",
            tools=[_tool("t")],
            tool_executor=tool_executor,
            max_rounds=3,
            on_round_end=failing_checkpoint,
            on_round_end_strict=True,
        )

    assert model_calls == 1, "no later round may repeat work after checkpoint failure"


@pytest.mark.asyncio
async def test_on_round_end_must_be_callable():
    async def tool_executor(name, arguments):
        return "{}"

    with pytest.raises(TypeError, match="on_round_end"):
        await loop_module.agentic_loop(
            system_prompt="s", user_message="u", tools=[], tool_executor=tool_executor,
            on_round_end="not callable",
        )


# ── the harness forwards it ───────────────────────────────────────────


def test_runtime_agentic_loop_forwards_on_round_end():
    captured: dict = {}
    observed: list[tuple] = []

    async def fake_route(**_kwargs):
        return "test-model", {}, None

    async def fake_agentic_loop(**kwargs):
        captured.update(kwargs)
        from packages.core.ai.runtime.nested_usage import runtime_record_nested_usage

        runtime_record_nested_usage({"prompt_tokens": 3, "completion_tokens": 2})
        await kwargs["on_round_end"](
            3,
            [{"role": "assistant", "content": "checkpoint"}],
            {"prompt_tokens": 4, "completion_tokens": 1},
            ["web_search"],
        )
        return SimpleNamespace(usage={}, rounds=0, tool_calls_made=[])

    def hook(rounds, messages, usage, tool_calls_made):
        observed.append((rounds, messages, usage, tool_calls_made))

    with (
        patch("packages.core.ai.runtime.completions.runtime_resolve_text_completion_route", new=fake_route),
        patch("packages.core.ai.runtime.billing.runtime_ensure_billing_context", new=lambda *a, **k: None),
        patch("packages.core.ai.agentic_loop.agentic_loop", new=fake_agentic_loop),
    ):
        asyncio.run(harness.runtime_execute_agentic_loop(
            runtime_envelope=None, system_prompt="system", user_message="go", tools=[],
            entity_id="entity", agent_id=None, on_round_end=hook,
            on_round_end_strict=True,
            initial_usage={"prompt_tokens": 4},
            initial_rounds=2,
            initial_tool_calls_made=["web_search"],
        ))

    assert callable(captured["on_round_end"])
    assert captured["on_round_end_strict"] is True
    assert captured["initial_usage"] == {"prompt_tokens": 4}
    assert captured["initial_rounds"] == 2
    assert captured["initial_tool_calls_made"] == ["web_search"]
    assert observed[0][0] == 3
    assert observed[0][2]["prompt_tokens"] == 7
    assert observed[0][2]["completion_tokens"] == 3
    assert observed[0][3] == ["web_search"]


def test_runtime_agentic_loop_forwards_terminal_round_state():
    observed: list[dict] = []

    async def fake_route(**_kwargs):
        return "test-model", {}, None

    async def fake_agentic_loop(**kwargs):
        await kwargs["on_round_end"](
            1, [{"role": "assistant", "content": "error"}], {}, [],
            {"terminal_failure": "tool_outcome_unconfirmed"},
        )
        return SimpleNamespace(usage={}, rounds=1, tool_calls_made=[])

    def hook(_rounds, _messages, _usage, _calls, runtime_state):
        observed.append(runtime_state)

    with (
        patch("packages.core.ai.runtime.completions.runtime_resolve_text_completion_route", new=fake_route),
        patch("packages.core.ai.runtime.billing.runtime_ensure_billing_context", new=lambda *a, **k: None),
        patch("packages.core.ai.agentic_loop.agentic_loop", new=fake_agentic_loop),
    ):
        asyncio.run(harness.runtime_execute_agentic_loop(
            runtime_envelope=None, system_prompt="system", user_message="go", tools=[],
            entity_id="entity", agent_id=None, on_round_end=hook,
        ))

    # The declared loop signal reaches the observer beside the run state.
    from packages.core.ai.runtime.run_state import RUN_STATE_VERSION_KEY

    assert len(observed) == 1
    assert observed[0]["terminal_failure"] == "tool_outcome_unconfirmed"
    assert observed[0][RUN_STATE_VERSION_KEY] and observed[0]["rounds"] == 1
