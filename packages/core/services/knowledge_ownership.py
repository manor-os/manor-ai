"""Ownership transfer for Knowledge left behind by departed users."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Document, DocumentFolder
from packages.core.services.audit_service import log_action
from packages.core.services.tool_cache_version import bump_tool_cache_version


@dataclass(frozen=True)
class KnowledgeOwnershipTransferResult:
    document_count: int
    folder_count: int


async def transfer_departed_user_knowledge_to_entity(
    db: AsyncSession,
    *,
    entity_id: str,
    departing_user_id: str,
    actor_user_id: str,
) -> KnowledgeOwnershipTransferResult:
    """Move one departed user's Knowledge ownership to the entity.

    The transfer intentionally changes ownership and visibility only. Stable
    creator/audit fields and physical paths remain intact.
    """
    all_folders = list((await db.execute(
        select(DocumentFolder)
        .where(
            DocumentFolder.entity_id == entity_id,
            or_(
                DocumentFolder.owner_id == departing_user_id,
                DocumentFolder.owner_id.is_(None),
            ),
        )
        .order_by(DocumentFolder.id)
        .with_for_update()
    )).scalars())
    folders = [folder for folder in all_folders if folder.owner_id == departing_user_id]
    entity_folders = [folder for folder in all_folders if folder.owner_id is None]
    documents = list((await db.execute(
        select(Document)
        .where(
            Document.entity_id == entity_id,
            Document.owner_id == departing_user_id,
        )
        .order_by(Document.id)
        .with_for_update()
    )).scalars())
    departing_folder_ids = {str(folder.id) for folder in folders}
    entity_folder_ids = {str(folder.id) for folder in entity_folders}
    for document in documents:
        # Keep documents inside folders that are transferred with them or are
        # already company-owned. A legacy document nested under somebody
        # else's private folder must be promoted to root or the transfer would
        # leave it inaccessible despite its new company ownership.
        folder_id = str(document.folder_id or "")
        if folder_id and folder_id not in departing_folder_ids | entity_folder_ids:
            document.folder_id = None
        document.owner_id = None
        document.visibility = "entity"
    occupied_positions = {
        (str(folder.parent_id or ""), str(folder.name or ""))
        for folder in entity_folders
    }
    transfer_suffix = str(departing_user_id)[-8:]
    for folder in folders:
        # A transferred tree cannot remain beneath somebody else's private
        # folder: it would be entity-owned but still unreachable through the
        # parent path. Promote each owned root to the entity root.
        if str(folder.parent_id or "") not in departing_folder_ids:
            folder.parent_id = None
        base_name = str(folder.name or "Transferred Knowledge")
        candidate_name = base_name
        counter = 1
        position = (str(folder.parent_id or ""), candidate_name)
        while position in occupied_positions:
            counter_suffix = "" if counter == 1 else f" {counter}"
            candidate_name = (
                f"{base_name} · transferred {transfer_suffix}{counter_suffix}"
            )
            counter += 1
            position = (str(folder.parent_id or ""), candidate_name)
        folder.name = candidate_name
        occupied_positions.add(position)
        folder.owner_id = None
        folder.visibility = "entity"

    result = KnowledgeOwnershipTransferResult(
        document_count=len(documents),
        folder_count=len(folders),
    )
    await log_action(
        db,
        entity_id,
        action="knowledge.ownership.transfer_to_entity",
        resource_type="user",
        resource_id=departing_user_id,
        user_id=actor_user_id,
        details={
            "documents_transferred": result.document_count,
            "folders_transferred": result.folder_count,
        },
    )
    await bump_tool_cache_version(entity_id, "documents")
    return result
