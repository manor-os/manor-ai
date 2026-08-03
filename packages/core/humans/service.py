"""Human participation service (M9).

Same-session discipline throughout: every function operates on the
caller's ``AsyncSession`` — flush only, no commits, no second
connections, no LLM/network calls. Ledger writes go through
``packages.core.ledger.service.record_event`` (idempotent, savepointed)
with ``source_kind="human"``.

Authority resolution (:func:`participant_can`, M9.1) precedence:

1. ``ParticipantProfile.authority[permission_key]`` — an explicit
   boolean wins outright (workspace-scoped profile first, then the
   entity-level default profile with ``workspace_id IS NULL``).
2. ``WorkspaceStaff.role`` default map — a known workspace role decides
   (owner → everything; editor → task + goal approvals; contributor /
   viewer → nothing).
3. Tenant role fallback — entity owner/admin → everything; else deny.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ledger import event_types as et
from packages.core.ledger.service import record_event
from packages.core.models.participant import (
    HumanCommitment,
    HumanContribution,
    ParticipantProfile,
)

logger = logging.getLogger(__name__)

# Permission vocabulary v1 (M9.1).
PERMISSION_KEYS: frozenset[str] = frozenset({
    "approve_tasks",
    "approve_automation_changes",
    "approve_external_publish",
    "approve_goal_changes",
    "manage_standing_grants",
})

# WorkspaceStaff.role → default authority map. A key missing from a
# role's map means "not granted" (the map decides — no fall-through to
# the tenant role once the user has a known workspace role).
ROLE_DEFAULT_AUTHORITY: dict[str, dict[str, bool]] = {
    "owner": {key: True for key in PERMISSION_KEYS},
    "editor": {"approve_tasks": True, "approve_goal_changes": True},
    "contributor": {},
    "viewer": {},
}

_PROFILE_PATCH_FIELDS: frozenset[str] = frozenset({
    "roles",
    "declared_capabilities",
    "authority",
    "availability",
    "capacity_preferences",
})

COMMITMENT_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "fulfilled", "declined", "expired", "cancelled",
})

_COMMITMENT_EVENT_BY_STATUS: dict[str, str] = {
    "fulfilled": et.HUMAN_COMMITMENT_FULFILLED,
    "declined": et.HUMAN_COMMITMENT_DECLINED,
    "expired": et.HUMAN_COMMITMENT_EXPIRED,
    # "cancelled" has no ledger vocabulary entry (v1) — resolved silently.
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Profiles (M9.1) ───────────────────────────────────────────────────

async def get_or_create_profile(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    workspace_id: Optional[str] = None,
) -> ParticipantProfile:
    """Fetch the profile for (entity, user, workspace) — creating an
    empty revision-1 row on first touch. ``workspace_id=None`` addresses
    the entity-level default profile."""
    profile = await _find_profile(db, entity_id, user_id, workspace_id)
    if profile is not None:
        return profile

    profile = ParticipantProfile(
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    try:
        async with db.begin_nested():
            db.add(profile)
            await db.flush()
    except IntegrityError:
        # Concurrent first-touch — someone else won the insert.
        existing = await _find_profile(db, entity_id, user_id, workspace_id)
        if existing is None:  # pragma: no cover — race resolution
            raise
        return existing
    return profile


async def _find_profile(
    db: AsyncSession,
    entity_id: str,
    user_id: str,
    workspace_id: Optional[str],
) -> Optional[ParticipantProfile]:
    query = select(ParticipantProfile).where(
        ParticipantProfile.entity_id == entity_id,
        ParticipantProfile.user_id == user_id,
    )
    if workspace_id is None:
        query = query.where(ParticipantProfile.workspace_id.is_(None))
    else:
        query = query.where(ParticipantProfile.workspace_id == workspace_id)
    return (await db.execute(query.limit(1))).scalar_one_or_none()


async def update_profile(
    db: AsyncSession,
    profile: ParticipantProfile,
    *,
    patch: dict[str, Any],
    updated_by: Optional[str],
) -> ParticipantProfile:
    """Apply a whitelisted patch and bump the revision. Non-whitelisted
    keys are silently ignored (the API layer validates shapes)."""
    changed = False
    for key, value in (patch or {}).items():
        if key not in _PROFILE_PATCH_FIELDS or value is None:
            continue
        setattr(profile, key, value)
        changed = True
    if changed:
        profile.revision = int(profile.revision or 1) + 1
        profile.updated_by = updated_by
        await db.flush()
    return profile


# ── Authority (M9.1) ──────────────────────────────────────────────────

async def participant_can(
    db: AsyncSession,
    *,
    user: Any,
    entity_id: str,
    workspace_id: str,
    permission_key: str,
) -> bool:
    """Can this user exercise ``permission_key`` in the workspace?

    See module docstring for precedence. Unknown permission keys are
    always denied.
    """
    if permission_key not in PERMISSION_KEYS:
        return False
    user_id = getattr(user, "id", None)
    if not user_id or getattr(user, "entity_id", entity_id) != entity_id:
        return False

    # 1. Explicit profile authority (workspace profile, then entity default).
    for scope in (workspace_id, None):
        profile = await _find_profile(db, entity_id, user_id, scope)
        if profile is None:
            continue
        value = (profile.authority or {}).get(permission_key)
        if isinstance(value, bool):
            return value

    # 2. Workspace role default map — a known role decides.
    from packages.core.services.workspace_access import (
        get_active_workspace_membership,
    )
    membership = await get_active_workspace_membership(
        db, workspace_id=workspace_id, user_id=user_id,
    )
    if membership is not None:
        role = str(membership.role or "").strip().lower()
        if role in ROLE_DEFAULT_AUTHORITY:
            return bool(ROLE_DEFAULT_AUTHORITY[role].get(permission_key, False))

    # 3. Tenant role fallback — entity owner/admin can do everything.
    return str(getattr(user, "role", "") or "").strip().lower() in ("owner", "admin")


# ── Commitments (M9.2) ────────────────────────────────────────────────

async def open_commitment(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    request_kind: str,
    source_kind: str,
    source_id: str,
    expected_input: Optional[str] = None,
    participant_id: Optional[str] = None,
    role_required: Optional[str] = None,
    expected_by: Optional[datetime] = None,
    blocking_execution_ids: Optional[list[str]] = None,
    causation_id: Optional[str] = None,
) -> HumanCommitment:
    """Open a commitment (idempotent per open source): if a ``waiting``
    commitment already exists for the same (workspace, source_kind,
    source_id, request_kind) it is returned instead of duplicated —
    executor cycles may re-visit a waiting step."""
    existing = (await db.execute(
        select(HumanCommitment).where(
            HumanCommitment.workspace_id == workspace_id,
            HumanCommitment.source_kind == source_kind,
            HumanCommitment.source_id == source_id,
            HumanCommitment.request_kind == request_kind,
            HumanCommitment.status == "waiting",
        ).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return existing

    commitment = HumanCommitment(
        entity_id=entity_id,
        workspace_id=workspace_id,
        request_kind=request_kind,
        participant_id=participant_id,
        role_required=role_required,
        source_kind=source_kind,
        source_id=source_id,
        expected_input=expected_input,
        expected_by=expected_by,
        blocking_execution_ids=list(blocking_execution_ids or []) or None,
    )
    db.add(commitment)
    await db.flush()

    await record_event(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        event_type=et.HUMAN_COMMITMENT_OPENED,
        source_kind="human",
        source_id=commitment.id,
        idempotency_key=f"hc:{commitment.id}:opened",
        status="waiting",
        causation_id=causation_id,
        payload={
            "request_kind": request_kind,
            "commitment_source_kind": source_kind,
            "commitment_source_id": source_id,
            "blocking_execution_ids": list(blocking_execution_ids or []),
        },
    )
    return commitment


async def resolve_commitment(
    db: AsyncSession,
    commitment: HumanCommitment,
    *,
    status: str,
    response: Optional[dict] = None,
    participant_id: Optional[str] = None,
) -> HumanCommitment:
    """Move a commitment to a terminal status and emit the matching
    ledger event (``cancelled`` resolves silently — no vocabulary
    entry in v1)."""
    if status not in COMMITMENT_TERMINAL_STATUSES:
        raise ValueError(f"invalid terminal commitment status: {status!r}")
    commitment.status = status
    if response is not None:
        commitment.response = response
    if participant_id is not None:
        commitment.participant_id = participant_id
    if status == "fulfilled":
        commitment.fulfilled_at = _utcnow()
    await db.flush()

    event_type = _COMMITMENT_EVENT_BY_STATUS.get(status)
    if event_type:
        await record_event(
            db,
            entity_id=commitment.entity_id,
            workspace_id=commitment.workspace_id,
            event_type=event_type,
            source_kind="human",
            source_id=commitment.id,
            idempotency_key=f"hc:{commitment.id}:{status}",
            status=status,
            actor_kind="user" if participant_id else "system",
            actor_id=participant_id,
            causation_id=(
                commitment.source_id
                if commitment.source_kind == "proposal_item"
                else None
            ),
            payload={
                "request_kind": commitment.request_kind,
                "commitment_source_kind": commitment.source_kind,
                "commitment_source_id": commitment.source_id,
            },
        )

    # M10: a human_request proposal item's lifecycle is driven by its
    # commitment — fulfilment succeeds the item, decline/expiry cancels
    # it. Local import keeps humans ⟂ proposals import-light.
    if commitment.source_kind == "proposal_item":
        await _sync_proposal_item_from_commitment(db, commitment, status)
    return commitment


async def _sync_proposal_item_from_commitment(
    db: AsyncSession,
    commitment: HumanCommitment,
    status: str,
) -> None:
    """Flip the source ProposalItemRecord to its terminal status when
    its commitment resolves (fulfilled → succeeded; declined/expired →
    cancelled). Silent no-op when the item is missing or already
    terminal."""
    from packages.core.models.proposal import ProposalItemRecord  # local: avoid cycle

    item = await db.get(ProposalItemRecord, commitment.source_id)
    if item is None or item.status not in ("approved", "executing"):
        return
    if status == "fulfilled":
        item.status = "succeeded"
    elif status in ("declined", "expired"):
        item.status = "cancelled"
    else:  # "cancelled" — commitment withdrawn, mirror it.
        item.status = "cancelled"
    item.finished_at = _utcnow()
    await db.flush()


async def resolve_commitments_for_step(
    db: AsyncSession,
    step_id: str,
    response: Optional[dict] = None,
    *,
    participant_id: Optional[str] = None,
) -> int:
    """Fulfil any open commitments raised for an execution step —
    silent no-op when there are none. Returns the number fulfilled."""
    rows = list((await db.execute(
        select(HumanCommitment).where(
            HumanCommitment.source_kind == "execution_step",
            HumanCommitment.source_id == str(step_id),
            HumanCommitment.status == "waiting",
        )
    )).scalars().all())
    for row in rows:
        await resolve_commitment(
            db, row,
            status="fulfilled",
            response=response,
            participant_id=participant_id,
        )
    return len(rows)


async def list_open_commitments(
    db: AsyncSession,
    workspace_id: str,
) -> list[HumanCommitment]:
    """Open (waiting) commitments — blocking first, then expected_by
    ascending (nulls last), then requested_at."""
    rows = list((await db.execute(
        select(HumanCommitment).where(
            HumanCommitment.workspace_id == workspace_id,
            HumanCommitment.status == "waiting",
        )
    )).scalars().all())
    far_future = datetime.max.replace(tzinfo=timezone.utc)

    def _key(row: HumanCommitment):
        blocking = bool(row.blocking_execution_ids)
        expected = row.expected_by
        if expected is not None and expected.tzinfo is None:
            expected = expected.replace(tzinfo=timezone.utc)
        return (
            0 if blocking else 1,
            expected or far_future,
            row.requested_at or far_future,
        )

    return sorted(rows, key=_key)


# ── Contributions (M9.4) ──────────────────────────────────────────────

async def record_contribution(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    participant_id: str,
    kind: str,
    target_kind: str,
    target_id: str,
    diff_summary: Optional[dict] = None,
) -> HumanContribution:
    """Record that a human contributed work + emit the ledger event.

    ``diff_summary`` must stay structural (field names / size deltas) —
    never full old/new values (M9.6 privacy boundary)."""
    contribution = HumanContribution(
        entity_id=entity_id,
        workspace_id=workspace_id,
        participant_id=participant_id,
        kind=kind,
        target_kind=target_kind,
        target_id=str(target_id),
        diff_summary=diff_summary,
    )
    db.add(contribution)
    await db.flush()

    await record_event(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        event_type=et.HUMAN_CONTRIBUTION_RECORDED,
        source_kind="human",
        source_id=contribution.id,
        idempotency_key=f"hcontrib:{contribution.id}",
        actor_kind="user",
        actor_id=participant_id,
        payload={
            "kind": kind,
            "target_kind": target_kind,
            "target_id": str(target_id),
        },
    )
    return contribution
