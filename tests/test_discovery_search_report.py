"""Real search outcomes, read from tool_call_logs with nothing extra recorded."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.skill import Skill
from packages.core.models.usage import ToolCallLog
from packages.core.services.discovery_search_report import discovery_search_report

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
CATALOG = (
    {"kind": "provider", "name": "gmail", "description": "Gmail: read, draft and send email messages."},
    {"kind": "tool", "name": "notify_user", "description": "Notify the user with an alert."},
    {"kind": "tool", "name": "generate_image", "description": "Generate an image from a text prompt."},
    {"kind": "tool", "name": "still_to_video", "description": "Animate a still photo into a short video."},
    {"kind": "tool", "name": "list_scheduled_jobs", "description": "List scheduled jobs."},
    {"kind": "tool", "name": "cancel_scheduled_job", "description": "Cancel a scheduled job."},
    {"kind": "tool", "name": "weather_search", "description": "Weather forecast for a location."},
    {"catalog_id": "manor_action:list_tasks", "kind": "operation", "name": "manor", "description": "List tasks."},
    {"kind": "skill", "skill_id": "docx", "name": "docx", "description": "Create Word documents."},
    {"kind": "tool", "name": "workspace_search", "description": "Search the workspace for goal progress."},
)


def _call(conversation, tool, *, at, args=None, entity="ent-report"):
    return ToolCallLog(id=generate_ulid(), entity_id=entity, conversation_id=conversation, tool_name=tool,
                       tool_args=args or {}, created_at=at)


def _search(conversation, query, *, at, tool="search_tools", entity="ent-report"):
    args = {"query": query} if tool == "search_tools" else {"action": "search", "query": query}
    return _call(conversation, tool, at=at, args=args, entity=entity)


@pytest.mark.asyncio
async def test_outcomes_come_from_the_query_and_the_models_next_call(db_session):
    s = timedelta(seconds=1)
    db_session.add_all([
        # Found: an action of the provider the query ranks.
        _search("c-hit", "send gmail email", at=T0), _call("c-hit", "mcp__gmail__send_message", at=T0 + 20 * s),
        # Found through call_tool, a Manor operation and a Skill.
        _search("c-call", "cancel scheduled job", at=T0),
        _call("c-call", "call_tool", at=T0 + 9 * s, args={"name": "cancel_scheduled_job", "arguments": {}}),
        _search("c-op", "list tasks", at=T0, tool="manor"),
        _call("c-op", "manor", at=T0 + 9 * s, args={"action": "list_tasks"}),
        _search("c-skill", "create word document", at=T0),
        _call("c-skill", "invoke_skill", at=T0 + 9 * s, args={"skill": "docx"}),
        # Searched again, then used a tool the first query did not reach.
        _search("c-again", "make it move", at=T0), _search("c-again", "animate photo video", at=T0 + 15 * s),
        _call("c-again", "still_to_video", at=T0 + 40 * s),
        # Used something the results did not contain.
        _search("c-other", "weather forecast", at=T0), _call("c-other", "generate_image", at=T0 + 12 * s),
        # The Workspace gateway names its action in params.
        _search("c-ws", "workspace goal progress", at=T0),
        _call("c-ws", "manor", at=T0 + 9 * s, args={"action": "workspace", "params": {"action": "search"}}),
        # A Manor action named like a tool is that tool.
        _search("c-alias", "list scheduled jobs", at=T0),
        _call("c-alias", "manor", at=T0 + 9 * s, args={"action": "list_scheduled_jobs"}),
        # The model searched again, but this query now ranks what it used.
        _search("c-twice", "cancel scheduled job", at=T0), _search("c-twice", "stop a job", at=T0 + 5 * s),
        _call("c-twice", "cancel_scheduled_job", at=T0 + 20 * s),
        # Fell back to an always-loaded tool.
        _search("c-base", "weather forecast", at=T0), _call("c-base", "web_search", at=T0 + 9 * s),
        # Rejected for rewriting; a call only long after the window (no follow-up).
        _search("c-rewrite", "发送邮件", at=T0),
        _search("c-idle", "weather forecast", at=T0), _call("c-idle", "weather_search", at=T0 + timedelta(hours=1)),
        # Another tenant stays out of an entity report; exact lookups are not semantic searches.
        _search("c-foreign", "send gmail email", at=T0, entity="ent-other"),
        _call("c-exact", "search_tools", at=T0, args={"mode": "exact", "exact_names": ["weather_search"]}),
    ])
    await db_session.commit()

    report = await discovery_search_report(
        db_session, since=T0 - timedelta(minutes=1), entity_id="ent-report", catalog=CATALOG,
    )
    assert report["searches"] == 14
    # c-again's and c-twice's second searches are hits of their own.
    assert report["outcomes"] == {
        "used_result": 9, "searched_again": 1, "used_other": 1, "used_baseline": 1,
        "query_not_english": 1, "no_tool_call": 1,
    }
    assert report["found_rate"] == pytest.approx(9 / 11, abs=0.001)
    misses = {miss["query"]: miss for miss in report["misses"]}
    assert misses["make it move"]["outcome"] == "searched_again"
    assert misses["make it move"]["eventually_used"] == "still_to_video"
    assert misses["weather forecast"] == {"query": "weather forecast", "outcome": "used_other",
                                          "eventually_used": "generate_image", "results": ["tool:weather_search"]}


@pytest.mark.asyncio
async def test_skills_are_named_by_row_id_and_tenant_skills_are_candidates(db_session):
    s = timedelta(seconds=1)
    builtin_row, tenant_row = generate_ulid(), generate_ulid()
    db_session.add_all([
        Skill(id=builtin_row, entity_id=None, name="docx", slug="docx", system_prompt="-"),
        Skill(id=tenant_row, entity_id="ent-skills", name="Quarterly board pack", slug="board-pack",
              description="Assemble the quarterly board pack from finance exports.", system_prompt="-"),
        Skill(id=generate_ulid(), entity_id="ent-elsewhere", name="Board pack elsewhere", slug="board-pack-2",
              description="Assemble the quarterly board pack.", system_prompt="-"),
        _search("c-row", "create word document", at=T0, entity="ent-skills"),
        _call("c-row", "invoke_skill", at=T0 + 9 * s, args={"skill_id": builtin_row}, entity="ent-skills"),
        _search("c-own", "quarterly board pack", at=T0, entity="ent-skills"),
        _call("c-own", "invoke_skill", at=T0 + 9 * s, args={"skill_id": tenant_row}, entity="ent-skills"),
    ])
    await db_session.commit()
    report = await discovery_search_report(
        db_session, since=T0 - timedelta(minutes=1), entity_id="ent-skills", catalog=CATALOG,
    )
    assert report["outcomes"] == {"used_result": 2}


@pytest.mark.asyncio
async def test_the_report_reranks_against_the_built_in_catalog_by_default(db_session):
    db_session.add_all([
        _search("c-default", "weather forecast tomorrow", at=T0, entity="ent-default"),
        _call("c-default", "weather_search", at=T0 + timedelta(seconds=8), entity="ent-default"),
    ])
    await db_session.commit()
    report = await discovery_search_report(db_session, since=T0 - timedelta(minutes=1), entity_id="ent-default")
    assert report["outcomes"] == {"used_result": 1}


@pytest.mark.asyncio
async def test_a_multi_step_search_is_read_query_by_query(db_session):
    s = timedelta(seconds=1)
    db_session.add_all([
        _call("c-steps", "search_tools", at=T0, entity="ent-steps",
              args={"queries": ["weather forecast rain tomorrow", "generate image"]}),
        _call("c-steps", "generate_image", at=T0 + 9 * s, entity="ent-steps"),
        _call("c-mixed", "search_tools", at=T0, entity="ent-steps", args={"queries": ["weather forecast", "取消任务"]}),
    ])
    await db_session.commit()
    weather = tuple({"kind": "tool", "name": f"forecast_{i}",
                     "description": "Weather forecast for rain tomorrow, with a generated image of the map."}
                    for i in range(8))
    report = await discovery_search_report(
        db_session, since=T0 - timedelta(minutes=1), entity_id="ent-steps", catalog=(*CATALOG, *weather),
    )
    # Ranked as one query, eight forecast tools would fill the shortlist; the
    # second step's capability counts because each query is ranked on its own.
    assert report["outcomes"] == {"used_result": 1, "query_not_english": 1}
