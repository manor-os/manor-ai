"""Workspace event ledger — append service (M1).

``record_event`` is the single write path into ``workspace_events``.

Discipline (mirrors ``event_emitter.emit_in_session``):

* Same-session write: the caller already owns the transaction. We only
  ``add`` + ``flush`` — never commit, never open a second connection. If the
  caller rolls back, the event rolls back with the business write (that is
  the point: ledger facts are transactionally tied to what they describe).
* Idempotent: a duplicate ``(entity_id, idempotency_key)`` insert is silently
  dropped (returns ``None``). The insert runs inside a SAVEPOINT
  (``begin_nested``) so the IntegrityError does not poison the caller's
  outer transaction.
* No LLM calls, no external network, no secondary side effects on this path.
* ``payload`` is a small business summary — serialized size is capped at 8KB;
  anything larger is replaced with a truncation marker. Large objects belong
  in ``output_refs`` / ``evidence_refs``.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ledger.event_types import ALL_EVENT_TYPES
from packages.core.models.workspace_event import WorkspaceEvent

logger = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 8 * 1024
_TRUNCATED_SUMMARY_CHARS = 2000


def _bounded_payload(payload: Any) -> Any:
    """Cap serialized payload size at 8KB; replace with a marker if exceeded."""
    if payload is None:
        return None
    try:
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        serialized = str(payload)
    if len(serialized.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        return payload
    return {
        "payload_truncated": True,
        "summary": str(payload)[:_TRUNCATED_SUMMARY_CHARS],
    }


async def record_event(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    event_type: str,
    source_kind: str,
    source_id: str,
    idempotency_key: str,
    occurred_at: datetime | None = None,
    **fields: Any,
) -> WorkspaceEvent | None:
    """Append one event to the workspace ledger in the caller's transaction.

    Returns the persisted (flushed) ``WorkspaceEvent``, or ``None`` when this
    ``(entity_id, idempotency_key)`` was already recorded.

    Raises ``ValueError`` for an event_type outside the closed vocabulary
    (``ledger.event_types.ALL_EVENT_TYPES``).
    """
    if event_type not in ALL_EVENT_TYPES:
        raise ValueError(f"Unknown workspace event_type: {event_type!r}")

    if "payload" in fields:
        fields["payload"] = _bounded_payload(fields["payload"])

    event = WorkspaceEvent(
        entity_id=entity_id,
        workspace_id=workspace_id,
        event_type=event_type,
        source_kind=source_kind,
        source_id=source_id,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        **fields,
    )

    try:
        # SAVEPOINT: a duplicate-key failure rolls back only this insert,
        # leaving the caller's outer transaction usable.
        async with db.begin_nested():
            db.add(event)
            await db.flush()
    except IntegrityError:
        logger.debug(
            "workspace_events duplicate suppressed: entity=%s key=%s type=%s",
            entity_id, idempotency_key, event_type,
        )
        return None
    return event
