"""The run state slots of a delegated child run (``workers.task_agent_child_loop``).

A child run is driven wake by wake like the Task's root loop, by the same
``TaskLoopHost``. Its transcript, usage, rounds and ledger are the
RuntimeRunState fields; what the child host keeps on top of them is
registered here, so a new piece of child state cannot survive park/resume in
one place and be dropped in another.

* ``answer_revised``: the delegated answer's one chance to fit its share
  (``DelegatedAnswerLengthContract.revised``). Kept across wakes, or every
  slice would get a fresh chance.
* ``tool_outcomes``: the classified status of each call the child made, for
  its learning outcome (``chat_learning_outcome``). Bounded.

A child run is never retried in place (a new delegation makes a new child),
so both slots reset on retry.
"""
from __future__ import annotations

import copy
from typing import Any

from packages.core.ai.runtime.run_state import (
    RETRY_RESET,
    RuntimeRunStateSlot,
    RuntimeRunStateSlots,
)
from packages.core.services.task_agent_loop_state import ROUND_JOURNAL_KEY


ANSWER_REVISED_KEY = "answer_revised"
TOOL_OUTCOMES_KEY = "tool_outcomes"
# The newest outcomes are kept; a long child loses only its oldest.
TOOL_OUTCOMES_MAX = 200

# Live state the host keeps between rounds that a round checkpoint never
# carries (the same ephemera as the root loop's).
TASK_AGENT_CHILD_EPHEMERAL_STATE_KEYS = frozenset({
    "version", "yield", "phase_running", "last_checkpoint", "resumed_wait",
    ROUND_JOURNAL_KEY,
})


def task_agent_child_initial_state(version: int) -> dict[str, Any]:
    """The live state of one child wake before its checkpoint is loaded."""

    return {
        "version": version,
        "yield": False,
        "phase_running": False,
        "last_checkpoint": {},
        "resumed_wait": None,
        ROUND_JOURNAL_KEY: [],
        ANSWER_REVISED_KEY: False,
        TOOL_OUTCOMES_KEY: [],
    }


def _outcomes(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [copy.deepcopy(item) for item in raw if isinstance(item, dict)][-TOOL_OUTCOMES_MAX:]


def task_agent_child_run_state_slots(state: dict[str, Any]) -> RuntimeRunStateSlots:
    """The child host's registered slots over its live ``state``."""

    return RuntimeRunStateSlots([
        RuntimeRunStateSlot(
            ANSWER_REVISED_KEY, RETRY_RESET,
            encode=lambda revised: bool(revised),
            decode=lambda raw: raw is True,
            default=lambda: False,
        ),
        RuntimeRunStateSlot(
            TOOL_OUTCOMES_KEY, RETRY_RESET,
            encode=lambda outcomes: _outcomes(outcomes) or None,
            decode=_outcomes,
            default=list,
        ),
    ], live=state)


def record_tool_outcome(state: dict[str, Any], outcome: dict[str, Any]) -> None:
    """Append one classified call outcome, keeping the newest ``TOOL_OUTCOMES_MAX``."""

    outcomes = state.get(TOOL_OUTCOMES_KEY)
    if not isinstance(outcomes, list):
        outcomes = []
    outcomes.append(outcome)
    del outcomes[:-TOOL_OUTCOMES_MAX]
    state[TOOL_OUTCOMES_KEY] = outcomes


__all__ = [
    "ANSWER_REVISED_KEY",
    "TASK_AGENT_CHILD_EPHEMERAL_STATE_KEYS",
    "TOOL_OUTCOMES_KEY",
    "TOOL_OUTCOMES_MAX",
    "record_tool_outcome",
    "task_agent_child_initial_state",
    "task_agent_child_run_state_slots",
]
