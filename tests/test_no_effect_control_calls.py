"""Calls that execute nothing stay no-effect after they became CONTROL.

The effect contract moved a gateway-rejected (or unwrappable) ``call_tool`` and
``wait_until`` from READ_ONLY to CONTROL, so that they never cross the consent
gate. Every "did this call do anything?" judgement had used READ_ONLY as its
proof of no effect; it must keep treating these two as no effect. Otherwise a
refused gateway call becomes an unconfirmed effect and ``complete_task`` is
refused for it (the R2-6 resume regression in
test_task_agent_loop_gateway_resume).
"""
from __future__ import annotations

import json

import pytest

from packages.core.ai.runtime.approval_classifier import (
    RUNTIME_NO_EFFECT_CONTROL_TOOL_NAMES,
    classify_runtime_tool,
    runtime_call_executes_nothing,
)
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect

_REJECTED = {"name": "generate_file", "arguments": {"kind": "md"},
             "rejected_reason": "'generate_file' is not loaded for this request."}
_UNWRAPPABLE = {"arguments": {}}
_WAIT = {"seconds": 120, "reason": "render queue"}


@pytest.mark.parametrize(("name", "arguments"), [
    ("call_tool", _REJECTED), ("call_tool", _UNWRAPPABLE), ("wait_until", _WAIT),
])
def test_these_calls_are_control_and_execute_nothing(name, arguments):
    classification = classify_runtime_tool(name, arguments)
    assert classification.effect is RuntimeToolEffect.CONTROL
    assert runtime_call_executes_nothing(name, classification)


def test_other_control_tools_are_not_assumed_effectless():
    for name in ("sandbox_destroy", "update_goal_value", "complete_task"):
        classification = classify_runtime_tool(name, {})
        assert not runtime_call_executes_nothing(name, classification), name
    assert RUNTIME_NO_EFFECT_CONTROL_TOOL_NAMES == frozenset({"call_tool", "wait_until"})


def test_a_valid_gateway_call_is_judged_as_the_tool_it_calls():
    classification = classify_runtime_tool("call_tool", {"name": "generate_file", "arguments": {"kind": "md"}})
    assert classification.effect is RuntimeToolEffect.ACTION
    assert not runtime_call_executes_nothing("call_tool", classification)


@pytest.mark.parametrize(("name", "arguments", "result"), [
    ("call_tool", _REJECTED, json.dumps({"error": "gateway_call_rejected", "message": "not loaded"})),
    ("wait_until", _WAIT, json.dumps({"error": "invalid_wait", "message": "seconds out of range"})),
])
def test_the_ledger_records_them_as_no_effect_even_on_error(name, arguments, result):
    from packages.core.ai.runtime.receipts import EffectState, runtime_tool_effect_state

    assert runtime_tool_effect_state(name, arguments, result) is EffectState.NONE


def test_wait_until_may_run_in_a_parallel_batch():
    from packages.core.ai.runtime.human_gate import runtime_tool_requires_serial_execution

    assert runtime_tool_requires_serial_execution("wait_until", _WAIT) is False
    assert runtime_tool_requires_serial_execution("call_tool", _REJECTED) is False


@pytest.mark.parametrize(("name", "arguments"), [("call_tool", _REJECTED), ("wait_until", _WAIT)])
def test_an_interrupted_call_that_executes_nothing_did_not_run(name, arguments):
    from packages.core.ai.runtime.interrupted_tool_calls import _call_is_read_only

    assert _call_is_read_only(name, json.dumps(arguments)) is True
