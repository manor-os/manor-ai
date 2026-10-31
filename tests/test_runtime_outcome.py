"""The single outcome vocabulary: host effect_state x model disposition."""

from __future__ import annotations

import pytest

from packages.core.ai.runtime.outcome import (
    Disposition,
    RunOutcome,
    decide_outcome,
)
from packages.core.ai.runtime.receipts import EffectState


@pytest.mark.parametrize(
    ("effect", "disposition", "outcome", "retryable", "do_not_replay"),
    [
        (EffectState.NONE, Disposition.SUCCESS, RunOutcome.COMPLETED, False, False),
        (EffectState.NONE, Disposition.NON_SUCCESS, RunOutcome.COMPLETED_NON_SUCCESS, True, False),
        (EffectState.NONE, Disposition.UNDECLARED, RunOutcome.FAILED, True, False),
        (EffectState.CONFIRMED, Disposition.SUCCESS, RunOutcome.COMPLETED, False, False),
        (EffectState.CONFIRMED, Disposition.NON_SUCCESS, RunOutcome.COMPLETED_NON_SUCCESS, False, False),
        (EffectState.CONFIRMED, Disposition.UNDECLARED, RunOutcome.FAILED, False, False),
        # An unconfirmed effect can never be reported as success.
        (EffectState.UNCONFIRMED, Disposition.SUCCESS, RunOutcome.COMPLETED_NON_SUCCESS, False, True),
        (EffectState.UNCONFIRMED, Disposition.NON_SUCCESS, RunOutcome.COMPLETED_NON_SUCCESS, False, True),
        (EffectState.UNCONFIRMED, Disposition.UNDECLARED, RunOutcome.FAILED, False, True),
    ],
)
def test_outcome_table(effect, disposition, outcome, retryable, do_not_replay):
    decision = decide_outcome(effect, disposition)
    assert decision.outcome is outcome
    assert decision.effect_state is effect
    assert decision.retryable is retryable
    assert decision.do_not_replay is do_not_replay


def test_decision_control_projection_is_flat_and_json_safe():
    decision = decide_outcome(EffectState.UNCONFIRMED, Disposition.NON_SUCCESS)
    assert decision.as_control() == {
        "kind": "outcome",
        "outcome": "completed_non_success",
        "effect_state": "unconfirmed",
        "retryable": False,
        "do_not_replay": True,
        "downgraded_success": False,
    }
    assert decide_outcome(EffectState.UNCONFIRMED, Disposition.SUCCESS).as_control()["downgraded_success"] is True


def test_replay_fields_from_control():
    from packages.core.ai.runtime.outcome import replay_fields_from_control

    assert replay_fields_from_control(None) == {}
    assert replay_fields_from_control({"kind": "tool_error"}) == {}
    assert replay_fields_from_control({"retryable": False}) == {"retryable": False}
    assert replay_fields_from_control({"do_not_replay": True, "retryable": False, "effect_state": "unconfirmed"}) == {
        "retryable": False, "do_not_replay": True, "effect_state": "unconfirmed",
    }
    # do_not_replay is only ever raised, never lowered, by a control.
    assert replay_fields_from_control({"do_not_replay": False}) == {}
    # Nested result dicts (skill results) carry the control under "control".
    assert replay_fields_from_control({"control": {"do_not_replay": True}}) == {"do_not_replay": True}


def test_replay_fields_from_result_reads_an_unconfirmed_ledger_without_control():
    """A max_rounds stop carries no outcome control, only the ledger state."""
    from types import SimpleNamespace

    from packages.core.ai.runtime.outcome import replay_fields_from_result

    max_rounds = SimpleNamespace(
        stop_reason="max_rounds", control=None,
        tool_evidence={"effect_state": "unconfirmed", "unconfirmed_calls": [{"call_id": "c1", "tool": "send"}]},
    )
    assert replay_fields_from_result(max_rounds) == {"do_not_replay": True, "effect_state": "unconfirmed"}

    # Unconfirmed calls alone are enough, whatever the aggregate says.
    calls_only = SimpleNamespace(control=None, tool_evidence={"effect_state": "none", "unconfirmed_calls": [{"call_id": "c1"}]})
    assert replay_fields_from_result(calls_only) == {"do_not_replay": True, "effect_state": "unconfirmed"}

    # A valid host ledger state remains available to retry callers.
    clean = SimpleNamespace(control={"retryable": True}, tool_evidence={"effect_state": "confirmed", "unconfirmed_calls": []})
    assert replay_fields_from_result(clean) == {"retryable": True, "effect_state": "confirmed"}
    # A nested receipt cannot be weakened by an incomplete outer ledger. This
    # is the same monotonic ordering used by the host receipt aggregator.
    nested_effect = SimpleNamespace(
        control={"effect_state": "confirmed"},
        tool_evidence={"effect_state": "none", "unconfirmed_calls": []},
    )
    assert replay_fields_from_result(nested_effect) == {"effect_state": "confirmed"}
    assert replay_fields_from_result(SimpleNamespace(control=None, tool_evidence={})) == {}
    assert replay_fields_from_result(None) == {}


def test_replay_fields_from_result_reads_a_dict_skill_result_ledger():
    from packages.core.ai.runtime.outcome import replay_fields_from_result

    skill_result = {
        "content": "x", "stop_reason": "error",
        "control": {"kind": "tool_error", "retryable": True, "do_not_replay": False},
        "tool_evidence": {"effect_state": "unconfirmed", "unconfirmed_calls": []},
    }
    # The ledger raises do_not_replay; the control can never lower it.
    assert replay_fields_from_result(skill_result) == {
        "retryable": True, "do_not_replay": True, "effect_state": "unconfirmed",
    }
    assert replay_fields_from_result({"control": {"do_not_replay": True}}) == {"do_not_replay": True}
    assert replay_fields_from_result({"content": "ok", "tool_evidence": {"effect_state": "none"}}) == {
        "effect_state": "none",
    }


def test_attempt_forbids_replay_gates_on_derived_marker_or_effect_receipt():
    from packages.core.ai.runtime.outcome import attempt_forbids_replay

    assert attempt_forbids_replay({"do_not_replay": True}) is True
    assert attempt_forbids_replay({"do_not_replay": True, "retryable": True}) is True
    # Older result projections can retain a host receipt without the derived
    # marker. A retry could duplicate a delivered report or another effect.
    assert attempt_forbids_replay({"effect_state": "confirmed"}) is True
    assert attempt_forbids_replay({"effect_state": "unconfirmed"}) is True
    # retryable=False alone is an exhausted in-loop budget, not an unknown effect.
    assert attempt_forbids_replay({"retryable": False}) is False
    assert attempt_forbids_replay({"retryable": False, "retry_budget_exhausted": True}) is False
    assert attempt_forbids_replay({"effect_state": "none"}) is False
    assert attempt_forbids_replay({"do_not_replay": False}) is False
    assert attempt_forbids_replay(None) is False
