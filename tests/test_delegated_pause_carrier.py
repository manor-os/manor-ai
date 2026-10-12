"""A delegated agent's pause is no effect of its own, with host proof only.

The child's effects reach the parent ledger through its merged child ledger.
Only when that hand-off actually happened does the delegation return the
host carrier; the same JSON without it stays an unconfirmed effect.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime import workspace_delegation_actions as delegation
from packages.core.ai.runtime.receipts import EffectState, runtime_tool_effect_state
from packages.core.ai.runtime.tool_evidence import runtime_child_evidence_scope
from packages.core.ai.runtime.tool_results import RuntimeDelegatedPauseToolResult

PAUSE = {
    "delegated": True, "status": "blocked", "stop_reason": "waiting_resource",
    "blocker_kind": "request_input", "stop_parent": True, "content": "Which date?",
}
ARGS = {"tasks": [{"agent": "Writer", "task": "Draft it."}]}


def test_the_carrier_is_no_effect_and_plain_json_is_unconfirmed():
    raw = json.dumps(PAUSE)
    assert runtime_tool_effect_state("delegate_to_agent", ARGS, RuntimeDelegatedPauseToolResult(raw)) is EffectState.NONE
    assert runtime_tool_effect_state("delegate_to_agent", ARGS, raw) is EffectState.UNCONFIRMED


def test_the_carrier_passes_through_the_delegation_result_unchanged():
    carrier = RuntimeDelegatedPauseToolResult(json.dumps(PAUSE))
    assert delegation.runtime_delegation_tool_result(carrier) is carrier


def _child(tool_evidence):
    return SimpleNamespace(tool_evidence=tool_evidence)


def test_publish_reports_success_only_into_a_live_sink_with_a_ledger():
    ledger = {"observations": [{"call_id": "c1", "tool": "write_file", "effect_state": "confirmed"}]}
    # No parent sink: nothing reached the parent.
    assert delegation._publish_delegated_child_ledger("run", _child(ledger)) is False
    with runtime_child_evidence_scope() as sink:
        assert delegation._publish_delegated_child_ledger("run", _child(ledger)) is True
        assert sink and sink[0]["call_id"] == "run/c1"
    with runtime_child_evidence_scope():
        assert delegation._publish_delegated_child_ledger("run", _child(None)) is False


def test_a_failed_publish_returns_plain_json(monkeypatch):
    def broken(_observations):
        raise RuntimeError("ledger down")

    monkeypatch.setattr("packages.core.ai.runtime.tool_evidence.runtime_publish_child_evidence", broken)
    with runtime_child_evidence_scope():
        published = delegation._publish_delegated_child_ledger("run", _child({"observations": []}))
    assert published is False
    result = delegation._delegated_pause_result(PAUSE, published=published)
    assert not isinstance(result, RuntimeDelegatedPauseToolResult)
    assert json.loads(result) == PAUSE
    assert isinstance(delegation._delegated_pause_result(PAUSE, published=True), RuntimeDelegatedPauseToolResult)
