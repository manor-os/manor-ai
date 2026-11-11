"""The content calendar Flow builders: what a planned item becomes, and making one."""

from __future__ import annotations

import contextlib
import io
import json

from packages.core.blueprints.content_calendar import (
    CalendarFile, CalendarSpec, calendar_item_steps, calendar_plan_steps, calendar_producer_hook,
)

NEWSLETTER = CalendarSpec(
    period_type="newsletter_month", item_type="newsletter_issue", item_key_prefix="newsletter:issue:",
    item_key_field="issue_key", parent_field="month_id", date_field="send_on",
    item_fields=("month_id", "issue_key", "send_on", "title", "angle"),
    period_fields=("title", "start", "end"), stages=("idea", "drafted", "sent"), made_stage="drafted",
    files=(CalendarFile("draft_document_id", "draft_path", "Find the draft"),),
    period_word="month", item_word="issue",
)


def _run(step: dict, inputs: dict) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(step["config"]["code"], {"inputs": inputs})
    return out.getvalue()


def _step(steps: list[dict], step_id: str) -> dict:
    return next(step for step in steps if step["id"] == step_id)


def test_an_item_is_made_only_when_its_file_is_there_and_stays_made():
    steps = calendar_item_steps(NEWSLETTER)
    # Every step leads somewhere that exists; the Flow starts by looking for the named file.
    ids = {step["id"] for step in steps}
    assert all(nxt in ids for step in steps for nxt in step.get("next", []))
    assert _step(steps, "start")["next"] == ["draft_read"]
    describe = _step(steps, "issue_state")
    item = {"issue_key": "M1-I1", "send_on": "2026-11-05", "title": "Gear for winter", "stage": "drafted",
            "draft_path": "Workspaces/x/draft.md", "month_id": "P1", "unknown": "dropped"}
    missing = json.loads(_run(describe, {"item": item, "draft_read": {"error": "File not found"}}))
    assert missing["stage"] == "idea" and "draft_document_id" not in missing["state"]
    assert missing["key"] == "newsletter:issue:M1-I1" and "unknown" not in missing["state"]
    found = json.loads(_run(describe, {"item": item, "draft_read": {"content": "# Draft", "document_id": "D1"}}))
    assert (found["stage"], found["state"]["draft_document_id"]) == ("drafted", "D1")
    # Planning the month again keeps an issue already drafted.
    keep = _step(steps, "issue_stage")
    kept = json.loads(_run(keep, {"issue": {"stage": "idea"}, "issue_project": {"state": {"draft_document_id": "D1"}}}))
    assert kept == {"stage": "drafted"}
    assert json.loads(_run(keep, {"issue": {"stage": "idea"}, "issue_project": {"state": {}}})) == {"stage": "idea"}
    # Nor does it send an issue already sent back to drafted.
    sent = {"issue": {"stage": "drafted"}, "issue_project": {"current_stage": "sent", "state": {"draft_document_id": "D1"}}}
    assert json.loads(_run(keep, sent)) == {"stage": "sent"}


def test_a_plan_becomes_a_period_and_its_dated_items():
    steps = calendar_plan_steps(NEWSLETTER, plan_var="month_plan", item_workflow="WF_ITEM", then="end")
    assert [step["id"] for step in steps] == ["claim_month", "save_month", "issue_items", "save_issues",
                                              "month_issues", "stale_issues", "unplan_issues"]
    assert _step(steps, "unplan_issues")["next"] == ["end"]
    assert _step(steps, "claim_month")["config"]["project_key"] == "{{month_plan.key}}"
    assert _step(steps, "save_issues")["config"]["workflow_id"] == "WF_ITEM"
    dated = json.loads(_run(_step(steps, "issue_items"), {
        "month_plan": {"items": [{"id": "M1-I1", "issue_key": "M1-I1"}]}, "saved_month": {"project_id": "P1"}}))
    assert dated == {"items": [{"id": "M1-I1", "issue_key": "M1-I1", "month_id": "P1"}]}
    # Planned again without an issue, that issue leaves the month; the ones still planned stay.
    stale = json.loads(_run(_step(steps, "stale_issues"), {
        "issue_items": dated, "month_issues": {"projects": [
            {"project_key": "newsletter:issue:M1-I1", "current_stage": "idea", "state": {}},
            {"project_key": "newsletter:issue:M1-I9", "current_stage": "drafted", "state": {
                "month_id": "P1", "issue_key": "M1-I9", "send_on": "2026-11-20", "title": "Old", "angle": "",
                "draft_document_id": "D9"}}]}}))
    assert stale == {"items": [{"month_id": "", "issue_key": "M1-I9", "send_on": "2026-11-20", "title": "Old", "angle": "",
                                "id": "newsletter:issue:M1-I9", "stage": "drafted"}]}
    # It keeps every field its saved state needs, so the child Flow saves it like any other issue.
    left = json.loads(_run(_step(calendar_item_steps(NEWSLETTER), "issue_state"), {"item": stale["items"][0], "draft_read": {}}))
    assert left["state"]["month_id"] == "" and left["state"]["send_on"] == "2026-11-20"
    assert _step(steps, "unplan_issues")["config"]["error_next"] == ["end"]


def test_making_content_for_an_item_reads_it_and_marks_it_made():
    hook = calendar_producer_hook(NEWSLETTER, input_key="issue_id", input_label="Calendar issue",
                                  brief=(("Send on", "send_on"), ("Angle", "angle")), then="write", done_next="end",
                                  made_files={"draft_document_id": "{{saved.document_id}}"},
                                  format_field="title", format_choices=("long", "short"), format_var="length")
    assert hook["run_input"]["schema"]["x-ui"]["allowed_stages"] == ["idea", "drafted"]
    assert hook["brief"] == "{{issue_brief.text}}"
    entry = {step["id"]: step for step in hook["entry"]}
    assert entry["issue_given"]["true_next"] == ["get_issue"] and entry["issue_given"]["false_next"] == ["issue_brief"]
    brief = json.loads(_run(entry["issue_brief"], {"issue": {"state": {"send_on": "2026-11-05", "angle": "Layering", "title": "short"}}}))
    assert "- Angle: Layering" in brief["text"] and brief["format"] == "short"
    assert json.loads(_run(entry["issue_brief"], {})) == {"text": "", "format": ""}
    # The item's format replaces the form's default; without one the form's own choice stands.
    assert _run(entry["issue_format"], {"issue_brief": brief, "length": "long"}) == "short"
    assert _run(entry["issue_format"], {"issue_brief": {"format": ""}, "length": "long"}) == "long"
    mark = next(step for step in hook["exit"] if step["id"] == "mark_issue")
    assert mark["config"]["current_stage"] == "drafted" and mark["config"]["patch"] == {"draft_document_id": "{{saved.document_id}}"}
    assert mark["next"] == ["end"]
