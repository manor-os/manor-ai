"""StepResult envelope part ②: forced submit_result finalization.

The subagent loop carries a submit_result tool; calling it terminates the
loop and its payload IS the step result. If the model ends without
submitting, one follow-up round (submit_result as the only tool) forces the
hand-off; only if that also yields nothing do the legacy text-coercion
heuristics apply. These tests pin all three tiers plus the tool contract.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import packages.core.workers.internal as internal
from packages.core.workers.submit_result import (
    SUBMIT_RESULT_TERMINAL_POLICY,
    SUBMIT_RESULT_TOOL_NAME,
    build_submit_result_tool,
    step_result_from_submit,
    submit_result_capture,
    submit_result_followup_message,
)


# ── tool contract ──────────────────────────────────────────────────


def test_tool_schema_embeds_expected_output_schema_without_required():
    tool = build_submit_result_tool({
        "type": "object",
        "properties": {"post_url": {"type": "string"}, "post_text": {"type": "string"}},
        "required": ["post_url"],
    })
    assert tool["type"] == "function"
    fn = tool["function"]
    assert fn["name"] == SUBMIT_RESULT_TOOL_NAME
    params = fn["parameters"]
    assert params["required"] == ["summary"]
    result_schema = params["properties"]["result"]
    assert set(result_schema["properties"]) == {"post_url", "post_text"}
    # The plan's `required` must NOT leak in — schema strictness never turns
    # finished work into a failure (envelope part ① invariant).
    assert "required" not in result_schema


def test_tool_schema_without_expected_schema_is_free_object():
    tool = build_submit_result_tool(None)
    assert tool["function"]["parameters"]["properties"]["result"] == {"type": "object"}


def test_terminal_policy_matches_the_tool_name():
    rules = SUBMIT_RESULT_TERMINAL_POLICY["terminal_tool_results"]
    assert rules[0]["tool_names"] == [SUBMIT_RESULT_TOOL_NAME]


def test_capture_handler_never_errors_and_returns_ack():
    handler, get_payload = submit_result_capture()
    assert get_payload() is None
    ack = json.loads(handler({"summary": "did the thing", "result": {"a": 1}}))
    assert ack["ok"] is True
    assert get_payload() == {"summary": "did the thing", "result": {"a": 1}}
    # malformed args still captured as empty payload, never raise
    assert json.loads(handler("not-a-dict"))["ok"] is True
    assert get_payload() == {}


def test_step_result_from_submit_normalizes():
    out = step_result_from_submit({
        "summary": "posted it",
        "status": "done",
        "result": {"post_url": "https://x.com/p/1"},
    })
    assert out["post_url"] == "https://x.com/p/1"
    assert out["text"] == "posted it"
    assert out["summary"] == "posted it"
    # The agent's "done" is normalized onto the canonical enum here. Keeping it
    # as the raw word is what let the envelope's unknown-status branch infer
    # "partial" downstream — a finished step rendered as unfinished.
    assert out["status"] == "succeeded"

    # summary-only payload degrades to a text result
    out = step_result_from_submit({"summary": "blocked on login"})
    assert out["text"] == "blocked on login"

    # a submitted text field wins over the summary
    out = step_result_from_submit({
        "summary": "sum", "result": {"text": "the deliverable"},
    })
    assert out["text"] == "the deliverable"


def test_followup_message_lists_schema_fields():
    msg = submit_result_followup_message({
        "type": "object", "properties": {"b": {}, "a": {}},
    })
    assert "submit_result" in msg
    assert "a, b" in msg


# ── _exec_subagent integration (loop mocked) ───────────────────────


def _fake_ctx():
    return SimpleNamespace(
        system_prompt="sys",
        runtime_envelope=None,
        tools=[{"type": "function", "function": {"name": "existing_tool", "parameters": {}}}],
        tool_profile="profile",
        allowed_tool_names=["existing_tool"],
        model="model-x",
        llm_metadata=None,
    )


def _loop_result(**overrides):
    base = dict(
        content="final prose",
        messages=[],
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        rounds=2,
        tool_calls_made=[],
        stop_reason="completed",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _step(schema=None):
    return {
        "params": {"prompt": "publish the post"},
        "entity_id": "ent",
        "resolved_agent_id": "agent",
        "user_id": "user",
        "workspace_id": "ws",
        "conversation_id": "conv",
        "task_id": "task",
        "expected_output_schema": schema,
    }


def _wire_common(monkeypatch):
    async def fake_build_agent_context(*args, **kwargs):
        return _fake_ctx()

    class _FakeSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("packages.core.ai.context.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("packages.core.database.async_session", lambda: _FakeSession())

    async def fake_persist(_envelope):
        return None

    monkeypatch.setattr(internal, "runtime_persist_internal_worker_runtime_events", fake_persist)
    monkeypatch.setattr(internal, "runtime_metadata_from_context", lambda ctx: {})


def test_submit_payload_becomes_the_step_result(monkeypatch):
    """Tier 1: the model called submit_result — its payload is the result and
    no fallback round runs."""
    _wire_common(monkeypatch)
    captured_kwargs: dict = {}

    async def fake_worker_loop(**kwargs):
        captured_kwargs.update(kwargs)
        # simulate the model calling submit_result mid-loop
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({
            "summary": "posted", "status": "done",
            "result": {"post_url": "https://x.com/p/9"},
        })
        return SimpleNamespace(result=_loop_result(), run=None)

    async def fail_fallback(**kwargs):  # must not be called
        raise AssertionError("fallback round must not run when payload captured")

    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    monkeypatch.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fail_fallback)

    out = asyncio.run(internal._exec_subagent(_step(
        {"type": "object", "properties": {"post_url": {"type": "string"}}},
    )))

    assert out["result"]["post_url"] == "https://x.com/p/9"
    assert out["result"]["status"] == "succeeded"
    # the loop was armed correctly
    tool_names = [t["function"]["name"] for t in captured_kwargs["tools"]]
    assert SUBMIT_RESULT_TOOL_NAME in tool_names and "existing_tool" in tool_names
    assert SUBMIT_RESULT_TOOL_NAME in captured_kwargs["allowed_tool_names"]
    assert captured_kwargs["terminal_tool_result_policy"] is SUBMIT_RESULT_TERMINAL_POLICY
    assert "submit_result" in captured_kwargs["user_message"]


def test_missing_submit_triggers_forced_round(monkeypatch):
    """Tier 2: loop ended without submitting — one follow-up round with
    submit_result as the only tool captures the hand-off."""
    _wire_common(monkeypatch)
    fallback_kwargs: dict = {}

    async def fake_worker_loop(**kwargs):
        return SimpleNamespace(result=_loop_result(), run=None)

    async def fake_fallback(**kwargs):
        fallback_kwargs.update(kwargs)
        kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME]({
            "summary": "recovered", "result": {"post_url": "https://x.com/p/10"},
        })
        return _loop_result(rounds=1, usage={"prompt_tokens": 3, "completion_tokens": 2})

    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    monkeypatch.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fake_fallback)

    out = asyncio.run(internal._exec_subagent(_step(
        {"type": "object", "properties": {"post_url": {"type": "string"}}},
    )))

    assert out["result"]["post_url"] == "https://x.com/p/10"
    # follow-up round constrained to the single tool, one round, nudge appended
    assert [t["function"]["name"] for t in fallback_kwargs["tools"]] == [SUBMIT_RESULT_TOOL_NAME]
    assert fallback_kwargs["max_rounds"] == 1
    assert fallback_kwargs["allowed_tool_names"] == [SUBMIT_RESULT_TOOL_NAME]
    assert fallback_kwargs["initial_messages"][-1]["role"] == "user"
    assert "submit_result" in fallback_kwargs["initial_messages"][-1]["content"]
    # fallback usage rolled into cost
    assert out["cost"]["llm_tokens_input"] == 13
    assert out["cost"]["llm_tokens_output"] == 7
    assert out["cost"]["llm_rounds"] == 3


def test_no_submit_anywhere_falls_back_to_text_coercion(monkeypatch):
    """Tier 3: even the forced round produced nothing — legacy behavior."""
    _wire_common(monkeypatch)

    async def fake_worker_loop(**kwargs):
        return SimpleNamespace(result=_loop_result(content="plain answer"), run=None)

    async def fake_fallback(**kwargs):
        return _loop_result(rounds=1)  # no handler call

    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    monkeypatch.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fake_fallback)

    out = asyncio.run(internal._exec_subagent(_step(None)))
    assert out["result"] == {"text": "plain answer"}


def test_fallback_round_failure_is_swallowed(monkeypatch):
    """A crashing forced round must never fail the step."""
    _wire_common(monkeypatch)

    async def fake_worker_loop(**kwargs):
        return SimpleNamespace(result=_loop_result(content="prose"), run=None)

    async def exploding_fallback(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    monkeypatch.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", exploding_fallback)

    out = asyncio.run(internal._exec_subagent(_step(None)))
    assert out["result"] == {"text": "prose"}


# ── the submit_result terminator is a SUCCESS, not a failure ───────
#
# The loop stops on the submit_result tool call with
# stop_reason="submit_result" — that is the mechanism working as designed.
# Treating it as a failed loop made every *compliant* subagent step fail.


def _submitted_loop_result(**overrides):
    """What the loop really returns when submit_result terminated it."""
    rule = SUBMIT_RESULT_TERMINAL_POLICY["terminal_tool_results"][0]
    base = dict(
        content=rule["notice"],
        stop_reason=rule["stop_reason"],
        control={
            "terminal": True,
            "content": rule["notice"],
            "stop_reason": rule["stop_reason"],
            "replace_visible_text": False,
        },
    )
    base.update(overrides)
    return _loop_result(**base)


def _run_with_loop(monkeypatch, loop_result, *, submit=None, schema=None):
    async def fake_worker_loop(**kwargs):
        if submit is not None:
            kwargs["dynamic_tool_handlers"][SUBMIT_RESULT_TOOL_NAME](submit)
        return SimpleNamespace(result=loop_result, run=None)

    async def fail_fallback(**kwargs):
        raise AssertionError("fallback round must not run when payload captured")

    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fail_fallback
    )
    return asyncio.run(internal._exec_subagent(_step(schema)))


def test_submit_result_stop_reason_is_not_a_failure(monkeypatch):
    """stop_reason="submit_result" + a captured payload → the step succeeds."""
    _wire_common(monkeypatch)

    out = _run_with_loop(
        monkeypatch,
        _submitted_loop_result(),
        submit={"summary": "posted", "status": "done",
                "result": {"post_url": "https://x.com/p/11"}},
        schema={"type": "object", "properties": {"post_url": {"type": "string"}}},
    )

    assert out["result"]["post_url"] == "https://x.com/p/11"


def test_submit_result_step_carries_payload_not_the_terminal_notice(monkeypatch):
    """The deliverable is the submitted payload; "Result submitted." is
    bookkeeping and must never become the step's content."""
    _wire_common(monkeypatch)

    out = _run_with_loop(
        monkeypatch,
        _submitted_loop_result(),
        submit={"summary": "wrote the brief", "status": "done",
                "result": {"text": "the actual deliverable", "headline": "H"}},
    )

    assert out["result"]["text"] == "the actual deliverable"
    assert out["result"]["headline"] == "H"
    assert out["result"]["status"] == "succeeded"
    assert "Result submitted." not in json.dumps(out["result"])


def test_captured_payload_survives_any_non_success_stop_reason(monkeypatch):
    """Defense in depth: a captured deliverable is never discarded, whatever
    the loop's bookkeeping says about how it stopped."""
    _wire_common(monkeypatch)

    out = _run_with_loop(
        monkeypatch,
        _loop_result(content="", stop_reason="error", error="provider blip"),
        submit={"summary": "done anyway", "result": {"post_url": "https://x.com/p/12"}},
    )

    assert out["result"]["post_url"] == "https://x.com/p/12"


@pytest.mark.parametrize("stop_reason", ["error", "credit_exhausted"])
def test_genuinely_failed_loop_still_raises(monkeypatch, stop_reason):
    """Regression guard: no content, no payload → still a hard failure."""
    _wire_common(monkeypatch)

    async def fake_worker_loop(**kwargs):
        return SimpleNamespace(
            result=_loop_result(content="", stop_reason=stop_reason, error="boom"),
            run=None,
        )

    async def fake_fallback(**kwargs):
        return _loop_result(rounds=1)

    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fake_fallback
    )

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(internal._exec_subagent(_step(None)))
    assert stop_reason in str(excinfo.value)


def test_max_rounds_with_content_still_succeeds(monkeypatch):
    """Existing behavior preserved: a rounds-capped loop that produced text
    is not a failure."""
    _wire_common(monkeypatch)

    async def fake_worker_loop(**kwargs):
        return SimpleNamespace(
            result=_loop_result(content="ran out of rounds but here it is",
                                stop_reason="max_rounds"),
            run=None,
        )

    async def fake_fallback(**kwargs):
        return _loop_result(rounds=1)

    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", fake_worker_loop)
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_execute_worker_subagent_followup", fake_fallback
    )

    out = asyncio.run(internal._exec_subagent(_step(None)))
    assert out["result"] == {"text": "ran out of rounds but here it is"}


def test_terminal_success_stop_reasons_are_registry_driven():
    """The success terminator is declared once and shared, not re-spelled at
    every call site."""
    from packages.core.ai.terminal_stops import (
        is_terminal_success_stop_reason,
        is_terminal_tool_success,
    )
    from packages.core.workers.submit_result import SUBMIT_RESULT_STOP_REASON

    assert SUBMIT_RESULT_TERMINAL_POLICY["terminal_tool_results"][0]["stop_reason"] == (
        SUBMIT_RESULT_STOP_REASON
    )
    assert is_terminal_success_stop_reason(SUBMIT_RESULT_STOP_REASON)
    assert is_terminal_success_stop_reason("skill_terminal")
    assert is_terminal_success_stop_reason("media_generation_tool_result")
    assert is_terminal_success_stop_reason("local_coding_dispatched")
    assert not is_terminal_success_stop_reason("error")
    assert not is_terminal_success_stop_reason("max_rounds")

    # Any policy-driven terminal control counts, even a skill-configured
    # stop_reason nobody registered.
    assert is_terminal_tool_success(
        SimpleNamespace(stop_reason="custom_skill_stop", control={"terminal": True})
    )
    assert not is_terminal_tool_success(
        SimpleNamespace(stop_reason="error", control=None)
    )
