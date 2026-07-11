"""Sandbox workspace → live workspace.

The operator has run a blueprint in simulate mode for a while, looked
at the chat / activity / projected cost, and wants to flip the switch.
Promote does that atomically:

  1. Preflight — every ``required: true`` channel + browser session
     in the original blueprint must now be paired / captured in the
     real entity. If not, return the unmet requirements without
     touching the workspace.
  2. Flip — ``settings.sandbox=false``; restore ``kind`` from the
     blueprint metadata; clear the ``[SIM] `` name prefix.
  3. Annotate — append a promotion event to ``settings._blueprint``
     so the audit trail shows when sandbox→live happened.

What we DON'T do:

  * Reset / clear the simulation history. The trial-run plans + chat
    are kept as evidence of what the operator decided to ship on.
  * Change governance. The operator was already free to edit it during
    simulation.
  * Touch budget. ``monthly_spent_usd`` resets on the calendar boundary
    via the existing monthly-reset job; promoting mid-month inherits
    the simulated spend (which under sandbox should have been near-zero
    anyway).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.document import Channel
from packages.core.models.integration_session import IntegrationSession
from packages.core.models.workspace import Workspace

logger = logging.getLogger(__name__)


class PromoteError(Exception):
    """Raised when a workspace can't be promoted."""


@dataclass
class UnmetRequirement:
    kind: str  # 'channel' | 'browser_session'
    detail: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromoteResult:
    workspace_id: str
    promoted: bool
    """False if preflight failed — workspace is unchanged."""
    unmet: list[UnmetRequirement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ── Preflight ────────────────────────────────────────────────────────

async def preflight_promote(
    db: AsyncSession, workspace_id: str,
) -> list[UnmetRequirement]:
    """Return the list of requirements still unmet for this workspace.
    Empty list = ready to promote. Read-only — does not mutate state."""
    ws = await _load(db, workspace_id)
    bp_meta = (ws.settings or {}).get("_blueprint") or {}

    unmet: list[UnmetRequirement] = []

    for req in bp_meta.get("channel_requirements") or []:
        if not req.get("required", True):
            continue
        if not await _channel_present(db, ws.entity_id, req):
            unmet.append(UnmetRequirement(
                kind="channel",
                detail=(
                    f"no active {req.get('channel_type')!r} channel"
                    + (f" for {req.get('purpose')}" if req.get("purpose") else "")
                ),
                payload=dict(req),
            ))

    for req in bp_meta.get("session_requirements") or []:
        if not req.get("required", True):
            continue
        if not await _session_present(db, ws.entity_id, req):
            unmet.append(UnmetRequirement(
                kind="browser_session",
                detail=(
                    f"no active {req.get('provider')!r} browser session"
                    + (f" labelled {req.get('label')!r}" if req.get("label") else "")
                ),
                payload=dict(req),
            ))

    return unmet


# ── Promote ──────────────────────────────────────────────────────────

async def promote_workspace(
    db: AsyncSession, workspace_id: str,
    *,
    user_id: Optional[str] = None,
    force: bool = False,
) -> PromoteResult:
    """Flip a sandboxed workspace into live mode. Caller commits.

    ``force=True`` skips the preflight — useful for testing or for
    operators who deliberately want to ship with unpaired channels.
    Default is to refuse + return the todo list.
    """
    ws = await _load(db, workspace_id)
    settings = dict(ws.settings or {})

    if not settings.get("sandbox"):
        raise PromoteError(
            f"workspace {workspace_id!r} is not in sandbox mode "
            f"(settings.sandbox is not true)"
        )

    if not force:
        unmet = await preflight_promote(db, workspace_id)
        if unmet:
            return PromoteResult(
                workspace_id=workspace_id,
                promoted=False,
                unmet=unmet,
                notes=[
                    "Preflight failed — pair the listed channels / capture "
                    "the listed sessions, then retry.",
                ],
            )

    # ── Flip ──
    bp_meta = dict(settings.get("_blueprint") or {})
    settings["sandbox"] = False
    if bp_meta.get("original_kind"):
        ws.kind = bp_meta["original_kind"]

    # Strip the [SIM] prefix the installer added.
    if ws.name and ws.name.startswith("[SIM] "):
        ws.name = ws.name[len("[SIM] "):]

    # Annotate the audit trail.
    promotions = list(bp_meta.get("promotions") or [])
    promotions.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "by": user_id,
        "force": bool(force),
    })
    bp_meta["promotions"] = promotions
    bp_meta["promoted_at"] = promotions[-1]["at"]
    settings["_blueprint"] = bp_meta

    ws.settings = settings
    await db.flush()

    return PromoteResult(
        workspace_id=workspace_id,
        promoted=True,
        notes=[
            "Sandbox → live. Real plans will run on the next scheduler tick.",
            "Past simulation history is preserved on this workspace as audit.",
        ],
    )


# ── Internals ────────────────────────────────────────────────────────

async def _load(db: AsyncSession, workspace_id: str) -> Workspace:
    ws = (await db.execute(
        select(Workspace).where(Workspace.id == workspace_id)
    )).scalar_one_or_none()
    if ws is None:
        raise PromoteError(f"workspace {workspace_id!r} not found")
    return ws


async def _channel_present(
    db: AsyncSession, entity_id: str, req: dict[str, Any],
) -> bool:
    channel_type = req.get("channel_type")
    if not channel_type:
        return True  # malformed requirement — don't block on it
    row = (await db.execute(
        select(Channel).where(
            Channel.entity_id == entity_id,
            Channel.type == channel_type,
            Channel.status == "active",
        ).limit(1)
    )).scalar_one_or_none()
    return row is not None


async def _session_present(
    db: AsyncSession, entity_id: str, req: dict[str, Any],
) -> bool:
    provider = req.get("provider")
    label = req.get("label")
    if not provider:
        return True
    stmt = select(IntegrationSession).where(
        IntegrationSession.entity_id == entity_id,
        IntegrationSession.provider == provider,
        IntegrationSession.status == "active",
    )
    if label is not None:
        stmt = stmt.where(IntegrationSession.label == label)
    row = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    return row is not None
