"""Gather everything Strategist needs as context for one review.

Mostly DB reads with one bounded filesystem cache refresh for canonical
workspace memory docs. The result is a small plain-data bundle that ``prompt``
formats into the LLM message.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.execution import ExecutionPlan
from packages.core.models.goal import Goal
from packages.core.models.task import Task
from packages.core.models.workspace import Agent, AgentSubscription, Workspace, WorkspaceActivity
from packages.core.services.provider_keys import canonical_provider_key
from packages.core.services.workspace_readiness import (
    check_workspace_readiness,
    iter_workspace_channel_blocks,
)

logger = logging.getLogger(__name__)


@dataclass
class StrategistContext:
    workspace: Workspace
    subscriptions: list[AgentSubscription] = field(default_factory=list)
    agents_by_id: dict[str, Agent] = field(default_factory=dict)
    allowed_service_keys: list[str] = field(default_factory=list)
    """Services the Strategist may name as owner/delegate. Derived from
    the subscriptions list, *not* from operating_model.services — what
    actually exists in DB wins over what setup intended."""

    goals: list[Goal] = field(default_factory=list)
    recent_tasks: list[Task] = field(default_factory=list)
    recent_plans: list[ExecutionPlan] = field(default_factory=list)
    recent_activity: list[WorkspaceActivity] = field(default_factory=list)
    """Recent workspace activity/events that may not be represented as tasks."""

    work_batch_reconciliation: list[dict[str, Any]] = field(default_factory=list)
    """Stale/completed batch reconciliation snapshots from the current review."""

    relevant_memory: list[dict[str, Any]] = field(default_factory=list)
    """As returned by memory.service.get_relevant_memory."""

    operating_memory: str = ""
    """Bounded fixed Markdown operating docs loaded from workspace memory."""

    open_proposed_tasks: list[Task] = field(default_factory=list)
    """Tasks already in ``status='proposed'`` from prior reviews —
    used to dedupe so the Strategist doesn't propose the same thing
    twice in the same cycle."""

    recent_proposal_outcomes: dict[str, list[Task]] = field(default_factory=dict)
    """Past Strategist proposals (last 30d) bucketed by what happened
    to them. Keys: ``completed``, ``rejected``, ``abandoned``,
    ``in_progress``. Used so the Strategist can learn from approval /
    rejection patterns instead of repeating misses."""

    configured_integrations: list[str] = field(default_factory=list)
    """Provider keys this workspace can use.

    This is scoped to the workspace's declared provider needs rather than
    every integration connected to the entity, so a LinkedIn credential used
    elsewhere cannot steer an X-only workspace toward LinkedIn tasks.
    """

    configured_channels: list[dict[str, Any]] = field(default_factory=list)
    """Workspace channels that are actually configured or built in.

    This is intentionally separate from entity integrations: built-in
    channels like webchat and internal chat do not need external
    credentials but are still real runtime surfaces.
    """

    knowledge_nets: list[dict[str, Any]] = field(default_factory=list)
    """Workspace knowledge nets with document counts."""

    governance_policy: dict[str, Any] | None = None
    """Current workspace governance policy, if configured."""

    missing_setup: list[str] = field(default_factory=list)
    """What's not ready yet — e.g. 'no_channels', 'no_goals', 'no_agents'.
    If non-empty, Strategist should propose setup tasks instead of work tasks."""

    missing_channel_requirements: list[dict[str, Any]] = field(default_factory=list)
    """Declared external channels that still need a concrete channel config."""

    workspace_readiness: dict[str, Any] = field(default_factory=dict)
    """Unified readiness report with part roles, checks, statuses, and blockers."""

    calibration: dict[str, Any] = field(default_factory=dict)
    """How well past predictions matched reality. Empty until the
    outcome-evaluation job has labeled some history.

    Keys when populated:
      ``sample_size``     — labeled proposals informing the stats
      ``mean_ratio``      — actual_delta / predicted_delta, averaged
      ``approval_rate``   — approved / proposed last 30d
      ``win_rate``        — won / labeled
      ``harmed_rate``     — harmed / labeled
      ``narrative``       — one-sentence English summary for the prompt
    """

    recent_runtime_evidence: list[Any] = field(default_factory=list)
    """Compact evidence ledger entries from recent agent/workspace runs."""

    learning_candidates: list[Any] = field(default_factory=list)
    """Open candidate memories/skills/profile patches the Strategist should
    consider before proposing more work.
    """

    workspace_evaluation: dict[str, Any] | None = None
    """Latest computed workspace scorecard across goal impact, cost, time,
    execution health, output quality, feedback, governance, and learning.
    """

    strategist_template: dict[str, Any] = field(default_factory=dict)
    """Per-workspace Strategist configuration sourced from
    ``operating_model.strategist``. Populated by blueprint installs
    that ship a ``recipe.strategist`` section, or hand-edited later.

    Recognised keys (all optional):
      ``cadence``                str — legacy schedule label
      ``trigger_conditions``     dict — ``{"skip_if_any": ["expr", ...]}``
      ``business_model``         dict — model_type / primary_signal /
                                          anti_signals / decision_window
      ``proposal_shape``         dict — max_tasks_per_cycle / preferred_*
      ``priors``                 dict — calibration seeds
      ``evaluation_rubric``      dict — self-scoring weights
      ``do_not_propose``         list[str] — hard-block proposal patterns
      ``voice``                  dict — style + examples for card text
      ``system_prompt_override`` str — escape hatch; replaces entire
                                       preamble when set

    The prompt renderer injects most of these into the system prompt;
    the post-LLM filter uses ``proposal_shape`` to cap and reshape the
    cohort; ``run_review`` evaluates ``trigger_conditions`` before the
    LLM call. Empty dict means "use defaults".
    """

    trigger: str = "scheduled"


# ── Public ────────────────────────────────────────────────────────────

async def gather_context(
    db: AsyncSession,
    workspace: Workspace,
    *,
    trigger: str = "scheduled",
    recent_task_limit: int = 15,
    recent_plan_limit: int = 10,
    memory_top_k: int = 12,
) -> StrategistContext:
    subs = await _list_subscriptions(db, workspace.id)
    agents_by_id = await _agents_by_id(db, [s.agent_id for s in subs])
    allowed = sorted({s.service_key for s in subs if s.service_key})

    goals = await _active_goals(db, workspace.entity_id, workspace.id)
    recent_tasks = await _recent_tasks(db, workspace.id, limit=recent_task_limit)
    recent_plans = await _recent_plans(db, workspace.id, limit=recent_plan_limit)
    recent_activity = await _recent_activity(db, workspace.id)
    open_proposed = await _open_proposed_tasks(db, workspace.id)
    recent_outcomes = await _recent_proposal_outcomes(db, workspace.id)

    relevant_memory = await _gather_memory(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        goals=goals,
        recent_tasks=recent_tasks,
        k=memory_top_k,
    )
    operating_memory = await _gather_operating_memory(db, workspace)

    # Provider readiness is scoped to this workspace's declared provider needs;
    # entity-level integrations such as a connected LinkedIn account must not
    # make an X-only workspace propose LinkedIn work.
    declared_provider_keys = _workspace_declared_provider_keys(workspace, goals)
    active_provider_keys = await _active_provider_keys(db, workspace.entity_id)
    if declared_provider_keys:
        configured_integrations = sorted(declared_provider_keys & active_provider_keys)
    else:
        configured_integrations = sorted(active_provider_keys)

    knowledge_nets = await _knowledge_nets(db, workspace)
    governance_policy = await _governance_policy(db, workspace)
    readiness = await check_workspace_readiness(
        db,
        workspace,
        subscriptions=subs,
        goals=goals,
        declared_provider_keys=declared_provider_keys,
        active_provider_keys=active_provider_keys,
        configured_integrations=configured_integrations,
        knowledge_nets=knowledge_nets,
        governance_policy=governance_policy,
        operating_memory=operating_memory,
    )
    configured_channels = readiness.configured_channels
    missing_channel_requirements = readiness.missing_channel_requirements
    missing_setup = readiness.missing_setup_keys

    calibration = await _calibration_stats(db, workspace.id)
    recent_runtime_evidence = await _recent_runtime_evidence(db, workspace)
    learning_candidates = await _learning_candidates(db, workspace)
    workspace_evaluation = await _workspace_evaluation(db, workspace)

    # Strategist template lives under operating_model.strategist. It's
    # never required — empty dict means "use baked-in defaults". Note
    # we copy out a shallow dict so the caller can mutate without
    # touching the SQLAlchemy-tracked JSONB.
    strategist_template = dict(
        (workspace.operating_model or {}).get("strategist") or {}
    )

    return StrategistContext(
        workspace=workspace,
        subscriptions=subs,
        agents_by_id=agents_by_id,
        allowed_service_keys=allowed,
        goals=goals,
        recent_tasks=recent_tasks,
        recent_plans=recent_plans,
        recent_activity=recent_activity,
        relevant_memory=relevant_memory,
        operating_memory=operating_memory,
        open_proposed_tasks=open_proposed,
        recent_proposal_outcomes=recent_outcomes,
        configured_integrations=configured_integrations,
        configured_channels=configured_channels,
        missing_channel_requirements=missing_channel_requirements,
        knowledge_nets=knowledge_nets,
        governance_policy=governance_policy,
        missing_setup=missing_setup,
        calibration=calibration,
        recent_runtime_evidence=recent_runtime_evidence,
        learning_candidates=learning_candidates,
        workspace_evaluation=workspace_evaluation,
        workspace_readiness=readiness.as_dict(),
        strategist_template=strategist_template,
        trigger=trigger,
    )


# ── DB helpers ────────────────────────────────────────────────────────

async def _list_subscriptions(
    db: AsyncSession, workspace_id: str,
) -> list[AgentSubscription]:
    return list((await db.execute(
        select(AgentSubscription).where(
            AgentSubscription.workspace_id == workspace_id,
            AgentSubscription.status == "active",
        )
    )).scalars().all())


async def _agents_by_id(
    db: AsyncSession, ids: list[str],
) -> dict[str, Agent]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    rows = list((await db.execute(
        select(Agent).where(Agent.id.in_(ids))
    )).scalars().all())
    return {a.id: a for a in rows}


async def _active_provider_keys(db: AsyncSession, entity_id: str) -> set[str]:
    """Return active credential providers available to this entity.

    Social OAuth credentials often live on ``oauth_accounts`` rather than
    the entity-scope ``integrations`` table. Strategist needs both sources,
    then workspace scoping decides which providers are relevant.
    """
    out: set[str] = set()
    try:
        from packages.core.models.document import Integration

        rows = list((await db.execute(
            select(Integration.provider).where(
                Integration.entity_id == entity_id,
                Integration.status == "active",
            )
        )).scalars().all())
        out.update(_canonical_provider_values(rows))
    except Exception:
        pass

    try:
        from packages.core.models.user import OAuthAccount, User

        rows = list((await db.execute(
            select(OAuthAccount.provider)
            .join(User, User.id == OAuthAccount.user_id)
            .where(
                User.entity_id == entity_id,
                OAuthAccount.access_token.isnot(None),
            )
        )).scalars().all())
        out.update(_canonical_provider_values(rows))
    except Exception:
        pass

    return out


def _canonical_provider_values(values: list[Any]) -> set[str]:
    return {
        key
        for key in (canonical_provider_key(value) for value in values)
        if key
    }


_GENERIC_CHANNEL_KEYS = {
    "",
    "custom",
    "in_app",
    "internal",
    "internal_chat",
    "other",
    "webchat",
}


def _workspace_declared_provider_keys(workspace: Workspace, goals: list[Goal]) -> set[str]:
    """Infer provider requirements from workspace design, not recent task drift."""
    out: set[str] = set()
    settings = workspace.settings or {}
    operating_model = workspace.operating_model or {}

    for raw_flag in settings.get("flagged_integrations") or []:
        if not isinstance(raw_flag, dict):
            continue
        provider = raw_flag.get("provider") or raw_flag.get("server_key")
        key = canonical_provider_key(provider)
        if key:
            out.add(key)

    for goal in goals:
        source = goal.measurement_source or {}
        if isinstance(source, dict):
            key = canonical_provider_key(source.get("provider"))
            if key:
                out.add(key)

    for _, block in iter_workspace_channel_blocks(operating_model):
        for provider_field in ("provider", "channel_type"):
            key = canonical_provider_key(block.get(provider_field))
            if key and key not in _GENERIC_CHANNEL_KEYS:
                out.add(key)

    text_parts = [
        workspace.name,
        workspace.description,
        workspace.kind,
        workspace.operating_context,
        workspace.primary_work,
    ]
    for service in operating_model.get("services") or []:
        if not isinstance(service, dict):
            continue
        text_parts.extend([
            service.get("key"),
            service.get("name"),
            service.get("title"),
            service.get("description"),
            service.get("rationale"),
            service.get("owner_role"),
        ])
    for _, block in iter_workspace_channel_blocks(operating_model):
        text_parts.extend([
            block.get("name"),
            block.get("purpose"),
            block.get("notes"),
            block.get("provider"),
            block.get("channel_type"),
        ])
    out.update(_social_provider_keys_from_text(" \n".join(str(part or "") for part in text_parts)))
    return out


def _social_provider_keys_from_text(text: str) -> set[str]:
    lowered = f" {(text or '').lower()} "
    out: set[str] = set()
    social_context = bool(re.search(
        r"\b(post|posts|posting|publish|tweet|tweets|follower|followers|"
        r"engagement|mentions|replies|dm|audience|social|campaign|content|"
        r"growth|analytics)\b",
        lowered,
    ))
    if re.search(r"\b(linkedin|linked-in|linked in)\b", lowered):
        out.add("linkedin")
    if re.search(r"\b(facebook|fb)\b", lowered):
        out.add("facebook")
    if (
        re.search(r"\b(twitter|tweet|tweets|tweeting|retweet|retweets)\b", lowered)
        or re.search(r"\bx\.com\b", lowered)
        or re.search(r"\bx\s*/\s*twitter\b", lowered)
        or re.search(r"\bx\s*\(\s*twitter\s*\)", lowered)
        or re.search(r"\b(x post|x posts|x thread|x threads|x account|x growth)\b", lowered)
        or (social_context and re.search(r"\bx\b", lowered))
    ):
        out.add("twitter_x")
    return out


async def _knowledge_nets(db: AsyncSession, workspace: Workspace) -> list[dict[str, Any]]:
    from packages.core.models.document import DocumentGroup, DocumentGroupMember
    from packages.core.services.knowledge_starter import starter_document_state

    rows = list((await db.execute(
        select(
            DocumentGroup.id,
            DocumentGroup.name,
            DocumentGroup.settings,
            func.count(DocumentGroupMember.document_id).label("document_count"),
        )
        .outerjoin(DocumentGroupMember, DocumentGroupMember.group_id == DocumentGroup.id)
        .where(
            DocumentGroup.workspace_id == workspace.id,
            DocumentGroup.entity_id == workspace.entity_id,
        )
        .group_by(DocumentGroup.id)
        .order_by(DocumentGroup.name)
    )).all())

    out: list[dict[str, Any]] = []
    for group_id, name, settings, document_count in rows:
        cfg = dict(settings or {})
        if cfg.get("workspace_file_bucket"):
            continue
        count = int(document_count or 0)
        row = {
            "group_id": group_id,
            "name": name,
            "purpose": cfg.get("purpose") or "",
            "linked_service_keys": list(cfg.get("linked_service_keys") or []),
            "document_count": count,
        }
        starter = starter_document_state(
            cfg,
            group_name=name,
            document_count=count,
        )
        if starter:
            row["starter_document_status"] = starter["status"]
            row["starter_task_key"] = starter["task_key"]
        out.append(row)
    return out


async def _governance_policy(db: AsyncSession, workspace: Workspace) -> dict[str, Any] | None:
    try:
        from packages.core.models.governance import GovernancePolicy

        policy = (await db.execute(
            select(GovernancePolicy.policy).where(
                GovernancePolicy.workspace_id == workspace.id,
                GovernancePolicy.entity_id == workspace.entity_id,
            )
        )).scalar_one_or_none()
        return dict(policy or {}) if policy else None
    except Exception:
        return None


async def _active_goals(
    db: AsyncSession, entity_id: str, workspace_id: str,
) -> list[Goal]:
    return list((await db.execute(
        select(Goal).where(
            Goal.entity_id == entity_id,
            Goal.workspace_id == workspace_id,
            Goal.status == "active",
        ).order_by(Goal.priority.desc(), Goal.created_at.desc())
    )).scalars().all())


async def _recent_tasks(
    db: AsyncSession, workspace_id: str, *, limit: int,
) -> list[Task]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    return list((await db.execute(
        select(Task).where(
            Task.workspace_id == workspace_id,
            Task.created_at >= cutoff,
        ).order_by(desc(Task.created_at)).limit(limit)
    )).scalars().all())


async def _recent_plans(
    db: AsyncSession, workspace_id: str, *, limit: int,
) -> list[ExecutionPlan]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    return list((await db.execute(
        select(ExecutionPlan).where(
            ExecutionPlan.workspace_id == workspace_id,
            ExecutionPlan.created_at >= cutoff,
        ).order_by(desc(ExecutionPlan.created_at)).limit(limit)
    )).scalars().all())


async def _recent_activity(
    db: AsyncSession, workspace_id: str, *, limit: int = 12,
) -> list[WorkspaceActivity]:
    return list((await db.execute(
        select(WorkspaceActivity).where(
            WorkspaceActivity.workspace_id == workspace_id,
        ).order_by(desc(WorkspaceActivity.created_at)).limit(limit)
    )).scalars().all())


async def _open_proposed_tasks(
    db: AsyncSession, workspace_id: str,
) -> list[Task]:
    return list((await db.execute(
        select(Task).where(
            Task.workspace_id == workspace_id,
            Task.status == "proposed",
        ).order_by(desc(Task.created_at))
    )).scalars().all())


async def _recent_proposal_outcomes(
    db: AsyncSession, workspace_id: str,
) -> dict[str, list[Task]]:
    """Bucket past Strategist proposals (last 30d) by what happened.

    Buckets:
      * ``completed``     — approved + finished (LLM should keep doing similar)
      * ``rejected``      — operator cancelled with a rejection_reason
      * ``abandoned``     — cancelled with no reason, or stuck >30d
      * ``in_progress``   — accepted but still running (informational only)

    Bucket sizes are capped so the prompt doesn't balloon.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    rows = list((await db.execute(
        select(Task).where(
            Task.workspace_id == workspace_id,
            Task.created_at >= cutoff,
            Task.details["strategist_review_id"].astext.isnot(None),
            Task.status != "proposed",
        ).order_by(desc(Task.created_at)).limit(60)
    )).scalars().all())

    buckets: dict[str, list[Task]] = {
        "completed": [], "rejected": [], "abandoned": [], "in_progress": [],
    }
    for t in rows:
        details = t.details or {}
        if t.status == "completed":
            buckets["completed"].append(t)
        elif t.status == "cancelled":
            if details.get("rejection_reason"):
                buckets["rejected"].append(t)
            else:
                buckets["abandoned"].append(t)
        elif t.status in {"failed", "blocked"}:
            buckets["abandoned"].append(t)
        else:
            buckets["in_progress"].append(t)

    return {k: v[:6] for k, v in buckets.items() if v}


async def _calibration_stats(
    db: AsyncSession, workspace_id: str,
) -> dict[str, Any]:
    """Gather "how well are we predicting?" stats from labeled proposals.

    Reads the last 90d of evaluated proposals (those with
    ``details.outcome_label`` set by ``strategist.evaluation``). Returns
    an empty dict when nothing has been labeled yet — Strategist's prompt
    builder skips the calibration block in that case.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    rows = list((await db.execute(
        select(Task).where(
            Task.workspace_id == workspace_id,
            Task.details["strategist_review_id"].astext.isnot(None),
            Task.details["outcome_label"].astext.isnot(None),
            Task.created_at >= cutoff,
        ).order_by(desc(Task.completed_at)).limit(80)
    )).scalars().all())

    labeled = 0
    won = lost = harmed = washed = 0
    ratios: list[float] = []
    for t in rows:
        details = t.details or {}
        label = details.get("outcome_label")
        if label in {"untracked", "goal_missing"}:
            continue
        labeled += 1
        if label == "won":
            won += 1
        elif label == "lost":
            lost += 1
        elif label == "harmed":
            harmed += 1
        elif label == "washed":
            washed += 1
        impact = details.get("estimated_impact") or {}
        predicted = impact.get("metric_delta")
        actual = details.get("outcome_actual_delta")
        if predicted and actual is not None:
            try:
                ratios.append(float(actual) / float(predicted))
            except (ZeroDivisionError, TypeError, ValueError):
                pass

    if labeled == 0:
        # Approval rate alone is still useful even without outcome labels.
        approved, proposed = await _approval_counts(db, workspace_id)
        if proposed == 0:
            return {}
        approval_rate = approved / proposed if proposed else 0.0
        return {
            "sample_size": 0,
            "approval_rate": approval_rate,
            "narrative": (
                f"No labeled outcomes yet — too soon to calibrate. "
                f"Operator has approved {approved} of last {proposed} "
                f"proposals ({approval_rate:.0%})."
            ),
        }

    mean_ratio = sum(ratios) / len(ratios) if ratios else None
    win_rate = won / labeled
    harmed_rate = harmed / labeled
    approved, proposed = await _approval_counts(db, workspace_id)
    approval_rate = (approved / proposed) if proposed else 0.0

    narrative = _narrate_calibration(
        sample_size=labeled,
        mean_ratio=mean_ratio,
        win_rate=win_rate,
        harmed_rate=harmed_rate,
        approval_rate=approval_rate,
    )

    return {
        "sample_size": labeled,
        "mean_ratio": mean_ratio,
        "approval_rate": approval_rate,
        "win_rate": win_rate,
        "harmed_rate": harmed_rate,
        "won": won, "washed": washed, "lost": lost, "harmed": harmed,
        "narrative": narrative,
    }


async def _approval_counts(
    db: AsyncSession, workspace_id: str,
) -> tuple[int, int]:
    """(approved, proposed) over the last 30d."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    rows = list((await db.execute(
        select(Task.status).where(
            Task.workspace_id == workspace_id,
            Task.details["strategist_review_id"].astext.isnot(None),
            Task.created_at >= cutoff,
        )
    )).scalars().all())
    proposed = len(rows)
    approved = sum(1 for s in rows if s not in ("proposed", "cancelled"))
    return approved, proposed


def _narrate_calibration(
    *,
    sample_size: int,
    mean_ratio: Optional[float],
    win_rate: float,
    harmed_rate: float,
    approval_rate: float,
) -> str:
    bits: list[str] = []
    if mean_ratio is not None:
        if mean_ratio >= 1.2:
            bits.append(
                f"You tend to *under*-predict impact (avg actual is "
                f"{mean_ratio:.1f}x predicted) — feel free to be bolder."
            )
        elif mean_ratio <= 0.7:
            bits.append(
                f"You tend to *over*-predict impact (avg actual is only "
                f"{mean_ratio:.1f}x predicted) — be more conservative on "
                f"`metric_delta`."
            )
        else:
            bits.append(
                f"Past predictions are well-calibrated "
                f"(avg actual / predicted ≈ {mean_ratio:.1f}x)."
            )
    if harmed_rate > 0.15:
        bits.append(
            f"⚠ {harmed_rate:.0%} of labeled proposals moved the metric "
            f"the WRONG way — review the recent learnings before proposing."
        )
    if approval_rate < 0.5 and sample_size >= 5:
        bits.append(
            f"Operator only approves {approval_rate:.0%} of proposals — "
            f"propose fewer / higher-conviction items."
        )
    elif approval_rate >= 0.8:
        bits.append(
            f"High approval rate ({approval_rate:.0%}) — your reads are "
            f"matching the operator's priorities."
        )
    return " ".join(bits) or "Calibration is neutral so far."


async def _gather_memory(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    goals: list[Goal],
    recent_tasks: list[Task],
    k: int,
) -> list[dict[str, Any]]:
    """Build a query string from active goals + recent task titles so
    the embedding search returns memory entries relevant to current
    state. Empty query falls through to recency-ordered top-K, which
    is fine for first-ever reviews when nothing has happened yet."""
    from packages.core.memory import get_relevant_memory

    parts: list[str] = []
    for g in goals[:5]:
        parts.append(f"{g.title} (target {g.target_value} {g.metric_key})")
    for t in recent_tasks[:5]:
        parts.append(t.title or "")
    query = " | ".join(p for p in parts if p)

    return await get_relevant_memory(
        db,
        entity_id=entity_id,
        workspace_id=workspace_id,
        query=query,
        k=k,
    )


async def _gather_operating_memory(db: AsyncSession, workspace: Workspace) -> str:
    try:
        from packages.core.memory.canonical import (
            ensure_workspace_memory_docs,
            load_workspace_operating_memory,
        )
        from packages.core.services.entity_fs import is_fs_enabled

        if not is_fs_enabled():
            return ""
        try:
            from packages.core.services.workspace_state_files import refresh_workspace_state_files

            await refresh_workspace_state_files(db, workspace)
        except Exception:
            logger.debug(
                "Strategist: workspace state/file cache refresh skipped",
                exc_info=True,
            )
        ensure_workspace_memory_docs(
            workspace.entity_id,
            workspace.id,
            workspace_name=workspace.name,
            workspace_kind=workspace.kind,
        )
        return load_workspace_operating_memory(
            workspace.entity_id,
            workspace.id,
            max_chars=7_200,
        )
    except Exception:
        return ""


async def _recent_runtime_evidence(db: AsyncSession, workspace: Workspace) -> list[Any]:
    try:
        from packages.core.services.runtime_learning import list_runtime_evidence

        return await list_runtime_evidence(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            limit=8,
        )
    except Exception:
        return []


async def _learning_candidates(db: AsyncSession, workspace: Workspace) -> list[Any]:
    try:
        from packages.core.services.runtime_learning import list_learning_candidates

        return await list_learning_candidates(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            status="proposed",
            limit=8,
        )
    except Exception:
        return []


async def _workspace_evaluation(db: AsyncSession, workspace: Workspace) -> dict[str, Any] | None:
    try:
        from packages.core.services.workspace_evaluation import build_workspace_evaluation

        return await build_workspace_evaluation(
            db,
            workspace.id,
            entity_id=workspace.entity_id,
            window_days=30,
        )
    except Exception:
        return None
