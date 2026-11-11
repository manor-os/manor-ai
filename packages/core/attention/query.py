"""Reading a Workspace's open attention items."""
from __future__ import annotations

from typing import Any

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.attention import AttentionState, AttentionUrgency
from packages.core.models.attention import AttentionItem

_URGENCY_ORDER = case(
    {
        AttentionUrgency.ACT_NOW.value: 0,
        AttentionUrgency.ACT_TODAY.value: 1,
        AttentionUrgency.DECIDE.value: 2,
        AttentionUrgency.FYI.value: 3,
    },
    value=AttentionItem.urgency,
    else_=4,
)


def serialize_attention_item(item: AttentionItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "workspace_id": item.workspace_id,
        "kind": item.kind,
        "urgency": item.urgency,
        "stage": item.stage,
        "title": item.title,
        "summary": item.summary,
        "subject": {"type": item.subject_type, "id": item.subject_id},
        "evidence": item.evidence or {},
        "assignee_user_id": item.assignee_user_id,
        "opened_at": item.opened_at.isoformat() if item.opened_at else None,
        "last_changed_at": item.last_changed_at.isoformat() if item.last_changed_at else None,
    }


async def list_open_attention(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    limit: int = 50,
) -> list[AttentionItem]:
    """Open items for one Workspace: most urgent first, then oldest first."""
    return list((await db.execute(
        select(AttentionItem)
        .where(
            AttentionItem.entity_id == entity_id,
            AttentionItem.workspace_id == workspace_id,
            AttentionItem.state == AttentionState.OPEN.value,
        )
        .order_by(_URGENCY_ORDER, AttentionItem.opened_at.asc())
        .limit(max(1, min(limit, 200)))
    )).scalars().all())
