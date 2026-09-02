"""ReviewBriefing — deterministic assembly of the Strategist's data core (M5).

``build_briefing(db, review, report_rows)`` folds the review's persisted
``consolidation_reports`` rows plus a handful of live governance facts into
one validated, size-bounded pydantic model. **No LLM is involved** — the
briefing is a pure function of the frozen snapshot, so it can be persisted
into ``review_runs.briefing`` for audit and replay.

Composition rules (v1):

* ``reports`` — one :class:`ReportDigest` per domain. Observations are
  Top-K (K=10) per domain, ranked by evidence count (descending, stable);
  the overflow is counted in ``observations_omitted`` instead of dropped
  silently.
* ``coverage_gaps`` — domains whose report is ``failed``/``partial`` plus
  ``"<domain> (reused cached)"`` entries for cache-reused reports. The
  Strategist prompt turns this list into a hard "no high-risk changes for
  these domains" instruction.
* ``open_approvals`` — currently-pending :class:`HitlRequest` rows for
  the workspace that are actually APPROVALS (``GOVERNANCE_HITL_TYPES``;
  max 10, oldest first). Live state, not window state: an approval
  bottleneck is relevant no matter when it was created. Pending rows that
  ask for information rather than permission (a login wall, a missing
  field, a failed step) are counted in ``non_governance_hitl_open`` and
  kept out of the list — the strategist cannot act on them, and counting
  them as approvals is how "3 approvals are aging" came to include a
  connectivity error.
* ``previous_decisions`` — the last 20 ``proposal_item_approved`` /
  ``proposal_item_rejected`` ledger facts (newest first), bounded at the
  review's frozen ``watermark_end`` so a replayed briefing is identical.
* ``workflow_runs`` — active Workflow run families plus recent terminal
  families from the review window. Results and blockers are redacted and
  size-bounded before they enter the persisted briefing.
* ``strategist_template`` — ``workspace.operating_model["strategist"]``
  passed through untouched (现状沿用).

Size budget: after serialization the briefing must stay under
``SOFT_LIMIT_CHARS`` (32k). If it doesn't, progressively lossier truncations
are applied — drop individual metric values whose own JSON exceeds 2k chars,
reduce Top-K to 5, then reduce Workflow run families to 6.
``HARD_LIMIT_CHARS`` (48k) is an assertion: crossing it raises
:class:`BriefingTooLarge` (the review fails, the watermark does not advance —
a huge briefing is a bug, not an input).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.approvals import ApprovalStatus
from packages.core.constants.proposal import ProposalDecisionKind
from packages.core.constants.review import ConsolidationReportStatus
from packages.core.constants.workflow import (
    WORKFLOW_RUN_OPEN_STATUSES,
    WORKFLOW_RUN_TERMINAL_STATUSES,
    WorkflowRunStatus,
)
from packages.core.ledger import event_types as et
from packages.core.models.hitl_request import (
    HitlRequest,
    governance_hitl_clause,
)
from packages.core.models.review_run import ReviewRun
from packages.core.models.workflow import (
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowRun,
)
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.services.workflow_run_trace import (
    summarize_trace_text,
    summarize_trace_value,
)
from packages.core.services.workspace_flow_catalog import (
    workspace_blueprint_slug,
    workspace_flow_slug,
)

logger = logging.getLogger(__name__)

TOP_K_OBSERVATIONS = 10
REDUCED_TOP_K_OBSERVATIONS = 5
MAX_OPEN_APPROVALS = 10
MAX_PREVIOUS_DECISIONS = 20
MAX_WORKFLOW_RUNS = 12
REDUCED_MAX_WORKFLOW_RUNS = 6
WORKFLOW_RUN_QUERY_LIMIT = 64
WORKFLOW_VALUE_MAX_JSON_CHARS = 1200
WORKFLOW_FIELD_MAX_JSON_CHARS = 400
WORKFLOW_TEXT_MAX_CHARS = 800
METRIC_VALUE_MAX_JSON_CHARS = 2000
SOFT_LIMIT_CHARS = 32_000
HARD_LIMIT_CHARS = 48_000


class BriefingTooLarge(Exception):
    """The briefing exceeded the hard size cap even after truncation."""

    def __init__(self, size: int):
        self.size = size
        super().__init__(
            f"briefing serialization is {size} chars, above the hard cap "
            f"of {HARD_LIMIT_CHARS} — a consolidator is emitting unbounded data"
        )


class ReportDigest(BaseModel):
    """One domain's consolidation report, truncated for prompt use."""

    model_config = ConfigDict(extra="forbid")

    domain: str
    status: ConsolidationReportStatus
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    observations: list[dict] = Field(default_factory=list)
    observations_omitted: int = 0
    uncertainties: list[dict] = Field(default_factory=list)
    reused: bool = False


class DecisionDigest(BaseModel):
    """One historical proposal-item decision fact from the ledger."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    review_id: Optional[str] = None
    decision: ProposalDecisionKind
    reason: Optional[str] = None
    decided_at: Optional[str] = None


class WorkflowRunDigest(BaseModel):
    """One display-safe snapshot of the latest attempt in a run family."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    lineage_root_run_id: str
    workflow_id: str
    workflow_name: str
    binding_id: Optional[str] = None
    binding_name: Optional[str] = None
    blueprint_slug: Optional[str] = None
    workflow_slug: Optional[str] = None
    status: WorkflowRunStatus
    business_outcome: Optional[str] = None
    current_step_id: Optional[str] = None
    current_step_name: Optional[str] = None
    trigger_source: Optional[str] = None
    scheduled_job_id: Optional[str] = None
    period_key: Optional[str] = None
    attempt_number: int = 1
    retry_from_step_id: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result_summary: Any = None
    blocker: Any = None
    error: Optional[str] = None


class ReviewBriefingModel(BaseModel):
    """The full deterministic briefing persisted to ``review_runs.briefing``."""

    model_config = ConfigDict(extra="forbid")

    review: dict[str, Any]
    reports: dict[str, ReportDigest]
    coverage_gaps: list[str] = Field(default_factory=list)
    open_approvals: list[dict] = Field(default_factory=list)
    #: Open HITL requests deliberately kept OUT of ``open_approvals`` because
    #: they are not approvals (input / choice / error). Counted, not listed —
    #: see :func:`_non_governance_hitl_open`. Defaults to 0 so a briefing
    #: persisted before this field existed still validates on replay.
    non_governance_hitl_open: int = 0
    previous_decisions: list[DecisionDigest] = Field(default_factory=list)
    workflow_runs: list[WorkflowRunDigest] = Field(default_factory=list)
    strategist_template: dict[str, Any] = Field(default_factory=dict)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _json_len(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _rank_observations(observations: list[dict], top_k: int) -> tuple[list[dict], int]:
    """Top-K observations by evidence count (descending, stable order)."""
    rows = [obs for obs in (observations or []) if isinstance(obs, dict)]
    ranked = sorted(
        rows,
        key=lambda obs: -len(obs.get("evidence_refs") or []),
    )
    kept = ranked[:top_k]
    return kept, max(0, len(ranked) - len(kept))


def _report_digest(report: Any, *, top_k: int = TOP_K_OBSERVATIONS) -> ReportDigest:
    coverage = report.coverage if isinstance(report.coverage, dict) else {}
    kept, omitted = _rank_observations(list(report.observations or []), top_k)
    return ReportDigest(
        domain=report.domain,
        status=report.status,
        summary=report.summary or "",
        metrics=dict(report.metrics or {}),
        observations=kept,
        observations_omitted=omitted,
        uncertainties=[u for u in (report.uncertainties or []) if isinstance(u, dict)],
        reused=bool(coverage.get("reused")),
    )


def _coverage_gaps(report_rows: list[Any]) -> list[str]:
    gaps: list[str] = []
    for report in report_rows:
        if report.status in (
            ConsolidationReportStatus.FAILED,
            ConsolidationReportStatus.PARTIAL,
        ):
            gaps.append(report.domain)
    for report in report_rows:
        coverage = report.coverage if isinstance(report.coverage, dict) else {}
        if coverage.get("reused"):
            gaps.append(f"{report.domain} (reused cached)")
    return gaps


async def _open_approvals(db: AsyncSession, review: ReviewRun) -> list[dict]:
    """The oldest open GOVERNANCE approvals, as digests.

    Governance only. The strategist decides workspace policy and proposals;
    it can neither solve a CAPTCHA nor reconnect an operator's worker, so a
    request of that kind in this list is not a fact it can act on — it is a
    number that inflates "approvals are backing up" and skews every judgement
    downstream of it. Telling the model about three aging approvals when one
    is a connectivity error is the bigger lie; the smaller one is to not show
    what it cannot act on, and to say plainly that some rows were withheld
    (``non_governance_hitl_open``, rendered under the same heading).

    Filtered in SQL, before the LIMIT: taking the oldest 10 rows and then
    dropping the non-governance ones would return fewer than 10 governance
    approvals while more were waiting.
    """
    rows = list((await db.execute(
        select(HitlRequest)
        .where(
            HitlRequest.workspace_id == review.workspace_id,
            HitlRequest.status == ApprovalStatus.PENDING,
            governance_hitl_clause(),
        )
        .order_by(HitlRequest.created_at.asc(), HitlRequest.id.asc())
        .limit(MAX_OPEN_APPROVALS)
    )).scalars().all())
    now = datetime.now(timezone.utc)
    digests: list[dict] = []
    for row in rows:
        created_at = row.created_at
        if created_at is not None and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age_hours = (
            round((now - created_at).total_seconds() / 3600.0, 1)
            if created_at is not None else None
        )
        digests.append({
            "id": row.id,
            "action_key": row.action_key,
            "risk_level": row.risk_level,
            "age_hours": age_hours,
            "reason": row.reason,
            # Always a governance type, by the filter above. Stated anyway so
            # the persisted briefing is self-describing on replay.
            "hitl_type": row.hitl_type,
        })
    return digests


async def _non_governance_hitl_open(db: AsyncSession, review: ReviewRun) -> int:
    """How many open HITL requests ``_open_approvals`` withheld.

    A count, not a list: the strategist should know the workspace is waiting
    on a person for something else, and should NOT be handed the details of
    work it cannot act on. Unbounded by ``MAX_OPEN_APPROVALS`` — it is one
    integer, and a truncated count would be worse than none.
    """
    return int((await db.execute(
        select(func.count(HitlRequest.id)).where(
            HitlRequest.workspace_id == review.workspace_id,
            HitlRequest.status == ApprovalStatus.PENDING,
            ~governance_hitl_clause(),
        )
    )).scalar_one() or 0)


async def _previous_decisions(
    db: AsyncSession, review: ReviewRun,
) -> list[DecisionDigest]:
    """Last N proposal-decision facts, newest first, frozen at the review's
    ``watermark_end`` so replays of this briefing are byte-identical."""
    if review.watermark_end is None:
        return []
    rows = list((await db.execute(
        select(WorkspaceEvent)
        .where(
            WorkspaceEvent.workspace_id == review.workspace_id,
            WorkspaceEvent.id <= review.watermark_end,
            WorkspaceEvent.event_type.in_([
                et.PROPOSAL_ITEM_APPROVED,
                et.PROPOSAL_ITEM_REJECTED,
            ]),
        )
        .order_by(WorkspaceEvent.id.desc())
        .limit(MAX_PREVIOUS_DECISIONS)
    )).scalars().all())
    digests: list[DecisionDigest] = []
    for event in rows:
        payload = event.payload if isinstance(event.payload, dict) else {}
        decision = (
            ProposalDecisionKind.APPROVED
            if event.event_type == et.PROPOSAL_ITEM_APPROVED
            else ProposalDecisionKind.REJECTED
        )
        reason = payload.get("rejection_reason")
        digests.append(DecisionDigest(
            task_id=event.source_id,
            review_id=event.causation_id,
            decision=decision,
            reason=str(reason) if reason is not None else None,
            decided_at=_iso(event.occurred_at),
        ))
    return digests


def _bounded_workflow_value(value: Any) -> Any:
    """Redact one Workflow value and keep its briefing footprint small."""
    if value in (None, ""):
        return None
    summary = summarize_trace_value(value)
    rendered = json.dumps(summary, ensure_ascii=False, default=str)
    if len(rendered) <= WORKFLOW_VALUE_MAX_JSON_CHARS:
        return summary
    if isinstance(summary, dict):
        # JSONB key order is not semantic. Bound large fields independently so
        # one receipt/blob cannot push later result fields (for example a
        # publish URL) out of a serialized-prefix preview.
        fields: dict[str, Any] = {}
        for key, child in summary.items():
            child_json = json.dumps(child, ensure_ascii=False, default=str)
            compact_child = (
                child
                if len(child_json) <= WORKFLOW_FIELD_MAX_JSON_CHARS
                else {
                    "truncated": True,
                    "preview": child_json[:WORKFLOW_FIELD_MAX_JSON_CHARS],
                }
            )
            candidate = {"truncated": True, "fields": {**fields, key: compact_child}}
            if (
                len(json.dumps(candidate, ensure_ascii=False, default=str))
                <= WORKFLOW_VALUE_MAX_JSON_CHARS
            ):
                fields[key] = compact_child
        return {"truncated": True, "fields": fields}
    return {
        "truncated": True,
        "preview": rendered[:WORKFLOW_VALUE_MAX_JSON_CHARS],
    }


def _bounded_workflow_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = summarize_trace_text(value)
    if len(text) <= WORKFLOW_TEXT_MAX_CHARS:
        return text
    return text[:WORKFLOW_TEXT_MAX_CHARS] + "…"


def _workflow_project_state(run: WorkflowRun) -> dict[str, Any]:
    variables = run.variables if isinstance(run.variables, dict) else {}
    project = variables.get("project") if isinstance(variables.get("project"), dict) else {}
    state = project.get("state") if isinstance(project.get("state"), dict) else {}
    return state


def _workflow_result(run: WorkflowRun) -> Any:
    """Return the final result, falling back to the last visible node output."""
    variables = run.variables if isinstance(run.variables, dict) else {}
    if variables.get("__result") not in (None, ""):
        return variables["__result"]

    step_results = run.step_results if isinstance(run.step_results, dict) else {}
    snapshot = run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}
    nodes = snapshot.get("nodes") if isinstance(snapshot.get("nodes"), list) else []
    ordered_ids = [
        str(node.get("id") or "")
        for node in reversed(nodes)
        if isinstance(node, dict)
        and str(node.get("chat_projection") or "progress").lower() != "hidden"
        and node.get("id")
    ]
    ordered_ids.extend(str(step_id) for step_id in reversed(list(step_results)) if str(step_id) not in ordered_ids)
    for step_id in ordered_ids:
        result = step_results.get(step_id)
        if not isinstance(result, dict):
            continue
        output = result.get("output")
        if output not in (None, ""):
            return output
    return None


def _workflow_step_name(run: WorkflowRun, workflow: WorkflowDefinition) -> Optional[str]:
    if not run.current_step_id:
        return None
    snapshot = run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}
    nodes = snapshot.get("nodes") if isinstance(snapshot.get("nodes"), list) else []
    if not nodes:
        nodes = workflow.steps if isinstance(workflow.steps, list) else []
    for node in nodes:
        if isinstance(node, dict) and str(node.get("id") or "") == run.current_step_id:
            return str(node.get("name") or run.current_step_id)
    return run.current_step_id


def _workflow_run_digest(
    run: WorkflowRun,
    workflow: WorkflowDefinition,
    binding: WorkflowBinding | None,
    *,
    blueprint_slug: Optional[str],
) -> WorkflowRunDigest:
    trigger_data = run.trigger_data if isinstance(run.trigger_data, dict) else {}
    history = (
        trigger_data.get("_workflow_history_summary")
        if isinstance(trigger_data.get("_workflow_history_summary"), dict)
        else {}
    )
    state = _workflow_project_state(run)
    retry_state = state.get("retry_state") if isinstance(state.get("retry_state"), dict) else {}
    blocker = retry_state.get("observed_problem") or retry_state.get("required_change") or history.get("blocker")
    period_key = trigger_data.get("period_key") or state.get("period_key") or (run.variables or {}).get("period_key")
    snapshot = run.definition_snapshot if isinstance(run.definition_snapshot, dict) else {}
    workflow_name = str(snapshot.get("name") or workflow.name or workflow.id)
    return WorkflowRunDigest(
        run_id=run.id,
        lineage_root_run_id=run.lineage_root_run_id or run.id,
        workflow_id=run.workflow_id,
        workflow_name=workflow_name,
        binding_id=run.binding_id,
        binding_name=binding.name if binding is not None else None,
        blueprint_slug=blueprint_slug,
        workflow_slug=workspace_flow_slug(binding) or None,
        status=WorkflowRunStatus(run.status),
        business_outcome=(str(state.get("business_outcome") or history.get("business_outcome") or "") or None),
        current_step_id=run.current_step_id,
        current_step_name=_workflow_step_name(run, workflow),
        trigger_source=run.trigger_source,
        scheduled_job_id=str(trigger_data.get("scheduled_job_id") or "") or None,
        period_key=str(period_key or "") or None,
        attempt_number=run.effective_attempt_number,
        retry_from_step_id=run.effective_retry_from_step_id,
        started_at=_iso(run.started_at or run.created_at),
        completed_at=_iso(run.completed_at),
        result_summary=_bounded_workflow_value(_workflow_result(run)),
        blocker=_bounded_workflow_value(blocker),
        error=_bounded_workflow_text(run.error),
    )


async def _workflow_runs(
    db: AsyncSession,
    review: ReviewRun,
    workspace: Workspace | None,
) -> list[WorkflowRunDigest]:
    """Active families plus recent terminal families, latest attempt only."""
    terminal_at = func.coalesce(
        WorkflowRun.completed_at,
        WorkflowRun.updated_at,
        WorkflowRun.created_at,
    )
    terminal_window = [WorkflowRun.status.in_(WORKFLOW_RUN_TERMINAL_STATUSES)]
    if review.window_start is not None:
        terminal_window.append(terminal_at > review.window_start)
    if review.window_end is not None:
        terminal_window.append(terminal_at <= review.window_end)

    rows = list(
        (
            await db.execute(
                select(WorkflowRun, WorkflowDefinition, WorkflowBinding)
                .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowRun.workflow_id)
                .outerjoin(WorkflowBinding, WorkflowBinding.id == WorkflowRun.binding_id)
                .where(
                    WorkflowRun.entity_id == review.entity_id,
                    WorkflowRun.workspace_id == review.workspace_id,
                    or_(
                        WorkflowRun.status.in_(WORKFLOW_RUN_OPEN_STATUSES),
                        and_(*terminal_window),
                    ),
                )
                .order_by(
                    case(
                        (WorkflowRun.status.in_(WORKFLOW_RUN_OPEN_STATUSES), 0),
                        else_=1,
                    ),
                    WorkflowRun.updated_at.desc().nullslast(),
                    WorkflowRun.completed_at.desc().nullslast(),
                    WorkflowRun.started_at.desc().nullslast(),
                    WorkflowRun.created_at.desc(),
                    WorkflowRun.id.desc(),
                )
                .limit(WORKFLOW_RUN_QUERY_LIMIT)
            )
        ).all()
    )

    # The latest retry is the state of the work. Showing an earlier failed
    # attempt beside its running retry makes one piece of work look like two.
    latest_by_lineage: dict[
        str,
        tuple[WorkflowRun, WorkflowDefinition, WorkflowBinding | None],
    ] = {}
    for run, workflow, binding in rows:
        lineage = run.lineage_root_run_id or run.id
        previous = latest_by_lineage.get(lineage)
        if (
            previous is None
            or run.effective_attempt_number > previous[0].effective_attempt_number
        ):
            latest_by_lineage[lineage] = (run, workflow, binding)

    active: list[tuple[WorkflowRun, WorkflowDefinition, WorkflowBinding | None]] = []
    terminal: list[tuple[WorkflowRun, WorkflowDefinition, WorkflowBinding | None]] = []
    for row in latest_by_lineage.values():
        target = (
            active
            if WorkflowRunStatus(row[0].status) in WORKFLOW_RUN_OPEN_STATUSES
            else terminal
        )
        target.append(row)
    selected = (active + terminal)[:MAX_WORKFLOW_RUNS]

    blueprint_slug = workspace_blueprint_slug(workspace) or None
    return [
        _workflow_run_digest(
            run,
            workflow,
            binding,
            blueprint_slug=blueprint_slug,
        )
        for run, workflow, binding in selected
    ]


def _drop_oversized_metrics(briefing: ReviewBriefingModel) -> int:
    """Remove metric values whose own JSON footprint exceeds the cap."""
    dropped = 0
    for digest in briefing.reports.values():
        oversized = [
            key for key, value in digest.metrics.items()
            if _json_len(value) > METRIC_VALUE_MAX_JSON_CHARS
        ]
        for key in oversized:
            del digest.metrics[key]
            dropped += 1
        if oversized:
            logger.info(
                "briefing truncation: dropped oversized metric(s) %s from domain %s",
                oversized, digest.domain,
            )
    return dropped


def _reduce_top_k(briefing: ReviewBriefingModel, top_k: int) -> None:
    for digest in briefing.reports.values():
        if len(digest.observations) > top_k:
            omitted = len(digest.observations) - top_k
            digest.observations = digest.observations[:top_k]
            digest.observations_omitted += omitted


def _reduce_workflow_runs(briefing: ReviewBriefingModel, max_runs: int) -> None:
    if len(briefing.workflow_runs) > max_runs:
        briefing.workflow_runs = briefing.workflow_runs[:max_runs]


async def build_briefing(
    db: AsyncSession,
    review: ReviewRun,
    report_rows: list[Any],
) -> ReviewBriefingModel:
    """Deterministically assemble the briefing for one review.

    ``report_rows`` are the ``consolidation_reports`` rows returned by
    ``consolidators.run_all`` for this review (persistence-pending rows are
    fine — only attribute access is used). The caller persists the result:
    ``review.briefing = briefing.model_dump(mode="json")``.
    """
    workspace = await db.get(Workspace, review.workspace_id)
    operating_model = (
        workspace.operating_model if workspace is not None
        and isinstance(workspace.operating_model, dict) else {}
    )
    strategist_template = operating_model.get("strategist") or {}
    if not isinstance(strategist_template, dict):
        strategist_template = {}

    briefing = ReviewBriefingModel(
        review={
            "id": review.id,
            "trigger_kind": review.trigger_kind,
            "window_start": _iso(review.window_start),
            "window_end": _iso(review.window_end),
            "watermark_start": review.watermark_start,
            "watermark_end": review.watermark_end,
            "workspace_revision": review.workspace_revision,
            "policy_revision": review.policy_revision,
        },
        reports={
            report.domain: _report_digest(report) for report in report_rows
        },
        coverage_gaps=_coverage_gaps(report_rows),
        open_approvals=await _open_approvals(db, review),
        non_governance_hitl_open=await _non_governance_hitl_open(db, review),
        previous_decisions=await _previous_decisions(db, review),
        workflow_runs=await _workflow_runs(db, review, workspace),
        strategist_template=strategist_template,
    )

    # ── size budget ────────────────────────────────────────────────────
    size = len(briefing.model_dump_json())
    if size > SOFT_LIMIT_CHARS:
        _drop_oversized_metrics(briefing)
        size = len(briefing.model_dump_json())
    if size > SOFT_LIMIT_CHARS:
        _reduce_top_k(briefing, REDUCED_TOP_K_OBSERVATIONS)
        size = len(briefing.model_dump_json())
    if size > SOFT_LIMIT_CHARS:
        _reduce_workflow_runs(briefing, REDUCED_MAX_WORKFLOW_RUNS)
        size = len(briefing.model_dump_json())
    if size >= HARD_LIMIT_CHARS:
        raise BriefingTooLarge(size)
    return briefing
