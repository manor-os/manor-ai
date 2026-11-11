"""Workflow patterns: each builds Workflows creation accepts, for unrelated businesses alike."""
from __future__ import annotations

import contextlib
import io
import json

import pytest

from packages.core.blueprints.content_calendar import CalendarSpec
from packages.core.blueprints.installer import validate_portable_recipe_resources
from packages.core.blueprints.workflow_patterns import (
    content_calendar_flows, project_tree_flows, recurring_publish_flows, report_stat_flows, review_workflow,
    source_feed_for, source_watch_flows,
)
from packages.core.services.workspace_app_designer import (
    AppDesignContext, content_calendar_pattern, project_structure_issues, project_tree_pattern,
)

pytestmark = pytest.mark.unit

CAMPAIGN = CalendarSpec(
    period_type="campaign", item_type="campaign_post", item_key_prefix="campaign:post:", item_key_field="post_key",
    parent_field="campaign_id", date_field="post_at", item_fields=("campaign_id", "post_key", "post_at", "title", "channel"),
    period_fields=("title", "start", "end"), stages=("planned", "made"), made_stage="made",
    period_word="campaign", item_word="post",
)
NEWSLETTER = CalendarSpec(
    period_type="newsletter_month", item_type="newsletter_issue", item_key_prefix="newsletter:issue:",
    item_key_field="issue_key", parent_field="month_id", date_field="send_on",
    item_fields=("month_id", "issue_key", "send_on", "title", "angle"), period_fields=("title", "start", "end"),
    stages=("idea", "drafted"), made_stage="drafted", period_word="month", item_word="issue",
)
SCENARIOS = {
    # A song's campaign: posters and teasers made, each post due before its time, approved and published.
    "music campaign": (CAMPAIGN, dict(
        slug="campaign", service_key="promotion", plan_prompt="Plan the song's campaign.",
        make={"prompt": "Make the poster or teaser."}, schedule={"timezone": "America/Los_Angeles", "lead_minutes": 180},
        publish={"channel": "instagram", "prompt": "Post it with its caption.", "requires": ["channel"]})),
    # A shop's Instagram week: only scheduled publishing of planned posts.
    "instagram week": (CAMPAIGN, dict(
        slug="insta_week", service_key="social", plan_prompt="Plan next week's posts.",
        schedule={"timezone": "Asia/Shanghai", "lead_minutes": 30}, publish={"channel": "instagram"})),
    # A newsletter month: each issue drafted and approved, nothing published by the Flow.
    "newsletter month": (NEWSLETTER, dict(
        slug="newsletter", service_key="editor", plan_prompt="Plan the month's issues.",
        make={"prompt": "Draft the issue.", "approve": True})),
}


def _run(step: dict, inputs: dict) -> dict:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(step["config"]["code"], {"inputs": inputs})
    return json.loads(out.getvalue())


def _step(workflow: dict, step_id: str) -> dict:
    return next(step for step in workflow["steps"] if step["id"] == step_id)


def _by_slug(result: dict) -> dict[str, dict]:
    return {workflow["slug"]: workflow for workflow in result["workflows"]}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_a_content_calendar_is_accepted_by_creation_and_by_the_app_calendar(scenario):
    spec, options = SCENARIOS[scenario]
    result = content_calendar_flows(spec, name="Plan", **options)
    # The checks creation runs: each graph, and every sub-Flow a step runs is one of these.
    validate_portable_recipe_resources({"workflows": result["workflows"]})
    flows = _by_slug(result)
    slug = options["slug"]
    assert flows[f"{slug}_item"]["internal"] is True
    assert ("schedule" in options) == (f"{slug}_due" in flows)
    assert [row["workflow"] for row in result["automations"]] == ([f"{slug}_due"] if "schedule" in options else [])
    # The plan is confirmed before anything lands on the calendar.
    plan = flows[f"{slug}_plan"]
    assert _step(plan, "plan")["next"] == ["approve_plan"] and _step(plan, "approve_plan")["next"] == ["calendar_plan"]
    # The App designer lays it out as a calendar: the period, its dated items and their stages.
    context = AppDesignContext.from_draft_fields({"workflows": result["workflows"]})
    stages = {stage: stage for stage in (spec.stages[0], spec.made_stage)}
    laid_out = content_calendar_pattern(context, {
        "workflow": f"{slug}_item", "period_workflow": f"{slug}_plan", "period_project_type": spec.period_type,
        "item_project_type": spec.item_type, "parent_field": spec.parent_field, "date_field": spec.date_field,
        "stages": stages, "done_stage": spec.made_stage,
    })
    assert laid_out["ok"], laid_out


def test_the_confirmed_plan_becomes_dated_items_due_before_their_time():
    flows = _by_slug(content_calendar_flows(CAMPAIGN, name="Plan", **SCENARIOS["music campaign"][1]))
    plan = _step(flows["campaign_plan"], "calendar_plan")
    planned = _run(plan, {"plan": {"key": "W45", "period": {"title": "Launch"}, "items": [
        {"post_key": "P1", "post_at": "2026-11-05T18:00:00", "title": "Teaser"},
        {"post_key": "P1", "post_at": "2026-11-06", "title": "Duplicate"}, {"title": "No key"}]}})
    assert planned["key"] == "W45" and [item["id"] for item in planned["items"]] == ["P1"]
    assert planned["items"][0]["stage"] == "planned"
    # After saving, every item of the campaign is scheduled; the worker starts due ones every 15 minutes.
    assert _step(flows["campaign_plan"], "unplan_posts")["next"] == ["posts_to_schedule"]
    assert _step(flows["campaign_plan"], "schedule_posts")["config"]["workflow_id"] == "campaign_schedule"
    when = _step(flows["campaign_schedule"], "when")
    post = {"current_stage": "made", "state": {"post_at": "2026-11-05T18:00:00"}}
    # 18:00 in Los Angeles less the three hours it takes to get ready.
    assert _run(when, {"item": post}) == {"schedule": True, "due_at": "2026-11-05T23:00:00+00:00"}
    assert _run(when, {"item": {**post, "current_stage": "published"}})["schedule"] is False
    assert _run(when, {"item": {**post, "state": {}}})["schedule"] is False
    due = flows["campaign_due"]
    assert _step(due, "lease")["config"]["pending_stages"] == ["scheduled"]
    # The lease ends before the post's Flow runs, so a Flow waiting for approval never holds it.
    assert _step(due, "started")["next"] == ["run"]
    assert _step(due, "run")["config"]["workflow_id"] == "campaign_publish"


def test_a_post_is_published_only_after_approval_within_its_window_with_a_verified_receipt():
    publish = _by_slug(content_calendar_flows(CAMPAIGN, name="Plan", **SCENARIOS["music campaign"][1]))["campaign_publish"]
    order = ["approve_post", "approved", "publish_by", "on_time", "is_on_time", "grant", "deadline", "publish", "receipt",
             "published"]
    for step_id, nxt in zip(order, order[1:]):
        step = _step(publish, step_id)
        assert nxt in [*step["next"], *step.get("true_next", [])], step_id
    assert _step(publish, "approved")["false_next"] == ["keep_skipped"]
    assert _step(publish, "keep_skipped")["next"] == ["skipped"]
    assert _step(publish, "grant")["config"]["approval_step_id"] == "approve_post"
    receipt = _step(publish, "receipt")["config"]
    assert receipt["require_verified"] is True and receipt["error_next"] == ["not_published"]
    # A post not made yet is made first.
    assert _step(publish, "needs_making")["true_next"] == ["make_first"]



def test_an_approval_after_the_deadline_sets_the_post_aside_instead_of_publishing():
    publish = _by_slug(content_calendar_flows(CAMPAIGN, name="Plan", **SCENARIOS["music campaign"][1]))["campaign_publish"]
    on_time = _step(publish, "on_time")
    assert _run(on_time, {"item": {"state": {}}, "publish_by": "2999-01-01T00:00:00+00:00"}) == {"ok": True, "issue": ""}
    late = _run(on_time, {"item": {"state": {}}, "publish_by": "2000-01-01T00:00:00+00:00"})
    assert late["ok"] is False and "deadline" in late["issue"]
    # The person's decision stays on the post, and no grant is made for it.
    assert _step(publish, "is_on_time")["true_next"] == ["grant"]
    too_late = _step(publish, "too_late")["config"]
    assert too_late["current_stage"] == "needs_attention" and too_late["patch"] == {"publish_issue": "{{on_time.issue}}"}
    assert "history_event" in too_late


@pytest.mark.parametrize("within, start, end", [
    (True, "start", "end"),
    ({"start": "opens", "end": "closes"}, "opens", "closes"),
])
def test_a_plan_can_keep_its_posts_inside_its_dates(within, start, end):
    spec = CalendarSpec(**{**CAMPAIGN.__dict__, "period_fields": ("title", "start", "end", "opens", "closes")})
    options = {**SCENARIOS["music campaign"][1], "publish": {
        "channel": "instagram", "prompt": "Post it.", "within_period": within}}
    flows = _by_slug(content_calendar_flows(spec, name="Plan", **options))
    planned = _run(_step(flows["campaign_plan"], "calendar_plan"), {"plan": {
        "key": "W45", "period": {"title": "Launch", start: "2000-01-01", end: "2000-01-31"},
        "items": [{"post_key": "P1", "post_at": "2000-01-05", "title": "Teaser"}]}})
    assert planned["items"][0]["not_before"] == "2000-01-01" and planned["items"][0]["not_after"] == "2000-01-31"
    # What the plan's Agent is asked to return does not change, and the person is not shown the dates twice.
    plan_item = _step(flows["campaign_plan"], "plan")["config"]["output_schema"]["properties"]["items"]["items"]
    assert "not_after" not in plan_item["properties"]
    assert "not_after" not in _step(flows["campaign_publish"], "approve_post")["config"]["message"]
    ready, on_time = _step(flows["campaign_publish"], "ready"), _step(flows["campaign_publish"], "on_time")
    ended = {"post_document_id": "D1", "not_before": "2000-01-01", "not_after": "2000-01-31"}
    assert _run(ready, {"item": {"state": ended}}) == {"ok": False, "issue": "The campaign ended on 2000-01-31"}
    assert _run(on_time, {"item": {"state": ended}, "publish_by": "2999-01-01T00:00:00+00:00"})["ok"] is False
    open_now = {"post_document_id": "D1", "not_before": "2000-01-01", "not_after": "2999-01-31"}
    assert _run(ready, {"item": {"state": open_now}})["ok"] is True
    assert _run(ready, {"item": {"state": {**open_now, "not_before": "2999-01-01"}}})["ok"] is False
    # Dates an Agent wrote that cannot be read bound nothing: the person confirmed that plan.
    assert _run(ready, {"item": {"state": {**open_now, "not_after": "soon"}}})["ok"] is True
    assert any("dates" in guard for guard in review_workflow(flows["campaign_publish"])["guards"])


def test_within_period_names_only_dates_the_period_keeps():
    options = {**SCENARIOS["music campaign"][1], "publish": {"channel": "instagram", "within_period": {"end": "closes"}}}
    with pytest.raises(ValueError, match="does not keep: closes"):
        content_calendar_flows(CAMPAIGN, name="Plan", **options)


@pytest.mark.parametrize("options", [
    # A shop's Instagram posts on set days.
    dict(slug="product_post", service_key="social", channel="Instagram", cron_expr="0 9 * * 1,3,5",
         timezone="America/Los_Angeles", item_word="post", item_fields=["title", "caption", "account_id"],
         requires=["account_id"]),
    # A weekly newsletter sent through another channel.
    dict(slug="newsletter", service_key="editor", channel="newsletter", cron_expr="0 8 * * 4", timezone="Asia/Shanghai",
         item_word="issue", action_keys=["email.send"]),
], ids=["instagram posts", "weekly newsletter"])
async def test_a_rhythm_with_no_plan_makes_and_publishes_one_item_per_occurrence(options):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    result = recurring_publish_flows(name="Make", make_prompt="Make it.", **options)
    validate_portable_recipe_resources({"workflows": result["workflows"]})
    flows = _by_slug(result)
    slug, item = options["slug"], options["item_word"]
    # Nothing but what the person named runs on a clock: no plan to confirm and no worker.
    assert set(flows) == {slug, f"{slug}_publish"}
    assert result["automations"] == [{"workflow": slug, "cron_expr": options["cron_expr"], "timezone": options["timezone"],
                                      "why": result["automations"][0]["why"]}]
    assert [row["key"] for row in flows[slug]["run_inputs"] if row["required"]] == []
    assert not [step for step in flows[slug]["steps"] if step["type"] == "wait"]
    # A second run of the same occurrence makes nothing.
    assert _step(flows[slug], "is_new")["false_next"] == ["seen"]
    key = _run(_step(flows[slug], "occurrence"), {})["key"]
    assert _step(flows[slug], "claim")["config"]["project_key"] == f"{slug}:{{{{occurrence.key}}}}" and len(key) == 13
    # The scheduler's occurrence survives delayed execution/replay and keeps
    # distinct same-hour ticks (and repeated DST wall times) independent.
    frequent = recurring_publish_flows(name="Make", make_prompt="Make it.",
                                       **{**options, "cron_expr": "*/30 * * * *"})
    occurrence = _step(_by_slug(frequent)[slug], "occurrence")
    periods = ["cron:2026-11-04T09:00:00+00:00", "cron:2026-11-04T09:30:00+00:00",
               "cron:2026-11-01T08:30:00+00:00", "cron:2026-11-01T09:30:00+00:00"]
    assert [_run(occurrence, {"period_key": period})["key"] for period in periods] == periods
    assert [_run(occurrence, {"start": {"period_key": period}})["key"] for period in periods] == periods
    assert _run(occurrence, {"start": {"period_key": periods[0]}, "period_key": "stale"})["key"] == periods[0]
    assert _run(occurrence, {"period_key": periods[0]})["key"] == periods[0]
    # Unbound scheduled Workflows expose trigger data under the start output;
    # bindings also merge it into top-level variables. Exercise the real handoff.
    start = _step(_by_slug(frequent)[slug], "start")
    run = WorkflowRun(id="scheduled-pattern-test", entity_id="pattern-test", variables={}, step_results={},
                      trigger_data={"period_key": periods[0]})
    runner = WorkflowRunner()
    started = await runner._execute_step(start, run, None)
    runner._record_step_result(start, started, run)
    assert _run(occurrence, run.variables)["key"] == periods[0]
    # What the Agent made is what the person approves and what is published.
    assert f"{item}_document_id" in _step(flows[slug], "make")["config"]["output_schema"]["required"]
    assert _step(flows[slug], "made")["config"]["patch"][f"{item}_document_id"] == "{{make.%s_document_id}}" % item
    assert _step(flows[slug], "publish")["config"]["workflow_id"] == f"{slug}_publish"
    publish = flows[f"{slug}_publish"]
    assert _step(publish, "grant")["config"]["approval_step_id"] == "approve_post"
    assert _step(publish, "receipt")["config"]["require_verified"] is True
    unmade = _run(_step(publish, "ready"), {"item": {"state": {}}})
    assert unmade["ok"] is False and f"{item}_document_id" in unmade["issue"]


def test_a_post_missing_what_it_needs_is_set_aside_before_anyone_is_asked():
    publish = _by_slug(content_calendar_flows(CAMPAIGN, name="Plan", **SCENARIOS["music campaign"][1]))["campaign_publish"]
    assert _step(publish, "made_item")["next"] == ["ready"]
    ready = _step(publish, "ready")
    # The account and the made file are read as the text the post keeps.
    assert _run(ready, {"item": {"state": {"channel": "instagram", "post_document_id": "D1"}}}) == {"ok": True, "issue": ""}
    unready = _run(ready, {"item": {"state": {"channel": " ", "post_document_id": "D1"}}})
    assert unready == {"ok": False, "issue": "Not ready to publish: missing channel"}
    assert _step(publish, "is_ready")["true_next"] == ["approve_post"]
    assert _step(publish, "not_ready")["config"]["patch"] == {"publish_issue": "{{ready.issue}}"}
    with pytest.raises(ValueError, match="does not keep: account_id"):
        content_calendar_flows(CAMPAIGN, name="Plan", **{**SCENARIOS["music campaign"][1],
                                                        "publish": {"channel": "instagram", "requires": ["account_id"]}})


def test_posts_on_several_channels_each_reach_their_own_channel():
    """The 2026-11-02 eval: hand-written routing compared 'Instagram' to the plan's 'instagram' and posted nothing."""
    flows = _by_slug(content_calendar_flows(CAMPAIGN, name="Plan", **{
        **SCENARIOS["music campaign"][1],
        "publish": {"channels": [{"channel": "Instagram"}, {"channel": "TikTok", "prompt": "Post as a short."}]},
    }))
    assert {"campaign_publish_instagram", "campaign_publish_tiktok", "campaign_publish"} <= set(flows)
    plan = flows["campaign_plan"]
    # The plan can name only a set-up channel, and names it as the Flows route by it.
    channel = _step(plan, "plan")["config"]["output_schema"]["properties"]["items"]["items"]["properties"]["channel"]
    assert channel["enum"] == ["instagram", "tiktok"]
    planned = _run(_step(plan, "calendar_plan"), {"plan": {"key": "W45", "period": {"title": "Launch"}, "items": [
        {"post_key": "P1", "post_at": "2026-11-05", "channel": " Instagram "}]}})
    assert planned["items"][0]["channel"] == "instagram"
    router = flows["campaign_publish"]
    assert _step(router, "on_instagram")["config"]["expression"] == "item.state.channel == 'instagram'"
    assert _step(router, "publish_tiktok")["config"]["workflow_id"] == "campaign_publish_tiktok"
    assert _step(router, "on_tiktok")["false_next"] == ["no_channel"]
    # The worker starts the router, so each due post reaches its own channel.
    assert _step(flows["campaign_due"], "run")["config"]["workflow_id"] == "campaign_publish"
    validate_portable_recipe_resources({"workflows": list(flows.values())})
    with pytest.raises(ValueError, match="add 'channel' to item_fields"):
        content_calendar_flows(NEWSLETTER, name="Plan", slug="news", service_key="editor", plan_prompt="Plan.",
                               publish={"channels": ["email", "web"]})


def test_a_report_records_assessed_stats_on_its_schedule():
    from packages.core.blueprints.payload import _validate_stat_assessment_producers
    from packages.core.services.workspace_goal_measurements import resolve_draft_goal_measurements

    result = report_stat_flows(
        slug="weekly_report", name="Weekly report", service_key="analyst", prompt="Report the week.",
        stats=[{"key": "posts_published", "name": "Posts published", "rubric": "Verified posts this week."},
               {"key": "campaign_score", "name": "Campaign score", "rubric": "0-100 rubric.", "unit": "score",
                "maximum": 100}],
        cron_expr="0 9 * * 1", timezone="Europe/Berlin",
    )
    resolve_draft_goal_measurements({"stats": result["stats"], "goals": []})
    [automation] = result["automations"]
    # The Blueprint export check: one Agent step records them, run by one shared Automation.
    _validate_stat_assessment_producers({
        "workflows": result["workflows"], "stats": result["stats"], "subscriptions": [{"service_key": "analyst"}],
        "scheduled_jobs": [{"job_id": "weekly", "execution_type": "workflow",
                            "execution_target": {"workflow_slug": automation["workflow"]}}],
    })


def test_every_approval_a_pattern_asks_can_send_the_work_back():
    """A wait that offers revise reads as an approval, not as the person confirming what they did."""
    from packages.core.services.workflow_contracts import workflow_wait_is_confirmation

    results = [content_calendar_flows(spec, name="Plan", **options) for spec, options in SCENARIOS.values()]
    results.append(project_tree_flows(
        slug="course", name="Plan", parent_type="course", child_type="lesson", plan_service_key="writer",
        plan_prompt="Plan.", make_service_key="writer", make_prompt="Write.", approve_made=True))
    waits = [step["config"] for result in results for workflow in result["workflows"] for step in workflow["steps"]
             if step["type"] == "wait" and "revise" in step["config"].get("options", [])]
    assert waits
    assert [config["message"] for config in waits if workflow_wait_is_confirmation(config)] == []


def test_each_decision_a_person_makes_reaches_the_projects_decision_log():
    """The 2026-11-03 rerun: jr drafts had no history_event, so a plan's and a post's decisions were kept nowhere."""
    flows = _by_slug(content_calendar_flows(CAMPAIGN, name="Plan", **{
        **SCENARIOS["music campaign"][1], "make": {"prompt": "Make it.", "approve": True}}))
    tree = _by_slug(project_tree_flows(
        slug="course", name="Plan", parent_type="course", child_type="lesson", plan_service_key="writer",
        plan_prompt="Plan.", make_service_key="writer", make_prompt="Write.", approve_made=True))
    for workflow, patch, approval in [
        (flows["campaign_plan"], "save_campaign", "approve_plan"),
        (flows["campaign_make"], "mark_post", "approve_made"),
        (flows["campaign_publish"], "published", "approve_post"),
        (flows["campaign_publish"], "not_published", "approve_post"),
        # A post the person skips stays where it was, with that decision kept.
        (flows["campaign_publish"], "keep_skipped", "approve_post"),
        (tree["course_plan"], "keep_decision", "approve_plan"),
        (tree["course_make"], "made", "approve_made"),
    ]:
        config = _step(workflow, patch)["config"]
        assert config["operation"] == "patch"
        assert config["history_event"]["decision"] == "{{%s_decision.choice}}" % approval, patch
        assert config["history_event"]["note"] == "{{%s_decision.note}}" % approval, patch
        assert "history" in config["state_schema"]["properties"], patch
    assert "current_stage" not in _step(flows["campaign_publish"], "keep_skipped")["config"]
    # No approval, nothing to log.
    unconfirmed = _by_slug(content_calendar_flows(CAMPAIGN, name="Plan", **{
        **SCENARIOS["instagram week"][1], "approve_plan": False}))
    assert "history_event" not in _step(unconfirmed["insta_week_plan"], "save_campaign")["config"]
    bare = _by_slug(project_tree_flows(
        slug="course", name="Plan", parent_type="course", child_type="lesson", plan_service_key="writer",
        plan_prompt="Plan.", make_service_key="writer", make_prompt="Write.", approve_plan=False))
    assert _step(bare["course_plan"], "save_plan")["next"] == ["done"]


@pytest.mark.parametrize("source, item, fields, handler, run_input", [
    ("the Gmail inbox", "email", ["sender", "subject"], "reply_to_email", "message_id"),
    ("comments on the Instagram posts", "comment", ["author"], "reply_to_comment", "comment_id"),
    # Nothing about this is about mail or social: a shop's new orders.
    ("the Shopify store", "order", ["customer"], "pack_order", "order_id"),
])
def test_a_watch_starts_a_flow_once_for_each_new_thing_in_a_connected_account(source, item, fields, handler, run_input):
    result = source_watch_flows(slug="watch", name="Look", service_key="care", source=source, prompt="Only customers.",
                                item_word=item, item_fields=fields, run_workflow=handler, run_input=run_input,
                                cron_expr="*/30 * * * *", timezone="America/Los_Angeles")
    handler_flow = {"slug": handler, "name": "Handle", "steps": [
        {"id": "start", "type": "trigger", "next": ["work"],
         "config": {"run_inputs": [{"key": run_input, "type": "string", "required": True}]}},
        {"id": "work", "type": "agent", "next": [], "config": {"service_key": "care", "prompt": "Handle {{%s}}." % run_input}},
    ], "run_inputs": [{"key": run_input, "type": "string", "required": True}]}
    validate_portable_recipe_resources({"workflows": [*result["workflows"], handler_flow]})
    flows = _by_slug(result)
    assert [row["workflow"] for row in result["automations"]] == ["watch_watch"]
    assert flows["watch_one"]["internal"] is True and flows["watch_watch"]["internal"] is False
    listed = _run(_step(flows["watch_watch"], "new_items"), {"find": {"could_read": True, "items": [
        {"key": " m1 ", fields[0]: "a"}, {"key": "m1", fields[0]: "again"}, {"key": ""}, "junk"]}})
    assert [row["key"] for row in listed["items"]] == ["m1"]
    # A source that cannot be read is neither "nothing new" nor a failure the person is told of.
    assert _step(flows["watch_watch"], "read_it")["false_next"] == ["unread"]
    one = flows["watch_one"]
    # Only a thing never seen starts its Flow, with the id the source gave it.
    assert _step(one, "claim")["config"]["project_key"] == "watch:{{item.key}}"
    assert _step(one, "is_new")["config"]["expression"] == "kept.claimed == true"
    assert _step(one, "is_new")["false_next"] == ["seen"]
    assert _step(one, "run")["config"]["inputs"] == [{"key": run_input, "value": "{{item.key}}"}]
    # One whose Flow fails is kept visibly, not retried behind the person's back.
    assert _step(one, "run")["config"]["error_next"] == ["not_handled"]


def test_a_project_tree_has_the_shape_the_app_lays_out():
    result = project_tree_flows(
        slug="course", name="Plan a course", parent_type="course", child_type="lesson", plan_service_key="writer",
        plan_prompt="Plan the course.", make_service_key="writer", make_prompt="Write the lesson.",
        parent_word="course", child_word="lesson", child_fields=["title", "synopsis"],
    )
    validate_portable_recipe_resources({"workflows": result["workflows"]})
    assert project_structure_issues(result["workflows"]) == []
    context = AppDesignContext.from_draft_fields({"workflows": result["workflows"]})
    laid_out = project_tree_pattern(context, {
        "workflow": "course_make", "parent_workflow": "course_plan", "parent_project_type": "course",
        "child_project_type": "lesson", "plan_path": "parts", "stages": {"in_production": "Making", "made": "Ready"},
        "done_stage": "made", "make": {"inputs": {"course_id": "parent", "lesson_code": "code"}},
    })
    assert laid_out["ok"], laid_out


@pytest.mark.parametrize("source, item, provider", [
    ("the Gmail inbox", "email", "gmail"),
    ("customer messages in Outlook", "message", "outlook"),
    ("comments on the Instagram posts", "comment", "facebook"),
    ("LinkedIn post comments", "comment", "linkedin"),
    # What the same platform's feed does not list is left to the Agent, and so is any other source.
    ("Instagram direct messages", "message", None),
    ("the Outlook calendar", "meeting", None),
    ("the Shopify store", "order", None),
])
def test_a_source_with_a_feed_is_read_without_the_model(source, item, provider):
    feed = source_feed_for(source, item)
    assert (feed.provider if feed else None) == provider
    if feed is None:
        return
    result = source_watch_flows(slug="watch", name="Look", service_key="care", source=source, prompt="Only customers.",
                                item_word=item, item_fields=["sender"], run_workflow="handle", run_input="item_id",
                                cron_expr="0 * * * *", timezone="UTC", feed=feed)
    handler = {"slug": "handle", "name": "Handle", "steps": [
        {"id": "start", "type": "trigger", "next": ["work"],
         "config": {"run_inputs": [{"key": "item_id", "type": "string", "required": True}]}},
        {"id": "work", "type": "agent", "next": [], "config": {"service_key": "care", "prompt": "Handle {{item_id}}."}},
    ], "run_inputs": [{"key": "item_id", "type": "string", "required": True}]}
    validate_portable_recipe_resources({"workflows": [*result["workflows"], handler]})
    watch = _by_slug(result)["watch_watch"]
    # Before the Agent nothing runs that costs a model call or a sandbox.
    before = [step["type"] for step in watch["steps"][:watch["steps"].index(_step(watch, "find"))]]
    assert before == ["trigger", "workflow_project", "tool", "condition", "condition", "end"]
    assert _step(watch, "read")["config"]["tool"] == feed.tool
    # Connected is not watched: a look does not read every account its owner has.
    assert "integration_account_selection" not in _step(watch, "read")["config"]["args"]
    assert _step(watch, "has_new")["false_next"] == ["nothing_new"]


def test_a_listing_tells_what_was_not_seen_before_across_every_account():
    from packages.core.ai.workflow_runner import _new_items_in_listing

    spec = {"items": "messages", "key": "id", "seen": "{{cursor.state.seen}}", "limit": 3}
    seen = {"cursor": {"state": {"seen": ["old", "older"]}}}
    assert _new_items_in_listing({"messages": [{"id": "old"}, {"id": "new", "nested": {"a": 1}, "gone": None}, "junk"]},
                                 spec, seen) == {
        "could_read": True, "has_new": True, "items": [{"id": "new", "nested": '{"a": 1}'}], "accounts_not_read": 0,
        "seen": ["new", "old", "older"]}
    both = {"integration_account_selection": "all", "results": [
        {"ok": True, "result": json.dumps({"messages": [{"id": "a"}]})},
        {"ok": False, "result": {"messages": [{"id": "failed"}]}, "error": "expired"},
        {"ok": True, "result": {"messages": [{"id": "b"}, {"id": "a"}]}},
    ]}
    listed = _new_items_in_listing(both, spec, {})
    assert [row["id"] for row in listed["items"]] == ["a", "b"] and listed["accounts_not_read"] == 1
    # Nothing new is not the same as not being able to read.
    assert _new_items_in_listing({"messages": []}, spec, seen)["could_read"] is True
    assert _new_items_in_listing({"error": "not connected"}, spec, seen)["could_read"] is False
    assert _new_items_in_listing("not json", spec, seen)["could_read"] is False


@pytest.mark.asyncio
async def test_a_menu_is_kept_by_hand_added_changed_from_its_values_and_taken_off(client, db_session):
    """Restaurant marketing (staging 2026-11-08): dishes are added, changed and taken off from the App.
    The three Flows run for real; a change that leaves a field out keeps its value."""
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.blueprints.workflow_patterns import RecordField, record_collection_flows
    from packages.core.models.workflow import WorkflowProject, WorkflowRun
    from tests.test_workflows import _auth, _create_workspace

    flows = record_collection_flows(slug="menu", item_type="dish", item_word="dish", title_field="name", fields=[
        RecordField("name", "Dish", required=True), RecordField("price", "Price", "money", required=True),
        RecordField("spice", "Spice", choices=(("mild", "Mild"), ("hot", "Hot"))), RecordField("photo", "Photo", "picture")],
        live_words="On the menu", off_words="Off the menu")
    assert not any(step["type"] == "agent" for flow in flows["workflows"] for step in flow["steps"])
    headers = await _auth(client, "menu_by_hand")
    workspace_id = await _create_workspace(client, headers, "menu_by_hand")
    bindings = {}
    for flow in flows["workflows"]:
        created = await client.post("/api/v1/workflows", headers=headers, json={
            "name": flow["name"], "workspace_id": workspace_id, "steps": flow["steps"]})
        assert created.status_code == 201, created.text
        bound = await client.post("/api/v1/workflows/bindings", headers=headers,
                                  json={"workflow_id": created.json()["id"], "workspace_id": workspace_id})
        assert bound.status_code == 201, bound.text
        bindings[flow["slug"]] = bound.json()["id"]

    async def run(slug, changes=None, **inputs):
        started = await client.post(f"/api/v1/workflows/bindings/{bindings[slug]}/run", headers=headers,
                                    json={"execute": False, "trigger_data": inputs})
        assert started.status_code == 201, started.text
        run_id = started.json()["id"]
        await WorkflowRunner().run(run_id)
        if changes is not None:
            # The change form opens holding the dish's stored values; the owner edits only the price.
            path = f"/api/v1/workflows/runs/{run_id}"
            action = (await client.get(path, headers=headers)).json()["resume_action"]
            form = json.dumps(action)
            assert action["step_id"] == "changes" and '"default": 38' in form and '"default": "Boiled fish"' in form, form[:600]
            resumed = await client.post(path + "/resume", headers=headers, json={
                "expected_step_id": "changes", "variables": {"changes": changes}})
            assert resumed.status_code == 200, resumed.text
            await WorkflowRunner().run(run_id)
        result = await db_session.get(WorkflowRun, run_id, populate_existing=True)
        assert result.status == "completed", result.error
        return result

    photo = [{"document_id": "01M6BH2SFJ6ZVJ0A7QW55CRQN0", "name": "fish.jpg"}]
    added = await run("menu_add", name="Boiled fish", price=38, spice="hot", photo=photo)
    dish_id = added.step_results["save"]["output"]["project_id"]

    async def dish():
        return await db_session.get(WorkflowProject, dish_id, populate_existing=True)

    stored = await dish()
    assert (stored.current_stage, stored.state["price"], stored.state["display"]) == (
        "active", 38, {"title": "Boiled fish", "status": "On the menu", "media": photo})
    # A change from the menu card: the new price, the rest left as it was.
    await run("menu_edit", changes={"price": 42}, item_id=dish_id)
    stored = await dish()
    assert (stored.state["name"], stored.state["price"], stored.state["photo"]) == ("Boiled fish", 42, photo)
    await run("menu_take_off", item_id=dish_id)
    stored = await dish()
    assert (stored.current_stage, stored.state["display"]["status"], stored.state["price"]) == ("retired", "Off the menu", 42)

