"""The single outcome vocabulary for agent runs, steps and delegated children.

outcome = host ``effect_state`` x model ``disposition``

* ``effect_state`` comes only from the host receipt ledger (receipts.py).
* ``disposition`` is what the model declared (success / non-success) or
  ``undeclared`` when it produced no valid declaration.

An unconfirmed effect is never reported as success and is never replayed
automatically (``do_not_replay``); every retry path must honour that flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from packages.core.ai.runtime.receipts import EffectState


class RunOutcome(StrEnum):
    COMPLETED = "completed"
    COMPLETED_NON_SUCCESS = "completed_non_success"
    FAILED = "failed"


class Disposition(StrEnum):
    SUCCESS = "success"
    NON_SUCCESS = "non_success"
    UNDECLARED = "undeclared"


@dataclass(frozen=True)
class OutcomeDecision:
    outcome: RunOutcome
    effect_state: EffectState
    retryable: bool
    do_not_replay: bool
    downgraded_success: bool = False

    def as_control(self) -> dict[str, Any]:
        return {
            "kind": "outcome",
            "outcome": self.outcome.value,
            "effect_state": self.effect_state.value,
            "retryable": self.retryable,
            "do_not_replay": self.do_not_replay,
            "downgraded_success": self.downgraded_success,
        }


def decide_outcome(effect_state: EffectState, disposition: Disposition) -> OutcomeDecision:
    if effect_state is EffectState.UNCONFIRMED:
        outcome = (
            RunOutcome.FAILED
            if disposition is Disposition.UNDECLARED
            else RunOutcome.COMPLETED_NON_SUCCESS
        )
        return OutcomeDecision(
            outcome=outcome,
            effect_state=effect_state,
            retryable=False,
            do_not_replay=True,
            downgraded_success=disposition is Disposition.SUCCESS,
        )
    if disposition is Disposition.SUCCESS:
        return OutcomeDecision(RunOutcome.COMPLETED, effect_state, retryable=False, do_not_replay=False)
    outcome = (
        RunOutcome.COMPLETED_NON_SUCCESS
        if disposition is Disposition.NON_SUCCESS
        else RunOutcome.FAILED
    )
    # Nothing happened yet → trying again is safe; a confirmed effect already
    # happened → a blind retry would duplicate it.
    return OutcomeDecision(
        outcome=outcome,
        effect_state=effect_state,
        retryable=effect_state is EffectState.NONE,
        do_not_replay=False,
    )


def replay_fields_from_control(control: Any) -> dict[str, Any]:
    """Replay-safety fields a failed attempt must carry to every retry path."""
    if isinstance(control, dict) and isinstance(control.get("control"), dict) and not (
        {"retryable", "do_not_replay", "effect_state"} & set(control)
    ):
        control = control["control"]
    if not isinstance(control, dict):
        return {}
    out: dict[str, Any] = {}
    if isinstance(control.get("retryable"), bool):
        out["retryable"] = control["retryable"]
    if control.get("do_not_replay") is True:
        out["do_not_replay"] = True
    if isinstance(control.get("effect_state"), str):
        out["effect_state"] = control["effect_state"]
    return out


def attempt_forbids_replay(result: Any) -> bool:
    """True when an automatic retry of this attempt could repeat an effect.

    Only ``do_not_replay`` says that. ``retryable=False`` on its own is what
    loop controls set when their in-loop budget is spent (e.g.
    ``retry_budget_exhausted``); a later, separate attempt is still safe.
    """
    if not isinstance(result, dict):
        return False
    return result.get("do_not_replay") is True


def _ledger_is_unconfirmed(evidence: Any) -> bool:
    """True when a run's receipt ledger holds an effect nobody confirmed.

    ``evidence`` is the ledger state the harness writes on the result
    (``RuntimeToolEvidence.checkpoint_state()``) or a live ledger object.
    """
    if isinstance(evidence, dict):
        effect_state = evidence.get("effect_state")
        unconfirmed_calls = evidence.get("unconfirmed_calls")
    elif evidence is not None:
        effect_state = getattr(evidence, "effect_state", None)
        unconfirmed_calls = getattr(evidence, "unconfirmed_calls", None)
    else:
        return False
    return effect_state == EffectState.UNCONFIRMED.value or bool(unconfirmed_calls)


def replay_fields_from_result(result: Any) -> dict[str, Any]:
    """Replay-safety fields of a finished attempt: its control plus its ledger.

    The control covers tool errors and outcome decisions; the ledger covers
    stops that carry no outcome control (``max_rounds``, calls without an
    output schema) but still left an unconfirmed effect behind.
    ``do_not_replay`` is only ever raised here, never lowered.
    """
    if result is None:
        return {}
    control = getattr(result, "control", None)
    evidence = getattr(result, "tool_evidence", None)
    if isinstance(result, dict):
        control = control or result.get("control")
        evidence = evidence or result.get("tool_evidence")
    out = replay_fields_from_control(control)
    if _ledger_is_unconfirmed(evidence):
        out["do_not_replay"] = True
        out["effect_state"] = EffectState.UNCONFIRMED.value
    return out
