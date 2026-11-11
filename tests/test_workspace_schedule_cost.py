"""What frequent schedules cost: which runs pay for the model, how often, and the words the person reads."""
import pytest

from packages.core.services.workspace_schedule_cost import (
    LookPrice,
    frequent_model_schedules,
    runs_model_every_time,
    runs_per_day,
)


@pytest.mark.parametrize("schedule, expected", [
    ({"every_seconds": 900}, 96),
    ({"cron_expr": "*/15 * * * *"}, 96),
    ({"cron_expr": "0 * * * *"}, 24),
    ({"cron_expr": "0 9,17 * * *"}, 2),
    ({"cron_expr": "0 9-17 * * 1-5"}, 9 * 5 / 7),
    ({"cron_expr": "0 19 * * 0"}, 1 / 7),
    ({"cron_expr": "0 * * 1 *"}, 24 * 31 * 400 / 146_097),
    ({"cron_expr": "0 0 31 * *"}, 7 * 400 / 146_097),
    ({"cron_expr": "0 0 30 2 *"}, 0),
    ({"cron_expr": "0-1000000000000 * * * *"}, 0),
    ({"every_seconds": -1}, 0),
    ({"every_seconds": "nan"}, 0),
    ({"cron_expr": "not a cron"}, 0),
    ({}, 0),
])
def test_runs_per_day(schedule, expected):
    assert runs_per_day(**schedule) == pytest.approx(expected)


WATCH = {"slug": "inbox_watch", "steps": [
    {"id": "start", "type": "trigger", "next": ["find"]},
    {"id": "find", "type": "agent", "next": ["read_it"]},
    {"id": "read_it", "type": "condition", "next": []}]}
WRAPPER = {"slug": "inbox_check", "steps": [
    {"id": "start", "type": "trigger", "next": ["check"]},
    {"id": "check", "type": "subworkflow", "next": ["done"], "config": {"workflow_id": "inbox_watch"}},
    {"id": "done", "type": "end", "next": []}]}
# A due worker only reaches its Agent when something is due: an idle run is free.
DUE = {"slug": "posts_due", "steps": [
    {"id": "start", "type": "trigger", "next": ["lease"]},
    {"id": "lease", "type": "workflow_project", "next": ["has_item"]},
    {"id": "has_item", "type": "condition", "next": [], "true_next": ["make"], "false_next": ["idle"]},
    {"id": "make", "type": "agent", "next": []},
    {"id": "idle", "type": "end", "next": []}]}


@pytest.mark.parametrize("slug, expected", [
    ("inbox_watch", True), ("inbox_check", True), ("posts_due", False), ("gone", False),
    ("reordered", True), ("stage_watch", True), ("stage_due", False), ("stage_exit", True),
    ("stage_stop", False), ("conditional_child", False),
])
def test_only_a_workflow_that_reaches_an_agent_before_any_branch_pays_every_run(slug, expected):
    def stage_flow(name, operations, target):
        return {"slug": name, "steps": [
            {"id": "start", "type": "trigger", "next": ["stage"]},
            {"id": "stage", "type": "stage", "next": ["model"], "config": {
                "entry_operation_id": "first", "operations": operations, "routes": {"exit": target}}},
            {"id": "model", "type": "agent", "next": []}]}

    watch = stage_flow("stage_watch", [{"id": "first", "type": "agent", "next": ["exit"]}], None)
    due = stage_flow("stage_due", [{"id": "first", "type": "condition", "next": [],
                                    "true_next": ["exit"], "false_next": []}], "model")
    exit_to_model = stage_flow("stage_exit", [{"id": "first", "type": "transform", "next": ["exit"]}], "model")
    stop = stage_flow("stage_stop", [{"id": "first", "type": "transform", "next": ["exit"]}], None)
    child = {"slug": "conditional_child", "steps": [
        {"id": "start", "type": "trigger", "next": ["child"]},
        {"id": "child", "type": "subworkflow", "next": ["model"], "config": {"workflow_id": "posts_due"}},
        {"id": "model", "type": "agent", "next": []}]}
    assert runs_model_every_time(slug, [WATCH, WRAPPER, DUE, watch, due, exit_to_model, stop, child,
                                      {**WATCH, "slug": "reordered", "steps": list(reversed(WATCH["steps"]))}]) is expected


def test_sub_flows_that_call_each_other_do_not_hang():
    loop = {"slug": "a", "steps": [{"id": "start", "type": "trigger", "next": ["s"]},
                                   {"id": "s", "type": "subworkflow", "next": [], "config": {"workflow_id": "a"}}]}
    assert runs_model_every_time("a", [loop]) is False


def test_a_watch_that_reads_its_source_itself_pays_for_no_look():
    from packages.core.blueprints.workflow_patterns import source_feed_for, source_watch_flows

    def flows(source, item):
        return source_watch_flows(slug="s", name="Look", service_key="care", source=source, prompt="Only customers.",
                                  item_word=item, item_fields=[], run_workflow="handle", run_input="item_id",
                                  cron_expr="0 * * * *", timezone="UTC", feed=source_feed_for(source, item))["workflows"]

    assert runs_model_every_time("s_watch", flows("the Gmail inbox", "email")) is False
    assert runs_model_every_time("s_watch", flows("the Shopify store", "order")) is True


def test_frequent_schedules_leave_out_due_workers_and_weekly_reports():
    def job(job_id, slug, **schedule):
        return {"job_id": job_id, "execution_target": {"workflow_slug": slug}, **schedule}

    report = {"slug": "weekly", "steps": [{"id": "start", "type": "trigger", "next": ["write"]},
                                          {"id": "write", "type": "agent", "next": []}]}
    fields = {"workflows": [WATCH, WRAPPER, DUE, report], "scheduled_jobs": [
        job("mail", "inbox_check", cron_expr="0 * * * *"), job("comments", "inbox_watch", every_seconds=900),
        job("due", "posts_due", cron_expr="*/15 * * * *"), job("sunday", "weekly", cron_expr="0 19 * * 0")]}
    assert frequent_model_schedules(fields) == [
        {"job": "mail", "workflow": "inbox_check", "looks_per_day": 24},
        {"job": "comments", "workflow": "inbox_watch", "looks_per_day": 96}]


def test_cost_is_in_credits_unless_it_goes_on_the_persons_own_key():
    assert LookPrice("m", byok=False, credits_per_look=2.5, usd_per_look=0.03).per_day(48) == "about 120 credits a day"
    assert LookPrice("m", byok=True, credits_per_look=2.5, usd_per_look=0.03).per_day(48) == "about $1.44 a day on the model key"
    # Self-hosted Manor has no credits.
    assert LookPrice("m", byok=False, credits_per_look=0, usd_per_look=0.03).per_day(2) == "about $0.06 a day on the model key"
