"""The Task agent loop's run state slots, shared by the worker and its retries.

The worker (``workers.task_agent_loop``) checkpoints its live state through
these slots every round; a retry of a stopped run
(``services.task_coordinator_wakeup.agent_loop_retry_checkpoint``) derives
what it carries from the same declarations. One registry, so a new piece of
loop state cannot survive park/resume yet be silently dropped by a retry.
"""
from __future__ import annotations

import copy
from typing import Any

from packages.core.ai.runtime.run_state import (
    RETRY_CARRY,
    RETRY_RESET,
    RuntimeRunStateSlot,
    RuntimeRunStateSlots,
)


TASK_PLAN_KEY = "task_plan"
TASK_SUBMISSION_KEY = "task_submission"
# What the host verified when it accepted the submission (files, outcome).
TASK_COMPLETION_KEY = "task_completion"
INSPECTION_KEY = "inspection"
# Unconfirmed call ids already shown to the model in a refusal (rule 1.4).
UNCONFIRMED_REFUSAL_KEY = "unconfirmed_refusal"
# Digest of the work list a host-forced stop last posted; it survives resumes
# and retries so a re-block with no new work posts nothing.
POSTED_WORK_KEY = "posted_work_digest"
NON_SUBMIT_RETRIES_KEY = "non_submit_retries"
# How this run's delegate_to_agent runs its agents: "durable" (each one its
# own child run; services.task_agent_delegation) or "inline" (nested in this
# worker). Frozen at the first wake that offers delegation.
DELEGATION_MODE_KEY = "delegation_mode"
DELEGATION_MODE_DURABLE = "durable"
DELEGATION_MODE_INLINE = "inline"
DELEGATION_MODES = frozenset({DELEGATION_MODE_DURABLE, DELEGATION_MODE_INLINE})

# The host's buffer of the finished calls of the round in progress; the
# round checkpoint journals and clears it (services.task_run_journal).
ROUND_JOURNAL_KEY = "round_journal"

# Live loop state that a round checkpoint never carries: the CAS version,
# control flags, the payload last persisted, the wait this wake resumes (the
# park path writes that one itself) and the round's journal buffer (its row
# is written with the round). Every other key of the live state is a
# registered slot (guarded in test_run_state_roundtrip).
TASK_LOOP_EPHEMERAL_STATE_KEYS = frozenset({
    "version", "yield", "phase_running", "last_checkpoint", "resumed_wait",
    # When this wake's brief was built: later Task changes are delivered to the run.
    "brief_built_at",
    # The Workspace governance revision of this wake's prompt: a later one is delivered.
    "rules_revision",
    ROUND_JOURNAL_KEY,
})


def task_loop_initial_state(version: int) -> dict[str, Any]:
    """The live state of one wake before its checkpoint is loaded."""

    state: dict[str, Any] = {
        "version": version,
        "yield": False,
        "phase_running": False,
        "last_checkpoint": {},
        # The parked wait this wake resumes (task_loop_wait), if any.
        "resumed_wait": None,
        ROUND_JOURNAL_KEY: [],
        "task_plan": None,
        "submission": None,
        "completion": None,
        "inspection": None,
        "unconfirmed_refusal": None,
        "posted_work_digest": None,
        "non_submit_retries": 0,
        DELEGATION_MODE_KEY: None,
    }
    return state


def _dict_or_none(raw: Any) -> dict[str, Any] | None:
    return copy.deepcopy(raw) if isinstance(raw, dict) else None


def _non_negative_int(raw: Any) -> int:
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _inspection_tracker(raw: Any) -> Any:
    from packages.core.workers.task_agent_loop_completion import InspectionTracker

    return InspectionTracker(raw)


def task_loop_run_state_slots(state: dict[str, Any]) -> RuntimeRunStateSlots:
    """The loop's registered run state slots over its live ``state``.

    Every round snapshot captures them and every wake loads them back; a new
    piece of loop state that must survive park/resume is registered here,
    with what a retry of the stopped run does with it.
    """

    slots = [
        # The plan outlives the attempt that wrote it.
        RuntimeRunStateSlot(TASK_PLAN_KEY, RETRY_CARRY, decode=_dict_or_none),
        # A retry is a new attempt: its submission, completion and the
        # refusals it was shown are its own.
        RuntimeRunStateSlot(TASK_SUBMISSION_KEY, RETRY_RESET, decode=_dict_or_none, host_key="submission"),
        RuntimeRunStateSlot(TASK_COMPLETION_KEY, RETRY_RESET, decode=_dict_or_none, host_key="completion"),
        # Reads made before a retry do not vouch for files after it.
        RuntimeRunStateSlot(
            INSPECTION_KEY, RETRY_RESET,
            encode=lambda tracker: tracker.state() if tracker is not None else None,
            decode=_inspection_tracker,
        ),
        RuntimeRunStateSlot(
            UNCONFIRMED_REFUSAL_KEY, RETRY_RESET,
            encode=lambda shown: list(shown) if shown is not None else None,
            decode=lambda raw: [str(item) for item in raw] if isinstance(raw, list) else None,
        ),
        RuntimeRunStateSlot(
            POSTED_WORK_KEY, RETRY_CARRY,
            encode=lambda digest: digest or None,
            decode=lambda raw: raw if isinstance(raw, str) else None,
        ),
        # A retry gets a fresh continuation budget.
        RuntimeRunStateSlot(
            NON_SUBMIT_RETRIES_KEY, RETRY_RESET,
            encode=_non_negative_int,
            decode=_non_negative_int,
            default=lambda: 0,
        ),
        # A retry keeps the mode its run started with: the child runs it
        # already made, and their results, belong to that mode.
        RuntimeRunStateSlot(
            DELEGATION_MODE_KEY, RETRY_CARRY,
            encode=lambda mode: mode if mode in DELEGATION_MODES else None,
            decode=lambda raw: raw if raw in DELEGATION_MODES else None,
        ),
    ]
    return RuntimeRunStateSlots(slots, live=state)


__all__ = [
    "DELEGATION_MODES",
    "DELEGATION_MODE_DURABLE",
    "DELEGATION_MODE_INLINE",
    "DELEGATION_MODE_KEY",
    "INSPECTION_KEY",
    "NON_SUBMIT_RETRIES_KEY",
    "POSTED_WORK_KEY",
    "ROUND_JOURNAL_KEY",
    "TASK_COMPLETION_KEY",
    "TASK_LOOP_EPHEMERAL_STATE_KEYS",
    "TASK_PLAN_KEY",
    "TASK_SUBMISSION_KEY",
    "UNCONFIRMED_REFUSAL_KEY",
    "task_loop_initial_state",
    "task_loop_run_state_slots",
]
