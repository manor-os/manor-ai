"""End-to-end business workspace scenarios.

These tests cover the workspace setup surface that matters for real business
operation: services, custom agents, knowledge, channels, governance,
automations, goals, and task ownership.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from sqlalchemy import select

from packages.core.ai.tools.workspace_agent_tools import _workspace_create_task_handler
from packages.core.governance import check_step_policy, get_policy
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel, DocumentGroup
from packages.core.models.goal import Goal, GoalTaskLink
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.task import Task
from packages.core.models.worker import SubscriptionWorker
from packages.core.models.workspace import (
    AgentSubscription,
    AgentToolBinding,
    ToolDefinition,
    Workspace,
)
from packages.core.services.credit_service import usd_to_credits
from packages.core.services.workspace_setup_service import (
    WorkspaceSetupSession,
    finalize_setup,
)

# Register built-in channel adapters, including webchat, before setup finalizes.
import packages.core.services.channels  # noqa: F401,E402

pytestmark = [pytest.mark.manual, pytest.mark.slow, pytest.mark.cloud]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _service(service_key: str, title: str, description: str) -> dict[str, Any]:
    return {
        "service_key": service_key,
        "title": title,
        "description": description,
    }


def _agent_mapping(
    service_key: str,
    agent_name: str,
    system_prompt: str,
    skill_names: list[str],
    capabilities: list[str],
) -> dict[str, Any]:
    return {
        "service_key": service_key,
        "strategy": "create_custom",
        "create_agent_draft": {
            "agent_name": agent_name,
            "system_prompt": system_prompt,
            "tool_bindings": ["web_search", "generate_file"],
            "business_capabilities": capabilities,
            "skill_bindings": [_slug(name) for name in skill_names],
            "missing_skill_specs": [
                {
                    "name": name,
                    "slug": _slug(name),
                    "description": f"Domain workflow for {name}.",
                    "system_prompt": (
                        "Use workspace context, cite known source material, "
                        "produce review-ready drafts, and flag external actions "
                        "for approval."
                    ),
                    "tools": ["rag", "workspace_agent", "generate_file"],
                }
                for name in skill_names
            ],
        },
    }


def _knowledge(
    name: str,
    purpose: str,
    service_keys: list[str],
) -> dict[str, Any]:
    return {
        "mode": "create_new",
        "name": name,
        "purpose": purpose,
        "linked_service_keys": service_keys,
        "generate_starter_doc": True,
    }


BUSINESS_SCENARIOS: list[dict[str, Any]] = [
    {
        "slug": "consultant",
        "workspace_kind": "consulting_business",
        "name": "Consultant Delivery Workspace",
        "primary_work": "consulting client acquisition, delivery, and account follow-up",
        "operating_context": (
            "Boutique consultant serving strategy clients with discovery, "
            "research-backed recommendations, deliverable production, and "
            "follow-up coordination."
        ),
        "services": [
            _service(
                "client_discovery",
                "Client Discovery",
                "Qualify inbound clients and convert intake notes into scoped opportunities.",
            ),
            _service(
                "strategy_delivery",
                "Strategy Delivery",
                "Research client problems and produce strategy memos or deck outlines.",
            ),
            _service(
                "client_followup",
                "Client Follow-up",
                "Draft status updates, decision logs, and next-step client messages.",
            ),
        ],
        "agent_mappings": [
            _agent_mapping(
                "client_discovery",
                "Consulting Discovery Agent",
                "You qualify consulting opportunities and prepare concise discovery briefs.",
                ["Consulting Discovery Brief"],
                ["web.safe_search", "workspace.search", "file.write", "external.message"],
            ),
            _agent_mapping(
                "strategy_delivery",
                "Strategy Delivery Agent",
                "You create research-backed consulting recommendations and deliverable drafts.",
                ["Strategy Recommendation Memo"],
                ["web.safe_search", "workspace.search", "file.write"],
            ),
            _agent_mapping(
                "client_followup",
                "Client Follow-up Agent",
                "You draft client-facing follow-ups while respecting approval rules.",
                ["Client Follow-up Drafting"],
                ["workspace.search", "file.write", "external.email", "external.message"],
            ),
        ],
        "knowledge_attachments": [
            _knowledge(
                "Consulting Offer Library",
                "Service positioning, discovery questions, and packaged offers.",
                ["client_discovery", "strategy_delivery"],
            ),
            _knowledge(
                "Client Delivery Notes",
                "Active client notes, decision logs, deliverable standards, and follow-up history.",
                ["strategy_delivery", "client_followup"],
            ),
        ],
        "goals": [
            {
                "goal_key": "qualified-leads",
                "title": "Qualify consulting leads",
                "description": "Turn inbound prospects into scoped opportunities.",
                "metric_key": "qualified_leads",
                "target": 8,
                "cadence": "monthly",
            },
            {
                "goal_key": "deliverables",
                "title": "Prepare client deliverables",
                "description": "Produce client-ready strategy memos and deck outlines.",
                "metric_key": "client_deliverables",
                "target": 6,
                "cadence": "monthly",
            },
            {
                "goal_key": "followups",
                "title": "Send client follow-ups",
                "description": "Keep active clients moving with weekly next-step messages.",
                "metric_key": "client_followups",
                "target": 12,
                "cadence": "monthly",
            },
        ],
        "rules": [
            {
                "title": "Approve client-facing messages",
                "description": "Client emails and external messages must be reviewed before sending.",
                "rule_type": "hitl_required",
                "action_patterns": ["email.send", "external_message.send"],
            },
            {
                "title": "Never delete client source material",
                "description": "Client files and research source material may not be deleted by agents.",
                "rule_type": "never_allow",
                "action_patterns": ["workspace.file.delete"],
            },
        ],
        "flagged_integrations": [
            {
                "provider": "gmail",
                "reason": "Client follow-up emails need a connected account.",
                "linked_service_key": "client_followup",
            },
            {
                "provider": "slack",
                "reason": "Client status updates may require a team channel.",
                "linked_service_key": "client_followup",
            },
        ],
        "channel_config": {
            "channels": [
                {
                    "role": "client_intake",
                    "channel_type": "webchat",
                    "provider": "webchat",
                    "name": "Consulting Intake Chat",
                    "purpose": "Capture new client discovery details.",
                    "linked_service_key": "client_discovery",
                    "login_required": False,
                }
            ]
        },
        "automations": [
            {
                "automation_key": "weekly-client-status",
                "title": "Weekly client status review",
                "description": "Summarize active engagements and unresolved blockers.",
                "trigger": {"type": "cron", "cron": "0 16 * * FRI"},
                "cron_expr": "0 16 * * 5",
                "service_key": "client_followup",
            },
            {
                "automation_key": "monday-deliverable-plan",
                "title": "Monday deliverable plan",
                "description": "Plan the week's client deliverables and research needs.",
                "trigger": {"type": "cron", "cron": "0 9 * * MON"},
                "cron_expr": "0 9 * * 1",
                "service_key": "strategy_delivery",
            },
        ],
        "policy_checks": [
            ("email.send", "medium", False, True),
            ("workspace.file.delete", "medium", False, False),
        ],
        "task": {
            "title": "Draft a discovery-to-proposal brief for a new strategy client",
            "description": "Use intake notes and market research to prepare a scoped proposal brief.",
            "owner_service_key": "strategy_delivery",
            "goal_key": "client_deliverables",
            "required_capabilities": ["web.safe_search", "workspace.search", "file.write"],
        },
    },
    {
        "slug": "wechat-video-account",
        "workspace_kind": "wechat_video_business",
        "name": "WeChat Video Account Growth Workspace",
        "primary_work": "WeChat Channels content planning, publishing preparation, and audience follow-up",
        "operating_context": (
            "A small content business running a WeChat Channels video account, "
            "from topic research through scripts, calendar planning, and comment triage."
        ),
        "services": [
            _service(
                "topic_research",
                "Topic Research",
                "Track market themes and collect video topic angles.",
            ),
            _service(
                "script_storyboard",
                "Script and Storyboard",
                "Turn selected topics into short-form scripts and storyboard notes.",
            ),
            _service(
                "wechat_channel_ops",
                "WeChat Channel Operations",
                "Prepare posting packs and triage comments or direct messages.",
            ),
        ],
        "agent_mappings": [
            _agent_mapping(
                "topic_research",
                "Video Topic Research Agent",
                "You find topical angles for a WeChat Channels account.",
                ["WeChat Channels Topic Radar"],
                ["web.safe_search", "workspace.search", "file.write"],
            ),
            _agent_mapping(
                "script_storyboard",
                "Video Script Agent",
                "You write concise short-video scripts and storyboard directions.",
                ["WeChat Channels Script Planner"],
                ["workspace.search", "file.write"],
            ),
            _agent_mapping(
                "wechat_channel_ops",
                "WeChat Channel Ops Agent",
                "You prepare posting packs and comment triage drafts for human approval.",
                ["Video Account Content Calendar", "Comment Follow-up Triage"],
                ["workspace.search", "file.write", "external.social", "external.message"],
            ),
        ],
        "knowledge_attachments": [
            _knowledge(
                "Video Brand Playbook",
                "Audience, positioning, content pillars, tone, and video style rules.",
                ["topic_research", "script_storyboard"],
            ),
            _knowledge(
                "Publishing and Comment Ops",
                "Posting checklist, approval notes, comment triage patterns, and channel history.",
                ["wechat_channel_ops"],
            ),
        ],
        "goals": [
            {
                "goal_key": "topic-backlog",
                "title": "Maintain video topic backlog",
                "description": "Keep a ranked list of timely video topic opportunities.",
                "metric_key": "topic_backlog",
                "target": 30,
                "cadence": "monthly",
            },
            {
                "goal_key": "script-output",
                "title": "Produce scripts",
                "description": "Create ready-to-review scripts and storyboards.",
                "metric_key": "script_output",
                "target": 20,
                "cadence": "monthly",
            },
            {
                "goal_key": "comment-triage",
                "title": "Triage comments",
                "description": "Prepare timely response drafts for audience engagement.",
                "metric_key": "comment_triage",
                "target": 80,
                "cadence": "monthly",
            },
        ],
        "rules": [
            {
                "title": "Approve social publishing",
                "description": "Video account posts must be approved before publishing.",
                "rule_type": "hitl_required",
                "action_patterns": ["social_post.publish", "external_message.send"],
            },
            {
                "title": "Never delete published social posts",
                "description": "Agents cannot delete published content.",
                "rule_type": "never_allow",
                "action_patterns": ["social_post.delete"],
            },
        ],
        "flagged_integrations": [
            {
                "provider": "wechat_personal",
                "reason": "WeChat Channels publishing and inbox operations require a connected operator account.",
                "linked_service_key": "wechat_channel_ops",
            },
            {
                "provider": "wechat_official",
                "reason": "Official-account audience operations require connected credentials.",
                "linked_service_key": "wechat_channel_ops",
            },
        ],
        "channel_config": {
            "channels": [
                {
                    "role": "audience_inbox",
                    "channel_type": "webchat",
                    "provider": "webchat",
                    "name": "Video Account Intake Chat",
                    "purpose": "Collect topic requests and audience questions before social accounts are connected.",
                    "linked_service_key": "wechat_channel_ops",
                    "login_required": False,
                }
            ]
        },
        "automations": [
            {
                "automation_key": "daily-topic-scan",
                "title": "Daily topic scan",
                "description": "Collect timely ideas and rank them against the content pillars.",
                "trigger": {"type": "cron", "cron": "0 9 * * *"},
                "cron_expr": "0 9 * * *",
                "service_key": "topic_research",
            },
            {
                "automation_key": "weekly-video-calendar",
                "title": "Weekly video calendar",
                "description": "Prepare next week's scripts, posting notes, and approval queue.",
                "trigger": {"type": "cron", "cron": "0 10 * * MON"},
                "cron_expr": "0 10 * * 1",
                "service_key": "script_storyboard",
            },
        ],
        "policy_checks": [
            ("social_post.publish", "high", False, True),
            ("social_post.delete", "medium", False, False),
        ],
        "task": {
            "title": "Create next week's WeChat Channels script and posting pack",
            "description": "Use the content pillars to prepare five scripts and review notes.",
            "owner_service_key": "script_storyboard",
            "goal_key": "script_output",
            "required_capabilities": ["workspace.search", "file.write"],
        },
    },
    {
        "slug": "social-media-ops",
        "workspace_kind": "social_media_operations",
        "name": "Social Media Account Operations Workspace",
        "primary_work": "cross-platform content calendar, community management, and performance reporting",
        "operating_context": (
            "An operator managing brand social accounts across planning, publishing preparation, "
            "community response, and reporting."
        ),
        "services": [
            _service(
                "content_calendar",
                "Content Calendar",
                "Plan platform-specific posts and campaign themes.",
            ),
            _service(
                "community_management",
                "Community Management",
                "Triage comments, direct messages, and moderation decisions.",
            ),
            _service(
                "performance_reporting",
                "Performance Reporting",
                "Summarize channel performance and recommend adjustments.",
            ),
        ],
        "agent_mappings": [
            _agent_mapping(
                "content_calendar",
                "Social Calendar Agent",
                "You maintain campaign calendars and draft platform-specific post packs.",
                ["Cross-platform Draft Pack"],
                ["web.safe_search", "workspace.search", "file.write", "external.social"],
            ),
            _agent_mapping(
                "community_management",
                "Community Ops Agent",
                "You triage audience interactions and draft responses for approval.",
                ["Community Reply Triage"],
                ["workspace.search", "file.write", "external.message", "external.social"],
            ),
            _agent_mapping(
                "performance_reporting",
                "Social Reporting Agent",
                "You convert social metrics into concise performance reports and recommendations.",
                ["Social Analytics Brief"],
                ["workspace.search", "file.write"],
            ),
        ],
        "knowledge_attachments": [
            _knowledge(
                "Social Brand System",
                "Brand voice, audience segments, campaign themes, and platform rules.",
                ["content_calendar", "community_management"],
            ),
            _knowledge(
                "Performance History",
                "Past reports, content experiments, metrics notes, and learnings.",
                ["performance_reporting", "content_calendar"],
            ),
        ],
        "goals": [
            {
                "goal_key": "calendar-coverage",
                "title": "Maintain content coverage",
                "description": "Keep the next two weeks of social content drafted.",
                "metric_key": "calendar_coverage",
                "target": 14,
                "cadence": "weekly",
            },
            {
                "goal_key": "response-drafts",
                "title": "Draft community responses",
                "description": "Prepare audience response drafts for review.",
                "metric_key": "response_drafts",
                "target": 100,
                "cadence": "monthly",
            },
            {
                "goal_key": "weekly-report",
                "title": "Produce weekly performance report",
                "description": "Generate a weekly performance brief with next actions.",
                "metric_key": "weekly_report",
                "target": 1,
                "cadence": "weekly",
            },
        ],
        "rules": [
            {
                "title": "Approve publishing and replies",
                "description": "Publishing and outbound community replies require human approval.",
                "rule_type": "hitl_required",
                "action_patterns": [
                    "social_post.publish",
                    "social_post.mutate",
                    "external_message.send",
                ],
            },
            {
                "title": "Never delete posts",
                "description": "Agents cannot delete social posts.",
                "rule_type": "never_allow",
                "action_patterns": ["social_post.delete"],
            },
        ],
        "flagged_integrations": [
            {
                "provider": "twitter_x",
                "reason": "X publishing and inbox operations require connected account access.",
                "linked_service_key": "content_calendar",
            },
            {
                "provider": "linkedin",
                "reason": "LinkedIn publishing and analytics require connected account access.",
                "linked_service_key": "content_calendar",
            },
            {
                "provider": "instagram",
                "reason": "Instagram publishing and community management require connected account access.",
                "linked_service_key": "community_management",
            },
        ],
        "channel_config": {
            "channels": [
                {
                    "role": "community_intake",
                    "channel_type": "webchat",
                    "provider": "webchat",
                    "name": "Community Intake Chat",
                    "purpose": "Collect social community issues before platform accounts are connected.",
                    "linked_service_key": "community_management",
                    "login_required": False,
                }
            ]
        },
        "automations": [
            {
                "automation_key": "daily-inbox-triage",
                "title": "Daily inbox triage",
                "description": "Summarize audience messages and response drafts.",
                "trigger": {"type": "cron", "cron": "0 11 * * *"},
                "cron_expr": "0 11 * * *",
                "service_key": "community_management",
            },
            {
                "automation_key": "weekly-performance-report",
                "title": "Weekly performance report",
                "description": "Create a weekly metrics brief and recommended calendar changes.",
                "trigger": {"type": "cron", "cron": "0 15 * * FRI"},
                "cron_expr": "0 15 * * 5",
                "service_key": "performance_reporting",
            },
        ],
        "policy_checks": [
            ("social_post.publish", "high", False, True),
            ("social_post.delete", "medium", False, False),
        ],
        "task": {
            "title": "Prepare a cross-platform campaign calendar and review queue",
            "description": "Draft two weeks of posts and identify items that need approval.",
            "owner_service_key": "content_calendar",
            "goal_key": "calendar_coverage",
            "required_capabilities": ["web.safe_search", "workspace.search", "file.write"],
        },
    },
]


def _business_workspace_fields(scenario: dict[str, Any], budget_credits: int) -> dict[str, Any]:
    return {
        "kind": scenario["workspace_kind"],
        "name": scenario["name"],
        "primary_work": scenario["primary_work"],
        "operating_context": scenario["operating_context"],
        "services": scenario["services"],
        "agent_mappings": scenario["agent_mappings"],
        "knowledge_attachments": scenario["knowledge_attachments"],
        "knowledge_policy": {
            "auto_search": True,
            "citation_required": True,
            "retrieval_mode": "auto",
        },
        "goals": scenario["goals"],
        "rules": scenario["rules"],
        "governance_policy": {
            "max_risk_level": "high",
            "default_action": "allow",
        },
        "flagged_integrations": scenario["flagged_integrations"],
        "channel_config": scenario["channel_config"],
        "automations": scenario["automations"],
        "budget_policy": {
            "monthly_budget_credits": budget_credits,
            "auto_pause_on_budget": True,
        },
        "heartbeat_cadence": "0 9 * * 1-5",
    }


async def _create_business_workspace(db_session, scenario: dict[str, Any]):
    entity_id = generate_ulid()
    user_id = generate_ulid()
    budget_credits = usd_to_credits(250)
    setup_session = WorkspaceSetupSession(
        entity_id=entity_id,
        fields=_business_workspace_fields(scenario, budget_credits),
        messages=[],
        ready=True,
        missing=[],
        user_id=user_id,
    )

    workspace_id = await finalize_setup(setup_session, db_session)
    await db_session.commit()
    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()
    return workspace, entity_id, user_id, budget_credits


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    BUSINESS_SCENARIOS,
    ids=[scenario["slug"] for scenario in BUSINESS_SCENARIOS],
)
async def test_business_workspaces_can_operate_end_to_end(db_session, scenario):
    workspace, entity_id, user_id, budget_credits = await _create_business_workspace(
        db_session,
        scenario,
    )
    workspace_id = workspace.id

    assert workspace.id == workspace_id
    assert workspace.entity_id == entity_id
    assert workspace.kind == scenario["workspace_kind"]
    assert workspace.name == scenario["name"]
    assert workspace.primary_work == scenario["primary_work"]
    assert workspace.heartbeat_enabled is True
    assert workspace.heartbeat_cadence == "0 9 * * 1-5"
    assert workspace.operating_model["budget_policy"]["monthly_budget_credits"] == budget_credits
    assert float(workspace.monthly_budget_usd) == 250.0
    assert workspace.auto_pause_on_budget is True

    persisted_workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()
    operating_model = persisted_workspace.operating_model
    assert [service["service_key"] for service in operating_model["services"]] == [
        service["service_key"] for service in scenario["services"]
    ]
    assert {goal["metric_key"] for goal in operating_model["goals"]} == {
        goal["metric_key"] for goal in scenario["goals"]
    }

    await _assert_custom_agent_subscriptions(db_session, workspace_id, scenario)
    await _assert_knowledge(db_session, workspace_id, scenario)
    await _assert_channels_and_integrations(db_session, workspace_id, scenario)
    await _assert_governance(db_session, workspace_id, scenario)
    goals_by_metric = await _assert_goals(db_session, workspace_id, scenario)
    await _assert_automations(db_session, workspace_id, scenario)
    await _assert_task_creation(
        db_session,
        workspace_id,
        entity_id,
        user_id,
        scenario,
        goals_by_metric,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    BUSINESS_SCENARIOS,
    ids=[scenario["slug"] for scenario in BUSINESS_SCENARIOS],
)
async def test_business_workspace_strategist_context_reflects_business_state(
    db_session,
    scenario,
):
    from packages.core.ai.runtime import (
        runtime_strategist_system_prompt,
        runtime_strategist_user_prompt,
    )
    from packages.core.strategist.context import gather_context

    workspace, _, _, _ = await _create_business_workspace(db_session, scenario)
    ctx = await gather_context(db_session, workspace, trigger="manual")

    expected_service_keys = sorted(service["service_key"] for service in scenario["services"])
    assert ctx.allowed_service_keys == expected_service_keys
    assert {subscription.service_key for subscription in ctx.subscriptions} == set(expected_service_keys)
    assert {goal.metric_key for goal in ctx.goals} == {goal["metric_key"] for goal in scenario["goals"]}
    assert {"no_agents", "no_goals", "no_channels"}.isdisjoint(ctx.missing_setup)
    assert "no_integrations" in ctx.missing_setup

    assert any(
        channel["channel_type"] == "webchat" and channel["built_in"] is True and channel["linked_service_key"]
        for channel in ctx.configured_channels
    )
    assert {net["name"] for net in ctx.knowledge_nets} == {
        attachment["name"] for attachment in scenario["knowledge_attachments"]
    }

    policy = ctx.governance_policy or {}
    required_actions = set(policy.get("hitl_required_actions") or [])
    blocked_actions = set(policy.get("never_allow_actions") or [])
    for rule in scenario["rules"]:
        patterns = set(rule["action_patterns"])
        if rule["rule_type"] == "hitl_required":
            assert patterns.issubset(required_actions)
        if rule["rule_type"] == "never_allow":
            assert patterns.issubset(blocked_actions)

    system_prompt = runtime_strategist_system_prompt(ctx)
    user_prompt = runtime_strategist_user_prompt(
        ctx,
        review_id="rv_business_strategist_check",
    )

    for service_key in expected_service_keys:
        assert service_key in system_prompt
    for goal in scenario["goals"]:
        assert goal["title"] in user_prompt
    assert "# Workspace setup incomplete" in user_prompt
    assert "No external integrations are configured yet" in user_prompt
    assert "# Configured channels" in user_prompt
    assert "webchat" in user_prompt
    assert "# Governance policy" in user_prompt
    assert "HITL required actions" in user_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    BUSINESS_SCENARIOS,
    ids=[scenario["slug"] for scenario in BUSINESS_SCENARIOS],
)
async def test_business_workspace_strategist_review_persists_business_proposal(
    db_session,
    monkeypatch,
    scenario,
):
    from packages.core.strategist import service as strategist_service
    from packages.core.strategist.proposal import Deliverable, EstimatedImpact, Proposal, ProposedTask

    workspace, _, _, _ = await _create_business_workspace(db_session, scenario)
    task_spec = scenario["task"]
    goals = (await db_session.execute(select(Goal).where(Goal.workspace_id == workspace.id))).scalars().all()
    goals_by_metric = {goal.metric_key: goal for goal in goals}
    goal_id = goals_by_metric[task_spec["goal_key"]].id
    owner_service_key = task_spec["owner_service_key"]
    task_title = f"Strategist business check for {scenario['slug']}"

    async def _fake_generate_proposal(ctx, *, review_id: str, db=None):
        assert ctx.workspace.id == workspace.id
        assert owner_service_key in ctx.allowed_service_keys
        assert "no_integrations" in ctx.missing_setup
        return Proposal(
            review_id=review_id,
            summary=f"Advance {scenario['name']} with one review-ready task.",
            tasks=[
                ProposedTask(
                    deliverables=[
                        Deliverable(
                            name="result",
                            kind="value",
                            shape="TextResult",
                            acceptance="task output produced",
                            usage="reviewed by operator",
                        )
                    ],
                    task_key="business_next_step",
                    title=task_title,
                    description=(
                        "Prepare the next business work product using workspace "
                        "knowledge and current goals. Keep the output internal "
                        "and review-ready because external actions require approval."
                    ),
                    owner_service_key=owner_service_key,
                    delegate_service_keys=[owner_service_key],
                    required_capabilities=task_spec["required_capabilities"],
                    priority=4,
                    estimated_impact=EstimatedImpact(
                        goal_id=goal_id,
                        metric_delta=1.0,
                        rationale="Moves the scenario's tracked business metric.",
                    ),
                    rationale="The workspace is configured enough for internal draft work.",
                    expected_output={
                        "type": "object",
                        "properties": {
                            "brief": {
                                "type": "string",
                                "description": "Review-ready business work product.",
                            }
                        },
                        "required": ["brief"],
                    },
                )
            ],
            notes="Generated by business scenario strategist test.",
        )

    posted_cards: list[dict[str, Any]] = []

    async def _fake_post_proposal_chat(workspace_arg, proposal, task_ids, *, auto_approved=False):
        assert auto_approved is False
        posted_cards.append(
            {
                "workspace_id": workspace_arg.id,
                "review_id": proposal.review_id,
                "task_ids": list(task_ids),
            }
        )

    monkeypatch.setattr(strategist_service, "generate_proposal", _fake_generate_proposal)
    monkeypatch.setattr(strategist_service, "_post_proposal_chat", _fake_post_proposal_chat)

    result = await strategist_service.run_review(
        db_session,
        workspace.id,
        trigger="manual",
        skip_if_open_proposals=False,
    )

    assert result["workspace_id"] == workspace.id
    assert result["task_count"] == 1
    assert result["task_ids"]
    assert posted_cards == [
        {
            "workspace_id": workspace.id,
            "review_id": result["review_id"],
            "task_ids": result["task_ids"],
        }
    ]

    task = (await db_session.execute(select(Task).where(Task.id == result["task_ids"][0]))).scalar_one()
    assert task.status == "proposed"
    assert task.task_type == "ai_generated"
    assert task.title == task_title
    assert task.owner_service_key == owner_service_key
    assert task.owner_subscription_id
    assert task.delegate_service_keys == [owner_service_key]
    assert task.details["strategist_review_id"] == result["review_id"]
    assert task.details["strategist_task_key"] == "business_next_step"
    assert task.details["runtime_context"]["required_capabilities"] == task_spec["required_capabilities"]
    assert task.expected_output["required"] == ["brief"]

    link = (
        await db_session.execute(
            select(GoalTaskLink).where(
                GoalTaskLink.goal_id == goal_id,
                GoalTaskLink.task_id == task.id,
            )
        )
    ).scalar_one()
    assert link.contribution == "direct"
    assert float(link.estimated_impact) == 1.0


async def _assert_custom_agent_subscriptions(db_session, workspace_id: str, scenario):
    subscriptions = (
        (await db_session.execute(select(AgentSubscription).where(AgentSubscription.workspace_id == workspace_id)))
        .scalars()
        .all()
    )
    subscriptions_by_service = {subscription.service_key: subscription for subscription in subscriptions}

    expected_service_keys = {service["service_key"] for service in scenario["services"]}
    assert set(subscriptions_by_service) == expected_service_keys

    workers = (
        (
            await db_session.execute(
                select(SubscriptionWorker).where(
                    SubscriptionWorker.subscription_id.in_([subscription.id for subscription in subscriptions])
                )
            )
        )
        .scalars()
        .all()
    )
    assert {worker.subscription_id for worker in workers} == {subscription.id for subscription in subscriptions}

    for mapping in scenario["agent_mappings"]:
        service_key = mapping["service_key"]
        subscription = subscriptions_by_service[service_key]
        draft = mapping["create_agent_draft"]

        assert subscription.agent_id
        assert subscription.custom_prompt
        assert scenario["primary_work"] in subscription.custom_prompt
        assert scenario["operating_context"] in subscription.custom_prompt

        tool_rows = (
            (
                await db_session.execute(
                    select(ToolDefinition.name)
                    .join(AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id)
                    .where(AgentToolBinding.agent_id == subscription.agent_id)
                )
            )
            .scalars()
            .all()
        )
        assert {"manor", "workspace_agent", "rag", "generate_file", "web_search"}.issubset(set(tool_rows))
        assert "invoke_skill" in set(tool_rows)

        skill_slugs = set(draft["skill_bindings"])
        skill_rows = (await db_session.execute(select(Skill).where(Skill.slug.in_(skill_slugs)))).scalars().all()
        assert {skill.slug for skill in skill_rows} == skill_slugs

        skill_bindings = (
            (
                await db_session.execute(
                    select(AgentSkillBinding).where(AgentSkillBinding.agent_id == subscription.agent_id)
                )
            )
            .scalars()
            .all()
        )
        assert {binding.skill_id for binding in skill_bindings} == {skill.id for skill in skill_rows}


async def _assert_knowledge(db_session, workspace_id: str, scenario):
    groups = (
        (await db_session.execute(select(DocumentGroup).where(DocumentGroup.workspace_id == workspace_id)))
        .scalars()
        .all()
    )
    group_names = {group.name for group in groups}
    assert group_names == {attachment["name"] for attachment in scenario["knowledge_attachments"]}

    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()
    knowledge_model = workspace.operating_model["knowledge"]
    assert set(knowledge_model["default_group_ids"]) == {group.id for group in groups}
    assert set(knowledge_model["group_purposes"].values()) == {
        attachment["purpose"] for attachment in scenario["knowledge_attachments"]
    }
    assert knowledge_model["auto_search"] is True
    assert knowledge_model["citation_required"] is True


async def _assert_channels_and_integrations(db_session, workspace_id: str, scenario):
    configs = (
        (await db_session.execute(select(ChannelConfig).where(ChannelConfig.workspace_id == workspace_id)))
        .scalars()
        .all()
    )
    assert len(configs) == 1
    config = configs[0]
    expected_channel = scenario["channel_config"]["channels"][0]
    assert config.provider == "webchat"
    assert config.channel_type == "webchat"
    assert config.config["role"] == expected_channel["role"]
    assert config.config["public_token"]
    assert config.status == "active"

    channels = (
        (
            await db_session.execute(
                select(Channel).where(
                    Channel.workspace_id == workspace_id,
                    Channel.config["channel_config_id"].as_string() == config.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(channels) == 1
    assert channels[0].type == "webchat"
    assert channels[0].agent_subscription_id

    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()
    flagged = workspace.settings["flagged_integrations"]
    actual_or_covered = {
        value for item in flagged for value in (item.get("provider"), item.get("covered_provider")) if value
    }
    assert actual_or_covered.issuperset({item["provider"] for item in scenario["flagged_integrations"]})


async def _assert_governance(db_session, workspace_id: str, scenario):
    policy = await get_policy(db_session, workspace_id)
    assert policy is not None

    for action_key, risk_level, expected_allowed, expected_hitl in scenario["policy_checks"]:
        outcome = await check_step_policy(
            db_session,
            workspace_id=workspace_id,
            kind="agent",
            action_key=action_key,
            risk_level=risk_level,
        )
        assert outcome.allowed is expected_allowed
        assert outcome.pause_for_hitl is expected_hitl


async def _assert_goals(db_session, workspace_id: str, scenario):
    goals = (await db_session.execute(select(Goal).where(Goal.workspace_id == workspace_id))).scalars().all()
    goals_by_metric = {goal.metric_key: goal for goal in goals}

    assert set(goals_by_metric) == {goal["metric_key"] for goal in scenario["goals"]}
    for expected in scenario["goals"]:
        goal = goals_by_metric[expected["metric_key"]]
        assert goal.title == expected["title"]
        assert goal.target_value == float(expected["target"])
        assert goal.measurement_cadence == expected["cadence"]

    return goals_by_metric


async def _assert_automations(db_session, workspace_id: str, scenario):
    expected_job_ids = {
        f"wa:{workspace_id}:{_slug(automation['automation_key'])}" for automation in scenario["automations"]
    }
    jobs = (
        (await db_session.execute(select(ScheduledJob).where(ScheduledJob.job_id.in_(expected_job_ids))))
        .scalars()
        .all()
    )
    assert {job.job_id for job in jobs} == expected_job_ids

    jobs_by_id = {job.job_id: job for job in jobs}
    for automation in scenario["automations"]:
        job_id = f"wa:{workspace_id}:{_slug(automation['automation_key'])}"
        job = jobs_by_id[job_id]
        assert job.workspace_id == workspace_id
        assert job.execution_type == "agent"
        assert job.schedule_kind == "cron"
        assert job.cron_expr == automation["cron_expr"]
        assert automation["description"] in (job.payload_message or "")
        assert f"Responsible service: {automation['service_key']}." in (job.payload_message or "")


async def _assert_task_creation(
    db_session,
    workspace_id: str,
    entity_id: str,
    user_id: str,
    scenario,
    goals_by_metric,
):
    task_spec = scenario["task"]
    goal = goals_by_metric[task_spec["goal_key"]]
    goal_id = goal.id

    result = json.loads(
        await _workspace_create_task_handler(
            workspace_id=workspace_id,
            entity_id=entity_id,
            title=task_spec["title"],
            description=task_spec["description"],
            owner_service_key=task_spec["owner_service_key"],
            goal_ids=[goal_id],
            required_capabilities=task_spec["required_capabilities"],
            runtime_instructions=(
                "Prepare a review-ready work product, cite workspace knowledge, and pause before any external action."
            ),
            user_id=user_id,
        )
    )

    assert result["created"] is True, result
    assert result["owner_resolution"]["owner_service_key"] == task_spec["owner_service_key"]
    assert result["goal_links"]["goal_ids"] == [goal_id]
    task_id = result["task"]["id"]

    await db_session.rollback()
    task = (await db_session.execute(select(Task).where(Task.id == task_id))).scalar_one()
    assert task.workspace_id == workspace_id
    assert task.owner_service_key == task_spec["owner_service_key"]
    assert task.owner_subscription_id
    assert task.details["runtime_context"]["required_capabilities"] == task_spec["required_capabilities"]
    assert task.details["goal_ids"] == [goal_id]
