"""Runtime-owned facade for entity filesystem mutations and Knowledge sync."""

from __future__ import annotations

from typing import Any


def runtime_entity_file_root(entity_id: str) -> str | None:
    """Return the entity filesystem root when Runtime file storage is enabled."""

    import os

    from packages.core.config import get_settings

    settings = get_settings()
    if not settings.MANOR_FS_ENABLED:
        return None
    return os.path.join(settings.MANOR_FS_ROOT, entity_id)


def runtime_user_visible_file_path(path: str) -> bool:
    """Return whether a relative entity path is user-visible Knowledge content."""

    from packages.core.services.knowledge_visibility import is_user_visible_path

    return is_user_visible_path(path)


def runtime_normalize_entity_file_path(path: str) -> str:
    """Normalize an entity-relative file path using Knowledge visibility rules."""

    from packages.core.services.knowledge_visibility import normalize_rel_path

    return normalize_rel_path(path)


def runtime_write_entity_file_atomic(
    entity_id: str,
    rel_path: str,
    data: bytes,
    *,
    expected_size: int | None = None,
    allow_empty: bool = False,
) -> str:
    """Persist bytes through the entity filesystem write guard."""

    from packages.core.services.entity_fs import write_entity_file_atomic

    return write_entity_file_atomic(
        entity_id,
        rel_path,
        data,
        expected_size=expected_size,
        allow_empty=allow_empty,
    )


async def runtime_guard_file_mutation(
    *,
    entity_id: str,
    user_id: str | None = None,
    conversation_id: str | None = None,
    tool_name: str,
    action: str,
    paths: list[str],
    approval_token: str | None = None,
    content_preview: Any = None,
) -> str | None:
    """Run AI file mutation approval policy through the Runtime file boundary."""

    from packages.core.services.ai_file_permissions import guard_ai_file_mutation

    return await guard_ai_file_mutation(
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
        tool_name=tool_name,
        action=action,
        paths=paths,
        approval_token=approval_token,
        content_preview=content_preview,
    )


async def runtime_sync_entity_file_to_knowledge(
    *,
    entity_id: str,
    abs_path: str,
    entity_root: str,
    source: str,
    created_by: str,
    force: bool | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
    tool_name: str | None = None,
) -> Any:
    """Sync an entity filesystem file into Knowledge through the Runtime boundary."""

    from packages.core.services.knowledge_sync import sync_file_to_knowledge

    return await sync_file_to_knowledge(
        entity_id=entity_id,
        abs_path=abs_path,
        entity_root=entity_root,
        source=source,
        created_by=created_by,
        force=force,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name=tool_name,
    )


async def runtime_trash_knowledge_path(entity_id: str, rel_path: str) -> None:
    """Mark a user-visible filesystem path as trashed in Knowledge."""

    from packages.core.services.knowledge_sync import trash_path

    await trash_path(entity_id, rel_path)


async def runtime_get_document_for_entity(
    db: Any,
    *,
    entity_id: str,
    document_id: str,
) -> Any:
    """Load a document record through the Runtime file boundary."""

    from packages.core.services.document_service import get_document

    return await get_document(db, document_id, entity_id)


def runtime_trigger_document_embeddings(document_id: str | None) -> None:
    """Trigger document embeddings best-effort after a Runtime file write."""

    if not document_id:
        return
    try:
        from packages.core.tasks.ai_tasks import process_document_embeddings

        process_document_embeddings.delay(document_id)
    except Exception:
        return
