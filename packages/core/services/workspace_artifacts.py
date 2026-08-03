"""Stable storage and user-facing folder mapping for workspace artifacts."""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentFolder
from packages.core.models.workspace import Workspace
from packages.core.services.knowledge_visibility import normalize_rel_path


WORKSPACE_FOLDER_ROOT = "Workspaces"
WORKSPACE_STORAGE_SEGMENT = "_by_id"


@dataclass(frozen=True)
class WorkspaceFolderBinding:
    workspace_id: str
    artifact_folder_id: str
    display_folder_name: str
    relative_parts: tuple[str, ...] = ()

    @property
    def storage_dir(self) -> str:
        parts = [workspace_artifact_storage_base(self.artifact_folder_id), *self.relative_parts]
        return "/".join(part for part in parts if part)

    @property
    def display_dir(self) -> str:
        parts = [workspace_artifact_display_base(self.display_folder_name), *self.relative_parts]
        return "/".join(part for part in parts if part)


@dataclass(frozen=True)
class WorkspaceArtifactDirectory:
    """Physical and user-facing locations for one Workspace artifact directory."""

    folder_id: str
    storage_path: str
    display_path: str


def workspace_artifact_storage_base(folder_id: str | None) -> str:
    """Return the immutable physical root for a workspace's artifacts."""
    clean_id = str(folder_id or "").strip()
    if not clean_id:
        return ""
    return f"{WORKSPACE_FOLDER_ROOT}/{WORKSPACE_STORAGE_SEGMENT}/{clean_id}"


def workspace_artifact_display_base(folder_name: str | None) -> str:
    """Return the logical Knowledge path shown to users."""
    return f"{WORKSPACE_FOLDER_ROOT}/{_display_folder_name(folder_name)}"


def workspace_artifact_display_path(
    location: str,
    *,
    artifact_folder_id: str | None,
    display_folder_name: str | None,
) -> str:
    """Translate an internal workspace path to its logical display path."""
    raw = str(location or "").strip()
    storage_base = workspace_artifact_storage_base(artifact_folder_id)
    if not raw or not storage_base:
        return raw
    normalized = normalize_rel_path(raw)
    marker_index = normalized.find(storage_base)
    if marker_index > 0 and normalized[marker_index - 1] == "/":
        normalized = normalized[marker_index:]
    if normalized != storage_base and not normalized.startswith(f"{storage_base}/"):
        return raw
    suffix = normalized[len(storage_base):].lstrip("/")
    display_base = workspace_artifact_display_base(display_folder_name)
    return "/".join(part for part in (display_base, suffix) if part)


def artifact_folder_id_from_storage_path(location: str | None) -> str | None:
    """Extract the canonical workspace folder ID from a physical path."""
    normalized = normalize_rel_path(str(location or ""))
    parts = [part for part in normalized.split("/") if part]
    for index in range(max(0, len(parts) - 2)):
        if parts[index:index + 2] == [WORKSPACE_FOLDER_ROOT, WORKSPACE_STORAGE_SEGMENT]:
            return parts[index + 2] if len(parts) > index + 2 else None
    return None


async def infer_workspace_id_from_storage_path(
    *,
    entity_id: str,
    rel_path: str,
) -> str | None:
    """Resolve manually added ID-scoped files back to their workspace."""
    folder_id = artifact_folder_id_from_storage_path(rel_path)
    if not entity_id or not folder_id:
        return None
    from packages.core.database import async_session

    async with async_session() as db:
        return (await db.execute(
            select(Workspace.id).where(
                Workspace.entity_id == entity_id,
                Workspace.artifact_folder_id == folder_id,
                Workspace.deleted_at.is_(None),
            ).limit(1)
        )).scalar_one_or_none()


async def ensure_workspace_artifact_folder(
    db: AsyncSession,
    workspace: Workspace,
) -> DocumentFolder:
    """Ensure one DocumentFolder is the workspace's logical and storage ID.

    Legacy workspaces reuse an unclaimed ``Workspaces/<workspace name>`` folder
    when one exists. This preserves the user's Knowledge tree while switching
    all future physical writes to the folder ID.
    """
    root = await _ensure_folder(db, entity_id=workspace.entity_id, name=WORKSPACE_FOLDER_ROOT, parent_id=None)
    # Serialize sibling name allocation and legacy-folder claims per entity.
    root = (await db.execute(
        select(DocumentFolder).where(DocumentFolder.id == root.id).with_for_update()
    )).scalar_one()
    desired_name = _display_folder_name(workspace.name)

    linked: DocumentFolder | None = None
    if workspace.artifact_folder_id:
        linked = (await db.execute(
            select(DocumentFolder).where(
                DocumentFolder.id == workspace.artifact_folder_id,
                DocumentFolder.entity_id == workspace.entity_id,
            ).limit(1)
        )).scalar_one_or_none()

    if linked is None:
        legacy = (await db.execute(
            select(DocumentFolder).where(
                DocumentFolder.entity_id == workspace.entity_id,
                DocumentFolder.parent_id == root.id,
                DocumentFolder.name == desired_name,
            ).limit(1)
        )).scalar_one_or_none()
        if (
            legacy is not None
            and not await _folder_claimed_by_other_workspace(db, workspace, legacy.id)
            and await _legacy_folder_belongs_to_workspace(db, workspace, legacy.id)
        ):
            linked = legacy
        else:
            folder_name = await _available_folder_name(
                db,
                workspace=workspace,
                parent_id=root.id,
                desired_name=desired_name,
            )
            linked = DocumentFolder(
                id=generate_ulid(),
                entity_id=workspace.entity_id,
                name=folder_name,
                parent_id=root.id,
            )
            db.add(linked)
            await db.flush()
        workspace.artifact_folder_id = linked.id
        await db.flush()

    if linked.parent_id != root.id:
        linked.parent_id = root.id
    target_name = await _available_folder_name(
        db,
        workspace=workspace,
        parent_id=root.id,
        desired_name=desired_name,
        current_folder_id=linked.id,
    )
    if linked.name != target_name:
        linked.name = target_name
    await db.flush()
    return linked


async def ensure_workspace_document_folder(
    *,
    entity_id: str,
    workspace_id: str,
    rel_path: str,
) -> str | None:
    """Project a physical workspace file into its logical folder tree."""
    if not entity_id or not workspace_id:
        return None
    from packages.core.database import async_session

    async with async_session() as db:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.entity_id == entity_id,
                Workspace.deleted_at.is_(None),
            ).with_for_update().limit(1)
        )).scalar_one_or_none()
        if workspace is None:
            return None
        root = await ensure_workspace_artifact_folder(db, workspace)
        parent_id = root.id
        for name in _artifact_relative_directory_parts(rel_path, root.id):
            child = await _ensure_folder(db, entity_id=entity_id, name=name, parent_id=parent_id)
            parent_id = child.id
        await db.commit()
        return parent_id


async def ensure_workspace_artifact_directory(
    *,
    entity_id: str,
    workspace_id: str,
    directory_path: str,
) -> WorkspaceArtifactDirectory:
    """Create a Workspace-scoped artifact directory and its Knowledge folders."""
    if not entity_id or not workspace_id:
        raise ValueError("entity_id and workspace_id are required")
    from packages.core.database import async_session

    async with async_session() as db:
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.entity_id == entity_id,
                Workspace.deleted_at.is_(None),
            ).with_for_update().limit(1)
        )).scalar_one_or_none()
        if workspace is None:
            raise ValueError("Workspace artifact directory could not resolve the Workspace")
        root = await ensure_workspace_artifact_folder(db, workspace)
        marker_path = f"{normalize_rel_path(directory_path)}/.artifact"
        relative_parts = _artifact_relative_directory_parts(marker_path, root.id)
        parent_id = root.id
        for name in relative_parts:
            child = await _ensure_folder(
                db,
                entity_id=entity_id,
                name=name,
                parent_id=parent_id,
            )
            parent_id = child.id
        await db.commit()
        storage_path = "/".join(
            part
            for part in (workspace_artifact_storage_base(root.id), *relative_parts)
            if part
        )
        display_path = "/".join(
            part
            for part in (workspace_artifact_display_base(root.name), *relative_parts)
            if part
        )
        return WorkspaceArtifactDirectory(
            folder_id=parent_id,
            storage_path=storage_path,
            display_path=display_path,
        )


async def resolve_workspace_folder_binding(
    db: AsyncSession,
    *,
    entity_id: str,
    folder_id: str | None,
) -> WorkspaceFolderBinding | None:
    """Resolve a logical folder (including children) to workspace storage."""
    current_id = str(folder_id or "").strip()
    if not current_id:
        return None
    relative_parts: list[str] = []
    seen: set[str] = set()
    while current_id and current_id not in seen:
        seen.add(current_id)
        folder = (await db.execute(
            select(DocumentFolder).where(
                DocumentFolder.id == current_id,
                DocumentFolder.entity_id == entity_id,
            ).limit(1)
        )).scalar_one_or_none()
        if folder is None:
            return None
        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.entity_id == entity_id,
                Workspace.artifact_folder_id == folder.id,
                Workspace.deleted_at.is_(None),
            ).limit(1)
        )).scalar_one_or_none()
        if workspace is not None:
            return WorkspaceFolderBinding(
                workspace_id=workspace.id,
                artifact_folder_id=folder.id,
                display_folder_name=folder.name,
                relative_parts=tuple(reversed(relative_parts)),
            )
        relative_parts.append(_display_folder_name(folder.name))
        current_id = str(folder.parent_id or "")
    return None


async def contains_workspace_artifact_root(
    db: AsyncSession,
    *,
    entity_id: str,
    folder_ids: set[str],
) -> bool:
    if not folder_ids:
        return False
    return (await db.execute(
        select(Workspace.id).where(
            Workspace.entity_id == entity_id,
            Workspace.artifact_folder_id.in_(folder_ids),
        ).limit(1)
    )).scalar_one_or_none() is not None


async def _ensure_folder(
    db: AsyncSession,
    *,
    entity_id: str,
    name: str,
    parent_id: str | None,
) -> DocumentFolder:
    existing = (await db.execute(
        select(DocumentFolder).where(
            DocumentFolder.entity_id == entity_id,
            DocumentFolder.name == name,
            DocumentFolder.parent_id == parent_id,
        ).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return existing
    folder_id = generate_ulid()
    result = await db.execute(
        pg_insert(DocumentFolder)
        .values(id=folder_id, entity_id=entity_id, name=name, parent_id=parent_id)
        .on_conflict_do_nothing()
        .returning(DocumentFolder.id)
    )
    created_id = result.scalar_one_or_none()
    if created_id:
        return (await db.execute(
            select(DocumentFolder).where(DocumentFolder.id == created_id).limit(1)
        )).scalar_one()
    return (await db.execute(
        select(DocumentFolder).where(
            DocumentFolder.entity_id == entity_id,
            DocumentFolder.name == name,
            DocumentFolder.parent_id == parent_id,
        ).limit(1)
    )).scalar_one()


async def _folder_claimed_by_other_workspace(
    db: AsyncSession,
    workspace: Workspace,
    folder_id: str,
) -> bool:
    return (await db.execute(
        select(Workspace.id).where(
            Workspace.entity_id == workspace.entity_id,
            Workspace.artifact_folder_id == folder_id,
            Workspace.id != workspace.id,
        ).limit(1)
    )).scalar_one_or_none() is not None


async def _legacy_folder_belongs_to_workspace(
    db: AsyncSession,
    workspace: Workspace,
    folder_id: str,
) -> bool:
    """Avoid claiming a merely same-named user folder or purged workspace."""
    return (await db.execute(
        select(Document.id).where(
            Document.entity_id == workspace.entity_id,
            Document.folder_id == folder_id,
            Document.metadata_["origin"]["workspace_id"].astext == workspace.id,
        ).limit(1)
    )).scalar_one_or_none() is not None


async def _available_folder_name(
    db: AsyncSession,
    *,
    workspace: Workspace,
    parent_id: str,
    desired_name: str,
    current_folder_id: str | None = None,
) -> str:
    candidate = desired_name
    suffix = str(workspace.id or "")[-6:] or "workspace"
    attempt = 1
    while True:
        conditions = [
            DocumentFolder.entity_id == workspace.entity_id,
            DocumentFolder.parent_id == parent_id,
            DocumentFolder.name == candidate,
        ]
        if current_folder_id:
            conditions.append(DocumentFolder.id != current_folder_id)
        conflict = (await db.execute(
            select(DocumentFolder.id).where(*conditions).limit(1)
        )).scalar_one_or_none()
        if conflict is None:
            return candidate
        tag = suffix if attempt == 1 else f"{suffix}-{attempt}"
        candidate = _truncate_folder_name(f"{desired_name} ({tag})")
        attempt += 1


def _artifact_relative_directory_parts(rel_path: str, folder_id: str) -> list[str]:
    normalized = normalize_rel_path(rel_path)
    directory = normalize_rel_path(posixpath.dirname(normalized))
    parts = [part for part in directory.split("/") if part]
    internal_prefix = [WORKSPACE_FOLDER_ROOT, WORKSPACE_STORAGE_SEGMENT, folder_id]
    if parts[:3] == internal_prefix:
        parts = parts[3:]
    elif parts[:2] == [WORKSPACE_FOLDER_ROOT, WORKSPACE_STORAGE_SEGMENT]:
        raise ValueError("Workspace artifact path belongs to a different folder ID")
    elif len(parts) >= 2 and parts[0] == WORKSPACE_FOLDER_ROOT:
        # Legacy name-based workspace path.
        parts = parts[2:]
    return [_display_folder_name(part) for part in parts]


def _display_folder_name(value: str | None) -> str:
    clean = re.sub(r"[\x00-\x1f\x7f/\\]+", " - ", str(value or "").strip())
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    return _truncate_folder_name(clean or "Workspace")


def _truncate_folder_name(value: str, max_length: int = 255) -> str:
    return value[:max_length].rstrip(" .") or "Workspace"
