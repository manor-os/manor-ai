"""Room-local review of a Workspace Agent's frozen outbound reply.

The Workspace policy supplies the gate. The draft stays in the room run, not
Workspace Chat, so a Workspace operator outside the room cannot read it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.audit import AuditLog
from packages.core.models.base import generate_ulid
from packages.core.models.chat_room import ChatRoomAgentRun
from packages.core.services.room_agent_runtime import (
    RoomAgentCompletionError,
    _admit_run,
    room_reply_approver_is_current,
    room_reply_checkpoint,
)


class RoomReplyApprovalError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


async def pending_room_reply_approvals(
    db: AsyncSession, *, room_id: str, approver_user_id: str,
) -> list[dict]:
    rows = (await db.execute(select(ChatRoomAgentRun).where(
        ChatRoomAgentRun.room_id == room_id,
        ChatRoomAgentRun.status == "waiting_approval",
        ChatRoomAgentRun.checkpoint.has_key("pending_reply"),
    ).order_by(ChatRoomAgentRun.created_at.desc(), ChatRoomAgentRun.id.desc()).limit(50))).scalars().all()
    result = []
    for run in rows:
        try:
            checkpoint = room_reply_checkpoint(run)
        except RoomAgentCompletionError:
            continue
        if checkpoint is None or checkpoint["status"] != "pending":
            continue
        turn, _ = await _admit_run(db, run)
        if turn is None or not turn.workspace_id or not await room_reply_approver_is_current(
            db, room_id=room_id, user_id=approver_user_id,
            workspace_id=turn.workspace_id,
        ):
            continue
        result.append({
            "run_id": run.id, "agent_name": run.agent_name,
            "content": checkpoint["content"], "digest": checkpoint["digest"],
            "created_at": run.created_at,
        })
    return result


async def decide_room_reply_approval(
    db: AsyncSession, *, room_id: str, run_id: str,
    approver_user_id: str, expected_digest: str, approve: bool,
) -> bool:
    """Decide the exact displayed draft; return whether to enqueue publication."""
    run = (await db.execute(select(ChatRoomAgentRun).where(
        ChatRoomAgentRun.id == run_id,
        ChatRoomAgentRun.room_id == room_id,
    ).with_for_update())).scalar_one_or_none()
    if run is None or run.status != "waiting_approval":
        raise RoomReplyApprovalError("approval_unavailable")
    try:
        checkpoint = room_reply_checkpoint(run)
    except RoomAgentCompletionError as exc:
        raise RoomReplyApprovalError("approval_unavailable") from exc
    if checkpoint is None or checkpoint["status"] != "pending":
        raise RoomReplyApprovalError("approval_unavailable")
    if checkpoint["digest"] != expected_digest:
        raise RoomReplyApprovalError("approval_draft_changed")
    turn, error = await _admit_run(db, run)
    if turn is None or not turn.workspace_id:
        raise RoomReplyApprovalError(error or "approval_scope_changed")
    if not await room_reply_approver_is_current(
        db, room_id=room_id, user_id=approver_user_id,
        workspace_id=turn.workspace_id,
    ):
        raise RoomReplyApprovalError("approver_not_authorized")
    if approve:
        run.checkpoint = {"pending_reply": {
            **checkpoint, "status": "approved",
            "approved_by_user_id": approver_user_id,
        }}
        run.status = "pending"
        run.last_enqueued_at = None
    else:
        run.checkpoint = None
        run.status = "interrupted"
        run.error_code = "reply_approval_rejected"
        run.completed_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        id=generate_ulid(), entity_id=turn.entity_id,
        user_id=approver_user_id,
        action="chat_room.reply_approval.approved" if approve else "chat_room.reply_approval.rejected",
        resource_type="chat_room_agent_run", resource_id=run.id,
        details={
            "room_id": room_id, "binding_id": run.binding_id,
            "workspace_id": turn.workspace_id,
            "reply_digest": checkpoint["digest"],
        },
    ))
    return approve
