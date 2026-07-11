from __future__ import annotations

import json
import os
from typing import Any


async def runtime_generate_document_file(
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    name: str,
    content: str,
    file_type: str,
    approval_token: str | None = None,
    expected_sha256: str | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
) -> str:
    """Generate a user-visible document file through the Runtime boundary."""

    from packages.core.ai.runtime.document_actions import runtime_document_to_dict
    from packages.core.ai.runtime.file_actions import (
        runtime_entity_file_root,
        runtime_get_document_for_entity,
        runtime_guard_file_mutation,
        runtime_normalize_entity_file_path,
        runtime_sync_entity_file_to_knowledge,
        runtime_user_visible_file_path,
        runtime_write_entity_file_atomic,
    )
    from packages.core.services.generated_media_naming import (
        resolve_workspace_artifact_base_dir,
        scope_workspace_artifact_path,
    )

    clean_name = runtime_normalize_entity_file_path(name or "")
    if not clean_name or not content:
        return json.dumps({"error": "name and content are required"})

    workspace_base_dir = await resolve_workspace_artifact_base_dir(
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    clean_name = runtime_normalize_entity_file_path(
        scope_workspace_artifact_path(
            clean_name,
            workspace_base_dir,
            default_subdir="documents",
        )
    )

    clean_file_type = str(file_type or "txt").lstrip(".").lower()
    if "." not in clean_name:
        clean_name = f"{clean_name}.{clean_file_type}"
    if not runtime_user_visible_file_path(clean_name):
        return json.dumps({"error": "Cannot upload hidden/system document path"})

    ext = os.path.splitext(clean_name)[1].lower()
    entity_dir = runtime_entity_file_root(entity_id)
    target = None
    if entity_dir:
        target = os.path.normpath(os.path.join(entity_dir, clean_name))
        if os.path.commonpath([os.path.normpath(entity_dir), target]) != os.path.normpath(entity_dir):
            return json.dumps({"error": "Path traversal detected"})
    if not target or not entity_dir:
        return json.dumps({"error": "Entity filesystem is not enabled"})

    clean_expected_sha256 = str(expected_sha256 or "").strip()
    if clean_expected_sha256:
        stale = await runtime_guard_generated_file_expected_source(
            abs_path=target,
            path=clean_name,
            expected_sha256=clean_expected_sha256,
        )
        if stale:
            return stale

    content_text = str(content)
    blocked = await runtime_guard_file_mutation(
        entity_id=entity_id,
        user_id=user_id or None,
        conversation_id=conversation_id or None,
        tool_name="generate_document_file",
        action="create_document",
        paths=[clean_name],
        approval_token=approval_token,
        content_preview=content_text,
    )
    if blocked:
        return blocked

    binary_content: bytes | None = None
    if ext == ".pptx":
        try:
            from packages.core.services.docgen_service import generate_pptx

            title = os.path.splitext(clean_name)[0]
            for line in content_text.split("\n"):
                if line.startswith("# ") and not line.startswith("## "):
                    title = line[2:].strip()
                    break
            binary_content = await generate_pptx(title, content_text)
        except Exception:
            pass
    elif ext == ".docx":
        try:
            from packages.core.services.docgen_service import generate_docx

            binary_content = await generate_docx(os.path.splitext(clean_name)[0], content_text)
        except Exception:
            pass
    elif ext == ".pdf":
        try:
            from packages.core.services.docgen_service import generate_pdf

            binary_content = await generate_pdf(os.path.splitext(os.path.basename(clean_name))[0], content_text)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"PDF generation failed: {exc}"})

    persisted_bytes = binary_content if binary_content is not None else content_text.encode("utf-8")
    file_size = len(persisted_bytes)
    mime_by_ext = {
        "txt": "text/plain",
        "md": "text/markdown",
        "csv": "text/csv",
        "json": "application/json",
        "html": "text/html",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pdf": "application/pdf",
    }
    mime_type = mime_by_ext.get(ext.lstrip("."), "text/plain")

    try:
        target = runtime_write_entity_file_atomic(
            entity_id,
            clean_name,
            persisted_bytes,
            expected_size=file_size,
            allow_empty=False,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Entity filesystem is not available: {exc}"})

    sync = await runtime_sync_entity_file_to_knowledge(
        entity_id=entity_id,
        abs_path=target,
        entity_root=entity_dir,
        source="ai_generated",
        created_by=user_id or "ai-agent",
        force=True,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id or None,
        user_id=user_id or None,
        tool_name="generate_document_file",
    )
    if not sync.synced:
        if sync.reason == "storage_limit":
            return json.dumps({"error": (
                "Knowledge base storage limit reached for this plan — the file "
                "could not be added. Ask the user to free up space or upgrade "
                "their plan, then try again."
            )})
        return json.dumps({"error": f"Document was not synced to knowledge: {sync.reason}"})

    from packages.core.database import async_session

    written_meta = await runtime_generated_file_metadata(target)
    async with async_session() as db:
        doc = (
            await runtime_get_document_for_entity(
                db,
                entity_id=entity_id,
                document_id=sync.document_id,
            )
            if sync.document_id
            else None
        )
        if doc:
            data = runtime_document_to_dict(doc, detail="details")
            data.update({
                "source_sha256": written_meta["source_sha256"],
                "mtime_ns": written_meta["mtime_ns"],
            })
            return json.dumps({"created": True, "document": data})

    return json.dumps({
        "created": True,
        "document": {
            "id": sync.document_id,
            "name": os.path.basename(clean_name),
            "fs_path": clean_name,
            "file_size": file_size,
            "file_type": clean_file_type,
            "mime_type": mime_type,
            "source_sha256": written_meta["source_sha256"],
            "mtime_ns": written_meta["mtime_ns"],
        },
    })


async def runtime_generated_file_metadata(abs_path: str) -> dict[str, Any]:
    """Return stable metadata for a generated user-visible file."""

    from packages.core.ai.tools.file_tools import _file_meta, _read_supported_text

    return _file_meta(abs_path, await _read_supported_text(abs_path))


async def runtime_guard_generated_file_expected_source(
    *,
    abs_path: str,
    path: str,
    expected_sha256: str,
) -> str | None:
    """Reject generated-file writes when the target source changed."""

    from packages.core.ai.tools.file_tools import _guard_expected_source_sha

    return await _guard_expected_source_sha(
        abs_path=abs_path,
        path=path,
        expected_sha256=expected_sha256,
    )
