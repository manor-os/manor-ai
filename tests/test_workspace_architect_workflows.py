"""Workflows the Workspace architect writes run as written, and the process they describe is real."""
from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.tools import workspace_arch_tools
from packages.core.constants.workspace_drafts import STAFF_REQUEST_TEXT_FIELD
from packages.core.services import workspace_draft_service
from packages.core.services.workflow_service import validate_workflow_steps


pytestmark = pytest.mark.unit

ROLES = ("research", "writing", "editing")


def _services():
    return [
        {"service_key": "research", "name": "Research", "handoff_to": ["writing"]},
        {"service_key": "writing", "name": "Writing", "handoff_to": ["editing"]},
        {"service_key": "editing", "name": "Editing", "human_review": True},
    ]


def _article_steps(*, approval=True):
    steps = [
        {"id": "research", "type": "agent", "next": ["write"], "config": {
            "service_key": "research", "prompt": "Research {{topic}} for {{audience}}.", "expects": ["files"]}},
        {"id": "write", "type": "agent", "next": ["edit"], "config": {
            "service_key": "writing", "prompt": "Draft from {{research}}.", "expects": ["files"]}},
        {"id": "edit", "type": "agent", "next": ["review"] if approval else [], "config": {
            "service_key": "editing", "prompt": "Edit {{write}}.", "expects": ["files"]}},
    ]
    if approval:
        steps.append({"id": "review", "type": "wait", "next": [], "config": {
            "wait_type": "approval", "message": "Approve the final article?", "options": ["approve", "revise"],
            "approval_values": ["approve"], "revise_from": "edit"}})
    return steps


def _run_inputs():
    return [{"key": "topic", "label": "Topic", "type": "string", "required": True},
            {"key": "audience", "label": "Audience", "type": "string"}]


@pytest.fixture
def draft(monkeypatch):
    draft = SimpleNamespace(fields={"services": _services()})
    monkeypatch.setattr(workspace_arch_tools, "_load_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(workspace_arch_tools, "_persist", AsyncMock())
    return draft


async def _propose(steps, **extra):
    return json.loads(await workspace_arch_tools._propose_workflow(
        None, entity_id="entity-1", draft_id="draft-1", slug="article_production", name="Article production",
        steps=steps, **extra,
    ))


# ── Shared validator ──────────────────────────────────────────────────────

def _graph(config, *, step_type="agent", extra=()):
    return [
        {"id": "start", "type": "trigger", "config": {"run_inputs": [{"key": "topic"}]}, "next": ["produce"]},
        {"id": "produce", "type": step_type, "config": config, "next": []},
        *extra,
    ]


@pytest.mark.parametrize("reference", ["{{ inputs.topic }}", "{{steps.research.summary}}", "{{vars.topic}}"])
def test_a_reference_under_an_undeclared_scope_is_rejected(reference):
    result = validate_workflow_steps(_graph({"prompt": f"Write about {reference}"}))
    assert not result["valid"]
    error = next(error for error in result["errors"] if error["code"] == "unresolvable_reference")
    assert reference.strip("{} ") in error["message"]


@pytest.mark.parametrize("graph,variables", [
    # Run inputs and step results by name are the runner's contract.
    (_graph({"prompt": "Write about {{topic}} from {{start.topic}}"}), ()),
    # An input wait names its answers ``inputs`` unless told otherwise.
    (_graph({"prompt": "{{inputs.angle}}"}, extra=[
        {"id": "ask", "type": "wait", "config": {"wait_type": "input", "input_schema": {
            "type": "object", "properties": {"angle": {"type": "string"}}}}, "next": ["produce"]},
    ]), ()),
    # A definition variable may be called anything.
    (_graph({"prompt": "{{params.tone}}"}), ("params",)),
])
def test_references_the_graph_declares_are_accepted(graph, variables):
    result = validate_workflow_steps(graph, variable_names=variables)
    assert not [error for error in result["errors"] if error["code"] == "unresolvable_reference"], result["errors"]


_SUMMARY_SCHEMA = {"type": "object", "properties": {"headline": {"type": "string"}}, "required": ["headline"]}


def _reads_agent_field(producer_config, reference, *, producer_type="agent", extra=()):
    return [
        {"id": "start", "type": "trigger", "config": {}, "next": ["edit"]},
        {"id": "edit", "type": producer_type, "config": {"prompt": "Edit the draft.", **producer_config},
         "next": ["notify_owner"]},
        {"id": "notify_owner", "type": "notify", "config": {"message": f"Ready: {reference}"}, "next": []},
        *extra,
    ]


@pytest.mark.parametrize("producer_config,producer_type,reference,fragment", [
    # The reply stays text when the schema is not asked for as JSON.
    ({"output_schema": _SUMMARY_SCHEMA}, "agent", "{{edit.headline}}", "output_format: 'json'"),
    ({"output_schema": _SUMMARY_SCHEMA, "output_format": "json"}, "agent", "{{edit.deliverable_document_id}}",
     "declares no 'deliverable_document_id' (it declares headline)"),
    ({"output_var": "edited", "output_schema": _SUMMARY_SCHEMA, "output_format": "json"}, "llm",
     "{{edited.title}}", "declares no 'title'"),
    ({}, "classifier", "{{edit.label}}", "returns its label as text"),
])
def test_a_field_an_agent_step_never_returns_is_rejected(producer_config, producer_type, reference, fragment):
    result = validate_workflow_steps(_reads_agent_field(producer_config, reference, producer_type=producer_type))
    assert not result["valid"]
    [error] = [error for error in result["errors"] if error["code"] == "unknown_agent_output"]
    assert reference in error["message"] and fragment in error["message"], error


@pytest.mark.parametrize("producer_config,reference,extra", [
    # Without its own output_schema the step returns the fields later steps read.
    ({"expects": ["files"]}, "{{edit.deliverable_document_id}}", ()),
    ({"output_schema": _SUMMARY_SCHEMA, "output_format": "json"}, "{{edit.headline}}", ()),
    # Another step also writes the alias, so the runner decides which value it reads.
    ({"output_var": "edited", "output_schema": _SUMMARY_SCHEMA}, "{{edited.title}}",
     ({"id": "fallback", "type": "transform", "config": {"set": {"edited": {"title": "Untitled"}}}, "next": []},)),
])
def test_a_field_an_agent_step_returns_is_accepted(producer_config, reference, extra):
    result = validate_workflow_steps(_reads_agent_field(producer_config, reference, extra=extra))
    assert not [error for error in result["errors"] if error["code"] == "unknown_agent_output"], result["errors"]


@pytest.mark.parametrize("under,valid", [
    ("Articles", False), ("projects/../secrets", False), ("projects/series", True),
    ("{{series.state.project_root}}", True),
])
def test_a_literal_project_folder_must_be_inside_projects(under, valid):
    config = {"operation": "create", "project_type": "article", "project_key": "{{topic}}", "schema_version": 1,
              "state_schema": {"type": "object", "properties": {}}, "state": {}, "current_stage": "new",
              "folder": {"name": "{{topic}}", "under": under}}
    result = validate_workflow_steps(_graph(config, step_type="workflow_project"))
    assert any(error["code"] == "invalid_project_folder" for error in result["errors"]) is not valid


_ARTICLE_SCHEMA = {"type": "object", "properties": {"status": {"type": "string"}, "title": {"type": "string"}}}


def _project_graph(stage_config):
    create = {"operation": "create", "project_type": "article", "project_key": "{{topic}}", "schema_version": 1,
              "state_schema": _ARTICLE_SCHEMA, "state": {"title": "{{topic}}"}, "current_stage": "research"}
    return [
        {"id": "start", "type": "trigger", "config": {"run_inputs": [{"key": "topic"}]}, "next": ["create_article"]},
        {"id": "create_article", "type": "workflow_project", "config": create, "next": ["writing_stage"]},
        {"id": "writing_stage", "type": "workflow_project", "config": stage_config, "next": []},
    ]


_PATCH = {"operation": "patch", "project_id": "{{create_article.project_id}}", "project_type": "article",
          "schema_version": 1, "expected_revision": "{{create_article.revision}}", "patch": {"status": "writing"},
          "current_stage": "writing", "state_schema": _ARTICLE_SCHEMA}


def test_a_project_stage_advances_with_patch():
    result = validate_workflow_steps(_project_graph(_PATCH))
    assert result["valid"], result["errors"]


@pytest.mark.parametrize("stage_config,fragment", [
    # The 2026-10-27 acceptance run: the studio's stage steps used "update".
    ({**_PATCH, "operation": "update"}, "unsupported config.operation 'update'"),
    ({**_PATCH, "project_id": "{{create_article.id}}"}, "outputs created_by"),
    # The 2026-11-02 eval: a schedule step moved posts to a stage its allowed_stages left out.
    ({**_PATCH, "allowed_stages": ["new"]}, "does not include"),
])
def test_a_project_stage_the_runner_cannot_execute_is_rejected(stage_config, fragment):
    result = validate_workflow_steps(_project_graph(stage_config))
    assert not result["valid"]
    assert any(fragment in error["message"] for error in result["errors"]), result["errors"]


def test_an_unknown_operation_is_rejected_for_every_operation_node():
    result = validate_workflow_steps([
        {"id": "start", "type": "trigger", "config": {}, "next": ["grant"]},
        {"id": "grant", "type": "workflow_action_grant", "config": {"operation": "grant"}, "next": []},
    ])
    assert any(error["code"] == "unsupported_node_operation" for error in result["errors"]), result["errors"]


# ── Project state a person can follow ─────────────────────────────────────
# Two unrelated processes kept the way the Architect is told to: what identifies
# the item, every file a stage saves, what a stage reports, each decision, dates.

_STUDIO = {
    "project_type": "client_article", "inputs": ["client", "topic", "deadline"], "approval": "final_approval",
    "stages": [("research", "research_document_id", "sources_checked"), ("write", "draft_document_id", "word_count"),
               ("edit", "deliverable_document_id", "editor_notes")],
    "done": "delivered_at",
}
_INSPECTION = {
    "project_type": "home_inspection", "inputs": ["client", "property_address", "inspection_date"],
    "approval": "report_approval",
    "stages": [("inspect", "field_notes_document_id", "defects_found"), ("report", "report_document_id", "repair_estimate")],
    "done": "sent_at",
}


def _followable_workflow(scenario):
    kind, stages, approval = scenario["project_type"], scenario["stages"], scenario["approval"]
    fields = [*scenario["inputs"], *(name for _, document, reported in stages for name in (document, reported)),
              scenario["done"]]
    schema = {"type": "object", "properties": {
        **{name: {"type": "string"} for name in fields}, "project_root": {"type": "string"},
        "history": {"type": "array"}}}

    def project(step_id, after, config, nxt):
        return {"id": step_id, "type": "workflow_project", "next": [nxt], "config": {
            "project_type": kind, "schema_version": 1, "state_schema": schema, **config,
            **({"operation": "patch", "project_id": "{{open.project_id}}",
                "expected_revision": f"{{{{{after}.revision}}}}"} if after else {})}}

    steps = [project("open", None, {"operation": "create", "project_key": "{{%s}}" % scenario["inputs"][1],
                                    "state": {name: "{{%s}}" % name for name in scenario["inputs"]},
                                    "folder": {"name": "{{%s}}" % scenario["inputs"][1]},
                                    "current_stage": stages[0][0]}, stages[0][0])]
    last = "open"
    for index, (stage, document, reported) in enumerate(stages):
        steps.append({"id": stage, "type": "agent", "next": [f"save_{stage}"], "config": {
            "service_key": stage, "prompt": f"Do the {stage} work for {{{{{scenario['inputs'][1]}}}}}.",
            "expects": ["files"]}})
        nxt = stages[index + 1][0] if index + 1 < len(stages) else "review"
        steps.append(project(f"save_{stage}", last, {
            "patch": {document: f"{{{{{stage}.{document}}}}}", reported: f"{{{{{stage}.{reported}}}}}"},
            "current_stage": nxt}, nxt))
        last = f"save_{stage}"
    steps += [
        {"id": "review", "type": "wait", "next": ["approved"], "config": {
            "wait_type": "approval", "message": "Approve it?", "options": ["approve", "revise"],
            "approval_values": ["approve"], "revise_from": stages[-1][0], "response_variable": approval}},
        project("approved", last, {"patch": {}, "current_stage": "approved", "history_event": {
            "kind": "review", "decision": f"{{{{{approval}.choice}}}}", "note": f"{{{{{approval}.note}}}}"}}, "stamp"),
        {"id": "stamp", "type": "datetime", "next": ["done"], "config": {"operation": "now"}},
        project("done", "approved", {"patch": {scenario["done"]: "{{stamp}}"}, "current_stage": "done"}, "finish"),
        {"id": "finish", "type": "end", "config": {}, "next": []},
    ]
    return {"slug": kind, "name": kind.replace("_", " ").title(), "steps": steps,
            "run_inputs": [{"key": name, "type": "string", "required": True} for name in scenario["inputs"]]}


@pytest.mark.parametrize("scenario", [_STUDIO, _INSPECTION], ids=["studio", "home_inspection"])
def test_a_project_kept_for_the_person_following_it_saves_as_written(scenario):
    from packages.core.blueprints.installer import _blueprint_workflow_definition_values
    from packages.core.services.workspace_app_designer import AppDesignContext

    workflow = _followable_workflow(scenario)
    _blueprint_workflow_definition_values(workflow)
    [project] = AppDesignContext.from_draft_fields({"workflows": [workflow]}).summary()["workflows"][0]["projects"]
    for _stage, document, reported in scenario["stages"]:
        assert {document, reported} <= set(project["fields"])
    assert {*scenario["inputs"], scenario["done"]} <= set(project["fields"])
    assert project["lists"] == ["history"]


@pytest.mark.parametrize("reference", ["{{review.decided_at}}", "{{review.choice}}"])
def test_an_approval_waits_own_id_has_no_fields(reference):
    workflow = _followable_workflow(_STUDIO)
    approved = next(step for step in workflow["steps"] if step["id"] == "approved")
    approved["config"]["history_event"]["at"] = reference
    result = validate_workflow_steps(workflow["steps"])
    [error] = [error for error in result["errors"] if error["code"] == "unknown_wait_output"]
    assert reference in error["message"] and "{{final_approval.note}}" in error["message"], error


@pytest.mark.parametrize("change,fragment", [
    # The 2026-11-01 studio comparison appended {path, value}: the runner reads items, so nothing was kept.
    (lambda config: config.update(list_appends=[{"path": "history", "value": {"decision": "approve"}}]),
     "list_appends entry without items"),
    (lambda config: config.update(list_upserts=[{"path": "history", "items": []}]), "list_upserts entry without item"),
    (lambda config: config.update(list_appends=True), "list_appends must be an array"),
    (lambda config: config.update(list_upserts=7), "list_upserts must be an array"),
    (lambda config: config.update(list_removes={"path": "history"}), "list_removes must be an array"),
])
def test_a_list_write_the_runner_cannot_read_is_rejected(change, fragment):
    workflow = _followable_workflow(_INSPECTION)
    change(next(step for step in workflow["steps"] if step["id"] == "approved")["config"])
    result = validate_workflow_steps(workflow["steps"])
    assert any(fragment in error["message"] for error in result["errors"]), result["errors"]


@pytest.mark.parametrize("entry,expected", [
    ({"path": "history", "keys": ["b", "a"]}, ["b", "a"]),
    ({"path": "history"}, []),
], ids=["reorder", "clear"])
def test_reconcile_defaults_keep_existing_workflows_valid(entry, expected):
    from packages.core.ai.workflow_runner import _reconcile_project_list_items

    config = {
        "operation": "patch", "project_id": "project-1", "project_type": "case", "schema_version": 1,
        "expected_revision": 1, "patch": {}, "list_reconciles": [entry],
        "state_schema": {"type": "object", "properties": {"history": {"type": "array"}}},
    }
    result = validate_workflow_steps(_graph(config, step_type="workflow_project"))
    assert result["valid"], result["errors"]

    rows = [{"id": "a", "value": 1}, {"id": "b", "value": 2}]
    _reconcile_project_list_items(rows, entry.get("items", []), "id", entry.get("keys", []))
    assert [row["id"] for row in rows] == expected


def test_a_list_or_history_write_on_a_create_is_rejected():
    workflow = _followable_workflow(_INSPECTION)
    workflow["steps"][0]["config"]["history_event"] = {"kind": "opened"}
    result = validate_workflow_steps(workflow["steps"])
    assert any("history_event, which only a workflow_project patch applies" in error["message"]
               for error in result["errors"]), result["errors"]


def test_an_approval_answer_resolves_as_the_architect_is_told():
    from packages.core.ai.workflow_runner import (
        UnresolvedWorkflowReference,
        _references_must_resolve,
        _resolve_structure,
    )

    # What the runner holds after the person approves without a note: the wait's
    # result is its message, the answer sits under its response_variable. The
    # note they did not write is left out of the logged decision.
    variables = {"review": "Approve it?", "final_approval": {"choice": "approve"}}
    with _references_must_resolve():
        assert _resolve_structure({"decision": "{{final_approval.choice}}", "note": "{{final_approval.note}}"},
                                  variables) == {"decision": "approve"}
        # The low-level resume API takes the bare choice: logging it must not fail a post already published.
        assert _resolve_structure({"decision": "{{final_approval.choice}}", "note": "{{final_approval.note}}"},
                                  {"final_approval": "approve"}) == {"decision": "approve"}
        with pytest.raises(UnresolvedWorkflowReference):
            _resolve_structure("{{review.decided_at}}", variables)


async def test_mcp_upsert_workflow_refuses_the_same_graph_without_saving():
    from apps.api.mcp.tools_workspace import upsert_workflow

    api = SimpleNamespace(put=AsyncMock(), post=AsyncMock())
    result = await upsert_workflow(SimpleNamespace(api=api), {
        "name": "Flow", "steps": _graph({"prompt": "{{ inputs.topic }}"}), "workspace_id": "ws-1",
    })
    assert result["saved"] is False
    assert any(error["code"] == "unresolvable_reference" for error in result["validation"]["errors"])
    api.post.assert_not_awaited()


# ── Architect tool ────────────────────────────────────────────────────────

async def test_architect_gets_the_validator_errors_back(draft):
    steps = _article_steps()
    steps[0]["config"]["prompt"] = "Research {{ inputs.topic }}."
    result = await _propose(steps, run_inputs=_run_inputs())
    assert result["ok"] is False
    assert "inputs.topic" in result["error"]
    assert "workflows" not in draft.fields


async def test_architect_workflow_must_run_services_in_the_draft(draft):
    steps = _article_steps()
    steps[2]["config"]["service_key"] = "proofreading"
    result = await _propose(steps, run_inputs=_run_inputs())
    assert result["ok"] is False
    assert result["unknown_service_keys"] == ["proofreading"]


async def test_a_staged_role_workflow_is_saved_runnable(draft):
    from packages.core.blueprints.installer import _blueprint_workflow_definition_values

    result = await _propose(_article_steps(), run_inputs=_run_inputs())
    assert result["ok"] is True, result
    [saved] = draft.fields["workflows"]
    assert saved["run_inputs"] == _run_inputs()
    runtime = _blueprint_workflow_definition_values(saved)["steps"]
    start = runtime[0]
    assert start["type"] == "trigger"
    # The start node routes to the first stage only, not to every stage.
    assert start["next"] == ["research"]
    assert [item["key"] for item in start["config"]["run_inputs"]] == ["topic", "audience"]


_MENU = {"slug": "menu", "item_project_type": "menu_dish", "item_word": "dish", "title_field": "name",
         "live_stage": "on_menu", "off_stage": "off_menu", "live_words": "On the menu", "off_words": "Off the menu",
         "fields": [{"key": "name", "label": "Dish name", "kind": "text", "required": True},
                    {"key": "price", "label": "Price", "kind": "money", "required": True},
                    {"key": "spice", "label": "Spice", "choices": [{"value": "mild", "label": "微辣"},
                                                                   {"value": "hot", "label": "特辣"}]},
                    {"key": "photo", "label": "Photo", "kind": "picture"}]}


async def test_a_menu_kept_by_hand_gets_its_add_change_and_take_off_flows_from_one_call(draft):
    """Staging 2026-11-09: an Architect building alone wrote these Flows itself; its edit saved stage
    ``updated`` and its Take off was the edit Flow under another name."""
    from packages.core.blueprints.installer import _blueprint_workflow_definition_values

    result = json.loads(await workspace_arch_tools._propose_record_collection(
        None, entity_id="entity-1", draft_id="draft-1", **_MENU))
    assert result["ok"] is True, result
    assert result["slugs"] == ["menu_add", "menu_edit", "menu_take_off"]
    saved = {row["slug"]: row for row in draft.fields["workflows"]}
    assert set(saved) == set(result["slugs"])
    for row in saved.values():
        validate_workflow_steps(_blueprint_workflow_definition_values(row)["steps"])
    [take_off] = [step for step in saved["menu_take_off"]["steps"] if step.get("config", {}).get("operation") == "patch"]
    assert take_off["config"]["current_stage"] == "off_menu"
    edit_stages = {step["config"].get("current_stage") for step in saved["menu_edit"]["steps"]
                   if step.get("config", {}).get("operation") == "patch"}
    assert edit_stages <= {None, "on_menu"}, edit_stages

    # Calling it again replaces the three Flows instead of adding copies.
    again = json.loads(await workspace_arch_tools._propose_record_collection(
        None, entity_id="entity-1", draft_id="draft-1", **_MENU))
    assert again["ok"] is True and len(draft.fields["workflows"]) == 3


async def test_adding_a_promotion_makes_its_poster_in_the_same_run(draft):
    """Staging 2026-11-09, 2 of 3 restaurant drafts: a new promotion only saved its record; its dishes and
    its poster and copy each took another button."""
    from packages.core.blueprints.installer import _blueprint_workflow_definition_values

    assets = json.loads(await workspace_arch_tools._propose_workflow(
        None, entity_id="entity-1", draft_id="draft-1", slug="promotion_assets", name="Make the poster and copy",
        run_inputs=[{"key": "promotion_id", "label": "Promotion", "type": "string", "required": True, "hidden": True}],
        steps=[{"id": "make", "type": "agent", "next": [], "config": {
            "service_key": "writing", "prompt": "Make the poster and copy for promotion {{promotion_id}}.",
            "expects": ["files"]}}]))
    assert assets["ok"] is True, assets
    promotions = {"slug": "promotion", "item_project_type": "promotion", "item_word": "promotion", "title_field": "name",
                  "fields": [{"key": "name", "label": "Name", "required": True},
                             {"key": "starts", "label": "Starts", "kind": "date"},
                             {"key": "dishes", "label": "Dishes", "kind": "records", "project_type": "menu_dish"}],
                  "then_workflow": "promotion_assets", "then_input": "promotion_id"}
    result = json.loads(await workspace_arch_tools._propose_record_collection(
        None, entity_id="entity-1", draft_id="draft-1", **promotions))
    assert result["ok"] is True, result
    add = next(row for row in draft.fields["workflows"] if row["slug"] == "promotion_add")
    steps = _blueprint_workflow_definition_values(add)["steps"]
    validate_workflow_steps(steps)
    then = next(step for step in add["steps"] if step["id"] == "then")
    assert then["config"]["workflow_id"] == "promotion_assets"
    assert then["config"]["inputs"] == [{"key": "promotion_id", "value": "{{save.project_id}}"}]
    [dishes] = [row for row in next(step for step in add["steps"] if step["type"] == "trigger")["config"]["run_inputs"]
                if row["key"] == "dishes"]
    assert dishes["schema"]["x-ui"] == {"control": "workflow_project_select", "project_type": "menu_dish"}


async def test_a_collection_the_pattern_cannot_build_saves_nothing(draft):
    result = json.loads(await workspace_arch_tools._propose_record_collection(
        None, entity_id="entity-1", draft_id="draft-1", **{**_MENU, "title_field": "title"}))
    assert result["ok"] is False and "title_field" in result["error"]
    assert "workflows" not in draft.fields


def _reply_steps(options):
    return [
        {"id": "draft", "type": "agent", "next": ["review"], "config": {
            "service_key": "writing", "prompt": "Draft a reply to {{topic}}.", "expects": ["files"]}},
        {"id": "review", "type": "wait", "next": ["send"], "config": {
            "wait_type": "approval", "options": options,
            "message": "Approve this reply before it is sent?" if "revise" in options else "Confirm this reply before it is sent.",
            **({"approval_values": ["approve"], "revise_from": "draft"} if "revise" in options else {})}},
        {"id": "send", "type": "agent", "next": [], "config": {
            "service_key": "editing", "prompt": "Send {{draft}} exactly.", "expects": ["effects"]}},
    ]


async def test_a_reply_confirmed_before_it_is_sent_can_be_sent_back(draft):
    """Staging 2026-11-09, 5 of 6 drafts: "confirm this reply before it is sent" offered one Confirm."""
    refused = await _propose(_reply_steps(["confirm"]), run_inputs=_run_inputs())
    assert refused["ok"] is False and "revise_from 'draft'" in refused["error"]
    assert await _propose(_reply_steps(["approve", "revise"]), run_inputs=_run_inputs()) == {
        "ok": True, "slug": "article_production", "internal": False}

    # Confirming what the person did themselves still takes one Confirm.
    delivered = [*_article_steps(approval=False), {"id": "delivered", "type": "wait", "next": [], "config": {
        "wait_type": "approval", "message": "Confirm once the Word file is delivered.", "options": ["confirm"]}}]
    delivered[2]["next"] = ["delivered"]
    assert (await _propose(delivered, run_inputs=_run_inputs()))["ok"] is True


async def test_an_approval_never_sends_twice(draft):
    """Staging 2026-11-09: a reply Flow's drafting step sent routine replies itself and returned
    needs_approval, every message still waited, and revise re-ran the step that sends."""
    flagged = [
        {"id": "draft", "type": "agent", "next": ["review"], "config": {
            "service_key": "writing", "expects": ["files"],
            "prompt": "Answer {{topic}}. Send routine replies yourself; set needs_approval true for the rest."}},
        *_reply_steps(["approve", "revise"])[1:],
    ]
    refused = await _propose(flagged, run_inputs=_run_inputs())
    assert refused["ok"] is False and "{{draft.needs_approval}}" in refused["error"]

    branched = [{**flagged[0], "next": ["needs_it"]},
                {"id": "needs_it", "type": "condition", "next": [], "config": {"expression": "draft.needs_approval == true"},
                 "true_next": ["review"], "false_next": []},
                *flagged[1:]]
    assert (await _propose(branched, run_inputs=_run_inputs()))["ok"] is True

    resend = _reply_steps(["approve", "revise"])
    resend[1]["config"]["revise_from"] = "send"
    refused = await _propose(resend, run_inputs=_run_inputs())
    assert refused["ok"] is False and "re-runs send" in refused["error"]


@pytest.mark.parametrize("timezone", ["America/Los_Angeles", "Invalid/Timezone"])
async def test_new_messages_start_their_flow_from_a_scheduled_watch(draft, timezone):
    """Staging 2026-11-09, 3 of 3 candle shop drafts: customer DMs were only answered when the owner pasted one in."""
    handle = json.loads(await workspace_arch_tools._propose_workflow(
        None, entity_id="entity-1", draft_id="draft-1", slug="customer_reply", name="Reply to a customer",
        run_inputs=[{"key": "message_id", "label": "Message", "type": "string", "required": True}],
        steps=_reply_steps(["approve", "revise"])))
    assert handle["ok"] is True, handle

    before = deepcopy(draft.fields)
    result = json.loads(await workspace_arch_tools._propose_source_watch(
        None, entity_id="entity-1", draft_id="draft-1", slug="customer_dms", service_key="research",
        source="Instagram direct messages", prompt="Customer questions about orders and products.",
        item_word="message", item_fields=["sender", "text"], run_workflow="customer_reply", run_input="message_id",
        timezone=timezone))
    if timezone == "Invalid/Timezone":
        assert result["ok"] is False and result["code"] == "invalid_automation_timezone"
        assert draft.fields == before
        return
    assert result["ok"] is True, result
    assert [row["slug"] for row in draft.fields["workflows"]] == ["customer_reply", "customer_dms_one", "customer_dms_watch"]
    [job] = draft.fields["scheduled_jobs"]
    assert job["execution_target"]["workflow_slug"] == "customer_dms_watch"
    assert (job["cron_expr"], job["timezone"]) == ("0 * * * *", "America/Los_Angeles")
    assert [row["job_id"] for row in draft.fields["automations"]] == ["customer_dms_watch"]


async def test_only_staff_the_person_named_are_assigned(draft, monkeypatch):
    staff = SimpleNamespace(id="01KTWNETW3A6PXPXJQHZ959XSZ", entity_id="entity-1", name="sam", display_name="Sam")

    class Db:
        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: staff)

    draft.fields["initial_brief"] = "Ask Sam to review"
    draft.fields[STAFF_REQUEST_TEXT_FIELD] = "Write sample posts for me to approve."
    draft.messages = []
    monkeypatch.setattr(workspace_draft_service, "flag_modified", lambda *_: None)
    workspace_draft_service._request_turn(
        draft, "Ask Sam to review", note="[附件/Attached: Sam]", mcp_client=False,
        staff_request_text="我审批后再发。",
    )

    async def assign():
        return json.loads(await workspace_arch_tools._assign_staff(
            Db(), entity_id="entity-1", draft_id="draft-1", staff_id=staff.id, role="reviewer"))

    refused = await assign()
    assert refused["ok"] is False and "needs no assignment" in refused["hint"]
    assert "staff_assignments" not in draft.fields

    workspace_draft_service._request_turn(
        draft, "Add a reviewer", note="", mcp_client=False, staff_request_text="让SAM也看一下帖子",
    )
    assert (await assign())["ok"] is True
    assert [row["staff_name"] for row in draft.fields["staff_assignments"]] == ["sam"]


def test_a_flow_that_routes_back_to_an_earlier_step_is_rejected():
    """The 2026-11-02 eval: a worker routed back to lease the next item and never ran its first step."""
    result = validate_workflow_steps([
        {"id": "start", "type": "trigger", "config": {}, "next": ["lease"]},
        {"id": "lease", "type": "workflow_project", "next": ["launch"], "config": {
            "operation": "lease_due", "project_type": "post", "schema_version": 1, "due_path": "due_at",
            "pending_stages": ["scheduled"], "state_schema": {"type": "object", "properties": {}}}},
        {"id": "launch", "type": "transform", "next": ["lease"], "config": {}},
    ])
    assert [error["message"] for error in result["errors"] if error["code"] == "graph_cycle"] == [
        "Workflow routes back to an earlier step (lease → launch → lease); a run takes each step at most once, so it "
        "would wait there forever. Repeat work with foreach_subworkflow over the items, or let an Automation run "
        "the Workflow again"]


def test_the_start_node_skips_a_step_only_a_failure_leads_to():
    """The 2026-11-02 eval: a Flow without a trigger marked a post failed at the start of every run."""
    from packages.core.blueprints.installer import _blueprint_workflow_definition_values

    steps = _blueprint_workflow_definition_values({"slug": "publish", "steps": [
        {"id": "publish", "type": "agent", "next": ["receipt"], "config": {"service_key": "writing", "prompt": "Post."}},
        {"id": "receipt", "type": "publication_receipt", "next": ["done"], "config": {
            "receipt": "{{publish.receipt}}", "on_error": "continue_error", "error_next": ["failed"]}},
        {"id": "failed", "type": "end", "next": [], "config": {}},
        {"id": "done", "type": "end", "next": [], "config": {}},
    ]})["steps"]
    assert steps[0]["type"] == "trigger" and steps[0]["next"] == ["publish"]


async def test_a_flow_that_runs_one_child_per_planned_item_names_a_child_of_the_draft(draft):
    """Branches, code and per-item child runs are proposed like any step; a child is another Draft Flow."""
    planning = [
        {"id": "plan", "type": "agent", "next": ["approved"], "config": {
            "service_key": "research", "prompt": "Plan articles on {{topic}}.", "output_format": "json",
            "output_schema": {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}}},
        {"id": "approved", "type": "condition", "next": [], "config": {"expression": "plan.items != []"},
         "true_next": ["dated"], "false_next": ["done"]},
        {"id": "dated", "type": "datetime", "next": ["each"], "config": {"operation": "now", "output_var": "dated"}},
        {"id": "each", "type": "foreach_subworkflow", "next": ["done"], "config": {
            "workflow_id": "write_one", "over": "{{plan.items}}", "item_key": "id", "item_var": "item"}},
        {"id": "done", "type": "end", "next": [], "config": {}},
    ]
    refused = await _propose(planning, run_inputs=_run_inputs())
    assert refused["ok"] is False and "write_one" in refused["error"]

    child = json.loads(await workspace_arch_tools._propose_workflow(
        None, entity_id="entity-1", draft_id="draft-1", slug="write_one", name="Write one", internal=True,
        trigger_type="internal", run_inputs=[{"key": "item", "type": "json", "required": True}],
        steps=[{"id": "write", "type": "agent", "next": [], "config": {
            "service_key": "writing", "prompt": "Write {{item}}."}}]))
    assert child["ok"] is True, child
    saved = await _propose(planning, run_inputs=_run_inputs())
    assert saved["ok"] is True, saved
    assert [row["slug"] for row in draft.fields["workflows"]] == ["write_one", "article_production"]


# ── Lint: declared process is run by a Workflow ───────────────────────────

def _process_issues(fields):
    return [issue["where"] for issue in workspace_arch_tools.workflow_process_issues(fields)]


def test_declared_handoffs_and_review_without_a_workflow_are_reported():
    assert _process_issues({"services": _services(), "workflows": []}) == [
        "services.research.handoff_to", "services.writing.handoff_to", "services.editing.human_review",
    ]


def test_review_needs_an_approval_wait_after_the_reviewed_stage():
    workflow = {"slug": "article", "name": "Article", "steps": _article_steps(approval=False)}
    assert _process_issues({"services": _services(), "workflows": [workflow]}) == ["services.editing.human_review"]


def test_a_workflow_that_runs_the_process_satisfies_the_lint():
    workflow = {"slug": "article", "name": "Article", "steps": _article_steps(), "run_inputs": _run_inputs()}
    assert _process_issues({"services": _services(), "workflows": [workflow]}) == []


def test_independent_services_need_no_workflow():
    services = [{"service_key": key, "name": key.title()} for key in ROLES]
    assert _process_issues({"services": services, "workflows": []}) == []


async def test_handoff_must_name_another_service(draft):
    result = json.loads(await workspace_arch_tools._propose_service(
        None, entity_id="entity-1", draft_id="draft-1", service_key="research", name="Research",
        description="Find sourced angles.", autonomy_level="supervised", owner_role="researcher",
        required_capabilities=[], handoff_to=["research"],
    ))
    assert result["ok"] is False


# ── Run input controls ────────────────────────────────────────────────────

_PRODUCT_STATE = {"type": "object", "properties": {
    name: {"type": "string"} for name in ("name", "status", "photo_document_id", "launch_date")}}
_UPLOAD = {"type": "array", "minItems": 1, "maxItems": 1, "x-ui": {"control": "documents", "accept": ["image"]},
           "items": {"type": "object", "required": ["document_id"], "properties": {"document_id": {"type": "string"}}}}
_STATUS = {"type": "string", "enum": ["active", "retired"], "x-enum-labels": ["Active", "Retired"],
           "x-ui": {"control": "segmented"}}


async def _propose_flow(slug, steps, run_inputs):
    return json.loads(await workspace_arch_tools._propose_workflow(
        None, entity_id="entity-1", draft_id="draft-1", slug=slug, name=slug, steps=steps, run_inputs=run_inputs))


def _project_step(step_id, operation, values, **config):
    key = "state" if operation == "create" else "patch"
    return {"id": step_id, "type": "workflow_project", "next": [], "config": {
        "operation": operation, "project_type": "product", "schema_version": 1, "state_schema": _PRODUCT_STATE,
        "current_stage": "listed", key: values, **config}}


async def test_run_inputs_a_person_could_upload_or_pick_are_refused_until_they_use_controls(draft):
    typed = await _propose_flow("add_product", [_project_step("create", "create", {
        "name": "{{name}}", "status": "{{status}}", "photo_document_id": "{{photo}}", "launch_date": "{{launch_date}}"})], [
        {"key": "name", "type": "string"}, {"key": "status", "type": "string"},
        {"key": "photo", "type": "string", "label": "Photo document ID or URL"},
        {"key": "launch_date", "type": "string"}])
    assert typed["ok"] is False
    assert [issue.split(" ")[0] for issue in typed["issues"]] == [
        f"workflows.add_product.run_inputs.{key}" for key in ("status", "photo", "launch_date")]
    # A control declared on the input itself is ignored by the run form, which then shows a text box.
    invented = await _propose_flow("add_product", [_project_step("create", "create", {"status": "{{status}}"})], [
        {"key": "status", "type": "select", "options": ["active", "retired"]}])
    assert invented["ok"] is False and "'select', 'options'" in invented["issues"][0]

    added = await _propose_flow("add_product", [_project_step("create", "create", {
        "name": "{{name}}", "status": "{{status}}", "photo_document_id": "{{photo.0.document_id}}",
        "launch_date": "{{launch_date}}"})], [
        {"key": "name", "type": "string"}, {"key": "status", "type": "string", "schema": _STATUS},
        {"key": "focus_on", "type": "string", "label": "What should we focus on?"},
        {"key": "photo", "type": "json", "schema": _UPLOAD},
        {"key": "launch_date", "type": "string", "schema": {"type": "string", "format": "date"}}])
    assert added["ok"] is True, added

    picker = {"key": "product_id", "type": "string", "schema": {
        "type": "string", "x-ui": {"control": "workflow_project_select", "project_type": "product"}}}
    get = _project_step("get", "get", {}, project_id="{{product_id}}")
    get["config"].pop("patch")
    retyped = await _propose_flow("edit_product", [{**get, "next": ["save"]}, _project_step(
        "save", "patch", {"name": "{{name}}", "status": "{{status}}"},
        project_id="{{get.project_id}}", expected_revision="{{get.revision}}")],
        [picker, {"key": "name", "type": "string"}, {"key": "status", "type": "string", "schema": _STATUS}])
    assert retyped["ok"] is False
    assert "name, status again" in retyped["issues"][0]

    # The edit asks only for the record and opens the changes with its current values.
    changes = {"id": "changes", "type": "wait", "next": ["save"], "config": {
        "wait_type": "input", "message": "Change the product", "response_variable": "changes",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string", "default": "{{get.state.name}}"},
            "status": {**_STATUS, "default": "{{get.state.status}}"},
            "photo": {**_UPLOAD, "default": [{"document_id": "{{get.state.photo_document_id}}"}]}}}}}
    edited = await _propose_flow("edit_product", [{**get, "next": ["changes"]}, changes, _project_step(
        "save", "patch", {"name": "{{changes.name}}", "status": "{{changes.status}}",
                          "photo_document_id": "{{changes.photo.0.document_id}}"},
        project_id="{{get.project_id}}", expected_revision="{{get.revision}}")], [picker])
    assert edited["ok"] is True, edited
    assert [row["slug"] for row in draft.fields["workflows"]] == ["add_product", "edit_product"]


def test_several_records_are_asked_with_a_picker_of_several():
    from packages.core.services.workflow_run_input_guidance import run_input_issues

    add = {"slug": "add_product", "run_inputs": [{"key": "name"}],
           "steps": [_project_step("create", "create", {"name": "{{name}}"})]}

    def issues(row):
        return run_input_issues([add, {"slug": "bundle", "run_inputs": [row], "steps": []}], checked={"bundle"})

    several = {"type": "array", "items": {"type": "string"}, "minItems": 1,
               "x-ui": {"control": "workflow_project_select", "project_type": "product"}}
    # A list of records typed as text is refused with the picker of several, not "not available".
    typed = issues({"key": "product_ids", "type": "string", "schema": {"type": "string", "x-ui": {"control": "textarea"}}})
    assert len(typed) == 1 and "picker of several" in typed[0] and "not available" not in typed[0]
    # The picker of several holds a list, so its input is json.
    assert "type:'json'" in issues({"key": "product_ids", "type": "string", "schema": several})[0]
    assert issues({"key": "product_ids", "type": "json", "schema": several}) == []
