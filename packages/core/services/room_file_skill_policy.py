"""Fail-closed argument policy for group use of ordinary file tools."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.skill import Skill


ROOM_FILE_SKILL_SLUGS = frozenset({"docx", "pdf", "pptx", "xlsx"})
ROOM_GENERATE_KINDS = frozenset({
    "code", "diagram", "document", "word_document", "pdf",
    "presentation", "spreadsheet", "image", "audio", "video",
})
ROOM_GENERATE_PARAM_KEYS = frozenset({
    "duration", "duration_seconds", "resolution", "aspect_ratio", "size",
    "quality", "purpose", "voice", "language", "response_format",
    "files", "entry",
})


def room_generate_file_schema() -> dict[str, Any]:
    """A path-free group view of the existing generate_file tool."""
    return {
        "type": "function",
        "function": {
            "name": "generate_file",
            "description": (
                "Generate a file for this group. The server chooses the private "
                "group folder; personal and Workspace sources are unavailable. "
                "For Markdown, plain text, or JSON, use kind=document with "
                "a filename and complete content. Kind=code requires a "
                "params.files code bundle. "
                "For PDF, DOCX, PPTX or XLSX, the existing file skill runs "
                "in an isolated sandbox even if you provide draft content. "
                "Read relevant group files before generating; save_result must produce "
                "a real group file before claiming completion."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": sorted(ROOM_GENERATE_KINDS)},
                    "prompt": {"type": "string"},
                    "name": {"type": "string", "description": "Plain filename only."},
                    "content": {"type": "string"},
                    "file_type": {"type": "string"},
                    "operations": {"type": "array", "items": {"type": "object"}},
                    "params": {
                        "type": "object",
                        "properties": {
                            "duration": {"type": "integer"},
                            "duration_seconds": {"type": "number"},
                            "resolution": {"type": "string"},
                            "aspect_ratio": {"type": "string"},
                            "size": {"type": "string"},
                            "quality": {"type": "string"},
                            "purpose": {"type": "string"},
                            "voice": {"type": "string"},
                            "language": {"type": "string"},
                            "response_format": {"type": "string"},
                            "files": {"type": "array", "items": {"type": "object"}},
                            "entry": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                },
                "required": ["kind"],
                "additionalProperties": False,
            },
        },
    }


class RoomFileToolPolicyError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_room_file_tool_arguments(tool_name: str, arguments: Any) -> tuple[dict, str]:
    """Reject non-file skill refs and model-controlled storage/source scope."""
    if not isinstance(arguments, dict):
        raise RoomFileToolPolicyError("invalid_arguments")
    if tool_name == "invoke_skill":
        if set(arguments) - {"skill_id", "input"}:
            raise RoomFileToolPolicyError("invalid_group_skill_arguments")
        slug = arguments.get("skill_id")
        prompt = arguments.get("input")
        if slug not in ROOM_FILE_SKILL_SLUGS:
            raise RoomFileToolPolicyError("group_skill_not_file_skill")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12_000:
            raise RoomFileToolPolicyError("invalid_group_skill_input")
        normalized = {"skill_id": slug, "input": prompt.strip()}
        return normalized, f"Create {slug.upper()} file for this group"
    if tool_name != "generate_file":
        raise RoomFileToolPolicyError("unsupported_group_file_tool")
    if set(arguments) - {"kind", "prompt", "name", "content", "file_type", "operations", "params"}:
        raise RoomFileToolPolicyError("invalid_group_generation_arguments")
    kind = arguments.get("kind")
    if kind not in ROOM_GENERATE_KINDS:
        raise RoomFileToolPolicyError("unsupported_group_generation_kind")
    office_aliases = {"pdf": "pdf", "docx": "word_document", "pptx": "presentation", "xlsx": "spreadsheet"}
    declared_type = str(arguments.get("file_type") or "").lower().lstrip(".")
    filename = str(arguments.get("name") or "").strip()
    filename_type = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if kind == "document" and (declared_type in office_aliases or filename_type in office_aliases):
        if declared_type in office_aliases and filename_type in office_aliases and declared_type != filename_type:
            raise RoomFileToolPolicyError("group_file_type_mismatch")
        kind = office_aliases[declared_type if declared_type in office_aliases else filename_type]
    normalized: dict[str, Any] = {"kind": kind}
    for key in ("prompt", "name", "content", "file_type"):
        value = arguments.get(key)
        if value is not None:
            if not isinstance(value, str) or len(value) > (48_000 if key == "content" else 12_000):
                raise RoomFileToolPolicyError("invalid_group_generation_arguments")
            if key == "name" and (
                not value.strip() or value != value.strip() or len(value) > 128
                or "/" in value or "\\" in value or value in {".", ".."}
            ):
                raise RoomFileToolPolicyError("invalid_group_filename")
            normalized[key] = value
    if "operations" in arguments:
        operations = arguments["operations"]
        if not isinstance(operations, list) or not operations:
            raise RoomFileToolPolicyError("invalid_group_office_operations")
        normalized["operations"] = operations
    if "params" in arguments:
        params = arguments["params"]
        if not isinstance(params, dict):
            raise RoomFileToolPolicyError("invalid_group_generation_params")
        # No personal/Workspace references or caller-selected persistence.
        forbidden = {
            "source", "template", "storage_scope", "workspace_id", "task_id",
            "conversation_id", "user_id", "entity_id", "approval_token",
            "sandbox_path", "workspace_asset_key", "reference_url",
            "reference_urls", "reference_video_urls", "audio_reference_url",
            "audio_reference_urls", "image_url", "input_image_url",
            "input_image_urls", "first_frame_url", "last_frame_url",
            "audio_url", "source_image_url", "source_image_path",
        }
        if forbidden.intersection(params) or set(params) - ROOM_GENERATE_PARAM_KEYS:
            raise RoomFileToolPolicyError("group_generation_reference_not_allowed")
        normalized["params"] = params
    try:
        encoded = json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RoomFileToolPolicyError("invalid_group_generation_arguments") from exc
    if len(encoded.encode("utf-8")) > 63_000:
        raise RoomFileToolPolicyError("group_generation_arguments_too_large")
    display_name = str(normalized.get("name") or kind).strip()
    return normalized, f"Generate group file: {display_name}"


async def require_builtin_room_file_skill(db: AsyncSession, slug: str) -> Skill:
    """Never resolve an entity skill or an opaque ID to a file-skill grant."""
    if slug not in ROOM_FILE_SKILL_SLUGS:
        raise RoomFileToolPolicyError("group_skill_not_file_skill")
    skill = (await db.execute(select(Skill).where(
        Skill.slug == slug,
        Skill.entity_id.is_(None),
        Skill.status == "active",
    ))).scalar_one_or_none()
    if (
        skill is None or (skill.config or {}).get("source") != "builtin"
        or (skill.config or {}).get("type") != "sandbox"
    ):
        raise RoomFileToolPolicyError("group_file_skill_unavailable")
    return skill
