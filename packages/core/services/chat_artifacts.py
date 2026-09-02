from __future__ import annotations

import json
import mimetypes
import os
import re
from typing import Any

from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory
from packages.core.services.generated_file_refs import (
    canonical_generated_file_ref,
    dedupe_generated_file_refs,
)


_COLLECTION_KEYS = (
    "files",
    "artifacts",
    "attachments",
    "documents",
    "outputs",
    "images",
    "videos",
    "audios",
)
_URL_KEYS = (
    "result_url",
    "file_url",
    "download_url",
    "document_url",
    "image_url",
    "video_url",
    "audio_url",
    "media_url",
    "output_url",
    "url",
)
_PATH_KEYS = ("fs_path", "path", "file_path", "output_path", "saved_to", "local_path")
_CREATION_FLAGS = (
    "created",
    "generated",
    "saved",
    "written",
    "exported",
    "uploaded",
    "downloaded",
)
_STATUS_KEYS = ("status", "state")
_NON_TERMINAL_STATUSES = {
    "pending",
    "queued",
    "running",
    "processing",
    "started",
    "in_progress",
}
_FAILED_STATUSES = {
    "error",
    "failed",
    "failure",
    "timeout",
    "cancelled",
    "canceled",
}
_GENERATING_TOOL_NAMES = {
    "generate_file",
    "generate_image",
    "generate_video",
    "merge_videos",
    "normalize_audio_loudness",
    "align_subtitles",
    "compose_video_timeline",
}
_EXPLICIT_ARTIFACT_TOOL_NAMES = {
    "sandbox_save_result",
    "save_sandbox_file",
}
_DISPLAY_FLAG_KEYS = (
    "display_as_artifact",
    "show_as_artifact",
    "show_in_chat",
    "chat_artifact",
)


def _parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _bool_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _artifact_role(obj: dict[str, Any]) -> str:
    artifact = obj.get("artifact")
    artifact_obj = artifact if isinstance(artifact, dict) else {}
    return _text(
        obj.get("artifact_role")
        or artifact_obj.get("role")
        or obj.get("role")
    ).lower()


def _requests_chat_artifact(obj: dict[str, Any]) -> bool:
    artifact = obj.get("artifact")
    artifact_obj = artifact if isinstance(artifact, dict) else {}
    for key in _DISPLAY_FLAG_KEYS:
        if _bool_flag(obj.get(key)) or _bool_flag(artifact_obj.get(key)):
            return True
    return _artifact_role(obj) == "final"


def _first_text(obj: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _text(obj.get(key))
        if value:
            return value
    return ""


def _entity_url_prefix_from_value(value: Any) -> str:
    match = re.match(r"^(/api/v1/fs/[^/]+/)", _text(value))
    return match.group(1) if match else ""


def _extension_from_name(name: str) -> str:
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    return ext[:20]


def _file_type(name: str, mime_type: str, explicit: Any = None) -> str:
    explicit_text = _text(explicit).lower().lstrip(".")
    if explicit_text and "/" not in explicit_text:
        return explicit_text[:20]
    ext = _extension_from_name(name)
    if ext:
        return ext
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type.split(";", 1)[0].strip())
        if guessed:
            return guessed.lstrip(".").lower()
    return ""


def _mime_type(name: str, explicit: Any = None) -> str:
    explicit_text = _text(explicit)
    if explicit_text:
        return explicit_text
    guessed, _encoding = mimetypes.guess_type(name)
    return guessed or ""


def _basename_from_reference(value: str) -> str:
    clean = value.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return os.path.basename(clean)


def _name_for_artifact(obj: dict[str, Any], reference: str, kind: str) -> str:
    for key in ("name", "filename", "file_name", "title"):
        value = _text(obj.get(key))
        if value:
            return value
    if reference:
        name = _basename_from_reference(reference)
        if name:
            return name
    doc = obj.get("document")
    if isinstance(doc, dict):
        value = _text(doc.get("name"))
        if value:
            return value
    if kind:
        return f"{kind}.file"
    return ""


def _looks_like_input_upload(obj: dict[str, Any], reference: str) -> bool:
    haystack = " ".join(
        _text(value)
        for value in (
            reference,
            obj.get("fs_path"),
            obj.get("path"),
            obj.get("url"),
            obj.get("result_url"),
        )
    ).replace("\\", "/").lower()
    return "/uploads/chat/" in haystack or haystack.startswith("uploads/chat/")


def _is_terminal_success(obj: dict[str, Any]) -> bool:
    for key in _STATUS_KEYS:
        status = _text(obj.get(key)).lower()
        if not status:
            continue
        if status in _NON_TERMINAL_STATUSES or status in _FAILED_STATUSES:
            return False
    return True


def _has_creation_signal(obj: dict[str, Any], *, tool_name: str, inherited: bool) -> bool:
    if inherited:
        return True
    if tool_name.lower() in _GENERATING_TOOL_NAMES:
        return True
    if any(bool(obj.get(key)) for key in _CREATION_FLAGS):
        return True
    kind = _text(obj.get("kind")).lower()
    return kind in {
        "image",
        "video",
        "audio",
        "pdf",
        "document",
        "word_document",
        "presentation",
        "spreadsheet",
        "code",
        "subtitle",
    }


def _canonical_url(url: str, fs_path: str, entity_prefix: str) -> str:
    if url:
        return url
    if fs_path and entity_prefix and not fs_path.startswith(("http://", "https://", "/api/")):
        return entity_prefix + fs_path.lstrip("/")
    return ""


def chat_attachments_from_tool_results(tool_results: list[dict] | None) -> list[dict]:
    """Extract generated files with an exact address from chat tool results."""

    if not tool_results:
        return []

    entity_prefix = ""
    for item in tool_results:
        if not isinstance(item, dict):
            continue
        payload = _parse_json(item.get("raw_result", item.get("result")))
        stack = [payload]
        while stack:
            current = stack.pop()
            if isinstance(current, str):
                entity_prefix = entity_prefix or _entity_url_prefix_from_value(current)
                continue
            if not isinstance(current, dict):
                continue
            for key in (*_URL_KEYS, *_PATH_KEYS):
                entity_prefix = entity_prefix or _entity_url_prefix_from_value(current.get(key))
            for key in _COLLECTION_KEYS:
                value = current.get(key)
                if isinstance(value, list):
                    stack.extend(value)
                elif isinstance(value, dict):
                    stack.append(value)
            document = current.get("document")
            if isinstance(document, dict):
                stack.append(document)

    canonical_refs: list[dict[str, Any]] = []
    entity_id = entity_prefix.removeprefix("/api/v1/fs/").strip("/")

    def add_from_obj(
        obj: dict[str, Any],
        *,
        tool_name: str,
        inherited_created: bool,
        inherited_display: bool = False,
        inherited_terminal: bool = True,
    ) -> None:
        created = _has_creation_signal(obj, tool_name=tool_name, inherited=inherited_created)
        terminal = inherited_terminal and _is_terminal_success(obj)
        display_requested = inherited_display or _requests_chat_artifact(obj)
        doc = obj.get("document")
        if isinstance(doc, dict):
            child = {
                **doc,
                "result_url": obj.get("result_url") or obj.get("url") or doc.get("result_url") or doc.get("url"),
                "fs_path": _first_text(doc, _PATH_KEYS) or _first_text(obj, _PATH_KEYS),
            }
            add_from_obj(
                child,
                tool_name=tool_name,
                inherited_created=created,
                inherited_display=display_requested,
                inherited_terminal=terminal,
            )

        for key in _COLLECTION_KEYS:
            value = obj.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        add_from_obj(
                            item,
                            tool_name=tool_name,
                            inherited_created=created,
                            inherited_display=display_requested,
                            inherited_terminal=terminal,
                        )
            elif isinstance(value, dict):
                add_from_obj(
                    value,
                    tool_name=tool_name,
                    inherited_created=created,
                    inherited_display=display_requested,
                    inherited_terminal=terminal,
                )

        if not created or not terminal:
            return
        if tool_name.lower() in _EXPLICIT_ARTIFACT_TOOL_NAMES and not display_requested:
            return

        document_id = _text(obj.get("document_id"))
        url = _first_text(obj, _URL_KEYS)
        fs_path = _first_text(obj, _PATH_KEYS)
        reference = url or fs_path
        if not document_id and not reference:
            return
        if _looks_like_input_upload(obj, reference):
            return

        kind = _text(obj.get("kind")).lower()
        name = _name_for_artifact(obj, reference, kind)
        if not name:
            return
        mime_type = _mime_type(name, obj.get("mime_type") or obj.get("mimeType"))
        file_type = _file_type(name, mime_type, obj.get("file_type") or obj.get("fileType"))
        preview_url = _canonical_url(url, fs_path, entity_prefix)
        canonical = canonical_generated_file_ref({
            "name": name,
            "document_id": document_id,
            "fs_path": fs_path,
            "url": url,
            "file_type": file_type,
            "mime_type": mime_type,
            "preview_url": preview_url,
        }, entity_id=entity_id)
        if not canonical.get("open_url"):
            return
        canonical_refs.append(canonical)

    for item in tool_results:
        if not isinstance(item, dict):
            continue
        payload = _parse_json(item.get("raw_result", item.get("result")))
        if isinstance(payload, dict):
            arguments = item.get("arguments")
            canonical_call = RuntimeCompositeToolCallFactory.create(
                _text(item.get("name")),
                arguments if isinstance(arguments, dict) else {},
            )
            add_from_obj(
                payload,
                tool_name=canonical_call.tool_name,
                inherited_created=False,
            )

    attachments: list[dict[str, Any]] = []
    for ref in dedupe_generated_file_refs(canonical_refs, entity_id=entity_id)[:12]:
        document_id = _text(ref.get("document_id"))
        fs_path = _text(ref.get("fs_path"))
        preview_url = _text(ref.get("preview_url"))
        attachment: dict[str, Any] = {
            "name": _text(ref.get("name")),
            "type": "knowledge" if document_id or fs_path else "file",
            "open_url": _text(ref.get("open_url")),
        }
        if ref.get("markdown_link"):
            attachment["markdown_link"] = _text(ref.get("markdown_link"))
        if document_id:
            attachment["document_id"] = document_id
        if fs_path:
            attachment["fs_path"] = fs_path
        if ref.get("file_type"):
            attachment["fileType"] = _text(ref.get("file_type"))
        if ref.get("mime_type"):
            attachment["mimeType"] = _text(ref.get("mime_type"))
        if preview_url:
            attachment["previewUrl"] = preview_url
        attachments.append(attachment)
    return attachments
