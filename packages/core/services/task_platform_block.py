"""Project an internal Task failure and report it to the deployment operator."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

BLOCKED_BY_PLATFORM = "platform"
BLOCKED_BY_AGENT = "agent"

#: What the customer reads on the Task. The technical cause stays in the log
#: metadata and the staff report.
TASK_PLATFORM_BLOCK_MESSAGE = (
    "An internal issue stopped this Task. Your deployment operator can inspect "
    "the server logs. Completed work and files are preserved."
)


async def report_task_platform_block(
    *,
    task_id: str,
    entity_id: str | None,
    workspace_id: str | None,
    code: str,
    cause: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Report a committed platform block; never changes Task state.

    Call after the BLOCKED transition commits. The report is deduplicated per
    Task and code, so a re-woken Task does not page twice.
    """
    logger.error("Task %s blocked by internal issue %s: %s", task_id, code, cause)
    from packages.core.services.platform_faults import report_platform_fault

    await report_platform_fault(
        target=f"task:{task_id}",
        code=code,
        summary=cause,
        context={
            "task_id": task_id,
            "entity_id": entity_id,
            "workspace_id": workspace_id,
            **{
                key: details[key]
                for key in ("plan_id", "platform_error", "tool", "operation")
                if isinstance(details, dict) and details.get(key)
            },
        },
    )


__all__ = [
    "BLOCKED_BY_AGENT",
    "BLOCKED_BY_PLATFORM",
    "TASK_PLATFORM_BLOCK_MESSAGE",
    "report_task_platform_block",
]
