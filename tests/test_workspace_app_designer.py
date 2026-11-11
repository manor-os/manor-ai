"""The App design sub-agent: the Architect and Workspace chat command it and accept its page."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.builtin_app_views import BUILTIN_APP_VIEWS
from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Workspace
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services import workspace_app_designer
from packages.core.services.workspace_app_designer import (
    AppDesignContext, AppDesignResult, content_calendar_pattern, project_tree_pattern,
)
from packages.core.services.workspace_app_selfcheck import app_self_check_report, app_test_plan
from packages.core.services.workspace_app_view import (
    APP_VIEW_FIELDS, PREVIEW_PLATFORMS, AppViewContractError, normalize_app_view_config,
)

_CONFIGS = Path(__file__).resolve().parents[1] / "packages" / "core" / "blueprints" / "configs" / "creative"


def _recipe_workflows(name: str) -> list[dict]:
    return json.loads((_CONFIGS / f"{name}.json").read_text())["recipe"]["workflows"]


def _scripted(calls):
    """Stand in for the sub-agent's model: run the given real tool calls in order."""
    async def designer(context, **_kwargs):
        state: dict = {}
        tools = workspace_app_designer.app_designer_executor(context, state)
        replies = []
        for name, build in calls:
            args = build(replies) if callable(build) else build
            replies.append(json.loads(await tools(name, args)))
        return AppDesignResult(app_view=state.get("candidate"), recipes=state.get("recipes") or [],
                               warnings=state.get("warnings") or [], notes="scripted design",
                               attempts=state.get("attempts", 0))
    return designer


_SERIES_SCHEMA = {"type": "object", "properties": {
    "title": {"type": "string"}, "premise": {"type": "string"}, "episodes": {"type": "array"},
    "project_root": {"type": "string"}, "references": {"type": "object"}}}
_EPISODE_SCHEMA = {"type": "object", "properties": {
    "code": {"type": "string"}, "title": {"type": "string"}, "synopsis": {"type": "string"},
    "project_root": {"type": "string"}, "parent_project_id": {"type": "string"}}}
SERIES_FLOW = {
    "slug": "course_series", "name": "Course series",
    "steps": [
        {"id": "create_series", "type": "workflow_project", "name": "Plan the course", "next": ["claim_lesson"],
         "config": {"operation": "create", "project_type": "course", "schema_version": 1,
                    "state_schema": _SERIES_SCHEMA, "state": {"episodes": "{{brief_plan.lessons}}"},
                    "folder": {"name": "Course"},
                    "current_stage": "active"}},
        {"id": "claim_lesson", "type": "workflow_project", "name": "Make a lesson", "next": ["finish_lesson"],
         "config": {"operation": "claim", "project_type": "lesson", "project_key": "lesson", "schema_version": 1,
                    "state_schema": _EPISODE_SCHEMA, "state": {"parent_project_id": "{{create_series.project_id}}"},
                    "folder": {"name": "Lesson", "under": "{{create_series.state.project_root}}"},
                    "current_stage": "in_production"}},
        {"id": "finish_lesson", "type": "workflow_project", "name": "Finish the lesson", "next": [],
         "config": {"operation": "patch", "project_type": "lesson", "project_id": "{{claim_lesson.project_id}}",
                    "schema_version": 1, "state_schema": _EPISODE_SCHEMA, "patch": {}, "current_stage": "made",
                    "expected_revision": "{{claim_lesson.revision}}"}},
    ],
    "run_inputs": [
        {"key": "brief", "type": "string", "label": "What is the course about?", "required": True},
        {"key": "course_id", "type": "string", "label": "Course", "hidden": True,
         "schema": {"type": "string", "x-ui": {"control": "workflow_project_select", "project_type": "course"}}},
        {"key": "lesson_code", "type": "string", "label": "Lesson", "hidden": True},
        {"key": "lesson_id", "type": "string", "label": "Lesson", "hidden": True,
         "schema": {"type": "string", "x-ui": {"control": "workflow_project_select", "project_type": "lesson"}}},
    ],
}
_LESSON_TREE = {
    "workflow": "course_series", "parent_project_type": "course", "child_project_type": "lesson",
    "parent_label": "Courses", "child_label": "Lessons", "plan_path": "episodes",
    "about": [{"field": "premise", "label": "Premise"}], "child_fields": [{"field": "synopsis", "label": "Synopsis"}],
    "stages": {"in_production": "Making", "made": "Ready"}, "done_stage": "made",
    "references": [{"kind": "images", "label": "Visuals"}], "entry_prefix": "lesson",
    "remake": {"input": "lesson_id", "label": "Remake", "title": "Remake a lesson"},
    "make": {"inputs": {"course_id": "parent", "lesson_code": "code"}, "label": "Make this lesson"},
}


def _series_page(replies):
    fragment = replies[-1]["fragment"]
    return {"app_view": {
        "title": "Make a video course", "description": "Plan a course and make it lesson by lesson.",
        "hero": "workflow:course_series", "labels": {"workflow:course_series": "Plan a course"},
        **fragment,
    }, "recipes": ["one-flow studio", "project tree"]}


def test_the_project_tree_pattern_is_the_built_in_series_layout():
    context = AppDesignContext.from_draft_fields({"workflows": _recipe_workflows("creative-video-v1")})
    result = project_tree_pattern(context, {
        "workflow": "produce-video-v1", "parent_project_type": "video_series", "child_project_type": "video_episode",
        "parent_label": "Series", "child_label": "Episodes", "plan_path": "episodes",
        "about": [{"field": "premise", "label": "Premise"}], "child_fields": [{"field": "synopsis", "label": "Synopsis"}],
        "stages": {"in_production": "Making", "made": "Ready"}, "done_stage": "made",
        "references": [{"kind": "voices", "label": "Voices"}, {"kind": "images", "label": "Visuals"}],
        "entry_prefix": "series", "remake": {"input": "episode_id", "label": "Remake"},
        "make": {"inputs": {"series_id": "parent", "episode_code": "code"}, "label": "Make this episode"},
    })
    built_in = BUILTIN_APP_VIEWS["creative-video-v1"]
    fragment = result["fragment"]
    assert (fragment["data_sources"], fragment["components"]) == (built_in["data_sources"], built_in["components"])
    assert set(fragment["hidden"]) == set(built_in["hidden"])
    assert fragment["asks"] == {key: built_in["asks"][key] for key in fragment["hidden"]}
    # Inputs the Flow does not take are refused, not silently laid out.
    refused = project_tree_pattern(context, {**_LESSON_TREE, "workflow": "produce-video-v1",
                                             "parent_project_type": "video_series", "child_project_type": "video_episode"})
    assert refused["ok"] is False and any("remake.input" in error for error in refused["errors"])


_SLOT_SCHEMA = {"type": "object", "properties": {
    "week_id": {"type": "string"}, "title": {"type": "string"}, "due_at": {"type": "string"},
    "hook": {"type": "string"}, "script_document_id": {"type": "string"}}}
WEEK_PLAN_FLOW = {
    "slug": "plan_week", "name": "Plan the week",
    "steps": [
        {"id": "claim_week", "type": "workflow_project", "name": "Find the week", "next": ["save_slot"],
         "config": {"operation": "claim", "project_type": "week", "project_key": "{{plan.key}}", "schema_version": 1,
                    "state_schema": {"type": "object", "properties": {
                        "title": {"type": "string"}, "start": {"type": "string"}, "plan_document_id": {"type": "string"}}},
                    "current_stage": "planned"}},
        {"id": "save_slot", "type": "workflow_project", "name": "Save a slot", "next": [],
         "config": {"operation": "patch", "project_type": "slot", "project_id": "{{slot.project_id}}",
                    "schema_version": 1, "state_schema": _SLOT_SCHEMA, "patch": "{{slot.state}}",
                    "current_stage": "{{slot.stage}}", "allowed_stages": ["idea", "draft", "published"]}},
    ],
}
WRITE_SCRIPT_FLOW = {
    "slug": "write_script", "name": "Write a script", "steps": [],
    "run_inputs": [{"key": "brief", "type": "string", "label": "Request"},
                   {"key": "plan_slot", "type": "string", "label": "Calendar slot",
                    "schema": {"type": "string", "x-ui": {"control": "workflow_project_select", "project_type": "slot"}}}],
}
_WEEK_CALENDAR = {
    "workflow": "plan_week", "period_project_type": "week", "item_project_type": "slot",
    "parent_field": "week_id", "date_field": "due_at", "period_label": "Week", "collection_title": "Weeks",
    "item_label": "Calendar", "date_label": "Post on", "plan_document_field": "plan_document_id",
    "item_fields": [{"field": "hook", "label": "Hook"}, {"field": "script_document_id", "label": "Script", "format": "document"}],
    "period_fields": [{"field": "start", "label": "Starts"}],
    "stages": {"idea": "Needs a script", "draft": "Script drafted", "published": "Published"},
    "done_stage": "published", "attention_stages": ["idea"],
    "action": {"workflow": "write_script", "input": "plan_slot", "label": "Write the script", "stages": ["idea"]},
}


def test_the_content_calendar_pattern_is_a_runnable_schedule():
    """A week's dated slots on the calendar, each idea opening the script Flow with itself chosen."""
    context = AppDesignContext.from_draft_fields({"workflows": [WEEK_PLAN_FLOW, WRITE_SCRIPT_FLOW]})
    result = content_calendar_pattern(context, _WEEK_CALENDAR)
    assert result["ok"], result
    page = {"title": "Weekly plan", "hero": "workflow:plan_week", **result["fragment"]}
    config, errors, _warnings = context.check(page)
    assert errors == []
    component = config["components"][0]
    calendar = next(block for block in component["blocks"] if block["key"] == "calendar")
    assert calendar["item_action"] == {"action": "workflow:write_script", "input": "plan_slot",
                                       "label": "Write the script", "stages": ["idea"]}
    assert component["display"]["calendar"]["attention_stages"] == ["idea"]
    # The plan document reads in the week's header, beside its progress.
    assert component["display"]["groups"][0]["blocks"] == ["progress", "plan"]
    # Fields the Flows do not keep, and buttons for inputs they do not take, are refused.
    for change, error in ((
        {"item_fields": [{"field": "caption", "label": "Caption"}]}, "caption is not declared"), (
        {"action": {**_WEEK_CALENDAR["action"], "input": "slot_id"}}, "action.input"), (
        {"stages": {"queued": "Queued", "published": "Published"}}, "stages must label")):
        refused = content_calendar_pattern(context, {**_WEEK_CALENDAR, **change})
        assert refused["ok"] is False and any(error in message for message in refused["errors"]), refused


@pytest.mark.parametrize("change, error", [
    (lambda page: page["presets"].update({"action:deck-pitch": {"slides": "10"}}), "inputs the Workflow does not take"),
    (lambda page: page["targets"].pop("action:deck-pitch"), "has no target"),
    (lambda page: page["order"].append("service:missing"), "is not an entry of this Workspace"),
    (lambda page: page["asks"].update({"action:deck-pitch": ["episode_id"]}), "inputs the Workflow does not take"),
    (lambda page: page.update(hidden=[page["hero"]]), "hero entry cannot be hidden"),
])
def test_a_candidate_must_be_runnable_on_this_workspace(change, error):
    """A second, unrelated scenario: one Flow offered as several deck kinds."""
    context = AppDesignContext.from_draft_fields({"workflows": _recipe_workflows("creative-presentation-v1")})
    page = copy.deepcopy(BUILTIN_APP_VIEWS["creative-presentation-v1"])
    config, errors, warnings = context.check(page)
    assert (errors, warnings) == ([], []) and config["presets"]
    page.setdefault("asks", {})
    change(page)
    _, errors, _ = context.check(page)
    assert any(error in message for message in errors), errors


_INSPECTION_SCHEMA = {"type": "object", "properties": {
    "address": {"type": "string"}, "due_date": {"type": "string"}, "project_root": {"type": "string"},
    "report": {"type": "object", "properties": {"final_document_id": {"type": "string"}}}}}
INSPECTION_FLOW = {
    "slug": "inspect_property", "name": "Inspect a property",
    "steps": [
        {"id": "open", "type": "workflow_project", "name": "Open the job", "next": ["deliver"],
         "config": {"operation": "create", "project_type": "inspection", "project_key": "{{address}}",
                    "schema_version": 1, "state_schema": _INSPECTION_SCHEMA, "folder": {"name": "{{address}}"},
                    "state": {"address": "{{address}}", "due_date": "{{due_date}}"}, "current_stage": "visiting"}},
        {"id": "deliver", "type": "workflow_project", "name": "Deliver the report", "next": [],
         "config": {"operation": "patch", "project_type": "inspection", "project_id": "{{open.project_id}}",
                    "schema_version": 1, "state_schema": _INSPECTION_SCHEMA, "current_stage": "delivered",
                    "patch": {"report": {"final_document_id": "{{write.final_document_id}}"}},
                    "expected_revision": "{{open.revision}}"}},
    ],
    "run_inputs": [{"key": "address", "type": "string", "label": "Address", "required": True},
                   {"key": "due_date", "type": "string", "label": "Due"}],
}


def _inspection_page(*, summary_fields, documents=True, named=True):
    """Recipe 5 over the inspection Flow; ``documents`` adds the report and folder blocks,
    ``named`` names each job in the picker by its address."""
    sources = {"jobs": {"type": "workflow_projects", "project_type": "inspection",
                        "producer_workflows": ["workflow_definition:inspect_property"],
                        "fields": [field["field"] for field in summary_fields]}}
    if named:
        sources["jobs"]["label_fields"] = [["address"]]
    blocks = [{"key": "details", "type": "summary", "source": "jobs", "fields": summary_fields}]
    if documents:
        sources.update(report={"type": "project_documents", "parent": "jobs", "document_role": "report",
                               "document_path": ["report", "final_document_id"]},
                       files={"type": "project_files", "parent": "jobs"})
        blocks += [{"key": "report", "type": "documents", "source": "report"},
                   {"key": "files", "type": "documents", "source": "files"}]
    return {"hero": "workflow:inspect_property", "data_sources": sources, "components": [
        {"key": "progress", "type": "data_view", "context": {"kind": "workspace"},
         "blocks": [{"key": "stages", "type": "progress", "source": "jobs"}]},
        {"key": "job", "type": "data_view",
         "context": {"kind": "project", "source": "jobs", "label": "Job", "display": "cards"}, "blocks": blocks}]}


def test_a_projects_deliverables_open_as_documents_never_as_text():
    """The designer is told which fields hold a document id, and a page that shows
    one (or the folder path) as text, or leaves the deliverable out, is sent back."""
    context = AppDesignContext.from_draft_fields({"workflows": [INSPECTION_FLOW]})
    project = context.summary()["workflows"][0]["projects"][0]
    assert (project["document_fields"], project["has_folder"]) == ([["report", "final_document_id"]], True)
    address = {"field": ["address"], "label": "Address"}

    _, errors, warnings = context.check(_inspection_page(summary_fields=[address]))
    assert (errors, warnings) == ([], [])

    as_text = [address, {"field": ["report", "final_document_id"], "label": "Report"},
               {"field": ["project_root"], "label": "Folder"}]
    _, errors, warnings = context.check(_inspection_page(summary_fields=as_text, documents=False))
    assert len(errors) == 2, errors
    assert "report.final_document_id as text, but it holds a document id" in errors[0]
    assert "project_root as text" in errors[1]
    assert any("shows none" in message for message in warnings)
    assert any("keeps a folder the page does not show" in message for message in warnings)

    # A ``document`` field opens the report; the folder still has to be shown.
    as_document = [address, {"field": ["report", "final_document_id"], "label": "Report", "format": "document"}]
    _, errors, warnings = context.check(_inspection_page(summary_fields=as_document, documents=False))
    assert errors == [] and len(warnings) == 1 and "folder" in warnings[0]

    _, errors, warnings = context.check({"hero": "workflow:inspect_property"})
    assert errors == [] and any("not on the page" in message for message in warnings)


def _first_guess_page():
    """A first submit as the designer writes it before it knows the shape (local replay 2026-11-04)."""
    job_source = {"type": "workflow_projects", "project_type": "inspection", "workflow": "workflow_definition:inspect_property"}
    return {
        "hero": "workflow:inspect_property",
        "sections": [{"block": "outputs"}, {"block": "outputs"}],
        "data_sources": {"jobs": job_source, "waiting": {"kind": "workflow_projects", "stages": ["visiting"]},
                         "report": {"type": "project_documents", "parent": "jobs", "document_path": "report"}},
        "components": [
            {"key": "overview", "context": {"kind": "workspace"},
             "display": {"layout": "tabs", "tabs": [{"key": "all", "blocks": ["stages"]}]},
             "blocks": [{"key": "stages", "type": "progress", "source": "waiting", "stage_labels": {"visiting": "On site"}},
                        {"key": "bad-key", "type": "progress", "source": "jobs"}]},
            {"key": "job", "type": "data_view", "context": {"kind": "project", "source": "jobs", "display": "cards"},
             "blocks": [{"key": "details", "type": "summary", "source": "jobs",
                         "fields": [{"field": "address", "label": "Address"}]},
                        {"key": "rollup", "type": "project_rollup", "source": "report", "count_by_stage": True,
                         "shown": True}]},
        ],
    }


def test_one_submit_reports_every_problem_naming_the_field_and_what_is_allowed():
    """Local replay 2026-11-04: one nameless error per submit, 11 of 12 rejected, and the page shrank to pass."""
    context = AppDesignContext.from_draft_fields({"workflows": [INSPECTION_FLOW]})
    config, errors, _ = context.check(_first_guess_page())
    assert config is None

    def problem(*parts):
        found = [message for message in errors if all(part in message for part in parts)]
        assert found, (parts, errors)
        return found[0]

    # The source: the field it names, the fields its type takes, and what it still lacks.
    wrong_field = problem("app_view.data_sources.jobs", "unsupported fields workflow")
    assert "requires project_type, producer_workflows" in wrong_field and "label_fields" in wrong_field
    problem("app_view.data_sources.jobs", "require producer_workflows")
    assert "workflow_projects" in problem("app_view.data_sources.waiting", "source type None in type")
    assert 'such as ["title"]' in problem("app_view.data_sources.report", "document_path", "'report'")
    # The component, its display and each block on its own.
    problem("app_view.components.overview", "set type to data_view")
    assert "layout, groups" in problem("app_view.components.overview", "display", "unsupported fields tabs")
    problem("app_view.components.overview.blocks[1]", "key 'bad-key'")
    problem("app_view.components.job.blocks.details", "summary field path", "'address'")
    assert "item_action" in problem("app_view.components.job.blocks.rollup", "unsupported fields shown")
    # The page's other parts in the same pass.
    assert "own id" in problem("app_view section ids must be unique", "outputs")

    # Each one is about this page, so nothing in it can be fixed by deleting something it does not name.
    assert len(errors) == len(set(errors)) == 10, errors


def test_the_shape_the_designer_is_given_is_a_page_the_checks_accept():
    """The designer writes its first submit from this example, so a stale one costs a rejected submit on every design."""
    context = AppDesignContext.from_draft_fields({"workflows": [INSPECTION_FLOW]})
    catalog = context.summary()["module_catalog"]
    assert normalize_app_view_config(catalog["page_example"])
    for source_type, takes in catalog["data_source_fields"].items():
        # Every field the catalog lists passes the field check; one it does not list is refused by name.
        source = {"type": source_type, **{name: None for name in [*takes["required"], *takes["optional"]]}, "not_listed": 1}
        with pytest.raises(ValueError) as rejected:
            normalize_app_view_config({"data_sources": {"one": source}})
        unsupported = [message for message in rejected.value.problems if "unsupported fields" in message]
        assert len(unsupported) == 1 and "unsupported fields not_listed:" in unsupported[0]


def test_every_unavailable_target_is_named_at_once_with_what_is_available():
    """Local replay 2026-11-04: two internal Flows as targets took two more submits, one error each."""
    context = AppDesignContext.from_draft_fields({"workflows": [INSPECTION_FLOW, {**WEEK_PLAN_FLOW, "internal": True}]})
    page = _inspection_page(summary_fields=[{"field": ["address"], "label": "Address"}])
    internal = WEEK_PLAN_FLOW["slug"]
    page.update(entries={"action:one": {"title": "One"}, "action:two": {"title": "Two"}},
                targets={"action:one": f"workflow:{internal}", "action:two": "workflow:not_a_flow"})
    _, errors, _ = context.check(page)
    named = [message for message in errors if "is not included" in message]
    assert len(named) == 2 and all("it includes inspect_property" in message for message in named), errors


def test_a_source_with_a_problem_invents_no_problems_in_what_reads_it():
    """Its own error says what to fix: a block over it is neither undeclared nor reading undeclared fields."""
    context = AppDesignContext.from_draft_fields({"workflows": [INSPECTION_FLOW]})
    page = _inspection_page(summary_fields=[{"field": ["address"], "label": "Address"}])
    page["data_sources"]["jobs"]["workflow"] = "workflow_definition:inspect_property"
    page["data_sources"]["files"]["unknown"] = True
    _, errors, _ = context.check(page)
    assert [message.split(":", 1)[0] for message in errors] == [
        "app_view.data_sources.jobs", "app_view.data_sources.files"], errors


def test_checks_past_the_shape_report_together_and_name_what_the_producer_declares():
    """A page that reads well gets its schema and coverage problems in one reply, not one per submit."""
    context = AppDesignContext.from_draft_fields({"workflows": [INSPECTION_FLOW]})
    page = _inspection_page(summary_fields=[{"field": ["address"], "label": "Address"},
                                            {"field": ["report", "final_document_id"], "label": "Report"},
                                            {"field": ["notes"], "label": "Notes"},
                                            {"field": ["summary"], "label": "Summary"}], named=False)
    page["order"] = ["workflow:not_a_flow"]
    config, errors, warnings = context.check(page)
    assert config is not None
    absent = next(message for message in errors if "absent from its producer schema" in message)
    assert "notes, summary" in absent and "address" in absent and "due_date" in absent
    assert any("every one reads 'Inspection'" in message for message in errors), errors
    assert any("as text, but it holds a document id" in message for message in errors), errors
    assert any("workflow:not_a_flow is not an entry" in message for message in errors), errors


def test_a_block_names_the_fields_its_source_does_not_declare():
    context = AppDesignContext.from_draft_fields({"workflows": [INSPECTION_FLOW]})
    page = _inspection_page(summary_fields=[{"field": ["address"], "label": "Address"}])
    page["components"][1]["blocks"][0]["fields"].append({"field": ["due_date"], "label": "Due"})
    _, errors, _ = context.check(page)
    assert len(errors) == 1, errors
    assert errors[0].startswith("app_view.components.job.blocks.details:")
    assert "add due_date to the fields of source 'jobs', which lists address" in errors[0]


def test_other_surfaces_keep_a_value_error_with_every_problem_in_its_text():
    """Blueprint validation, draft issues and the routes report ``str(exc)``; any built-in page is rejected the same way."""
    page = copy.deepcopy(next(view for view in BUILTIN_APP_VIEWS.values() if view.get("data_sources") and view.get("components")))
    assert normalize_app_view_config(page)
    name = next(iter(page["data_sources"]))
    page["data_sources"][name]["producer"] = "x"
    page["components"][0]["unknown"] = True
    with pytest.raises(ValueError) as rejected:
        normalize_app_view_config(page)
    assert isinstance(rejected.value, AppViewContractError) and len(rejected.value.problems) == 2
    assert all(message in str(rejected.value) for message in rejected.value.problems)
    assert f"app_view.data_sources.{name}: contains unsupported fields producer" in str(rejected.value)


async def test_the_submit_tool_returns_every_problem_and_keeps_no_candidate():
    context = AppDesignContext.from_draft_fields({"workflows": [INSPECTION_FLOW]})
    state: dict = {}
    tools = workspace_app_designer.app_designer_executor(context, state)
    reply = json.loads(await tools("app_submit_candidate", {"app_view": _first_guess_page(), "recipes": []}))
    assert reply["ok"] is False and len(reply["errors"]) > 1
    assert "candidate" not in state and state["attempts"] == 1


async def test_a_designed_page_needs_a_picture_for_a_picture_layout_and_carries_brand_and_dates():
    """Local Lumen 2026-11-06: a brief asking for a look got layout cover with no picture (an empty
    panel), no icons and its dated posts only in a list. A Blueprint's own page is not held to this."""
    context = AppDesignContext.from_draft_fields({"workflows": [INSPECTION_FLOW],
                                                  "services": [{"key": "intake", "name": "Intake"}]})
    page = _inspection_page(summary_fields=[{"field": ["address"], "label": "Address"}])
    page.update(layout="cover", visual="image")
    config, errors, warnings = context.check(page)
    assert (errors, warnings) == ([], [])

    look_errors, look_warnings = context.look(config)
    assert len(look_errors) == 2 and "layout: cover and visual: image needs a picture" in look_errors[0]
    assert look_errors[1].startswith("service:intake, workflow:inspect_property have no icon")
    assert any("no theme.accent" in warning for warning in look_warnings)
    assert any(warning.startswith("inspection is dated (due_date)") for warning in look_warnings)
    reply = json.loads(await workspace_app_designer.app_designer_executor(context, {})(
        "app_submit_candidate", {"app_view": page, "recipes": []}))
    assert reply == {"ok": False, "errors": look_errors}

    # The Workspace's own cover is a picture; a Workspace-wide calendar shows the dated jobs.
    context.workspace_cover = True
    page.update(theme={"accent": "#b5793a"}, icons={"service:intake": "clipboard", "workflow:inspect_property": "search"})
    page["data_sources"]["jobs"].update(fields=[["address"], ["due_date"]],
                                        order={"field": ["due_date"], "type": "datetime", "direction": "asc"})
    page["components"].append({"key": "dates", "type": "data_view", "context": {"kind": "workspace"},
                               "blocks": [{"key": "due", "type": "progress", "source": "jobs"}],
                               "display": {"calendar": {"block": "due", "date_field": ["due_date"],
                                                        "attention_stages": [], "collection_title": "Visits"}}})
    config, errors, _ = context.check(page)
    assert errors == [] and context.look(config) == ([], [])


def test_dated_amounts_only_listed_are_offered_a_chart():
    """A sales pipeline's deals carry an amount and a close date: as rows alone nobody sees revenue by month."""
    from packages.core.services.workspace_app_view import app_module_catalog

    schema = {"type": "object", "properties": {"account": {"type": "string"}, "amount": {"type": "number"},
                                               "closed_at": {"type": "string", "format": "date-time"}}}
    flow = {"slug": "log_deal", "name": "Log a deal", "steps": [
        {"id": "save", "type": "workflow_project", "name": "Save the deal", "next": [],
         "config": {"operation": "create", "project_type": "deal", "project_key": "{{account}}", "schema_version": 1,
                    "state_schema": schema, "state": {"account": "{{account}}"}, "current_stage": "lead"}}],
        "run_inputs": [{"key": "account", "type": "string", "label": "Account", "required": True}]}
    context = AppDesignContext.from_draft_fields({"workflows": [flow]})
    source = {"type": "workflow_projects", "project_type": "deal", "producer_workflows": ["workflow_definition:log_deal"],
              "label_fields": [["account"]], "fields": [["account"], ["amount"], ["closed_at"]],
              "order": {"field": ["closed_at"], "type": "datetime", "direction": "desc"}}
    listed = {"key": "deals", "type": "progress", "source": "deals"}
    page = {"layout": "list", "theme": {"accent": "#334155"}, "data_sources": {"deals": source}, "components": [
        {"key": "deals", "type": "data_view", "context": {"kind": "workspace"}, "blocks": [listed],
         "display": {"calendar": {"block": "deals", "date_field": ["closed_at"], "attention_stages": [], "collection_title": "Deals"}}}]}
    config, errors, _ = context.check(page)
    assert errors == []
    assert [warning for warning in context.look(config)[1] if "only lists them" in warning] == [
        "deal keeps amounts (amount) by date (closed_at) but only lists them: a chart block shows the trend, e.g. chart "
        "{kind: line, x: {field: [closed_at], bucket: month}, value: {agg: sum, field: [amount]}}, or add totals to its table"]
    page["components"][0]["blocks"].append({"key": "revenue", "type": "chart", "source": "deals", "chart": {
        "kind": "line", "x": {"field": ["closed_at"], "bucket": "month"}, "value": {"agg": "sum", "field": ["amount"]}}})
    config, errors, _ = context.check(page)
    assert errors == [] and not any("only lists them" in warning for warning in context.look(config)[1])
    # The designer composes from the catalog the normalizer itself reads.
    catalog = app_module_catalog()
    assert "chart" in catalog["data_blocks"] and "funnel" in catalog["data_block_chart"]["kinds"]


def test_a_social_post_is_previewed_and_a_platform_without_a_frame_is_named():
    """A thought-leadership Workspace writes each insight as posts: listed as fields, nobody sees the post."""
    schema = {"type": "object", "properties": {
        "title": {"type": "string"}, "caption": {"type": "string"}, "images": {"type": "array"},
        "platform": {"type": "string", "enum": ["linkedin", "twitter", "weibo"]}}}
    flow = {"slug": "write_post", "name": "Write a post", "steps": [
        {"id": "save", "type": "workflow_project", "name": "Save the post", "next": [],
         "config": {"operation": "create", "project_type": "post", "project_key": "{{title}}", "schema_version": 1,
                    "state_schema": schema, "state": {"title": "{{title}}"}, "current_stage": "draft"}}],
        "run_inputs": [{"key": "title", "type": "string", "label": "Title", "required": True}]}
    context = AppDesignContext.from_draft_fields({"workflows": [flow]})
    page = {"layout": "list", "theme": {"accent": "#334155"}, "data_sources": {"posts": {
        "type": "workflow_projects", "project_type": "post", "producer_workflows": ["workflow_definition:write_post"],
        "label_fields": [["title"]], "fields": [["title"], ["caption"], ["images"], ["platform"]]}},
        "components": [{"key": "posts", "type": "data_view", "context": {"kind": "workspace"}, "blocks": [
            {"key": "posts", "type": "progress", "source": "posts", "fields": [
                {"field": ["platform"], "label": "Platform", "labels": {"linkedin": "LinkedIn", "twitter": "X", "weibo": "Weibo"}}]}]}]}
    config, errors, _ = context.check(page)
    assert errors == []
    assert any(warning.startswith("post is a social post (platform, caption) but only listed")
               for warning in context.look(config)[1])

    page["components"][0]["blocks"][0]["preview"] = {"fields": {"platform": ["platform"], "caption": ["caption"],
                                                               "images": ["images"]}}
    config, errors, warnings = context.check(page)
    assert errors == [] and not any("social post" in warning for warning in context.look(config)[1])
    # twitter reads as x; weibo has no frame and shows as a plain post, which the designer is told.
    assert [warning for warning in warnings if "no platform frame" in warning] == [
        "posts.posts previews each record on its platform, but weibo is no platform frame, so it shows as a plain "
        f"post: name it as one of {', '.join(PREVIEW_PLATFORMS)}"]

    # The App's own check reads the post's words, and a post with no words or pictures is blank.
    from packages.core.services.workspace_app_selfcheck import _value_finding
    assert _value_finding("posts", "progress", [{"label": "Pricing", "preview": {"caption": "Price first."}}]) is None
    assert "an unfilled template in its post caption" in _value_finding(
        "posts", "progress", [{"label": "Pricing", "preview": {"caption": "{{draft.caption}}"}}])["message"]
    assert "a post with no words or pictures" in _value_finding(
        "posts", "progress", [{"label": "Pricing", "preview": {"hashtags": ["pricing"]}}])["message"]


def test_each_project_in_a_picker_is_named_by_its_own_fields():
    """Staging 2026-11-02: the article picker showed two cards, both "Client Article"."""
    context = AppDesignContext.from_draft_fields({"workflows": [INSPECTION_FLOW]})
    page = _inspection_page(summary_fields=[{"field": ["address"], "label": "Address"}], named=False)
    _, errors, _ = context.check(page)
    assert len(errors) == 1 and "every one reads 'Inspection'" in errors[0], errors
    assert errors[0].endswith("(they carry address, due_date)")

    page["data_sources"]["jobs"]["label_fields"] = [["address"]]
    assert context.check(page)[1] == []


def test_a_project_waiting_on_a_person_is_on_the_front_page():
    """Local eval 2026-11-03: the studio's article waited for the owner's approval, shown only inside each article."""
    approve = {"id": "approve", "type": "wait", "name": "Owner approval", "next": ["deliver"],
               "config": {"wait_type": "approval", "options": ["approve", "revise"], "approval_values": ["approve"],
                          "response_variable": "decision", "message": "Approve the report?"}}
    flow = {**INSPECTION_FLOW, "steps": [INSPECTION_FLOW["steps"][0], approve, *INSPECTION_FLOW["steps"][1:]]}
    context = AppDesignContext.from_draft_fields({"workflows": [flow]})
    page = _inspection_page(summary_fields=[{"field": ["address"], "label": "Address"}])
    assert context.check(page)[1] == []

    # The same waiting list, but only inside the job a person picked.
    overview = page["components"].pop(0)
    page["components"][0]["blocks"].insert(0, overview["blocks"][0])
    _, errors, _ = context.check(page)
    assert len(errors) == 1 and "inspect_property stops for a person's decision on each inspection" in errors[0], errors
    # A Flow that never waits on anyone needs no front-page list.
    assert AppDesignContext.from_draft_fields({"workflows": [INSPECTION_FLOW]}).check(page)[1] == []


def test_a_page_keeps_where_a_decision_shows():
    approve = {"id": "approve", "type": "wait", "name": "Owner approval", "next": [],
               "config": {"wait_type": "approval", "message": "Approve?"}}
    waiting = AppDesignContext.from_draft_fields({"workflows": [{**INSPECTION_FLOW, "steps": [
        INSPECTION_FLOW["steps"][0], approve]}]})
    page = _inspection_page(summary_fields=[{"field": ["address"], "label": "Address"}])
    assert waiting.check(page)[1] == []  # the default page has the queue

    actions = {"id": "actions", "block": "actions", "view": "default"}
    queue = {"id": "queue", "block": "queue", "source": "approvals:pending", "view": "list"}
    _, errors, _ = waiting.check({**page, "sections": [actions]})
    assert any("no queue block" in error for error in errors), errors
    assert not any("no queue block" in error for error in waiting.check({**page, "sections": [actions, queue]})[1])
    # A Flow nobody waits on may leave it out.
    plain = AppDesignContext.from_draft_fields({"workflows": [INSPECTION_FLOW]})
    assert not any("no queue block" in error for error in plain.check({**page, "sections": [actions]})[1])


def test_a_waiting_list_reads_its_own_stages_not_every_project():
    """A "waiting for you" tab beside "all jobs" over the same source would list every job twice."""
    context = AppDesignContext.from_draft_fields({"workflows": [INSPECTION_FLOW]})
    page = _inspection_page(summary_fields=[{"field": ["address"], "label": "Address"}])
    progress = page["components"][0]
    progress["blocks"].insert(0, {"key": "waiting", "type": "progress", "source": "jobs", "title": "Waiting for you"})
    _, errors, _ = context.check(page)
    assert [message for message in errors if "list the same projects" in message], errors

    page["data_sources"]["visits_due"] = {**page["data_sources"]["jobs"], "stages": ["visiting"]}
    progress["blocks"][0]["source"] = "visits_due"
    assert context.check(page)[1] == []


async def test_feedback_revises_the_waiting_candidate_with_the_brief_it_answered(monkeypatch):
    """A send-back runs a new sub-agent; it gets the design and the original brief, not a blank page."""
    seen = {}

    async def loop(**kwargs):
        seen["message"] = kwargs["user_message"]
        return SimpleNamespace(content="Revised.", rounds=1)

    monkeypatch.setattr("packages.core.ai.runtime.harness.runtime_execute_agentic_loop", loop)
    page = copy.deepcopy(BUILTIN_APP_VIEWS["creative-presentation-v1"])
    draft = _presentation_draft(_candidates={"app": {"value": page, "brief": "Pitch, sales and board decks."}})
    tools = _patch_draft_io(monkeypatch, draft)

    await tools._design_app_view(None, entity_id="ent-1", draft_id="draft-1", brief="Decks.",
                                 feedback="Label the pitch entry Pitch deck.")
    message = seen["message"]
    assert "Label the pitch entry Pitch deck." in message
    assert "The brief your last design answered" in message and "Pitch, sales and board decks." in message
    assert json.dumps(page["presets"], ensure_ascii=False, separators=(",", ":")) in message

    # A fresh command (no feedback) designs from the brief alone.
    await tools._design_app_view(None, entity_id="ent-1", draft_id="draft-1", brief="Decks.")
    assert "Your last design" not in seen["message"]


@pytest.mark.parametrize("write,expected", [
    # The 2026-11-02 staging studio page listed its decision log with no fields: three bare ids.
    ({"history_event": {"kind": "review", "decision": "{{ok.choice}}", "note": "{{ok.note}}"}},
     ["kind", "decision", "note", "timestamp", "step_id", "run_id", "attempt_number"]),
    ({"list_appends": [{"path": "history", "items": [{"defect": "{{write.defect}}", "room": "{{write.room}}"}]}]},
     ["defect", "room"]),
], ids=["history_event", "list_appends"])
def test_a_project_list_shows_its_items_fields_never_bare_ids(write, expected):
    flow = copy.deepcopy(INSPECTION_FLOW)
    for step in flow["steps"]:
        step["config"]["state_schema"]["properties"]["history"] = {"type": "array"}
    flow["steps"][1]["config"].update(write)
    context = AppDesignContext.from_draft_fields({"workflows": [flow]})
    project = context.summary()["workflows"][0]["projects"][0]
    assert project["list_item_fields"] == {"history": expected} and "history" in project["lists"]

    page = _inspection_page(summary_fields=[{"field": ["address"], "label": "Address"}])
    page["data_sources"]["log"] = {"type": "project_records", "parent": "jobs", "record_path": ["history"]}
    page["components"][1]["blocks"].append({"key": "log", "type": "records", "source": "log"})
    _, errors, _ = context.check(page)
    assert len(errors) == 1 and f"the items carry {', '.join(expected)}" in errors[0], errors

    page["data_sources"]["log"]["fields"] = [[name] for name in expected[:2]]
    page["components"][1]["blocks"][-1]["fields"] = [{"field": [name], "label": name} for name in expected[:2]]
    assert context.check(page)[1] == []

    # Items with a title read on their own.
    page["components"][1]["blocks"][-1]["fields"] = []
    _list_title = {"title": "{{write.defect}}"}
    flow["steps"][1]["config"]["list_appends"] = [{"path": "history", "items": [_list_title]}]
    assert AppDesignContext.from_draft_fields({"workflows": [flow]}).check(page)[1] == []


_SPICE = {"type": "string", "enum": ["none", "mild", "medium", "hot", "extra_hot"]}
_DISH_SCHEMA = {"type": "object", "properties": {name: {"type": kind} for name, kind in (
    ("name", "string"), ("price", "number"), ("spice", "string"), ("status", "string"), ("added_on", "string"))}}
# Staging 2026-11-08 (01M6YX991C4RK6XS73RTEVFKEA): a menu card showed "extra_hot" and "medium" as stored.
ADD_DISH_FLOW = {"slug": "add_dish", "name": "Add dish", "run_inputs": [
    {"key": "name", "schema": {"type": "string"}}, {"key": "spice", "schema": _SPICE}], "steps": [
    {"id": "start", "type": "trigger", "config": {"outputs": [{"key": "spice", "value": "{{start.spice}}"}]}},
    {"id": "create", "type": "workflow_project", "config": {
        "operation": "create", "project_type": "dish", "current_stage": "active", "state_schema": _DISH_SCHEMA,
        "state": {"name": "{{name}}", "spice": "{{spice}}", "status": "active", "added_on": "{{start.added_on}}"}}}]}
EDIT_DISH_FLOW = {"slug": "edit_dish", "name": "Edit dish", "steps": [
    {"id": "changes", "type": "wait", "config": {"wait_type": "input", "response_variable": "changes", "input_schema": {
        "type": "object", "properties": {"spice": _SPICE, "status": {"type": "string", "enum": ["active", "retired"]}}}}},
    {"id": "save", "type": "workflow_project", "config": {
        "operation": "patch", "project_type": "dish", "state_schema": _DISH_SCHEMA,
        "patch": {"spice": "{{changes.spice}}", "status": "{{changes.status}}"}}}]}


def _menu_page(*fields, stage_labels=None):
    menu = {"key": "menu_list", "type": "progress", "source": "dishes", "fields": list(fields),
            **({"stage_labels": stage_labels} if stage_labels else {})}
    return {"hero": "workflow:add_dish", "hidden": ["workflow:edit_dish"], "data_sources": {"dishes": {
        "type": "workflow_projects", "project_type": "dish", "label_fields": [["name"]],
        "producer_workflows": ["workflow_definition:add_dish", "workflow_definition:edit_dish"],
        "fields": [["name"], ["price"], ["spice"], ["status"], ["added_on"]]}},
        "components": [{"key": "menu", "type": "data_view", "context": {"kind": "workspace"}, "blocks": [menu]}]}


def _dish_flow(slug, inputs, patch):
    return {"slug": slug, "name": slug.replace("_", " ").title(), "run_inputs": [
        {"key": key, "required": required, "schema": {"type": kind}} for key, kind, required in inputs], "steps": [
        {"id": "save", "type": "workflow_project", "config": {
            "operation": "patch", "project_type": "dish", "project_id": "{{dish_id}}", "state_schema": _DISH_SCHEMA,
            "patch": patch}}]}


def test_a_menu_card_edits_a_dish_with_its_values_and_asks_before_taking_it_off():
    """Restaurant marketing (staging 2026-11-08): the edit entry prefilled only the dish id, so every other
    field had to be typed again, and nothing asked before a dish came off the menu."""
    update = _dish_flow("update_dish", [("dish_id", "string", True), ("name", "string", False), ("price", "number", False)],
                        {"name": "{{name}}", "price": "{{price}}"})
    retire = _dish_flow("retire_dish", [("dish_id", "string", True), ("reason", "string", True)], {"status": "retired"})
    context = AppDesignContext.from_draft_fields({"workflows": [ADD_DISH_FLOW, update, retire]})
    page = _menu_page({"field": ["name"], "label": "Dish"}, {"field": ["price"], "label": "Price"})
    page["hidden"] = ["workflow:update_dish", "workflow:retire_dish"]
    page["data_sources"]["dishes"]["producer_workflows"] = [
        "workflow_definition:add_dish", "workflow_definition:update_dish", "workflow_definition:retire_dish"]
    menu = page["components"][0]["blocks"][0]
    menu["item_action"] = {"action": "workflow:update_dish", "input": "dish_id", "label": "Edit",
                           "prefill": {"name": ["name"], "price": ["price"]}}
    menu["item_actions"] = [{"action": "workflow:retire_dish", "input": "dish_id", "label": "Take off the menu",
                             "confirm": "Take this dish off the menu?"}]
    _, errors, _ = context.check(page)
    assert errors == ["menu.menu_list.item_actions asks first and then runs without its form, but nothing fills "
                      "workflow:retire_dish's required reason: prefill them from the record or drop confirm"]

    retire["run_inputs"][1]["required"] = False
    config, errors, _ = AppDesignContext.from_draft_fields({"workflows": [ADD_DISH_FLOW, update, retire]}).check(page)
    assert errors == []
    block = config["components"][0]["blocks"][0]
    assert block["item_action"]["prefill"] == {"name": ["name"], "price": ["price"]}
    assert block["item_actions"][0]["confirm"] == "Take this dish off the menu?"


def test_a_page_listing_records_people_keep_by_hand_must_let_them_add_and_change_one():
    """Staging 2026-11-09: the restaurant's Add and Edit Flows were hidden behind two chat entries,
    so the menu listed dishes nobody could add or change from the page."""
    from packages.core.blueprints.workflow_patterns import RecordField, record_collection_flows
    from packages.core.services.workspace_app_designer import record_collection_pattern

    flows = record_collection_flows(slug="menu", item_type="dish", item_word="dish", title_field="name", fields=[
        RecordField("name", "Dish", required=True), RecordField("price", "Price", "money", required=True)])["workflows"]
    context = AppDesignContext.from_draft_fields({"workflows": flows, "services": [{"key": "menu", "name": "Menu"}]})
    fragment = record_collection_pattern(context, {"add_workflow": "menu_add", "edit_workflow": "menu_edit",
                                                   "take_off_workflow": "menu_take_off", "item_word": "dish",
                                                   "currency": "CNY"})["fragment"]
    page = {"hero": "service:menu", "hidden": ["workflow:menu_add", *fragment["hidden"]], "theme": {"accent": "#b23a2a"},
            "icons": {"service:menu": "chat"}, "data_sources": fragment["data_sources"], "components": fragment["components"]}
    block = page["components"][0]["blocks"][0]
    block.pop("item_action")
    block.pop("item_actions")
    block.pop("add_action")
    config, errors, _ = context.check(page)
    assert errors == []
    no_add_button = (
        "the page lists dish records, which people add with workflow:menu_add and change by hand, but the list of "
        f'them has no Add button: give {page["components"][0]["key"]}.{block["key"]} '
        'add_action: {"action": "workflow:menu_add", '
        '"label": "Add a dish"} in the person\'s words (it may stay an entry too)')
    assert context.look(config)[0] == [
        no_add_button,
        "workflow:menu_edit changes one dish the person picks, but no card that lists them opens it: put it on "
        "those cards as item_action or in item_actions (confirm when it takes one off)",
        "workflow:menu_take_off changes one dish the person picks, but no card that lists them opens it: put it on "
        "those cards as item_action or in item_actions (confirm when it takes one off)"]
    # Staging 2026-11-09, three Architect runs: Add dish was the hero, and the menu itself had no Add button.
    args = {"add_workflow": "menu_add", "edit_workflow": "menu_edit", "take_off_workflow": "menu_take_off",
            "item_word": "dish", "currency": "CNY"}
    as_hero = {**page, "hero": "workflow:menu_add", "hidden": fragment["hidden"],
               "icons": {"service:menu": "chat", "workflow:menu_add": "cart"},
               "components": record_collection_pattern(context, args)["fragment"]["components"]}
    as_hero["components"][0]["blocks"][0].pop("add_action")
    assert context.look(context.check(as_hero)[0])[0] == [no_add_button]
    # The pattern's own page offers all three, the Add button on the list whether or not Add is also the hero.
    for hero in ("workflow:menu_add", "service:menu"):
        page = {**as_hero, "hero": hero, "components": record_collection_pattern(context, args)["fragment"]["components"]}
        config, errors, _ = context.check(page)
        assert errors == [] and context.look(config)[0] == []


def test_a_menu_kept_by_hand_gets_its_list_with_change_take_off_filters_and_search():
    """The App half of wf_pattern_record_collection: the restaurant's menu as a list people keep from the page."""
    from packages.core.blueprints.workflow_patterns import RecordField, record_collection_flows
    from packages.core.services.workspace_app_designer import record_collection_pattern

    flows = record_collection_flows(slug="menu", item_type="dish", item_word="dish", title_field="name", fields=[
        RecordField("name", "Dish", required=True), RecordField("price", "Price", "money", required=True),
        RecordField("spice", "Spice", choices=(("mild", "Mild"), ("hot", "Hot"))), RecordField("photo", "Photo", "picture")],
        live_words="On the menu", off_words="Off the menu")["workflows"]
    context = AppDesignContext.from_draft_fields({"workflows": flows})
    args = {"add_workflow": "menu_add", "edit_workflow": "menu_edit", "take_off_workflow": "menu_take_off",
            "item_word": "dish", "title": "Menu", "live_words": "On the menu", "off_words": "Off the menu"}
    assert record_collection_pattern(context, args)["errors"] == [
        "currency is required for the money fields: a 3-letter code such as CNY"]
    fragment = record_collection_pattern(context, {**args, "currency": "CNY"})["fragment"]
    block = fragment["components"][0]["blocks"][0]
    assert [(row["label"], row["format"]) for row in block["fields"]] == [("Price", "money"), ("Spice", "text"), ("Photo", "image")]
    assert block["fields"][1]["labels"] == {"mild": "Mild", "hot": "Hot"}
    # The Change Flow opens its own form holding the dish's values, so the card fills only the dish.
    assert block["item_action"] == {"action": "workflow:menu_edit", "input": "item_id", "label": "Change"}
    assert block["item_actions"][0]["confirm"] and block["item_actions"][0]["stages"] == ["active"]
    assert block["add_action"] == {"action": "workflow:menu_add", "label": "Add a dish"}
    assert block["filters"] == [{"field": ["spice"], "label": "Spice"}, {"by": "stage", "label": "Status"}]

    page = {"hero": fragment["suggested_hero"], "hidden": fragment["hidden"], "theme": {"accent": "#b23a2a"},
            "data_sources": fragment["data_sources"], "components": fragment["components"]}
    config, errors, warnings = context.check(page)
    assert (errors, warnings) == ([], [])
    assert context.look(config) == ([], [])


_CANDLE_SCHEMA = {"type": "object", "properties": {
    "name": {"type": "string"}, "scent": {"type": "string"},
    "stock_status": {"type": "string", "enum": ["in_stock", "sold_out", "discontinued"]}}}
# A candle shop's catalogue written by hand: no record-collection pattern, the status a field beside the stage.
ADD_CANDLE_FLOW = {"slug": "add_candle", "name": "Add a candle", "run_inputs": [
    {"key": "name", "required": True, "schema": {"type": "string"}}, {"key": "scent", "schema": {"type": "string"}}],
    "steps": [{"id": "save", "type": "workflow_project", "config": {
        "operation": "create", "project_type": "candle", "current_stage": "listed", "state_schema": _CANDLE_SCHEMA,
        "state": {"name": "{{name}}", "scent": "{{scent}}", "stock_status": "in_stock"}}}]}
UPDATE_CANDLE_FLOW = {"slug": "update_candle", "name": "Update a candle", "run_inputs": [
    {"key": "candle_id", "required": True, "schema": {"type": "string"}},
    {"key": "stock_status", "schema": _CANDLE_SCHEMA["properties"]["stock_status"]}],
    "steps": [{"id": "save", "type": "workflow_project", "config": {
        "operation": "patch", "project_type": "candle", "project_id": "{{candle_id}}", "state_schema": _CANDLE_SCHEMA,
        "patch": {"stock_status": "{{stock_status}}"}}}]}


def test_a_catalogue_people_keep_by_hand_is_added_to_from_its_list_and_shows_its_status():
    """Staging 2026-11-09, the same restaurant brief three times: the menu table had no Add button, and one run
    left out the dishes' status, so a dish taken off read like one on the menu. A candle shop's catalogue is
    held to the same, with no restaurant in sight."""
    context = AppDesignContext.from_draft_fields({"workflows": [ADD_CANDLE_FLOW, UPDATE_CANDLE_FLOW]})
    catalogue = {"key": "catalogue", "type": "progress", "source": "candles", "view": "table",
                 "fields": [{"field": ["scent"], "label": "Scent"}],
                 "item_action": {"action": "workflow:update_candle", "input": "candle_id", "label": "Update"}}
    page = {"hero": "workflow:add_candle", "hidden": ["workflow:update_candle"], "theme": {"accent": "#a0522d"},
            "data_sources": {"candles": {"type": "workflow_projects", "project_type": "candle", "label_fields": [["name"]],
                                         "producer_workflows": ["workflow_definition:add_candle",
                                                                "workflow_definition:update_candle"],
                                         "fields": [["name"], ["scent"], ["stock_status"]]}},
            "components": [{"key": "shop", "type": "data_view", "context": {"kind": "workspace"}, "blocks": [catalogue]}]}
    config, errors, _ = context.check(page)
    assert errors == []
    no_add, hidden_status = context.look(config)[0]
    assert no_add.startswith("the page lists candle records, which people add with workflow:add_candle and change by "
                             "hand, but the list of them has no Add button")
    assert hidden_status.startswith("shop.catalogue lists candle records whose stock_status people change with "
                                    "workflow:update_candle, but leaves it out, so one taken off reads like one in use")
    assert ('{"field": ["stock_status"], "label": "Status", "format": "status", "labels": {"in_stock": "In stock", '
            '"sold_out": "Sold out", "discontinued": "Discontinued"}}') in hidden_status

    # The Add button must open the Workflow that adds what the list holds.
    catalogue["add_action"] = {"action": "workflow:update_candle", "label": "Add a candle"}
    assert context.check(page)[1] == ["shop.catalogue.add_action opens workflow:update_candle, which adds no candle "
                                      "records: open the Workflow that creates the candle records the block lists"]
    catalogue["add_action"]["action"] = "workflow:add_candle"
    catalogue["fields"].append({"field": ["stock_status"], "label": "Stock", "format": "status", "labels": {
        "in_stock": "In stock", "sold_out": "Sold out", "discontinued": "No longer made"}})
    config, errors, warnings = context.check(page)
    assert (errors, warnings) == ([], [])
    assert context.look(config) == ([], [])
    # The entry stays a tile: the Add button only opens it from the list too.
    assert config["components"][0]["blocks"][0]["add_action"] == {"action": "workflow:add_candle", "label": "Add a candle"}

    # A second list of the same records still needs its own Add button.
    second_list = {key: value for key, value in catalogue.items() if key != "add_action"}
    source = page["data_sources"]["candles"]
    page["data_sources"]["listed_candles"] = {**source, "stages": ["listed"]}
    page["components"][0]["blocks"].append({
        **second_list, "key": "catalogue_overview", "source": "listed_candles",
    })
    config, errors, _ = context.check(page)
    assert errors == []
    assert context.look(config)[0] == [no_add.replace("shop.catalogue", "shop.catalogue_overview")]


def test_a_field_holding_a_fixed_choice_must_name_each_value():
    """A Flow's enum values are machine values whatever their shape: "medium" as much as "extra_hot"."""
    context = AppDesignContext.from_draft_fields({"workflows": [ADD_DISH_FLOW, EDIT_DISH_FLOW]})
    name = {"field": ["name"], "label": "Dish"}
    healthy = [name, {"field": ["price"], "label": "Price"}, {"field": ["added_on"], "label": "Added", "format": "date"}]
    spice = {"field": ["spice"], "label": "Spice"}

    config, errors, _ = context.check(_menu_page(*healthy, spice))
    assert len(errors) == 1, errors
    assert errors[0].startswith("menu.menu_list shows its Spice field (spice) as it is stored")
    assert "no label for none, mild, medium, hot, extra_hot" in errors[0] and '"extra_hot": "Extra hot"' in errors[0]
    # The App self-check fails the block that shows it.
    report = app_self_check_report(app_test_plan(config, context), errors=errors, warnings=[], reads=None)
    block = next(row for row in report["items"] if row["id"] == "block:menu.menu_list")
    assert report["status"] == "failed" and block["static_errors"] == errors
    # A block nobody sees is not held to it.
    hidden = _menu_page(*healthy, spice)
    hidden["components"][0]["display"] = {"hidden_blocks": ["menu_list"]}
    assert context.check(hidden)[1] == []

    labeled = {**spice, "labels": {"none": "Not spicy", "mild": "Mild", "medium": "Medium", "hot": "Hot"}}
    _, errors, _ = context.check(_menu_page(*healthy, labeled))
    assert len(errors) == 1 and "no label for extra_hot:" in errors[0], errors
    labeled["labels"]["extra_hot"] = "Extra hot"
    assert context.check(_menu_page(*healthy, labeled))[1] == []

    # A status is written as a fixed text by one Flow and picked from fixed values in another.
    status = {"field": ["status"], "label": "Status", "format": "status"}
    _, errors, _ = context.check(_menu_page(name, status))
    assert len(errors) == 1 and "no label for active, retired" in errors[0] and "stage_labels" in errors[0], errors
    stages = {"active": "On the menu", "retired": "Retired"}
    assert context.check(_menu_page(name, status, stage_labels=stages))[1] == []
    # Stage labels name only what a status field shows.
    assert context.check(_menu_page(name, {**status, "format": "text"}, stage_labels=stages))[1] != []


def test_a_result_field_holding_a_fixed_choice_must_name_each_value():
    context = AppDesignContext.from_draft_fields({"workflows": [ADD_DISH_FLOW]})
    page = {"hero": "workflow:add_dish", "data_sources": {"made": {
        "type": "workflow_results", "producer_workflows": ["workflow_definition:add_dish"],
        "fields": [["name"], ["spice"]], "media": [{"field": ["photo"]}]}},
        "components": [{"key": "dishes", "type": "data_view", "context": {"kind": "workspace"}, "blocks": [
            {"key": "tiles", "type": "gallery", "source": "made",
             "fields": [{"field": ["name"], "label": "Dish"}, {"field": ["spice"], "label": "Spice"}]}]}]}
    errors = context.coverage(normalize_app_view_config(page))[0]
    assert len(errors) == 1 and errors[0].startswith("dishes.tiles shows its Spice field (spice)"), errors


PROMOTE_FLOW = {"slug": "promote", "name": "Plan a promotion", "run_inputs": [
    {"key": "title", "schema": {"type": "string"}},
    {"key": "dish_ids", "schema": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                                   "x-ui": {"control": "workflow_project_select", "project_type": "dish"}}}], "steps": [
    {"id": "create", "type": "workflow_project", "config": {
        "operation": "create", "project_type": "promotion", "current_stage": "planned",
        "state_schema": {"type": "object", "properties": {"title": {"type": "string"}, "dish_ids": {"type": "array"}}},
        "state": {"title": "{{inputs.title}}", "dish_ids": "{{inputs.dish_ids}}"}}}]}


def test_a_field_holding_picked_records_must_show_them_by_name():
    """Staging 2026-11-08: a promotion's dishes, picked with the multi-record picker, read as ids."""
    context = AppDesignContext.from_draft_fields({"workflows": [ADD_DISH_FLOW, PROMOTE_FLOW]})
    dishes = {"field": ["dish_ids"], "label": "Dishes"}
    page = {"hero": "workflow:promote", "hidden": ["workflow:add_dish"], "data_sources": {"promotions": {
        "type": "workflow_projects", "project_type": "promotion", "label_fields": [["title"]],
        "producer_workflows": ["workflow_definition:promote"], "fields": [["title"], ["dish_ids"]]}},
        "components": [{"key": "promotions", "type": "data_view", "context": {"kind": "workspace"}, "blocks": [
            {"key": "list", "type": "progress", "source": "promotions", "fields": [dishes]}]}]}

    config, errors, _ = context.check(page)
    assert len(errors) == 1, errors
    assert errors[0].startswith("promotions.list shows dish_ids as text, but it holds the ids of the dish records")
    assert "format 'record' (project_type 'dish')" in errors[0]
    report = app_self_check_report(app_test_plan(config, context), errors=errors, warnings=[], reads=None)
    block = next(row for row in report["items"] if row["id"] == "block:promotions.list")
    assert report["status"] == "failed" and block["static_errors"] == errors

    dishes.update(format="record", project_type="poster")
    assert "set project_type 'dish'" in context.check(page)[1][0]
    dishes["project_type"] = "dish"
    assert context.check(page)[1] == []
    # The project_type is optional: its Workflow says which records the field names.
    del dishes["project_type"]
    assert context.check(page)[1] == []
    # Only a record field names a kind of record.
    dishes.update(format="text", project_type="dish")
    with pytest.raises(ValueError, match="project_type needs format record"):
        normalize_app_view_config(page)


def test_a_card_action_must_fill_inputs_its_flow_takes():
    context = AppDesignContext.from_draft_fields({"workflows": [SERIES_FLOW]})
    page = _series_page([project_tree_pattern(context, _LESSON_TREE)])["app_view"]
    assert context.check(page)[1:] == ([], [])
    packs = page["components"][0]["blocks"][2]
    packs["plan_action"]["inputs"] = {"series_id": "parent"}
    _, errors, _ = context.check(page)
    assert any("plan_action fills inputs" in message for message in errors), errors
    packs["plan_action"]["inputs"] = {"course_id": "parent", "lesson_code": "code"}
    page["hidden"] = []
    _, errors, warnings = context.check(page)
    assert errors == [] and any("add it to hidden" in message for message in warnings)


def _split_series(*, linked: bool) -> list[dict]:
    """The shape a real Architect wrote: plan, make and remake as three Flows."""
    plan, make = copy.deepcopy(SERIES_FLOW), copy.deepcopy(SERIES_FLOW)
    plan.update(slug="plan_course", steps=plan["steps"][:1], run_inputs=plan["run_inputs"][:1])
    plan["steps"][0]["next"] = []
    make.update(slug="make_lesson", steps=make["steps"][1:], run_inputs=make["run_inputs"][:3])
    if not linked:
        make["steps"][0]["config"]["state"].pop("parent_project_id")
        plan["steps"][0]["config"]["state"]["episodes"] = []
        make["steps"][0]["config"].pop("folder")
    remake = {"slug": "remake_lesson", "name": "Remake a lesson", "run_inputs": [SERIES_FLOW["run_inputs"][3]],
              "steps": [{"id": "get_lesson", "type": "workflow_project", "name": "Take", "next": [], "config": {
                  "operation": "get", "project_type": "lesson", "project_id": "{{lesson_id}}", "schema_version": 1,
                  "state_schema": _EPISODE_SCHEMA, "production": "take"}}]}
    return [plan, make, remake]


def test_parent_and_children_may_come_from_different_flows():
    context = AppDesignContext.from_draft_fields({"workflows": _split_series(linked=True)})
    assert workspace_app_designer.project_structure_issues(context.workflows) == []
    result = project_tree_pattern(context, {**_LESSON_TREE, "workflow": "make_lesson",
                                            "parent_workflow": "plan_course", "remake_workflow": "remake_lesson"})
    assert result["ok"] is True, result
    fragment = result["fragment"]
    assert fragment["data_sources"]["parents"]["producer_workflows"] == ["workflow_definition:plan_course"]
    assert fragment["data_sources"]["children"]["producer_workflows"] == ["workflow_definition:make_lesson"]
    assert fragment["targets"] == {"action:lesson-remake": "workflow:remake_lesson",
                                   "action:lesson-make": "workflow:make_lesson"}
    page = {"hero": "workflow:plan_course", **fragment}
    assert context.check(page)[1:] == ([], [])


async def test_a_series_without_parent_links_is_fixed_before_the_app_is_designed(monkeypatch):
    from packages.core.ai.tools import workspace_arch_tools

    workflows = _split_series(linked=False)
    issues = workspace_app_designer.project_structure_issues(workflows)
    assert [(issue["severity"], issue["where"]) for issue in issues] == [
        ("P0", "workflows.make_lesson"), ("P1", "workflows.make_lesson"), ("P1", "workflows.make_lesson")]
    assert "parent_project_id" in issues[0]["message"]
    draft = SimpleNamespace(fields={"workflows": workflows, "services": []})
    monkeypatch.setattr(workspace_arch_tools, "_load_draft", lambda *_args: _resolved(draft))

    async def never(*_args, **_kwargs):
        raise AssertionError("the sub-agent must not run on Workflows that cannot show a series")

    monkeypatch.setattr(workspace_app_designer, "run_app_designer", never)
    refused = json.loads(await workspace_arch_tools._design_app_view(
        None, entity_id="ent-1", draft_id="draft-1", brief="Plan a course, make each lesson."))
    assert refused["ok"] is False and refused["issues"][0]["severity"] == "P0"


def test_a_project_made_without_its_display_is_named_for_the_workflow_author():
    def flow(slug, operation, **config):
        return {"slug": slug, "steps": [{"id": "save", "type": "workflow_project", "config": {
            "operation": operation, "project_type": "weekly_content", **config}}]}

    bare = flow("weekly_posts", "create", state={"brief": "{{brief}}"})
    assert [issue.split(":")[0] for issue in workspace_app_designer.project_display_issues([bare])] == [
        "workflows.weekly_posts"]
    # The display may be saved by a later patch, in another Flow of the set.
    shown = flow("approve_posts", "patch", patch={"display": {"title": "{{prepare.title}}", "status": "Ready for review"}})
    assert workspace_app_designer.project_display_issues([bare, shown]) == []


async def _resolved(value):
    return value


async def test_the_designer_runs_as_its_bounded_subagent(monkeypatch):
    seen = {}

    async def loop(**kwargs):
        seen["tools"] = {schema["function"]["name"] for schema in kwargs["tools"]}
        seen["max_rounds"] = kwargs["max_rounds"]
        executor = kwargs["tool_executor"]
        context_reply = json.loads(await executor("app_read_context", {}))
        seen["catalog"] = context_reply["module_catalog"]["data_blocks"]
        bad = json.loads(await executor("app_submit_candidate", {"app_view": {"hero": "workflow:nope"}, "recipes": []}))
        seen["bad"] = bad
        page = copy.deepcopy(BUILTIN_APP_VIEWS["creative-presentation-v1"])
        await executor("app_submit_candidate", {"app_view": page, "recipes": ["one flow, several kinds"]})
        return SimpleNamespace(content="Deck kinds as entries.", rounds=3)

    monkeypatch.setattr("packages.core.ai.runtime.harness.runtime_execute_agentic_loop", loop)
    from packages.core.ai.runtime.surfaces import ChatSurface

    result = await workspace_app_designer.run_app_designer(
        AppDesignContext.from_draft_fields({"workflows": _recipe_workflows("creative-presentation-v1")}),
        brief="Make decks of different kinds.", feedback=None, entity_id="ent-1", user_id="user-1",
        workspace_id=None, surface=ChatSurface.WORKSPACE_DRAFT_ARCHITECT,
    )
    assert seen["tools"] == {"app_read_context", "app_pattern_project_tree", "app_pattern_content_calendar",
                             "app_pattern_record_collection", "app_submit_candidate"}
    assert seen["max_rounds"] == 500
    assert "packs" in seen["catalog"]
    assert seen["bad"]["ok"] is False
    assert result.attempts == 2 and result.recipes == ["one flow, several kinds"]
    assert result.app_view["presets"] and result.notes.startswith("Deck kinds as entries.")
    # A Draft's page has no Workspace to read yet, and its design note says so.
    assert "reads have not run" in result.notes and result.details["self_check"]["status"] == "passed"


async def test_architect_sub_agents_log_usage_apart_from_the_architect(monkeypatch):
    """App design and capability matching share the Architect's billing context
    (same payer) but each logs its calls under its own source."""
    from packages.core.ai import llm_client
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services import agent_generator
    from packages.core.services.agent_capability_catalog import (
        AgentCapabilityCandidate, AgentCapabilityCatalog, AgentCapabilityKind,
    )

    recorded, scheduled = [], []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    async def record(db, **kwargs):
        recorded.append((kwargs["source"], kwargs["payer_entity_id"]))

    monkeypatch.setattr("packages.core.database.async_session", lambda: Session())
    monkeypatch.setattr("packages.core.services.usage_service.record_llm_usage", record)
    monkeypatch.setattr(llm_client, "fire_and_forget", lambda factory, **_kwargs: scheduled.append(factory) or True)

    def provider_call():
        llm_client._schedule_billing_record(llm_client._billing_ctx_var.get(), {"total": 1}, 1)

    async def loop(**_kwargs):
        provider_call()
        return SimpleNamespace(content="", rounds=1)

    async def completion(_messages, **_kwargs):
        provider_call()
        return SimpleNamespace(content='{"capability_ids": []}')

    monkeypatch.setattr("packages.core.ai.runtime.harness.runtime_execute_agentic_loop", loop)
    monkeypatch.setattr(agent_generator, "runtime_execute_text_completion", completion)
    catalog = AgentCapabilityCatalog(candidates=(AgentCapabilityCandidate(
        catalog_id="tool:web_search", kind=AgentCapabilityKind.TOOL, ref="web_search", name="Web Search"),))

    async with llm_client.llm_billing_context("ent-1", source="workspace_architect", payer_entity_id="payer-1"):
        provider_call()
        await workspace_app_designer.run_app_designer(
            AppDesignContext.from_draft_fields({"workflows": _recipe_workflows("creative-presentation-v1")}),
            brief="Make decks.", feedback=None, entity_id="ent-1", user_id="user-1",
            workspace_id=None, surface=ChatSurface.WORKSPACE_DRAFT_ARCHITECT,
        )
        await agent_generator.match_agent_capabilities(
            prompt="Research public sources.", spec={"name": "Research Agent"},
            entity_id="ent-1", capability_catalog=catalog,
        )
        provider_call()
    for factory in scheduled:
        await factory()

    assert recorded == [
        ("workspace_architect", "payer-1"),
        ("workspace_architect.app_designer", "payer-1"),
        ("workspace_architect.capability_match", "payer-1"),
        ("workspace_architect", "payer-1"),
    ]


async def test_a_series_brief_yields_a_workspace_with_the_full_series_app(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Architect commands, the sub-agent designs, the Architect accepts; then chat redesigns it."""
    from packages.core.ai.tools.workspace_arch_tools import (
        _accept_app_view,
        _design_app_view,
        _lint_draft,
        _propose_workflow,
    )
    from packages.core.services.workspace_draft_service import finalize_draft
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    from packages.core.models.user import User, UserMembership

    entity_id, user_id = generate_ulid(), generate_ulid()
    db_session.add_all([
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.test", password_hash="x", role="owner",
             status="active"),
        UserMembership(user_id=user_id, entity_id=entity_id, role="owner", status="active"),
    ])
    draft = WorkspaceDraft(id=generate_ulid(), entity_id=entity_id, user_id=user_id, messages=[], missing=[],
                           ready=False, status="active", fields={
                               **dict(DEFAULT_FIELDS), "name": "Course studio", "kind": "content",
                               "operating_context": "Video courses.", "primary_work": "Make video courses.",
                               "services": [], "agent_mappings": [], "goals": [], "stats": []})
    db_session.add(draft)
    await db_session.flush()
    saved = await _propose_workflow(db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id,
                                    **copy.deepcopy(SERIES_FLOW))
    assert json.loads(saved)["ok"] is True, saved

    monkeypatch.setattr(workspace_app_designer, "run_app_designer", _scripted([
        ("app_read_context", {}),
        ("app_pattern_project_tree", _LESSON_TREE),
        ("app_submit_candidate", _series_page),
    ]))
    designed = json.loads(await _design_app_view(db_session, entity_id=entity_id, user_id=user_id,
                                                 draft_id=draft.id, brief="Plan a course, then make each lesson."))
    assert designed["ok"] is True, designed
    assert {entry["key"]: entry["hidden"] for entry in designed["candidate"]["entries"]} == {
        "action:lesson-remake": True, "action:lesson-make": True}
    lint = json.loads(await _lint_draft(db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id))
    assert any(issue["where"] == "_candidates.app" for issue in lint["issues"])
    accepted = json.loads(await _accept_app_view(db_session, entity_id=entity_id, user_id=user_id, draft_id=draft.id))
    assert accepted["ok"] is True, accepted

    async def ready(*_args, **_kwargs): return True
    monkeypatch.setattr("packages.core.services.workspace_draft_service._refresh_missing_from_lint", ready)
    draft.ready, draft.status = True, "ready"
    workspace_id, _ = await finalize_draft(db_session, draft_id=draft.id, entity_id=entity_id, user_id=user_id)
    workspace = await db_session.get(Workspace, workspace_id)
    app = workspace.operating_model["app_view"]
    packs = next(block for block in app["components"][0]["blocks"] if block["type"] == "packs")
    assert packs["plan"] == "plan" and app["data_sources"]["plan"]["record_path"] == ["episodes"]
    assert packs["item_action"]["action"] == "action:lesson-remake"
    assert packs["plan_action"] == {"action": "action:lesson-make", "label": "Make this lesson",
                                    "inputs": {"course_id": "parent", "lesson_code": "code"}}
    assert set(app["hidden"]) == {"action:lesson-make", "action:lesson-remake"}
    binding = app["hero"].removeprefix("workflow:")
    assert app["targets"] == {"action:lesson-make": f"workflow:{binding}", "action:lesson-remake": f"workflow:{binding}"}
    assert app["asks"] == {"action:lesson-remake": ["lesson_id"], "action:lesson-make": ["course_id", "lesson_code"]}
    await db_session.commit()

    # Workspace chat: the same sub-agent redesigns the live page as a reviewed draft.
    def without_remake(replies):
        page = _series_page(replies)
        view = page["app_view"]
        view["title"] = "Make a course"
        for key in ("entries", "targets", "asks"):
            view[key].pop("action:lesson-remake")
        view["hidden"].remove("action:lesson-remake")
        view["components"][0]["blocks"][2].pop("item_action")
        return page

    monkeypatch.setattr(workspace_app_designer, "run_app_designer", _scripted([
        ("app_read_context", {}),
        ("app_pattern_project_tree", _LESSON_TREE),
        ("app_submit_candidate", without_remake),
    ]))
    staged = await workspace_app_designer.design_workspace_app_view(
        db_session, workspace_id=workspace_id, entity_id=entity_id, user_id=user_id,
        brief="Drop Remake; lessons are made once.")
    assert staged["ok"] is True, staged
    assert not (staged["draft"].get("validation") or {}).get("errors"), staged["draft"]["validation"]
    update = staged["draft"]["patches"][0]["payload"]["app_view"]
    assert update["title"] == "Make a course"
    assert update["entries"]["action:lesson-remake"] is None
    assert update["targets"]["action:lesson-make"] == f"workflow:{binding}"


def _presentation_draft(**candidate_fields):
    fields = {"workflows": _recipe_workflows("creative-presentation-v1"), **candidate_fields}
    return SimpleNamespace(fields=fields)


def _patch_draft_io(monkeypatch, draft):
    from packages.core.ai.tools import workspace_arch_tools

    async def persist(_db, _draft):
        return None

    monkeypatch.setattr(workspace_arch_tools, "_load_draft", lambda *_args: _resolved(draft))
    monkeypatch.setattr(workspace_arch_tools, "_persist", persist)
    return workspace_arch_tools


async def test_a_draft_saved_before_the_registry_keeps_its_app_candidate(monkeypatch):
    """A candidate in the old ``app_view_candidate`` field is still waiting and
    acceptable; accepting several parts stops at the first that fails and
    keeps the ones accepted before it."""
    from packages.core.services.workspace_part_designers import pending_candidate_issues

    page = copy.deepcopy(BUILTIN_APP_VIEWS["creative-presentation-v1"])
    draft = _presentation_draft(app_view_candidate={"app_view": page, "recipes": ["one flow, several kinds"]})
    assert [issue["where"] for issue in pending_candidate_issues(draft.fields)] == ["_candidates.app"]
    tools = _patch_draft_io(monkeypatch, draft)

    reply = json.loads(await tools._accept_parts(None, entity_id="ent-1", draft_id="draft-1",
                                                 parts=["app", "workflows", "app"]))
    assert reply["ok"] is False and reply["failed"]["part"] == "workflows"
    assert list(reply["accepted"]) == ["app"] and reply["not_tried"] == []
    assert draft.fields["app_view"]["hero"] == page["hero"]
    assert "app_view_candidate" not in draft.fields and "_candidates" not in draft.fields
    assert pending_candidate_issues(draft.fields) == []


async def test_a_candidate_is_checked_again_against_the_latest_draft_when_accepted(monkeypatch):
    page = copy.deepcopy(BUILTIN_APP_VIEWS["creative-presentation-v1"])
    draft = _presentation_draft(_candidates={"app": {"value": page, "recipes": []}})
    tools = _patch_draft_io(monkeypatch, draft)
    # The Workflow the page runs was removed after the page was designed.
    draft.fields["workflows"] = []

    reply = json.loads(await tools._accept_parts(None, entity_id="ent-1", draft_id="draft-1", parts=["app"]))
    assert reply["ok"] is False and reply["accepted"] == {}
    assert "Draft changed" in reply["failed"]["error"] and reply["failed"]["errors"]
    assert "app_view" not in draft.fields and draft.fields["_candidates"]["app"]["value"] == page


def test_every_part_designer_is_commanded_without_the_draft_lock_on_both_surfaces():
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.subagents import subagent_specs_for_surface
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.workspace_drafts import draft_command_tools
    from packages.core.ai.tools.workspace_arch_tools import HANDLERS
    from packages.core.services.workspace_part_designers import part_designers

    architect = {spec.name: spec for spec in subagent_specs_for_surface(
        surface=ChatSurface.WORKSPACE_DRAFT_ARCHITECT, profile=RuntimeProfile.WORKSPACE_ARCHITECT)}
    chat = {spec.name: spec for spec in subagent_specs_for_surface(
        surface=ChatSurface.WORKSPACE_CHAT, profile=RuntimeProfile.WORKSPACE_OPERATOR, workspace_id="ws-1")}
    for designer in part_designers().values():
        name = designer.subagent.name
        assert set(architect[name].allowed_tools) == {schema["function"]["name"] for schema in designer.tool_schemas}
        assert (architect[name].max_steps, architect[name].profile) == (500, RuntimeProfile.WORKSPACE_ARCHITECT)
        assert (chat[name].max_steps, chat[name].profile) == (200, RuntimeProfile.WORKSPACE_OPERATOR)
        assert designer.command_tool in HANDLERS and designer.command_tool in draft_command_tools()


async def test_a_design_from_chat_is_shown_as_a_review_card_and_a_revision_replaces_it(client, monkeypatch):
    import packages.core.database as db_module
    from packages.core.ai.runtime.workspace_operation_actions import runtime_workspace_operation_action
    from packages.core.models.workspace import WorkspaceOperationDraft

    owner = (await client.post("/api/v1/auth/register", json={
        "username": "app_card_owner", "email": "app_card_owner@test.com",
        "password": "pass123", "entity_name": "Card Studio",
    })).json()
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Inspections"})).json()
    titles = iter(["Visits due today", "Reports to sign"])

    async def designer(context, **_kwargs):
        state: dict = {}
        await workspace_app_designer.app_designer_executor(context, state)(
            "app_submit_candidate", {"app_view": {"title": next(titles)}, "recipes": []})
        return AppDesignResult(app_view=state["candidate"], recipes=[], warnings=[], notes="", attempts=1)

    monkeypatch.setattr(workspace_app_designer, "run_app_designer", designer)

    async def design(**params):
        return json.loads(await runtime_workspace_operation_action(
            entity_id=me["entity_id"], workspace_id=workspace["id"], user_id=me["id"], conversation_id="conv-1",
            params={"action": "design_app", **params}))

    first = await design(brief="Redesign this page", audience="The inspector: visits due today first")
    assert first["__hitl__"] is True and first["hitl"]["prompt"] == "Apply these page changes?"
    assert first["hitl"]["operation"]["draft_id"] == first["draft_id"] and first["candidate"]["title"] == "Visits due today"
    second = await design(brief="Redesign this page", feedback="Reports first", draft_id=first["draft_id"])
    assert second["__hitl__"] is True and second["candidate"]["title"] == "Reports to sign"
    async with db_module.async_session() as session:
        assert (await session.get(WorkspaceOperationDraft, first["draft_id"])).status == "discarded"
        assert (await session.get(WorkspaceOperationDraft, second["draft_id"])).status == "open"


def _staged_on_live_page(current: dict, candidate: dict) -> dict:
    """The live page after the operation a design stages for ``candidate`` is applied."""
    from packages.core.services.workspace_operation_service import _apply_single_patch

    update = workspace_app_designer.app_view_replacement(current, normalize_app_view_config(candidate))
    state = _apply_single_patch({"operating_model": {"app_view": normalize_app_view_config(current)}},
                                {"op": "app_view.update", "payload": {"app_view": update}})
    return state["operating_model"]["app_view"]


def test_a_candidates_look_reaches_the_live_page():
    look = {"theme": {"accent": "#112233", "surface": "deep"},
            "cover_image_url": "/blueprints/covers/studio.webp",
            "motion_cover_url": "/blueprints/motion/wave.mp4",
            "motion_cover_source_url": "/blueprints/covers/studio.webp"}
    page = _staged_on_live_page({"title": "Old"}, {"title": "New", "visual": "motion", **look})
    assert {key: page[key] for key in look} == look


def test_a_candidate_replaces_the_theme_but_never_clears_a_picture_it_does_not_name():
    # An uploaded cover is not portable, so the designer never sees it and cannot keep it.
    current = {"title": "Old", "theme": {"surface": "deep"}, "cover_image_url": "/api/v1/files/upload.png",
               "motion_cover_url": "/api/v1/files/loop.mp4", "motion_cover_source_url": "/api/v1/files/upload.png"}
    page = _staged_on_live_page(current, {"title": "New"})
    assert "theme" not in page
    assert page["cover_image_url"] == "/api/v1/files/upload.png"
    assert page["motion_cover_url"] == "/api/v1/files/loop.mp4"
    assert page["motion_cover_source_url"] == "/api/v1/files/upload.png"


def test_a_new_app_field_is_not_silently_dropped_from_a_live_design():
    candidate = normalize_app_view_config({"theme": {"surface": "deep"}, "cover_image_url": "/blueprints/covers/a.webp",
                                           "motion_cover_url": "/blueprints/motion/a.mp4",
                                           "motion_cover_source_url": "/blueprints/covers/a.webp"})
    update = workspace_app_designer.app_view_replacement(None, candidate)
    # Kept as they are on purpose: translations and how the Workspace opens are not the page's design.
    assert set(APP_VIEW_FIELDS) - set(update) == {"version", "choices", "open_as", "i18n"}


async def _design_on_page(client, monkeypatch, *, username: str, page: dict, candidate):
    """A live Workspace showing ``page``, a helper to design it, and what the designer was given."""
    import packages.core.database as db_module
    from packages.core.ai.runtime.workspace_operation_actions import runtime_workspace_operation_action

    owner = (await client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@test.com", "password": "pass123", "entity_name": username,
    })).json()
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Inspections"})).json()
    async with db_module.async_session() as session:
        row = await session.get(Workspace, workspace["id"])
        row.operating_model = {**(row.operating_model or {}), "app_view": page}
        await session.commit()
    seen: list[dict] = []

    async def designer(context, **kwargs):
        seen.append({"context": context, **kwargs})
        state: dict = {}
        reply = json.loads(await workspace_app_designer.app_designer_executor(context, state)(
            "app_submit_candidate", {"app_view": candidate(context), "recipes": []}))
        assert reply["ok"], reply
        return AppDesignResult(app_view=state["candidate"], recipes=[], warnings=[], notes="", attempts=1)

    monkeypatch.setattr(workspace_app_designer, "run_app_designer", designer)

    async def design(**params):
        return json.loads(await runtime_workspace_operation_action(
            entity_id=me["entity_id"], workspace_id=workspace["id"], user_id=me["id"], conversation_id="conv-1",
            params={"action": "design_app", "brief": "Redesign this page", **params}))

    async def staged(draft_id: str) -> dict:
        from packages.core.models.workspace import WorkspaceOperationDraft

        async with db_module.async_session() as session:
            draft = await session.get(WorkspaceOperationDraft, draft_id)
            return draft.current_state["operating_model"]["app_view"]

    return design, staged, seen


async def test_a_page_with_an_uploaded_cover_is_revised_not_designed_from_scratch(client, monkeypatch):
    upload = "/api/v1/files/upload.png"
    page = {"title": "Visits due today", "description": "Keep me", "theme": {"surface": "deep"},
            "cover_image_url": upload, "motion_cover_url": "/api/v1/files/loop.mp4", "motion_cover_source_url": upload}
    design, staged, seen = await _design_on_page(
        client, monkeypatch, username="app_upload_owner", page=page,
        candidate=lambda context: {**context.app_view, "title": "Reports to sign"})

    first = await design()
    context = seen[0]["context"]
    assert context.app_view["description"] == "Keep me" and context.app_view["theme"] == {"surface": "deep"}
    assert not context.app_view.get("cover_image_url") and not context.app_view.get("motion_cover_url")
    # It is told the pictures are there, so it does not design as if the page had none.
    assert context.summary()["uploaded_pictures"] == ["cover_image_url", "motion_cover_url"]
    result = await staged(first["draft_id"])
    assert (result["title"], result["description"]) == ("Reports to sign", "Keep me")
    assert result["theme"] == {"surface": "deep"} and result["cover_image_url"] == upload
    assert result["motion_cover_url"] == "/api/v1/files/loop.mp4"

    # Feedback on that draft carries the design it is about, though the draft holds the upload too.
    await design(feedback="Reports first", draft_id=first["draft_id"])
    assert seen[1]["sent_back"]["value"]["title"] == "Reports to sign"


async def test_a_page_redesigned_from_scratch_keeps_a_theme_the_designer_never_saw(client, monkeypatch):
    page = {"title": "Old", "theme": {"surface": "deep"}, "order": ["workflow:removed-long-ago"]}
    design, staged, seen = await _design_on_page(
        client, monkeypatch, username="app_stale_owner", page=page, candidate=lambda _context: {"title": "New"})

    first = await design()
    assert seen[0]["context"].app_view is None
    result = await staged(first["draft_id"])
    assert result["title"] == "New" and result["theme"] == {"surface": "deep"}


def test_the_designer_reads_which_declared_fields_projects_actually_fill():
    from packages.core.services.workspace_app_designer import project_sample

    live = project_sample(
        [("course", "active", 2), ("course", "archived", 1)],
        [("course", {"title": "Cooking", "episodes": [{"code": "e1"}, {"code": "e2"}], "premise": ""}),
         ("course", {"title": "Baking", "episodes": []})],
    )
    assert live["course"] == {"count": 3, "stages": {"active": 2, "archived": 1}, "sampled": 2,
                              "filled": {"title": 2, "episodes": 1},
                              "lists": {"episodes": {"with_items": 1, "most_items": 2}}}
    context = AppDesignContext(workflows=[copy.deepcopy(SERIES_FLOW)], live_projects=live,
                               purpose={"primary_work": "Make video courses."})
    seen = context.summary()
    assert seen["purpose"] == {"primary_work": "Make video courses."}
    # A block for these would stay empty on every course the Workspace has.
    assert seen["live_projects"]["course"]["never_filled"] == ["premise", "project_root", "references"]


async def test_a_page_is_designed_for_the_person_named_and_previewed_before_approval(client, monkeypatch):
    """The audience a guided start proposes from, the designer's brief, and the draft's page with real data."""
    import packages.core.database as db_module
    from packages.core.ai.runtime.workspace_operation_actions import runtime_workspace_operation_action
    from packages.core.models.participant import ParticipantProfile
    from packages.core.models.task import Conversation, Message
    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowProject, WorkflowRun
    from packages.core.models.workspace import WorkspaceStaff
    from packages.core.services.auth_service import create_access_token, hash_password

    owner = (await client.post("/api/v1/auth/register", json={
        "username": "app_audience_owner", "email": "app_audience_owner@test.com",
        "password": "pass123", "entity_name": "Article Studio",
    })).json()
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    entity_id = me["entity_id"]
    workspace = (await client.post("/api/v1/workspaces", headers=headers, json={
        "name": "Client articles", "primary_work": "Write client articles; the account owner approves each one.",
    })).json()
    async with db_module.async_session() as session:
        editor = User(entity_id=entity_id, email="app_audience_editor@test.com", display_name="Erin Editor",
                      password_hash=hash_password("pass123"), role="member", status="active")
        session.add(editor)
        await session.flush()
        conversation = Conversation(entity_id=entity_id, workspace_id=workspace["id"], title="Workspace",
                                    channel="workspace", scope="workspace_main")
        session.add(conversation)
        await session.flush()
        session.add_all([
            WorkspaceStaff(workspace_id=workspace["id"], user_id=editor.id, role="viewer", status="active"),
            ParticipantProfile(entity_id=entity_id, user_id=editor.id, workspace_id=workspace["id"],
                               roles=["editor"], authority={"approve_tasks": False}),
            WorkflowProject(entity_id=entity_id, workspace_id=workspace["id"], project_type="client_article",
                            current_stage="awaiting_approval", created_by=me["id"],
                            state={"client": "Acme", "topic": "Pricing", "deliverable_document_id": "doc-1"}),
            WorkflowProject(entity_id=entity_id, workspace_id=workspace["id"], project_type="client_article",
                            current_stage="drafting", created_by=me["id"],
                            state={"client": "Beta", "topic": "", "notes": ["tighten intro", "cite source"]}),
            WorkflowRun(workflow_id="flow-1", entity_id=entity_id, workspace_id=workspace["id"], status="completed",
                        trigger_data={"_workspace_app_action": "action:new-article"}),
            Message(conversation_id=conversation.id, role="user", content="Which articles still need my approval?",
                    meta={"author_user_id": me["id"]}),
            Message(conversation_id=conversation.id, role="user", content="Start an article for Acme",
                    meta={"workspace_app_action": "action:new-article"}),
            Message(conversation_id=conversation.id, role="assistant", author_kind="agent",
                    content="The final Word file for Acme is ready for your approval.",
                    pending_action={"kind": "workflow_approval", "title": "Approve the final Word file: Acme"}),
        ])
        await session.commit()
        editor_headers = {"Authorization": f"Bearer {create_access_token(editor.id, entity_id, 'member')}"}

    audience = await client.get(f"/api/v1/workspaces/{workspace['id']}/app-view/design/audience", headers=headers)
    assert audience.status_code == 200, audience.text
    body = audience.json()
    assert body["purpose"]["primary_work"].startswith("Write client articles")
    people = {person["name"]: person for person in body["people"]}
    assert people["Erin Editor"] == {"name": "Erin Editor", "workspace_role": "viewer", "roles": ["editor"],
                                     "approves": False}
    assert [person["approves"] for name, person in people.items() if name != "Erin Editor"] == [True]
    assert body["waiting"] == {"workflow_approval": 1}
    assert body["live_projects"]["client_article"]["stages"] == {"awaiting_approval": 1, "drafting": 1}
    assert body["live_projects"]["client_article"]["filled"] == {
        "client": 2, "topic": 1, "deliverable_document_id": 1, "notes": 1}
    # Entries started from the App, as a Flow run and as a chat request; asks typed in chat, not from the App.
    assert body["usage"]["entry_starts"] == {"action:new-article": 2}
    assert [ask["text"] for ask in body["usage"]["recent_chat_asks"]] == ["Which articles still need my approval?"]
    assert "who" in body["usage"]["recent_chat_asks"][0]
    assert (await client.get(f"/api/v1/workspaces/{workspace['id']}/app-view/design/audience",
                             headers=editor_headers)).status_code == 403
    for actor_id in (editor.id, None):
        denied = json.loads(await runtime_workspace_operation_action(
            entity_id=entity_id, workspace_id=workspace["id"], user_id=actor_id,
            params={"action": "app_audience"},
        ))
        assert denied["error"] == "blocked_by_permission" and "people" not in denied
    allowed = json.loads(await runtime_workspace_operation_action(
        entity_id=entity_id, workspace_id=workspace["id"], user_id=me["id"],
        params={"action": "app_audience"},
    ))
    assert allowed["people"] == body["people"]

    briefs = []

    async def designer(context, *, brief, **_kwargs):
        briefs.append(brief)
        state: dict = {}
        tools = workspace_app_designer.app_designer_executor(context, state)
        assert json.loads(await tools("app_read_context", {}))["people"] == body["people"]
        await tools("app_submit_candidate", {"app_view": {
            "title": "Approve client articles", "sections": [
                {"id": "queue", "block": "queue"}, {"id": "actions", "block": "actions"}]}, "recipes": ["review gate"]})
        return AppDesignResult(app_view=state.get("candidate"), recipes=state.get("recipes") or [],
                               warnings=state.get("warnings") or [], notes="For the account owner.", attempts=1)

    monkeypatch.setattr(workspace_app_designer, "run_app_designer", designer)
    designed = await client.post(f"/api/v1/workspaces/{workspace['id']}/app-view/design", headers=headers, json={
        "brief": "Redesign this page", "audience": "The account owner: pending approvals and finished files first"})
    assert designed.status_code == 200, designed.text
    assert briefs[0].endswith("what they act on first: The account owner: pending approvals and finished files first")
    draft_id = designed.json()["draft"]["id"]

    app_view = f"/api/v1/workspaces/{workspace['id']}/app-view"
    saved = (await client.get(app_view, headers=headers)).json()
    preview = await client.get(app_view, headers=headers, params={"draft_id": draft_id})
    assert preview.status_code == 200, preview.text
    assert preview.json()["title"] == "Approve client articles" != saved["title"]
    assert saved["can_start"] is True and preview.json()["can_start"] is False
    assert [section["block"] for section in preview.json()["sections"]] == ["queue", "actions"]
    queue = await client.get(f"{app_view}/sections/queue", headers=headers, params={"draft_id": draft_id})
    assert queue.status_code == 200, queue.text
    # Only the people who may approve the change see the page it stages.
    assert (await client.get(app_view, headers=editor_headers, params={"draft_id": draft_id})).status_code == 403
    discarded = await client.post(f"/api/v1/workspaces/{workspace['id']}/operation/drafts/{draft_id}/discard",
                                  headers=headers)
    assert discarded.status_code == 200, discarded.text
    assert (await client.get(app_view, headers=headers, params={"draft_id": draft_id})).status_code == 404
    assert (await client.get(app_view, headers=headers)).json()["title"] == saved["title"]


@pytest.mark.parametrize(("labels", "named"), [
    ([["report_period"]], False),
    ([["thread_id"]], False),
    ([["status"], ["thread_id"]], False),
    ([["cohort"]], False),
    ([["title"]], True),
    ([["report_period"], ["title"]], True),
])
def test_records_must_be_named_by_something_that_tells_them_apart(labels, named):
    """Local eval 2026-11-03: every growth report read "daily", a customer case its thread id."""
    flow = copy.deepcopy(SERIES_FLOW)
    flow["run_inputs"] = [{"key": "cohort", "schema": {"type": "string", "enum": ["a", "b"]}}]
    context = AppDesignContext(workflows=[flow])
    config = {"data_sources": {"courses": {"type": "workflow_projects", "project_type": "course", "label_fields": labels}},
              "components": []}
    errors, _warnings = context.coverage(config)
    assert any("cannot tell them apart" in error for error in errors) is not named


@pytest.mark.parametrize("field", ["thread_reference", "thread_ref"])
def test_a_page_is_not_refused_for_a_name_the_workflow_does_not_save(field):
    """Refusing it would loop until the design gave up, and the Workspace lost its App."""
    flow = copy.deepcopy(SERIES_FLOW)
    context = AppDesignContext(workflows=[flow])
    project = {"project_type": "course", "fields": {field: "string", "stage": "string", "history": "array"},
               "document_fields": [], "list_item_fields": {}, "has_folder": False, "waits_in": []}
    context.projects = lambda: {"course": project}
    config = {"data_sources": {"courses": {"type": "workflow_projects", "project_type": "course",
                                           "label_fields": [[field]]}},
              "components": []}
    errors, warnings = context.coverage(config)
    assert not any("cannot tell them apart" in error for error in errors)
    assert any("save a title" in warning for warning in warnings)

    project["fields"]["customer_name"] = "string"
    errors, _warnings = context.coverage(config)
    assert any("use customer_name as label_fields" in error for error in errors)

