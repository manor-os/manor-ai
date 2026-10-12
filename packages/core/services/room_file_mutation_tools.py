"""Authorized, room-scoped file mutations for group Agents.

No tool accepts a path, URL, Entity id, Workspace id, or Document id. The
model may name a new file or select an existing room attachment by file_id;
the claimed run and the room's private Knowledge folder supply authority.
"""
from __future__ import annotations

import csv
import asyncio
import hashlib
import importlib.util
import io
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from packages.core.services.room_generation_scope import RoomGenerationScope

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import database
from packages.core.ai.runtime.file_actions import RuntimeFileProjectionTransactionFactory
from packages.core.models.chat_room import (
    ChatRoom, ChatRoomAgentBinding, ChatRoomAgentRun, ChatRoomFile, ChatRoomMember,
    ChatRoomMessage, ChatRoomToolApproval,
)
from packages.core.models.document import Document
from packages.core.models.user import User
from packages.core.models.workspace import AgentSubscription
from packages.core.services.document_metadata import merge_document_metadata
from packages.core.services.entity_fs import (
    EntityFilesystemError, get_entity_root, open_entity_file_snapshot,
)
from packages.core.services.knowledge_sync import find_folder_path
from packages.core.services.room_agent_runtime import room_audience_fingerprint
from packages.core.services.room_generated_files import (
    RoomGeneratedFileError, _group_artifact_dir, ensure_group_generated_folder,
)
from packages.core.services.room_tool_approval_service import (
    RoomToolApprovalError, _validated_scope, canonical_arguments_hash,
)


_MAX_CONTENT_CHARS = 48_000
_MAX_GENERATED_BYTES = 10 * 1024 * 1024
_MAX_ROWS = 1_000
_MAX_COLUMNS = 64
_MAX_CELL_CHARS = 2_000
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}\Z")
_FORMATS = {
    "txt": "text/plain", "md": "text/markdown", "csv": "text/csv",
    "json": "application/json", "html": "text/html",
    "py": "text/plain", "js": "text/plain", "ts": "text/plain",
    "tsx": "text/plain", "jsx": "text/plain", "java": "text/plain",
    "go": "text/plain", "rs": "text/plain", "c": "text/plain",
    "cpp": "text/plain", "h": "text/plain", "rb": "text/plain",
    "sql": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
if importlib.util.find_spec("reportlab") is not None or importlib.util.find_spec("fpdf") is not None:
    _FORMATS["pdf"] = "application/pdf"


class RoomFileMutationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RoomFileMutationResult:
    json_result: str
    document_id: str | None = None
    media_job_id: str | None = None
    sandbox_id: str | None = None
    reference_hashes: tuple[tuple[str, str], ...] = ()


def room_file_mutation_tool_schemas() -> list[dict[str, Any]]:
    """Small, path-free schemas. Execution requires an exact claimed receipt."""
    name = {"type": "string", "minLength": 1, "maxLength": 128,
            "description": "Filename including extension; no folders or paths."}
    content = {"type": "string", "minLength": 1, "maxLength": _MAX_CONTENT_CHARS,
               "description": (
                   "File contents. For DOCX/PPTX use Markdown; for XLSX use CSV rows. "
                   f"Supported extensions: {', '.join(sorted(_FORMATS))}."
               )}
    file_id = {"type": "string", "minLength": 26, "maxLength": 26,
               "description": "An attachment ID returned by room_list_files."}
    definitions = (
        ("room_create_file", "Create a new file in this group's private Knowledge folder.",
         {"name": name, "content": content}, ["name", "content"]),
        ("room_replace_file", "Create a new version of a generated group file without changing old bytes.",
         {"file_id": file_id, "content": content}, ["file_id", "content"]),
        ("room_rename_file", "Rename only the group display/download name of a generated file.",
         {"file_id": file_id, "new_name": name}, ["file_id", "new_name"]),
        ("room_delete_file", "Remove a generated file from this group; preserve its private Knowledge original.",
         {"file_id": file_id}, ["file_id"]),
    )
    return [{"type": "function", "function": {
        "name": tool_name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
    }} for tool_name, description, properties, required in definitions]


def _safe_name(value: Any, *, generation_format: bool = True) -> str:
    if not isinstance(value, str):
        raise RoomFileMutationError("invalid_filename")
    name = unicodedata.normalize("NFC", value).strip()
    if (
        not name or len(name) > 128 or name in {".", ".."}
        or name.startswith(".") or "/" in name or "\\" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or PurePosixPath(name).name != name
    ):
        raise RoomFileMutationError("invalid_filename")
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if re.fullmatch(r"[a-z0-9]{1,12}", extension) is None:
        raise RoomFileMutationError("invalid_filename")
    if generation_format and extension not in _FORMATS:
        raise RoomFileMutationError("unsupported_file_format")
    return name


def _safe_file_id(value: Any) -> str:
    if not isinstance(value, str) or _ULID.fullmatch(value) is None:
        raise RoomFileMutationError("invalid_file_id")
    return value


def validate_room_file_mutation(
    tool_name: str, arguments: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Normalize a call before asking humans to approve its exact arguments."""
    if not isinstance(arguments, dict):
        raise RoomFileMutationError("invalid_arguments")
    if tool_name == "room_create_file":
        required = {"name", "content"}
    elif tool_name == "room_replace_file":
        required = {"file_id", "content"}
    elif tool_name == "room_rename_file":
        required = {"file_id", "new_name"}
    elif tool_name == "room_delete_file":
        required = {"file_id"}
    else:
        raise RoomFileMutationError("unsupported_room_file_tool")
    if set(arguments) != required:
        raise RoomFileMutationError("invalid_arguments")
    normalized: dict[str, Any] = {}
    if "file_id" in required:
        normalized["file_id"] = _safe_file_id(arguments["file_id"])
    if "name" in required:
        normalized["name"] = _safe_name(arguments["name"])
    if "new_name" in required:
        normalized["new_name"] = _safe_name(arguments["new_name"], generation_format=False)
    if "content" in required:
        content = arguments["content"]
        if not isinstance(content, str) or not content or len(content) > _MAX_CONTENT_CHARS:
            raise RoomFileMutationError("invalid_content")
        normalized["content"] = content
        if tool_name == "room_create_file":
            extension = normalized["name"].rsplit(".", 1)[-1].lower()
            if extension == "json":
                try:
                    json.loads(content)
                except ValueError as exc:
                    raise RoomFileMutationError("invalid_json") from exc
            elif extension == "xlsx":
                _spreadsheet_rows(content)
    canonical_arguments_hash(arguments)
    content_receipt = ""
    if "content" in normalized:
        encoded = normalized["content"].encode("utf-8")
        content_receipt = (
            f"; {len(encoded)} UTF-8 bytes; "
            f"SHA-256 {hashlib.sha256(encoded).hexdigest()[:12]}"
        )
    if tool_name == "room_create_file":
        kind = normalized["name"].rsplit(".", 1)[-1].upper()
        summary = f"Create {kind} group file: {normalized['name']}{content_receipt}"
    elif tool_name == "room_replace_file":
        summary = (
            f"Create new version of group file {normalized['file_id'][:10]}…"
            f"{content_receipt}"
        )
    elif tool_name == "room_rename_file":
        summary = f"Rename group file {normalized['file_id']} to {normalized['new_name']}"
    else:
        summary = f"Remove group file {normalized['file_id']} from the group"
    return normalized, summary


async def _render_file(name: str, content: str) -> tuple[bytes, str]:
    extension = name.rsplit(".", 1)[-1].lower()
    if extension == "json":
        try:
            json.loads(content)
        except ValueError as exc:
            raise RoomFileMutationError("invalid_json") from exc
    if extension == "docx":
        from packages.core.services.docgen_service import generate_docx
        output = await generate_docx(name.rsplit(".", 1)[0], content)
    elif extension == "pdf":
        from packages.core.services.docgen_service import generate_pdf
        output = await generate_pdf(name.rsplit(".", 1)[0], content)
    elif extension == "pptx":
        from packages.core.services.docgen_service import generate_pptx
        output = await generate_pptx(name.rsplit(".", 1)[0], content)
    elif extension == "xlsx":
        from openpyxl import Workbook

        rows = _spreadsheet_rows(content)
        workbook = Workbook()
        try:
            sheet = workbook.active
            for row in rows:
                sheet.append(row)
            stream = io.BytesIO()
            workbook.save(stream)
            output = stream.getvalue()
        finally:
            workbook.close()
    else:
        output = content.encode("utf-8")
    if not output or len(output) > _MAX_GENERATED_BYTES:
        raise RoomFileMutationError("generated_file_size_limit")
    return output, _FORMATS[extension]


def _spreadsheet_rows(content: str) -> list[list[str]]:
    try:
        rows = list(csv.reader(io.StringIO(content)))
    except csv.Error as exc:
        raise RoomFileMutationError("invalid_spreadsheet_csv") from exc
    if (
        not rows or len(rows) > _MAX_ROWS
        or any(len(row) > _MAX_COLUMNS or any(len(cell) > _MAX_CELL_CHARS for cell in row)
               for row in rows)
    ):
        raise RoomFileMutationError("spreadsheet_limit_exceeded")
    return rows


async def _claimed_scope(
    db: AsyncSession, *, tool_name: str, arguments: dict[str, Any],
    room_id: str, run_id: str, approval_id: str,
    expected_execution_revision: int, expected_entity_id: str,
) -> tuple[ChatRoom, ChatRoomAgentRun, ChatRoomToolApproval, str, str, datetime]:
    """Recheck the claimed receipt and live authority under run -> room locks."""
    if type(expected_execution_revision) is not int or expected_execution_revision < 0:
        raise RoomFileMutationError("execution_setting_changed")
    run = (await db.execute(select(ChatRoomAgentRun).where(
        ChatRoomAgentRun.id == run_id,
        ChatRoomAgentRun.room_id == room_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if (
        run is None or run.status != "running"
        or run.ai_tool_execution_revision != expected_execution_revision
    ):
        raise RoomFileMutationError("run_unavailable")
    try:
        room, source, _ = await _validated_scope(db, run)
    except RoomToolApprovalError as exc:
        raise RoomFileMutationError(exc.code) from exc
    if (
        room.id != room_id or room.ai_tool_execution_revision != expected_execution_revision
        or not room.ai_tool_execution_enabled
        or (room.owner_entity_id or expected_entity_id) != expected_entity_id
    ):
        raise RoomFileMutationError("execution_setting_changed")
    approval = (await db.execute(select(ChatRoomToolApproval).where(
        ChatRoomToolApproval.id == approval_id,
        ChatRoomToolApproval.run_id == run_id,
        ChatRoomToolApproval.room_id == room_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if (
        approval is None or approval.status != "claimed" or approval.claimed_at is None
        or approval.tool_name != tool_name
        or approval.arguments_hash != canonical_arguments_hash(arguments)
        or approval.requester_user_id != source.sender_user_id
        or approval.binding_id != run.binding_id
        or approval.audience_fingerprint != run.audience_fingerprint
        or approval.ai_tool_execution_revision != expected_execution_revision
    ):
        raise RoomFileMutationError("tool_call_not_claimed")
    approver_query = select(ChatRoomMember.id).join(
        User, User.id == ChatRoomMember.user_id,
    ).where(
        ChatRoomMember.room_id == room_id,
        ChatRoomMember.user_id == approval.decided_by_user_id,
        ChatRoomMember.status == "active",
        User.status == "active",
        User.deleted_at.is_(None),
    )
    from packages.core.services.room_tool_approval_service import room_tool_policy
    current_policy = await room_tool_policy(db, room=room, source=source,
        tool_name=approval.tool_name, arguments=arguments)
    if not current_policy.allowed and not current_policy.pause_for_hitl:
        raise RoomFileMutationError("blocked_by_governance")
    # An allowed policy covers an active member's own call; reviewed calls
    # retain the owner/admin decision boundary.
    if not (current_policy.allowed and approval.decided_by_user_id == source.sender_user_id):
        approver_query = approver_query.where(ChatRoomMember.role.in_(("owner", "admin")))
    approver = (await db.execute(approver_query)).scalar_one_or_none()
    if approver is None:
        raise RoomFileMutationError("approver_not_authorized")
    fingerprint, common_visible_from = await room_audience_fingerprint(db, room_id)
    if (
        not fingerprint or fingerprint != run.audience_fingerprint
        or common_visible_from is None or source.created_at < common_visible_from
    ):
        raise RoomFileMutationError("audience_changed")
    return (
        room, run, approval, source.sender_user_id,
        approval.tool_call_id, common_visible_from,
    )


def _assert_pinned_file(entity_id: str, rel_path: str, digest: str, size: int) -> None:
    with open_entity_file_snapshot(
        entity_id, rel_path, expected_content_sha256=digest,
    ) as snapshot:
        if snapshot.stat.st_size != size:
            raise RoomFileMutationError("file_changed")


async def _target_generated_file(
    db: AsyncSession, *, room_id: str, file_id: str,
    common_visible_from: datetime,
) -> tuple[ChatRoomFile, Document]:
    attachment = (await db.execute(select(ChatRoomFile).join(
        ChatRoomMessage, ChatRoomMessage.id == ChatRoomFile.message_id,
    ).where(
        ChatRoomFile.id == file_id,
        ChatRoomMessage.room_id == room_id,
        ChatRoomMessage.created_at >= common_visible_from,
        ChatRoomFile.deleted_at.is_(None),
        ChatRoomFile.document_id.is_not(None),
        ChatRoomFile.source_run_id.is_not(None),
    ).with_for_update())).scalar_one_or_none()
    if attachment is None:
        raise RoomFileMutationError("room_file_unavailable")
    producer = (await db.execute(select(ChatRoomAgentRun.id).where(
        ChatRoomAgentRun.id == attachment.source_run_id,
        ChatRoomAgentRun.room_id == room_id,
    ))).scalar_one_or_none()
    document = (await db.execute(select(Document).where(
        Document.id == attachment.document_id,
        Document.entity_id == attachment.document_entity_id,
        Document.source == "ai_generated",
        Document.visibility == "private",
        Document.quarantine_status == "clean",
        Document.is_trashed.is_(False),
        Document.fs_path.is_not(None),
        Document.file_size == attachment.size,
    ).with_for_update())).scalar_one_or_none()
    if producer is None or document is None or not attachment.content_sha256:
        raise RoomFileMutationError("room_file_unavailable")
    if (
        (document.mime_type or "application/octet-stream").split(";", 1)[0].strip().lower()
        != attachment.mime_type.split(";", 1)[0].strip().lower()
    ):
        raise RoomFileMutationError("room_file_changed")
    try:
        rel_dir = _group_artifact_dir(document.fs_path, room_id)
    except RoomGeneratedFileError as exc:
        raise RoomFileMutationError(exc.code) from exc
    folder_id = await find_folder_path(document.entity_id, rel_dir, db=db)
    if folder_id is None or folder_id != document.folder_id:
        raise RoomFileMutationError("room_file_changed")
    try:
        await asyncio.to_thread(
            _assert_pinned_file, document.entity_id, document.fs_path,
            attachment.content_sha256, attachment.size,
        )
    except EntityFilesystemError as exc:
        raise RoomFileMutationError("room_file_changed") from exc
    return attachment, document


async def _create_document(
    *, tool_name: str, arguments: dict[str, Any], normalized: dict[str, Any],
    room_id: str, run_id: str, approval_id: str,
    expected_execution_revision: int, expected_entity_id: str,
) -> RoomFileMutationResult:
    async with RuntimeFileProjectionTransactionFactory.create(expected_entity_id) as transaction:
        db = transaction.db
        room, run, approval, requester_id, tool_call_id, floor = await _claimed_scope(
            db, tool_name=tool_name, arguments=arguments,
            room_id=room_id, run_id=run_id, approval_id=approval_id,
            expected_execution_revision=expected_execution_revision,
            expected_entity_id=expected_entity_id,
        )
        replaced_file_id = None
        if tool_name == "room_replace_file":
            replaced_file_id = normalized["file_id"]
            target, _ = await _target_generated_file(
                db, room_id=room_id, file_id=replaced_file_id,
                common_visible_from=floor,
            )
            name = _safe_name(target.name)
        else:
            name = normalized["name"]
        folder = await ensure_group_generated_folder(
            db, room=room, requester_user_id=requester_id,
        )
        if folder.entity_id != expected_entity_id:
            raise RoomFileMutationError("document_owner_unavailable")
        agent_id = (await db.execute(select(AgentSubscription.agent_id).join(
            ChatRoomAgentBinding,
            ChatRoomAgentBinding.subscription_id == AgentSubscription.id,
        ).where(
            ChatRoomAgentBinding.id == run.binding_id,
            ChatRoomAgentBinding.room_id == room_id,
            ChatRoomAgentBinding.status == "active",
        ))).scalar_one_or_none()
        if agent_id is None:
            raise RoomFileMutationError("agent_unavailable")
        output, mime_type = await _render_file(name, normalized["content"])
        digest = hashlib.sha256(output).hexdigest()
        # Receipt IDs are server-issued; a retry cannot overwrite an older
        # generated file, and the user-facing Document name remains clean.
        rel_path = f"{folder.rel_dir}/{approval_id}-{name}"
        abs_path = await transaction.write_bytes(
            rel_path, output, expected_content_sha256=digest,
            expected_size=len(output), require_missing=True,
        )
        sync = await transaction.project_file(
            abs_path=abs_path, entity_root=get_entity_root(folder.entity_id),
            source="ai_generated", created_by=requester_id, force=True,
            workspace_id=None, task_id=None, agent_id=agent_id,
            conversation_id=None, user_id=requester_id, tool_name=tool_name,
            expected_content_sha256=digest,
        )
        document = (await db.execute(select(Document).where(
            Document.id == sync.document_id,
            Document.entity_id == folder.entity_id,
        ).with_for_update())).scalar_one_or_none()
        if document is None:
            raise RoomFileMutationError("document_projection_failed")
        if document.fs_path != rel_path:
            raise RoomFileMutationError("document_projection_changed")
        document.name = name
        document.mime_type = mime_type
        document.file_size = len(output)
        document.file_url = None
        document.source = "ai_generated"
        document.owner_id = folder.owner_user_id
        document.created_by = requester_id
        document.folder_id = folder.folder_id
        document.visibility = "private"
        document.classification = "internal"
        document.client_visible = False
        document.quarantine_status = "clean"
        document.metadata_ = merge_document_metadata(
            document.metadata_,
            origin={
                "room_id": room.id, "room_agent_run_id": run.id,
                "room_tool_call_id": tool_call_id, "user_id": requester_id,
            },
            artifact={"replaces_file_id": replaced_file_id} if replaced_file_id else None,
        )
        await transaction.commit()
    return RoomFileMutationResult(
        json_result=json.dumps({
            "created": True, "name": name,
            "replaced_previous_version": replaced_file_id is not None,
            "pending_group_share": True,
        }),
        document_id=document.id,
    )


async def store_room_generated_bytes(
    *, scope: "RoomGenerationScope", name: str, content: bytes,
    mime_type: str, entity_id: str,
) -> RoomFileMutationResult:
    """Save an existing generator's bytes using the exact claimed room receipt.

    The renderer/provider remains the ordinary Chat implementation. This is
    only its group-specific output sink; no model-supplied path is honored.
    """
    from packages.core.services.room_generation_scope import RoomGenerationScope

    if not isinstance(scope, RoomGenerationScope):
        raise RoomFileMutationError("missing_group_output_scope")
    filename = _safe_name(name, generation_format=False)
    if not content or not isinstance(content, bytes):
        raise RoomFileMutationError("invalid_generated_content")
    if not isinstance(mime_type, str) or not mime_type.strip() or len(mime_type) > 255:
        raise RoomFileMutationError("invalid_generated_mime")
    async with RuntimeFileProjectionTransactionFactory.create(entity_id) as transaction:
        db = transaction.db
        room, run, approval, requester_id, tool_call_id, _ = await _claimed_scope(
            db, tool_name=scope.tool_name, arguments=scope.arguments,
            room_id=scope.room_id, run_id=scope.run_id,
            approval_id=scope.approval_id,
            expected_execution_revision=scope.execution_revision,
            expected_entity_id=entity_id,
        )
        if requester_id != scope.requester_user_id:
            raise RoomFileMutationError("requester_changed")
        folder = await ensure_group_generated_folder(
            db, room=room, requester_user_id=requester_id,
        )
        if folder.entity_id != entity_id:
            raise RoomFileMutationError("document_owner_unavailable")
        agent_id = (await db.execute(select(AgentSubscription.agent_id).join(
            ChatRoomAgentBinding,
            ChatRoomAgentBinding.subscription_id == AgentSubscription.id,
        ).where(
            ChatRoomAgentBinding.id == run.binding_id,
            ChatRoomAgentBinding.room_id == room.id,
            ChatRoomAgentBinding.status == "active",
        ))).scalar_one_or_none()
        if not agent_id:
            raise RoomFileMutationError("agent_unavailable")
        digest = hashlib.sha256(content).hexdigest()
        rel_path = f"{folder.rel_dir}/{approval.id}-{filename}"
        abs_path = await transaction.write_bytes(
            rel_path, content, expected_content_sha256=digest,
            expected_size=len(content), allow_empty=False, require_missing=True,
        )
        sync = await transaction.project_file(
            abs_path=abs_path, entity_root=get_entity_root(entity_id),
            source="ai_generated", created_by=requester_id, force=True,
            workspace_id=None, task_id=None, agent_id=agent_id,
            conversation_id=None, user_id=requester_id,
            tool_name=scope.tool_name, expected_content_sha256=digest,
        )
        document = (await db.execute(select(Document).where(
            Document.id == sync.document_id,
            Document.entity_id == entity_id,
        ).with_for_update())).scalar_one_or_none()
        if document is None or document.fs_path != rel_path:
            raise RoomFileMutationError("document_projection_changed")
        document.name = filename
        document.mime_type = mime_type.strip().lower()
        document.file_size = len(content)
        document.file_url = None
        document.source = "ai_generated"
        document.owner_id = folder.owner_user_id
        document.created_by = requester_id
        document.folder_id = folder.folder_id
        document.visibility = "private"
        document.classification = "internal"
        document.client_visible = False
        document.quarantine_status = "clean"
        document.metadata_ = merge_document_metadata(
            document.metadata_,
            origin={
                "room_id": room.id, "room_agent_run_id": run.id,
                "room_tool_call_id": tool_call_id, "user_id": requester_id,
            },
        )
        await transaction.commit()
    return RoomFileMutationResult(
        json_result=json.dumps({
            "created": True, "name": filename,
            "pending_group_share": True,
            "document_id": document.id,
        }),
        document_id=document.id,
    )


async def execute_room_file_mutation(
    tool_name: str, arguments: dict[str, Any], *,
    room_id: str, run_id: str, approval_id: str,
    expected_execution_revision: int,
) -> RoomFileMutationResult:
    """Run one previously claimed, exactly authorized group file operation."""
    normalized, _ = validate_room_file_mutation(tool_name, arguments)
    async with database.async_session() as scope_db:
        room = (await scope_db.execute(select(ChatRoom).where(
            ChatRoom.id == room_id,
            ChatRoom.kind == "group",
            ChatRoom.status == "active",
        ))).scalar_one_or_none()
        if room is None:
            raise RoomFileMutationError("room_unavailable")
        if room.owner_entity_id:
            expected_entity_id = room.owner_entity_id
        else:
            expected_entity_id = (await scope_db.execute(select(User.entity_id).where(
                User.id == room.owner_user_id,
                User.status == "active",
                User.deleted_at.is_(None),
            ))).scalar_one_or_none()
        if not expected_entity_id:
            raise RoomFileMutationError("document_owner_unavailable")
    if tool_name in {"room_create_file", "room_replace_file"}:
        return await _create_document(
            tool_name=tool_name, arguments=arguments, normalized=normalized,
            room_id=room_id, run_id=run_id, approval_id=approval_id,
            expected_execution_revision=expected_execution_revision,
            expected_entity_id=expected_entity_id,
        )
    async with database.async_session() as db:
        room, _, _, _, _, floor = await _claimed_scope(
            db, tool_name=tool_name, arguments=arguments,
            room_id=room_id, run_id=run_id, approval_id=approval_id,
            expected_execution_revision=expected_execution_revision,
            expected_entity_id=expected_entity_id,
        )
        attachment, document = await _target_generated_file(
            db, room_id=room.id, file_id=normalized["file_id"],
            common_visible_from=floor,
        )
        if document.entity_id != expected_entity_id:
            raise RoomFileMutationError("room_file_unavailable")
        if tool_name == "room_rename_file":
            new_name = normalized["new_name"]
            old_extension = attachment.name.rsplit(".", 1)[-1].lower()
            new_extension = new_name.rsplit(".", 1)[-1].lower()
            if new_extension != old_extension:
                raise RoomFileMutationError("file_format_change_not_allowed")
            attachment.name = new_name
            result = {"renamed": True, "name": new_name}
        else:
            attachment.deleted_at = datetime.now(timezone.utc)
            other_active = (await db.execute(select(ChatRoomFile.id).where(
                ChatRoomFile.message_id == attachment.message_id,
                ChatRoomFile.id != attachment.id,
                ChatRoomFile.deleted_at.is_(None),
            ).limit(1))).scalar_one_or_none()
            if other_active is None:
                generated_message = (await db.execute(select(ChatRoomMessage).where(
                    ChatRoomMessage.id == attachment.message_id,
                    ChatRoomMessage.room_id == room_id,
                    ChatRoomMessage.sender_agent_binding_id.is_not(None),
                    ChatRoomMessage.content == "Generated a file",
                ).with_for_update())).scalar_one_or_none()
                if generated_message is not None:
                    generated_message.content = "File removed"
            result = {"removed_from_group": True, "name": attachment.name}
        await db.commit()
    return RoomFileMutationResult(json_result=json.dumps(result))
