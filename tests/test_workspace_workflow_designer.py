"""The Workflows design sub-agent: the Architect commands it and accepts its Workflows."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services import workspace_workflow_designer
from packages.core.services.workspace_part_designers import PartDesignResult
from packages.core.services.workspace_workflow_designer import WorkflowDesignContext, workflow_designer_executor

SERVICES = [
    {"service_key": "promotion", "name": "Music promotion", "human_review": True},
    {"service_key": "reporting", "name": "Reporting"},
]
CAMPAIGN = {
    "slug": "campaign", "name": "Plan a campaign", "service_key": "promotion",
    "plan_prompt": "Plan a time-limited campaign for the song on the chosen accounts.",
    "period_word": "campaign", "item_word": "post", "item_fields": ["title", "channel", "angle"],
    "date_field": "post_at", "make": {"prompt": "Make the poster or teaser the post needs."},
    "schedule": {"timezone": "America/Los_Angeles", "lead_minutes": 180},
    "publish": {"channel": "instagram", "prompt": "Post it with its caption.", "action_keys": ["social_post.publish"]},
}
REPORT = {
    "slug": "weekly_report", "name": "Weekly report", "service_key": "reporting",
    "prompt": "Report each song and campaign.", "cron_expr": "0 9 * * 1", "timezone": "America/Los_Angeles",
    "stats": [{"key": "posts_published", "name": "Posts published", "rubric": "Verified published posts this week."}],
}


def _scripted(calls):
    """Stand in for the sub-agent's model: run the given real tool calls in order."""
    async def designer(_designer, context, **_kwargs):
        state: dict = {}
        tools = workflow_designer_executor(context, state)
        for name, args in calls:
            state.setdefault("replies", []).append(json.loads(await tools(name, args)))
        return PartDesignResult(candidate=state.get("candidate"), warnings=state.get("warnings") or [],
                                notes="scripted design", attempts=state.get("attempts", 0),
                                details=state.get("details") or {}, state=state)
    return designer


async def _tools(calls):
    state: dict = {}
    tools = workflow_designer_executor(WorkflowDesignContext.from_draft_fields({"services": SERVICES}), state)
    return state, [json.loads(await tools(name, args)) for name, args in calls]


async def test_patterns_are_submitted_by_slug_with_their_stats_and_automations():
    state, replies = await _tools([
        ("wf_pattern_content_calendar", CAMPAIGN), ("wf_pattern_report_stat", REPORT),
        ("wf_submit_candidate", {"use_patterns": ["campaign_plan", "campaign_item", "campaign_make",
                                                  "campaign_publish", "campaign_schedule", "campaign_due",
                                                  "weekly_report"]}),
    ])
    # The designer reads each pattern's shape, not its steps.
    assert "steps" in replies[0]["workflows"][0] and "config" not in json.dumps(replies[0]["workflows"])
    assert replies[-1]["ok"] is True, replies[-1]
    candidate = state["candidate"]
    assert [row["key"] for row in candidate["stats"]] == ["posts_published"]
    assert sorted(row["workflow"] for row in candidate["automations"]) == ["campaign_due", "weekly_report"]


SPEC_PUBLISHING = {"workflows": [{"slug": "campaign", "service_key": "promotion", "publishes_to": ["instagram", "TikTok"]}]}
TWO_CHANNELS = {**CAMPAIGN, "item_fields": ["title", "channel"], "publish": {
    "channels": [{"channel": "instagram"}, {"channel": "tiktok"}], "action_keys": ["social_post.publish"]}}
ALL_FLOWS = ["campaign_plan", "campaign_item", "campaign_make", "campaign_publish_instagram", "campaign_publish_tiktok",
             "campaign_publish", "campaign_schedule", "campaign_due"]


async def _spec_tools(calls, *, services=SERVICES, spec=SPEC_PUBLISHING):
    context = WorkflowDesignContext.from_draft_fields({"services": services})
    context.spec = spec
    tools = workflow_designer_executor(context, {})
    return [json.loads(await tools(name, args)) for name, args in calls]


async def test_a_design_cannot_drop_the_publishing_the_spec_promises():
    # Local eval 2026-11-04: asked for a check no pattern has, the designer removed or disabled publishing.
    replies = await _spec_tools([
        ("wf_pattern_content_calendar", {**TWO_CHANNELS, "publish": {"channel": "instagram"}}),
        ("wf_submit_candidate", {"use_patterns": ["campaign_plan", "campaign_item", "campaign_make", "campaign_publish",
                                                  "campaign_schedule", "campaign_due"]}),
        ("wf_pattern_recurring_publish", {"slug": "unrelated", "service_key": "promotion", "channel": "tiktok",
                                           "make_prompt": "Make another post.", "cron_expr": "0 9 * * *", "timezone": "UTC"}),
        ("wf_submit_candidate", {"use_patterns": ["campaign_plan", "campaign_item", "campaign_make", "campaign_publish",
                                                  "campaign_schedule", "campaign_due", "unrelated", "unrelated_publish"]}),
        ("wf_pattern_content_calendar", TWO_CHANNELS),
        ("wf_submit_candidate", {"use_patterns": ALL_FLOWS}),
    ])
    assert replies[1]["ok"] is False
    assert [error for error in replies[1]["errors"] if "publishes to TikTok" in error]
    assert not [error for error in replies[1]["errors"] if "publishes to instagram" in error]
    assert replies[3]["ok"] is False and any("publishes to TikTok" in error for error in replies[3]["errors"])
    assert replies[5]["ok"] is True, replies[5]

    # Publishing may live in an explicitly called Flow with another slug.
    context = WorkflowDesignContext(spec=SPEC_PUBLISHING)
    linked = {"workflows": [
        {"slug": "campaign", "steps": [{"type": "subworkflow", "config": {"workflow_id": "delivery"}}]},
        {"slug": "delivery", "steps": [{"type": "foreach_subworkflow", "config": {"workflow_id": "channels"}}]},
        {"slug": "channels", "steps": [
            {"type": "workflow_action_grant", "config": {"scope": {"channel": channel}}}
            for channel in ("instagram", "tiktok")]},
    ], "stats": []}
    assert context.spec_issues(linked) == []


async def test_a_workflow_on_the_same_schedule_as_one_that_runs_it_is_refused():
    # Local eval 2026-11-04: an hourly wrapper around an hourly watch looked in the inbox twice an hour.
    watch = {"slug": "inbox", "service_key": "promotion", "source": "the Gmail inbox", "prompt": "New customer emails.",
             "item_word": "email", "item_fields": ["subject"], "run_workflow": "reply", "run_input": "message_id",
             "cron_expr": "0 * * * *", "timezone": "UTC"}
    reply = {"slug": "reply", "name": "Reply", "internal": True, "trigger_type": "internal", "steps": [
        {"id": "draft", "type": "agent", "next": [], "config": {"service_key": "promotion", "prompt": "Draft {{message_id}}."}}],
        "run_inputs": [{"key": "message_id", "type": "string", "required": True}]}
    wrapper = {"slug": "inbox_check", "name": "Check", "steps": [
        {"id": "check", "type": "subworkflow", "next": [], "config": {"workflow_id": "inbox_watch", "inputs": []}}]}
    replies = await _spec_tools([
        ("wf_pattern_source_watch", watch),
        ("wf_submit_candidate", {"use_patterns": ["inbox_watch", "inbox_one"], "workflows": [reply, wrapper],
                                 "automations": [{"workflow": "inbox_check", "cron_expr": "0 * * * *", "timezone": "UTC"}]}),
        ("wf_submit_candidate", {"use_patterns": ["inbox_watch", "inbox_one"], "workflows": [reply]}),
    ], services=[{"service_key": "promotion", "name": "Promotion"}], spec={})
    assert replies[1]["ok"] is False and [error for error in replies[1]["errors"] if "runs twice" in error]
    assert replies[2]["ok"] is True, replies[2]


async def test_a_sent_back_design_is_revised_from_its_summary_with_its_workflows_ready(monkeypatch):
    state, _ = await _tools([("wf_pattern_content_calendar", CAMPAIGN), ("wf_pattern_report_stat", REPORT), (
        "wf_submit_candidate", {"use_patterns": ["campaign_plan", "campaign_item", "campaign_make", "campaign_publish",
                                                 "campaign_schedule", "campaign_due", "weekly_report"]})])
    candidate = state["candidate"]
    seen = {}

    async def loop(**kwargs):
        seen["message"] = kwargs["user_message"]
        # The revision keeps everything by slug, without rebuilding a pattern.
        seen["reply"] = json.loads(await kwargs["tool_executor"]("wf_submit_candidate", {
            "use_patterns": [row["slug"] for row in candidate["workflows"]]}))
        return SimpleNamespace(content="Revised.", rounds=1)

    monkeypatch.setattr("packages.core.ai.runtime.harness.runtime_execute_agentic_loop", loop)
    result = await workspace_workflow_designer.WORKFLOWS_DESIGNER.design(
        WorkflowDesignContext.from_draft_fields({"services": SERVICES}), brief="Campaigns.", feedback="Name the first post.",
        sent_back={"value": candidate, "brief": "Campaigns."}, entity_id="ent-1", user_id="u-1", workspace_id=None,
        surface=_draft_surface())
    assert seen["reply"]["ok"] is True, seen["reply"]
    # Every call of the run repeats this message, so it holds each Workflow's shape, not its steps' config.
    assert "Name the first post." in seen["message"] and "campaign_publish" in seen["message"]
    assert '"config"' not in seen["message"] and len(seen["message"]) < len(json.dumps(candidate)) / 2
    assert [row["key"] for row in result.candidate["stats"]] == ["posts_published"]
    assert {row["workflow"] for row in result.candidate["automations"]} == {"campaign_due", "weekly_report"}


async def test_the_lead_reviews_what_a_design_enforces_and_asks():
    state, _ = await _tools([("wf_pattern_content_calendar", CAMPAIGN), (
        "wf_submit_candidate", {"use_patterns": ["campaign_plan", "campaign_item", "campaign_make", "campaign_publish",
                                                 "campaign_schedule", "campaign_due"]})])
    summary = {row["slug"]: row for row in
               workspace_workflow_designer.workflows_candidate_summary(state["candidate"])["workflows"]}
    assert summary["campaign_plan"]["guards"] and "approve_plan" in summary["campaign_plan"]["approvals"]
    assert "post_at" in summary["campaign_publish"]["approvals"]["approve_post"]
    assert any("verified receipt" in guard for guard in summary["campaign_publish"]["guards"])
    assert "guards" not in summary["campaign_item"]


def _draft_surface():
    from packages.core.ai.runtime import ChatSurface

    return ChatSurface.WORKSPACE_DRAFT_ARCHITECT


def _templated_project_workflow():
    return {
        "slug": "existing_posts", "steps": [
            {"id": "start", "type": "trigger", "config": {}, "next": ["item"]},
            {"id": "item", "type": "workflow_project", "next": [], "config": {
                "operation": "get", "project_type": "post", "project_id": "{{post_id}}",
                "state_schema": "{{post_schema}}", "schema_version": 1}},
        ],
        "variables": [{"key": "post_id", "default": "post-1"}, {"key": "post_schema", "default": {
            "type": "object", "properties": {"title": {"type": "string"}}}}],
    }


@pytest.mark.parametrize("pattern", ["wf_pattern_channel_publish", "wf_pattern_schedule_expansion"])
def test_a_runtime_schema_returns_a_pattern_error_instead_of_crashing(pattern):
    from packages.core.services.workflow_service import validate_workflow_steps

    workflow = _templated_project_workflow()
    assert validate_workflow_steps(workflow["steps"], variable_names=["post_id", "post_schema"])["valid"]
    context = WorkflowDesignContext.from_draft_fields({"services": SERVICES, "workflows": [workflow]})
    reply, result = workspace_workflow_designer.run_pattern(context, {}, pattern, {
        "slug": "publish_posts", "item_project_type": "post", "service_key": "promotion",
        "channel": "instagram", "ready_stages": ["made"],
        "date_field": "date", "timezone": "UTC", "waiting_stages": ["made"],
        "run_workflow": "publish_one", "run_input": "post_id",
    })
    assert result is None and reply["ok"] is False
    assert "state_schema.properties" in reply["errors"][0]


def test_a_declared_schema_still_builds_a_pattern_beside_a_runtime_schema():
    from packages.core.services.workflow_service import validate_workflow_steps, workflow_variable_names

    workflow = _templated_project_workflow()
    partial = copy.deepcopy(workflow)
    partial["slug"] = "runtime_properties"
    partial["steps"][1]["config"]["state_schema"] = {"type": "object", "properties": "{{post_fields}}"}
    partial["variables"].append({"key": "post_fields", "default": {"title": {"type": "string"}}})
    assert validate_workflow_steps(partial["steps"], variable_names=workflow_variable_names(partial["variables"]))["valid"]
    declared = copy.deepcopy(workflow)
    declared["slug"] = "declared_posts"
    declared["steps"][1]["config"]["state_schema"] = {
        "type": "object", "properties": {"title": {"type": "string"}}}
    context = WorkflowDesignContext.from_draft_fields({"services": SERVICES, "workflows": [workflow, partial, declared]})
    reply, result = workspace_workflow_designer.run_pattern(context, {}, "wf_pattern_channel_publish", {
        "slug": "publish_posts", "item_project_type": "post", "service_key": "promotion",
        "channel": "instagram", "ready_stages": ["made"],
    })
    assert reply["ok"] is True and result is not None
    item = next(step for step in result["workflows"][0]["steps"] if step["id"] == "item")
    assert item["config"]["state_schema"]["properties"]["title"] == {"type": "string"}


@pytest.mark.parametrize("field, flagged", [("channel", False), ("title", True)])
def test_a_runtime_schema_beside_a_condition_is_still_checked(field, flagged):
    workflow = {"slug": "route_posts", "steps": [
        {"id": "start", "type": "trigger", "config": {}, "next": ["item"]},
        {"id": "item", "type": "workflow_project", "next": ["route"], "config": {
            "operation": "get", "project_type": "post",
            "state_schema": {"type": "object", "properties": "{{post_fields}}", "anyOf": "{{more}}"}}},
        {"id": "plan", "type": "agent", "next": [], "config": {"output_schema": {"type": "object", "properties": {
            "channel": {"type": "string", "enum": ["instagram", "tiktok"]}, "title": {"type": "string"}}}}},
        {"id": "route", "type": "condition", "true_next": [], "false_next": [],
         "config": {"expression": f"item.state.{field} == 'instagram'"}},
    ]}
    issues = workspace_workflow_designer.logic_step_issues([workflow])
    assert bool(issues) is flagged
    assert all(f"compares item.state.{field}" in issue for issue in issues)


@pytest.mark.parametrize("change, error", [
    # A report that records Stats does nothing until an Automation runs it.
    ({"automations": []}, "runs on a schedule"),
    # Every Agent step runs a service the Workspace has.
    ({"service": "billing"}, "services already in this draft"),
    # A sub-Flow step runs a Workflow of the same design.
    ({"workflows": "plan only"}, "references missing Blueprint Flow"),
    # A result the person reviews waits for their approval.
    ({"reviewed": True}, "waits for that approval"),
    # The 2026-11-02 evals: every live failure came from logic the designer wrote itself -- a gate parsing
    # text the plan Agent writes as JSON, a gate reading a date the plan left empty...
    ({"gate": "accounts = json.loads(inputs['plan']['summary'])\nprint(json.dumps({'ok': bool(accounts)}))"},
     "you wrote a code step yourself"),
    # ...and routing that compared that text to an exact value: 'Instagram' is not 'instagram'.
    ({"route": "plan.summary == 'Instagram'"}, "compares plan.summary to an exact value"),
    # The 2026-11-02 candle eval: a schedule-triggered Workflow is never bound, so its Job failed creation.
    ({"trigger_type": "schedule"}, "runs on a clock through an Automation"),
])
def test_a_candidate_that_cannot_run_as_designed_is_not_kept(change, error):
    from packages.core.blueprints.workflow_patterns import report_stat_flows

    report = report_stat_flows(slug="weekly_report", name="Weekly report", service_key="reporting",
                               prompt="Report the week.", stats=REPORT["stats"], cron_expr="0 9 * * 1", timezone="UTC")
    candidate = {"workflows": report["workflows"], "stats": report["stats"], "automations": report["automations"]}
    if "automations" in change:
        candidate["automations"] = change["automations"]
    if change.get("service"):
        candidate["workflows"][0]["steps"][1]["config"]["service_key"] = change["service"]
    if change.get("trigger_type"):
        candidate["workflows"][0]["trigger_type"] = change["trigger_type"]
    if change.get("workflows"):
        candidate = {"workflows": [{"slug": "plan", "name": "Plan", "steps": [
            {"id": "each", "type": "foreach_subworkflow", "next": [], "config": {
                "workflow_id": "make_one", "over": "{{items}}", "item_key": "id", "item_var": "item"}}],
            "variables": [{"key": "items", "default": []}]}]}
    if change.get("route"):
        change = {"gate": "", "route": change["route"]}
    if "gate" in change:
        steps = candidate["workflows"][0]["steps"]
        steps[1]["next"] = ["plan"]
        steps[2:2] = [{"id": "plan", "type": "agent", "next": ["route" if change.get("route") else "gate"], "config": {
            "service_key": "reporting", "prompt": "Plan.", "output_format": "json", "output_schema": {
                "type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}}]
        if change.get("route"):
            steps.insert(3, {"id": "route", "type": "condition", "next": [], "config": {
                "expression": change["route"]}, "true_next": ["write"], "false_next": ["write"]})
        else:
            steps.insert(3, {"id": "gate", "type": "code", "next": ["write"], "config": {
                "language": "python", "code": "import json\n" + change["gate"], "output_var": "gate",
                "output_format": "json"}})
    services = [{"service_key": "reporting", "name": "Reporting", "human_review": bool(change.get("reviewed"))}]
    _, errors, _ = WorkflowDesignContext.from_draft_fields({"services": services}).check(candidate)
    assert any(error in message for message in errors), errors


async def test_a_pattern_step_the_designer_changed_is_its_own_logic():
    """A pattern's code runs as the pattern built it; one edited (or rerouted) is refused like any written by hand."""
    state, replies = await _tools([("wf_pattern_content_calendar", CAMPAIGN)])
    slugs = list(state["library"])
    publish = copy.deepcopy(state["library"]["campaign_publish"])
    ready = next(step for step in publish["steps"] if step["id"] == "ready")
    ready["config"]["code"] = ready["config"]["code"].replace("missing = [", "missing = [] or [")
    tools = workflow_designer_executor(WorkflowDesignContext.from_draft_fields({"services": SERVICES}), state)
    changed = json.loads(await tools("wf_submit_candidate", {
        "use_patterns": [slug for slug in slugs if slug != "campaign_publish"], "workflows": [publish]}))
    assert changed["ok"] is False
    assert any("campaign_publish.ready: you wrote a code step yourself" in error for error in changed["errors"])
    unchanged = json.loads(await tools("wf_submit_candidate", {"use_patterns": slugs}))
    assert unchanged["ok"] is True, unchanged


async def test_a_workflows_candidate_is_checked_again_when_accepted(monkeypatch):
    from packages.core.ai.tools import workspace_arch_tools

    services = [{"service_key": "reporting", "name": "Reporting"}, {"service_key": "support", "name": "Support"}]
    draft = SimpleNamespace(fields={"services": services})

    async def load(*_args):
        return draft

    async def persist(_db, _draft):
        return None

    monkeypatch.setattr(workspace_arch_tools, "_load_draft", load)
    monkeypatch.setattr(workspace_arch_tools, "_persist", persist)
    monkeypatch.setattr(workspace_workflow_designer, "run_part_designer", _scripted([
        ("wf_pattern_report_stat", REPORT), ("wf_submit_candidate", {"use_patterns": ["weekly_report"]})]))
    designed = json.loads(await workspace_arch_tools._design_workflows(
        None, entity_id="ent-1", draft_id="draft-1", brief="A weekly report of what was published."))
    assert designed["ok"] is True, designed
    assert designed["candidate"]["automations_needed"][0]["workflow"] == "weekly_report"
    # The service the report runs was removed after it was designed.
    draft.fields["services"] = services[1:]
    reply = json.loads(await workspace_arch_tools._accept_parts(None, entity_id="ent-1", draft_id="draft-1",
                                                                parts=["workflows"]))
    assert reply["ok"] is False and "Draft changed" in reply["failed"]["error"]
    assert "workflows" not in draft.fields and "workflows" in draft.fields["_candidates"]


def test_only_the_flag_shows_the_architect_its_workflows_designer():
    from packages.core.services.workspace_architect import architect_system_prompt, architect_tool_schemas

    def names(**kwargs):
        return {schema["function"]["name"] for schema in architect_tool_schemas(**kwargs)}

    assert "ws_design_workflows" not in names() and "ws_propose_workflow" in names()
    assert {"ws_design_workflows", "ws_propose_workflow"} <= names(workflows_designer=True)
    # Building alone, the Architect has the record-collection pattern; with the flag its Workflows designer has it.
    assert "ws_propose_record_collection" in names()
    assert "ws_propose_record_collection" not in names(workflows_designer=True)
    assert "ws_propose_record_collection" in architect_system_prompt()
    assert "ws_design_workflows" not in architect_system_prompt()
    with_designer = architect_system_prompt(workflows_designer=True)
    assert "ws_propose_record_collection" not in with_designer
    assert "ws_design_workflows" in with_designer and "STEP B0. PROCESS WORKFLOW" not in with_designer
    assert "STEP C. Before matching capabilities" in with_designer


async def test_a_campaign_brief_becomes_installed_workflows_that_run_on_their_schedules(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Architect commands, the designer composes patterns, the Architect accepts and schedules; creation installs it."""
    from packages.core.ai.tools.workspace_arch_tools import _accept_parts, _design_workflows, _lint_draft, _propose_automation
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.user import User, UserMembership
    from packages.core.models.workflow import WorkflowDefinition
    from packages.core.models.workspace_stat import WorkspaceStat
    from packages.core.services.workspace_draft_service import finalize_draft
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    entity_id, user_id = generate_ulid(), generate_ulid()
    db_session.add_all([
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.test", password_hash="x", role="owner",
             status="active"),
        UserMembership(user_id=user_id, entity_id=entity_id, role="owner", status="active"),
    ])
    draft = WorkspaceDraft(id=generate_ulid(), entity_id=entity_id, user_id=user_id, messages=[], missing=[],
                           ready=False, status="active", fields={
                               **dict(DEFAULT_FIELDS), "name": "Song promotion", "kind": "content",
                               "operating_context": "An independent musician.", "primary_work": "Promote songs.",
                               "services": [], "agent_mappings": [], "goals": [], "stats": []})
    db_session.add(draft)
    await db_session.flush()
    monkeypatch.setattr(workspace_workflow_designer, "run_part_designer", _scripted([
        ("wf_read_context", {}), ("wf_pattern_content_calendar", CAMPAIGN), ("wf_pattern_report_stat", REPORT),
        ("wf_submit_candidate", {"use_patterns": ["campaign_plan", "campaign_item", "campaign_make", "campaign_publish",
                                                  "campaign_schedule", "campaign_due", "weekly_report"]}),
    ]))
    ids = {"entity_id": entity_id, "user_id": user_id, "draft_id": draft.id}
    refused = json.loads(await _design_workflows(db_session, **ids, brief="Promote my song."))
    assert refused["ok"] is False and "services" in refused["error"]
    draft.fields = {**draft.fields, "services": copy.deepcopy(SERVICES), "agent_mappings": [{
        "service_key": row["service_key"], "strategy": "create_custom", "create_agent_draft": {
            "agent_name": f"{row['name']} Agent", "system_prompt": f"Handle {row['name'].lower()} work and stay within it.",
            "business_capabilities": ["runtime.discovery"]}} for row in SERVICES]}
    await db_session.flush()

    designed = json.loads(await _design_workflows(db_session, **ids, brief="Plan a campaign I confirm, then post on schedule."))
    assert designed["ok"] is True, designed
    accepted = json.loads(await _accept_parts(db_session, **ids, parts=["workflows"]))
    assert accepted["ok"] is True, accepted
    needed = accepted["accepted"]["workflows"]["automations_needed"]
    lint = json.loads(await _lint_draft(db_session, **ids))
    assert {issue["where"] for issue in lint["issues"] if "schedule" in issue["message"]} == {
        "workflows.campaign_due", "workflows.weekly_report"}
    for row in needed:
        scheduled = json.loads(await _propose_automation(
            # The job and the automation need not share a name (local eval 2026-11-03: a second job ran each slot).
            db_session, **ids, automation_key=f"run_{row['workflow']}", description=row["why"], service_key="promotion",
            cron_expr=row["cron_expr"], timezone=row["timezone"],
            scheduled_job={"job_id": row["workflow"], "execution_type": "workflow",
                           "execution_target": {"workflow_slug": row["workflow"]}}))
        assert scheduled["ok"] is True, scheduled
    lint = json.loads(await _lint_draft(db_session, **ids))
    assert not [issue for issue in lint["issues"] if "schedule" in issue["message"]]

    async def ready(*_args, **_kwargs): return True
    monkeypatch.setattr("packages.core.services.workspace_draft_service._refresh_missing_from_lint", ready)
    draft.ready, draft.status = True, "ready"
    workspace_id, _ = await finalize_draft(db_session, draft_id=draft.id, entity_id=entity_id, user_id=user_id)
    installed = (await db_session.execute(select(WorkflowDefinition).where(
        WorkflowDefinition.workspace_id == workspace_id))).scalars().all()
    # Installed under their slugs; the Workspace shows each by its name.
    by_name = {row.name: row for row in installed}
    assert len(installed) == 7
    # The worker's sub-Flow step runs the installed publishing Flow, not a slug.
    run = next(step for step in by_name["campaign_due"].steps if step["id"] == "run")
    assert run["config"]["workflow_id"] == by_name["campaign_publish"].id
    jobs = (await db_session.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == workspace_id))).scalars().all()
    assert {"*/15 * * * *", "0 9 * * 1"} <= {job.cron_expr for job in jobs}
    assert not [job.job_id for job in jobs if job.job_id.startswith("wa:")]
    stat = (await db_session.execute(select(WorkspaceStat).where(WorkspaceStat.workspace_id == workspace_id))).scalar_one()
    assert (stat.key, stat.collector_type) == ("posts_published", "agent_review")


def _article_flow(save_patch: dict) -> dict:
    """One Agent saves the article; the next step keeps what it made on the project."""
    schema = {"type": "object", "properties": {"topic": {"type": "string"}, "history": {"type": "array"},
                                               **{name: {"type": "string"} for name in save_patch}}}
    project = {"project_type": "article", "schema_version": 1, "state_schema": schema}
    return {"slug": "article", "name": "Article", "run_inputs": [{"key": "topic", "type": "string", "required": True}],
            "steps": [
                {"id": "open", "type": "workflow_project", "next": ["write"], "config": {
                    **project, "operation": "create", "project_key": "{{topic}}", "state": {"topic": "{{topic}}"},
                    "current_stage": "writing"}},
                {"id": "write", "type": "agent", "next": ["stamp"], "config": {
                    "service_key": "reporting", "prompt": "Write the article on {{topic}}.", "expects": ["files"]}},
                {"id": "stamp", "type": "datetime", "next": ["save"], "config": {"operation": "now"}},
                {"id": "save", "type": "workflow_project", "next": [], "config": {
                    **project, "operation": "patch", "project_id": "{{open.project_id}}",
                    "expected_revision": "{{open.revision}}", "patch": save_patch, "current_stage": "done"}},
            ]}


@pytest.mark.parametrize("save_patch, issue", [
    ({"final_document_id": "{{write.final_document_id}}", "finished_at": "{{stamp}}"}, None),
    # The 2026-11-02 staging design kept every file as "<name>_file": the App showed ids as text.
    ({"final_file": "{{write.final_file}}"}, "name it final_document_id"),
    ({"report": {"draft_file_id": "{{write.draft_file_id}}"}}, "name it draft_document_id"),
    # A phase-2 eval kept media_files from an Agent: a list of ids reads as text too.
    ({"media_files": "{{write.media_files}}"}, "project's folder"),
    # ...had an Agent build the decision log...
    ({"approval_history": "{{write.history}}"}, "history_event"),
    # ...and kept the delivery time an Agent reported.
    ({"delivery_timestamp": "{{write.timestamp}}"}, "datetime step"),
], ids=["followable", "file", "nested_file", "files", "history", "moment"])
def test_a_project_the_person_cannot_follow_is_sent_back(save_patch, issue):
    flow = _article_flow(save_patch)
    _, errors, _ = WorkflowDesignContext.from_draft_fields({"services": SERVICES[1:]}).check({"workflows": [flow]})
    found = [error for error in errors if "workflows.article.save.patch" in error]
    assert (found == []) if issue is None else (len(found) == 1 and issue in found[0]), errors


def test_both_ways_of_writing_workflows_read_one_project_state_guidance():
    """The Architect (flag off) and the Workflows designer (flag on) are told the same rules."""
    from packages.core.services.workflow_project_state_guidance import FOLLOWABLE_PROJECT_STATE
    from packages.core.services.workspace_architect import architect_system_prompt
    from packages.core.services.workspace_part_designers import designer_system_prompt

    def words(text: str) -> str:
        return " ".join(text.split())

    guidance = words(FOLLOWABLE_PROJECT_STATE)
    assert guidance in words(architect_system_prompt())
    assert guidance in words(designer_system_prompt(workspace_workflow_designer.WORKFLOWS_DESIGNER))
    assert "<<" not in architect_system_prompt() and guidance not in words(architect_system_prompt(workflows_designer=True))


@pytest.mark.parametrize("alias", ["written_article", "written-article"])
def test_project_file_check_follows_the_agent_output_alias(alias):
    flow = _article_flow({"final_file": "{{" + alias + ".final_file}}"})
    flow["steps"][1]["config"]["output_var"] = alias
    _, errors, _ = WorkflowDesignContext.from_draft_fields({"services": SERVICES[1:]}).check({"workflows": [flow]})
    assert any("workflows.article.save.patch.final_file" in error and "final_document_id" in error for error in errors)


@pytest.mark.parametrize("name, is_moment", [("processing_time", False), ("delivered_at", True)])
def test_project_numeric_duration_and_wall_clock_timestamp_are_distinct(name, is_moment):
    flow = _article_flow({name: "{{write.value}}"})
    for step in flow["steps"]:
        if step["type"] == "workflow_project":
            step["config"]["state_schema"]["properties"][name] = {"type": "number"}
    _, errors, _ = WorkflowDesignContext.from_draft_fields({"services": SERVICES[1:]}).check({"workflows": [flow]})
    assert any(f"workflows.article.save.patch.{name}" in error for error in errors) is is_moment, errors


def test_a_step_cannot_require_an_agent_to_post_to_the_workspace_chat():
    """Local eval 2026-11-03: a saved report ended "failed" on every run at "Share in Internal Chat"."""
    from packages.core.services.workspace_workflow_designer import chat_post_step_issues

    def flow(*steps):
        return [{"slug": "report", "steps": list(steps)}]

    def agent(prompt, expects):
        return {"id": "share", "type": "agent", "config": {"prompt": prompt, "expects": expects}}

    for prompt in ("Send report to this workspace's internal chat.", "Post it to the team chat.",
                   "Share in the workspace chat.", "把报告发到内部聊天。"):
        [issue] = chat_post_step_issues(flow(agent(prompt, ["effects"])))
        assert "report.share" in issue and "notify" in issue
    assert chat_post_step_issues(flow(agent("Share in internal chat.", ["files", "effects"])))

    # Sending somewhere an Agent has a tool for, or no required action, is fine; so is the notify step.
    assert chat_post_step_issues(flow(agent("Email the client the report.", ["effects"]))) == []
    assert chat_post_step_issues(flow(agent("Summarize what the internal chat said.", ["files"]))) == []
    assert chat_post_step_issues(flow({"id": "tell", "type": "notify", "config": {
        "channel": "workspace_chat", "message": "Report ready: {{write.report_document_id}}"}})) == []


# ── Approvals nobody asked for ──────────────────────────────────────────────

def _reviewed_flow(slug: str, service: str, **wait) -> dict:
    """Prepare, a person approves, act: the shape of a reply, a post and a report alike."""
    return {"slug": slug, "name": slug, "steps": [
        {"id": "prepare", "type": "agent", "next": ["review"], "config": {"service_key": service, "prompt": "Prepare."}},
        {"id": "review", "type": "wait", "next": ["act"], **wait, "config": {
            "wait_type": "approval", "message": "Approve this?", "options": ["approve", "revise"],
            "approval_values": ["approve"], "revise_from": "prepare", "response_variable": "review_decision"}},
        {"id": "act", "type": "agent", "next": [], "config": {"service_key": service, "prompt": "Do it as approved."}},
    ]}


def _approval_errors(flows, *, services, rules=(), spec=None, asked_words=None, saved=()):
    context = WorkflowDesignContext.from_draft_fields({"services": services, "rules": list(rules)})
    context.spec, context.asked_words, context.workflows = spec or {}, asked_words, list(saved)
    _, errors, _ = context.check({"workflows": flows})
    return [error for error in errors if "asked" in error]


CARE = [{"service_key": "care", "name": "Customer care"}, {"service_key": "social", "name": "Instagram", "human_review": True}]
SEND_RULE = {"rule_key": "approve_email", "rule_type": "approval_required", "scope": "care",
             "description": "Approve each customer email before sending.", "action_patterns": ["email.send"]}


def test_an_approval_nothing_asks_for_is_sent_back_before_the_lead_sees_it():
    """The 2026-11-03 rerun: 3 of 4 samples were sent back to remove a report's or a comment reply's wait."""
    # A candle shop: the post flow has the person's one review of Instagram work; the comment reply wants another.
    flows = [_reviewed_flow("post", "social"), _reviewed_flow("comment_reply", "social")]
    [error] = _approval_errors(flows, services=CARE, rules=[SEND_RULE])
    assert error.startswith("workflows.comment_reply.review:") and "approve_plan:false" in error

    # A musician: the plan is reviewed, the weekly report only goes to them.
    music = [{"service_key": "promotion", "name": "Promotion", "human_review": True}]
    flows = [_reviewed_flow("plan", "promotion"), _reviewed_flow("weekly_report", "promotion")]
    [error] = _approval_errors(flows, services=music)
    assert error.startswith("workflows.weekly_report.review:")

    # Reusing a saved non-approval step's ID does not make a new approval pre-existing.
    saved = _reviewed_flow("reply", "care")
    saved["steps"][1].update(type="transform", config={"set": {"note": "ready"}})
    [error] = _approval_errors([_reviewed_flow("reply", "care")], services=CARE, saved=[saved])
    assert error.startswith("workflows.reply.review:")


@pytest.mark.parametrize("flows, kwargs", [
    # A rule asks: the check never takes an approval a rule requires away.
    ([_reviewed_flow("reply", "care", meta={"asked_by": "rule:approve_email"})], {"rules": [SEND_RULE]}),
    # So do the person's words, whichever Workflow they are about.
    ([_reviewed_flow("report", "care", meta={"asked_by": "Let me  approve the REPORT"})],
     {"asked_words": "Weekly: let me approve the report before I read it."}),
    # The spec plans an approval stage (jr_1, candle) in this Workflow or one its pattern made.
    ([_reviewed_flow("reply", "care")], {"spec": {"workflows": [{"slug": "reply", "stages": [{"kind": "approval"}]}]}}),
    ([_reviewed_flow("reply_draft", "care")], {"spec": {"workflows": [{"slug": "reply", "stages": [{"kind": "approval"}]}]}}),
    # A service with human_review has its one review, without a stage (jr_2's spec had none).
    ([_reviewed_flow("post", "social")], {}),
    # It is already on the saved design: only what this design adds is checked.
    ([_reviewed_flow("reply", "care")], {"saved": [_reviewed_flow("reply", "care")]}),
], ids=["rule", "words", "stage", "pattern_stage", "human_review", "saved"])
def test_an_approval_a_rule_or_the_person_asks_for_stays(flows, kwargs):
    assert _approval_errors(flows, services=CARE, **kwargs) == []


@pytest.mark.parametrize("asked_by, kwargs, message", [
    ("rule:nope", {"rules": [SEND_RULE]}, "the rules are ['approve_email']"),
    # A rule of another service does not cover this Workflow.
    ("rule:approve_email", {"rules": [{**SEND_RULE, "scope": "social"}]}, "does not run"),
    ("I want to approve it", {"asked_words": "Reply to my customers."}, "quote their exact words"),
])
def test_what_an_approval_says_asks_for_it_is_verified(asked_by, kwargs, message):
    [error] = _approval_errors([_reviewed_flow("reply", "care", meta={"asked_by": asked_by})], services=CARE, **kwargs)
    assert message in error


def test_the_spec_counts_its_approvals_and_publishing_uses_one():
    from packages.core.blueprints.content_calendar import CalendarSpec
    from packages.core.blueprints.workflow_patterns import content_calendar_flows

    spec = CalendarSpec(period_type="campaign", item_type="post", item_key_prefix="c:p:", item_key_field="post_key",
                        parent_field="campaign_id", date_field="post_at", item_fields=("campaign_id", "post_key", "post_at", "title"),
                        period_fields=("title",), stages=("planned", "made"), made_stage="made",
                        period_word="campaign", item_word="post")
    built = content_calendar_flows(spec, slug="campaign", name="Plan", service_key="care", plan_prompt="Plan.",
                                   make={"prompt": "Make it.", "approve": True}, publish={"channel": "instagram"})
    stages = {"workflows": [{"slug": "campaign", "stages": [{"kind": "approval"}, {"kind": "approval"}]}]}
    # jr_1: "plan_approval" and "post_approval". Publishing takes one, the plan the other: nobody asked to review the file.
    [error] = _approval_errors(built["workflows"], services=CARE[:1], spec=stages)
    assert error.startswith("workflows.campaign_make.approve_made:")
    # The publish pattern's own approval needs no saying: nothing goes out without it.
    assert _approval_errors(built["workflows"], services=CARE[:1], spec={
        "workflows": [{"slug": "campaign", "stages": [{"kind": "approval"}] * 3}]}) == []
    # candle_2: a made post was reviewed, then reviewed again before publishing, to give the service its one review.
    # The publishing approval is that review, so the first goes and nothing else is missing.
    social = CARE[1:]
    built = content_calendar_flows(spec, slug="campaign", name="Plan", service_key="social", plan_prompt="Plan.",
                                   approve_plan=False, make={"prompt": "Make it.", "approve": True},
                                   publish={"channel": "instagram"})
    [error] = _approval_errors(built["workflows"], services=social)
    assert error.startswith("workflows.campaign_make.approve_made:")
    built = content_calendar_flows(spec, slug="campaign", name="Plan", service_key="social", plan_prompt="Plan.",
                                   approve_plan=False, make={"prompt": "Make it."}, publish={"channel": "instagram"})
    _, errors, _ = WorkflowDesignContext.from_draft_fields({"services": social}).check({"workflows": built["workflows"]})
    assert not [error for error in errors if "human_review" in error or "asked" in error], errors
    # A pattern option can say who asked instead.
    asked = content_calendar_flows(spec, slug="campaign", name="Plan", service_key="care", plan_prompt="Plan.",
                                   make={"prompt": "Make it.", "approve": "rule:approve_email"},
                                   approve_plan="rule:approve_email", publish={"channel": "instagram"})
    assert _approval_errors(asked["workflows"], services=CARE[:1], rules=[SEND_RULE]) == []


def test_asking_a_person_to_confirm_what_they_did_is_not_an_approval():
    flow = _reviewed_flow("manual_post", "care")
    flow["steps"][1]["config"].update(options=["confirm"], approval_values=["confirm"], message="Did you post it?")
    flow["steps"][1]["config"].pop("revise_from")
    assert _approval_errors([flow], services=CARE) == []


def test_a_quote_checked_on_submit_is_not_checked_again_on_accept():
    """Accepting has only the Draft, not the brief; the rules are still there to check."""
    flow = _reviewed_flow("reply", "care", meta={"asked_by": "approve it first"})
    assert _approval_errors([flow], services=CARE, asked_words=None) == []
    flow = _reviewed_flow("reply", "care", meta={"asked_by": "rule:gone"})
    assert _approval_errors([flow], services=CARE, asked_words=None) != []


# ── A decision reaches the log ──────────────────────────────────────────────

@pytest.mark.parametrize("logged", [True, False])
def test_an_approval_a_later_patch_could_record_must_record_it(logged):
    flow = _article_flow({"final_document_id": "{{write.final_document_id}}"})
    steps = flow["steps"]
    steps[2:2] = [{"id": "review", "type": "wait", "next": ["stamp"], "config": {
        "wait_type": "approval", "message": "Approve?", "options": ["approve", "revise"], "approval_values": ["approve"],
        "revise_from": "write", "response_variable": "review_decision"}}]
    steps[1]["next"] = ["review"]
    if logged:
        steps[-1]["config"]["history_event"] = {"kind": "review", "decision": "{{review_decision.choice}}",
                                                "note": "{{review_decision.note}}"}
    errors = workspace_workflow_designer.project_state_issues([flow])
    assert ([error for error in errors if "decision log" in error] == []) is logged, errors


def test_an_approval_with_no_project_to_write_to_is_not_held_to_it():
    assert workspace_workflow_designer.project_state_issues([_reviewed_flow("reply", "care")]) == []


# ── Something has to start a Workflow ───────────────────────────────────────

def _event_flow(slug: str = "email_reply", key: str = "message_id", **extra) -> dict:
    return {"slug": slug, "name": slug, "run_inputs": [{"key": key, "type": "string", "required": True}], **extra, "steps": [
        {"id": "reply", "type": "agent", "next": [], "config": {"service_key": "care", "prompt": "Answer {{%s}}." % key}},
    ]}


@pytest.mark.parametrize("flow, fields, flagged", [
    # All four candle drafts of the 2026-11-03 rerun, and what an event trigger did for two of them.
    (_event_flow(), {}, True),
    (_event_flow(key="comment_id"), {}, True),
    (_event_flow(key="email_event", trigger_type="event"), {}, True),
    (_event_flow(key="comment_event", trigger_type="event"), {}, True),
    # A person's own input starts nothing by itself either, but a person can give it.
    (_event_flow(key="topic"), {}, False),
    (_event_flow(key="order_notes"), {}, False),
    # Each way something really starts it.
    (_event_flow(), {"_automations_needed": [{"workflow": "email_reply"}]}, False),
    (_event_flow(), {"scheduled_jobs": [{"execution_target": {"workflow_slug": "email_reply"}}]}, False),
    (_event_flow(trigger_type="webhook"), {}, False),
    (_event_flow(), {"workflows": [{"slug": "watch", "name": "w", "steps": [{"id": "run", "type": "subworkflow", "config": {
        "workflow_id": "email_reply"}}]}]}, False),
    # The person said they start it themselves.
    (_event_flow(binding_config={"started_by_person": "I paste the id in myself"}), {}, False),
    # Internal, and nothing runs it: a person cannot either.
    (_event_flow(key="topic", internal=True), {}, True),
], ids=["message_id", "comment_id", "email_event", "comment_event", "topic", "order_notes",
        "needed", "job", "webhook", "parent", "by_person", "orphan_internal"])
def test_a_workflow_nothing_can_start_is_reported(flow, fields, flagged):
    from packages.core.ai.tools.workspace_arch_tools import unstartable_workflow_issues

    issues = unstartable_workflow_issues({**fields, "workflows": [flow, *fields.get("workflows", [])]})
    assert (flow["slug"] in {issue["where"].split(".")[1] for issue in issues}) is flagged, issues


def test_a_candidate_with_a_flow_nothing_starts_is_not_kept_until_something_does():
    """A pattern's watch is the way a new email reaches a Workflow; the check asks for it."""
    from packages.core.blueprints.workflow_patterns import source_watch_flows

    context = WorkflowDesignContext.from_draft_fields({"services": CARE[:1]})
    candidate, errors, _ = context.check({"workflows": [_event_flow()]})
    assert candidate is None and any("wf_pattern_source_watch" in error for error in errors), errors
    watch = source_watch_flows(slug="inbox", name="Look at the inbox", service_key="care", source="the Gmail inbox",
                               prompt="Customers' mail.", item_word="email", item_fields=["sender"],
                               run_workflow="email_reply", run_input="message_id", cron_expr="*/30 * * * *",
                               timezone="UTC")
    candidate, errors, _ = context.check({**watch, "workflows": [_event_flow(), *watch["workflows"]]})
    assert errors == []
    assert [row["workflow"] for row in candidate["automations"]] == ["inbox_watch"]


@pytest.mark.parametrize("with_spec", [True, False])
async def test_lint_reports_a_flow_nothing_starts_only_with_the_parts_flag_on(db_session: AsyncSession, with_spec):
    """A build spec exists only behind workspace_architect_parts: without one, lint says what it always said."""
    from packages.core.ai.tools.workspace_arch_tools import _lint_draft
    from packages.core.services.workspace_build_spec import SPEC_FIELD, SPEC_VERSION

    entity_id, user_id = generate_ulid(), generate_ulid()
    draft = WorkspaceDraft(id=generate_ulid(), entity_id=entity_id, user_id=user_id, messages=[], missing=[],
                           ready=False, status="active", fields={
                               "services": CARE, "workflows": [_event_flow()],
                               **({SPEC_FIELD: {"version": SPEC_VERSION, "mode": "team"}} if with_spec else {})})
    db_session.add(draft)
    await db_session.flush()
    lint = json.loads(await _lint_draft(db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id))
    found = [issue for issue in lint["issues"] if "nothing starts it" in issue["message"]]
    assert bool(found) is with_spec, lint["issues"]
    if with_spec:
        assert found[0]["where"] == "workflows.email_reply" and found[0]["severity"] == "P1"
