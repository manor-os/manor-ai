"""Decide how a Workflow run continues after its execution owner is gone.

An owner can disappear without settling its run: the process dies, or it
stops being able to prove its lease. The run is then resumed from its durable
step boundary whenever replaying the interrupted node cannot duplicate an
external effect. It is failed only when an interrupted node may already have
changed something outside the run and nothing durable says whether it did;
that error names the node so a person can check it before retrying.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from packages.core.services.workflow_contracts import workflow_step_label
from packages.core.services.workflow_run_trace import execution_trace_capacity

# Internal ``trigger_data`` keys (underscore-prefixed like the other runner keys).
WORKFLOW_EXECUTION_RECOVERY_TRIGGER_FIELD = "_execution_recovery"
WORKFLOW_EXECUTION_DRIVER_TRIGGER_FIELD = "_execution_driver"
MANUAL_STEP_EXECUTION_DRIVER = "manual_step"
# Celery header carrying a retained lease token to the resuming worker. Older
# workers ignore unknown headers and fall back to normal lease acquisition.
WORKFLOW_CLAIM_HANDOFF_HEADER = "manor_workflow_claim_handoff"
# Interruptions of one run (not redeliveries of one handoff) before recovery
# stops, so a node that kills its worker cannot loop forever.
MAX_WORKFLOW_EXECUTION_RECOVERIES = 3

# Nodes whose execution changes nothing outside this run's own row: control
# flow, data shaping, reads and model calls. Re-running an interrupted one is
# a plain retry (an LLM node is billed again, nothing else repeats). Every
# other type, including any type added later, is treated as having unknown
# external effects until it is listed here.
REPLAY_SAFE_WORKFLOW_STEP_TYPES = frozenset({
    "aggregate",
    "classifier",
    "condition",
    "datetime",
    "dedupe",
    "end",
    "extract",
    "extractfromfile",
    "filter",
    "limit",
    "llm",
    "merge",
    "note",
    "rag",
    "respond",
    "sort",
    "split",
    "stop",
    "switch",
    "transform",
    "trigger",
    "unsupported",
    "wait",
    "webhook",
})
_REPLAY_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def workflow_claim_handoff_digest(token: str) -> str:
    """Identify a retained lease in run data without publishing the token."""

    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:32]


def workflow_step_is_replay_safe(step: dict[str, Any]) -> bool:
    """Whether re-running an interrupted node cannot repeat an external effect."""

    step_type = str(step.get("type") or "")
    config = step.get("config") if isinstance(step.get("config"), dict) else {}
    if step_type in {"llm", "extract"} and config.get("batch"):
        # Provider batch jobs are submitted once and polled; resubmitting
        # would create (and bill) a second batch.
        return False
    if step_type == "http":
        return str(config.get("method") or "GET").upper() in _REPLAY_SAFE_HTTP_METHODS
    return step_type in REPLAY_SAFE_WORKFLOW_STEP_TYPES


def interrupted_workflow_step_ids(
    run: Any,
    frontier_step_ids: Iterable[str],
) -> list[str]:
    """Frontier nodes whose durable trace says they started and never ended.

    Every runner commits a node's ``running`` transition before executing it,
    so a frontier node without one never started. A trace at capacity stops
    recording, which leaves start evidence unknown: every frontier node then
    counts as interrupted.
    """

    frontier = [str(step_id) for step_id in frontier_step_ids]
    trace = run.execution_trace if isinstance(run.execution_trace, list) else []
    if len(trace) >= execution_trace_capacity(run):
        return frontier
    latest: dict[str, str] = {}
    for entry in trace:
        if isinstance(entry, dict) and entry.get("node_id"):
            latest[str(entry["node_id"])] = str(entry.get("status") or "")
    return [step_id for step_id in frontier if latest.get(step_id) == "running"]


@dataclass(frozen=True)
class WorkflowClaimLossDecision:
    resume: bool
    error: str | None
    recovery_state: dict[str, Any] = field(default_factory=dict)
    interrupted_step_ids: tuple[str, ...] = ()


def decide_workflow_claim_loss(
    run: Any,
    frontier_steps: list[dict[str, Any]],
    *,
    lost_claim_token: str | None,
    now: datetime,
) -> WorkflowClaimLossDecision:
    """Resume when every interrupted node is replay-safe, else fail explicitly."""

    trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
    previous = trigger_data.get(WORKFLOW_EXECUTION_RECOVERY_TRIGGER_FIELD)
    state = dict(previous) if isinstance(previous, dict) else {}
    attempts = int(state.get("attempts") or 0)
    # A handoff whose lease expired unadopted (broker lost the delivery) is
    # the same interruption being redelivered, not a new one.
    if not (
        lost_claim_token
        and workflow_claim_handoff_digest(lost_claim_token)
        == state.get("handoff_claim_digest")
    ):
        attempts += 1
    steps_by_id = {str(step.get("id")): step for step in frontier_steps}
    interrupted = interrupted_workflow_step_ids(run, steps_by_id)
    state.update({
        "attempts": attempts,
        "last_interrupted_at": now.isoformat(),
        "interrupted_step_ids": list(interrupted),
    })
    state.pop("handoff_claim_digest", None)
    from packages.core.services.workflow_step_resilience import (
        interrupted_step_proved_effect_free,
    )

    # An agent node is replayable when its effect-intent journal proves the
    # interrupted attempt never started a call that could change anything.
    unsafe = [
        steps_by_id[step_id]
        for step_id in interrupted
        if not workflow_step_is_replay_safe(steps_by_id[step_id])
        and not interrupted_step_proved_effect_free(run, step_id)
    ]
    effect_free = [
        step_id for step_id in interrupted
        if not workflow_step_is_replay_safe(steps_by_id[step_id])
        and interrupted_step_proved_effect_free(run, step_id)
    ]
    if effect_free:
        state["effect_free_step_ids"] = effect_free
    else:
        state.pop("effect_free_step_ids", None)
    if unsafe:
        step = unsafe[0]
        label = workflow_step_label(step)
        error = (
            f"Workflow execution was interrupted while step '{label}' "
            f"({step.get('type') or 'unknown'}) was running. It may already "
            "have changed files or external systems and that could not be "
            "confirmed, so it was not re-run automatically. Check its results, "
            "then retry the run from that step."
        )
        state["outcome"] = "failed_effects_unknown"
        return WorkflowClaimLossDecision(False, error, state, tuple(interrupted))
    if attempts > MAX_WORKFLOW_EXECUTION_RECOVERIES:
        error = (
            f"Workflow execution was interrupted {attempts} times; automatic "
            "recovery stopped. Retry the run once the platform is stable."
        )
        state["outcome"] = "failed_recovery_limit"
        return WorkflowClaimLossDecision(False, error, state, tuple(interrupted))
    state["outcome"] = "resumed"
    return WorkflowClaimLossDecision(True, None, state, tuple(interrupted))


__all__ = [
    "MANUAL_STEP_EXECUTION_DRIVER",
    "MAX_WORKFLOW_EXECUTION_RECOVERIES",
    "REPLAY_SAFE_WORKFLOW_STEP_TYPES",
    "WORKFLOW_CLAIM_HANDOFF_HEADER",
    "WORKFLOW_EXECUTION_DRIVER_TRIGGER_FIELD",
    "WORKFLOW_EXECUTION_RECOVERY_TRIGGER_FIELD",
    "WorkflowClaimLossDecision",
    "decide_workflow_claim_loss",
    "interrupted_workflow_step_ids",
    "workflow_claim_handoff_digest",
    "workflow_step_is_replay_safe",
]
