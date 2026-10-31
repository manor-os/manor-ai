"""Read a Task's recorded capability blocker (historical Plan-planning gaps).

The retired Plan/Step planner wrote ``task.details["capability_blocker"]``
when an Agent lacked a capability before planning. Nothing writes it now
(the agent loop acquires capabilities itself), but a Task blocked that way
keeps the record and the Strategist still reads it.
"""
from __future__ import annotations

from typing import Any

from packages.core.constants.task import TaskStatus
from packages.core.models.task import Task
from packages.core.services.task_execution import task_execution_intent_generation

BLOCKER_KEY = "capability_blocker"


def current_capability_blocker(task: Task) -> dict[str, Any] | None:
    raw_details = getattr(task, "details", None)
    details = raw_details if isinstance(raw_details, dict) else {}
    blocker = details.get(BLOCKER_KEY)
    if (
        getattr(task, "status", None) != TaskStatus.BLOCKED.value
        or not isinstance(blocker, dict)
        or blocker.get("generation") != task_execution_intent_generation(task)
    ):
        return None
    return blocker
