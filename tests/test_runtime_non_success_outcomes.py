"""An honest non-success under an output schema completes instead of failing."""

from __future__ import annotations

import inspect
import json

import pytest

import packages.core.ai.agentic_loop as loop_module

SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked", "needs_input"]},
        "reason": {"type": "string"},
    },
    "required": ["status", "reason"],
    "additionalProperties": False,
}
NON_SUCCESS = {"field": "status", "values": ["blocked", "needs_input"]}


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


async def _run(monkeypatch, final_text: str, *, non_success=NON_SUCCESS):
    """One unconfirmed tool error, then the model's own final reply."""
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    rounds = {"n": 0}

    async def fake_round(messages, tools, **_kwargs):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return "", [{"id": "call-1", "name": "ws_external_action", "arguments": {}}], {}
        # The error reaches the model with its tools; no tool-free summary.
        assert [tool["function"]["name"] for tool in tools] == ["ws_external_action"]
        assert any(
            item.get("role") == "tool"
            and item.get("tool_call_id") == "call-1"
            and "provider_outcome_unknown" in item.get("content", "")
            for item in messages
        )
        return final_text, None, {}

    executed: list[str] = []

    async def tool_executor(name, _args):
        executed.append(name)
        return json.dumps({"error": "provider_outcome_unknown"})

    text_rounds: list[str] = []

    async def text_round(messages, **_kwargs):
        # Only an output-schema repair may take a tool-free round.
        text_rounds.append(messages[-1]["content"])
        return final_text, {}

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", text_round)
    ledger = RuntimeToolEvidence()
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="do it",
        tools=[_tool("ws_external_action")], tool_executor=tool_executor,
        max_rounds=4, output_schema=SCHEMA, non_success_outcomes=non_success,
        tool_evidence=ledger,
    )
    assert ledger.effect_state == "unconfirmed", "precondition: the ledger floor is set"
    # The failed call is never replayed automatically.
    assert executed == ["ws_external_action"]
    return result, text_rounds


@pytest.mark.asyncio
async def test_valid_declared_non_success_completes(monkeypatch):
    text = json.dumps({"status": "blocked", "reason": "The upload may have happened; check the channel."})
    result, text_rounds = await _run(monkeypatch, text)
    assert text_rounds == []

    assert result.stop_reason == "completed"
    assert json.loads(result.content)["status"] == "blocked"
    assert result.control["kind"] == "outcome"
    assert result.control["outcome"] == "completed_non_success"
    assert result.control["effect_state"] == "unconfirmed"
    assert result.control["do_not_replay"] is True
    assert result.control["non_success_value"] == "blocked"
    assert result.control["downgraded_success"] is False
    # Not a tool-error control: consumers that fail on error_reason ==
    # "tool_outcome_unconfirmed" (plan workers) must not see that key here.
    assert "error_reason" not in result.control
    assert result.control.get("terminal_failure") is not True


@pytest.mark.asyncio
async def test_claimed_success_after_unconfirmed_error_is_downgraded(monkeypatch):
    result, text_rounds = await _run(monkeypatch, json.dumps({"status": "done", "reason": "uploaded"}))
    assert text_rounds == []
    # An unconfirmed effect never becomes a plain success, and is not replayed.
    assert result.control["kind"] == "outcome"
    assert result.control["outcome"] == "completed_non_success"
    assert result.control["downgraded_success"] is True
    assert result.control["effect_state"] == "unconfirmed"
    assert result.control["do_not_replay"] is True
    assert result.control["retryable"] is False


@pytest.mark.asyncio
async def test_invalid_json_still_fails(monkeypatch):
    result, text_rounds = await _run(monkeypatch, "I could not finish.")
    assert result.stop_reason == "error"
    assert len(text_rounds) == 1, "one output-schema repair round, then failure"
    assert result.error == "output_schema_validation_failed"


@pytest.mark.asyncio
async def test_without_config_reply_is_not_a_declared_non_success(monkeypatch):
    result, text_rounds = await _run(monkeypatch, json.dumps({"status": "blocked", "reason": "x"}), non_success=None)
    assert text_rounds == []
    assert "non_success_value" not in result.control
    assert result.control["outcome"] == "completed_non_success"
    assert result.control["downgraded_success"] is True
    assert result.control["do_not_replay"] is True


def test_config_naming_a_field_outside_the_schema_is_ignored():
    assert loop_module._parse_non_success_outcomes({"field": "nope", "values": ["x"]}, SCHEMA) is None
    assert loop_module._parse_non_success_outcomes({"field": "status", "values": []}, SCHEMA) is None
    assert loop_module._parse_non_success_outcomes(NON_SUCCESS, SCHEMA) == ("status", ("blocked", "needs_input"))


def test_harness_passes_non_success_outcomes_through():
    from packages.core.ai.runtime import harness

    assert "non_success_outcomes" in inspect.signature(harness.runtime_execute_agentic_loop).parameters
    source = inspect.getsource(harness.runtime_execute_agentic_loop)
    assert '"non_success_outcomes": non_success_outcomes' in source


@pytest.mark.asyncio
async def test_normal_success_without_ledger_problems_keeps_control_unchanged(monkeypatch):
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    async def fake_round(messages, _tools, **_kwargs):
        return json.dumps({"status": "done", "reason": "ok"}), None, {}

    # With no tools the loop takes the text-completion round.
    async def fake_text_round(messages, **_kwargs):
        return json.dumps({"status": "done", "reason": "ok"}), {}

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", fake_text_round)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="do it", tools=[], tool_executor=None,
        max_rounds=2, output_schema=SCHEMA, non_success_outcomes=NON_SUCCESS,
        tool_evidence=RuntimeToolEvidence(),
    )

    assert result.stop_reason == "completed"
    assert result.control is None


@pytest.mark.asyncio
async def test_chat_turn_without_schema_that_recovered_carries_no_outcome_control(monkeypatch):
    """Chat has no consumer for a disposition; a recovered turn is just a turn."""
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    rounds = {"n": 0}

    async def fake_round(messages, _tools, **_kwargs):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return "", [{"id": "call-1", "name": "ws_external_action", "arguments": {}}], {}
        return "All sorted.", None, {}

    async def tool_executor(_name, _args):
        return json.dumps({"error": "provider_outcome_unknown"})

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    ledger = RuntimeToolEvidence()
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="do it",
        tools=[_tool("ws_external_action")], tool_executor=tool_executor,
        max_rounds=4, tool_evidence=ledger,
    )

    assert ledger.effect_state == "unconfirmed", "precondition: the ledger floor is set"
    assert result.stop_reason == "completed"
    assert result.control is None


@pytest.mark.asyncio
async def test_execution_evidence_survives_alongside_the_outcome_keys(monkeypatch):
    from packages.core.ai.runtime.final_response import FinalResponseAction, FinalResponseDecision
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    class Contract:
        instructions = ""
        response_format = None

        async def validate(self, content):
            return FinalResponseDecision(
                FinalResponseAction.ACCEPT, content=content, evidence=({"kind": "receipt", "id": "r1"},),
            )

    rounds = {"n": 0}

    async def fake_round(messages, _tools, **_kwargs):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return "", [{"id": "call-1", "name": "ws_external_action", "arguments": {}}], {}
        return json.dumps({"status": "blocked", "reason": "upload state unknown"}), None, {}

    async def tool_executor(_name, _args):
        return json.dumps({"error": "provider_outcome_unknown"})

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="do it",
        tools=[_tool("ws_external_action")], tool_executor=tool_executor,
        max_rounds=4, output_schema=SCHEMA, non_success_outcomes=NON_SUCCESS,
        tool_evidence=RuntimeToolEvidence(), final_response_contract=Contract(),
    )

    assert result.stop_reason == "completed"
    assert result.control["execution_evidence"] == [{"kind": "receipt", "id": "r1"}]
    assert result.control["kind"] == "outcome"
    assert result.control["outcome"] == "completed_non_success"


def test_ledger_effect_state_fails_closed_on_an_odd_value():
    from packages.core.ai.runtime.receipts import EffectState

    class Ledger:
        effect_state = "weird"

    assert loop_module._ledger_effect_state(Ledger(), default=EffectState.NONE) is EffectState.UNCONFIRMED


def test_ledger_effect_state_defaults_when_absent():
    from packages.core.ai.runtime.receipts import EffectState

    class Ledger:
        effect_state = None

    assert loop_module._ledger_effect_state(Ledger(), default=EffectState.NONE) is EffectState.NONE
    assert loop_module._ledger_effect_state(None, default=EffectState.NONE) is EffectState.NONE
    assert loop_module._ledger_effect_state(None, default=EffectState.UNCONFIRMED) is EffectState.UNCONFIRMED


def test_ledger_effect_state_reads_a_valid_value():
    from packages.core.ai.runtime.receipts import EffectState

    class Ledger:
        effect_state = "unconfirmed"

    assert loop_module._ledger_effect_state(Ledger(), default=EffectState.NONE) is EffectState.UNCONFIRMED
