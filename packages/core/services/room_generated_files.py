"""Publish an authorized, immutable Knowledge result into one group conversation.

Generation and Knowledge projection happen before this boundary. A room Agent
cannot publish a path, URL, or arbitrary Document id: the exact result must be
recorded by a succeeded, exactly claimed room tool call.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

import anyio
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.audit import AuditLog
from packages.core.models.base import generate_ulid
from packages.core.models.billing import CreditReservation
from packages.core.models.chat_room import (
    ChatRoom,
    ChatRoomAgentRun,
    ChatRoomFile,
    ChatRoomMember,
    ChatRoomMessage,
    ChatRoomToolApproval,
)
from packages.core.models.document import Document, DocumentFolder
from packages.core.models.media_job import MediaJob, MediaJobStatus
from packages.core.models.permission import Classification
from packages.core.models.user import User, UserMembership
from packages.core.services.document_access import effective_document_folder_policy
from packages.core.services.document_metadata import metadata_origin
from packages.core.services.entity_fs import EntityFilesystemError, open_entity_file_snapshot
from packages.core.services.knowledge_sync import ensure_folder_path


logger = logging.getLogger(__name__)


class RoomGeneratedFileError(RuntimeError):
    """A result is not eligible for publication to the current room audience."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RoomGeneratedFolder:
    entity_id: str
    rel_dir: str
    folder_id: str
    owner_user_id: str


async def _room_file_custodian(
    db: AsyncSession, *, room: ChatRoom, requester_user_id: str,
) -> tuple[User, User]:
    if room.kind != "group" or room.status != "active":
        raise RoomGeneratedFileError("room_unavailable")
    requester = (await db.execute(select(User).where(
        User.id == requester_user_id,
        User.status == "active", User.deleted_at.is_(None),
    ))).scalar_one_or_none()
    requester_member = (await db.execute(select(ChatRoomMember).where(
        ChatRoomMember.room_id == room.id,
        ChatRoomMember.user_id == requester_user_id,
        ChatRoomMember.status == "active",
    ))).scalar_one_or_none()
    if requester is None or requester_member is None:
        raise RoomGeneratedFileError("requester_not_authorized")
    custodian_id = room.created_by_user_id if room.owner_entity_id else room.owner_user_id
    custodian = (await db.execute(select(User).where(
        User.id == custodian_id,
        User.status == "active", User.deleted_at.is_(None),
    ))).scalar_one_or_none()
    custodian_member = (await db.execute(select(ChatRoomMember).where(
        ChatRoomMember.room_id == room.id,
        ChatRoomMember.user_id == custodian_id,
        ChatRoomMember.status == "active",
        ChatRoomMember.role.in_(("owner", "admin")),
    ))).scalar_one_or_none()
    if custodian is None or custodian_member is None:
        raise RoomGeneratedFileError("group_file_custodian_unavailable")
    if room.owner_entity_id:
        membership = (await db.execute(select(UserMembership.id).where(
            UserMembership.user_id == custodian.id,
            UserMembership.entity_id == room.owner_entity_id,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
        ))).scalar_one_or_none()
        if membership is None:
            raise RoomGeneratedFileError("group_file_custodian_unavailable")
    # The owner opted this group into AI file actions. External members write
    # only to the group's custodian-owned folder, never to their Entity FS.
    return requester, custodian


async def ensure_group_generated_folder(
    db: AsyncSession, *, room: ChatRoom, requester_user_id: str,
) -> RoomGeneratedFolder:
    """Stage the sole private Knowledge folder for this room's new outputs.

    Call in the same transaction as the generated Document. Folder naming is
    stable across room rename: an existing room-id-suffixed child is reused.
    """
    _, custodian = await _room_file_custodian(
        db, room=room, requester_user_id=requester_user_id,
    )
    entity_id = room.owner_entity_id or custodian.entity_id
    root_id = await ensure_folder_path(entity_id, "Group chat files", db=db)
    root = (await db.execute(select(DocumentFolder).where(
        DocumentFolder.id == root_id,
        DocumentFolder.entity_id == entity_id,
    ).with_for_update())).scalar_one_or_none()
    if root is None or root.client_visible or (
        root.visibility == "private" and root.owner_id != custodian.id
    ):
        raise RoomGeneratedFileError("group_folder_root_unavailable")
    matches = (await db.execute(select(DocumentFolder).where(
        DocumentFolder.entity_id == entity_id,
        DocumentFolder.parent_id == root_id,
        DocumentFolder.name.endswith(f"-{room.id}"),
    ).with_for_update())).scalars().all()
    if len(matches) > 1:
        raise RoomGeneratedFileError("group_folder_ambiguous")
    if matches:
        folder = matches[0]
    else:
        slug = re.sub(r"[^\w -]+", "", room.title or "", flags=re.UNICODE)
        slug = re.sub(r"\s+", "-", slug.strip()).strip("-_ ")[:80] or "group"
        folder_name = f"{slug}-{room.id}"
        folder = DocumentFolder(
            id=generate_ulid(), entity_id=entity_id, name=folder_name,
            parent_id=root_id, owner_id=custodian.id, visibility="private",
            classification="internal", client_visible=False,
        )
        db.add(folder)
        await db.flush()
    if (
        folder.owner_id != custodian.id or folder.visibility != "private"
        or folder.client_visible or folder.parent_id != root_id
    ):
        raise RoomGeneratedFileError("group_folder_not_private")
    return RoomGeneratedFolder(
        entity_id=entity_id,
        rel_dir=f"Group chat files/{folder.name}",
        folder_id=folder.id,
        owner_user_id=custodian.id,
    )


def _group_artifact_dir(rel_path: str, room_id: str) -> str:
    """Accept a stable room-id-suffixed Knowledge folder, even after rename."""
    if not rel_path or rel_path.startswith("/") or "\\" in rel_path:
        raise RoomGeneratedFileError("invalid_document_path")
    parts = rel_path.split("/")
    if (
        len(parts) < 3 or parts[0] != "Group chat files"
        or not parts[1].endswith(f"-{room_id}")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise RoomGeneratedFileError("document_outside_group_folder")
    return str(PurePosixPath(*parts[:-1]))


async def assert_group_generated_folder(
    db: AsyncSession, *, document: Document, room_id: str, custodian_user_id: str,
) -> None:
    """Validate the mixed-ownership Knowledge folder chain for a group result."""
    rel_dir = _group_artifact_dir(document.fs_path or "", room_id)
    group_folder = (await db.execute(select(DocumentFolder).where(
        DocumentFolder.id == document.folder_id,
        DocumentFolder.entity_id == document.entity_id,
    ))).scalar_one_or_none()
    if (
        group_folder is None or group_folder.name != PurePosixPath(rel_dir).name
        or group_folder.visibility != "private" or group_folder.client_visible
        or group_folder.owner_id != custodian_user_id or not group_folder.parent_id
    ):
        raise RoomGeneratedFileError("group_folder_not_private")
    root = (await db.execute(select(DocumentFolder).where(
        DocumentFolder.id == group_folder.parent_id,
        DocumentFolder.entity_id == document.entity_id,
    ))).scalar_one_or_none()
    if (
        root is None or root.name != "Group chat files"
        or root.parent_id is not None or root.client_visible
        or root.owner_id not in {None, custodian_user_id}
        or (root.visibility == "private" and root.owner_id != custodian_user_id)
    ):
        raise RoomGeneratedFileError("document_folder_changed")


def _pinned_sha256(entity_id: str, rel_path: str, expected_size: int) -> str:
    try:
        with open_entity_file_snapshot(entity_id, rel_path) as snapshot:
            if snapshot.stat.st_size != expected_size:
                raise RoomGeneratedFileError("document_size_changed")
            digest = hashlib.sha256()
            with open(snapshot.descriptor_path, "rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            return digest.hexdigest()
    except EntityFilesystemError as exc:
        raise RoomGeneratedFileError("document_unavailable") from exc


async def room_media_job_approved_for_execution(job_id: str) -> bool:
    """No paid provider submission before the exact room receipt is durable."""
    from packages.core.database import async_session
    from packages.core.services.room_tool_approval_service import (
        RoomToolApprovalError, _validated_scope,
    )

    async with async_session() as db:
        job = (await db.execute(select(MediaJob).where(
            MediaJob.id == job_id,
        ))).scalar_one_or_none()
        if job is None:
            return False
        if job.room_agent_run_id is None:
            return True
        run = (await db.execute(select(ChatRoomAgentRun).where(
            ChatRoomAgentRun.id == job.room_agent_run_id,
            ChatRoomAgentRun.status.in_(("running", "succeeded")),
        ))).scalar_one_or_none()
        if run is None:
            return False
        approval = (await db.execute(select(ChatRoomToolApproval).where(
            ChatRoomToolApproval.run_id == run.id,
            ChatRoomToolApproval.result_media_job_id == job.id,
            ChatRoomToolApproval.tool_name == "generate_file",
            ChatRoomToolApproval.status == "succeeded",
        ))).scalar_one_or_none()
        if approval is None:
            return False
        try:
            room, source, _ = await _validated_scope(db, run)
        except RoomToolApprovalError:
            return False
        expected_entity_id = room.owner_entity_id
        if expected_entity_id is None and room.owner_user_id:
            expected_entity_id = (await db.execute(select(User.entity_id).where(
                User.id == room.owner_user_id,
                User.status == "active",
                User.deleted_at.is_(None),
            ))).scalar_one_or_none()
        return (
            approval.room_id == room.id
            and approval.binding_id == run.binding_id
            and approval.requester_user_id == source.sender_user_id
            and approval.audience_fingerprint == run.audience_fingerprint
            and approval.ai_tool_execution_revision == run.ai_tool_execution_revision
            and bool(expected_entity_id)
            and job.entity_id == expected_entity_id
            and job.user_id == source.sender_user_id
            and job.agent_id == await _run_agent_id(db, run)
        )


async def store_room_media_bytes(
    *, job_id: str, content: bytes, extension: str, mime_type: str,
) -> str:
    """Project completed provider bytes privately before billing/publishing.

    The worker derives the folder and owner from the durable job/approval,
    never from a persisted model-supplied path. A changed room audience or
    revoked execution switch leaves the bytes unpublished.
    """
    from packages.core.ai.runtime.file_actions import RuntimeFileProjectionTransactionFactory
    from packages.core.services.document_metadata import merge_document_metadata
    from packages.core.services.entity_fs import get_entity_root
    from packages.core.services.room_file_mutation_tools import _safe_name
    from packages.core.services.room_tool_approval_service import (
        RoomToolApprovalError, _validated_scope,
    )

    if not content or not extension.startswith(".") or not mime_type:
        raise RoomGeneratedFileError("invalid_media_result")
    from packages.core.database import async_session

    async with async_session() as lookup:
        entity_id = (await lookup.execute(select(MediaJob.entity_id).where(
            MediaJob.id == job_id,
            MediaJob.room_agent_run_id.is_not(None),
        ))).scalar_one_or_none()
    if not entity_id:
        raise RoomGeneratedFileError("media_job_unavailable")
    async with RuntimeFileProjectionTransactionFactory.create(entity_id) as transaction:
        db = transaction.db
        job = (await db.execute(select(MediaJob).where(
            MediaJob.id == job_id,
            MediaJob.entity_id == entity_id,
        ).with_for_update())).scalar_one_or_none()
        if job is None or not job.room_agent_run_id or job.status not in {
            MediaJobStatus.PENDING.value, MediaJobStatus.PROCESSING.value,
        }:
            raise RoomGeneratedFileError("media_job_unavailable")
        run = (await db.execute(select(ChatRoomAgentRun).where(
            ChatRoomAgentRun.id == job.room_agent_run_id,
            ChatRoomAgentRun.status.in_(("running", "succeeded")),
        ).with_for_update())).scalar_one_or_none()
        if run is None:
            raise RoomGeneratedFileError("run_unavailable")
        try:
            room, source, _ = await _validated_scope(db, run)
        except RoomToolApprovalError as exc:
            raise RoomGeneratedFileError(exc.code) from exc
        approval = (await db.execute(select(ChatRoomToolApproval).where(
            ChatRoomToolApproval.run_id == run.id,
            ChatRoomToolApproval.room_id == room.id,
            ChatRoomToolApproval.tool_name == "generate_file",
            ChatRoomToolApproval.result_media_job_id == job.id,
            ChatRoomToolApproval.status == "succeeded",
        ).with_for_update())).scalar_one_or_none()
        if approval is None or (
            approval.binding_id != run.binding_id
            or approval.requester_user_id != source.sender_user_id
            or approval.audience_fingerprint != run.audience_fingerprint
            or approval.ai_tool_execution_revision != run.ai_tool_execution_revision
            or job.user_id != source.sender_user_id
            or job.agent_id != await _run_agent_id(db, run)
        ):
            raise RoomGeneratedFileError("media_result_not_approved")
        folder = await ensure_group_generated_folder(
            db, room=room, requester_user_id=source.sender_user_id,
        )
        if folder.entity_id != entity_id:
            raise RoomGeneratedFileError("media_entity_changed")
        requested_name = str((job.params or {}).get("output_name") or "").strip()
        if requested_name and ("/" in requested_name or "\\" in requested_name):
            raise RoomGeneratedFileError("invalid_media_filename")
        stem = requested_name.rsplit(".", 1)[0] if requested_name else f"generated-{job.kind}"
        name = _safe_name(f"{stem}{extension}", generation_format=False)
        digest = hashlib.sha256(content).hexdigest()
        rel_path = f"{folder.rel_dir}/{approval.id}-{name}"
        existing = (await db.execute(select(Document).where(
            Document.entity_id == entity_id,
            Document.fs_path == rel_path,
            Document.is_trashed.is_(False),
        ).with_for_update())).scalar_one_or_none()
        if existing is not None:
            origin = metadata_origin(existing.metadata_)
            if (
                existing.folder_id != folder.folder_id
                or existing.owner_id != folder.owner_user_id
                or existing.created_by != source.sender_user_id
                or existing.visibility != "private" or existing.client_visible
                or existing.file_size != len(content)
                or origin.get("room_agent_run_id") != run.id
                or origin.get("room_tool_call_id") != approval.tool_call_id
            ):
                raise RoomGeneratedFileError("media_output_collision")
            existing_digest = await anyio.to_thread.run_sync(
                _pinned_sha256, entity_id, rel_path, len(content),
            )
            if existing_digest != digest:
                raise RoomGeneratedFileError("media_output_changed")
            return existing.id
        abs_path = transaction.write_bytes(
            rel_path, content, expected_content_sha256=digest,
            expected_size=len(content), allow_empty=False, require_missing=True,
        )
        sync = await transaction.project_file(
            abs_path=abs_path, entity_root=get_entity_root(entity_id),
            source="ai_generated", created_by=source.sender_user_id,
            force=True, workspace_id=None, task_id=None,
            agent_id=job.agent_id, conversation_id=None,
            user_id=source.sender_user_id, tool_name="generate_file",
            expected_content_sha256=digest,
        )
        doc = (await db.execute(select(Document).where(
            Document.id == sync.document_id,
            Document.entity_id == entity_id,
        ).with_for_update())).scalar_one_or_none()
        if doc is None or doc.fs_path != rel_path:
            raise RoomGeneratedFileError("media_projection_changed")
        doc.name = name
        doc.mime_type = mime_type
        doc.file_size = len(content)
        doc.file_url = None
        doc.source = "ai_generated"
        doc.owner_id = folder.owner_user_id
        doc.created_by = source.sender_user_id
        doc.folder_id = folder.folder_id
        doc.visibility = "private"
        doc.classification = "internal"
        doc.client_visible = False
        doc.quarantine_status = "clean"
        doc.metadata_ = merge_document_metadata(
            doc.metadata_,
            origin={
                "room_id": room.id, "room_agent_run_id": run.id,
                "room_tool_call_id": approval.tool_call_id,
                "user_id": source.sender_user_id,
                "agent_id": job.agent_id,
            },
        )
        await transaction.commit()
    return doc.id


async def project_room_generated_document(
    db: AsyncSession, *, run_id: str, document_id: str,
    media_job_id: str | None = None, message_id: str | None = None,
) -> ChatRoomFile:
    """Stage one idempotent room file/message; caller commits the transaction.

    Lock ordering matches room Agent execution (run, then room). A pending or
    failed media job, changed audience, invalid receipt, or mutated source file
    never gains a room-visible reference.
    """
    from packages.core.services.room_tool_approval_service import (
        RoomToolApprovalError,
        _validated_scope,
    )

    run = (await db.execute(select(ChatRoomAgentRun).where(
        ChatRoomAgentRun.id == run_id,
    ).with_for_update())).scalar_one_or_none()
    if run is None or run.status != "succeeded":
        raise RoomGeneratedFileError("run_not_completed")
    try:
        room, source, _ = await _validated_scope(db, run)
    except RoomToolApprovalError as exc:
        raise RoomGeneratedFileError(exc.code) from exc
    if run.ai_tool_execution_revision is None:
        raise RoomGeneratedFileError("execution_not_approved")

    approval_query = select(ChatRoomToolApproval).where(
        ChatRoomToolApproval.run_id == run.id,
        ChatRoomToolApproval.room_id == room.id,
        ChatRoomToolApproval.binding_id == run.binding_id,
        ChatRoomToolApproval.requester_user_id == source.sender_user_id,
        ChatRoomToolApproval.audience_fingerprint == run.audience_fingerprint,
        ChatRoomToolApproval.ai_tool_execution_revision == run.ai_tool_execution_revision,
        ChatRoomToolApproval.tool_name.in_((
            "room_create_file", "room_replace_file", "room_generate_media",
            "generate_file",
        )),
        ChatRoomToolApproval.status == "succeeded",
    )
    if media_job_id is None:
        approval_query = approval_query.where(
            ChatRoomToolApproval.result_document_id == document_id,
            ChatRoomToolApproval.result_media_job_id.is_(None),
        )
    else:
        approval_query = approval_query.where(
            ChatRoomToolApproval.result_media_job_id == media_job_id,
            ChatRoomToolApproval.result_document_id.is_(None),
        )
    approvals = (await db.execute(approval_query.with_for_update())).scalars().all()
    if len(approvals) != 1:
        raise RoomGeneratedFileError("result_not_approved")
    approval = approvals[0]
    approver_query = select(ChatRoomMember).join(
        User, User.id == ChatRoomMember.user_id,
    ).where(
        ChatRoomMember.room_id == room.id,
        ChatRoomMember.user_id == approval.decided_by_user_id,
        ChatRoomMember.status == "active",
        User.status == "active",
        User.deleted_at.is_(None),
    )
    from packages.core.services.room_tool_approval_service import room_tool_policy
    current_policy = await room_tool_policy(db, room=room, source=source,
        tool_name=approval.tool_name, arguments={})
    if not current_policy.allowed and not current_policy.pause_for_hitl:
        raise RoomGeneratedFileError("blocked_by_governance")
    if not (current_policy.allowed and approval.decided_by_user_id == source.sender_user_id):
        approver_query = approver_query.where(ChatRoomMember.role.in_(("owner", "admin")))
    approver = (await db.execute(approver_query)).scalar_one_or_none()
    if approver is None:
        raise RoomGeneratedFileError("approver_not_authorized")

    requester, custodian = await _room_file_custodian(
        db, room=room, requester_user_id=source.sender_user_id,
    )
    expected_entity_id = room.owner_entity_id or custodian.entity_id
    if not expected_entity_id:
        raise RoomGeneratedFileError("document_owner_unavailable")
    if media_job_id is not None:
        job = (await db.execute(select(MediaJob).where(
            MediaJob.id == media_job_id,
        ).with_for_update())).scalar_one_or_none()
        if (
            job is None or not MediaJobStatus.is_completed(job.status)
            or job.room_agent_run_id != run.id
            or job.entity_id != expected_entity_id
            or job.user_id != requester.id
            or job.agent_id != (await _run_agent_id(db, run))
            or (job.params or {}).get("result_document_id") != document_id
        ):
            raise RoomGeneratedFileError("media_result_unavailable")
        if not job.byok and int(job.credits or 0) > 0:
            # A sponsored trial holds the reservation against its payer and
            # records the job's entity as the data owner.
            reservation_status = (await db.execute(select(CreditReservation.status).where(
                CreditReservation.source_kind == "media_job",
                CreditReservation.source_id == job.id,
                or_(
                    CreditReservation.entity_id == job.entity_id,
                    CreditReservation.meta["data_owner_entity_id"].astext == job.entity_id,
                ),
            ).order_by(CreditReservation.created_at.desc()).limit(1))).scalar_one_or_none()
            if reservation_status != "consumed":
                raise RoomGeneratedFileError("media_credits_unsettled")

    document = (await db.execute(select(Document).where(
        Document.id == document_id,
        Document.entity_id == expected_entity_id,
        Document.is_trashed.is_(False),
    ).with_for_update())).scalar_one_or_none()
    if (
        document is None or document.source != "ai_generated"
        or not document.fs_path or document.file_url
        or document.file_size is None or document.file_size <= 0
        or document.owner_id != custodian.id or document.created_by != requester.id
        or document.visibility != "private" or document.client_visible
        or document.quarantine_status != "clean"
    ):
        raise RoomGeneratedFileError("document_not_group_private")
    # This is a fresh result written under this room's private folder. Its
    # exact succeeded room tool receipt is the external-publication grant;
    # generic SHARE_EXTERNAL grants still govern preexisting Knowledge files.
    classification, visibility, client_visible = await effective_document_folder_policy(db, document)
    if (
        classification not in {Classification.PUBLIC, Classification.INTERNAL}
        or visibility != "private" or client_visible
    ):
        raise RoomGeneratedFileError("document_policy_not_group_safe")
    await assert_group_generated_folder(
        db, document=document, room_id=room.id, custodian_user_id=custodian.id,
    )
    origin = metadata_origin(document.metadata_)
    if origin.get("user_id") != requester.id:
        raise RoomGeneratedFileError("document_origin_changed")
    if media_job_id is None:
        if (
            origin.get("room_id") != room.id
            or origin.get("room_agent_run_id") != run.id
            or origin.get("room_tool_call_id") != approval.tool_call_id
        ):
            raise RoomGeneratedFileError("document_origin_changed")
    elif origin.get("agent_id") != job.agent_id:
        # The exact succeeded approval -> MediaJob -> result_document_id chain
        # is the async provenance receipt; legacy media savers do not yet put
        # the room run id into Document metadata.
        raise RoomGeneratedFileError("document_origin_changed")
    name = str(document.name or "").strip()
    path_name = PurePosixPath(document.fs_path).name
    if (
        not name or len(name) > 255 or "/" in name or "\\" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or (path_name != name and not path_name.endswith(f"-{name}"))
    ):
        raise RoomGeneratedFileError("invalid_document_name")
    mime_type = str(document.mime_type or "application/octet-stream").strip().lower()
    if not mime_type or len(mime_type) > 255:
        raise RoomGeneratedFileError("invalid_document_mime")
    digest = await anyio.to_thread.run_sync(
        _pinned_sha256, document.entity_id, document.fs_path, document.file_size,
    )

    existing = (await db.execute(select(ChatRoomFile).where(
        ChatRoomFile.source_run_id == run.id,
        ChatRoomFile.document_id == document.id,
    ))).scalar_one_or_none()
    if existing is not None:
        if existing.deleted_at is not None:
            raise RoomGeneratedFileError("published_file_deleted")
        if (
            existing.document_entity_id != document.entity_id
            or existing.size != document.file_size
            or existing.content_sha256 != digest
        ):
            raise RoomGeneratedFileError("published_document_changed")
        return existing

    now = datetime.now(timezone.utc)
    if message_id is not None:
        message = (await db.execute(select(ChatRoomMessage).where(
            ChatRoomMessage.id == message_id,
        ).with_for_update())).scalar_one_or_none()
        if (
            message is None or message.room_id != room.id
            or message.sender_user_id is not None
            or message.sender_agent_binding_id != run.binding_id
            or message.reply_to_message_id != run.source_message_id
            or message.agent_run_status != "succeeded"
        ):
            raise RoomGeneratedFileError("invalid_result_message")
    else:
        # Asynchronous media results may arrive after the final reply.
        message = ChatRoomMessage(
            id=generate_ulid(), room_id=room.id,
            sender_agent_binding_id=run.binding_id,
            sender_agent_name=run.agent_name,
            sender_agent_avatar_url=run.agent_avatar_url,
            reply_to_message_id=run.source_message_id,
            content="Generated a file", created_at=now,
        )
        db.add(message)
        await db.flush()
    attachment = ChatRoomFile(
        id=generate_ulid(), message_id=message.id,
        name=name, mime_type=mime_type, size=document.file_size,
        content=None, document_id=document.id,
        document_entity_id=document.entity_id,
        content_sha256=digest, source_run_id=run.id,
        source_media_job_id=media_job_id, created_at=now,
    )
    db.add(attachment)
    room.last_message_at = now
    db.add(AuditLog(
        id=generate_ulid(), entity_id=room.owner_entity_id,
        user_id=approval.decided_by_user_id,
        action="chat_room.generated_file.published",
        resource_type="chat_room_file", resource_id=attachment.id,
        details={
            "room_id": room.id, "run_id": run.id,
            "approval_id": approval.id, "document_id": document.id,
            "media_job_id": media_job_id,
            "size": document.file_size, "sha256": digest,
        },
    ))
    await db.flush()
    return attachment


async def _run_agent_id(db: AsyncSession, run: ChatRoomAgentRun) -> str | None:
    """Resolve the current binding rather than trusting result metadata."""
    from packages.core.models.chat_room import ChatRoomAgentBinding
    from packages.core.models.workspace import AgentSubscription

    return (await db.execute(select(AgentSubscription.agent_id).join(
        ChatRoomAgentBinding,
        ChatRoomAgentBinding.subscription_id == AgentSubscription.id,
    ).where(
        ChatRoomAgentBinding.id == run.binding_id,
        ChatRoomAgentBinding.room_id == run.room_id,
        ChatRoomAgentBinding.status == "active",
    ))).scalar_one_or_none()


async def project_completed_room_media_job(job_id: str) -> bool:
    """Retry-safe worker handoff after media billing and Knowledge projection."""
    from packages.core.database import async_session

    async with async_session() as db:
        job = (await db.execute(select(MediaJob).where(
            MediaJob.id == job_id,
            MediaJob.status == MediaJobStatus.COMPLETED.value,
            MediaJob.room_agent_run_id.is_not(None),
        ))).scalar_one_or_none()
        if job is None:
            return False
        document_id = str((job.params or {}).get("result_document_id") or "")
        if not document_id:
            return False
        try:
            await project_room_generated_document(
                db, run_id=job.room_agent_run_id,
                document_id=document_id, media_job_id=job.id,
            )
            await db.commit()
            return True
        except RoomGeneratedFileError as exc:
            await db.rollback()
            logger.info("Room media job %s remains private: %s", job_id, exc.code)
            return False


async def reconcile_completed_room_media_jobs(*, limit: int = 20) -> dict[str, int]:
    """Bounded recovery after a worker dies between media commit and room link."""
    from packages.core.database import async_session

    async with async_session() as db:
        job_ids = (await db.execute(select(MediaJob.id).outerjoin(
            ChatRoomFile, ChatRoomFile.source_media_job_id == MediaJob.id,
        ).where(
            MediaJob.status == MediaJobStatus.COMPLETED.value,
            MediaJob.room_agent_run_id.is_not(None),
            MediaJob.params["result_document_id"].astext.is_not(None),
            ChatRoomFile.id.is_(None),
        ).order_by(MediaJob.completed_at.desc(), MediaJob.id.desc()).limit(limit))).scalars().all()
    projected = 0
    for job_id in job_ids:
        try:
            projected += int(await project_completed_room_media_job(job_id))
        except Exception:  # noqa: BLE001
            logger.warning("Room media job %s projection retry failed", job_id, exc_info=True)
    return {"checked": len(job_ids), "projected": projected}


__all__ = [
    "RoomGeneratedFileError",
    "RoomGeneratedFolder",
    "ensure_group_generated_folder",
    "project_room_generated_document",
    "project_completed_room_media_job",
    "reconcile_completed_room_media_jobs",
]
