"""Authorization contract shared by every static-site publication surface."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Document
from packages.core.models.permission import Classification
from packages.core.models.site import Site
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.permissions import (
    effective_user_role_name,
    user_is_effective_entity_admin,
)
from packages.core.services.document_access import (
    user_can_edit_document,
    user_can_share_document_externally,
)
from packages.core.services.site_publisher import PublishTarget
from packages.core.services.workspace_access import user_can_manage_workspace


class SitePublishAccessDenied(PermissionError):
    """Raised when an actor cannot expose the requested snapshot publicly."""


async def user_can_manage_site(
    db: AsyncSession,
    *,
    user: User,
    site: Site,
) -> bool:
    """Authorize durable site management independently of live source ACLs."""
    if site.entity_id != user.entity_id:
        return False
    if await user_is_effective_entity_admin(db, user):
        return True
    if site.created_by_user_id and site.created_by_user_id == user.id:
        return True

    effective_role = await effective_user_role_name(db, user)
    workspace_id = str(site.workspace_id or "").strip()
    if workspace_id:
        workspace = await db.scalar(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.entity_id == user.entity_id,
                Workspace.deleted_at.is_(None),
                Workspace.status == "active",
            )
        )
        if workspace is not None and await user_can_manage_workspace(
            db,
            workspace_id=workspace.id,
            user_id=user.id,
            entity_role=effective_role,
        ):
            return True

    # Compatibility for rows published before durable ownership existed.  An
    # edit grant alone is deliberately insufficient for external-site control.
    if site.created_by_user_id is None:
        source_path = site.source_path
        if not source_path.lower().endswith((".html", ".htm")):
            source_path = f"{source_path.rstrip('/')}/{site.entry}"
        source = await db.scalar(
            select(Document).where(
                Document.entity_id == user.entity_id,
                Document.fs_path == source_path,
                Document.is_trashed.is_(False),
            ).limit(1)
        )
        if source is not None and await user_can_share_document_externally(
            db,
            source,
            user=user,
        ):
            return True
    return False


def target_entry_path(target: PublishTarget) -> str:
    if target.kind == "file":
        return target.root_rel
    return f"{target.root_rel.rstrip('/')}/{target.entry}"


async def require_site_publish_access(
    db: AsyncSession,
    *,
    user: User,
    target: PublishTarget,
    included_paths: set[str],
) -> None:
    """Require edit and external-share access to one prepared snapshot.

    Entity administrators may publish legacy unindexed filesystem bundles.
    Other roles need every copied file to have an active Document projection.
    Every projected file must be editable and externally shareable by the
    actor. Multiple legacy rows are deliberately all checked until the
    uniqueness migration has repaired them.
    """
    documents = list((await db.scalars(
        select(Document).where(
            Document.entity_id == user.entity_id,
            Document.fs_path.in_(sorted(included_paths)),
            Document.is_trashed.is_(False),
        )
    )).all()) if included_paths else []
    indexed_paths = {document.fs_path for document in documents if document.fs_path}
    missing_paths = included_paths - indexed_paths
    entry_path = target_entry_path(target)
    if (
        (entry_path not in indexed_paths or missing_paths)
        and not await user_is_effective_entity_admin(db, user)
    ):
        raise SitePublishAccessDenied(
            "Every file in this site must be indexed before it can be published"
        )

    for document in documents:
        classification = getattr(document, "classification", None)
        if classification == Classification.RESTRICTED:
            raise SitePublishAccessDenied(
                "Restricted documents cannot be published externally"
            )
        if classification == Classification.CONFIDENTIAL:
            raise SitePublishAccessDenied(
                "Confidential documents require the external-share approval flow"
            )
        if not await user_can_edit_document(db, document, user=user):
            raise SitePublishAccessDenied(
                "Edit access is required to publish every document in this site"
            )
        if not await user_can_share_document_externally(
            db,
            document,
            user=user,
        ):
            raise SitePublishAccessDenied(
                "External share access is required to publish every document in this site"
            )
