"""Durable, fail-closed authorization for exact group Agent file mutations.

The owner switch is only a capability ceiling. These operations never grant
the Agent a personal or Workspace filesystem identity. Callers must separately
enforce that each room_* tool touches only this group's own files.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.audit import AuditLog
from packages.core.models.base import generate_ulid
from packages.core.models.chat_room import (
    ChatRoom, ChatRoomAgentRun, ChatRoomMember, ChatRoomMessage, ChatRoomToolApproval,
)
from packages.core.models.user import User


ROOM_MUTATION_TOOLS = frozenset({
    "room_create_file", "room_replace_file", "room_rename_file", "room_delete_file",
    "generate_file",
})
_MAX_ARGUMENT_BYTES = 64_000
_MAX_CHECKPOINT_BYTES = 512_000


class RoomToolApprovalError(ValueError):
    """A room tool invocation may not proceed under the current authority."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical_arguments_hash(arguments: dict[str, Any]) -> str:
    if not isinstance(arguments, dict):
        raise RoomToolApprovalError("invalid_arguments")
    try:
        encoded = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RoomToolApprovalError("invalid_arguments") from exc
    if len(encoded) > _MAX_ARGUMENT_BYTES:
        raise RoomToolApprovalError("arguments_too_large")
    return hashlib.sha256(encoded).hexdigest()


def _validate_new_file_tool(tool_name: str, arguments: dict[str, Any]) -> None:
    if tool_name == "generate_file":
        from packages.core.services.room_file_skill_policy import (
            RoomFileToolPolicyError, validate_room_file_tool_arguments,
        )

        try:
            normalized, _ = validate_room_file_tool_arguments(tool_name, arguments)
        except RoomFileToolPolicyError as exc:
            raise RoomToolApprovalError(exc.code) from exc
        if normalized != arguments:
            raise RoomToolApprovalError("group_generation_arguments_not_canonical")


async def _validated_scope(
    db: AsyncSession, run: ChatRoomAgentRun,
) -> tuple[ChatRoom, ChatRoomMessage, ChatRoomMember]:
    # Runtime imports this service for tool callbacks; defer the reciprocal
    # import to avoid module initialization cycles.
    from packages.core.services.room_agent_runtime import _admit_run

    # _admit_run locks the room and rechecks audience, binding, deployment,
    # requester, and owner/binder authority. Lock order is always run -> room.
    turn, error = await _admit_run(db, run)
    if turn is None:
        raise RoomToolApprovalError(error or "run_unavailable")
    room = (await db.execute(select(ChatRoom).where(ChatRoom.id == run.room_id))).scalar_one()
    if (
        room.kind != "group" or room.status != "active"
        or not room.ai_tool_execution_enabled
        or run.ai_tool_execution_revision is None
        or room.ai_tool_execution_revision != run.ai_tool_execution_revision
    ):
        raise RoomToolApprovalError("execution_setting_changed")
    source = (await db.execute(select(ChatRoomMessage).where(
        ChatRoomMessage.id == run.source_message_id,
        ChatRoomMessage.room_id == room.id,
        ChatRoomMessage.sender_user_id.is_not(None),
    ))).scalar_one_or_none()
    if source is None:
        raise RoomToolApprovalError("source_unavailable")
    member = (await db.execute(select(ChatRoomMember).where(
        ChatRoomMember.room_id == room.id,
        ChatRoomMember.user_id == source.sender_user_id,
        ChatRoomMember.status == "active",
    ))).scalar_one_or_none()
    if member is None:
        raise RoomToolApprovalError("requester_not_authorized")
    return room, source, member


async def _locked_run(db: AsyncSession, run_id: str) -> ChatRoomAgentRun:
    run = (await db.execute(select(ChatRoomAgentRun).where(
        ChatRoomAgentRun.id == run_id,
    ).with_for_update())).scalar_one_or_none()
    if run is None:
        raise RoomToolApprovalError("run_unavailable")
    return run


def _check_approval_binding(
    approval: ChatRoomToolApproval, run: ChatRoomAgentRun, source: ChatRoomMessage,
) -> None:
    if (
        approval.room_id != run.room_id or approval.run_id != run.id
        or approval.requester_user_id != source.sender_user_id
        or approval.binding_id != run.binding_id
        or approval.audience_fingerprint != run.audience_fingerprint
        or approval.ai_tool_execution_revision != run.ai_tool_execution_revision
    ):
        raise RoomToolApprovalError("approval_scope_changed")


async def room_tool_policy(db, *, room, source, tool_name: str, arguments: dict):
    """The room owns consent; requester membership/ACL is checked separately."""
    from packages.core.governance.service import check_step_policy
    binding = room.approval_policy if isinstance(room.approval_policy, dict) else {}
    root_entity_id = str(binding.get("root_entity_id") or "")
    operation = "delete" if tool_name == "room_delete_file" else "modify" if tool_name == "room_replace_file" else "create"
    return await check_step_policy(
        db, entity_id=root_entity_id, room_id=room.id, workspace_id=None,
        user_id=source.sender_user_id, kind="action", action_key=f"room.file.{operation}",
        resource_kind="room_file", resource_id=str(arguments.get("file_id") or "") or None,
        capability_id="file.write", operation=operation,
        risk_level="high" if operation == "delete" else "medium",
    )


async def request_room_tool_approval(
    db: AsyncSession, *, run_id: str, tool_call_id: str, tool_name: str,
    arguments: dict[str, Any], display_summary: str,
) -> ChatRoomToolApproval:
    """Reserve one pending call; caller commits before returning suspension.

    The runtime next persists the checkpoint with ``park_room_tool_approval``.
    An orphan reservation cannot be approved because decisions require a
    matching waiting run and checkpoint.
    """
    if tool_name not in ROOM_MUTATION_TOOLS:
        raise RoomToolApprovalError("tool_not_approvable")
    _validate_new_file_tool(tool_name, arguments)
    if not tool_call_id or len(tool_call_id) > 255:
        raise RoomToolApprovalError("invalid_tool_call")
    summary = display_summary.strip() if isinstance(display_summary, str) else ""
    if not summary or len(summary) > 500:
        raise RoomToolApprovalError("invalid_display_summary")
    digest = canonical_arguments_hash(arguments)
    run = await _locked_run(db, run_id)
    if run.status != "running":
        raise RoomToolApprovalError("run_not_running")
    room, source, _ = await _validated_scope(db, run)
    policy = await room_tool_policy(db, room=room, source=source, tool_name=tool_name, arguments=arguments)
    if not policy.allowed and not policy.pause_for_hitl:
        raise RoomToolApprovalError("blocked_by_governance")
    existing = (await db.execute(select(ChatRoomToolApproval).where(
        ChatRoomToolApproval.run_id == run.id,
    ).with_for_update())).scalars().all()
    for approval in existing:
        if approval.tool_call_id == tool_call_id:
            _check_approval_binding(approval, run, source)
            if approval.tool_name != tool_name or approval.arguments_hash != digest:
                raise RoomToolApprovalError("tool_call_changed")
            if approval.status != "pending":
                raise RoomToolApprovalError("tool_call_already_decided")
            return approval
    if any(approval.status in {"pending", "approved", "claimed"} for approval in existing):
        raise RoomToolApprovalError("another_tool_call_waiting")
    from packages.core.governance.approvals import _consent_requirements
    approval = ChatRoomToolApproval(
        approval_requirements=_consent_requirements(policy),
        id=generate_ulid(), room_id=room.id, run_id=run.id,
        requester_user_id=source.sender_user_id, binding_id=run.binding_id,
        audience_fingerprint=run.audience_fingerprint,
        ai_tool_execution_revision=run.ai_tool_execution_revision,
        tool_call_id=tool_call_id, tool_name=tool_name,
        arguments_hash=digest, display_summary=summary, status="pending",
    )
    db.add(approval)
    await db.flush()
    return approval


async def park_room_tool_approval(
    db: AsyncSession, *, run_id: str, approval_id: str,
    checkpoint: dict[str, Any],
) -> ChatRoomToolApproval:
    """Bind the Runtime checkpoint to the exact pending tool call."""
    if not isinstance(checkpoint, dict):
        raise RoomToolApprovalError("invalid_checkpoint")
    try:
        checkpoint_bytes = json.dumps(
            checkpoint, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RoomToolApprovalError("invalid_checkpoint") from exc
    if len(checkpoint_bytes) > _MAX_CHECKPOINT_BYTES:
        raise RoomToolApprovalError("checkpoint_too_large")
    pending = checkpoint.get("pending_tool_call")
    if not isinstance(pending, dict):
        raise RoomToolApprovalError("invalid_checkpoint")
    run = await _locked_run(db, run_id)
    if run.status != "running":
        raise RoomToolApprovalError("run_not_running")
    _, source, _ = await _validated_scope(db, run)
    approval = (await db.execute(select(ChatRoomToolApproval).where(
        ChatRoomToolApproval.id == approval_id,
        ChatRoomToolApproval.run_id == run.id,
    ).with_for_update())).scalar_one_or_none()
    if approval is None or approval.status != "pending":
        raise RoomToolApprovalError("approval_unavailable")
    _check_approval_binding(approval, run, source)
    if (
        pending.get("id") != approval.tool_call_id
        or pending.get("name") != approval.tool_name
        or canonical_arguments_hash(pending.get("arguments")) != approval.arguments_hash
    ):
        raise RoomToolApprovalError("checkpoint_tool_changed")
    run.checkpoint = checkpoint
    run.status = "waiting_approval"
    return approval


async def invalidate_stale_room_tool_approvals(db: AsyncSession, *, room_id: str) -> None:
    """Retire old waits after a setting revision changes, using run -> room locks."""
    run_ids = (await db.execute(select(ChatRoomAgentRun.id).join(
        ChatRoomToolApproval, ChatRoomToolApproval.run_id == ChatRoomAgentRun.id,
    ).where(
        ChatRoomAgentRun.room_id == room_id,
        ChatRoomAgentRun.status == "waiting_approval",
        ChatRoomToolApproval.status == "pending",
    ))).scalars().all()
    for run_id in run_ids:
        run = await _locked_run(db, run_id)
        if run.status != "waiting_approval":
            await db.rollback()
            continue
        room = (await db.execute(select(ChatRoom).where(
            ChatRoom.id == room_id,
        ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
        if room is None or (
            room.status == "active" and room.ai_tool_execution_enabled
            and run.ai_tool_execution_revision == room.ai_tool_execution_revision
        ):
            await db.rollback()
            continue
        approvals = (await db.execute(select(ChatRoomToolApproval).where(
            ChatRoomToolApproval.run_id == run.id,
            ChatRoomToolApproval.status.in_(("pending", "approved")),
        ).with_for_update())).scalars().all()
        for approval in approvals:
            approval.status = "invalidated"
            approval.finished_at = datetime.now(timezone.utc)
        run.status = "interrupted"
        run.error_code = "execution_setting_changed"
        run.checkpoint = None
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()


async def decide_room_tool_approval(
    db: AsyncSession, *, room_id: str, approval_id: str,
    approver_user_id: str, approve: bool,
) -> tuple[ChatRoomToolApproval, bool]:
    """CAS a pending approval; true return flag means enqueue the resumed run."""
    lookup = (await db.execute(select(ChatRoomToolApproval.run_id).where(
        ChatRoomToolApproval.id == approval_id,
        ChatRoomToolApproval.room_id == room_id,
    ))).scalar_one_or_none()
    if lookup is None:
        raise RoomToolApprovalError("approval_unavailable")
    run = await _locked_run(db, lookup)
    room, source, _ = await _validated_scope(db, run)
    if room.id != room_id:
        raise RoomToolApprovalError("approval_unavailable")
    approver = (await db.execute(select(User).where(
        User.id == approver_user_id, User.status == "active", User.deleted_at.is_(None),
    ))).scalar_one_or_none()
    member = (await db.execute(select(ChatRoomMember).where(
        ChatRoomMember.room_id == room.id,
        ChatRoomMember.user_id == approver_user_id,
        ChatRoomMember.status == "active",
    ))).scalar_one_or_none()
    if approver is None or member is None:
        raise RoomToolApprovalError("approval_unavailable")
    if member.role not in {"owner", "admin"}:
        raise RoomToolApprovalError("approver_not_authorized")
    approval = (await db.execute(select(ChatRoomToolApproval).where(
        ChatRoomToolApproval.id == approval_id,
        ChatRoomToolApproval.room_id == room_id,
    ).with_for_update())).scalar_one()
    _check_approval_binding(approval, run, source)
    if approval.status != "pending":
        if approval.status == ("approved" if approve else "rejected"):
            return approval, False
        raise RoomToolApprovalError("approval_already_decided")
    if run.status != "waiting_approval" or not isinstance(run.checkpoint, dict):
        raise RoomToolApprovalError("run_not_waiting")
    pending = run.checkpoint.get("pending_tool_call")
    if (
        not isinstance(pending, dict)
        or pending.get("id") != approval.tool_call_id
        or pending.get("name") != approval.tool_name
        or canonical_arguments_hash(pending.get("arguments")) != approval.arguments_hash
    ):
        raise RoomToolApprovalError("checkpoint_tool_changed")
    now = datetime.now(timezone.utc)
    approval.status = "approved" if approve else "rejected"
    approval.decided_by_user_id = approver_user_id
    approval.decided_at = now
    if approve:
        run.status = "pending"
    else:
        run.status = "interrupted"
        run.error_code = "tool_approval_rejected"
        run.checkpoint = None
        run.completed_at = now
    db.add(AuditLog(
        id=generate_ulid(), entity_id=room.owner_entity_id,
        user_id=approver_user_id,
        action=f"chat_room.tool_approval.{approval.status}",
        resource_type="chat_room_tool_approval", resource_id=approval.id,
        details={
            "room_id": room.id, "run_id": run.id,
            "tool_name": approval.tool_name,
            "arguments_hash": approval.arguments_hash,
            "revision": approval.ai_tool_execution_revision,
        },
    ))
    return approval, approve


async def claim_approved_room_tool_call(
    db: AsyncSession, *, run_id: str, tool_call_id: str,
    tool_name: str, arguments: dict[str, Any],
) -> ChatRoomToolApproval:
    """Commit an exact approved→claimed transition BEFORE provider I/O.

    A process crash after this commit is ambiguous. Never automatically
    reclaim or repeat a claimed mutation.
    """
    _validate_new_file_tool(tool_name, arguments)
    digest = canonical_arguments_hash(arguments)
    run = await _locked_run(db, run_id)
    if run.status != "running":
        raise RoomToolApprovalError("run_not_running")
    room, source, _ = await _validated_scope(db, run)
    policy = await room_tool_policy(db, room=room, source=source, tool_name=tool_name, arguments=arguments)
    if not policy.allowed and not policy.pause_for_hitl:
        raise RoomToolApprovalError("blocked_by_governance")
    approval = (await db.execute(select(ChatRoomToolApproval).where(
        ChatRoomToolApproval.run_id == run_id,
        ChatRoomToolApproval.tool_call_id == tool_call_id,
    ).with_for_update())).scalar_one_or_none()
    if approval is None or approval.status != "approved":
        raise RoomToolApprovalError("tool_call_not_approved")
    from packages.core.governance.approvals import _consent_requirements
    if approval.approval_requirements is None or not set(_consent_requirements(policy)).issubset(set(approval.approval_requirements)):
        approval.status = "invalidated"
        await db.commit()
        raise RoomToolApprovalError("approval_requirements_changed")
    _check_approval_binding(approval, run, source)
    if approval.tool_name != tool_name or approval.arguments_hash != digest:
        raise RoomToolApprovalError("tool_call_changed")
    pending = run.checkpoint.get("pending_tool_call") if isinstance(run.checkpoint, dict) else None
    if (
        not isinstance(pending, dict)
        or pending.get("id") != tool_call_id
        or pending.get("name") != tool_name
        or canonical_arguments_hash(pending.get("arguments")) != digest
    ):
        raise RoomToolApprovalError("checkpoint_tool_changed")
    approver = (await db.execute(select(ChatRoomMember).join(
        User, User.id == ChatRoomMember.user_id,
    ).where(
        ChatRoomMember.room_id == run.room_id,
        ChatRoomMember.user_id == approval.decided_by_user_id,
        ChatRoomMember.status == "active",
        ChatRoomMember.role.in_(("owner", "admin")),
        User.status == "active", User.deleted_at.is_(None),
    ))).scalar_one_or_none()
    if approver is None:
        raise RoomToolApprovalError("approver_not_authorized")
    approval.status = "claimed"
    approval.claimed_at = datetime.now(timezone.utc)
    await db.commit()
    return approval


async def claim_direct_room_tool_call(
    db: AsyncSession, *, run_id: str, tool_call_id: str,
    tool_name: str, arguments: dict[str, Any], display_summary: str,
) -> ChatRoomToolApproval:
    """Commit one owner/admin room-file claim before its side effect.

    A claimed call may have produced an effect before a crash. No status,
    including failed, permits an automatic replay of the same exact call.
    Existing pending approvals remain on their explicit decision path.
    """
    if tool_name not in ROOM_MUTATION_TOOLS:
        raise RoomToolApprovalError("tool_not_approvable")
    _validate_new_file_tool(tool_name, arguments)
    if not tool_call_id or len(tool_call_id) > 255:
        raise RoomToolApprovalError("invalid_tool_call")
    summary = display_summary.strip() if isinstance(display_summary, str) else ""
    if not summary or len(summary) > 500:
        raise RoomToolApprovalError("invalid_display_summary")
    digest = canonical_arguments_hash(arguments)
    run = await _locked_run(db, run_id)
    if run.status != "running":
        raise RoomToolApprovalError("run_not_running")
    room, source, _ = await _validated_scope(db, run)
    policy = await room_tool_policy(db, room=room, source=source, tool_name=tool_name, arguments=arguments)
    if not policy.allowed:
        raise RoomToolApprovalError("approval_required" if policy.pause_for_hitl else "blocked_by_governance")
    existing = (await db.execute(select(ChatRoomToolApproval).where(
        ChatRoomToolApproval.run_id == run.id,
    ).with_for_update())).scalars().all()
    if any(approval.tool_call_id == tool_call_id and (
        approval.tool_name != tool_name or approval.arguments_hash != digest
    ) for approval in existing):
        raise RoomToolApprovalError("tool_call_changed")
    if any(
        approval.tool_call_id == tool_call_id
        or (approval.tool_name == tool_name and approval.arguments_hash == digest)
        for approval in existing
    ):
        raise RoomToolApprovalError("tool_call_already_decided")
    if any(approval.status in {"pending", "approved", "claimed"} for approval in existing):
        raise RoomToolApprovalError("another_tool_call_waiting")
    now = datetime.now(timezone.utc)
    receipt = ChatRoomToolApproval(
        id=generate_ulid(), room_id=room.id, run_id=run.id,
        requester_user_id=source.sender_user_id, binding_id=run.binding_id,
        audience_fingerprint=run.audience_fingerprint,
        ai_tool_execution_revision=run.ai_tool_execution_revision,
        tool_call_id=tool_call_id, tool_name=tool_name,
        arguments_hash=digest, display_summary=summary, status="claimed",
        decided_by_user_id=source.sender_user_id, decided_at=now, claimed_at=now,
    )
    db.add(receipt)
    await db.commit()
    return receipt


async def finish_room_tool_call(
    db: AsyncSession, *, approval_id: str, succeeded: bool,
    result_document_id: str | None = None, result_media_job_id: str | None = None,
) -> ChatRoomToolApproval:
    """Store terminal receipt; never transition a claimed call to approved."""
    approval = (await db.execute(select(ChatRoomToolApproval).where(
        ChatRoomToolApproval.id == approval_id,
    ).with_for_update())).scalar_one_or_none()
    if approval is None or approval.status != "claimed":
        raise RoomToolApprovalError("tool_call_not_claimed")
    if succeeded and approval.tool_name in {"room_create_file", "room_replace_file", "generate_file"}:
        if (result_document_id is None) == (result_media_job_id is None):
            raise RoomToolApprovalError("result_identity_required")
    if result_document_id is not None and len(result_document_id) != 26:
        raise RoomToolApprovalError("invalid_result_identity")
    if result_media_job_id is not None and len(result_media_job_id) != 26:
        raise RoomToolApprovalError("invalid_result_identity")
    approval.status = "succeeded" if succeeded else "failed"
    if succeeded:
        approval.result_document_id = result_document_id
        approval.result_media_job_id = result_media_job_id
    approval.finished_at = datetime.now(timezone.utc)
    await db.commit()
    return approval
