"""Workspace-scoped dashboard analytics."""
from __future__ import annotations

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.task import Task
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.workspace import AgentSubscription
from packages.core.models.custom_field import CustomFieldDefinition
from packages.core.services.task_deadlines import task_deadline_overdue_expr
from packages.core.services.timezone_utils import user_current_date, utc_now


async def get_workspace_stats(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
    *,
    timezone_name: str | None = None,
) -> dict:
    """Get stats for a specific workspace."""
    now = utc_now()
    today = user_current_date(timezone_name, now)

    # ── Tasks by status ──
    task_q = select(
        func.count().label("total"),
        func.count().filter(Task.status == "pending").label("pending"),
        func.count().filter(Task.status == "in_progress").label("in_progress"),
        func.count().filter(Task.status == "completed").label("completed"),
        func.count().filter(Task.status == "cancelled").label("cancelled"),
        func.count().filter(
            task_deadline_overdue_expr(
                Task.deadline,
                now_expr=now,
                current_date_expr=today,
            )
            & (Task.status.notin_(["completed", "cancelled"]))
        ).label("overdue"),
    ).where(
        (Task.entity_id == entity_id) & (Task.workspace_id == workspace_id)
    )
    t = (await db.execute(task_q)).one()

    # ── Workspace Knowledge documents ──
    # Only explicit workspace Knowledge Nets count here. Historical
    # auto-created "Workspace Files" buckets are provenance, not Knowledge.
    groups = (await db.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == workspace_id,
        )
    )).scalars().all()
    visible_group_ids = [
        group.id for group in groups
        if not (group.settings or {}).get("workspace_file_bucket")
    ]
    if visible_group_ids:
        doc_q = (
            select(func.count(distinct(DocumentGroupMember.document_id)))
            .select_from(DocumentGroupMember)
            .join(Document, Document.id == DocumentGroupMember.document_id)
            .where(
                DocumentGroupMember.group_id.in_(visible_group_ids),
                Document.entity_id == entity_id,
            )
        )
        doc_total = (await db.execute(doc_q)).scalar() or 0
    else:
        doc_total = 0

    # ── Agent subscriptions ──
    agent_q = select(func.count()).select_from(AgentSubscription).where(
        (AgentSubscription.entity_id == entity_id)
        & (AgentSubscription.workspace_id == workspace_id)
        & (AgentSubscription.status == "active")
    )
    agent_total = (await db.execute(agent_q)).scalar() or 0

    # ── Recent tasks ──
    recent_q = (
        select(Task)
        .where(
            (Task.entity_id == entity_id) & (Task.workspace_id == workspace_id)
        )
        .order_by(Task.created_at.desc())
        .limit(5)
    )
    recent_rows = (await db.execute(recent_q)).scalars().all()
    recent_tasks = [
        {
            "id": r.id,
            "title": r.title,
            "status": r.status,
            "priority": r.priority,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in recent_rows
    ]

    return {
        "workspace_id": workspace_id,
        "tasks": {
            "total": int(t.total),
            "by_status": {
                "pending": int(t.pending),
                "in_progress": int(t.in_progress),
                "completed": int(t.completed),
                "cancelled": int(t.cancelled),
            },
            "overdue": int(t.overdue),
        },
        "documents": {"total": int(doc_total)},
        "agents": {"total": int(agent_total)},
        "recent_tasks": recent_tasks,
    }


async def get_workspace_custom_field_summary(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
) -> list[dict]:
    """Get summary of custom field values across workspace tasks.

    For select fields: count per option.
    For number fields: min, max, avg.
    """
    # Get field definitions for this workspace targeting tasks
    fd_q = select(CustomFieldDefinition).where(
        (CustomFieldDefinition.entity_id == entity_id)
        & (CustomFieldDefinition.status == "active")
        & (CustomFieldDefinition.target == "task")
        & (
            (CustomFieldDefinition.workspace_id == workspace_id)
            | (CustomFieldDefinition.workspace_id.is_(None))
        )
    )
    field_defs = (await db.execute(fd_q)).scalars().all()
    if not field_defs:
        return []

    # Get all task details for workspace
    tasks_q = select(Task.details).where(
        (Task.entity_id == entity_id) & (Task.workspace_id == workspace_id)
    )
    task_details = (await db.execute(tasks_q)).scalars().all()

    summaries: list[dict] = []
    for fd in field_defs:
        summary: dict = {
            "field_id": fd.id,
            "name": fd.name,
            "display_name": fd.display_name,
            "field_type": fd.field_type,
        }

        values = []
        for details in task_details:
            if isinstance(details, dict):
                custom = details.get("custom_fields", {})
                if fd.name in custom and custom[fd.name] is not None:
                    values.append(custom[fd.name])

        if fd.field_type in ("select", "multiselect"):
            counts: dict[str, int] = {}
            for v in values:
                if isinstance(v, list):
                    for item in v:
                        counts[item] = counts.get(item, 0) + 1
                else:
                    counts[v] = counts.get(v, 0) + 1
            summary["distribution"] = counts
        elif fd.field_type == "number":
            nums = []
            for v in values:
                try:
                    nums.append(float(v))
                except (ValueError, TypeError):
                    continue
            if nums:
                summary["min"] = min(nums)
                summary["max"] = max(nums)
                summary["avg"] = round(sum(nums) / len(nums), 2)
            else:
                summary["min"] = None
                summary["max"] = None
                summary["avg"] = None
        else:
            summary["count"] = len(values)

        summaries.append(summary)

    return summaries
