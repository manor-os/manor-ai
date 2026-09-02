"""Workflow execution lifecycle constants."""
from __future__ import annotations

from enum import Enum


class WorkflowRunStatus(str, Enum):
    """Every state a WorkflowRun row can hold."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    # Preserve the stored value in f-strings and logs instead of rendering
    # ``WorkflowRunStatus.PENDING``.
    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


# Internal receipt stored on WorkflowRun.trigger_data.  Retry creation must
# never copy it into a new attempt.
WORKFLOW_TERMINAL_EFFECTS_TRIGGER_FIELD = "_workflow_terminal_effects"


#: Runs that still represent one live execution lineage. A paused run remains
#: open because it can resume from its checkpoint and must block a duplicate.
WORKFLOW_RUN_OPEN_STATUSES: tuple[WorkflowRunStatus, ...] = (
    WorkflowRunStatus.PENDING,
    WorkflowRunStatus.RUNNING,
    WorkflowRunStatus.PAUSED,
)

#: Runs whose attempt has settled. Some completed/failed outcomes remain
#: retryable, but a retry is a new attempt in the same lineage.
WORKFLOW_RUN_TERMINAL_STATUSES: tuple[WorkflowRunStatus, ...] = (
    WorkflowRunStatus.COMPLETED,
    WorkflowRunStatus.FAILED,
    WorkflowRunStatus.CANCELLED,
)


assert set(WORKFLOW_RUN_OPEN_STATUSES).isdisjoint(WORKFLOW_RUN_TERMINAL_STATUSES)
assert set(WORKFLOW_RUN_OPEN_STATUSES) | set(WORKFLOW_RUN_TERMINAL_STATUSES) == set(WorkflowRunStatus)
