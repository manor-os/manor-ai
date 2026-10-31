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
from packages.core.services.workspace_app_designer import AppDesignContext, AppDesignResult, project_tree_pattern

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
         "config": {"operation": "create", "project_type": "course", "project_key": "course", "schema_version": 1,
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
    assert seen["tools"] == {"app_read_context", "app_pattern_project_tree", "app_submit_candidate"}
    assert seen["max_rounds"] == 200
    assert "packs" in seen["catalog"]
    assert seen["bad"]["ok"] is False
    assert result.attempts == 2 and result.recipes == ["one flow, several kinds"]
    assert result.app_view["presets"] and result.notes == "Deck kinds as entries."


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
    assert any(issue["where"] == "app_view_candidate" for issue in lint["issues"])
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
