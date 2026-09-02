"""Shared authorization for user-visible entity filesystem mutations."""
from __future__ import annotations

import os

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Document, DocumentFolder
from packages.core.models.permission import Capability
from packages.core.models.user import User
from packages.core.permissions import (
    Permission,
    effective_user_has_permission,
    user_is_effective_entity_admin,
)
from packages.core.services.document_access import (
    document_is_owned_by_deleted_workspace,
    document_workspace_ids_batched,
    folder_is_owned_by_deleted_workspace,
    user_can_edit_document,
    user_has_document_capability,
    user_has_folder_capability,
)
from packages.core.services.knowledge_visibility import normalize_rel_path


class FilesystemAccessDenied(PermissionError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def _require_workspace_artifact_scope(
    db: AsyncSession,
    *,
    user: User,
    rel_path: str,
    workspace_id: str | None,
) -> bool:
    """Keep a Workspace runtime inside its active physical artifact root."""
    if not workspace_id:
        return False
    from packages.core.services.workspace_access import (
        lock_workspace_access_boundary,
        user_can_write_workspace_artifacts,
    )
    from packages.core.services.workspace_artifacts import workspace_artifact_storage_base

    workspace = await lock_workspace_access_boundary(
        db,
        workspace_id=workspace_id,
        entity_id=user.entity_id,
    )
    if (
        workspace is None
        or workspace.status != "active"
        or workspace.deleted_at is not None
        or not workspace.artifact_folder_id
    ):
        raise FilesystemAccessDenied(
            403,
            "The active Workspace artifact scope is unavailable",
        )
    workspace_artifact_folder_id = workspace.artifact_folder_id
    normalized_path = normalize_rel_path(rel_path)
    workspace_base = workspace_artifact_storage_base(workspace_artifact_folder_id)
    workspace_storage_root = workspace_base.rsplit("/", 1)[0]
    inside_workspace_artifacts = (
        normalized_path == workspace_base
        or normalized_path.startswith(f"{workspace_base}/")
    )
    inside_physical_workspace_storage = (
        normalized_path == workspace_storage_root
        or normalized_path.startswith(f"{workspace_storage_root}/")
    )
    if inside_physical_workspace_storage and not inside_workspace_artifacts:
        raise FilesystemAccessDenied(
            403,
            "The path is outside the active Workspace artifact scope",
        )
    if not await user_can_write_workspace_artifacts(
        db,
        workspace_id=workspace_id,
        user_id=user.id,
        entity_role=user.role,
    ):
        raise FilesystemAccessDenied(
            403,
            "Workspace artifact write access is required",
        )
    return inside_workspace_artifacts


async def _require_active_workspace_path_owner(
    db: AsyncSession,
    *,
    entity_id: str,
    rel_path: str,
    documents: list[Document] | None = None,
    folder: DocumentFolder | None = None,
) -> None:
    """Serialize path mutation with its Workspace lifecycle owner."""
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_artifacts import (
        artifact_folder_id_from_entity_storage_path,
    )

    artifact_folder_id = artifact_folder_id_from_entity_storage_path(rel_path)
    if artifact_folder_id:
        workspace = (await db.execute(
            select(Workspace)
            .where(
                Workspace.entity_id == entity_id,
                Workspace.artifact_folder_id == artifact_folder_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if workspace is not None and workspace.deleted_at is not None:
            raise FilesystemAccessDenied(
                403,
                "The Workspace that owns this path is deleted",
            )

    if folder is not None and await folder_is_owned_by_deleted_workspace(
        db,
        folder,
        for_update=True,
    ):
        raise FilesystemAccessDenied(
            403,
            "The Workspace that owns this folder is deleted",
        )

    for document in sorted(documents or [], key=lambda item: str(item.id)):
        if await document_is_owned_by_deleted_workspace(
            db,
            document,
            for_update=True,
        ):
            raise FilesystemAccessDenied(
                403,
                "The Workspace that owns this document is deleted",
            )


async def _require_documents_workspace_scope(
    db: AsyncSession,
    *,
    documents: list[Document],
    workspace_id: str | None,
) -> None:
    """Reject a Document explicitly contained by another Workspace."""
    if not workspace_id or not documents:
        return

    workspace_ids_by_document = await document_workspace_ids_batched(db, documents)
    for document in documents:
        linked_workspace_ids = workspace_ids_by_document.get(str(document.id), set())
        if linked_workspace_ids and workspace_id not in linked_workspace_ids:
            raise FilesystemAccessDenied(
                403,
                "The document belongs to another Workspace",
            )


async def folder_projection_for_path(
    db: AsyncSession,
    *,
    entity_id: str,
    rel_path: str,
) -> DocumentFolder | None:
    parent_id: str | None = None
    folder: DocumentFolder | None = None
    for name in (part for part in normalize_rel_path(rel_path).split("/") if part):
        folder = await db.scalar(
            select(DocumentFolder).where(
                DocumentFolder.entity_id == entity_id,
                DocumentFolder.name == name,
                DocumentFolder.parent_id == parent_id,
            ).limit(1)
        )
        if folder is None:
            return None
        parent_id = folder.id
    return folder


async def require_directory_write_access(
    db: AsyncSession,
    *,
    user: User,
    rel_path: str,
) -> None:
    """Require upload/create access to an existing directory projection."""
    if not await effective_user_has_permission(db, user, Permission.DOCS_UPLOAD):
        raise FilesystemAccessDenied(403, "This role cannot create or upload documents")
    rel_path = normalize_rel_path(rel_path)
    if not rel_path:
        return
    folder = await folder_projection_for_path(
        db,
        entity_id=user.entity_id,
        rel_path=rel_path,
    )
    await _require_active_workspace_path_owner(
        db,
        entity_id=user.entity_id,
        rel_path=rel_path,
        folder=folder,
    )
    if await user_is_effective_entity_admin(db, user):
        return
    if folder is None:
        raise FilesystemAccessDenied(
            403,
            "Create folders through Knowledge so parent permissions can be enforced",
        )
    if folder.owner_id == user.id:
        return
    if await user_has_folder_capability(
        db,
        entity_id=user.entity_id,
        folder_id=folder.id,
        user_id=user.id,
        capabilities={Capability.UPLOAD_TO, Capability.EDIT},
    ):
        return
    raise FilesystemAccessDenied(
        403,
        "Upload or edit access is required for the target folder",
    )


async def require_path_write_access(
    db: AsyncSession,
    *,
    user: User,
    rel_path: str,
    exists: bool,
    workspace_id: str | None = None,
) -> None:
    """Fail closed for path-based writes that bypass a Document id."""
    inside_workspace_artifacts = await _require_workspace_artifact_scope(
        db,
        user=user,
        rel_path=rel_path,
        workspace_id=workspace_id,
    )
    documents = list((await db.scalars(
        select(Document).where(
            Document.entity_id == user.entity_id,
            Document.fs_path == rel_path,
            Document.is_trashed.is_(False),
        )
    )).all())
    if documents:
        await _require_active_workspace_path_owner(
            db,
            entity_id=user.entity_id,
            rel_path=rel_path,
            documents=documents,
        )
        await _require_documents_workspace_scope(
            db,
            documents=documents,
            workspace_id=workspace_id,
        )
        for document in documents:
            if not await user_can_edit_document(db, document, user=user):
                raise FilesystemAccessDenied(
                    403,
                    "Edit access is required to write this file",
                )
        return
    await _require_active_workspace_path_owner(
        db,
        entity_id=user.entity_id,
        rel_path=rel_path,
    )
    is_entity_admin = await user_is_effective_entity_admin(db, user)
    if exists and not is_entity_admin:
        raise FilesystemAccessDenied(
            403,
            "Unindexed files can only be overwritten by an entity administrator",
        )
    if not exists and not is_entity_admin:
        if inside_workspace_artifacts:
            return
        await require_directory_write_access(
            db,
            user=user,
            rel_path=os.path.dirname(rel_path),
        )


async def _active_documents_for_path(
    db: AsyncSession,
    *,
    entity_id: str,
    rel_path: str,
    is_dir: bool,
) -> list[Document]:
    filters = [Document.fs_path == rel_path]
    if is_dir:
        filters.append(Document.fs_path.like(rel_path.rstrip("/") + "/%"))
    return list((await db.scalars(
        select(Document).where(
            Document.entity_id == entity_id,
            Document.is_trashed.is_(False),
            or_(*filters),
        )
    )).all())


def _filesystem_file_paths(full_path: str, root: str) -> set[str]:
    if not os.path.isdir(full_path):
        return {normalize_rel_path(os.path.relpath(full_path, root))}
    paths: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(full_path):
        for filename in filenames:
            paths.add(normalize_rel_path(os.path.relpath(
                os.path.join(dirpath, filename),
                root,
            )))
    return paths


async def _user_can_delete_document(
    db: AsyncSession,
    *,
    user: User,
    document: Document,
) -> bool:
    if await document_is_owned_by_deleted_workspace(
        db,
        document,
        for_update=True,
    ):
        return False
    if await user_is_effective_entity_admin(db, user) or document.owner_id == user.id:
        return True
    if document.created_by == user.id:
        return True
    return await user_has_document_capability(
        db,
        document=document,
        user_id=user.id,
        capabilities={Capability.DELETE},
    )


async def _folder_subtree_ids(
    db: AsyncSession,
    *,
    entity_id: str,
    root_id: str,
) -> set[str]:
    rows = (await db.execute(
        select(DocumentFolder.id, DocumentFolder.parent_id).where(
            DocumentFolder.entity_id == entity_id,
        )
    )).all()
    children: dict[str | None, list[str]] = {}
    for folder_id, parent_id in rows:
        children.setdefault(parent_id, []).append(folder_id)
    found: set[str] = set()
    pending = [root_id]
    while pending:
        folder_id = pending.pop()
        if folder_id in found:
            continue
        found.add(folder_id)
        pending.extend(children.get(folder_id, []))
    return found


async def require_path_mutation_access(
    db: AsyncSession,
    *,
    user: User,
    rel_path: str,
    full_path: str,
    entity_root: str,
    action: str,
    workspace_id: str | None = None,
    destination_rel_path: str | None = None,
) -> list[Document]:
    """Authorize a low-level move/delete against every affected projection."""
    await _require_workspace_artifact_scope(
        db,
        user=user,
        rel_path=rel_path,
        workspace_id=workspace_id,
    )
    is_dir = os.path.isdir(full_path)
    documents = await _active_documents_for_path(
        db,
        entity_id=user.entity_id,
        rel_path=rel_path,
        is_dir=is_dir,
    )
    await _require_documents_workspace_scope(
        db,
        documents=documents,
        workspace_id=workspace_id,
    )
    folder = None
    if is_dir:
        folder = await folder_projection_for_path(
            db,
            entity_id=user.entity_id,
            rel_path=rel_path,
        )
        if folder is not None:
            from packages.core.services.workspace_artifacts import contains_workspace_artifact_root

            folder_ids = await _folder_subtree_ids(
                db,
                entity_id=user.entity_id,
                root_id=folder.id,
            )
            if await contains_workspace_artifact_root(
                db,
                entity_id=user.entity_id,
                folder_ids=folder_ids,
            ):
                raise FilesystemAccessDenied(
                    409,
                    "Workspace folders cannot be moved or deleted",
                )
    await _require_active_workspace_path_owner(
        db,
        entity_id=user.entity_id,
        rel_path=rel_path,
        documents=documents,
        folder=folder,
    )
    if destination_rel_path:
        destination_folder = await folder_projection_for_path(
            db,
            entity_id=user.entity_id,
            rel_path=os.path.dirname(destination_rel_path),
        )
        await _require_active_workspace_path_owner(
            db,
            entity_id=user.entity_id,
            rel_path=destination_rel_path,
            folder=destination_folder,
        )
    if is_dir:
        if not await user_is_effective_entity_admin(db, user):
            if folder is None:
                raise FilesystemAccessDenied(
                    403,
                    f"Unindexed folders can only be {action}d by an entity administrator",
                )
            if folder.owner_id != user.id:
                raise FilesystemAccessDenied(
                    403,
                    "Only the folder owner can move or delete this folder",
                )
    if await user_is_effective_entity_admin(db, user):
        return documents

    indexed_paths = {document.fs_path for document in documents if document.fs_path}
    disk_paths = _filesystem_file_paths(full_path, entity_root)
    if (not is_dir and not documents) or disk_paths - indexed_paths:
        raise FilesystemAccessDenied(
            403,
            f"Unindexed files can only be {action}d by an entity administrator",
        )

    for document in documents:
        allowed = (
            await user_can_edit_document(db, document, user=user)
            if action == "move"
            else await _user_can_delete_document(db, user=user, document=document)
        )
        if not allowed:
            capability = "Edit" if action == "move" else "Delete"
            raise FilesystemAccessDenied(
                403,
                f"{capability} access is required to {action} this path",
            )
    return documents
