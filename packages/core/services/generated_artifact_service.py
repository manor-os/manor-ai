"""Shared persistence boundary for generated file artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any


async def generated_artifact_document_folder_id(
    *,
    entity_id: str,
    user_id: str,
    workspace_id: str | None,
    rel_path: str,
    conversation_id: str | None = None,
    task_id: str | None = None,
    db: Any | None = None,
) -> str | None:
    if workspace_id:
        from packages.core.services.workspace_artifacts import (
            ensure_workspace_document_folder,
        )

        return await ensure_workspace_document_folder(
            entity_id=entity_id,
            workspace_id=workspace_id,
            rel_path=rel_path,
        )
    from packages.core.services.conversation_generated_files import (
        ensure_conversation_generated_folder, is_default_chat_output_path,
    )
    from packages.core.services.knowledge_sync import ensure_folder_path

    if conversation_id and user_id and not task_id and is_default_chat_output_path(rel_path):
        if db is None:
            from packages.core.database import async_session

            async with async_session() as owned_db:
                folder_id = await ensure_conversation_generated_folder(
                    owned_db, entity_id=entity_id, user_id=user_id,
                    conversation_id=conversation_id,
                )
                await owned_db.commit()
        else:
            folder_id = await ensure_conversation_generated_folder(
                db, entity_id=entity_id, user_id=user_id,
                conversation_id=conversation_id,
            )
        if folder_id:
            return folder_id

    rel_dir = str(Path(rel_path).parent).replace("\\", "/")
    rel_dir = "" if rel_dir == "." else rel_dir
    return await ensure_folder_path(entity_id, rel_dir, owner_id=user_id or None)


async def register_generated_file_artifact(
    *,
    entity_id: str,
    user_id: str,
    filename: str,
    rel_path: str,
    file_size: int,
    file_type: str,
    mime_type: str,
    workspace_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    conversation_id: str | None,
    tool_name: str,
    artifact_role: str,
    generation: dict[str, Any],
) -> str | None:
    from packages.core.database import async_session
    from packages.core.models.document import Document
    from packages.core.services.document_metadata import merge_document_metadata
    from packages.core.services.document_service import upsert_document_by_fs_path
    from packages.core.services.workspace_artifacts import infer_workspace_id_from_storage_path
    from sqlalchemy import select

    target_workspace_id = await infer_workspace_id_from_storage_path(
        entity_id=entity_id, rel_path=rel_path,
    )
    if workspace_id and target_workspace_id and workspace_id != target_workspace_id:
        raise ValueError("Generated artifact belongs to another Workspace")

    visibility = "workspace" if target_workspace_id else "private"
    async with async_session() as db:
        existing_document_id = (await db.execute(select(Document.id).where(
            Document.entity_id == entity_id,
            Document.fs_path == rel_path,
        ).limit(1))).scalar_one_or_none()
        folder_id = None
        if existing_document_id is None:
            folder_id = await generated_artifact_document_folder_id(
                entity_id=entity_id,
                user_id=user_id,
                workspace_id=target_workspace_id,
                rel_path=rel_path,
                conversation_id=conversation_id,
                task_id=task_id,
                db=db,
            )
        doc = await upsert_document_by_fs_path(
            db,
            entity_id,
            name=filename,
            fs_path=rel_path,
            file_size=file_size,
            file_type=file_type,
            mime_type=mime_type,
            source="ai_generated",
            created_by=user_id or None,
            owner_id=user_id or None,
            visibility=visibility,
            folder_id=folder_id,
        )
        doc.source = "ai_generated"
        if user_id:
            doc.created_by = user_id
        doc.metadata_ = merge_document_metadata(
            doc.metadata_,
            artifact={"role": artifact_role, "storage_scope": "artifact"},
            origin={
                "workspace_id": workspace_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "tool_name": tool_name,
            },
            generation=generation,
        )
        document_id = doc.id
        await db.commit()
    return document_id


async def bind_generated_artifact_to_workspace(
    *,
    entity_id: str,
    document_id: str | None,
    workspace_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    conversation_id: str | None,
    user_id: str,
    tool_name: str,
) -> None:
    if not workspace_id or not document_id:
        return
    from packages.core.services.knowledge_sync import bind_document_to_workspace

    await bind_document_to_workspace(
        entity_id=entity_id,
        document_id=document_id,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        user_id=user_id,
        tool_name=tool_name,
    )


__all__ = [
    "bind_generated_artifact_to_workspace",
    "generated_artifact_document_folder_id",
    "register_generated_file_artifact",
]
