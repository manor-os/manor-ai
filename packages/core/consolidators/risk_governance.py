"""RiskGovernanceConsolidator — M4.7 (`domain=risk_governance`, critical).

L0 digest of the governance surface: open approvals + window approval
decision counts, standing grants from the current GovernancePolicy, and
recurring policy blocks (same action_key denied >=2 times — a fact, not
a suggestion to change policy).

"Governance surface" is meant literally. Open ``HitlRequest`` rows whose
``hitl_type`` is not a governance type — a login wall, a missing field, a
failed step offering a retry — are excluded from every number here: they are
not policy state, and counting them made a workspace look policy-bound when
it was merely stuck. They are not hidden either; ``non_governance_hitl_open``
records how many were left out, so a reader sees the filter instead of
wondering why the count moved. What those rows actually ARE is reported by
the ``human_participation`` domain, which breaks them out by type.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import ApprovalStatus, is_governance_hitl
from packages.core.consolidators.base import SnapshotContext, age_hours, evidence_ids
from packages.core.consolidators.contract import (
    ConsolidationReportModel,
    Coverage,
    Observation,
)
from packages.core.ledger import event_types as et
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.governance import GovernancePolicy

POLICY_BLOCK_RECURRING_THRESHOLD = 2

_APPROVAL_EVENTS = (
    et.APPROVAL_REQUESTED,
    et.APPROVAL_GRANTED,
    et.APPROVAL_DENIED,
    et.APPROVAL_CONSUMED,
    et.APPROVAL_EXPIRED,
)


class RiskGovernanceConsolidator:
    domain = "risk_governance"
    # v2 — open approvals are filtered to GOVERNANCE_HITL_TYPES. The bump is
    # load-bearing: analyzer_version is part of the cache input_hash, so
    # without it a report computed under the old (count-everything) semantics
    # would be served as if it were current.
    analyzer_version = "risk_governance-consolidator-v2"
    critical = True

    async def run(self, db: AsyncSession, ctx: SnapshotContext) -> ConsolidationReportModel:
        ws_id = ctx.review.workspace_id
        approval_events = ctx.events_of(*_APPROVAL_EVENTS)
        policy_events = ctx.events_of(et.POLICY_CHANGED)

        open_hitl_requests = list((await db.execute(
            select(HitlRequest).where(
                HitlRequest.workspace_id == ws_id,
                HitlRequest.status == ApprovalStatus.PENDING,
            )
        )).scalars().all())
        # Governance only — see the module docstring. Filtered here rather
        # than in the query so the excluded count is available to report;
        # silently shrinking a number is how a filter becomes a mystery.
        open_approvals = [
            request for request in open_hitl_requests
            if is_governance_hitl(request.hitl_type)
        ]
        non_governance_open = len(open_hitl_requests) - len(open_approvals)
        ages = [
            age for age in (
                age_hours(request.created_at, ctx.now) for request in open_approvals
            ) if age is not None
        ]

        granted = [e for e in approval_events if e.event_type == et.APPROVAL_GRANTED]
        denied = [e for e in approval_events if e.event_type == et.APPROVAL_DENIED]
        consumed = [e for e in approval_events if e.event_type == et.APPROVAL_CONSUMED]

        policy_row = (await db.execute(
            select(GovernancePolicy).where(GovernancePolicy.workspace_id == ws_id)
        )).scalar_one_or_none()
        standing_grants = list(
            ((policy_row.policy or {}).get("auto_approve_actions") or [])
            if policy_row is not None else []
        )

        observations: list[Observation] = []

        # policy_block_recurring — same action_key denied repeatedly.
        # action_key is read from the event payload first; missing payloads
        # fall back to the HitlRequest row (source_id is the request id).
        denied_by_action: dict[str, list] = {}
        unresolved_denied = []
        for event in denied:
            action_key = (event.payload or {}).get("action_key")
            if action_key:
                denied_by_action.setdefault(action_key, []).append(event)
            else:
                unresolved_denied.append(event)
        if unresolved_denied:
            request_ids = {e.source_id for e in unresolved_denied}
            rows = (await db.execute(
                select(HitlRequest.id, HitlRequest.action_key).where(
                    HitlRequest.id.in_(request_ids)
                )
            )).all()
            action_by_request = {row.id: row.action_key for row in rows}
            for event in unresolved_denied:
                action_key = action_by_request.get(event.source_id)
                if action_key:
                    denied_by_action.setdefault(action_key, []).append(event)
        for action_key, action_events in sorted(denied_by_action.items()):
            if len(action_events) >= POLICY_BLOCK_RECURRING_THRESHOLD:
                observations.append(Observation(
                    type="policy_block_recurring",
                    description=(
                        f"action {action_key!r} was denied {len(action_events)} "
                        f"times this window"
                    ),
                    evidence_refs=evidence_ids(action_events),
                ))

        if policy_events:
            observations.append(Observation(
                type="standing_grant_widening",
                description=(
                    f"governance policy changed {len(policy_events)} time(s) this "
                    f"window; {len(standing_grants)} standing grant(s) currently active"
                ),
                evidence_refs=evidence_ids(policy_events),
            ))

        metrics = {
            "open_approvals": len(open_approvals),
            "oldest_open_approval_age_hours": max(ages, default=None),
            # Open HITL rows this domain deliberately does not govern. Reported
            # so the filter is visible; the human_participation domain is where
            # they are actually described.
            "non_governance_hitl_open": non_governance_open,
            "granted_count": len(granted),
            "denied_count": len(denied),
            "consumed_count": len(consumed),
            "standing_grants_active": standing_grants,
            "standing_grants_count": len(standing_grants),
        }
        return ConsolidationReportModel(
            domain=self.domain,
            status="complete",
            summary=(
                f"{len(open_approvals)} open approval(s); {len(granted)} granted, "
                f"{len(denied)} denied this window; "
                f"{len(standing_grants)} standing grant(s)"
                + (
                    f" ({non_governance_open} non-governance HITL request(s) "
                    f"excluded)" if non_governance_open else ""
                )
            ),
            metrics=metrics,
            observations=observations,
            evidence_refs=evidence_ids(approval_events + policy_events),
            coverage=Coverage(
                records_examined=(
                    len(approval_events) + len(policy_events)
                    + len(open_hitl_requests)
                    + (1 if policy_row is not None else 0)
                ),
                sources={
                    "approval_events": len(approval_events),
                    "policy_events": len(policy_events),
                    "open_approvals": len(open_approvals),
                    "non_governance_hitl_open": non_governance_open,
                },
            ),
            analyzer_version=self.analyzer_version,
        )
