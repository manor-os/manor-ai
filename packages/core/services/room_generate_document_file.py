"""Group output adapter for the ordinary generate_file document renderers."""
from __future__ import annotations

import asyncio
import json
from pathlib import PurePosixPath

from packages.core.contracts.file_engine import (
    TEXT_CONTENT_TYPES, file_patch_operations, normalize_file_patch_operation,
)
from packages.core.services.room_file_mutation_tools import (
    RoomFileMutationError, store_room_generated_bytes,
)
from packages.core.services.room_generation_scope import RoomGenerationScope


_MIME = {
    "txt": "text/plain", "md": "text/markdown", "csv": "text/csv",
    "json": "application/json", "html": "text/html",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "diagram.json": "application/json",
}
_MIME.update({kind: "text/plain" for kind in TEXT_CONTENT_TYPES if kind not in _MIME})


async def room_generate_document_file(
    *, scope: RoomGenerationScope, entity_id: str, name: str,
    content: str | None, file_type: str, operations: list[dict] | None,
    template: dict | None, source: dict | None,
) -> str:
    """Render with existing file engines, then commit only to this room.

    Existing personal/Workspace paths cannot be used as a source or template.
    A later explicit room-file source adapter can accept only pinned room file
    IDs; silently interpreting an entity path here would cross the boundary.
    """
    if source is not None or template is not None:
        return json.dumps({"error": "group_file_sources_require_room_file_id"})
    filename = PurePosixPath(str(name or "")).name
    if filename != name or not filename or "/" in name or "\\" in name:
        return json.dumps({"error": "invalid_group_filename"})
    extension = (
        "diagram.json" if filename.lower().endswith(".diagram.json")
        else filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    )
    if extension != str(file_type or "").strip().lower().lstrip("."):
        return json.dumps({"error": "file_type_mismatch"})
    if extension not in _MIME:
        return json.dumps({"error": "unsupported_group_document_type"})
    if operations is not None:
        if content is not None or extension not in {"docx", "pptx", "xlsx"}:
            return json.dumps({"error": "invalid_group_office_operations"})
        try:
            patches = [normalize_file_patch_operation(item) for item in operations]
            if not patches or any(
                patch["operation"] not in file_patch_operations(extension)
                for patch in patches
            ):
                return json.dumps({"error": "unsupported_group_office_operation"})
            # Resources may point to private Entity files, so do not allow
            # them until a room-file-ID-backed resource reader exists.
            if any(
                isinstance(patch.get("source"), dict)
                or '"resources"' in json.dumps(patch)
                or '"source"' in json.dumps(patch)
                for patch in patches
            ):
                return json.dumps({"error": "group_office_resources_require_room_file_id"})
            from packages.core.ai.runtime.generated_files import _generate_office_operations_sync

            rendered = await asyncio.to_thread(
                _generate_office_operations_sync, extension, patches,
            )
            if rendered.get("error"):
                return json.dumps(rendered)
            output = rendered["_persisted_bytes"]
        except (TypeError, ValueError, KeyError) as exc:
            return json.dumps({"error": "invalid_group_office_operations", "detail": str(exc)[:200]})
    else:
        if not isinstance(content, str) or not content:
            return json.dumps({"error": "group_document_content_required"})
        if extension in {"json", "diagram.json"}:
            try:
                json.loads(content)
            except ValueError:
                return json.dumps({"error": "invalid_json"})
        if extension == "docx":
            from packages.core.services.docgen_service import generate_docx

            output = await generate_docx(filename.rsplit(".", 1)[0], content)
        elif extension == "pptx":
            from packages.core.services.docgen_service import generate_pptx

            output = await generate_pptx(filename.rsplit(".", 1)[0], content)
        elif extension == "pdf":
            from packages.core.services.docgen_service import generate_pdf

            output = await generate_pdf(filename.rsplit(".", 1)[0], content)
        elif extension in TEXT_CONTENT_TYPES:
            output = content.encode("utf-8")
        else:
            return json.dumps({"error": "unsupported_group_document_type"})
    try:
        result = await store_room_generated_bytes(
            scope=scope, name=filename, content=output,
            mime_type=_MIME[extension], entity_id=entity_id,
        )
    except RoomFileMutationError as exc:
        return json.dumps({"error": exc.code})
    return result.json_result
