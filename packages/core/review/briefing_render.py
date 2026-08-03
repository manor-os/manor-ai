"""Deterministic Markdown rendering of a ReviewBriefing (M5 → M6 input).

``render_briefing_markdown`` is a pure function: the same briefing renders
to the same string, byte for byte, so the LLM input is attributable to the
persisted ``review_runs.briefing`` JSON.

Section headers stay in English (the Strategist prompt corpus is English).
The renderer carries **facts only** — no advice, no recommendations, no
priority language; interpretation belongs to the Strategist system prompt.
"""
from __future__ import annotations

import json
from typing import Any

from packages.core.review.briefing import ReportDigest, ReviewBriefingModel

_SCALAR_TYPES = (str, int, float, bool)
_COMPACT_VALUE_MAX_CHARS = 200

COVERAGE_GAP_WARNING = (
    "缺失域的数据不可用,不得对其提出高风险变更 "
    "(data for the domains listed above is unavailable or stale this cycle; "
    "do not propose high-risk changes for them)."
)


def _compact_json(value: Any) -> str | None:
    """Render a metric value compactly; ``None`` if it is too large."""
    if value is None or isinstance(value, _SCALAR_TYPES):
        return json.dumps(value, ensure_ascii=False)
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    if len(rendered) > _COMPACT_VALUE_MAX_CHARS:
        return None
    return rendered


def _domain_title(domain: str) -> str:
    return domain.replace("_", " ").strip().capitalize() or domain


def _render_review_window(briefing: ReviewBriefingModel) -> list[str]:
    review = briefing.review or {}
    lines = ["## Review window"]
    lines.append(f"- review_id: {review.get('id')}")
    lines.append(f"- trigger: {review.get('trigger_kind')}")
    lines.append(
        f"- window: {review.get('window_start')} → {review.get('window_end')}"
    )
    lines.append(
        f"- watermarks: {review.get('watermark_start')} → {review.get('watermark_end')}"
    )
    lines.append(
        f"- workspace_revision: {review.get('workspace_revision')} · "
        f"policy_revision: {review.get('policy_revision')}"
    )
    return lines


def _render_report(digest: ReportDigest) -> list[str]:
    lines = [f"## {_domain_title(digest.domain)} report"]
    lines.append(f"Status: {digest.status} — {digest.summary or '(no summary)'}")

    metric_lines: list[str] = []
    for key in sorted(digest.metrics):
        rendered = _compact_json(digest.metrics[key])
        if rendered is None:
            continue
        metric_lines.append(f"- {key}: {rendered}")
    if metric_lines:
        lines.append("Key metrics:")
        lines.extend(metric_lines)

    if digest.observations:
        lines.append("Observations:")
        for obs in digest.observations:
            obs_type = obs.get("type") or "observation"
            description = obs.get("description") or ""
            line = f"- [{obs_type}] {description}"
            evidence = [str(ref) for ref in (obs.get("evidence_refs") or []) if ref]
            if evidence:
                line += f" [evidence: {','.join(evidence)}]"
            if obs.get("baseline"):
                line += " (baseline)"
            lines.append(line)
        if digest.observations_omitted:
            lines.append(
                f"- … {digest.observations_omitted} more observation(s) omitted"
            )

    if digest.uncertainties:
        lines.append("Uncertainties:")
        for uncertainty in digest.uncertainties:
            code = uncertainty.get("code") or "uncertainty"
            description = uncertainty.get("description") or ""
            lines.append(f"- [{code}] {description}")

    return lines


def _render_coverage_gaps(briefing: ReviewBriefingModel) -> list[str]:
    lines = ["## Coverage gaps"]
    if briefing.coverage_gaps:
        for gap in briefing.coverage_gaps:
            lines.append(f"- {gap}")
        lines.append(COVERAGE_GAP_WARNING)
    else:
        lines.append("_(none — every domain report is fresh and complete)_")
    return lines


def _render_open_approvals(briefing: ReviewBriefingModel) -> list[str]:
    """The approvals block the strategist reads.

    Every line here is a governance approval — the briefing filtered the rest
    out before it got here. The withheld count is still stated: a model told
    "none pending" while a person is in fact blocking the workspace would
    reason from a false picture, just in the other direction.
    """
    lines = ["## Open approvals"]
    if briefing.open_approvals:
        for approval in briefing.open_approvals:
            line = (
                f"- {approval.get('id')}: action={approval.get('action_key')} "
                f"risk={approval.get('risk_level')} "
                f"age_hours={approval.get('age_hours')}"
            )
            reason = approval.get("reason")
            if reason:
                line += f" — {str(reason)[:200]}"
            lines.append(line)
    else:
        lines.append("_(none pending)_")
    if briefing.non_governance_hitl_open:
        lines.append(
            f"_({briefing.non_governance_hitl_open} other request(s) are waiting "
            f"on a person for information or a fix — not approvals, not listed, "
            f"and not yours to decide.)_"
        )
    return lines


def _render_previous_decisions(briefing: ReviewBriefingModel) -> list[str]:
    lines = ["## Previous proposal decisions"]
    if briefing.previous_decisions:
        for decision in briefing.previous_decisions:
            line = (
                f"- {decision.decision}: task={decision.task_id} "
                f"review={decision.review_id} at={decision.decided_at}"
            )
            if decision.reason:
                line += f" — reason: {decision.reason[:300]}"
            lines.append(line)
    else:
        lines.append("_(no prior proposal decisions on record)_")
    return lines


def render_briefing_markdown(b: ReviewBriefingModel) -> str:
    """Render the briefing to deterministic Markdown for the user prompt."""
    blocks: list[list[str]] = [_render_review_window(b)]
    for domain in sorted(b.reports):
        blocks.append(_render_report(b.reports[domain]))
    blocks.append(_render_coverage_gaps(b))
    blocks.append(_render_open_approvals(b))
    blocks.append(_render_previous_decisions(b))
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"
