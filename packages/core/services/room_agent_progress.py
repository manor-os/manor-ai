"""Group-visible execution metadata, independent of private reply candidates."""
from __future__ import annotations

from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.chat_room import ChatRoomAgentRun
from packages.core.services.realtime import queue_chat_room_change_after_commit

_STEP_FIELDS = {"id", "name", "status", "started_at_ms", "ended_at_ms", "duration_ms"}
_LIFECYCLE = {
    "pending": "waiting", "running": "running", "waiting_approval": "waiting",
    "succeeded": "completed", "failed": "failed", "interrupted": "cancelled",
}


def room_agent_process_blocks(run: ChatRoomAgentRun) -> list[dict]:
    """Project host lifecycle and public tool events, never checkpoint/prose."""
    steps = [
        {key: value for key, value in step.items() if key in _STEP_FIELDS}
        for block in (run.assistant_blocks or []) if block.get("type") == "process"
        for step in block.get("steps", [])
    ]
    process = {
        "id": f"room-process-{run.id}", "type": "process",
        "lifecycle_status": _LIFECYCLE[run.status],
        "default_collapsed": run.status not in {"pending", "running"}, "steps": steps,
    }
    if run.started_at:
        process["started_at_ms"] = int(run.started_at.timestamp() * 1000)
    if run.completed_at:
        process["ended_at_ms"] = int(run.completed_at.timestamp() * 1000)
        if run.started_at:
            process["duration_ms"] = max(0, process["ended_at_ms"] - process["started_at_ms"])
    return [process]


async def record_room_agent_tool_event(db: AsyncSession, run_id: str, step: dict, *, starting: bool) -> None:
    """Publish only actual tool invocations while this attempt owns the run."""
    run = (await db.execute(select(ChatRoomAgentRun).where(
        ChatRoomAgentRun.id == run_id,
    ).with_for_update())).scalar_one_or_none()
    if run is None or run.status != "running":
        return
    blocks = room_agent_process_blocks(run)
    steps = blocks[0]["steps"]
    public_step = {key: value for key, value in step.items() if key in _STEP_FIELDS}
    if starting:
        steps.append(public_step)
    else:
        for index, existing in enumerate(steps):
            if existing["id"] == step["id"]:
                steps[index] = public_step
                break
    run.assistant_blocks = deepcopy(blocks)
    queue_chat_room_change_after_commit(db, run.room_id)
    await db.commit()
