"""Helpers for setup-generated workspace starter knowledge documents."""
from __future__ import annotations

import re
from typing import Any


STARTER_DOCUMENT_ACTIVE_STATUSES = {"scheduled", "generating", "ready"}


def starter_knowledge_task_key(group_name: str | None) -> str:
    """Return the Strategist task key reserved for a starter doc request."""
    primary = re.split(r"\s+(?:&|and)\s+", str(group_name or "knowledge"), maxsplit=1)[0]
    primary = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", "", primary)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", primary.lower())
    slug = re.sub(r"_+", "_", slug).strip("_") or "knowledge"
    return f"seed_{slug[:64]}_knowledge"


def with_starter_document_settings(
    settings: dict[str, Any] | None,
    *,
    group_name: str,
    status: str = "scheduled",
    document_id: str | None = None,
    outline: list[str] | None = None,
) -> dict[str, Any]:
    """Merge starter-doc metadata into a DocumentGroup settings dict.

    ``outline`` is the section titles the generated document covers, in order.
    """
    out = dict(settings or {})
    starter = dict(out.get("starter_document") or {})
    starter.setdefault("mode", "auto_generate")
    starter.setdefault("task_key", starter_knowledge_task_key(group_name))
    starter["status"] = status
    if document_id:
        starter["document_id"] = document_id
    sections = [str(section).strip() for section in outline or [] if str(section or "").strip()]
    if sections:
        starter["outline"] = sections
    out["starter_document"] = starter
    out["generate_starter_doc"] = True
    return out


def starter_document_state(
    settings: dict[str, Any] | None,
    *,
    group_name: str,
    document_count: int = 0,
) -> dict[str, str] | None:
    """Return normalized starter-doc state for Strategist context."""
    cfg = dict(settings or {})
    starter = dict(cfg.get("starter_document") or {})
    has_starter = bool(starter) or bool(cfg.get("generate_starter_doc"))
    if not has_starter:
        return None
    status = str(starter.get("status") or "").strip().lower()
    if not status:
        status = "ready" if document_count > 0 else "scheduled"
    return {
        "status": status,
        "task_key": str(
            starter.get("task_key") or starter_knowledge_task_key(group_name)
        ),
    }


def stores_markdown_inline() -> bool:
    """Whether generated markdown lives in ``metadata.content_text``, not a file.

    Only a self-hosted deployment that runs without an entity filesystem does
    this, because its readers serve that text. Anywhere a filesystem is expected,
    agents cannot read a document that has no file.
    """
    from packages.core.config import get_settings

    settings = get_settings()
    if settings.MANOR_FS_ENABLED:
        return False
    return True


def require_markdown_storage() -> None:
    """Raise ``EntityFilesystemError`` when generated markdown cannot be stored now."""
    from packages.core.services.entity_fs import assert_entity_filesystem_ready

    if not stores_markdown_inline():
        assert_entity_filesystem_ready()


async def write_markdown_document(
    db,
    *,
    entity_id: str,
    name: str,
    content: str,
    source: str,
    metadata: dict[str, Any],
):
    """Store markdown content and create the Document row that points at it.

    Shared by the starter-document task and finalize link sources so both take
    the same write path. The content is stored before the row exists, so a failed
    write raises (``EntityFilesystemError``, ``OSError``) and leaves no row behind:
    callers retry it or report it, and never get a Document with no content.
    """
    import os
    import time as _time

    from packages.core.config import get_settings
    from packages.core.services.document_service import create_document
    from packages.core.services.entity_fs import write_entity_file_atomic
    from packages.core.services.knowledge_visibility import is_user_visible_path

    body = (content or "").strip()
    if not body:
        raise ValueError("Generated markdown document has no content")
    require_markdown_storage()
    filename = os.path.basename(name if name.endswith(".md") else f"{name}.md")
    if not is_user_visible_path(filename):
        filename = "Knowledge Starter.md"
    workspace_id = str((metadata.get("origin") or {}).get("workspace_id") or "").strip()
    folder_id = None
    relative_directory = ""
    if workspace_id:
        from packages.core.services.workspace_artifacts import (
            ensure_workspace_document_folder, workspace_artifact_storage_base,
        )
        folder_id = await ensure_workspace_document_folder(
            db=db, entity_id=entity_id, workspace_id=workspace_id, rel_path=filename,
        )
        if not folder_id:
            raise ValueError("Workspace starter destination could not be resolved")
        relative_directory = workspace_artifact_storage_base(folder_id)
    content_bytes = body.encode("utf-8")
    fs_path = None
    written_file = None
    if stores_markdown_inline():
        metadata = {**metadata, "content_text": body}
    else:
        entity_root = os.path.join(get_settings().MANOR_FS_ROOT, entity_id)
        os.makedirs(entity_root, exist_ok=True)
        target = os.path.normpath(os.path.join(entity_root, relative_directory, filename))
        entity_root_norm = os.path.normpath(entity_root)
        if os.path.commonpath([entity_root_norm, target]) != entity_root_norm:
            raise ValueError("Generated knowledge filename escaped entity root")
        if os.path.exists(target):
            base, ext = os.path.splitext(filename)
            target = os.path.join(entity_root, relative_directory, f"{base}_{int(_time.time())}{ext}")
        written_file = write_entity_file_atomic(
            entity_id,
            os.path.relpath(target, entity_root),
            content_bytes,
            expected_size=len(content_bytes),
            allow_empty=False,
        )
        fs_path = os.path.relpath(written_file, entity_root)
    try:
        doc = await create_document(
            db,
            entity_id,
            name=filename,
            fs_path=fs_path,
            file_size=len(content_bytes),
            folder_id=folder_id,
            file_type="md",
            mime_type="text/markdown",
            source=source,
            visibility="entity",
            metadata=metadata,
        )
    except BaseException:
        if written_file:
            try:
                os.unlink(written_file)
            except OSError:
                pass
        raise
    doc.vector_status = "pending"
    await db.flush()
    return doc
