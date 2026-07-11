"""Default automation skills seeded when a workspace is created.

Ported from manor-multi-agent default_operation_automation_skills.py.
These are prompt-based skills that use the existing ``manor`` composite tool
to gather data from the workspace and produce operational reports.
"""
from __future__ import annotations

import logging
from typing import List

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.services.skill_service import create_skill, get_skill_by_slug

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Skill definitions
# ---------------------------------------------------------------------------

_WORKSPACE_SKILLS = [
    {
        "slug": "workspace-task-analysis",
        "name": "Workspace Task Analysis",
        "display_name": "Daily Task Analysis",
        "description": (
            "Analyze the previous day of workspace work: backlog movement, "
            "completed tasks, blockers, and drift.  Used by daily task "
            "analysis automations."
        ),
        "tags": ["workspace", "automation", "daily-report", "tasks"],
        "system_prompt": (
            "# Workspace Task Analysis\n\n"
            "You are a workspace automation skill.  Your job is to produce a "
            "concise daily task analysis report for the workspace.\n\n"
            "## Workflow\n"
            "1. Use the `manor` tool to fetch tasks, activities, and backlog "
            "data for the workspace from the previous local day.\n"
            "2. Summarize: completed work, slipped work, blocker trends, and "
            "today's most important follow-ups.\n"
            "3. Keep the report concise and operational -- bullet points are "
            "preferred over prose.\n"
            "4. If delivery is requested, send the result to the specified "
            "channel or recipients.\n\n"
            "## Guardrails\n"
            "- Default reporting window is the previous local day unless the "
            "automation payload says otherwise.\n"
            "- Do not fabricate task data that is not supported by the context."
        ),
    },
    {
        "slug": "workspace-health-report",
        "name": "Workspace Health Report",
        "display_name": "Automation Health Report",
        "description": (
            "Summarize automation reliability and service health for the "
            "workspace: missed runs, warning signals, stale state."
        ),
        "tags": ["workspace", "automation", "daily-report", "health"],
        "system_prompt": (
            "# Workspace Health Report\n\n"
            "You are a workspace automation skill.  Your job is to produce a "
            "runtime health report for the workspace.\n\n"
            "## Workflow\n"
            "1. Use the `manor` tool to fetch automation run history, service "
            "status, and warning signals for the previous local day.\n"
            "2. Highlight: automation reliability, stale state, missing data, "
            "and warning conditions needing attention.\n"
            "3. Keep the report concise with clear severity levels.\n"
            "4. If delivery is requested, send the result to the specified "
            "channel or recipients.\n\n"
            "## Guardrails\n"
            "- Default reporting window is the previous local day.\n"
            "- Do not fabricate health metrics not supported by the context."
        ),
    },
    {
        "slug": "workspace-handoff-report",
        "name": "Workspace Handoff Report",
        "display_name": "Human Handoff Report",
        "description": (
            "Identify items needing human handling: approvals, escalations, "
            "and exception reviews from the previous day."
        ),
        "tags": ["workspace", "automation", "daily-report", "handoff"],
        "system_prompt": (
            "# Workspace Handoff Report\n\n"
            "You are a workspace automation skill.  Your job is to produce a "
            "human handoff digest for the workspace.\n\n"
            "## Workflow\n"
            "1. Use the `manor` tool to fetch tasks, escalations, exceptions, "
            "and approval requests from the previous local day.\n"
            "2. Surface: items needing approval, risks, customer-sensitive "
            "items, and exceptions requiring human decision.\n"
            "3. For each item, note the recommended handoff owner.\n"
            "4. If delivery is requested, send the result to the specified "
            "channel or recipients.\n\n"
            "## Guardrails\n"
            "- Default reporting window is the previous local day.\n"
            "- Do not fabricate escalation data not supported by the context."
        ),
    },
    {
        "slug": "workspace-daily-summary",
        "name": "Workspace Daily Summary",
        "display_name": "Morning Daily Summary",
        "description": (
            "Morning briefing for the workspace covering yesterday's "
            "outcomes, active risks, and today's focus areas."
        ),
        "tags": ["workspace", "automation", "daily-report", "summary"],
        "system_prompt": (
            "# Workspace Daily Summary\n\n"
            "You are a workspace automation skill. Your job is to produce an "
            "executive-friendly morning summary from a deterministic Manor "
            "data snapshot.\n\n"
            "## Workflow\n"
            "1. First call the `manor` tool with "
            "`action=get_workspace_daily_summary`. Pass the workspace_id from "
            "your skill config or invocation payload, plus timezone/date when "
            "provided.\n"
            "2. Treat the returned JSON as the source of truth. Do not infer "
            "extra completions, failures, blockers, handoff items, or metrics "
            "that are not present in the tool result.\n"
            "3. Structure the report as:\n"
            "   - Yesterday's outcomes (key completions, misses)\n"
            "   - Current health (service status, automation reliability)\n"
            "   - Needs human handling (approvals, escalations)\n"
            "   - Today's focus (priorities, deadlines)\n"
            "4. Keep the summary concise -- aim for quick scanning.\n"
            "5. If delivery is requested, send the result to the specified "
            "channel or recipients.\n\n"
            "## Guardrails\n"
            "- Default reporting window is the previous local day.\n"
            "- If the summary tool returns no urgent actions, say that plainly.\n"
            "- Do not fabricate outcomes not supported by the tool result."
        ),
    },
    {
        "slug": "workspace-scorecard-evaluation",
        "name": "Workspace Scorecard Evaluation",
        "display_name": "Scorecard Evaluation",
        "description": (
            "Evaluate the workspace against its scorecard metrics, target "
            "score, and warning thresholds."
        ),
        "tags": ["workspace", "automation", "evaluation", "scorecard"],
        "system_prompt": (
            "# Workspace Scorecard Evaluation\n\n"
            "You are a workspace automation skill.  Your job is to evaluate "
            "the workspace against its configured scorecard.\n\n"
            "## Workflow\n"
            "1. Use the `manor` tool to fetch the workspace scorecard config, "
            "recent performance data, task context, and runtime context.\n"
            "2. For each scorecard metric, compare current evidence against "
            "target_value and warning_value.\n"
            "3. Produce a structured JSON result with:\n"
            "   - summary: concise operator-facing summary\n"
            "   - overall_score: number between 0 and 1\n"
            "   - status: on_track | warning | critical | insufficient_data\n"
            "   - metrics: per-metric results with metric_key, label, status, "
            "current_value, target_value, warning_value, notes\n"
            "   - recommended_actions: concise next steps\n"
            "4. Return strict JSON only.\n\n"
            "## Guardrails\n"
            "- Treat target_score and warning_score as thresholds for the "
            "overall score.\n"
            "- If runtime evidence is partial, say so and keep the score "
            "conservative.\n"
            "- Mark metrics with insufficient data as 'insufficient_data' "
            "instead of inventing values."
        ),
    },
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def seed_workspace_skills(
    db: AsyncSession,
    entity_id: str,
    workspace_id: str,
) -> List[str]:
    """Create default automation skills for a workspace.

    Skips any skill whose slug already exists for this entity.
    Returns list of created skill IDs.
    """
    created_ids: List[str] = []

    for defn in _WORKSPACE_SKILLS:
        slug = defn["slug"]

        # Check if skill already exists for this entity
        existing = await get_skill_by_slug(db, slug, entity_id)
        if existing is not None:
            logger.debug("Workspace skill '%s' already exists for entity %s, skipping", slug, entity_id)
            continue

        skill = await create_skill(
            db,
            entity_id=entity_id,
            name=defn["name"],
            system_prompt=defn["system_prompt"],
            slug=slug,
            display_name=defn["display_name"],
            description=defn["description"],
            tools=["manor"],
            category="workspace-automation",
            tags=defn["tags"],
            is_public=False,
            config={"workspace_id": workspace_id},
        )
        created_ids.append(skill.id)
        logger.info("Created workspace skill '%s' (id=%s) for workspace %s", slug, skill.id, workspace_id)

    return created_ids
