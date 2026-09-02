"""Shared public projections for declarative Webchat page modules."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.contracts.webchat_page import (
    ResolvedWorkspaceContent,
    WebchatPage,
    public_webchat_page,
)
from packages.core.models.document import Document, DocumentChunk


def public_workspace_name(workspace: object) -> str:
    """Return the single public Workspace identity used by Webchat surfaces."""

    identity_label = str(getattr(workspace, "identity_label", "") or "").strip()
    name = identity_label or str(getattr(workspace, "name", "") or "").strip()
    return name[:160]


async def resolve_public_document_content(
    db: AsyncSession,
    document: Document,
) -> ResolvedWorkspaceContent:
    """Return the exact document projection used by review and public pages."""
    metadata = document.metadata_ if isinstance(document.metadata_, dict) else {}
    body = str(
        metadata.get("public_summary")
        or metadata.get("description")
        or metadata.get("content_text")
        or ""
    )
    if not body:
        chunks = list((await db.scalars(
            select(DocumentChunk.content)
            .where(DocumentChunk.document_id == document.id)
            .order_by(DocumentChunk.chunk_index.asc())
            .limit(3)
        )).all())
        body = "\n\n".join(str(chunk) for chunk in chunks)
    return ResolvedWorkspaceContent(
        name=str(document.name)[:160],
        body=body[:4000],
    )


def webchat_page_for_storage(page: WebchatPage) -> WebchatPage:
    """Strip read-time Workspace projections before persisting a page."""

    return page.model_copy(update={
        "modules": [
            module.model_copy(update={"resolved": None})
            if module.type == "workspace_content"
            else module
            for module in page.modules
        ],
    })


async def resolve_workspace_webchat_page(
    db: AsyncSession,
    *,
    value: object,
    entity_id: str,
    workspace_id: str | None,
) -> WebchatPage | None:
    """Resolve exactly the Workspace references that are public right now."""

    page = public_webchat_page(value)
    if page is None:
        return None
    workspace = str(workspace_id or "").strip()
    if not workspace:
        return page.model_copy(update={
            "modules": [
                module
                for module in page.modules
                if module.type not in {"workspace_content", "workspace_action"}
            ],
        })

    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
    from packages.core.models.workspace import Workspace
    from packages.core.services.document_access import (
        public_agent_visible_document_ids_batched,
    )

    action_binding_ids = {
        module.binding_id
        for module in page.modules
        if module.type == "workspace_action"
    }
    active_action_binding_ids = set((await db.scalars(
        select(WorkflowBinding.id)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowBinding.workflow_id)
        .where(
            WorkflowBinding.id.in_(action_binding_ids),
            WorkflowBinding.entity_id == entity_id,
            WorkflowBinding.workspace_id == workspace,
            WorkflowBinding.trigger_type == "manual",
            WorkflowBinding.enabled.is_(True),
            WorkflowBinding.status == "active",
            WorkflowDefinition.entity_id == entity_id,
            WorkflowDefinition.is_active.is_(True),
            WorkflowDefinition.status == "active",
        )
    )).all()) if action_binding_ids else set()

    document_ids = {
        module.resource_id
        for module in page.modules
        if module.type == "workspace_content"
        and module.source == "document"
        and module.resource_id
    }
    documents = list((await db.scalars(
        select(Document).where(
            Document.id.in_(document_ids),
            Document.entity_id == entity_id,
            Document.is_trashed.is_(False),
        )
    )).all()) if document_ids else []
    document_by_id = {str(document.id): document for document in documents}
    visible_document_ids = await public_agent_visible_document_ids_batched(
        db,
        documents,
        entity_id=entity_id,
        workspace_id=workspace,
    )

    needs_profile = any(
        module.type == "workspace_content" and module.source == "profile"
        for module in page.modules
    )
    workspace_row = await db.scalar(select(Workspace).where(
        Workspace.id == workspace,
        Workspace.entity_id == entity_id,
        Workspace.deleted_at.is_(None),
    )) if needs_profile else None
    profile = None
    if workspace_row is not None:
        image_url = str(workspace_row.cover_image_url or "")
        profile_values = {
            "name": public_workspace_name(workspace_row),
            "body": str(workspace_row.description or "")[:4000],
            "image_url": image_url,
            "items": [
                str(item)[:500]
                for item in (workspace_row.category, workspace_row.address)
                if item
            ][:8],
        }
        try:
            profile = ResolvedWorkspaceContent(**profile_values)
        except ValueError:
            profile = ResolvedWorkspaceContent(**{**profile_values, "image_url": ""})

    resolved_documents: dict[str, ResolvedWorkspaceContent] = {}
    modules = []
    for module in page.modules:
        if module.type == "workspace_action":
            if module.binding_id in active_action_binding_ids:
                modules.append(module)
            continue
        if module.type != "workspace_content":
            modules.append(module)
            continue
        if module.source == "profile":
            resolved = profile
        elif module.resource_id in visible_document_ids:
            resolved = resolved_documents.get(module.resource_id)
            if resolved is None:
                resolved = await resolve_public_document_content(
                    db,
                    document_by_id[module.resource_id],
                )
                resolved_documents[module.resource_id] = resolved
        else:
            resolved = None
        if resolved is not None:
            modules.append(module.model_copy(update={"resolved": resolved}))
    return page.model_copy(update={"modules": modules})
