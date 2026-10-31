"""Person-to-person and group chat, independent of Workspace membership."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Awaitable, BinaryIO, Callable, Literal
from urllib.parse import quote

import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from apps.api.deps import get_current_user
from apps.api.file_responses import PinnedFileStreamingResponse, room_file_snapshot_budget
from apps.api.web_base import public_web_base
from packages.core.constants.agents import MANOR_AGENT_ID, MANOR_AGENT_NAME, is_master_agent
from packages.core.constants.notification_types import NotificationDispatchStatus
from packages.core import database
from packages.core.database import get_db
from packages.core.models.base import generate_ulid
from packages.core.models.audit import AuditLog
from packages.core.models.notification import Notification
from packages.core.models.chat_room import ChatRoom, ChatRoomAgentBinding, ChatRoomAgentRun, ChatRoomFile, ChatRoomInvite, ChatRoomMember, ChatRoomMessage, ChatRoomMessageReaction, ChatRoomToolApproval
from packages.core.models.document import Document
from packages.core.models.permission import Capability, Classification
from packages.core.models.staff import Staff
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.user_session import UserSessionLease, UserSessionLog
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.permissions import Permission, has_permission, user_staff_role_assignment
from packages.core.services.actor_authorization import AuthenticatedUserCredential, resolve_current_user_actor
from packages.core.services.agent_subscription_service import ensure_default_subscription, ensure_personal_subscription
from packages.core.services.conversation_transcript import discard_room_transcripts
from packages.core.services.document_access import effective_document_folder_policy, user_has_document_capability
from packages.core.services.document_service import DocumentMutationConflictError, StorageLimitExceeded, get_document_for_read
from packages.core.services.connected_email_accounts import (
    ConnectedEmailAccountsUnavailable, available_email_senders, default_email_sender, send_email_from_account,
)
from packages.core.services.email_service import chat_room_invite_email_content
from packages.core.services.notification_service import create_notification
from packages.core.services.entity_fs import (
    EntityFilesystemBusyError,
    EntityFilesystemError,
    canonical_entity_root,
    entity_filesystem_read_lock,
    open_entity_file_snapshot,
)
from packages.core.services.permission_gate import DocumentDownloadNotAllowed, ResourcePermissionGate
from packages.core.services.room_agent_runtime import room_agent_reply_policy, room_audience_fingerprint
from packages.core.services.room_agent_progress import room_agent_process_blocks
from packages.core.services.realtime import queue_chat_room_change_after_commit
from packages.core.services.room_reply_approval_service import (
    RoomReplyApprovalError, decide_room_reply_approval, pending_room_reply_approvals,
)
from packages.core.tasks.room_agent_tasks import enqueue_room_agent_run
from packages.core.services.room_tool_approval_service import (
    RoomToolApprovalError, decide_room_tool_approval, invalidate_stale_room_tool_approvals,
)
from packages.core.services.workspace_access import lock_workspace_access_boundary, user_can_manage_workspace
from packages.core.services.upload_security import UploadSecurityError, inspect_upload_content, inspect_upload_stream
from packages.core.services.user_avatars import person_avatar_url, real_avatar_url
from packages.core.services.room_file_storage import (
    ROOM_FILE_CHUNK_BYTES, ROOM_FILE_INLINE_MAX_BYTES, ROOM_FILE_MAX_BYTES,
    ROOM_FILES_TOTAL_MAX_BYTES, RoomFileStorageError, copy_room_file_content,
    store_room_file_chunks,
)


router = APIRouter(prefix="/api/v1/chat/rooms", tags=["chat-rooms"])


async def _commit_room_change(
    db: AsyncSession, room_id: str, *, kind: str = "changed", message_id: str | None = None,
) -> None:
    queue_chat_room_change_after_commit(db, room_id, kind=kind, message_id=message_id)
    await db.commit()


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_INVITE_LIFETIME = timedelta(days=7)
_ROOM_FILE_MAX_BYTES = ROOM_FILE_MAX_BYTES
_ROOM_FILES_TOTAL_MAX_BYTES = ROOM_FILES_TOTAL_MAX_BYTES

def _valid_reaction_emoji(value: str) -> bool:
    if not 1 <= len(value) <= 16:
        return False
    if any(char.isspace() or char.isalpha() for char in value):
        return False
    if any(char.isdigit() for char in value) and "\u20e3" not in value:
        return False
    if any(
        unicodedata.category(char).startswith("C")
        and char != "\u200d"
        and not 0xE0020 <= ord(char) <= 0xE007F
        for char in value
    ):
        return False
    return any(
        unicodedata.category(char).startswith("S")
        or char == "\u20e3"
        or (ord(char) > 127 and unicodedata.category(char).startswith("P"))
        for char in value
    )


class CreateRoomRequest(BaseModel):
    kind: Literal["dm", "group"]
    recipient_email: str = Field(min_length=3, max_length=255)
    title: str | None = Field(default=None, max_length=255)
    owner_scope: Literal["personal", "entity"] | None = None
    sender_integration_id: str | None = Field(default=None, max_length=255)


class UpdateRoomRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class UpdateRoomAiExecutionRequest(BaseModel):
    enabled: StrictBool
    expected_revision: int = Field(ge=0)


class ReadRoomRequest(BaseModel):
    through_message_id: str = Field(min_length=26, max_length=26)


class UpdateRoomNotificationsRequest(BaseModel):
    muted: StrictBool


class RoomRetractionsRequest(BaseModel):
    message_ids: list[str] = Field(max_length=1000)


class RoomToolApprovalDecisionRequest(BaseModel):
    approve: StrictBool


class RoomReplyApprovalDecisionRequest(BaseModel):
    approve: StrictBool
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class InviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    sender_integration_id: str | None = Field(default=None, max_length=255)


class UpdateMemberRoleRequest(BaseModel):
    role: Literal["admin", "member"]


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)


class PublicInviteRequest(BaseModel):
    reset: StrictBool = False


class PostMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    mentioned_user_ids: list[str] = Field(default_factory=list, max_length=20)
    mentioned_agent_binding_ids: list[str] = Field(default_factory=list, max_length=5)
    reply_to_message_id: str | None = Field(default=None, min_length=26, max_length=26)


class KnowledgeRoomFileRequest(BaseModel):
    document_id: str = Field(min_length=26, max_length=26)
    content: str = Field(default="", max_length=4000)
    mentioned_user_ids: list[str] = Field(default_factory=list, max_length=20)
    mentioned_agent_binding_ids: list[str] = Field(default_factory=list, max_length=5)
    reply_to_message_id: str | None = Field(default=None, min_length=26, max_length=26)


class RoomReactionRequest(BaseModel):
    emoji: str = Field(min_length=1, max_length=16)


class BindAgentRequest(BaseModel):
    subscription_id: str = Field(min_length=26, max_length=26)


def _can_manage_bindings(room: ChatRoom, member: ChatRoomMember, user: User) -> bool:
    if room.kind != "group" or member.role not in {"owner", "admin"}:
        return False
    return room.owner_entity_id == user.entity_id if room.owner_entity_id else room.owner_user_id == user.id


def _can_manage_ai_execution(room: ChatRoom, member: ChatRoomMember, user: User) -> bool:
    return member.role == "owner" and _can_manage_bindings(room, member, user)


async def _binding_view(db: AsyncSession, binding: ChatRoomAgentBinding, *, manager: bool) -> dict:
    row = (await db.execute(
        select(AgentSubscription, Agent, Workspace)
        .join(Agent, Agent.id == AgentSubscription.agent_id)
        .outerjoin(Workspace, Workspace.id == AgentSubscription.workspace_id)
        .where(AgentSubscription.id == binding.subscription_id)
    )).one_or_none()
    if row is None:
        return {"id": binding.id, "name": "Unavailable Agent", "available": False}
    subscription, agent, workspace = row
    available = (
        binding.status == "active" and subscription.status == "active"
        and agent.status == "active" and agent.deleted_at is None
        and (agent.entity_id == subscription.entity_id or (
            is_master_agent(agent.id) and agent.entity_id is None
        ))
        and (subscription.workspace_id is None or (
            workspace is not None and workspace.deleted_at is None
            and workspace.entity_id == subscription.entity_id
            and workspace.status in {"active", "paused"}
        ))
    )
    if available and workspace is not None:
        decision = await room_agent_reply_policy(
            db, workspace_id=workspace.id, room_id=binding.room_id,
        )
        available = decision.allowed or decision.pause_for_hitl
    result = {
        "id": binding.id,
        "name": (MANOR_AGENT_NAME if is_master_agent(agent.id) else
                 (agent.name if subscription.workspace_id is None else subscription.name or agent.name)),
        "avatar_url": agent.avatar_url,
        "available": available,
        "is_default": is_master_agent(agent.id),
        # The built-in group assistant must never inherit the personal or
        # Workspace file context of the subscription used as its identity.
        "context_scope": "room_only" if is_master_agent(agent.id) else "agent_and_group_audience",
    }
    if manager:
        result.update({
            "subscription_id": binding.subscription_id,
            "workspace_name": workspace.name if workspace else None,
            "status": binding.status,
        })
    return result


def _email(value: str) -> str:
    normalized = value.strip().casefold()
    if len(normalized) > 255 or not _EMAIL_RE.fullmatch(normalized):
        raise HTTPException(422, "Enter a valid email address")
    return normalized


def _dm_key(left: str, right: str) -> str:
    return "dm:" + ":".join(sorted((left, right)))


def _file_view(file, *, room_id: str, sender_name: str) -> dict:
    return {
        "id": file.id,
        "message_id": file.message_id,
        "name": file.name,
        "size": file.size,
        "mime_type": file.mime_type,
        "sender_name": sender_name,
        "created_at": file.created_at,
        "download_url": f"/api/v1/chat/rooms/{room_id}/files/{file.id}",
    }


def _can_remove_room_file(member: ChatRoomMember, sender_user_id: str | None, user_id: str) -> bool:
    return member.role in {"owner", "admin"} or sender_user_id == user_id


async def _lock_dm_key(db: AsyncSession, key: str) -> None:
    # Serializes both direct creation and acceptance of a pending email invite.
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))


async def _active_room(
    db: AsyncSession, room_id: str, user_id: str, *, write: bool = False,
) -> tuple[ChatRoom, ChatRoomMember]:
    query = (
        select(ChatRoom, ChatRoomMember)
        .join(ChatRoomMember, ChatRoomMember.room_id == ChatRoom.id)
        .where(
            ChatRoom.id == room_id,
            ChatRoom.status == "active",
            ChatRoomMember.user_id == user_id,
            ChatRoomMember.status == "active",
        )
        .with_for_update(read=not write, of=(ChatRoom, ChatRoomMember))
    )
    row = (await db.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(404, "Conversation not found")
    room, member = row
    if room.owner_entity_id:
        owner = (await db.execute(select(Entity.id).where(
            Entity.id == room.owner_entity_id, Entity.deleted_at.is_(None),
        ))).scalar_one_or_none()
    else:
        owner = (await db.execute(select(User.id).where(
            User.id == room.owner_user_id, User.deleted_at.is_(None), User.status == "active",
        ))).scalar_one_or_none()
    if owner is None:
        raise HTTPException(404, "Conversation not found")
    return room, member


async def _person_avatar_urls(
    db: AsyncSession, users: list[User | None], *, owner_entity_id: str | None,
) -> dict[str, str | None]:
    """Each person's avatar by the shared people rule.

    Without an own avatar, the staff photo comes from the room's owning Entity
    (whose team page the members share), else from the person's home Entity.
    """
    people = {user.id: user for user in users if user is not None}
    missing = [user.id for user in people.values() if not real_avatar_url(user.avatar_url)]
    staff_photos: dict[tuple[str, str], str] = {}
    if missing:
        rows = (await db.execute(select(Staff.user_id, Staff.entity_id, Staff.avatar_url).where(
            Staff.user_id.in_(missing), Staff.deleted_at.is_(None), Staff.avatar_url.is_not(None),
        ).order_by(Staff.created_at.desc()))).all()
        for staff_user_id, entity_id, avatar_url in rows:
            staff_photos.setdefault((staff_user_id, entity_id), avatar_url)
    return {
        user.id: person_avatar_url(
            user.avatar_url,
            (owner_entity_id and staff_photos.get((user.id, owner_entity_id)))
            or staff_photos.get((user.id, user.entity_id)),
        )
        for user in people.values()
    }


async def _room_view(db: AsyncSession, room: ChatRoom, user_id: str) -> dict:
    members = (await db.execute(
        select(ChatRoomMember, User).join(User, User.id == ChatRoomMember.user_id)
        .where(ChatRoomMember.room_id == room.id, ChatRoomMember.status == "active")
        .order_by(ChatRoomMember.joined_at, ChatRoomMember.id)
    )).all()
    other = next((user for member, user in members if user.id != user_id), None)
    actor = next((member for member, user in members if user.id == user_id), None)
    actor_user = next((user for member, user in members if user.id == user_id), None)
    manager = bool(actor and actor_user and _can_manage_bindings(room, actor, actor_user))
    ai_execution_manager = bool(actor and actor_user and _can_manage_ai_execution(room, actor, actor_user))
    bindings = (await db.execute(select(ChatRoomAgentBinding).where(
        ChatRoomAgentBinding.room_id == room.id,
        ChatRoomAgentBinding.status == "active",
    ).order_by(ChatRoomAgentBinding.created_at, ChatRoomAgentBinding.id))).scalars().all()
    latest_message_at = (await db.execute(select(ChatRoomMessage.created_at).where(
        ChatRoomMessage.room_id == room.id,
    ).order_by(ChatRoomMessage.created_at.desc(), ChatRoomMessage.id.desc()).limit(1))).scalar_one_or_none()
    avatars = await _person_avatar_urls(db, [user for _, user in members], owner_entity_id=room.owner_entity_id)
    return {
        "id": room.id,
        "kind": room.kind,
        "title": room.title if room.kind == "group" else (
            (other.display_name or other.email) if other else room.title or "Pending private chat"
        ),
        "owner_scope": "entity" if room.owner_entity_id else "personal",
        "member_count": len(members),
        "members": [{
            "user_id": user.id,
            "name": user.display_name or user.email,
            "avatar_url": avatars[user.id],
            "role": member.role,
        } for member, user in members],
        "agents": [await _binding_view(db, binding, manager=manager) for binding in bindings],
        "can_manage_agents": manager,
        "ai_tool_execution_enabled": room.ai_tool_execution_enabled if room.kind == "group" else False,
        "ai_file_actions_direct_enabled": room.ai_file_actions_direct_enabled if room.kind == "group" else False,
        "ai_tool_execution_revision": room.ai_tool_execution_revision if room.kind == "group" else 0,
        "can_manage_ai_execution": ai_execution_manager,
        "notifications_muted": bool(actor and actor.notifications_muted),
        "last_message_at": latest_message_at,
        "created_at": room.created_at,
    }


async def _online_room_members(db: AsyncSession, room_ids: list[str]) -> dict[str, set[str]]:
    if not room_ids:
        return {}
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(ChatRoomMember.room_id, UserSessionLease.user_id)
        .join(UserSessionLease, UserSessionLease.user_id == ChatRoomMember.user_id)
        .join(UserSessionLog, UserSessionLog.id == UserSessionLease.session_id)
        .join(User, User.id == ChatRoomMember.user_id)
        .where(
            ChatRoomMember.room_id.in_(room_ids),
            ChatRoomMember.status == "active",
            User.status == "active",
            User.deleted_at.is_(None),
            UserSessionLease.expires_at > now,
            UserSessionLog.user_id == UserSessionLease.user_id,
            UserSessionLog.entity_id == UserSessionLease.entity_id,
            UserSessionLog.status == "active",
            UserSessionLog.ended_at.is_(None),
        )
        .distinct()
    )).all()
    by_room: dict[str, set[str]] = {}
    for room_id, user_id in rows:
        by_room.setdefault(room_id, set()).add(user_id)
    return by_room


@router.get("/{room_id}/agent-candidates")
async def agent_candidates(
    room_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id)
    if not _can_manage_bindings(room, member, user):
        raise HTTPException(403, "Cannot manage group Agents")
    rows = (await db.execute(
        select(AgentSubscription, Agent, Workspace)
        .join(Agent, Agent.id == AgentSubscription.agent_id)
        .outerjoin(Workspace, Workspace.id == AgentSubscription.workspace_id)
        .where(AgentSubscription.entity_id == user.entity_id,
               Agent.entity_id == user.entity_id,
               AgentSubscription.status == "active", Agent.status == "active",
               Agent.deleted_at.is_(None))
        .order_by(Agent.name, AgentSubscription.id).limit(200)
    )).all()
    candidates = []
    for subscription, agent, workspace in rows:
        if workspace:
            if (workspace.entity_id != user.entity_id or workspace.deleted_at is not None
                    or workspace.status not in {"active", "paused"}):
                continue
            if not await user_can_manage_workspace(
                db, workspace_id=workspace.id, user_id=user.id, entity_role=user.role,
            ):
                continue
            decision = await room_agent_reply_policy(
                db, workspace_id=workspace.id, room_id=room.id,
            )
            if not (decision.allowed or decision.pause_for_hitl):
                continue
        elif subscription.workspace_id is not None or subscription.owner_user_id != user.id:
            continue
        candidates.append({
            "subscription_id": subscription.id,
            "name": agent.name if subscription.workspace_id is None else subscription.name or agent.name,
            "workspace_name": workspace.name if workspace else None,
        })
    return candidates


@router.post("/{room_id}/agents", status_code=201)
async def bind_agent(
    room_id: str,
    body: BindAgentRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id, write=True)
    if not _can_manage_bindings(room, member, user):
        raise HTTPException(403, "Cannot manage group Agents")
    row = (await db.execute(
        select(AgentSubscription, Agent)
        .join(Agent, Agent.id == AgentSubscription.agent_id)
        .where(AgentSubscription.id == body.subscription_id,
               AgentSubscription.entity_id == user.entity_id,
               Agent.entity_id == user.entity_id,
               AgentSubscription.status == "active", Agent.status == "active",
               Agent.deleted_at.is_(None))
    )).one_or_none()
    if row is None:
        raise HTTPException(404, "Agent deployment not found")
    subscription, _ = row
    if subscription.workspace_id:
        workspace = await lock_workspace_access_boundary(
            db, workspace_id=subscription.workspace_id, entity_id=user.entity_id,
        )
        if workspace is None or workspace.deleted_at is not None or workspace.status not in {"active", "paused"}:
            raise HTTPException(404, "Agent deployment not found")
        if not await user_can_manage_workspace(
            db, workspace_id=workspace.id, user_id=user.id, entity_role=user.role,
        ):
            raise HTTPException(403, "Workspace management access required")
        decision = await room_agent_reply_policy(
            db, workspace_id=workspace.id, room_id=room.id,
        )
        if not (decision.allowed or decision.pause_for_hitl):
            raise HTTPException(403, "Agent cannot reply in this group under Workspace policy")
    elif subscription.owner_user_id != user.id:
        raise HTTPException(403, "Only your personal Agent can be added")
    binding = (await db.execute(select(ChatRoomAgentBinding).where(
        ChatRoomAgentBinding.room_id == room_id,
        ChatRoomAgentBinding.subscription_id == body.subscription_id,
    ).with_for_update())).scalar_one_or_none()
    if binding is None:
        binding = ChatRoomAgentBinding(
            id=generate_ulid(), room_id=room_id, subscription_id=body.subscription_id,
            added_by_user_id=user.id, status="active",
        )
        db.add(binding)
    else:
        binding.status = "active"
    await _commit_room_change(db, room_id)
    return await _binding_view(db, binding, manager=True)


@router.delete("/{room_id}/agents/{binding_id}", status_code=204)
async def remove_agent_binding(
    room_id: str,
    binding_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id, write=True)
    if not _can_manage_bindings(room, member, user):
        raise HTTPException(403, "Cannot manage group Agents")
    binding = (await db.execute(select(ChatRoomAgentBinding).where(
        ChatRoomAgentBinding.id == binding_id, ChatRoomAgentBinding.room_id == room_id,
    ).with_for_update())).scalar_one_or_none()
    if binding is None:
        raise HTTPException(404, "Agent binding not found")
    subscription = (await db.execute(select(AgentSubscription).where(
        AgentSubscription.id == binding.subscription_id,
    ))).scalar_one_or_none()
    if subscription and is_master_agent(subscription.agent_id):
        raise HTTPException(403, "Default Manor AI cannot be removed")
    await discard_room_transcripts(db, room_id, stream_key_prefix=f"room|{binding.id}|")
    await db.delete(binding)
    await _commit_room_change(db, room_id)


def _new_invite(room: ChatRoom, email: str, actor_id: str) -> tuple[ChatRoomInvite, str]:
    token = secrets.token_urlsafe(32)
    return ChatRoomInvite(
        id=generate_ulid(), room_id=room.id, email=email,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        invited_by_user_id=actor_id, status="pending",
        expires_at=datetime.now(timezone.utc) + _INVITE_LIFETIME,
    ), token


def _verified_room_recipient(recipient: User | None, email: str) -> bool:
    """A group membership by email needs proof of the account's current address."""
    return bool(
        recipient is not None
        and recipient.status == "active"
        and recipient.deleted_at is None
        and recipient.email.casefold() == email
        and (recipient.verified_email or "").casefold() == email
        and recipient.email_verified_at is not None
    )


async def _record_group_invite_notification(
    db: AsyncSession, *, invite: ChatRoomInvite, room: ChatRoom,
    recipient: User, inviter: User, token: str,
) -> None:
    await create_notification(
        db, recipient.entity_id, recipient.id,
        "chat_room_invite", "Group chat invitation",
        body=f"{inviter.display_name or inviter.email} invited you to {room.title}.",
        link=f"/chat?invite={quote(token, safe='')}",
        meta={"chat_room_invite_id": invite.id, "chat_room_id": room.id},
        idempotency_key=f"chat-room-invite:{invite.id}",
    )


async def _group_invite_senders(db: AsyncSession, user: User) -> list[tuple[str, str, str]]:
    try:
        return await available_email_senders(
            db, entity_id=user.entity_id, user_id=user.id, reason="group_invite_sender_check",
        )
    except ConnectedEmailAccountsUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


async def _select_group_invite_sender(
    db: AsyncSession, user: User, selected_id: str | None,
) -> tuple[str, str, str] | None:
    available = await _group_invite_senders(db, user)
    if selected_id:
        chosen = next((item for item in available if item[0] == selected_id), None)
        if chosen is None:
            raise HTTPException(409, "The selected sending account is no longer available")
        return chosen
    return await default_email_sender(db, entity_id=user.entity_id, user_id=user.id, available=available)


async def _send_group_invite_email(
    request: Request, *, invite: ChatRoomInvite, room: ChatRoom,
    inviter: User, token: str, sender: tuple[str, str, str] | None,
) -> bool:
    if sender is None:
        return False
    inviter_name = inviter.display_name or inviter.email
    return await send_email_from_account(
        sender=sender, entity_id=inviter.entity_id, user_id=inviter.id, to=invite.email,
        reason="group_invite_email_delivery",
        subject="You're invited to a Manor AI group chat", sender_name=inviter_name,
        content=chat_room_invite_email_content(
            room_name=room.title or "Group chat", inviter_name=inviter_name,
            invite_url=f"{public_web_base(request)}/chat?invite={quote(token, safe='')}",
        ),
    )


async def _retire_invite_notifications(db: AsyncSession, invites: list[ChatRoomInvite]) -> None:
    if invites:
        await db.execute(update(Notification).where(
            Notification.idempotency_key.in_([f"chat-room-invite:{invite.id}" for invite in invites]),
        ).values(dispatch_status=NotificationDispatchStatus.CANCELED.value))


async def _revoke_pending_invites(db: AsyncSession, room_id: str, email: str) -> None:
    # The room row is locked first by callers and by acceptance, so a stale
    # link cannot re-admit somebody after a manager removes them.
    pending = (await db.execute(select(ChatRoomInvite).where(
        ChatRoomInvite.room_id == room_id,
        ChatRoomInvite.email == email,
        ChatRoomInvite.status == "pending",
    ).with_for_update())).scalars().all()
    for invite in pending:
        invite.status = "revoked"
    await _retire_invite_notifications(db, pending)


async def _unread_room_counts(db: AsyncSession, user_id: str, room_ids: list[str]) -> dict[str, int]:
    if not room_ids:
        return {}
    rows = (await db.execute(select(
        ChatRoomMessage.room_id, func.count(ChatRoomMessage.id),
    ).join(ChatRoomMember, ChatRoomMember.room_id == ChatRoomMessage.room_id).where(
        ChatRoomMessage.room_id.in_(room_ids),
        ChatRoomMember.user_id == user_id,
        ChatRoomMember.status == "active",
        ChatRoomMessage.created_at >= ChatRoomMember.visible_from,
        ChatRoomMessage.created_at > func.coalesce(ChatRoomMember.last_read_at, ChatRoomMember.visible_from),
        ChatRoomMessage.sender_user_id.is_distinct_from(user_id),
        ChatRoomMessage.deleted_at.is_(None),
    ).group_by(ChatRoomMessage.room_id))).all()
    return {room_id: count for room_id, count in rows}


@router.get("")
async def list_rooms(
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    latest_message_at = (select(ChatRoomMessage.created_at).where(
        ChatRoomMessage.room_id == ChatRoom.id,
    ).order_by(ChatRoomMessage.created_at.desc(), ChatRoomMessage.id.desc()).limit(1)
        .correlate(ChatRoom).scalar_subquery())
    rooms = (await db.execute(
        select(ChatRoom)
        .join(ChatRoomMember, ChatRoomMember.room_id == ChatRoom.id)
        .where(ChatRoomMember.user_id == user.id, ChatRoomMember.status == "active", ChatRoom.status == "active")
        .order_by(func.coalesce(latest_message_at, ChatRoom.created_at).desc())
        .limit(100)
    )).scalars().all()
    visible = []
    for room in rooms:
        try:
            await _active_room(db, room.id, user.id)
        except HTTPException:
            continue
        visible.append(await _room_view(db, room, user.id))
    online_by_room = await _online_room_members(db, [room["id"] for room in visible])
    unread_by_room = await _unread_room_counts(db, user.id, [room["id"] for room in visible])
    for room in visible:
        room["online_user_ids"] = sorted(online_by_room.get(room["id"], set()))
        room["unread_count"] = unread_by_room.get(room["id"], 0)
    response.headers["Cache-Control"] = "private, no-store"
    return visible


@router.get("/group-usage")
async def group_chat_usage(
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Include left and closed rooms so past users do not get a launch hint."""
    member_id = await db.scalar(
        select(ChatRoomMember.id)
        .join(ChatRoom, ChatRoom.id == ChatRoomMember.room_id)
        .where(ChatRoomMember.user_id == user.id, ChatRoom.kind == "group")
        .limit(1)
    )
    response.headers["Cache-Control"] = "private, no-store"
    return {"used": member_id is not None}


@router.get("/invite-senders")
async def list_group_invite_senders(
    response: Response, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "private, no-store"
    return [
        {"id": sender_id, "email": address, "provider": provider}
        for sender_id, address, provider in await _group_invite_senders(db, user)
    ]


@router.post("", status_code=201)
async def create_room(
    body: CreateRoomRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    recipient_email = _email(body.recipient_email)
    if recipient_email == user.email.casefold():
        raise HTTPException(422, "Choose another person")
    recipient_query = select(User).where(func.lower(User.email) == recipient_email)
    if body.kind == "group":
        recipient_query = recipient_query.with_for_update()
    recipient = (await db.execute(recipient_query)).scalar_one_or_none()
    if body.kind == "group" and recipient and (
        recipient.deleted_at is not None or recipient.status not in {"active", "pending"}
    ):
        raise HTTPException(409, "This Manor account is unavailable")
    if body.kind == "dm" and recipient and (recipient.deleted_at is not None or recipient.status != "active"):
        recipient = None
    now = datetime.now(timezone.utc)
    if body.kind == "dm":
        if recipient:
            canonical_key = _dm_key(user.id, recipient.id)
            await _lock_dm_key(db, canonical_key)
            canonical = (await db.execute(select(ChatRoom).where(
                ChatRoom.dm_key == canonical_key, ChatRoom.status == "active",
            ))).scalar_one_or_none()
            if canonical:
                await _active_room(db, canonical.id, user.id)
                return await _room_view(db, canonical, user.id)
        same_company = bool(recipient and (await db.execute(select(UserMembership.id).where(
            UserMembership.user_id == recipient.id,
            UserMembership.entity_id == user.entity_id,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
        ))).scalar_one_or_none())
        if same_company and recipient:
            key = canonical_key
        else:
            key = f"pending:{user.id}:{recipient_email}"
            await _lock_dm_key(db, key)
            existing = (await db.execute(select(ChatRoom).where(
                ChatRoom.dm_key == key, ChatRoom.status == "active",
            ))).scalar_one_or_none()
            if existing:
                await _active_room(db, existing.id, user.id)
                return await _room_view(db, existing, user.id)
        room = ChatRoom(
            id=generate_ulid(), kind="dm", owner_user_id=user.id,
            created_by_user_id=user.id, dm_key=key,
            title=recipient_email if not same_company else None,
        )
    else:
        title = (body.title or "").strip()
        if not title or body.owner_scope is None:
            raise HTTPException(422, "Group name and owner are required")
        room = ChatRoom(
            id=generate_ulid(), kind="group", title=title,
            owner_entity_id=user.entity_id if body.owner_scope == "entity" else None,
            owner_user_id=user.id if body.owner_scope == "personal" else None,
            created_by_user_id=user.id,
        )
    sender = await _select_group_invite_sender(db, user, body.sender_integration_id) if body.kind == "group" else None
    from packages.core.governance.approval_policy_service import initialize_room_approval
    await initialize_room_approval(db, room)
    db.add(room)
    db.add(ChatRoomMember(
        id=generate_ulid(), room_id=room.id, user_id=user.id,
        role="owner", status="active", visible_from=now, last_read_at=now,
    ))
    if body.kind == "group":
        # Manor AI is implicit in each group; the concrete subscription gives
        # this room one stable runtime identity without Workspace membership.
        master_subscription = (
            await ensure_default_subscription(
                db, entity_id=user.entity_id, agent_id=MANOR_AGENT_ID,
            ) if body.owner_scope == "entity" else await ensure_personal_subscription(
                db, entity_id=user.entity_id, agent_id=MANOR_AGENT_ID,
                owner_user_id=user.id,
            )
        )
        db.add(ChatRoomAgentBinding(
            id=generate_ulid(), room_id=room.id,
            subscription_id=master_subscription.id,
            added_by_user_id=user.id, status="active",
        ))
    invite_token = None
    recipient_outcome = None
    invite_next_step = None
    invite = None
    if recipient and body.kind == "dm" and same_company:
        db.add(ChatRoomMember(
            id=generate_ulid(), room_id=room.id, user_id=recipient.id,
            role="member", status="active", visible_from=now, last_read_at=now,
        ))
    else:
        invite, invite_token = _new_invite(room, recipient_email, user.id)
        db.add(invite)
        if body.kind == "group":
            recipient_outcome = "invited"
            invite_next_step = "accept" if _verified_room_recipient(recipient, recipient_email) else "verify_email" if recipient else "sign_up"
            if invite_next_step == "accept":
                await _record_group_invite_notification(
                    db, invite=invite, room=room, recipient=recipient, inviter=user,
                    token=invite_token,
                )
    await _commit_room_change(db, room.id)
    await db.refresh(room)
    from packages.core.database_runtime import release_read_transaction

    await release_read_transaction(db)
    email_sent = (
        await _send_group_invite_email(request, invite=invite, room=room, inviter=user, token=invite_token, sender=sender)
        if room.kind == "group" and invite is not None else None
    )
    return {
        **(await _room_view(db, room, user.id)),
        "invite_token": invite_token,
        "recipient_outcome": recipient_outcome,
        "invite_next_step": invite_next_step,
        "invite_email_sent": email_sent,
    }


@router.get("/{room_id}")
async def get_room(
    room_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, _ = await _active_room(db, room_id, user.id)
    view = await _room_view(db, room, user.id)
    view["unread_count"] = (await _unread_room_counts(db, user.id, [room.id])).get(room.id, 0)
    return view


@router.patch("/{room_id}")
async def update_room(
    room_id: str,
    body: UpdateRoomRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id, write=True)
    if room.kind != "group" or member.role not in {"owner", "admin"}:
        raise HTTPException(403, "Cannot rename this conversation")
    title = body.title.strip()
    if not title:
        raise HTTPException(422, "Group name is required")
    if room.title != title:
        room.title = title
        await _commit_room_change(db, room_id)
    return await _room_view(db, room, user.id)


@router.patch("/{room_id}/ai-execution")
async def update_room_ai_execution(
    room_id: str,
    body: UpdateRoomAiExecutionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id, write=True)
    if not _can_manage_ai_execution(room, member, user):
        raise HTTPException(403, "Only the group owner can change AI execution")
    direct_enabled = body.enabled
    if room.ai_tool_execution_revision != body.expected_revision:
        if (
            room.ai_tool_execution_enabled != body.enabled
            or room.ai_file_actions_direct_enabled != direct_enabled
        ):
            raise HTTPException(409, "Group AI execution setting changed; refresh and try again")
    elif (
        room.ai_tool_execution_enabled != body.enabled
        or room.ai_file_actions_direct_enabled != direct_enabled
    ):
        previous_enabled = room.ai_tool_execution_enabled
        previous_direct_enabled = room.ai_file_actions_direct_enabled
        room.ai_tool_execution_enabled = body.enabled
        room.ai_file_actions_direct_enabled = direct_enabled
        room.ai_tool_execution_revision += 1
        db.add(AuditLog(
            id=generate_ulid(), entity_id=room.owner_entity_id, user_id=user.id,
            action="chat_room.ai_tool_execution.changed", resource_type="chat_room",
            resource_id=room.id,
            details={
                "previous_enabled": previous_enabled,
                "enabled": body.enabled,
                "previous_direct_enabled": previous_direct_enabled,
                "direct_enabled": direct_enabled,
                "revision": room.ai_tool_execution_revision,
                "owner_scope": "entity" if room.owner_entity_id else "personal",
            },
        ))
    await _commit_room_change(db, room_id)
    # The setting transaction held the room first. Cleanup uses fresh
    # run -> room locks so concurrent decisions cannot deadlock it. Repeat
    # it on idempotent PATCH retries after an interrupted cleanup.
    async with database.async_session() as cleanup_db:
        await invalidate_stale_room_tool_approvals(cleanup_db, room_id=room.id)
    return await _room_view(db, room, user.id)


def _room_tool_approval_view(approval: ChatRoomToolApproval) -> dict:
    # Deliberately exclude raw arguments, checkpoint, and message/file content.
    return {
        "id": approval.id,
        "run_id": approval.run_id,
        "tool_name": approval.tool_name,
        "summary": approval.display_summary,
        "status": approval.status,
        "requester_user_id": approval.requester_user_id,
        "decided_by_user_id": approval.decided_by_user_id,
        "created_at": approval.created_at,
        "decided_at": approval.decided_at,
    }


@router.get("/{room_id}/ai-tool-approvals")
async def list_room_tool_approvals(
    room_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id)
    if room.kind != "group" or member.role not in {"owner", "admin"}:
        raise HTTPException(403, "Only group owners and admins can review AI tool calls")
    rows = (await db.execute(select(ChatRoomToolApproval).join(
        ChatRoomAgentRun, ChatRoomAgentRun.id == ChatRoomToolApproval.run_id,
    ).where(
        ChatRoomToolApproval.room_id == room_id,
        (ChatRoomToolApproval.status != "pending") | (ChatRoomAgentRun.status == "waiting_approval"),
    ).order_by(ChatRoomToolApproval.created_at.desc(), ChatRoomToolApproval.id.desc()).limit(50))).scalars().all()
    return [_room_tool_approval_view(approval) for approval in rows]


@router.get("/{room_id}/ai-reply-approvals")
async def list_room_reply_approvals(
    room_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id)
    if room.kind != "group" or member.role not in {"owner", "admin"}:
        raise HTTPException(403, "Only group owners and admins can review AI replies")
    return await pending_room_reply_approvals(
        db, room_id=room_id, approver_user_id=user.id,
    )


@router.post("/{room_id}/ai-reply-approvals/{run_id}/decision")
async def decide_room_reply_approval_api(
    room_id: str,
    run_id: str,
    body: RoomReplyApprovalDecisionRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id)
    if room.kind != "group" or member.role not in {"owner", "admin"}:
        raise HTTPException(403, "Only group owners and admins can review AI replies")
    try:
        enqueue = await decide_room_reply_approval(
            db, room_id=room_id, run_id=run_id,
            approver_user_id=user.id, expected_digest=body.expected_digest,
            approve=body.approve,
        )
    except RoomReplyApprovalError as exc:
        if exc.code == "approver_not_authorized":
            raise HTTPException(403, "Workspace approval authority required") from exc
        raise HTTPException(409, "AI reply approval is stale or unavailable") from exc
    await _commit_room_change(db, room_id)
    if enqueue:
        background_tasks.add_task(enqueue_room_agent_run, run_id)
    return {"run_id": run_id, "status": "approved" if enqueue else "rejected"}


@router.post("/{room_id}/ai-tool-approvals/{approval_id}/decision")
async def decide_room_tool_approval_api(
    room_id: str,
    approval_id: str,
    body: RoomToolApprovalDecisionRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Preflight avoids disclosing whether a room/run exists to an outsider.
    member = (await db.execute(select(ChatRoomMember).join(
        ChatRoom, ChatRoom.id == ChatRoomMember.room_id,
    ).where(
        ChatRoom.id == room_id, ChatRoom.kind == "group", ChatRoom.status == "active",
        ChatRoomMember.user_id == user.id, ChatRoomMember.status == "active",
    ))).scalar_one_or_none()
    if member is None:
        raise HTTPException(404, "Conversation not found")
    if member.role not in {"owner", "admin"}:
        raise HTTPException(403, "Only group owners and admins can review AI tool calls")
    try:
        approval, enqueue = await decide_room_tool_approval(
            db, room_id=room_id, approval_id=approval_id,
            approver_user_id=user.id, approve=body.approve,
        )
    except RoomToolApprovalError as exc:
        if exc.code == "approval_unavailable":
            raise HTTPException(404, "Approval not found") from exc
        if exc.code == "approver_not_authorized":
            raise HTTPException(403, "Only group owners and admins can review AI tool calls") from exc
        raise HTTPException(409, "AI tool approval is stale or no longer available") from exc
    await _commit_room_change(db, room_id)
    if enqueue:
        background_tasks.add_task(enqueue_room_agent_run, approval.run_id)
    return _room_tool_approval_view(approval)


@router.delete("/{room_id}", status_code=204)
async def close_room(
    room_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id, write=True)
    if room.kind != "group" or member.role != "owner":
        raise HTTPException(403, "Only the group owner can close a group")
    room.status = "closed"
    # A closed group runs no Agent; keep no model context of it.
    await discard_room_transcripts(db, room.id)
    pending = (await db.execute(select(ChatRoomInvite).where(
        ChatRoomInvite.room_id == room.id, ChatRoomInvite.status == "pending",
    ))).scalars().all()
    await _retire_invite_notifications(db, pending)
    await _commit_room_change(db, room_id)


@router.get("/{room_id}/presence")
async def get_room_presence(
    room_id: str,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Only current room members may see which fellow members are online."""
    await _active_room(db, room_id, user.id)
    online_ids = await _online_room_members(db, [room_id])
    response.headers["Cache-Control"] = "private, no-store"
    return {"online_user_ids": sorted(online_ids.get(room_id, set()))}


@router.get("/{room_id}/messages")
async def get_messages(
    room_id: str,
    before: str | None = None,
    after: str | None = None,
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if before and after:
        raise HTTPException(422, "Use either before or after")
    room, member = await _active_room(db, room_id, user.id)
    query = select(ChatRoomMessage, User).outerjoin(User, User.id == ChatRoomMessage.sender_user_id).where(
        ChatRoomMessage.room_id == room_id,
        ChatRoomMessage.created_at >= member.visible_from,
    )
    if q and (search := q.strip()):
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(ChatRoomMessage.deleted_at.is_(None), ChatRoomMessage.content.ilike(f"%{escaped}%", escape="\\"))
    if before or after:
        anchor = (await db.execute(select(ChatRoomMessage).where(
            ChatRoomMessage.id == (before or after), ChatRoomMessage.room_id == room_id,
            ChatRoomMessage.created_at >= member.visible_from,
        ))).scalar_one_or_none()
        if anchor is None:
            raise HTTPException(404, "Message not found")
        cursor = tuple_(ChatRoomMessage.created_at, ChatRoomMessage.id)
        query = query.where(cursor < (anchor.created_at, anchor.id) if before else cursor > (anchor.created_at, anchor.id))
    order = (ChatRoomMessage.created_at.asc(), ChatRoomMessage.id.asc()) if after else (
        ChatRoomMessage.created_at.desc(), ChatRoomMessage.id.desc()
    )
    rows = (await db.execute(query.order_by(*order).limit(limit))).all()
    message_ids = [message.id for message, _ in rows]
    reply_ids = {message.reply_to_message_id for message, _ in rows if message.reply_to_message_id}
    reply_rows = (await db.execute(select(ChatRoomMessage, User).outerjoin(
        User, User.id == ChatRoomMessage.sender_user_id,
    ).where(
        ChatRoomMessage.id.in_(reply_ids),
        ChatRoomMessage.room_id == room_id,
        ChatRoomMessage.created_at >= member.visible_from,
    ))).all() if reply_ids else []
    reply_by_id = {
        target.id: {
            "id": target.id,
            "sender_name": target.sender_agent_name or ((sender.display_name or sender.email) if sender else "Deleted user"),
            "content": target.content[:160] if target.deleted_at is None else "Message removed",
        }
        for target, sender in reply_rows
    }
    reaction_rows = (await db.execute(select(
        ChatRoomMessageReaction.message_id, ChatRoomMessageReaction.emoji, ChatRoomMessageReaction.user_id,
    ).where(ChatRoomMessageReaction.message_id.in_(message_ids)))).all() if message_ids else []
    reactions_by_message: dict[str, dict[str, dict]] = {}
    for message_id, emoji, reactor_id in reaction_rows:
        reaction = reactions_by_message.setdefault(message_id, {}).setdefault(
            emoji, {"emoji": emoji, "count": 0, "reacted_by_me": False},
        )
        reaction["count"] += 1
        reaction["reacted_by_me"] |= reactor_id == user.id
    run_rows = (await db.execute(select(ChatRoomAgentRun).where(
        ChatRoomAgentRun.room_id == room_id,
        or_(ChatRoomAgentRun.source_message_id.in_([message.id for message, _ in rows]),
            ChatRoomAgentRun.response_message_id.in_([message.id for message, _ in rows])),
    ).order_by(ChatRoomAgentRun.created_at, ChatRoomAgentRun.id))).scalars().all() if rows else []
    runs_by_message: dict[str, list[dict]] = {}
    blocks_by_reply: dict[str, list[dict]] = {}
    for run in run_rows:
        run_view = {
            "id": run.id, "binding_id": run.binding_id,
            "agent_name": run.agent_name, "status": run.status,
            "assistant_blocks": room_agent_process_blocks(run),
        }
        if run.response_message_id:
            blocks_by_reply[run.response_message_id] = run_view["assistant_blocks"]
        if run.status == "waiting_approval":
            run_view["approval_kind"] = (
                "reply" if isinstance(run.checkpoint, dict)
                and "pending_reply" in run.checkpoint else "file"
            )
        if run.error_code == "llm_configuration_missing":
            run_view["error_code"] = run.error_code
        runs_by_message.setdefault(run.source_message_id, []).append(run_view)
    file_rows = (await db.execute(select(
        ChatRoomFile.id, ChatRoomFile.message_id, ChatRoomFile.name,
        ChatRoomFile.size, ChatRoomFile.mime_type, ChatRoomFile.created_at,
    ).where(
        ChatRoomFile.message_id.in_([message.id for message, _ in rows]),
        ChatRoomFile.deleted_at.is_(None),
    ))).all() if rows else []
    files_by_message: dict[str, list] = {}
    for file in file_rows:
        files_by_message.setdefault(file.message_id, []).append(file)
    avatars = await _person_avatar_urls(db, [sender for _, sender in rows], owner_entity_id=room.owner_entity_id)
    return [{
        "id": message.id, "room_id": room_id,
        "sender_user_id": message.sender_user_id,
        "sender_name": message.sender_agent_name or ((sender.display_name or sender.email) if sender else "Deleted user"),
        "sender_user_avatar_url": avatars[sender.id] if sender and message.sender_user_id else None,
        "sender_agent_binding_id": message.sender_agent_binding_id,
        "sender_agent_name": message.sender_agent_name,
        "sender_agent_avatar_url": message.sender_agent_avatar_url,
        "reply_to_message_id": message.reply_to_message_id,
        "reply_to": reply_by_id.get(message.reply_to_message_id),
        "reactions": [] if message.deleted_at else list(reactions_by_message.get(message.id, {}).values()),
        "agent_run_status": message.agent_run_status,
        "content": "" if message.deleted_at else message.content,
        "assistant_blocks": [] if message.deleted_at else blocks_by_reply.get(message.id, []),
        "deleted_at": message.deleted_at,
        "mentioned_user_ids": [] if message.deleted_at else message.mentioned_user_ids or [],
        "agent_runs": [] if message.deleted_at else runs_by_message.get(message.id, []),
        "created_at": message.created_at,
        "attachments": [] if message.deleted_at else [_file_view(file, room_id=room_id, sender_name=message.sender_agent_name or ((sender.display_name or sender.email) if sender else "Deleted user"))
                        for file in files_by_message.get(message.id, [])],
    } for message, sender in (rows if after else reversed(rows))]


@router.post("/{room_id}/read", status_code=204)
async def mark_room_read(
    room_id: str,
    body: ReadRoomRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, member = await _active_room(db, room_id, user.id, write=True)
    message_time = (await db.execute(select(ChatRoomMessage.created_at).where(
        ChatRoomMessage.id == body.through_message_id,
        ChatRoomMessage.room_id == room_id,
        ChatRoomMessage.created_at >= member.visible_from,
    ))).scalar_one_or_none()
    if message_time is None:
        raise HTTPException(404, "Message not found")
    if member.last_read_at is None or message_time > member.last_read_at:
        member.last_read_at = message_time
        await db.commit()


@router.patch("/{room_id}/notifications")
async def update_room_notifications(
    room_id: str, body: UpdateRoomNotificationsRequest,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    _, member = await _active_room(db, room_id, user.id, write=True)
    member.notifications_muted = body.muted
    await db.commit()
    return {"muted": member.notifications_muted}


@router.post("/{room_id}/message-retractions")
async def room_message_retractions(
    room_id: str, body: RoomRetractionsRequest,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """Refresh already-rendered history without reloading every old page."""
    _, member = await _active_room(db, room_id, user.id)
    if not body.message_ids:
        return {"message_ids": []}
    ids = list(dict.fromkeys(body.message_ids))
    retracted = (await db.execute(select(ChatRoomMessage.id).where(
        ChatRoomMessage.id.in_(ids),
        ChatRoomMessage.room_id == room_id,
        ChatRoomMessage.created_at >= member.visible_from,
        ChatRoomMessage.deleted_at.is_not(None),
    ))).scalars().all()
    return {"message_ids": retracted}


@router.delete("/{room_id}/messages/{message_id}", status_code=204)
async def retract_room_message(
    room_id: str, message_id: str,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    _, member = await _active_room(db, room_id, user.id, write=True)
    message = (await db.execute(select(ChatRoomMessage).where(
        ChatRoomMessage.id == message_id,
        ChatRoomMessage.room_id == room_id,
        ChatRoomMessage.created_at >= member.visible_from,
    ).with_for_update())).scalar_one_or_none()
    if message is None:
        raise HTTPException(404, "Message not found")
    if message.sender_user_id != user.id:
        raise HTTPException(403, "Only the sender can retract a message")
    if message.deleted_at is None:
        message.deleted_at = datetime.now(timezone.utc)
        message.deleted_by_user_id = user.id
        db.add(AuditLog(
            id=generate_ulid(), entity_id=user.entity_id, user_id=user.id,
            action="chat_room.message.retracted", resource_type="chat_room_message",
            resource_id=message.id, details={"room_id": room_id},
        ))
        await db.execute(update(Notification).where(
            Notification.idempotency_key.like(f"chat-room-message:{message.id}:%"),
        ).values(dispatch_status=NotificationDispatchStatus.CANCELED.value))
        await db.execute(update(ChatRoomAgentRun).where(
            ChatRoomAgentRun.source_message_id == message.id,
            ChatRoomAgentRun.status.in_(("pending", "running", "waiting_approval")),
        ).values(status="interrupted", error_code="source_retracted"))
        # Agents may have read the message; rebuild their context from what
        # is still visible rather than redact copies inside transcripts.
        await discard_room_transcripts(db, room_id)
        await _commit_room_change(db, room_id, kind="retracted")


async def _notify_room_message_recipients(
    db: AsyncSession, *, room: ChatRoom, message: ChatRoomMessage,
    sender_user_id: str, sender_name: str,
) -> None:
    """Only explicit mentions and reply targets create in-app alerts."""
    reply_sender_id = None
    if message.reply_to_message_id:
        reply_sender_id = (await db.execute(select(ChatRoomMessage.sender_user_id).where(
            ChatRoomMessage.id == message.reply_to_message_id,
            ChatRoomMessage.room_id == room.id,
            ChatRoomMessage.deleted_at.is_(None),
        ))).scalar_one_or_none()
    recipient_ids = (set(message.mentioned_user_ids or []) | {reply_sender_id}) - {None, sender_user_id}
    if not recipient_ids:
        return
    recipients = (await db.execute(select(ChatRoomMember, User).join(
        User, User.id == ChatRoomMember.user_id,
    ).where(
        ChatRoomMember.room_id == room.id,
        ChatRoomMember.user_id.in_(recipient_ids),
        ChatRoomMember.status == "active",
        ChatRoomMember.notifications_muted.is_(False),
        ChatRoomMember.visible_from <= message.created_at,
        User.status == "active", User.deleted_at.is_(None),
    ).order_by(ChatRoomMember.user_id).with_for_update(read=True, of=ChatRoomMember))).all()
    for member, recipient in recipients:
        is_reply = member.user_id == reply_sender_id
        await create_notification(
            db, recipient.entity_id, recipient.id,
            "chat_room_reply" if is_reply else "chat_room_mention",
            "Group reply" if is_reply else "Group mention",
            body=f"{sender_name} {'replied to you' if is_reply else 'mentioned you'} in {room.title or 'a chat'}.",
            link=f"/chat?room={quote(room.id, safe='')}",
            meta={"chat_room_id": room.id, "chat_room_message_id": message.id},
            idempotency_key=f"chat-room-message:{message.id}:{recipient.id}",
        )


async def _visible_reply_target(
    db: AsyncSession, *, room_id: str, member: ChatRoomMember, message_id: str | None,
) -> None:
    if message_id is None:
        return
    target = (await db.execute(select(ChatRoomMessage.id).where(
        ChatRoomMessage.id == message_id,
        ChatRoomMessage.room_id == room_id,
        ChatRoomMessage.created_at >= member.visible_from,
        ChatRoomMessage.deleted_at.is_(None),
    ))).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "Reply target not found")


async def _visible_reaction_target(
    db: AsyncSession, *, room_id: str, member: ChatRoomMember, message_id: str, user_id: str,
) -> None:
    target = (await db.execute(select(ChatRoomMessage.sender_user_id).where(
        ChatRoomMessage.id == message_id,
        ChatRoomMessage.room_id == room_id,
        ChatRoomMessage.created_at >= member.visible_from,
        ChatRoomMessage.deleted_at.is_(None),
    ))).one_or_none()
    if target is None:
        raise HTTPException(404, "Message not found")
    if target.sender_user_id == user_id:
        raise HTTPException(403, "React to another sender's message")


@router.put("/{room_id}/messages/{message_id}/reactions", status_code=204)
async def add_message_reaction(
    room_id: str, message_id: str, body: RoomReactionRequest,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    if not _valid_reaction_emoji(body.emoji):
        raise HTTPException(422, "Unsupported reaction")
    _, member = await _active_room(db, room_id, user.id)
    await _visible_reaction_target(db, room_id=room_id, member=member, message_id=message_id, user_id=user.id)
    await db.execute(pg_insert(ChatRoomMessageReaction).values(
        id=generate_ulid(), message_id=message_id, user_id=user.id, emoji=body.emoji,
    ).on_conflict_do_nothing(constraint="uq_chat_room_message_reaction"))
    await _commit_room_change(db, room_id, kind="reaction")


@router.delete("/{room_id}/messages/{message_id}/reactions/{emoji}", status_code=204)
async def remove_message_reaction(
    room_id: str, message_id: str, emoji: str,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    if not _valid_reaction_emoji(emoji):
        raise HTTPException(422, "Unsupported reaction")
    _, member = await _active_room(db, room_id, user.id)
    await _visible_reaction_target(db, room_id=room_id, member=member, message_id=message_id, user_id=user.id)
    await db.execute(delete(ChatRoomMessageReaction).where(
        ChatRoomMessageReaction.message_id == message_id,
        ChatRoomMessageReaction.user_id == user.id,
        ChatRoomMessageReaction.emoji == emoji,
    ))
    await _commit_room_change(db, room_id, kind="reaction")


async def _create_mentioned_agent_runs(
    db: AsyncSession, *, room: ChatRoom, member: ChatRoomMember,
    source_message_id: str, agent_binding_ids: list[str], now: datetime,
) -> list[ChatRoomAgentRun]:
    agent_binding_ids = list(dict.fromkeys(agent_binding_ids))
    if not agent_binding_ids:
        return []
    if room.kind != "group":
        raise HTTPException(422, "Agents can be mentioned in groups only")
    bindings = (await db.execute(select(ChatRoomAgentBinding).where(
        ChatRoomAgentBinding.room_id == room.id,
        ChatRoomAgentBinding.id.in_(agent_binding_ids),
        ChatRoomAgentBinding.status == "active",
    ).with_for_update())).scalars().all()
    if len(bindings) != len(agent_binding_ids):
        raise HTTPException(422, "Mentioned Agent is no longer in this group")
    by_id = {binding.id: binding for binding in bindings}
    audience_fingerprint, _ = await room_audience_fingerprint(db, room.id)
    if not audience_fingerprint:
        raise HTTPException(409, "This group has no active audience")
    runs: list[ChatRoomAgentRun] = []
    for binding_id in agent_binding_ids:
        binding = by_id[binding_id]
        view = await _binding_view(db, binding, manager=False)
        if not view.get("available"):
            raise HTTPException(422, "Mentioned Agent is not available")
        run = ChatRoomAgentRun(
            id=generate_ulid(), room_id=room.id, source_message_id=source_message_id,
            binding_id=binding.id, agent_name=view["name"],
            agent_avatar_url=view.get("avatar_url"),
            audience_fingerprint=audience_fingerprint, status="pending",
            last_enqueued_at=now,
            ai_tool_execution_revision=(
                room.ai_tool_execution_revision
                if room.ai_tool_execution_enabled
                else None
            ),
        )
        db.add(run)
        runs.append(run)
    return runs


@router.post("/{room_id}/messages", status_code=201)
async def post_message(
    room_id: str,
    body: PostMessageRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Shared row locks preserve the membership/close boundary while allowing
    # independent senders to insert concurrently. Room recency is derived from
    # the indexed message timeline instead of updating one hot room row.
    room, sender_member = await _active_room(db, room_id, user.id)
    sender_avatar_url = (await _person_avatar_urls(db, [user], owner_entity_id=room.owner_entity_id))[user.id]
    content = body.content.strip()
    if not content:
        raise HTTPException(422, "Message cannot be empty")
    members = set((await db.execute(select(ChatRoomMember.user_id).where(
        ChatRoomMember.room_id == room_id, ChatRoomMember.status == "active",
    ))).scalars())
    if room.kind == "dm" and len(members) != 2:
        raise HTTPException(409, "Private chat invitation is pending")
    mentioned = list(dict.fromkeys(body.mentioned_user_ids))
    if any(member_id not in members for member_id in mentioned):
        raise HTTPException(422, "Mentions must name current members")
    await _visible_reply_target(
        db, room_id=room_id, member=sender_member, message_id=body.reply_to_message_id,
    )
    now = datetime.now(timezone.utc)
    message = ChatRoomMessage(
        id=generate_ulid(), room_id=room_id, sender_user_id=user.id,
        content=content, mentioned_user_ids=mentioned,
        reply_to_message_id=body.reply_to_message_id, created_at=now,
    )
    db.add(message)
    await db.flush()
    runs = await _create_mentioned_agent_runs(
        db, room=room, member=sender_member, source_message_id=message.id,
        agent_binding_ids=body.mentioned_agent_binding_ids, now=now,
    )
    await _notify_room_message_recipients(
        db, room=room, message=message, sender_user_id=user.id,
        sender_name=user.display_name or user.email,
    )
    await _commit_room_change(db, room_id, kind="message", message_id=message.id)
    for run in runs:
        background_tasks.add_task(enqueue_room_agent_run, run.id)
    return {
        "id": message.id, "room_id": room_id, "sender_user_id": user.id,
        "sender_name": user.display_name or user.email,
        "sender_user_avatar_url": sender_avatar_url, "content": content,
        "reply_to_message_id": body.reply_to_message_id,
        "mentioned_user_ids": mentioned, "created_at": now, "attachments": [],
        "agent_runs": [{"id": run.id, "binding_id": run.binding_id, "status": run.status} for run in runs],
    }


async def _lock_room_file_owner(
    db: AsyncSession, *, owner_entity_id: str | None, owner_user_id: str | None,
) -> str:
    # Personal groups charge their owner's primary Entity. Lock that Entity
    # before the room so concurrent group uploads see one storage total.
    storage_entity_id = owner_entity_id
    if owner_user_id:
        storage_entity_id = (await db.execute(select(User.entity_id).where(
            User.id == owner_user_id, User.deleted_at.is_(None), User.status == "active",
        ))).scalar_one_or_none()
    if storage_entity_id is None:
        raise HTTPException(404, "Conversation not found")
    locked_entity = (await db.execute(select(Entity.id).where(
        Entity.id == storage_entity_id, Entity.deleted_at.is_(None),
    ).with_for_update())).scalar_one_or_none()
    if locked_entity is None:
        raise HTTPException(404, "Conversation not found")
    if owner_user_id:
        locked_owner_entity_id = (await db.execute(select(User.entity_id).where(
            User.id == owner_user_id, User.deleted_at.is_(None), User.status == "active",
        ).with_for_update())).scalar_one_or_none()
        if locked_owner_entity_id != storage_entity_id:
            raise HTTPException(409, "Conversation ownership changed")
    return storage_entity_id


async def _create_room_file_message(
    db: AsyncSession,
    *,
    room_id: str,
    user_id: str,
    sender_name: str,
    sender_avatar_url: str | None,
    owner_entity_id: str | None,
    owner_user_id: str | None,
    name: str,
    mime_type: str,
    payload: bytes | BinaryIO,
    payload_size: int | None = None,
    payload_sha256: str | None = None,
    content: str,
    mentioned: list[str],
    mentioned_agent_binding_ids: list[str] | None = None,
    background_tasks: BackgroundTasks | None = None,
    reply_to_message_id: str | None = None,
    owner_locked: bool = False,
    storage_entity_id: str | None = None,
    locked_room: ChatRoom | None = None,
    pre_insert_check: Callable[[], Awaitable[None]] | None = None,
) -> dict:
    """Apply the same membership, mention, and storage limits to every file source."""
    size = len(payload) if isinstance(payload, bytes) else payload_size
    if size is None or size <= 0:
        raise HTTPException(422, "File cannot be empty")
    if size > _ROOM_FILE_MAX_BYTES:
        raise HTTPException(413, "Group files must be 1 GiB or smaller")
    if not owner_locked:
        storage_entity_id = await _lock_room_file_owner(
            db, owner_entity_id=owner_entity_id, owner_user_id=owner_user_id,
        )
    if storage_entity_id is None:
        raise RuntimeError("Room file storage owner lock is required")
    if locked_room is None:
        room, sender_member = await _active_room(db, room_id, user_id, write=True)
    else:
        room = locked_room
        sender_member = (await db.execute(select(ChatRoomMember).where(
            ChatRoomMember.room_id == room_id,
            ChatRoomMember.user_id == user_id,
            ChatRoomMember.status == "active",
        ))).scalar_one()
    if room.kind != "group":
        raise HTTPException(403, "Files are available in group chats only")
    if room.owner_entity_id != owner_entity_id or room.owner_user_id != owner_user_id:
        raise HTTPException(409, "Conversation ownership changed")
    members = set((await db.execute(select(ChatRoomMember.user_id).where(
        ChatRoomMember.room_id == room_id, ChatRoomMember.status == "active",
    ))).scalars())
    if any(member_id not in members for member_id in mentioned):
        raise HTTPException(422, "Mentions must name current members")
    await _visible_reply_target(
        db, room_id=room_id, member=sender_member, message_id=reply_to_message_id,
    )
    # The room write lock above serializes competing uploads and closure.
    used_bytes = (await db.execute(select(func.coalesce(func.sum(ChatRoomFile.size), 0))
        .join(ChatRoomMessage, ChatRoomMessage.id == ChatRoomFile.message_id)
        .where(ChatRoomMessage.room_id == room_id, ChatRoomFile.document_id.is_(None)))).scalar_one()
    if used_bytes + size > _ROOM_FILES_TOTAL_MAX_BYTES:
        raise HTTPException(413, "This group has reached its 10 GiB file limit")
    from packages.core.services.plan_gate import check as check_plan_storage

    gate = await check_plan_storage(db, storage_entity_id, "storage_mb", use_cache=False)
    if not gate.allowed or (gate.limit is not None and gate.current is not None
                            and gate.current + size / (1024 * 1024) > gate.limit):
        raise StorageLimitExceeded(
            gate.message or "Knowledge base storage limit reached. Upgrade for more.",
            plan=gate.plan, limit=gate.limit, current=gate.current,
        )
    if pre_insert_check is not None:
        await pre_insert_check()
    now = datetime.now(timezone.utc)
    message = ChatRoomMessage(
        id=generate_ulid(), room_id=room_id, sender_user_id=user_id,
        content=content.strip(), mentioned_user_ids=mentioned,
        reply_to_message_id=reply_to_message_id, created_at=now,
    )
    attachment = ChatRoomFile(
        id=generate_ulid(), message_id=message.id, name=name, mime_type=mime_type,
        size=size, content=payload if isinstance(payload, bytes) else None, created_at=now,
        chunk_count=None if isinstance(payload, bytes) else (size + ROOM_FILE_CHUNK_BYTES - 1) // ROOM_FILE_CHUNK_BYTES,
        content_sha256=None if isinstance(payload, bytes) else payload_sha256,
    )
    db.add(message)
    await db.flush()
    db.add(attachment)
    if not isinstance(payload, bytes):
        await db.flush()
        await store_room_file_chunks(db, attachment, payload)
    runs = await _create_mentioned_agent_runs(
        db, room=room, member=sender_member, source_message_id=message.id,
        agent_binding_ids=mentioned_agent_binding_ids or [], now=now,
    )
    await _notify_room_message_recipients(
        db, room=room, message=message, sender_user_id=user_id, sender_name=sender_name,
    )
    room.last_message_at = now
    await _commit_room_change(db, room_id, kind="message", message_id=message.id)
    from packages.core.services.plan_gate import invalidate_gate_cache

    invalidate_gate_cache(storage_entity_id)
    if background_tasks is not None:
        for run in runs:
            background_tasks.add_task(enqueue_room_agent_run, run.id)
    return {
        "id": message.id, "room_id": room_id, "sender_user_id": user_id,
        "sender_name": sender_name, "sender_user_avatar_url": sender_avatar_url,
        "content": message.content,
        "reply_to_message_id": reply_to_message_id,
        "mentioned_user_ids": mentioned, "created_at": now,
        "attachments": [_file_view(attachment, room_id=room_id, sender_name=sender_name)],
        "agent_runs": [{"id": run.id, "binding_id": run.binding_id, "status": run.status} for run in runs],
    }


@router.post("/{room_id}/files", status_code=201)
async def upload_room_file(
    room_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    content: str = Form(default="", max_length=4000),
    mentioned_user_ids: str = Form(default="[]", max_length=1000),
    mentioned_agent_binding_ids: str = Form(default="[]", max_length=1000),
    reply_to_message_id: str | None = Form(default=None, min_length=26, max_length=26),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Post one room-only attachment without exposing Knowledge or entity FS."""
    user_id = user.id
    sender_name = user.display_name or user.email
    room, _ = await _active_room(db, room_id, user_id)
    sender_avatar_url = (await _person_avatar_urls(db, [user], owner_entity_id=room.owner_entity_id))[user_id]
    if room.kind != "group":
        raise HTTPException(403, "Files are available in group chats only")
    owner_entity_id, owner_user_id = room.owner_entity_id, room.owner_user_id
    # Release admission read locks before scanning; membership and closure are
    # checked again under a write lock immediately before the atomic commit.
    await db.rollback()
    name = re.sub(r"[\x00-\x1f\x7f]", "", (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]).strip()
    if not name or len(name) > 255:
        raise HTTPException(422, "Choose a file with a valid name")
    try:
        mentioned = json.loads(mentioned_user_ids)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "Invalid mentioned user IDs") from exc
    if (not isinstance(mentioned, list) or len(mentioned) > 20
            or any(not isinstance(item, str) for item in mentioned)):
        raise HTTPException(422, "Invalid mentioned user IDs")
    mentioned = list(dict.fromkeys(mentioned))
    try:
        mentioned_agents = json.loads(mentioned_agent_binding_ids)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "Invalid mentioned Agent IDs") from exc
    if (not isinstance(mentioned_agents, list) or len(mentioned_agents) > 5
            or any(not isinstance(item, str) or len(item) != 26 for item in mentioned_agents)):
        raise HTTPException(422, "Invalid mentioned Agent IDs")
    size, digest = await anyio.to_thread.run_sync(_room_file_digest, file.file)
    payload: bytes | BinaryIO = file.file
    if size <= ROOM_FILE_INLINE_MAX_BYTES:
        payload = await file.read(size)
    try:
        inspect = inspect_upload_content if isinstance(payload, bytes) else inspect_upload_stream
        mime_type = await inspect(
            payload, filename=name, declared_content_type=file.content_type,
        )
    except UploadSecurityError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    return await _create_room_file_message(
        db, room_id=room_id, user_id=user_id, sender_name=sender_name,
        sender_avatar_url=sender_avatar_url,
        owner_entity_id=owner_entity_id, owner_user_id=owner_user_id,
        name=name, mime_type=mime_type, payload=payload,
        payload_size=size, payload_sha256=digest,
        content=content, mentioned=mentioned,
        mentioned_agent_binding_ids=mentioned_agents, background_tasks=background_tasks,
        reply_to_message_id=reply_to_message_id,
    )


async def _authorized_knowledge_source(
    db: AsyncSession, *, user_id: str, entity_id: str,
    credential: AuthenticatedUserCredential, document_id: str,
) -> tuple[str, str, str | None]:
    """Authorize a fresh Knowledge read and external publication by this caller."""
    document = await get_document_for_read(db, document_id, entity_id)
    if document is None:
        # Metadata/remote Knowledge has no local FS path. Check its actor policy
        # before explaining that this first version cannot copy its bytes.
        document = (await db.execute(select(Document).where(
            Document.id == document_id,
            Document.entity_id == entity_id,
            Document.is_trashed.is_(False),
            Document.fs_path.is_(None),
        ).with_for_update())).scalar_one_or_none()
        if document is None:
            raise HTTPException(404, "Knowledge file not found")
    decision = await ResourcePermissionGate.authorize_document_read(
        db,
        credential=credential,
        document=document,
        download=True,
    )
    if decision is None:
        raise HTTPException(404, "Knowledge file not found")
    classification, _, _ = await effective_document_folder_policy(db, document)
    if classification not in {Classification.PUBLIC, Classification.INTERNAL}:
        raise HTTPException(403, "This Knowledge file cannot be shared to a group")
    if document.quarantine_status != "clean":
        raise HTTPException(403, "This Knowledge file is not ready to share")
    has_staff_record, _, _, staff_permissions = await user_staff_role_assignment(
        db, user_id, entity_id,
    )
    is_selected_entity_admin = (
        Permission.ADMIN_SETTINGS.value in staff_permissions if has_staff_record
        else has_permission(decision.actor.role or "", Permission.ADMIN_SETTINGS)
    )
    can_share = (
        document.owner_id == user_id
        or is_selected_entity_admin
        or await user_has_document_capability(
            db, document=document, user_id=user_id,
            capabilities={Capability.SHARE_EXTERNAL},
        )
    )
    if not can_share:
        raise HTTPException(403, "External sharing is not allowed for this Knowledge file")
    if not document.fs_path:
        raise HTTPException(415, "Only locally stored Knowledge files can be added to a group")
    rel_path = str(document.fs_path)
    if (rel_path.startswith("/") or "\\" in rel_path
            or any(part in {"", ".", ".."} for part in rel_path.split("/"))):
        raise HTTPException(409, "Knowledge file path is not available")
    name = re.sub(r"[\x00-\x1f\x7f]", "", document.name.replace("\\", "/").rsplit("/", 1)[-1]).strip()
    if not name or len(name) > 255:
        raise HTTPException(422, "Knowledge file name must be 255 characters or shorter")
    return name, rel_path, document.mime_type


def _room_file_digest(source: BinaryIO, target: BinaryIO | None = None) -> tuple[int, str]:
    """Measure real bytes with bounded reads, optionally pinning a private copy."""
    source.seek(0)
    size = 0
    digest = hashlib.sha256()
    try:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > _ROOM_FILE_MAX_BYTES:
                raise HTTPException(413, "Group files must be 1 GiB or smaller")
            digest.update(chunk)
            if target is not None:
                target.write(chunk)
        if not size:
            raise HTTPException(422, "File cannot be empty")
        if target is not None:
            target.seek(0)
        return size, digest.hexdigest()
    finally:
        source.seek(0)


def _knowledge_snapshot_identity(entity_id: str, rel_path: str, target: BinaryIO | None = None) -> tuple[int, str]:
    with open_entity_file_snapshot(entity_id, rel_path) as snapshot:
        if snapshot.stat.st_size > _ROOM_FILE_MAX_BYTES:
            raise HTTPException(413, "Group files must be 1 GiB or smaller")
        with open(snapshot.descriptor_path, "rb") as source:
            return _room_file_digest(source, target)


def _read_knowledge_snapshot(entity_id: str, rel_path: str) -> tuple[BinaryIO, int, str]:
    output = tempfile.TemporaryFile(mode="w+b")
    try:
        size, digest = _knowledge_snapshot_identity(entity_id, rel_path, output)
        return output, size, digest
    except BaseException:
        output.close()
        raise


@router.post("/{room_id}/files/from-knowledge", status_code=201)
async def add_room_file_from_knowledge(
    room_id: str,
    body: KnowledgeRoomFileRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Publish an authorized Knowledge snapshot, never a live source reference."""
    user_id = user.id
    entity_id = user.entity_id
    sender_name = user.display_name or user.email
    credential = AuthenticatedUserCredential.from_user(user)
    room, _ = await _active_room(db, room_id, user_id)
    sender_avatar_url = (await _person_avatar_urls(db, [user], owner_entity_id=room.owner_entity_id))[user_id]
    if room.kind != "group":
        raise HTTPException(403, "Files are available in group chats only")
    owner_entity_id, owner_user_id = room.owner_entity_id, room.owner_user_id
    await db.rollback()
    pinned_source = None
    try:
        async with entity_filesystem_read_lock(canonical_entity_root(entity_id)):
            source = await _authorized_knowledge_source(
                db, user_id=user_id, entity_id=entity_id,
                credential=credential, document_id=body.document_id,
            )
            pinned_source, size, digest = await anyio.to_thread.run_sync(_read_knowledge_snapshot, entity_id, source[1])
            await db.rollback()
        payload: bytes | BinaryIO = pinned_source
        if size <= ROOM_FILE_INLINE_MAX_BYTES:
            payload = await anyio.to_thread.run_sync(pinned_source.read, size)
        try:
            inspect = inspect_upload_content if isinstance(payload, bytes) else inspect_upload_stream
            mime_type = await inspect(
                payload, filename=source[0], declared_content_type=source[2],
            )
        except UploadSecurityError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        # Malware scanning waits outside all source locks. Reauthorize and
        # compare the pinned bytes after that wait, before committing a copy.
        async with entity_filesystem_read_lock(canonical_entity_root(entity_id)):
            # Match the owner → room → Workspace/folder/Document order used by
            # room agent binding and Knowledge lifecycle mutations.
            storage_entity_id = await _lock_room_file_owner(
                db, owner_entity_id=owner_entity_id, owner_user_id=owner_user_id,
            )
            locked_room, _ = await _active_room(db, room_id, user_id, write=True)
            if locked_room.kind != "group":
                raise HTTPException(403, "Files are available in group chats only")
            if locked_room.owner_entity_id != owner_entity_id or locked_room.owner_user_id != owner_user_id:
                raise HTTPException(409, "Conversation ownership changed")
            current_source = await _authorized_knowledge_source(
                db, user_id=user_id, entity_id=entity_id,
                credential=credential, document_id=body.document_id,
            )
            if current_source != source:
                raise HTTPException(409, "Knowledge file changed during sharing; retry")
            current_identity = await anyio.to_thread.run_sync(_knowledge_snapshot_identity, entity_id, source[1])
            if current_identity != (size, digest):
                raise HTTPException(409, "Knowledge file changed during sharing; retry")

            async def recheck_source_policy() -> None:
                # This runs after the room quota queries, immediately before
                # inserting the room-owned copy. Room and source policy rows
                # remain locked for the remainder of the transaction.
                latest = await _authorized_knowledge_source(
                    db, user_id=user_id, entity_id=entity_id,
                    credential=credential, document_id=body.document_id,
                )
                if latest != source:
                    raise HTTPException(409, "Knowledge file changed during sharing; retry")

            return await _create_room_file_message(
                db, room_id=room_id, user_id=user_id, sender_name=sender_name,
                sender_avatar_url=sender_avatar_url,
                owner_entity_id=owner_entity_id, owner_user_id=owner_user_id,
                name=source[0], mime_type=mime_type, payload=payload,
                payload_size=size, payload_sha256=digest,
                content=body.content, mentioned=list(dict.fromkeys(body.mentioned_user_ids)),
                mentioned_agent_binding_ids=body.mentioned_agent_binding_ids,
                background_tasks=background_tasks,
                reply_to_message_id=body.reply_to_message_id,
                owner_locked=True, locked_room=locked_room,
                storage_entity_id=storage_entity_id,
                pre_insert_check=recheck_source_policy,
            )
    except DocumentDownloadNotAllowed as exc:
        raise HTTPException(403, "Knowledge file download is not allowed") from exc
    except EntityFilesystemBusyError as exc:
        raise HTTPException(423, "Knowledge file is busy; retry shortly") from exc
    except (EntityFilesystemError, DocumentMutationConflictError, OSError) as exc:
        raise HTTPException(409, "Knowledge file is unavailable or changed") from exc
    finally:
        if pinned_source is not None:
            pinned_source.close()


@router.get("/{room_id}/files")
async def list_room_files(
    room_id: str,
    response: Response,
    before: str | None = None,
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id)
    if room.kind != "group":
        raise HTTPException(403, "Files are available in group chats only")
    query = (select(
        ChatRoomFile.id, ChatRoomFile.message_id, ChatRoomFile.name,
        ChatRoomFile.size, ChatRoomFile.mime_type, ChatRoomFile.created_at,
        User.display_name, User.email, ChatRoomMessage.sender_agent_name,
        ChatRoomMessage.sender_user_id,
    ).join(ChatRoomMessage, ChatRoomMessage.id == ChatRoomFile.message_id)
     .outerjoin(User, User.id == ChatRoomMessage.sender_user_id)
     .where(ChatRoomMessage.room_id == room_id, ChatRoomMessage.created_at >= member.visible_from,
            ChatRoomFile.deleted_at.is_(None)))
    if before:
        anchor = (await db.execute(select(ChatRoomFile.created_at, ChatRoomFile.id)
            .join(ChatRoomMessage, ChatRoomMessage.id == ChatRoomFile.message_id)
            .where(ChatRoomFile.id == before, ChatRoomFile.deleted_at.is_(None), ChatRoomMessage.room_id == room_id,
                   ChatRoomMessage.created_at >= member.visible_from))).one_or_none()
        if anchor is None:
            raise HTTPException(404, "File not found")
        query = query.where(tuple_(ChatRoomFile.created_at, ChatRoomFile.id) < (anchor.created_at, anchor.id))
    if q.strip():
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(ChatRoomFile.name.ilike(f"%{escaped}%", escape="\\"))
    rows = (await db.execute(query.order_by(ChatRoomFile.created_at.desc(), ChatRoomFile.id.desc()).limit(limit))).all()
    response.headers["Cache-Control"] = "private, no-store"
    return [{**_file_view(file, room_id=room_id, sender_name=file.sender_agent_name or file.display_name or file.email or "Deleted user"),
             "can_delete": _can_remove_room_file(member, file.sender_user_id, user.id)}
            for file in rows]


@router.delete("/{room_id}/files/{file_id}", status_code=204)
async def remove_room_file(
    room_id: str,
    file_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id, write=True)
    if room.kind != "group":
        raise HTTPException(403, "Files are available in group chats only")
    row = (await db.execute(select(
        ChatRoomFile, ChatRoomMessage.sender_user_id, ChatRoomMessage.content,
    ).join(ChatRoomMessage, ChatRoomMessage.id == ChatRoomFile.message_id)
     .where(ChatRoomFile.id == file_id, ChatRoomFile.deleted_at.is_(None),
            ChatRoomMessage.room_id == room_id, ChatRoomMessage.created_at >= member.visible_from)
     .with_for_update(of=ChatRoomFile))).one_or_none()
    if row is None:
        raise HTTPException(404, "File not found")
    attachment, sender_user_id, message_content = row
    if not _can_remove_room_file(member, sender_user_id, user.id):
        raise HTTPException(403, "Cannot remove this group file")
    attachment.deleted_at = datetime.now(timezone.utc)
    if message_content in {"", "Generated a file"}:
        other_active = (await db.execute(select(ChatRoomFile.id).where(
            ChatRoomFile.message_id == attachment.message_id,
            ChatRoomFile.id != attachment.id,
            ChatRoomFile.deleted_at.is_(None),
        ).limit(1))).scalar_one_or_none()
        if other_active is None:
            message = (await db.execute(select(ChatRoomMessage).where(
                ChatRoomMessage.id == attachment.message_id,
                ChatRoomMessage.room_id == room_id,
            ).with_for_update())).scalar_one()
            message.content = "File removed"
    db.add(AuditLog(
        id=generate_ulid(), entity_id=room.owner_entity_id, user_id=user.id,
        action="chat_room.file.removed", resource_type="chat_room_file",
        resource_id=attachment.id,
        details={"room_id": room_id, "message_id": attachment.message_id},
    ))
    await _commit_room_change(db, room_id)


async def _authorized_room_download(
    db: AsyncSession, room_id: str, file_id: str, credential: AuthenticatedUserCredential,
    *, metadata_only: bool = False,
) -> tuple[ChatRoomFile, Document | None]:
    if await resolve_current_user_actor(db, credential) is None:
        raise HTTPException(404, "File not found")
    room, member = await _active_room(db, room_id, credential.user_id)
    if room.kind != "group":
        raise HTTPException(403, "Files are available in group chats only")
    query = select(ChatRoomFile)
    if metadata_only:
        query = query.options(defer(ChatRoomFile.content))
    row = (await db.execute(query
        .join(ChatRoomMessage, ChatRoomMessage.id == ChatRoomFile.message_id)
        .where(ChatRoomFile.id == file_id, ChatRoomFile.deleted_at.is_(None), ChatRoomMessage.room_id == room_id,
               ChatRoomMessage.created_at >= member.visible_from))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "File not found")
    document = None
    if row.document_id is not None:
        from packages.core.services.room_generated_files import (
            RoomGeneratedFileError, assert_group_generated_folder,
        )

        linked_run = (await db.execute(select(ChatRoomAgentRun.id).where(
            ChatRoomAgentRun.id == row.source_run_id,
            ChatRoomAgentRun.room_id == room.id,
        ))).scalar_one_or_none()
        document = (await db.execute(select(Document).where(
            Document.id == row.document_id,
            Document.entity_id == row.document_entity_id,
            Document.is_trashed.is_(False),
        ))).scalar_one_or_none()
        if (
            linked_run is None or document is None
            or document.source != "ai_generated"
            or document.visibility != "private" or document.client_visible
            or document.quarantine_status != "clean"
            or document.owner_id != (room.created_by_user_id if room.owner_entity_id else room.owner_user_id)
            or not document.fs_path or document.file_size != row.size
            or str(document.mime_type or "application/octet-stream").strip().lower() != row.mime_type
            or not row.content_sha256
        ):
            raise HTTPException(404, "File not found")
        classification, visibility, client_visible = await effective_document_folder_policy(db, document)
        if classification not in {Classification.PUBLIC, Classification.INTERNAL} or visibility != "private" or client_visible:
            raise HTTPException(404, "File not found")
        try:
            await assert_group_generated_folder(
                db, document=document, room_id=room.id,
                custodian_user_id=document.owner_id,
            )
        except RoomGeneratedFileError as exc:
            raise HTTPException(404, "File not found") from exc
    return row, document


def _room_download_identity(row: ChatRoomFile, document: Document) -> tuple:
    return (
        row.id, row.message_id, row.name, row.size, row.mime_type, row.content_sha256,
        row.source_run_id, document.id, document.entity_id, document.fs_path,
    )


@router.get("/{room_id}/files/{file_id}")
async def download_room_file(
    room_id: str,
    file_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _room_file_download_response(
        db, room_id, file_id, AuthenticatedUserCredential.from_user(user),
    )


async def _room_file_download_response(
    db: AsyncSession, room_id: str, file_id: str, credential: AuthenticatedUserCredential,
):
    row, document = await _authorized_room_download(db, room_id, file_id, credential)
    reserved = row.size if document is not None or row.chunk_count else 0
    if reserved and not await room_file_snapshot_budget.reserve(reserved, timeout=0):
        raise HTTPException(503, "File downloads are busy; retry shortly", headers={"Retry-After": "5"})
    try:
        return await _build_room_file_download_response(
            db, room_id, file_id, credential, row, document,
            on_close=(lambda: room_file_snapshot_budget.release(reserved)) if reserved else None,
        )
    except BaseException:
        if reserved:
            room_file_snapshot_budget.release(reserved)
        raise


async def _build_room_file_download_response(
    db: AsyncSession, room_id: str, file_id: str, credential: AuthenticatedUserCredential,
    row: ChatRoomFile, document: Document | None, *, on_close,
):
    headers = {
        "Content-Disposition": f"attachment; filename=\"download\"; filename*=UTF-8''{quote(row.name, safe='')}",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
    }
    if document is not None:
        admitted_identity = _room_download_identity(row, document)
        # Release room/member read locks and the connection before disk I/O.
        # Revalidate the immutable credential and all file authority afterward.
        await db.commit()

        def verified_private_copy():
            # Never stream live Knowledge bytes after a preflight hash: another
            # writer could mutate them between chunks. Copy to an unlinked
            # private temporary file, verify exactly what was copied, then
            # expose only those pinned bytes.
            output = tempfile.TemporaryFile(mode="w+b")
            try:
                digest = hashlib.sha256()
                copied = 0
                with open_entity_file_snapshot(
                    document.entity_id, document.fs_path,
                    expected_content_sha256=row.content_sha256,
                ) as snapshot:
                    if snapshot.stat.st_size != row.size:
                        raise EntityFilesystemError("Group file size changed")
                    with open(snapshot.descriptor_path, "rb") as source:
                        while chunk := source.read(1024 * 1024):
                            copied += len(chunk)
                            if copied > row.size:
                                raise EntityFilesystemError("Group file grew while copying")
                            digest.update(chunk)
                            output.write(chunk)
                if copied != row.size or digest.hexdigest() != row.content_sha256:
                    raise EntityFilesystemError("Group file changed while copying")
                output.seek(0)
                return output
            except BaseException:
                output.close()
                raise

        try:
            pinned_copy = await anyio.to_thread.run_sync(verified_private_copy)
        except EntityFilesystemError as exc:
            raise HTTPException(409, "File changed after sharing") from exc
        except OSError as exc:
            raise HTTPException(503, "File temporarily unavailable") from exc

        try:
            db.expire_all()
            current_row, current_document = await _authorized_room_download(db, room_id, file_id, credential)
            if current_document is None or _room_download_identity(current_row, current_document) != admitted_identity:
                raise HTTPException(409, "File changed after sharing")
            response = PinnedFileStreamingResponse(
                pinned_copy, media_type="application/octet-stream",
                headers={**headers, "Content-Length": str(row.size)},
                on_close=on_close,
            )
            # No DB work remains: bytes and metadata are pinned. The request
            # dependency otherwise keeps its connection until a slow client
            # finishes reading (or disconnects), starving unrelated requests.
            await db.commit()
            return response
        except BaseException:
            pinned_copy.close()
            raise
    if row.chunk_count:
        admitted_identity = (row.id, row.message_id, row.name, row.size, row.mime_type, row.chunk_count, row.content_sha256)
        size = row.size
        pinned_copy = tempfile.TemporaryFile(mode="w+b")
        try:
            await copy_room_file_content(db, row, pinned_copy)
            await db.commit()
            db.expire_all()
            current, current_document = await _authorized_room_download(db, room_id, file_id, credential)
            if current_document is not None or (
                current.id, current.message_id, current.name, current.size,
                current.mime_type, current.chunk_count, current.content_sha256,
            ) != admitted_identity:
                raise HTTPException(409, "File changed after sharing")
            pinned_copy.seek(0)
            response = PinnedFileStreamingResponse(
                pinned_copy, media_type="application/octet-stream",
                headers={**headers, "Content-Length": str(size)},
                on_close=on_close,
            )
            await db.commit()
            return response
        except RoomFileStorageError as exc:
            pinned_copy.close()
            raise HTTPException(409, "File changed after sharing") from exc
        except OSError as exc:
            pinned_copy.close()
            raise HTTPException(503, "File temporarily unavailable") from exc
        except BaseException:
            pinned_copy.close()
            raise
    # Never inline user-controlled HTML/SVG/JS under the authenticated origin.
    response = Response(
        content=row.content,
        media_type="application/octet-stream",
        headers=headers,
    )
    await db.commit()
    return response


async def _join_room(db: AsyncSession, room: ChatRoom, user: User, now: datetime) -> None:
    """Both invite types share the audience boundary; retries preserve history."""
    member = (await db.execute(select(ChatRoomMember).where(
        ChatRoomMember.room_id == room.id, ChatRoomMember.user_id == user.id,
    ).with_for_update())).scalar_one_or_none()
    if member is not None and member.status == "active":
        return
    if member is None:
        db.add(ChatRoomMember(
            id=generate_ulid(), room_id=room.id, user_id=user.id,
            role="member", status="active", visible_from=now, last_read_at=now,
        ))
    else:
        member.status = "active"
        member.role = "member"
        member.left_at = None
        member.visible_from = now
        member.last_read_at = now
    # A new audience cannot inherit the previous audience's Agent context.
    await discard_room_transcripts(db, room.id)


async def _manage_public_invite(db: AsyncSession, room_id: str, user_id: str, *, write: bool = False) -> ChatRoom:
    room, member = await _active_room(db, room_id, user_id, write=write)
    if room.kind != "group" or member.role not in {"owner", "admin"}:
        raise HTTPException(403, "Only the group owner or an administrator can manage public invitations")
    return room


@router.get("/{room_id}/public-invite")
async def get_public_invite(
    room_id: str, response: Response,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    room = await _manage_public_invite(db, room_id, user.id)
    response.headers["Cache-Control"] = "private, no-store"
    return {"token": room.public_invite_token}


@router.put("/{room_id}/public-invite")
async def enable_public_invite(
    room_id: str, body: PublicInviteRequest, response: Response,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    room = await _manage_public_invite(db, room_id, user.id, write=True)
    if body.reset or not room.public_invite_token:
        room.public_invite_token = secrets.token_urlsafe(32)
        await _commit_room_change(db, room_id)
    response.headers["Cache-Control"] = "private, no-store"
    return {"token": room.public_invite_token}


@router.delete("/{room_id}/public-invite", status_code=204)
async def disable_public_invite(
    room_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    room = await _manage_public_invite(db, room_id, user.id, write=True)
    room.public_invite_token = None
    await _commit_room_change(db, room_id)


@router.post("/public-invites/join")
async def join_public_invite(
    body: AcceptInviteRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    # Serialize with reset/disable/close and other membership changes. PostgreSQL
    # rechecks the token predicate after waiting for a concurrent room update.
    room = (await db.execute(select(ChatRoom).where(
        ChatRoom.public_invite_token == body.token,
        ChatRoom.kind == "group", ChatRoom.status == "active",
    ).with_for_update())).scalar_one_or_none()
    if room is None:
        raise HTTPException(404, "Public invitation is unavailable")
    if room.owner_entity_id:
        owner = await db.scalar(select(Entity.id).where(
            Entity.id == room.owner_entity_id, Entity.deleted_at.is_(None),
        ))
    else:
        owner = await db.scalar(select(User.id).where(
            User.id == room.owner_user_id, User.deleted_at.is_(None), User.status == "active",
        ))
    if owner is None:
        raise HTTPException(404, "Public invitation is unavailable")
    await _join_room(db, room, user, datetime.now(timezone.utc))
    await _commit_room_change(db, room.id)
    return await _room_view(db, room, user.id)


@router.post("/{room_id}/invites", status_code=201)
async def invite_member(
    room_id: str,
    body: InviteRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id, write=True)
    if room.kind == "dm" and member.role != "owner":
        raise HTTPException(403, "Cannot invite members")
    email = _email(body.email)
    if room.kind == "dm" and email != room.title:
        raise HTTPException(403, "Private chats can only reissue the original invitation")
    existing = (await db.execute(select(ChatRoomMember.id)
        .join(User, User.id == ChatRoomMember.user_id)
        .where(ChatRoomMember.room_id == room_id, ChatRoomMember.status == "active", func.lower(User.email) == email)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "Already a member")
    recipient = None
    if room.kind == "group":
        recipient = (await db.execute(select(User).where(
            func.lower(User.email) == email,
        ).with_for_update())).scalar_one_or_none()
    if room.kind == "group" and recipient and (
        recipient.deleted_at is not None or recipient.status not in {"active", "pending"}
    ):
        raise HTTPException(409, "This Manor account is unavailable")
    if room.kind == "group" and member.role == "member":
        another_invite = (await db.execute(select(ChatRoomInvite.id).where(
            ChatRoomInvite.room_id == room_id,
            ChatRoomInvite.email == email,
            ChatRoomInvite.status == "pending",
            ChatRoomInvite.expires_at > datetime.now(timezone.utc),
            ChatRoomInvite.invited_by_user_id.is_distinct_from(user.id),
        ).limit(1))).scalar_one_or_none()
        if another_invite:
            raise HTTPException(409, "Already invited")
    sender = await _select_group_invite_sender(db, user, body.sender_integration_id) if room.kind == "group" else None
    await _revoke_pending_invites(db, room_id, email)
    invite, token = _new_invite(room, email, user.id)
    db.add(invite)
    next_step = (
        "accept" if _verified_room_recipient(recipient, email) else "verify_email" if recipient else "sign_up"
    ) if room.kind == "group" else None
    if next_step == "accept":
        await _record_group_invite_notification(
            db, invite=invite, room=room, recipient=recipient, inviter=user, token=token,
        )
    await _commit_room_change(db, room_id)
    email_sent = (
        await _send_group_invite_email(request, invite=invite, room=room, inviter=user, token=token, sender=sender)
        if room.kind == "group" else None
    )
    return {
        "outcome": "invited", "invite_token": token,
        "invite_next_step": next_step,
        "email_sent": email_sent,
        "expires_at": invite.expires_at, "email": email,
    }


@router.get("/{room_id}/invites")
async def list_pending_invites(
    room_id: str,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id)
    if room.kind == "dm" and member.role != "owner":
        raise HTTPException(403, "Cannot view invitations")
    query = select(ChatRoomInvite).where(
        ChatRoomInvite.room_id == room.id,
        ChatRoomInvite.status == "pending",
    )
    if room.kind == "group" and member.role == "member":
        query = query.where(ChatRoomInvite.invited_by_user_id == user.id)
    invites = (await db.execute(query.order_by(
        ChatRoomInvite.expires_at.desc(), ChatRoomInvite.id.desc(),
    ).limit(200))).scalars().all()
    now = datetime.now(timezone.utc)
    latest_by_email = {}
    for invite in invites:
        latest_by_email.setdefault(invite.email, {
            "id": invite.id,
            "email": invite.email,
            "status": "expired" if invite.expires_at <= now else "pending",
            "expires_at": invite.expires_at,
        })
    response.headers["Cache-Control"] = "private, no-store"
    return list(latest_by_email.values())


@router.delete("/{room_id}/invites/{invite_id}", status_code=204)
async def cancel_invite(
    room_id: str,
    invite_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id, write=True)
    if room.kind != "group":
        raise HTTPException(403, "Cannot cancel invitations")
    invite = (await db.execute(select(ChatRoomInvite).where(
        ChatRoomInvite.id == invite_id,
        ChatRoomInvite.room_id == room_id,
        ChatRoomInvite.status == "pending",
    ).with_for_update())).scalar_one_or_none()
    if invite is None:
        raise HTTPException(404, "Invitation is unavailable")
    if member.role == "member" and invite.invited_by_user_id != user.id:
        raise HTTPException(404, "Invitation is unavailable")
    invite.status = "revoked"
    await _retire_invite_notifications(db, [invite])
    await _commit_room_change(db, room_id)


@router.post("/invites/accept")
async def accept_invite(
    body: AcceptInviteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    digest = hashlib.sha256(body.token.encode()).hexdigest()
    # For DMs, take the canonical advisory lock before row locks. Creation
    # takes it first too, avoiding a room/advisory lock inversion.
    initial_invite = (await db.execute(select(ChatRoomInvite).where(
        ChatRoomInvite.token_hash == digest,
    ))).scalar_one_or_none()
    if initial_invite is None:
        raise HTTPException(404, "Invitation is unavailable")
    initial_room = (await db.execute(select(ChatRoom).where(
        ChatRoom.id == initial_invite.room_id,
    ))).scalar_one_or_none()
    if initial_room and initial_room.kind == "dm":
        await _lock_dm_key(db, _dm_key(initial_room.owner_user_id, user.id))
    room = (await db.execute(select(ChatRoom).where(
        ChatRoom.id == initial_invite.room_id, ChatRoom.status == "active",
    ).with_for_update())).scalar_one_or_none()
    if room is None:
        raise HTTPException(404, "Conversation not found")
    invite = (await db.execute(select(ChatRoomInvite).where(
        ChatRoomInvite.token_hash == digest,
    ).with_for_update())).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if invite is None or invite.status != "pending" or invite.expires_at <= now:
        raise HTTPException(404, "Invitation is unavailable")
    if (
        user.email.casefold() != invite.email
        or (user.verified_email or "").casefold() != invite.email
        or user.email_verified_at is None
    ):
        raise HTTPException(403, "Verify the invited email before accepting")
    if room.owner_entity_id:
        owner = (await db.execute(select(Entity.id).where(
            Entity.id == room.owner_entity_id, Entity.deleted_at.is_(None),
        ))).scalar_one_or_none()
    else:
        owner = (await db.execute(select(User.id).where(
            User.id == room.owner_user_id, User.deleted_at.is_(None), User.status == "active",
        ))).scalar_one_or_none()
    if owner is None:
        raise HTTPException(404, "Conversation not found")
    if room.kind == "dm":
        key = _dm_key(room.owner_user_id, user.id)
        await _lock_dm_key(db, key)
        existing = (await db.execute(select(ChatRoom).where(
            ChatRoom.dm_key == key, ChatRoom.status == "active",
        ))).scalar_one_or_none()
        if existing and existing.id != room.id:
            room.status = "closed"
            room.dm_key = None
            room = existing
        else:
            room.dm_key = key
            room.title = None
    await _join_room(db, room, user, now)
    invite.status = "accepted"
    invite.accepted_by_user_id = user.id
    invite.accepted_at = now
    await _retire_invite_notifications(db, [invite])
    await _commit_room_change(db, room.id)
    return await _room_view(db, room, user.id)


@router.patch("/{room_id}/members/{member_user_id}")
async def update_member_role(
    room_id: str,
    member_user_id: str,
    body: UpdateMemberRoleRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, actor = await _active_room(db, room_id, user.id, write=True)
    if room.kind != "group" or actor.role != "owner":
        raise HTTPException(403, "Only the group owner can manage administrators")
    target = (await db.execute(select(ChatRoomMember).where(
        ChatRoomMember.room_id == room_id,
        ChatRoomMember.user_id == member_user_id,
        ChatRoomMember.status == "active",
    ).with_for_update())).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "Member not found")
    if target.role == "owner":
        raise HTTPException(403, "The group owner role cannot be changed")
    if target.role != body.role:
        target.role = body.role
        await _commit_room_change(db, room_id)
    return await _room_view(db, room, user.id)


@router.delete("/{room_id}/members/{member_user_id}", status_code=204)
async def remove_member(
    room_id: str,
    member_user_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room, actor = await _active_room(db, room_id, user.id, write=True)
    if room.kind != "group" or actor.role not in {"owner", "admin"}:
        raise HTTPException(403, "Cannot remove members")
    target = (await db.execute(select(ChatRoomMember).where(
        ChatRoomMember.room_id == room_id,
        ChatRoomMember.user_id == member_user_id,
        ChatRoomMember.status == "active",
    ).with_for_update())).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "Member not found")
    if target.role == "owner":
        raise HTTPException(403, "The group owner cannot be removed")
    if actor.role == "admin" and target.role == "admin":
        raise HTTPException(403, "Only the group owner can remove an administrator")
    target.status = "left"
    target.role = "member"
    target.left_at = datetime.now(timezone.utc)
    await discard_room_transcripts(db, room_id)
    await _commit_room_change(db, room_id)


@router.delete("/{room_id}/membership", status_code=204)
async def leave_group(
    room_id: str,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    room, member = await _active_room(db, room_id, user.id, write=True)
    if room.kind != "group":
        raise HTTPException(403, "Private chats cannot be left")
    if member.role == "owner":
        raise HTTPException(409, "Transfer ownership or close the group before leaving")
    member.status = "left"
    member.role = "member"
    member.left_at = datetime.now(timezone.utc)
    await discard_room_transcripts(db, room_id)
    db.add(AuditLog(
        id=generate_ulid(), entity_id=user.entity_id, user_id=user.id,
        action="chat_room.member.left", resource_type="chat_room",
        resource_id=room.id, details={"room_id": room.id},
    ))
    await _commit_room_change(db, room_id)
