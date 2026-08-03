"""HumanParticipationConsolidator — M4.5 (`domain=human_participation`, critical).

L0 digest of human decisions and open blocking input: proposal item
decisions + approval decisions from the window, the open approval
backlog, plus (M9) open ``human_commitments`` and the window's
``human_contribution_recorded`` events. M4.5 privacy red line: the
contract validator rejects any per-participant performance metric key
(``FORBIDDEN_HUMAN_METRICS``); this consolidator only aggregates counts
and waiting facts — never per-person latency or throughput.

HitlRequests and HumanCommitments are counted separately by design
(a request never mirrors into a commitment — no double counting).

Open HITL requests are likewise split by ``hitl_type``: ``open_approvals``
and the ``approval_bottleneck`` observation cover governance approvals only
(``GOVERNANCE_HITL_TYPES``), while requests that ask a person for
information or a fix are counted as ``open_information_requests`` and, once
stale, surface as ``information_request_stalled``. Both are real waiting —
but a bottleneck is friction in the approval process, and an unanswered
CAPTCHA is not, so a metric that conflated them measured neither.

L1 (opt-in, ``MANOR_CONSOLIDATOR_L1``): when the window holds ≥3 human
contributions, the *field names* they touched are aggregated into
``{field, count}`` pairs and one LLM call induces a neutral label for
each recurring edit shape (``repeated_edit_pattern``). M9.6 privacy
boundary: only field names and counts ever cross that boundary — never
the edited text, never a participant identifier.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import ApprovalStatus, is_governance_hitl
from packages.core.consolidators import l1 as l1_layer
from packages.core.consolidators.base import SnapshotContext, age_hours, evidence_ids
from packages.core.consolidators.contract import (
    ConsolidationReportModel,
    Coverage,
    Observation,
)
from packages.core.ledger import event_types as et
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.participant import HumanCommitment, HumanContribution

APPROVAL_BOTTLENECK_HOURS = 24.0
#: A non-approval HITL request (login wall, missing field, failed step) that
#: has waited this long is also a real problem — just not a governance one.
#: Same threshold, different observation type: see ``run()``.
INFORMATION_STALL_HOURS = 24.0
REPEATED_EDIT_THRESHOLD = 3
MAX_EDIT_EVIDENCE_REFS = 20


class HumanParticipationConsolidator:
    domain = "human_participation"
    # v3 — adds the opt-in L1 edit-pattern layer (L0 output unchanged).
    # v4 — open HITL rows split by hitl_type: ``open_approvals`` and
    #      ``approval_bottleneck`` now mean approvals only, with
    #      ``open_information_requests`` / ``information_request_stalled``
    #      carrying the rest. The bump is load-bearing: analyzer_version is
    #      part of the cache input_hash, so without it a report computed under
    #      the old (everything-is-an-approval) semantics would be reused.
    analyzer_version = "human_participation-consolidator-v4"
    critical = True

    async def run(self, db: AsyncSession, ctx: SnapshotContext) -> ConsolidationReportModel:
        approved_events = ctx.events_of(et.PROPOSAL_ITEM_APPROVED, et.APPROVAL_GRANTED)
        rejected_events = ctx.events_of(et.PROPOSAL_ITEM_REJECTED, et.APPROVAL_DENIED)
        contribution_events = ctx.events_of(et.HUMAN_CONTRIBUTION_RECORDED)

        # Every open HITL row, then split by what it actually asks for.
        # "N open approvals" and an "approval bottleneck" must mean approvals:
        # an operator staring at a CAPTCHA is not governance friction, and
        # counting it as such is how a workspace looked policy-bound when it
        # was really just stuck. Nothing is dropped — the other rows get their
        # own count and their own observation type below.
        open_hitl_requests = list((await db.execute(
            select(HitlRequest).where(
                HitlRequest.workspace_id == ctx.review.workspace_id,
                HitlRequest.status == ApprovalStatus.PENDING,
            ).order_by(HitlRequest.created_at.asc())
        )).scalars().all())
        open_approvals = [
            request for request in open_hitl_requests
            if is_governance_hitl(request.hitl_type)
        ]
        open_information_requests = [
            request for request in open_hitl_requests
            if not is_governance_hitl(request.hitl_type)
        ]

        open_commitments = list((await db.execute(
            select(HumanCommitment).where(
                HumanCommitment.workspace_id == ctx.review.workspace_id,
                HumanCommitment.status == "waiting",
            ).order_by(HumanCommitment.requested_at.asc())
        )).scalars().all())
        blocking_commitments = [
            commitment for commitment in open_commitments
            if commitment.blocking_execution_ids
        ]

        ages = [
            age for age in (
                age_hours(request.created_at, ctx.now) for request in open_approvals
            ) if age is not None
        ]
        oldest_open_hours = max(ages, default=None)

        observations: list[Observation] = []
        for request in open_approvals:
            waited = age_hours(request.created_at, ctx.now)
            if waited is not None and waited > APPROVAL_BOTTLENECK_HOURS:
                observations.append(Observation(
                    type="approval_bottleneck",
                    description=(
                        f"approval for action {request.action_key or request.origin_kind!r} "
                        f"has been waiting {waited:.1f}h"
                    ),
                    evidence_refs=[f"approval_request:{request.id}"],
                ))
        # Same wait, different fact. An error card nobody answered is a stall,
        # not a bottleneck — surfaced under its own type so a reader (or a
        # metric built on these) never has to infer which it was, and carrying
        # the hitl_type so "waiting on a login" and "waiting on a fix" stay
        # distinguishable.
        for request in open_information_requests:
            waited = age_hours(request.created_at, ctx.now)
            if waited is not None and waited > INFORMATION_STALL_HOURS:
                observations.append(Observation(
                    type="information_request_stalled",
                    description=(
                        f"{request.hitl_type} request for "
                        f"{request.action_key or request.origin_kind!r} has been "
                        f"waiting {waited:.1f}h — the run needs a person to "
                        f"supply something, not to approve anything"
                    ),
                    evidence_refs=[f"approval_request:{request.id}"],
                ))
        for commitment in blocking_commitments:
            blocked = ", ".join(
                str(x) for x in (commitment.blocking_execution_ids or [])[:5]
            )
            observations.append(Observation(
                type="blocking_input_waiting",
                description=(
                    f"execution is blocked waiting for human "
                    f"{commitment.request_kind} (source "
                    f"{commitment.source_kind}:{commitment.source_id}; "
                    f"blocking: {blocked})"
                ),
                evidence_refs=[f"human_commitment:{commitment.id}"],
            ))

        # ── contribution edit aggregate (L0) ───────────────────────────
        # Window-scoped through the ledger: the contribution ids come from
        # the frozen events, so this stays replayable.
        contribution_ids = [event.source_id for event in contribution_events]
        contributions: list[HumanContribution] = []
        if contribution_ids:
            contributions = list((await db.execute(
                select(HumanContribution).where(
                    HumanContribution.id.in_(contribution_ids)
                ).order_by(HumanContribution.created_at.asc())
            )).scalars().all())
        # M9.6: field NAMES and counts only — diff_summary values (length
        # deltas, changed flags) and participant ids never leave this loop.
        # Kept as a list of {field, count} pairs rather than a dict so a
        # field literally named e.g. "efficiency" can never trip the
        # FORBIDDEN_HUMAN_METRICS key blacklist.
        field_counts: dict[str, int] = {}
        edited_contributions: list[HumanContribution] = []
        for contribution in contributions:
            fields = [
                str(name) for name in (contribution.diff_summary or {}).keys()
            ]
            if not fields:
                continue
            edited_contributions.append(contribution)
            for name in fields:
                field_counts[name] = field_counts.get(name, 0) + 1
        edited_fields = [
            {"field": name, "count": count}
            for name, count in sorted(
                field_counts.items(), key=lambda item: (-item[1], item[0]),
            )
        ]

        # ── L1 (opt-in): induce the common shape of repeated edits ─────
        # M4.5 — one LLM call per review at most: a single call site, no
        # retry, guarded by the ≥3-contribution threshold.
        l1_marker = l1_layer.L1_DISABLED
        if l1_layer.l1_enabled():
            l1_marker = l1_layer.L1_SKIPPED
            if len(edited_contributions) >= REPEATED_EDIT_THRESHOLD and edited_fields:
                patterns = await l1_layer.summarize_edit_patterns(
                    edited_fields,
                    entity_id=ctx.review.entity_id,
                    workspace_id=ctx.review.workspace_id,
                )
                if patterns is None:
                    l1_marker = l1_layer.L1_UNAVAILABLE
                else:
                    l1_marker = l1_layer.L1_USED
                    evidence = [
                        f"human_contribution:{contribution.id}"
                        for contribution in
                        edited_contributions[:MAX_EDIT_EVIDENCE_REFS]
                    ]
                    for pattern in patterns:
                        observations.append(Observation(
                            type="repeated_edit_pattern",
                            description=(
                                f"{pattern['pattern']} "
                                f"({pattern['count']} contributions)"
                            ),
                            evidence_refs=evidence,
                        ))

        metrics = {
            "decisions_since_last_review": {
                "approved": len(approved_events),
                "rejected": len(rejected_events),
            },
            "proposal_edits": 0,
            # Approvals only — see the split above. Its companion metric is
            # right below rather than folded in, so a reader can never take
            # one number for the other.
            "open_approvals": len(open_approvals),
            "oldest_open_approval_age_hours": oldest_open_hours,
            "open_information_requests": len(open_information_requests),
            "active_human_commitments": len(open_commitments),
            "blocking_commitments": len(blocking_commitments),
            "contributions_since_last_review": len(contribution_events),
            "edited_fields": edited_fields,
        }
        decision_events = approved_events + rejected_events
        return ConsolidationReportModel(
            domain=self.domain,
            status="complete",
            summary=(
                f"{len(approved_events)} approval(s), {len(rejected_events)} "
                f"rejection(s) this window; {len(open_approvals)} open approval(s); "
                f"{len(open_information_requests)} open information request(s); "
                f"{len(open_commitments)} open commitment(s) "
                f"({len(blocking_commitments)} blocking); "
                f"{len(contribution_events)} human contribution(s)"
            ),
            metrics=metrics,
            observations=observations,
            evidence_refs=evidence_ids(decision_events),
            coverage=Coverage(
                records_examined=(
                    len(decision_events) + len(open_hitl_requests)
                    + len(open_commitments) + len(contribution_events)
                ),
                sources={
                    "l1": l1_marker,
                    "decision_events": len(decision_events),
                    "open_approvals": len(open_approvals),
                    "open_information_requests": len(open_information_requests),
                    "open_commitments": len(open_commitments),
                    "contribution_events": len(contribution_events),
                    "contributions_with_edits": len(edited_contributions),
                    "proposal_edits": "no edit tracking yet (M9.3 card edit)",
                },
            ),
            analyzer_version=self.analyzer_version,
        )
