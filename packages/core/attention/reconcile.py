"""Level-triggered reconciliation of one watcher's items.

The watcher says what is true now; this makes the open rows match. Missing
an event can never leave an item stale: the next sweep recomputes it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.attention.contracts import DesiredItem, Watcher
from packages.core.constants.attention import AttentionState
from packages.core.models.attention import AttentionItem
from packages.core.models.base import generate_ulid


# The partial unique index's predicate, as a literal. A bound parameter
# (``state = $n``) fails once Postgres uses a generic plan for the prepared
# insert (from its sixth run on a connection): the planner can no longer prove
# it implies the index's ``state = 'open'``, so no arbiter index is found.
_OPEN_INDEX_PREDICATE = text(f"state = '{AttentionState.OPEN.value}'")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def reconcile_watcher(
    db: AsyncSession,
    watcher: Watcher,
    *,
    now: datetime | None = None,
    entity_id: str | None = None,
) -> dict[str, int]:
    """Open, update and resolve ``watcher``'s items. Caller commits.

    ``entity_id`` limits both sides to one entity: its situations, and only
    its open rows are resolved.
    """
    now = now or datetime.now(timezone.utc)
    desired: dict[str, DesiredItem] = {}
    for item in await watcher.desired(db, now=now):
        if entity_id is None or item.entity_id == entity_id:
            desired.setdefault(item.fingerprint, item)

    open_query = select(AttentionItem).where(
        AttentionItem.source == watcher.key,
        AttentionItem.state == AttentionState.OPEN.value,
    )
    if entity_id is not None:
        open_query = open_query.where(AttentionItem.entity_id == entity_id)
    open_rows = {
        row.fingerprint: row
        for row in (await db.execute(open_query)).scalars().all()
    }

    stats = {"opened": 0, "updated": 0, "resolved": 0}
    for fingerprint, item in desired.items():
        row = open_rows.get(fingerprint)
        if row is None:
            result = await db.execute(
                insert(AttentionItem)
                .values(
                    id=generate_ulid(),
                    entity_id=item.entity_id,
                    workspace_id=item.workspace_id,
                    kind=item.kind,
                    source=watcher.key,
                    subject_type=item.subject_type,
                    subject_id=item.subject_id,
                    fingerprint=fingerprint,
                    stage=item.stage,
                    urgency=item.urgency,
                    title=item.title[:300],
                    summary=item.summary,
                    evidence=item.evidence,
                    assignee_user_id=item.assignee_user_id,
                    state=AttentionState.OPEN.value,
                    opened_at=_aware(item.opened_at),
                    last_changed_at=now,
                )
                # A concurrent sweep opened it first: theirs stands.
                .on_conflict_do_nothing(
                    index_elements=["fingerprint"],
                    index_where=_OPEN_INDEX_PREDICATE,
                )
            )
            stats["opened"] += result.rowcount or 0
            continue
        wanted = {
            "stage": item.stage,
            "urgency": item.urgency,
            "title": item.title[:300],
            "summary": item.summary,
            "evidence": item.evidence,
            "assignee_user_id": item.assignee_user_id,
        }
        changed = {key: value for key, value in wanted.items() if getattr(row, key) != value}
        if changed:
            for key, value in changed.items():
                setattr(row, key, value)
            row.last_changed_at = now
            stats["updated"] += 1

    for fingerprint, row in open_rows.items():
        if fingerprint not in desired:
            row.state = AttentionState.RESOLVED.value
            row.resolved_at = now
            row.last_changed_at = now
            stats["resolved"] += 1
    await db.flush()
    return stats
