"""Human participation endpoints (M9.5 / M14) — work queue + profile + view.

Endpoints (all under ``/api/v1/workspaces/{workspace_id}``):

  GET /human-queue        merged view of what needs a human: open
                          commitments (blocking first, then expected_by),
                          open HITL requests — split into real ``approvals``
                          and ``information_requests`` (input / choice /
                          error) so a queue count cannot call a CAPTCHA wall
                          an aging approval — and open tasks assigned to the
                          current user.
  GET /human-participation
                          M14 "Human Participation" view — the whole
                          workspace's waiting queue, what it blocks, who
                          the declared participants are, recent human
                          decisions, and recent contributions.
                          M9.6 privacy boundary: counts and facts only —
                          never a per-person latency / response-time /
                          efficiency metric, and contributions expose
                          changed FIELD NAMES only (never values).
  GET /participants/me    own participant profile for this workspace
                          (created empty on first read).
  PUT /participants/me    whitelisted patch (roles / declared_capabilities /
                          authority / availability / capacity_preferences);
                          bumps the profile revision.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import get_current_user
from packages.core.constants.approvals import ApprovalStatus, is_governance_hitl
from packages.core.database import get_db
from packages.core.humans import (
    get_or_create_profile,
    list_open_commitments,
    participant_can,
    resolve_commitment,
    update_profile,
)
from packages.core.ledger import event_types as et
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.participant import (
    HumanCommitment,
    HumanContribution,
    ParticipantProfile,
)
from packages.core.models.proposal import ProposalItemRecord
from packages.core.models.task import Task
from packages.core.models.user import User
from packages.core.models.workspace import WorkspaceStaff
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.services.entity_service import get_workspace
from packages.core.services.task_state_machine import TERMINAL_STATUSES

# Rolling windows for the M14 view — both capped so the card stays light.
_RECENT_CONTRIBUTIONS_LIMIT = 20
_RECENT_DECISIONS_LIMIT = 20

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}",
    tags=["humans"],
)


# ── Models ────────────────────────────────────────────────────────────

class CommitmentDigest(BaseModel):
    id: str
    request_kind: str
    source_kind: str
    source_id: str
    expected_input: Optional[str] = None
    status: str
    requested_at: Optional[datetime] = None
    expected_by: Optional[datetime] = None
    blocking: bool = False
    blocking_execution_ids: list[str] = Field(default_factory=list)


class ApprovalDigest(BaseModel):
    """One open HITL request. ``hitl_type`` says what is being asked."""

    id: str
    action_key: Optional[str] = None
    capability_id: Optional[str] = None
    risk_level: str
    origin_kind: str
    #: ``authorize`` / ``review`` / ``input`` / ``choice`` / ``error``. Carried
    #: so a renderer never has to guess the ask from the prose reason — and so
    #: the split between the two lists below stays checkable client-side.
    hitl_type: str
    #: What tripped the pause (a policy rule name, ``step.*``, ``lease.*``).
    matched_rule: Optional[str] = None
    reason: Optional[str] = None
    created_at: Optional[datetime] = None


class AssignedTaskDigest(BaseModel):
    id: str
    title: str
    status: str
    priority: Optional[int] = None
    deadline: Optional[datetime] = None


class HumanQueueResponse(BaseModel):
    """The operator's queue. Two HITL lists, not one.

    ``approvals`` holds only what the name claims: requests where a person is
    granting permission or signing off on content (``GOVERNANCE_HITL_TYPES``).
    Everything else the run is stuck on — a login wall, a missing field, a
    failed step with a fix — lands in ``information_requests``. Nothing is
    dropped: both lists are returned and both are the operator's work.

    They are split rather than merged-and-tagged because a single list called
    ``approvals`` is what a renderer counts. "3 approvals pending" when one of
    them is "reconnect your worker" is the misreading this endpoint existed to
    produce, and a tag on each row does not prevent it — a separate key does.
    """

    commitments: list[CommitmentDigest] = Field(default_factory=list)
    approvals: list[ApprovalDigest] = Field(default_factory=list)
    #: Open HITL requests that are NOT approvals: ``input`` / ``choice`` /
    #: ``error``. The run needs something from you — an answer, a choice, or
    #: a fix — but you are not authorizing anything.
    information_requests: list[ApprovalDigest] = Field(default_factory=list)
    assigned_tasks: list[AssignedTaskDigest] = Field(default_factory=list)


class QueueCommitmentDigest(CommitmentDigest):
    """Workspace-wide queue entry — adds who it is addressed to and how
    long it has been waiting. Deliberately NOT per-person: ``age_hours``
    is the age of the REQUEST, never of a person's response."""

    participant_id: Optional[str] = None
    role_required: Optional[str] = None
    age_hours: Optional[float] = None


class BlockedExecution(BaseModel):
    id: str
    kind: str = "execution"   # "task" once the id resolves to a Task row
    title: Optional[str] = None
    status: Optional[str] = None


class BlockingEntry(BaseModel):
    commitment_id: str
    request_kind: str
    expected_by: Optional[datetime] = None
    execution_ids: list[str] = Field(default_factory=list)
    blocked: list[BlockedExecution] = Field(default_factory=list)


class ParticipantAvailability(BaseModel):
    timezone: Optional[str] = None
    out_of_office: bool = False


class ParticipantDigest(BaseModel):
    """Declared facts about a participant + one open-work COUNT.

    M9.6 red line: this model must never gain a timing, latency,
    throughput, or scoring field — see
    ``packages.core.consolidators.contract.FORBIDDEN_HUMAN_METRICS``.
    """

    user_id: str
    display_name: Optional[str] = None
    roles: list[Any] = Field(default_factory=list)
    declared_capabilities: list[Any] = Field(default_factory=list)
    availability: ParticipantAvailability = Field(
        default_factory=ParticipantAvailability,
    )
    open_commitments_count: int = 0


class ContributionDigest(BaseModel):
    """M9.4 contribution fact — field NAMES only, never values/diffs."""

    kind: str
    target_kind: str
    target_id: str
    fields_changed: list[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None


class DecisionDigest(BaseModel):
    item_id: str
    kind: Optional[str] = None
    decision: str
    reason_code: Optional[str] = None
    decided_at: Optional[datetime] = None


class HumanParticipationResponse(BaseModel):
    queue: list[QueueCommitmentDigest] = Field(default_factory=list)
    blocking: list[BlockingEntry] = Field(default_factory=list)
    participants: list[ParticipantDigest] = Field(default_factory=list)
    recent_contributions: list[ContributionDigest] = Field(default_factory=list)
    decisions: list[DecisionDigest] = Field(default_factory=list)


class ProfileResponse(BaseModel):
    id: str
    entity_id: str
    user_id: str
    workspace_id: Optional[str] = None
    roles: list[Any] = Field(default_factory=list)
    declared_capabilities: list[Any] = Field(default_factory=list)
    authority: dict[str, Any] = Field(default_factory=dict)
    availability: dict[str, Any] = Field(default_factory=dict)
    capacity_preferences: dict[str, Any] = Field(default_factory=dict)
    revision: int
    updated_by: Optional[str] = None
    updated_at: Optional[datetime] = None


class CommitmentRespondRequest(BaseModel):
    action: Literal["fulfill", "decline"]
    response: Optional[str] = Field(default=None, max_length=4000)


class ProfilePatchRequest(BaseModel):
    """Whitelisted patch — omitted fields keep their current value."""

    roles: Optional[list[Any]] = None
    declared_capabilities: Optional[list[Any]] = None
    authority: Optional[dict[str, bool]] = None
    availability: Optional[dict[str, Any]] = None
    capacity_preferences: Optional[dict[str, Any]] = None


# ── Helpers ───────────────────────────────────────────────────────────

async def _require_workspace(db: AsyncSession, workspace_id: str, entity_id: str):
    ws = await get_workspace(db, workspace_id, entity_id)
    if not ws:
        raise HTTPException(404, "Workspace not found")
    return ws


def _approval_digest(request: HitlRequest) -> ApprovalDigest:
    """One open HITL row → its wire digest. Which LIST it goes in is decided
    by ``is_governance_hitl``; this only renders."""
    return ApprovalDigest(
        id=request.id,
        action_key=request.action_key,
        capability_id=request.capability_id,
        risk_level=request.risk_level,
        origin_kind=request.origin_kind,
        hitl_type=request.hitl_type,
        matched_rule=request.matched_rule,
        reason=request.reason,
        created_at=request.created_at,
    )


def _profile_response(profile) -> ProfileResponse:
    return ProfileResponse(
        id=profile.id,
        entity_id=profile.entity_id,
        user_id=profile.user_id,
        workspace_id=profile.workspace_id,
        roles=list(profile.roles or []),
        declared_capabilities=list(profile.declared_capabilities or []),
        authority=dict(profile.authority or {}),
        availability=dict(profile.availability or {}),
        capacity_preferences=dict(profile.capacity_preferences or {}),
        revision=profile.revision,
        updated_by=profile.updated_by,
        updated_at=profile.updated_at,
    )


# ── Endpoints ─────────────────────────────────────────────────────────

@router.get("/human-queue", response_model=HumanQueueResponse)
async def get_human_queue(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """M9.5 work queue — open commitments (blocking first, then expected_by
    ascending), open HITL requests, and open tasks assigned to the current
    user.

    The HITL rows are split by ``is_governance_hitl``: real approvals in
    ``approvals``, everything the run is merely stuck on in
    ``information_requests``. Same query, same ordering — one pass, two
    buckets."""
    await _require_workspace(db, workspace_id, user.entity_id)

    commitments = await list_open_commitments(db, workspace_id)

    approvals = list((await db.execute(
        select(HitlRequest).where(
            HitlRequest.entity_id == user.entity_id,
            HitlRequest.workspace_id == workspace_id,
            HitlRequest.status == ApprovalStatus.PENDING,
        ).order_by(HitlRequest.created_at.asc())
    )).scalars().all())

    assigned = list((await db.execute(
        select(Task).where(
            Task.entity_id == user.entity_id,
            Task.workspace_id == workspace_id,
            Task.assignee_id == user.id,
            Task.status.notin_(tuple(TERMINAL_STATUSES)),
        ).order_by(Task.created_at.asc())
    )).scalars().all())

    return HumanQueueResponse(
        commitments=[
            CommitmentDigest(
                id=c.id,
                request_kind=c.request_kind,
                source_kind=c.source_kind,
                source_id=c.source_id,
                expected_input=c.expected_input,
                status=c.status,
                requested_at=c.requested_at,
                expected_by=c.expected_by,
                blocking=bool(c.blocking_execution_ids),
                blocking_execution_ids=[
                    str(x) for x in (c.blocking_execution_ids or [])
                ],
            )
            for c in commitments
        ],
        approvals=[
            _approval_digest(a) for a in approvals if is_governance_hitl(a.hitl_type)
        ],
        information_requests=[
            _approval_digest(a)
            for a in approvals if not is_governance_hitl(a.hitl_type)
        ],
        assigned_tasks=[
            AssignedTaskDigest(
                id=t.id,
                title=t.title,
                status=t.status,
                priority=t.priority,
                deadline=t.deadline,
            )
            for t in assigned
        ],
    )


@router.get("/human-participation", response_model=HumanParticipationResponse)
async def get_human_participation(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """M14 Human Participation view — the whole workspace, not just me.

    Five read-only sections over existing facts: the waiting queue
    (blocking first, then expected_by asc), what those blocking requests
    hold up, the declared participants, the recent proposal-item
    decisions, and the recent human contributions.

    Privacy (M9.6 / invariant 8): participants carry declared facts plus
    a single open-work COUNT — no response time, no latency, no
    efficiency/performance score, no per-person ranking of any kind.
    Contributions carry the NAMES of the fields a human changed, never
    the old/new values.
    """
    await _require_workspace(db, workspace_id, user.entity_id)
    now = datetime.now(timezone.utc)

    # ── queue (whole workspace) ───────────────────────────────────
    commitments = await list_open_commitments(db, workspace_id)

    def _age_hours(value: Optional[datetime]) -> Optional[float]:
        if value is None:
            return None
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return round(max((now - moment).total_seconds() / 3600.0, 0.0), 2)

    queue = [
        QueueCommitmentDigest(
            id=c.id,
            request_kind=c.request_kind,
            participant_id=c.participant_id,
            role_required=c.role_required,
            source_kind=c.source_kind,
            source_id=c.source_id,
            expected_input=c.expected_input,
            status=c.status,
            requested_at=c.requested_at,
            expected_by=c.expected_by,
            blocking=bool(c.blocking_execution_ids),
            blocking_execution_ids=[
                str(x) for x in (c.blocking_execution_ids or [])
            ],
            age_hours=_age_hours(c.requested_at),
        )
        for c in commitments
    ]

    # ── blocking → what it holds up (one batched Task join) ───────
    blocked_ids: list[str] = []
    for c in commitments:
        for raw in c.blocking_execution_ids or []:
            value = str(raw)
            if value and value not in blocked_ids:
                blocked_ids.append(value)
    blocked_tasks: dict[str, Task] = {}
    if blocked_ids:
        blocked_tasks = {
            row.id: row
            for row in (await db.execute(
                select(Task).where(
                    Task.workspace_id == workspace_id,
                    Task.entity_id == user.entity_id,
                    Task.id.in_(blocked_ids),
                )
            )).scalars().all()
        }
    blocking = [
        BlockingEntry(
            commitment_id=c.id,
            request_kind=c.request_kind,
            expected_by=c.expected_by,
            execution_ids=[str(x) for x in (c.blocking_execution_ids or [])],
            blocked=[
                BlockedExecution(
                    id=str(x),
                    kind="task" if str(x) in blocked_tasks else "execution",
                    title=getattr(blocked_tasks.get(str(x)), "title", None),
                    status=getattr(blocked_tasks.get(str(x)), "status", None),
                )
                for x in (c.blocking_execution_ids or [])
            ],
        )
        for c in commitments
        if c.blocking_execution_ids
    ]

    # ── participants (profiles ∪ staff rows) ──────────────────────
    profiles = list((await db.execute(
        select(ParticipantProfile).where(
            ParticipantProfile.entity_id == user.entity_id,
            ParticipantProfile.workspace_id == workspace_id,
        )
    )).scalars().all())
    staff_rows = list((await db.execute(
        select(WorkspaceStaff).where(
            WorkspaceStaff.workspace_id == workspace_id,
            WorkspaceStaff.status == "active",
            WorkspaceStaff.user_id.isnot(None),
        )
    )).scalars().all())

    profile_by_user = {p.user_id: p for p in profiles}
    staff_by_user = {s.user_id: s for s in staff_rows if s.user_id}
    member_ids = sorted(set(profile_by_user) | set(staff_by_user))

    users_by_id: dict[str, User] = {}
    open_counts: dict[str, int] = {}
    if member_ids:
        users_by_id = {
            row.id: row
            for row in (await db.execute(
                select(User).where(User.id.in_(member_ids))
            )).scalars().all()
        }
        # Counts only — never a duration, never a ranking.
        for participant_id, count in (await db.execute(
            select(HumanCommitment.participant_id, func.count()).where(
                HumanCommitment.workspace_id == workspace_id,
                HumanCommitment.status == "waiting",
                HumanCommitment.participant_id.in_(member_ids),
            ).group_by(HumanCommitment.participant_id)
        )).all():
            open_counts[participant_id] = int(count)

    participants: list[ParticipantDigest] = []
    for user_id in member_ids:
        profile = profile_by_user.get(user_id)
        staff = staff_by_user.get(user_id)
        member = users_by_id.get(user_id)
        availability = dict((profile.availability if profile else None) or {})
        roles = list((profile.roles if profile else None) or [])
        if not roles and staff is not None and staff.role:
            roles = [staff.role]
        participants.append(ParticipantDigest(
            user_id=user_id,
            display_name=(
                getattr(member, "display_name", None)
                or getattr(member, "email", None)
            ),
            roles=roles,
            declared_capabilities=list(
                (profile.declared_capabilities if profile else None) or []
            ),
            availability=ParticipantAvailability(
                timezone=availability.get("timezone"),
                out_of_office=bool(availability.get("out_of_office")),
            ),
            open_commitments_count=open_counts.get(user_id, 0),
        ))
    participants.sort(key=lambda p: ((p.display_name or "").lower(), p.user_id))

    # ── recent contributions (field names only) ───────────────────
    contributions = list((await db.execute(
        select(HumanContribution).where(
            HumanContribution.workspace_id == workspace_id,
            HumanContribution.entity_id == user.entity_id,
        ).order_by(
            HumanContribution.created_at.desc(), HumanContribution.id.desc(),
        ).limit(_RECENT_CONTRIBUTIONS_LIMIT)
    )).scalars().all())
    recent_contributions = [
        ContributionDigest(
            kind=row.kind,
            target_kind=row.target_kind,
            target_id=row.target_id,
            fields_changed=sorted(
                str(k) for k in (row.diff_summary or {}).keys()
            ) if isinstance(row.diff_summary, dict) else [],
            created_at=row.created_at,
        )
        for row in contributions
    ]

    # ── recent proposal-item decisions ────────────────────────────
    decision_events = list((await db.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type.in_(
                (et.PROPOSAL_ITEM_APPROVED, et.PROPOSAL_ITEM_REJECTED),
            ),
        ).order_by(WorkspaceEvent.id.desc()).limit(_RECENT_DECISIONS_LIMIT)
    )).scalars().all())
    item_ids = sorted({e.source_id for e in decision_events if e.source_id})
    items_by_id: dict[str, ProposalItemRecord] = {}
    if item_ids:
        items_by_id = {
            row.id: row
            for row in (await db.execute(
                select(ProposalItemRecord).where(
                    ProposalItemRecord.workspace_id == workspace_id,
                    ProposalItemRecord.id.in_(item_ids),
                )
            )).scalars().all()
        }
    decisions: list[DecisionDigest] = []
    for event in decision_events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        item = items_by_id.get(event.source_id)
        item_decision = (item.decision if item is not None else None) or {}
        decisions.append(DecisionDigest(
            item_id=event.source_id,
            kind=(item.kind if item is not None else None) or payload.get("kind"),
            decision=(
                "approved"
                if event.event_type == et.PROPOSAL_ITEM_APPROVED
                else "rejected"
            ),
            reason_code=(
                item_decision.get("reason_code")
                or payload.get("reason_code")
                or payload.get("rejection_reason")
            ),
            decided_at=event.occurred_at,
        ))

    return HumanParticipationResponse(
        queue=queue,
        blocking=blocking,
        participants=participants,
        recent_contributions=recent_contributions,
        decisions=decisions,
    )


@router.post(
    "/human-queue/commitments/{commitment_id}/respond",
    response_model=CommitmentDigest,
)
async def respond_to_commitment(
    workspace_id: str,
    commitment_id: str,
    req: CommitmentRespondRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fulfil or decline an open commitment (M10 human_request surface).

    Permission: the commitment's named participant when set; otherwise a
    role-required commitment needs ``participant_can("approve_tasks")``;
    a role-free commitment is answerable by any workspace member.
    """
    await _require_workspace(db, workspace_id, user.entity_id)

    commitment = (await db.execute(
        select(HumanCommitment).where(
            HumanCommitment.id == commitment_id,
            HumanCommitment.workspace_id == workspace_id,
            HumanCommitment.entity_id == user.entity_id,
        ).limit(1)
    )).scalar_one_or_none()
    if commitment is None:
        raise HTTPException(404, "Commitment not found")
    if commitment.status != "waiting":
        raise HTTPException(409, f"Commitment already {commitment.status}")

    if commitment.participant_id:
        if commitment.participant_id != user.id:
            raise HTTPException(
                403, "This commitment is assigned to a specific participant",
            )
    elif commitment.role_required:
        allowed = await participant_can(
            db,
            user=user,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            permission_key="approve_tasks",
        )
        if not allowed:
            raise HTTPException(
                403,
                "Responding to this request requires approve_tasks authority "
                f"(role_required={commitment.role_required})",
            )

    commitment = await resolve_commitment(
        db,
        commitment,
        status="fulfilled" if req.action == "fulfill" else "declined",
        response={"text": req.response} if req.response else None,
        participant_id=user.id,
    )
    await db.commit()

    return CommitmentDigest(
        id=commitment.id,
        request_kind=commitment.request_kind,
        source_kind=commitment.source_kind,
        source_id=commitment.source_id,
        expected_input=commitment.expected_input,
        status=commitment.status,
        requested_at=commitment.requested_at,
        expected_by=commitment.expected_by,
        blocking=bool(commitment.blocking_execution_ids),
        blocking_execution_ids=[
            str(x) for x in (commitment.blocking_execution_ids or [])
        ],
    )


@router.get("/participants/me", response_model=ProfileResponse)
async def get_own_profile(
    workspace_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Own participant profile for this workspace — created empty on
    first read."""
    await _require_workspace(db, workspace_id, user.entity_id)
    profile = await get_or_create_profile(
        db, entity_id=user.entity_id, user_id=user.id, workspace_id=workspace_id,
    )
    response = _profile_response(profile)
    await db.commit()
    return response


@router.put("/participants/me", response_model=ProfileResponse)
async def put_own_profile(
    workspace_id: str,
    req: ProfilePatchRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Whitelisted patch of the own profile; bumps the revision."""
    await _require_workspace(db, workspace_id, user.entity_id)
    profile = await get_or_create_profile(
        db, entity_id=user.entity_id, user_id=user.id, workspace_id=workspace_id,
    )
    patch = req.model_dump(exclude_none=True)
    if patch.get("authority"):
        from packages.core.humans import PERMISSION_KEYS
        unknown = set(patch["authority"]) - PERMISSION_KEYS
        if unknown:
            raise HTTPException(
                400, f"unknown authority keys: {', '.join(sorted(unknown))}",
            )
    profile = await update_profile(
        db, profile, patch=patch, updated_by=user.id,
    )
    # updated_at is a server-side onupdate value — expired after the
    # flush; load it explicitly (implicit IO is illegal on AsyncSession).
    await db.refresh(profile)
    response = _profile_response(profile)
    await db.commit()
    return response
