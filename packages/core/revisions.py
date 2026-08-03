"""Execution-config revision bookkeeping (M11).

Every operator/system change to one of the revisioned execution-config
rows (``ScheduledJob`` / ``WorkflowBinding`` / ``WorkflowDefinition`` /
``Goal`` / ``Agent`` / ``Skill``) goes through
``bump_revision``: the integer ``revision`` column is incremented inside
the caller's transaction (so CAS checks see a consistent value), and an
append-only ``AutomationRevision`` audit row records what changed and
who caused it. The audit write is best-effort — a failure there must
never break the mutation itself.

``assert_revision`` is the optimistic-concurrency precheck used by the
M7 validator and the M10 apply path (``expected_revision`` CAS).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.automation_revision import AutomationRevision

logger = logging.getLogger(__name__)

# ORM table name → automation_revisions.target_kind discriminator.
TARGET_KIND_BY_TABLE: dict[str, str] = {
    "scheduled_jobs": "scheduled_job",
    "workflow_bindings": "workflow_binding",
    "workflow_definitions": "workflow_definition",
    "goals": "goal",
    "agents": "agent",
    "skills": "skill",
}

# Fields whose value change alters what the agent/skill actually DOES.
# Everything else on those rows is presentation (name, display_name,
# description, avatar_url, tags, category, slug, is_public, version label)
# and must never move the revision — cosmetic churn would otherwise flood
# the audit trail and invalidate config-version attribution in the ledger.
AGENT_CONTENT_REVISION_FIELDS: frozenset[str] = frozenset(
    {"system_prompt", "config", "status"}
)
SKILL_CONTENT_REVISION_FIELDS: frozenset[str] = frozenset(
    {"system_prompt", "tools", "input_schema", "output_format", "config", "status"}
)


def content_patch_for(row: Any, updates: dict, fields: frozenset[str]) -> dict:
    """Return the subset of ``updates`` that actually CHANGES a
    behavior-affecting field of ``row``.

    Same-value writes drop out (``getattr(row, key) != value``), so an
    idempotent re-install / no-op PATCH never bumps. Call this BEFORE the
    ``setattr`` loop that applies the update.
    """
    patch: dict = {}
    for key, value in updates.items():
        if key not in fields or value is None:
            continue
        if not hasattr(row, key):
            continue
        current = getattr(row, key)
        # ARRAY columns come back as list; normalize tuple/list comparison.
        if isinstance(current, (list, tuple)) and isinstance(value, (list, tuple)):
            if list(current) == list(value):
                continue
        elif current == value:
            continue
        patch[key] = value
    return patch


class StaleRevisionError(Exception):
    """The row's revision no longer matches the expected one (CAS miss)."""

    def __init__(self, *, target_kind: str, target_id: str, expected: int, actual: int):
        self.target_kind = target_kind
        self.target_id = target_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"{target_kind} {target_id}: expected revision {expected}, "
            f"row is at {actual}"
        )


def _json_safe(value: Any) -> Any:
    """Coerce a patch value into a JSONB-storable shape (Decimal / date /
    datetime → str), recursively."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def target_kind_for(row: Any) -> str:
    """Infer the audit ``target_kind`` from the ORM row's table name."""
    table_name = getattr(getattr(row, "__table__", None), "name", None)
    kind = TARGET_KIND_BY_TABLE.get(str(table_name))
    if kind is None:
        raise ValueError(
            f"row of table {table_name!r} is not revisioned; expected one of "
            f"{sorted(TARGET_KIND_BY_TABLE)}"
        )
    return kind


async def assert_revision(row: Any, expected: Optional[int]) -> None:
    """Raise ``StaleRevisionError`` when the row's revision differs from
    ``expected``. ``expected=None`` skips the check (no CAS requested)."""
    if expected is None:
        return
    actual = int(getattr(row, "revision", 1) or 1)
    if actual != int(expected):
        raise StaleRevisionError(
            target_kind=target_kind_for(row),
            target_id=str(getattr(row, "id", "?")),
            expected=int(expected),
            actual=actual,
        )


async def bump_revision(
    db: AsyncSession,
    row: Any,
    *,
    patch: dict,
    changed_by_kind: str = "system",
    changed_by_id: Optional[str] = None,
    causation_id: Optional[str] = None,
) -> int:
    """Increment ``row.revision`` and append the audit row.

    The increment is part of the caller's transaction (roll back the
    mutation → the bump rolls back with it). The audit insert is
    savepointed and best-effort: it must never fail the mutation.
    Returns the new revision.
    """
    target_kind = target_kind_for(row)
    new_revision = int(getattr(row, "revision", 1) or 1) + 1
    row.revision = new_revision
    await db.flush()

    try:
        async with db.begin_nested():
            db.add(AutomationRevision(
                entity_id=getattr(row, "entity_id", None),
                workspace_id=getattr(row, "workspace_id", None),
                target_kind=target_kind,
                target_id=str(row.id),
                revision=new_revision,
                patch=_json_safe(patch) or None,
                changed_by_kind=changed_by_kind,
                changed_by_id=changed_by_id,
                causation_id=causation_id,
            ))
            await db.flush()
    except Exception:
        logger.warning(
            "automation_revisions audit write failed for %s %s rev %s (mutation kept)",
            target_kind, row.id, new_revision, exc_info=True,
        )
    return new_revision
