"""Sandbox workspace mode — safe end-to-end demo without real side effects.

The ``settings.sandbox`` boolean on ``Workspace`` is the orthogonal
toggle. ``kind`` stays a semantic label (`social_media`, `property`, …)
so a sandbox can mirror the same kind as a real workspace and exercise
the same code paths.

Three behaviours change when ``is_sandbox_workspace(ws)`` is true:

  1. Plan creation: ``execution_mode`` defaults to ``"sandbox"`` instead
     of ``"live"``. PlanExecutor's existing dry-run path takes over —
     action steps call adapter ``simulate_tool`` (Phase 3d).

  2. Goal measurement: skips real integrations. ``simulate_goal_value``
     returns a value that follows the goal's pace curve with light
     noise so the user sees realistic-looking measurements + pace
     transitions without burning real API quota.

  3. Demo seed: ``create_sandbox_workspace`` provisions a complete
     workspace (workspace + agent + subscription + goal + 1 task) in
     one call so the "Try a demo" button has a backend.

Sandbox workspaces are not second-class — they share schema, services,
chat, and execution paths with live workspaces. The only difference is
where side effects land.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel, Document, DocumentGroup, DocumentGroupMember, VectorStatus
from packages.core.models.goal import Goal, GoalMeasurement, GoalTaskLink
from packages.core.models.runtime_learning import AgentLearningCandidate, RuntimeEvidence
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    Workspace,
    WorkspaceActivity,
    WorkspaceWorkBatch,
)
from packages.core.services.document_metadata import merge_document_metadata
from packages.core.services.hitl_options import approval_options
from packages.core.services.workspace_access import settings_with_default_workspace_access


SANDBOX_KIND = "sandbox"
"""Reserved value of ``Workspace.kind`` for "this whole workspace IS a
demo" (vs. the orthogonal ``settings.sandbox`` toggle which just makes
plans/measurements safe). Both are honoured by ``is_sandbox_workspace``."""

LEASING_DEMO_KIND_ALIASES = {
    "leasing",
    "property",
    "property_leasing",
    "leasing_consultant",
    "lease_consultant",
}


def sandbox_demo_name(kind: str | None) -> str:
    """Default display name for a sandbox demo kind."""
    if (kind or "").lower() in LEASING_DEMO_KIND_ALIASES:
        return "Leasing Consultant — Sandbox Demo"
    return "Twitter Growth — Sandbox Demo"


def sandbox_demo_services(kind: str | None) -> list[dict]:
    """Services to seed into workspace memory for a sandbox demo kind."""
    if (kind or "").lower() in LEASING_DEMO_KIND_ALIASES:
        return [
            {
                "service_key": "lead_intake",
                "description": "Captures rental lead requirements and drafts first replies.",
                "autonomy_level": "assisted",
            },
            {
                "service_key": "unit_recommendation",
                "description": "Matches renters to available units using workspace knowledge.",
                "autonomy_level": "assisted",
            },
            {
                "service_key": "tour_scheduling",
                "description": "Prepares tour options and scheduling handoffs.",
                "autonomy_level": "review_required",
            },
            {
                "service_key": "followup_drafting",
                "description": "Drafts follow-ups after tours, applications, and stale leads.",
                "autonomy_level": "review_required",
            },
            {
                "service_key": "pipeline_tracking",
                "description": "Reviews leasing pipeline health and triggers Strategist loops.",
                "autonomy_level": "assisted",
            },
        ]
    return [{
        "service_key": "content_creator",
        "description": "Drafts and posts content",
        "autonomy_level": "assisted",
    }]


# ── Detection ─────────────────────────────────────────────────────────

def is_sandbox_workspace(workspace: Workspace) -> bool:
    """True if the workspace should run in sandbox mode.

    Two ways to flag:
      * ``workspace.kind == 'sandbox'``       — the whole workspace IS
                                                 a demo;
      * ``workspace.settings.sandbox is True`` — orthogonal toggle on a
                                                 normal-kind workspace
                                                 (useful for testing
                                                 production templates
                                                 against fake side effects).
    """
    if (workspace.kind or "").lower() == SANDBOX_KIND:
        return True
    settings = workspace.settings or {}
    return bool(settings.get("sandbox"))


def default_execution_mode(workspace: Workspace) -> str:
    """The execution_mode that plans on this workspace should default to."""
    return "sandbox" if is_sandbox_workspace(workspace) else "live"


# ── Goal measurement simulation ───────────────────────────────────────

def simulate_goal_value(goal: Goal, *, today: Optional[date] = None) -> Decimal:
    """Generate a plausible measurement for a sandbox goal.

    Strategy:
      * If a current value is already seeded, treat it as the latest
        observed fact and nudge it a small step toward the target.
      * Otherwise compute expected linear progress from baseline → target
        across the deadline window and add small deterministic jitter.
      * Move in the baseline→target direction and cap at target so
        ``achieved`` triggers only when the simulated value truly reaches the
        target. This supports lower-is-better metrics such as response time.

    Deterministic-ish: seeded from goal id + today's date so two runs on
    the same day produce the same value (avoids confusing noise in the
    chat feed if a measurement gets re-fired).
    """
    today = today or date.today()
    baseline = float(goal.baseline_value or goal.current_value or 0)
    target = float(goal.target_value or 0)
    if target == baseline:
        return Decimal(str(target))
    lower_is_better = target < baseline

    def _cap(value: float) -> float:
        if lower_is_better:
            return min(baseline, max(target, value))
        return max(baseline, min(target, value))

    if goal.current_value is not None:
        # Seeded demo goals start with a plausible current value. A fresh
        # measurement should never erase that reality by snapping back to the
        # baseline, nor should a lower-is-better goal jump straight to target.
        cur = float(goal.current_value)
        increment = (target - cur) * 0.05
        return Decimal(f"{_cap(cur + increment):.4f}")

    if goal.deadline is None:
        # No deadline and no current value → use baseline as the first point.
        return Decimal(f"{_cap(baseline):.4f}")

    start = goal.created_at.date() if isinstance(goal.created_at, datetime) else goal.created_at
    total_days = max(1, (goal.deadline - start).days)
    elapsed = max(0, (today - start).days)
    elapsed_frac = min(1.0, elapsed / total_days)

    # Expected linear progress.
    expected = baseline + (target - baseline) * elapsed_frac

    rng = random.Random(f"{goal.id}-{today.isoformat()}")
    drift = rng.uniform(0.85, 1.15)   # ±15% pace
    jitter = rng.uniform(0.95, 1.05)  # ±5% noise
    value = baseline + (expected - baseline) * drift * jitter

    value = _cap(value)
    return Decimal(f"{value:.4f}")


# ── Demo seed ─────────────────────────────────────────────────────────

async def create_sandbox_workspace(
    db: AsyncSession,
    *,
    entity_id: str,
    name: str = "Twitter Growth — Sandbox Demo",
    kind: str = "social_media",
    seed_task_title: str = "Publish your first AI-agent tutorial tweet",
) -> dict:
    """One-shot demo workspace builder.

    Creates everything needed to watch the full pipeline from a single
    button click in the UI:

      * Workspace flagged ``settings.sandbox=true``
      * One Agent template + AgentSubscription (service_key=content_creator)
      * One Goal (10k followers in 6mo)
      * One starter Task in ``status='pending'`` so the auto-trigger
        immediately calls Planner → executor → workspace_chat

    The MD memory layout is left to the existing ``seed_workspace_memory``
    hook (called by ``workspace_setup_service``), so callers that want
    memory should run those after this returns.

    Returns ``{workspace_id, agent_id, subscription_id, goal_id, task_id}``
    so the API can return ids directly to the UI for navigation.
    """
    kind_key = (kind or "").lower()
    if kind_key in LEASING_DEMO_KIND_ALIASES:
        if name == "Twitter Growth — Sandbox Demo":
            name = sandbox_demo_name(kind_key)
        return await _create_leasing_sandbox_workspace(
            db,
            entity_id=entity_id,
            name=name,
            kind="leasing",
            seed_task_title=(
                seed_task_title
                if seed_task_title != "Publish your first AI-agent tutorial tweet"
                else "Prepare a follow-up plan for today’s leasing leads"
            ),
        )

    workspace_id = generate_ulid()
    agent_id = generate_ulid()
    sub_id = generate_ulid()
    goal_id = generate_ulid()
    task_id = generate_ulid()
    channel_config_id = generate_ulid()
    channel_id = generate_ulid()
    conv_id = generate_ulid()
    approval_msg_id = generate_ulid()

    db.add(Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name=name,
        kind=kind,
        operating_model={
            "services": [{
                "service_key": "content_creator",
                "description": "Drafts and posts content; replies to comments.",
                "autonomy_level": "assisted",
            }],
        },
        settings=settings_with_default_workspace_access({
            "sandbox": True,
            "sandbox_started_at": datetime.now(timezone.utc).isoformat(),
        }),
        status="active",
    ))

    db.add(Agent(
        id=agent_id,
        entity_id=entity_id,
        name="Content Creator (sandbox)",
        system_prompt=(
            "You are a content creator for a solo founder. Draft tweets in "
            "the workspace voice (see workspace memory). Keep them short, "
            "concrete, and useful — no hype."
        ),
        config={},
        is_template=False,
        is_public=False,
        source="sandbox_seed",
        status="active",
    ))

    db.add(AgentSubscription(
        id=sub_id,
        entity_id=entity_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        service_key="content_creator",
        status="active",
    ))

    db.add(Goal(
        id=goal_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Reach 10,000 Twitter followers",
        description="Demo goal — values are simulated.",
        metric_key="followers_count",
        target_value=Decimal("10000"),
        baseline_value=Decimal("1000"),
        current_value=Decimal("1000"),
        deadline=date.today() + timedelta(days=180),
        measurement_source={
            "provider": "twitter_x",
            "action": "get_profile_stats",
            "_sandbox": True,
        },
        measurement_cadence="daily",
        priority=2,
        status="active",
        pace_status="unknown",
    ))

    db.add(ChannelConfig(
        id=channel_config_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        channel_type="twitter_x",
        provider="sandbox_social",
        name="Sandbox X account",
        config={"handle": "@sandbox_founder", "sandbox": True},
        credentials={},
        status="active",
    ))
    db.add(Channel(
        id=channel_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        type="twitter_x",
        name="Sandbox X publishing channel",
        config={"channel_config_id": channel_config_id, "sandbox": True},
        agent_id=agent_id,
        agent_subscription_id=sub_id,
        status="active",
    ))

    # Starter task lands in 'pending' so the existing auto-trigger
    # (task_service.update_task hook) fires plan_and_run_task as soon
    # as it sees a status transition. We bypass that hook here by
    # writing the row in 'pending' directly — the dispatch happens
    # the moment a worker picks the task up via run_agent_task or the
    # caller invokes plan_and_run_task explicitly.
    db.add(Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title=seed_task_title,
        description=(
            "Sandbox demo task — Planner will draft a tweet, Executor will "
            "'post' it (simulated). Approve the proposal in chat to watch "
            "the full pipeline."
        ),
        status="pending",
        priority=3,
        task_type="ai_generated",
        details={"seeded_by": "sandbox_demo"},
        owner_service_key="content_creator",
        owner_subscription_id=sub_id,
        delegate_service_keys=["content_creator"],
    ))
    db.add(GoalTaskLink(
        goal_id=goal_id,
        task_id=task_id,
        contribution="direct",
        estimated_impact=Decimal("250.0000"),
    ))
    db.add(Conversation(
        id=conv_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Social growth demo workspace chat",
        channel="workspace",
        scope="workspace_main",
        meta={"demo": True},
    ))
    db.add(Message(
        id=generate_ulid(),
        conversation_id=conv_id,
        role="system",
        content=(
            "Sandbox demo initialized. The content agent drafted a simulated X post; "
            "review the approval card below before anything is published."
        ),
        author_kind="system",
        message_kind="system",
        refs=[{"type": "workspace", "id": workspace_id}],
    ))
    db.add(Message(
        id=approval_msg_id,
        conversation_id=conv_id,
        role="assistant",
        content=(
            "I drafted the first AI-agent tutorial post for the sandbox X account. "
            "Because public publishing is approval-gated, please approve or reject this draft."
        ),
        author_kind="agent",
        author_subscription_id=sub_id,
        message_kind="hitl_request",
        refs=[
            {"type": "task", "id": task_id},
            {"type": "channel_config", "id": channel_config_id},
        ],
        pending_action={
            "kind": "external_message_approval",
            "action_key": "social_post.publish",
            "channel_config_id": channel_config_id,
            "channel_type": "twitter_x",
            "channel_conversation_id": conv_id,
            "chat_id": "sandbox_x_account",
            "sender_id": "sandbox_x_account",
            "agent_subscription_id": sub_id,
            "task_ids": [task_id],
            "reply_text": (
                "AI agents are most useful when they do the boring follow-through: "
                "track the goal, cite the source, ask for approval, then learn from the result."
            ),
            "options": approval_options(),
            "risk_level": "medium",
            "demo": True,
        },
    ))
    db.add(RuntimeEvidence(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        conversation_id=conv_id,
        task_id=task_id,
        evidence_type="chat_run",
        source="sandbox_demo",
        status="blocked",
        summary="Sandbox social post draft is waiting for operator approval.",
        details={
            "task_id": task_id,
            "pending_message_id": approval_msg_id,
            "tool_calls_made": ["workspace_search_knowledge", "social_post.publish"],
            "demo": True,
        },
        metrics={"rounds": 1, "tool_call_count": 2, "total_tokens": 640},
    ))
    db.add(WorkspaceActivity(
        id=generate_ulid(),
        workspace_id=workspace_id,
        entity_id=entity_id,
        event_type="approval_requested",
        summary="Sandbox X post is waiting for operator approval.",
        details={
            "message_id": approval_msg_id,
            "task_id": task_id,
            "action_key": "social_post.publish",
        },
        agent_id=sub_id,
    ))

    await db.flush()

    return {
        "workspace_id": workspace_id,
        "agent_id": agent_id,
        "subscription_id": sub_id,
        "goal_id": goal_id,
        "task_id": task_id,
    }


async def _create_leasing_sandbox_workspace(
    db: AsyncSession,
    *,
    entity_id: str,
    name: str,
    kind: str,
    seed_task_title: str,
) -> dict:
    """Seed a realistic leasing workspace demo.

    This deliberately exercises the same surfaces a live workspace uses:
    goals, services, channels, knowledge nets, chat approvals, work batches,
    runtime evidence, and learning candidates. Nothing here is a separate demo
    schema; every row is production-shaped and safe because
    ``settings.sandbox=true`` plus review-required rules prevent side effects.
    """
    now = datetime.now(timezone.utc)
    deadline = date.today() + timedelta(days=90)
    workspace_id = generate_ulid()

    service_specs = [
        {
            "key": "lead_intake",
            "name": "Lead Intake Agent",
            "description": "Understands renter needs and drafts the first response.",
            "autonomy": "assisted",
            "prompt": (
                "You are the leasing lead intake specialist. Extract renter budget, move-in date, "
                "bed/bath needs, pets, parking, and urgency before recommending units."
            ),
        },
        {
            "key": "unit_recommendation",
            "name": "Unit Recommendation Agent",
            "description": "Matches leads to units using inventory and property policies.",
            "autonomy": "assisted",
            "prompt": (
                "You match renter requirements against unit inventory and policies. Explain tradeoffs "
                "clearly and cite workspace knowledge when recommending units."
            ),
        },
        {
            "key": "tour_scheduling",
            "name": "Tour Scheduling Agent",
            "description": "Prepares tour windows and scheduling handoffs.",
            "autonomy": "review_required",
            "prompt": (
                "You coordinate tour options. Draft scheduling messages, but never send external "
                "messages until the operator approves them."
            ),
        },
        {
            "key": "followup_drafting",
            "name": "Follow-up Drafting Agent",
            "description": "Drafts post-tour and stale-lead follow-ups.",
            "autonomy": "review_required",
            "prompt": (
                "You draft short, helpful follow-ups for leasing prospects. Preserve warmth and never "
                "promise availability without checking the inventory knowledge net."
            ),
        },
        {
            "key": "pipeline_tracking",
            "name": "Pipeline Strategist Agent",
            "description": "Reviews pipeline health and proposes the next batch of work.",
            "autonomy": "assisted",
            "prompt": (
                "You are responsible for leasing pipeline health. Look at current goals, recent task "
                "outcomes, and runtime evidence before proposing the next work batch."
            ),
        },
    ]

    workspace_services = [
        {
            "service_key": spec["key"],
            "name": spec["name"],
            "description": spec["description"],
            "autonomy_level": spec["autonomy"],
            "tool_profile": {
                "always": [
                    "workspace_search",
                    "workspace_search_knowledge",
                    "workspace_create_task",
                    "workspace_post_update",
                ],
                "contextual": [
                    "calendar_scheduling",
                    "email_drafting",
                    "document_generation",
                    "pms_listing_search",
                ],
            },
        }
        for spec in service_specs
    ]

    db.add(Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name=name,
        description=(
            "Sandbox leasing operation that shows how Manor tracks goals, works through tasks, "
            "uses knowledge nets, asks for approval, and learns from runtime evidence."
        ),
        category="Leasing",
        kind=kind,
        operating_context="Demo apartment community with simulated leads, units, and tour windows.",
        primary_work="Respond to renter inquiries, recommend units, schedule tours, and improve pipeline conversion.",
        operating_model={
            "services": workspace_services,
            "goals": [
                {
                    "goal_key": "lead_response_time",
                    "title": "Reply to qualified leasing leads within 2 hours",
                    "metric_key": "avg_draft_response_time_hours",
                    "target_value": 2,
                    "baseline_value": 6,
                    "measurement_source": {"provider": "sandbox_leasing", "action": "leasing.get_response_time"},
                    "cadence": "daily",
                    "owner_service_key": "lead_intake",
                },
                {
                    "goal_key": "lead_to_tour_conversion",
                    "title": "Lift lead-to-tour conversion to 40%",
                    "metric_key": "lead_to_tour_conversion_pct",
                    "target_value": 40,
                    "baseline_value": 22,
                    "measurement_source": {"provider": "sandbox_leasing", "action": "leasing.get_pipeline_stats"},
                    "cadence": "daily",
                    "owner_service_key": "tour_scheduling",
                },
                {
                    "goal_key": "tour_to_application_conversion",
                    "title": "Lift tour-to-application conversion to 30%",
                    "metric_key": "tour_to_application_conversion_pct",
                    "target_value": 30,
                    "baseline_value": 14,
                    "measurement_source": {"provider": "sandbox_leasing", "action": "leasing.get_pipeline_stats"},
                    "cadence": "daily",
                    "owner_service_key": "followup_drafting",
                },
                {
                    "goal_key": "active_pipeline_size",
                    "title": "Maintain at least 50 active qualified leads",
                    "metric_key": "active_qualified_leads",
                    "target_value": 50,
                    "baseline_value": 20,
                    "measurement_source": {"provider": "sandbox_leasing", "action": "leasing.get_pipeline_stats"},
                    "cadence": "daily",
                    "owner_service_key": "pipeline_tracking",
                },
                {
                    "goal_key": "stale_lead_rate",
                    "title": "Reduce stale lead rate below 10%",
                    "metric_key": "stale_lead_rate_pct",
                    "target_value": 10,
                    "baseline_value": 31,
                    "measurement_source": {"provider": "sandbox_leasing", "action": "leasing.get_stale_leads"},
                    "cadence": "daily",
                    "owner_service_key": "followup_drafting",
                },
            ],
            "rules": [
                {
                    "id": "demo_approval_external_messages",
                    "summary": "All external prospect messages require operator approval before sending.",
                    "action_keys": ["external_message.send", "channel.reply"],
                    "enforcement": "review_required",
                },
                {
                    "id": "demo_inventory_check",
                    "summary": "Check the Unit Inventory knowledge net before promising availability or price.",
                    "action_keys": ["leasing.recommend_unit", "external_message.send"],
                    "enforcement": "required_context",
                },
                {
                    "id": "demo_no_destructive_files",
                    "summary": "Agents may create workspace files but must not delete or overwrite files in sandbox demo.",
                    "action_keys": ["workspace_file.delete", "workspace_file.overwrite"],
                    "enforcement": "blocked",
                },
            ],
            "knowledge": {
                "default_group_ids": [],
                "expected_nets": [
                    "Unit Inventory & Availability",
                    "Property FAQ & Policies",
                    "Leasing Playbook & Message Templates",
                ],
            },
            "channel_config": {
                "primary_external_channel": {
                    "channel_type": "webchat",
                    "provider": "manor_public_chat",
                    "name": "Sandbox leasing webchat",
                    "linked_service_key": "lead_intake",
                    "purpose": "Inbound leasing inquiries from renters.",
                },
                "internal_channel": {
                    "channel_type": "internal_chat",
                    "provider": "manor_workspace",
                    "name": "Workspace operator chat",
                    "linked_service_key": "pipeline_tracking",
                    "purpose": "Workspace operator approvals and Strategist reviews.",
                },
            },
            "evaluation": {
                "loop": "After every active work batch completes, Strategist reviews goals, evidence, pending actions, and budget before proposing the next batch.",
                "evidence_window_days": 14,
                "requires_operator_confirmation_for_runtime_changes": True,
            },
            "budget": {
                "monthly_budget_credits": 12000,
                "auto_pause_on_budget": True,
                "stop_when_remaining_credits_below": 500,
            },
        },
        settings=settings_with_default_workspace_access({
            "sandbox": True,
            "demo_kind": "leasing",
            "sandbox_started_at": now.isoformat(),
            "runtime": {
                "strategist_loop": "batch_completion",
                "heartbeat_triggers_strategist": True,
                "user_message_triggers_runtime_resolver": True,
            },
            "execution_policy": {
                "default_execution_mode": "sandbox",
                "approval_required_action_keys": ["external_message.send", "channel.reply"],
            },
        }),
        status="active",
        heartbeat_enabled=True,
        heartbeat_cadence="daily",
        last_heartbeat_at=now - timedelta(hours=18),
        monthly_budget_usd=Decimal("12.000000"),
        monthly_spent_usd=Decimal("1.840000"),
        budget_reset_at=now + timedelta(days=19),
        auto_pause_on_budget=True,
        budget_alert_state="normal",
    ))

    subscriptions: dict[str, tuple[str, str]] = {}
    for spec in service_specs:
        agent_id = generate_ulid()
        sub_id = generate_ulid()
        db.add(Agent(
            id=agent_id,
            entity_id=entity_id,
            name=f"{spec['name']} (sandbox)",
            description=spec["description"],
            system_prompt=spec["prompt"],
            config={
                "demo_kind": "leasing",
                "memory_scope": "workspace",
                "tool_profile": spec["key"],
            },
            is_template=False,
            is_public=False,
            source="sandbox_seed",
            status="active",
            category="workspace",
            tags=["workspace-agent", "sandbox", "leasing"],
        ))
        db.add(AgentSubscription(
            id=sub_id,
            entity_id=entity_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            name=spec["name"],
            service_key=spec["key"],
            custom_prompt=spec["prompt"],
            config={
                "demo_kind": "leasing",
                "autonomy_level": spec["autonomy"],
                "tool_profile": [svc for svc in workspace_services if svc["service_key"] == spec["key"]][0]["tool_profile"],
            },
            status="active",
        ))
        subscriptions[spec["key"]] = (agent_id, sub_id)

    goal_specs = [
        {
            "title": "Reply to qualified leasing leads within 2 hours",
            "description": "Average first-draft response time across inbound rental prospects.",
            "metric_key": "avg_draft_response_time_hours",
            "baseline": "6.0000",
            "current": "3.4000",
            "target": "2.0000",
            "pace": "behind",
            "source_action": "leasing.get_response_time",
            "owner": "lead_intake",
        },
        {
            "title": "Lift lead-to-tour conversion to 40%",
            "description": "Share of qualified leads that book a tour.",
            "metric_key": "lead_to_tour_conversion_pct",
            "baseline": "22.0000",
            "current": "28.0000",
            "target": "40.0000",
            "pace": "at_risk",
            "source_action": "leasing.get_pipeline_stats",
            "owner": "tour_scheduling",
        },
        {
            "title": "Lift tour-to-application conversion to 30%",
            "description": "Share of completed tours that submit an application.",
            "metric_key": "tour_to_application_conversion_pct",
            "baseline": "14.0000",
            "current": "18.0000",
            "target": "30.0000",
            "pace": "behind",
            "source_action": "leasing.get_pipeline_stats",
            "owner": "followup_drafting",
        },
        {
            "title": "Maintain at least 50 active qualified leads",
            "description": "Healthy open pipeline count for the demo property.",
            "metric_key": "active_qualified_leads",
            "baseline": "20.0000",
            "current": "32.0000",
            "target": "50.0000",
            "pace": "on_track",
            "source_action": "leasing.get_pipeline_stats",
            "owner": "pipeline_tracking",
        },
        {
            "title": "Reduce stale lead rate below 10%",
            "description": "Qualified leads without a helpful follow-up in 72 hours.",
            "metric_key": "stale_lead_rate_pct",
            "baseline": "31.0000",
            "current": "18.0000",
            "target": "10.0000",
            "pace": "at_risk",
            "source_action": "leasing.get_stale_leads",
            "owner": "followup_drafting",
        },
    ]

    goal_ids: list[str] = []
    for spec in goal_specs:
        goal_id = generate_ulid()
        goal_ids.append(goal_id)
        db.add(Goal(
            id=goal_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title=spec["title"],
            description=spec["description"],
            metric_key=spec["metric_key"],
            target_value=Decimal(spec["target"]),
            baseline_value=Decimal(spec["baseline"]),
            current_value=Decimal(spec["current"]),
            current_value_updated_at=now - timedelta(hours=3),
            deadline=deadline,
            measurement_source={
                "provider": "sandbox_leasing",
                "action": spec["source_action"],
                "params": {"workspace_id": workspace_id, "owner_service_key": spec["owner"]},
                "_sandbox": True,
            },
            measurement_cadence="daily",
            priority=2,
            status="active",
            pace_status=spec["pace"],
            pace_computed_at=now - timedelta(hours=3),
        ))
        db.add(GoalMeasurement(
            goal_id=goal_id,
            measured_at=now - timedelta(days=7),
            value=Decimal(spec["baseline"]),
            source="simulated",
            meta={"demo": True, "phase": "baseline"},
        ))
        db.add(GoalMeasurement(
            goal_id=goal_id,
            measured_at=now - timedelta(hours=3),
            value=Decimal(spec["current"]),
            source="simulated",
            meta={"demo": True, "phase": "current", "pace_status": spec["pace"]},
        ))

    knowledge_specs = [
        (
            "Unit Inventory & Availability",
            "Current unit list, rent ranges, move-in dates, deposits, and constraints.",
            [
                (
                    "Demo Unit Inventory.md",
                    "Inventory snapshot: Unit 2B is a 1 bed at $2,350 available June 1. Unit 4C is a 2 bed at $3,050 available now. Unit 5A is a studio at $1,980 available June 15. Link to [[Property FAQ & Policies]].",
                    ["Property FAQ & Policies"],
                ),
                (
                    "Demo Pricing Notes.md",
                    "Pricing notes: quote ranges only after checking current availability. Concessions require manager review. Prefer transparent tradeoffs over urgency pressure.",
                    ["Leasing Playbook & Message Templates"],
                ),
            ],
        ),
        (
            "Property FAQ & Policies",
            "Renter-facing answers for pets, parking, deposits, amenities, and qualification.",
            [
                (
                    "Demo Property FAQ.md",
                    "Pets: cats and small dogs allowed with pet rent. Parking: gated garage available. Application fee: $45. Tours: weekdays 10am-6pm, Saturday by appointment. Link to [[Demo Unit Inventory.md]].",
                    ["Demo Unit Inventory.md"],
                ),
                (
                    "Demo Qualification Policy.md",
                    "Qualification basics: income 2.5x rent, standard ID check, refundable deposit subject to screening. Never give legal advice; escalate unusual cases.",
                    ["Leasing Playbook & Message Templates"],
                ),
            ],
        ),
        (
            "Leasing Playbook & Message Templates",
            "Voice, escalation rules, follow-up templates, and conversion experiments.",
            [
                (
                    "Demo Follow-up Templates.md",
                    "Template: acknowledge the requirement, recommend 1-2 matching units, give clear tour windows, and ask one simple next question. All external sends require approval.",
                    ["Unit Inventory & Availability", "Property FAQ & Policies"],
                ),
                (
                    "Demo Leasing Playbook.md",
                    "Operating rule: summarize lead needs before recommending units. If budget is below inventory, suggest closest-fit alternatives. Use friendly, concise messages.",
                    ["Demo Follow-up Templates.md"],
                ),
            ],
        ),
    ]

    for group_name, purpose, docs in knowledge_specs:
        group_id = generate_ulid()
        db.add(DocumentGroup(
            id=group_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            name=group_name,
            settings={
                "kind": "knowledge_net",
                "network_type": "workspace",
                "purpose": purpose,
                "user_manageable": True,
                "auto_created": True,
                "demo": True,
            },
        ))
        for doc_name, content, links in docs:
            doc_id = generate_ulid()
            db.add(Document(
                id=doc_id,
                entity_id=entity_id,
                name=doc_name,
                file_type="md",
                mime_type="text/markdown",
                vector_status=VectorStatus.READY,
                source="sandbox",
                metadata_=merge_document_metadata(
                    origin={"workspace_id": workspace_id},
                    extra={
                        "demo": True,
                        "content_text": content,
                        "wiki_links": links,
                        "knowledge_net": group_name,
                    },
                ),
            ))
            db.add(DocumentGroupMember(document_id=doc_id, group_id=group_id))

    webchat_cc_id = generate_ulid()
    internal_cc_id = generate_ulid()
    public_token = f"demo_{workspace_id.lower()}"
    lead_agent_id, lead_sub_id = subscriptions["lead_intake"]
    pipeline_agent_id, pipeline_sub_id = subscriptions["pipeline_tracking"]
    db.add(ChannelConfig(
        id=webchat_cc_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        channel_type="webchat",
        provider="manor_public_chat",
        name="Sandbox leasing webchat",
        config={
            "public_token": public_token,
            "welcome_message": "Hi! Tell me your move-in date, budget, and bedroom needs.",
            "role": "primary_external",
            "purpose": "Inbound leasing inquiries from renters.",
            "linked_service_key": "lead_intake",
            "login_required": False,
            "notes": "",
            "sandbox": True,
        },
        credentials={},
        status="active",
    ))
    db.add(Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        type="webchat",
        name="Sandbox leasing webchat",
        config={
            "channel_config_id": webchat_cc_id,
            "role": "primary_external",
            "purpose": "Inbound leasing inquiries from renters.",
            "linked_service_key": "lead_intake",
            "public_token": public_token,
        },
        agent_id=lead_agent_id,
        agent_subscription_id=lead_sub_id,
        status="active",
    ))
    db.add(ChannelConfig(
        id=internal_cc_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        channel_type="internal_chat",
        provider="manor_workspace",
        name="Workspace operator chat",
        config={
            "role": "internal",
            "purpose": "Workspace operator approvals and Strategist reviews.",
            "linked_service_key": "pipeline_tracking",
            "login_required": False,
            "notes": "",
            "sandbox": True,
        },
        credentials={},
        status="active",
    ))
    db.add(Channel(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        type="internal_chat",
        name="Workspace operator chat",
        config={
            "channel_config_id": internal_cc_id,
            "role": "internal",
            "purpose": "Workspace operator approvals and Strategist reviews.",
            "linked_service_key": "pipeline_tracking",
        },
        agent_id=pipeline_agent_id,
        agent_subscription_id=pipeline_sub_id,
        status="active",
    ))

    workspace_files_group_id = generate_ulid()
    db.add(DocumentGroup(
        id=workspace_files_group_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        name="Workspace Files",
        settings={
            "workspace_file_bucket": True,
            "kind": "workspace_files",
            "user_manageable": False,
            "demo": True,
            "purpose": "Generated files and runtime artifacts for this workspace.",
        },
    ))

    task_specs = [
        {
            "title": "Audit today’s qualified leasing leads",
            "description": "Summarize lead urgency, budget fit, and missing information.",
            "status": "completed",
            "service": "pipeline_tracking",
            "days": 1,
            "output": {"summary": "12 qualified leads, 4 need same-day replies, 3 likely fit Unit 4C."},
            "artifact": {
                "name": "Daily Lead Audit Summary.md",
                "fs_path": "Workspaces/Leasing Sandbox/artifacts/daily-lead-audit-summary.md",
                "file_type": "md",
                "mime_type": "text/markdown",
                "role": "final",
                "tool_name": "generate_file",
                "content_text": (
                    "# Daily Lead Audit Summary\n\n"
                    "- 12 qualified leads reviewed.\n"
                    "- 4 leads need same-day replies.\n"
                    "- 3 leads likely fit Unit 4C based on budget, bedroom count, and move-in date.\n"
                    "- Maya Chen is the highest-intent lead and should receive a pet-friendly 1-bed recommendation.\n"
                ),
            },
        },
        {
            "title": seed_task_title,
            "description": "Draft follow-ups for the most urgent leads and request operator approval before sending.",
            "status": "in_progress",
            "service": "followup_drafting",
            "days": 0,
            "output": {"summary": "Drafted Maya Chen follow-up and paused for operator approval."},
            "artifact": {
                "name": "Maya Chen Follow-up Draft.md",
                "fs_path": "Workspaces/Leasing Sandbox/artifacts/maya-chen-follow-up-draft.md",
                "file_type": "md",
                "mime_type": "text/markdown",
                "role": "draft",
                "tool_name": "generate_file",
                "content_text": (
                    "# Maya Chen Follow-up Draft\n\n"
                    "Hi Maya — based on your June move-in, cat-friendly requirement, and under-$2,500 "
                    "budget, Unit 2B looks like the best fit. Would Tuesday 11:30am or Wednesday 4pm "
                    "work for a tour?\n\n"
                    "_Requires operator approval before sending._\n"
                ),
            },
        },
        {
            "title": "Recommend units for Maya Chen",
            "description": "Maya wants a pet-friendly 1 bed under $2,500 with a June move-in.",
            "status": "pending",
            "service": "unit_recommendation",
            "days": 0,
            "output": None,
        },
        {
            "title": "Prepare weekly leasing pipeline review",
            "description": "Review goals, runtime evidence, budget, and blocked approvals after this work batch completes.",
            "status": "pending",
            "service": "pipeline_tracking",
            "days": 2,
            "output": None,
        },
    ]

    batch_id = generate_ulid()
    task_ids: list[str] = []
    for spec in task_specs:
        task_id = generate_ulid()
        task_ids.append(task_id)
        agent_id, sub_id = subscriptions[spec["service"]]
        started_at = now - timedelta(days=spec["days"], hours=2) if spec["status"] != "pending" else None
        completed_at = now - timedelta(days=spec["days"], hours=1) if spec["status"] == "completed" else None
        actual_output = dict(spec["output"] or {}) if spec.get("output") else None
        artifact_spec = spec.get("artifact") or {}
        if artifact_spec:
            artifact_doc_id = generate_ulid()
            artifact_ref = {
                "document_id": artifact_doc_id,
                "name": artifact_spec["name"],
                "fs_path": artifact_spec["fs_path"],
                "file_type": artifact_spec["file_type"],
                "artifact_role": artifact_spec["role"],
            }
            actual_output = actual_output or {}
            actual_output["files"] = [artifact_ref]
            actual_output.setdefault("steps", [{
                "key": "generate_artifact",
                "status": "done",
                "result_summary": f"Generated {artifact_spec['name']}",
                "files": [artifact_ref],
            }])
            db.add(Document(
                id=artifact_doc_id,
                entity_id=entity_id,
                name=artifact_spec["name"],
                fs_path=artifact_spec["fs_path"],
                file_size=len(str(artifact_spec.get("content_text") or "").encode("utf-8")) or None,
                file_type=artifact_spec["file_type"],
                mime_type=artifact_spec["mime_type"],
                vector_status=VectorStatus.READY,
                source="agent",
                metadata_=merge_document_metadata(
                    origin={
                        "workspace_id": workspace_id,
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "conversation_id": None,
                        "tool_name": artifact_spec["tool_name"],
                    },
                    artifact={
                        "role": artifact_spec["role"],
                        "storage_scope": "artifact",
                    },
                    extra={
                        "demo": True,
                        "content_text": artifact_spec.get("content_text") or f"Demo generated artifact: {artifact_spec['name']}",
                    },
                ),
            ))
            db.add(DocumentGroupMember(document_id=artifact_doc_id, group_id=workspace_files_group_id))
        db.add(Task(
            id=task_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title=spec["title"],
            description=spec["description"],
            status=spec["status"],
            priority=2 if spec["status"] == "in_progress" else 3,
            task_type="ai_generated",
            agent_id=agent_id,
            details={
                "seeded_by": "sandbox_demo",
                "demo_kind": "leasing",
                "workspace_work_batch_id": batch_id,
                "goal_ids": goal_ids[:2] if spec["service"] in {"lead_intake", "followup_drafting", "unit_recommendation"} else goal_ids,
            },
            started_at=started_at,
            completed_at=completed_at,
            owner_service_key=spec["service"],
            owner_subscription_id=sub_id,
            delegate_service_keys=[spec["service"], "pipeline_tracking"],
            actual_output=actual_output,
        ))
        if spec["service"] in {"lead_intake", "followup_drafting", "unit_recommendation"}:
            db.add(GoalTaskLink(
                goal_id=goal_ids[1],
                task_id=task_id,
                contribution="direct",
                estimated_impact=Decimal("1.5000"),
                actual_impact=Decimal("0.7000") if spec["status"] == "completed" else None,
            ))

    db.add(WorkspaceWorkBatch(
        id=batch_id,
        workspace_id=workspace_id,
        entity_id=entity_id,
        source_kind="sandbox_demo",
        summary="Demo batch: audit leads, draft follow-ups, recommend units, then trigger Strategist review.",
        status="active",
        task_ids=task_ids,
        details={
            "trigger_strategist_when_all_complete": True,
            "demo": True,
            "goal_ids": goal_ids,
        },
    ))

    conv_id = generate_ulid()
    db.add(Conversation(
        id=conv_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Leasing demo workspace chat",
        channel="workspace",
        scope="workspace_main",
        meta={"demo": True},
    ))
    db.add(Message(
        id=generate_ulid(),
        conversation_id=conv_id,
        role="system",
        content=(
            "Sandbox demo initialized. This workspace has leasing goals, knowledge nets, agents, "
            "channels, a work batch, runtime evidence, and one approval waiting below."
        ),
        author_kind="system",
        message_kind="system",
        refs=[{"type": "workspace", "id": workspace_id}],
    ))
    db.add(Message(
        id=generate_ulid(),
        conversation_id=conv_id,
        role="assistant",
        content=(
            "I found Maya Chen as a high-fit lead for Unit 2B. I drafted a concise reply, but the "
            "workspace rule requires your approval before any external message is sent."
        ),
        author_kind="agent",
        author_subscription_id=lead_sub_id,
        message_kind="hitl_request",
        refs=[
            {"type": "task", "id": task_ids[1]},
            {"type": "channel_config", "id": webchat_cc_id},
        ],
        pending_action={
            "kind": "external_message_approval",
            "action_key": "external_message.send",
            "channel_config_id": webchat_cc_id,
            "channel_type": "webchat",
            "channel_conversation_id": conv_id,
            "chat_id": "demo_lead_maya",
            "sender_id": "demo_lead_maya",
            "agent_subscription_id": lead_sub_id,
            "reply_text": (
                "Hi Maya — based on your June move-in, cat-friendly requirement, and under-$2,500 "
                "budget, Unit 2B looks like the best fit. Would Tuesday 11:30am or Wednesday 4pm "
                "work for a tour?"
            ),
            "options": approval_options(),
            "risk_level": "medium",
            "demo": True,
        },
    ))
    db.add(Message(
        id=generate_ulid(),
        conversation_id=conv_id,
        role="assistant",
        content=(
            "Next loop: once the active batch finishes, Strategist will review goal movement, "
            "evidence, unresolved approvals, and remaining budget before proposing the next tasks."
        ),
        author_kind="agent",
        author_subscription_id=pipeline_sub_id,
        message_kind="agent_update",
        refs=[{"type": "workspace_work_batch", "id": batch_id}],
    ))

    evidence_ids: list[str] = []
    for evidence_type, status, summary, details, metrics in [
        (
            "strategist_review",
            "succeeded",
            "Strategist reviewed prior batch outcomes and identified stale-lead follow-up as the highest leverage next move.",
            {"created_task_ids": task_ids, "goal_ids_considered": goal_ids, "budget_checked": True},
            {"rounds": 2, "tool_call_count": 4, "total_tokens": 2840},
        ),
        (
            "task_run",
            "succeeded",
            "Lead audit completed and produced a prioritized list of high-intent renters.",
            {"task_id": task_ids[0], "tool_calls_made": ["workspace_search_knowledge", "workspace_post_update"]},
            {"rounds": 1, "tool_call_count": 2, "total_tokens": 910},
        ),
        (
            "chat_run",
            "blocked",
            "External reply was drafted but blocked by the workspace approval rule.",
            {"task_id": task_ids[1], "tool_calls_made": ["workspace_search_knowledge", "external_message.send"]},
            {"rounds": 1, "tool_call_count": 2, "total_tokens": 1220},
        ),
    ]:
        ev_id = generate_ulid()
        evidence_ids.append(ev_id)
        db.add(RuntimeEvidence(
            id=ev_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            agent_id=pipeline_agent_id if evidence_type == "strategist_review" else lead_agent_id,
            conversation_id=conv_id,
            task_id=details.get("task_id"),
            evidence_type=evidence_type,
            source="sandbox_demo",
            status=status,
            summary=summary,
            details={**details, "demo": True},
            metrics=metrics,
        ))

    db.add(AgentLearningCandidate(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=pipeline_agent_id,
        candidate_type="memory",
        scope="workspace",
        title="Remember leasing approval rule",
        summary=(
            "The workspace repeatedly blocks external leasing replies until the operator approves the exact content."
        ),
        payload={
            "apply_target": "RULES.md",
            "content": "All renter-facing external messages must be shown to the operator and approved before sending.",
            "memory_type": "rule",
            "demo": True,
        },
        evidence_ids=evidence_ids,
        dedupe_key=f"sandbox:{workspace_id}:approval_rule",
        risk_level="low",
        status="proposed",
        confidence=0.86,
        created_by="sandbox_demo",
    ))
    db.add(AgentLearningCandidate(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=lead_agent_id,
        candidate_type="tool_experience",
        scope="agent",
        title="Unit recommendation pattern worked",
        summary=(
            "For leasing leads, the best pattern was: summarize requirements, search Unit Inventory, search FAQ, then draft two tour windows."
        ),
        payload={
            "apply_target": "agent_tool_experience",
            "tools": ["workspace_search_knowledge", "workspace_post_update", "external_message.send"],
            "pattern": "lead_requirements -> inventory_match -> policy_check -> approval_draft",
            "demo": True,
        },
        evidence_ids=evidence_ids,
        dedupe_key=f"sandbox:{workspace_id}:unit_recommendation_pattern",
        risk_level="low",
        status="proposed",
        confidence=0.74,
        created_by="sandbox_demo",
    ))

    for event_type, summary, details in [
        ("workspace_created", "Leasing sandbox workspace created.", {"demo": True}),
        ("knowledge_seeded", "Three workspace Knowledge Nets were seeded for leasing demo.", {"group_count": 3}),
        ("batch_started", "Demo work batch started and will trigger Strategist after all tasks complete.", {"task_ids": task_ids}),
        ("approval_requested", "External renter reply is waiting for operator approval.", {"message_kind": "hitl_request"}),
        ("learning_candidate_created", "Runtime evidence produced reviewable learning candidates.", {"evidence_ids": evidence_ids}),
    ]:
        db.add(WorkspaceActivity(
            id=generate_ulid(),
            workspace_id=workspace_id,
            entity_id=entity_id,
            event_type=event_type,
            summary=summary,
            details=details,
        ))

    await db.flush()

    primary_agent_id, primary_sub_id = subscriptions["lead_intake"]
    return {
        "workspace_id": workspace_id,
        "agent_id": primary_agent_id,
        "subscription_id": primary_sub_id,
        "goal_id": goal_ids[0],
        "task_id": task_ids[0],
    }
