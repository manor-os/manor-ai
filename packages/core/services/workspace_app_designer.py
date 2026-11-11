"""The App design sub-agent: designs a Workspace App page on command.

The Workspace Architect (on a creation Draft) and Workspace chat (on a live
Workspace) do not lay out the App themselves. They send this sub-agent a brief
and accept or send back the candidate it returns. The sub-agent reads what the
Workspace runs, composes the page from the module schematic and recipes in the
``workspace-app-design`` skill, and submits a candidate that has passed the
same checks either surface applies later.

Both surfaces design in the portable (Blueprint) key space: Workflow slugs,
scheduled job ids, service keys and Agent slugs. A Draft already uses it; a
live Workspace's App is projected into it and back with the export identities,
so one checker and one pattern serve both.

It is the ``app`` part of the part designer registry
(``workspace_part_designers``).
"""
from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.subagents import WORKSPACE_APP_DESIGNER_SUBAGENT, SubAgentSpec
from packages.core.contracts.project_display import PROJECT_DISPLAY_KEY
from packages.core.services.workspace_part_designers import PartDesigner, run_part_designer
from packages.core.services.workspace_app_view import (
    DOCUMENT_FIELD_FORMATS, PREVIEW_PLATFORMS, block_card_actions, preview_platform,
)

logger = logging.getLogger(__name__)

# Map fields of an App config: a whole-page candidate removes the keys it drops.
_APP_MAP_FIELDS = ("labels", "descriptions", "forms", "targets", "entries", "prompts", "presets", "asks",
                   "icons", "data_sources")
_APP_PAGE_FIELDS = ("eyebrow", "title", "description", "hero", "order", "hidden", "visual", "layout",
                    "components", "sections", "theme")
# A candidate sets these but never clears them: a page with an uploaded cover
# is not portable, so it reaches the designer without its current page. ``visual: plain`` hides them.
# A project field that holds a date by its name (post_at, due_date, publish_on, date, when);
# bookkeeping stamps say when the record changed, not when the work happens.
_DATE_NAME = re.compile(r"(?!(?:created|updated|started|finished|completed|last_\w+|modified)_at$)"
                        r"(?:\w+_(?:at|date|on)|date|when)")
# The words of a social post by their usual names; with a platform field the project is a post to preview.
_POST_TEXT_FIELDS = ("caption", "post_text", "post_copy", "tweet", "hashtags")
_APP_PICTURE_FIELDS = ("cover_image_url", "motion_cover_url", "motion_cover_source_url")


# ── Context ─────────────────────────────────────────────────────────────────

def _names_nothing(field: str, fixed_inputs: set[str]) -> bool:
    """Whether a record's field can't tell it from the others: an id, a state or a fixed choice."""
    key = field.lower()
    return (key == "id" or key.endswith(("_id", "_ref", "_reference", "_period", "_type", "_kind"))
            or key in {"status", "stage", "state", "cadence", "frequency"} or field in fixed_inputs)


def _run_input_rows(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    rows = workflow.get("run_inputs")
    if not isinstance(rows, list) or not rows:
        variables = workflow.get("variables") or []
        rows = ([{"key": key, **(value if isinstance(value, dict) else {})} for key, value in variables.items()]
                if isinstance(variables, dict) else [row for row in variables if isinstance(row, dict)])
    return [row for row in rows if isinstance(row, dict)]


def _workflow_inputs(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """The inputs a run of this Workflow takes, with how the App can fill them."""
    inputs = []
    for row in _run_input_rows(workflow):
        key = str(row.get("key") or row.get("name") or "").strip()
        if not key:
            continue
        schema = row.get("schema") if isinstance(row.get("schema"), dict) else {}
        ui = schema.get("x-ui") if isinstance(schema.get("x-ui"), dict) else {}
        inputs.append({
            "key": key,
            "label": row.get("label") or key,
            "required": bool(row.get("required")),
            "hidden": bool(row.get("hidden")),
            **({"description": row["description"]} if row.get("description") else {}),
            **({"selects_project_type": ui["project_type"]} if ui.get("control") == "workflow_project_select"
               and ui.get("project_type") else {}),
            **({"choices": schema["enum"]} if isinstance(schema.get("enum"), list) else {}),
        })
    return inputs


def _document_fields(properties: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[list[str]]:
    """Paths of the string fields that hold a Knowledge document id, nested ones too."""
    from packages.core.ai.workflow_runner import is_document_id_field

    paths: list[list[str]] = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("type") == "string" and is_document_id_field(name):
            paths.append([*prefix, name])
        elif spec.get("type") == "object" and isinstance(spec.get("properties"), dict):
            paths.extend(_document_fields(spec["properties"], (*prefix, name)))
    return paths


def _list_item_fields(project: dict[str, Any], path: Any, names: Any) -> None:
    """Note the fields a project list's items carry, so a page can show them."""
    if not isinstance(path, str) or not path:
        return
    fields = project["list_item_fields"].setdefault(path, [])
    fields += [name for name in names if isinstance(name, str) and name not in fields]


def _waits_on_person(workflow: dict[str, Any]) -> bool:
    return any(isinstance(step, dict) and (step.get("type") or step.get("kind")) == "wait"
               and isinstance(step.get("config"), dict)
               and step["config"].get("wait_type", "approval") in {"approval", "input"}
               for step in workflow.get("steps") or [])


def _workflow_projects(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """The projects a Workflow keeps, read from its ``workflow_project`` steps."""
    from packages.core.ai.workflow_runner import HISTORY_EVENT_STAMPS

    projects: dict[str, dict[str, Any]] = {}
    for step in workflow.get("steps") or []:
        if not isinstance(step, dict) or (step.get("type") or step.get("kind")) != "workflow_project":
            continue
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        project_type = str(config.get("project_type") or "").strip()
        if not project_type:
            continue
        project = projects.setdefault(project_type, {
            "project_type": project_type, "operations": [], "fields": {}, "lists": [], "written_lists": [],
            "list_item_fields": {}, "document_fields": [], "stages": [], "has_folder": False,
            "child_of_another_project": False, "remade_in_takes": False, "display_fields": [], "date_fields": [],
        })
        operation = str(config.get("operation") or "")
        if operation and operation not in project["operations"]:
            project["operations"].append(operation)
        # A schema may be a run value ("{{video_project_schema}}"), which declares nothing here.
        schema = config.get("state_schema") if isinstance(config.get("state_schema"), dict) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for name, spec in properties.items():
            kind = spec.get("type") if isinstance(spec, dict) else None
            project["fields"].setdefault(name, kind or "any")
            if kind in (None, "string") and name not in project["date_fields"] and (
                    (isinstance(spec, dict) and spec.get("format") in ("date", "date-time")) or _DATE_NAME.fullmatch(name)):
                project["date_fields"].append(name)
            if kind == "array" and name not in project["lists"]:
                project["lists"].append(name)
            items = spec.get("items") if isinstance(spec, dict) else None
            if kind == "array" and isinstance(items, dict) and isinstance(items.get("properties"), dict):
                _list_item_fields(project, name, items["properties"])
        # Shown as documents (project_documents, or a ``document`` field), never as text.
        for path in _document_fields(properties):
            if path not in project["document_fields"]:
                project["document_fields"].append(path)
        # A stage set from a run value is not a name; the step's allowed stages are.
        for stage in [config.get("current_stage"), *(config.get("allowed_stages") or [])]:
            if isinstance(stage, str) and stage and "{{" not in stage and stage not in project["stages"]:
                project["stages"].append(stage)
        project["has_folder"] = project["has_folder"] or isinstance(config.get("folder"), dict)
        state = config.get("state") if isinstance(config.get("state"), dict) else {}
        project["child_of_another_project"] = project["child_of_another_project"] or "parent_project_id" in state
        # A list is filled when a step appends or upserts into it, or sets it from a run value.
        written = [update.get("path") for key in ("list_appends", "list_upserts")
                   for update in config.get(key) or [] if isinstance(update, dict)]
        for key in ("list_appends", "list_upserts"):
            for update in config.get(key) or []:
                items = update.get("items") if isinstance(update, dict) else None
                for item in items if isinstance(items, list) else [items]:
                    if isinstance(item, dict):
                        _list_item_fields(project, update.get("path"), item)
        # A decision log: the step's values plus the ones the runner stamps on.
        if isinstance(config.get("history_event"), dict):
            written.append("history")
            if "history" not in project["lists"]:
                project["lists"].append("history")
            _list_item_fields(project, "history", [*config["history_event"], *HISTORY_EVENT_STAMPS])
        # A patch may be a whole run value ("{{slot.state}}"), which names no field.
        patch = config.get("patch") if isinstance(config.get("patch"), dict) else {}
        # What the Workflow says a person should see (``state.display``), which every page reads first.
        for written_display in (state.get(PROJECT_DISPLAY_KEY), patch.get(PROJECT_DISPLAY_KEY)):
            for name in written_display if isinstance(written_display, dict) else {}:
                if name not in project["display_fields"]:
                    project["display_fields"].append(name)
        written += [name for name, value in {**state, **patch}.items()
                    if isinstance(value, str) and "{{" in value]
        for name in written:
            if isinstance(name, str) and name not in project["written_lists"]:
                project["written_lists"].append(name)
        project["remade_in_takes"] = project["remade_in_takes"] or config.get("production") == "take"
    return list(projects.values())


_WHOLE_REFERENCE = re.compile(r"\{\{\s*([A-Za-z_][\w-]*(?:\.[\w-]+)*)\s*\}\}")
# A field's labels hold at most this many values, so a longer list of choices cannot be labeled.
_MAX_FIELD_LABELS = 24
# Blocks whose stage_labels also name a status field's values.
_STAGE_LABELED_BLOCKS = {"project_rollup", "progress", "packs", "records"}


def _schema_choices(schema: Any) -> dict[str, str] | None:
    """The fixed text values a schema allows, each with the name its Flow gives it ("" for none)."""
    enum = schema.get("enum") if isinstance(schema, dict) else None
    if not isinstance(enum, list) or not enum or not all(isinstance(value, str) for value in enum):
        return None
    names = schema.get("x-enum-labels")
    names = names if isinstance(names, list) and len(names) == len(enum) else [""] * len(enum)
    return {value: name.strip() if isinstance(name, str) else "" for value, name in zip(enum, names)}


def _add_choices(into: dict[Any, dict[str, str]], key: Any, choices: dict[str, str]) -> None:
    held = into.setdefault(key, {})
    for value, name in choices.items():
        held[value] = held.get(value) or name


def _workflow_value_refs(workflow: dict[str, Any], read: Callable[[Any], Any],
                         add: Callable[[dict[str, Any], str, Any], None]) -> dict[str, Any]:
    """What ``read`` finds in the schema of each run value of a Workflow, by every reference its steps can use for it.

    A run input is read as ``{{key}}``, ``{{inputs.key}}`` or through its
    trigger step; a person's answer to an input wait as
    ``{{<response_variable>.<field>}}``; a step output copying either as its own key.
    """
    steps = [step for step in workflow.get("steps") or [] if isinstance(step, dict)]
    triggers = [str(step["id"]) for step in steps if (step.get("type") or step.get("kind")) == "trigger" and step.get("id")]
    refs: dict[str, Any] = {}
    for row in _run_input_rows(workflow):
        key = str(row.get("key") or row.get("name") or "").strip()
        found = read(row.get("schema"))
        if key and found:
            for prefix in ("", "inputs.", "input.", "trigger.", *(f"{step_id}." for step_id in triggers)):
                add(refs, f"{prefix}{key}", found)
    for step in steps:
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        if (step.get("type") or step.get("kind")) != "wait" or config.get("wait_type") != "input":
            continue
        schema = config.get("input_schema") if isinstance(config.get("input_schema"), dict) else {}
        answer = str(config.get("response_variable") or "inputs").strip() or "inputs"
        for name, spec in (schema.get("properties") or {}).items() if isinstance(schema.get("properties"), dict) else ():
            if found := read(spec):
                for prefix in dict.fromkeys((answer, str(step.get("id") or answer))):
                    add(refs, f"{prefix}.{name}", found)
    for step in steps:
        outputs = (step.get("config") or {}).get("outputs") if isinstance(step.get("config"), dict) else None
        for output in outputs if isinstance(outputs, list) else []:
            value = output.get("value") if isinstance(output, dict) else None
            match = _WHOLE_REFERENCE.fullmatch(value.strip()) if isinstance(value, str) else None
            if match and match.group(1) in refs and output.get("key"):
                add(refs, str(output["key"]), refs[match.group(1)])
    return refs


def _workflow_choices(workflow: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Each run value of a Workflow that holds a fixed choice (an ``enum``), by every reference to it."""
    return _workflow_value_refs(workflow, _schema_choices, _add_choices)


def _picked_project_type(schema: Any) -> str | None:
    from packages.core.services.workflow_project_select import project_select

    picker = project_select(schema)
    return picker.project_type if picker else None


def project_record_fields(workflows: list[dict[str, Any]]) -> dict[str, dict[tuple[str, ...], str]]:
    """Per project type, the state fields that hold Workspace record ids, with the type of record they name.

    A field holds them when a ``workflow_project`` step's ``state_schema``
    declares it a ``workflow_project_select`` picker, or a step writes it from a
    run input or an input wait field that is one (the dishes a promotion picked).
    """
    found: dict[str, dict[tuple[str, ...], str]] = {}
    for workflow in workflows:
        refs = _workflow_value_refs(workflow, _picked_project_type, lambda into, key, value: into.setdefault(key, value))
        for step in workflow.get("steps") or []:
            if not isinstance(step, dict) or (step.get("type") or step.get("kind")) != "workflow_project":
                continue
            config = step.get("config") if isinstance(step.get("config"), dict) else {}
            project_type = str(config.get("project_type") or "").strip()
            if not project_type:
                continue
            fields = found.setdefault(project_type, {})
            schema = config.get("state_schema") if isinstance(config.get("state_schema"), dict) else {}
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            for name, spec in properties.items():
                if picked := _picked_project_type(spec):
                    fields.setdefault((name,), picked)
            written = {**(config.get("state") if isinstance(config.get("state"), dict) else {}),
                       **(config.get("patch") if isinstance(config.get("patch"), dict) else {})}
            for name, value in written.items():
                match = _WHOLE_REFERENCE.fullmatch(value.strip()) if isinstance(value, str) else None
                if match and match.group(1) in refs:
                    fields.setdefault((name,), refs[match.group(1)])
    return {project_type: fields for project_type, fields in found.items() if fields}


# A state field that says where a record stands beside its stage: status, availability, menu_status.
_STATUS_NAME = re.compile(r"(?:[a-z0-9]+_)*(?:status|availability)")


def _hand_changed_fields(workflows: list[dict[str, Any]], runnable: dict[str, str]) -> dict[str, dict[str, set[str]]]:
    """Per project type, the Flows a person runs to change one record, with the state fields each patches.

    Such a Flow patches a record of a type it does not make, chosen by a picker input or a run input
    holding its id (change this dish, take it off). A Flow that makes its record and then works on it
    changes nothing by hand.
    """
    found: dict[str, dict[str, set[str]]] = {}
    for workflow in workflows:
        slug = workflow.get("slug")
        if slug not in runnable:
            continue
        keys = {str(row.get("key") or row.get("name") or "").strip() for row in _run_input_rows(workflow)} - {""}
        picked = {row["selects_project_type"] for row in _workflow_inputs(workflow) if row.get("selects_project_type")}
        made = {row["project_type"] for row in _workflow_projects(workflow) if {"create", "claim"} & set(row["operations"])}
        for step in workflow.get("steps") or []:
            config = step.get("config") if isinstance(step, dict) and isinstance(step.get("config"), dict) else None
            if config is None or (step.get("type") or step.get("kind")) != "workflow_project" \
                    or config.get("operation") != "patch":
                continue
            project_type = str(config.get("project_type") or "").strip()
            target = config.get("project_id")
            match = _WHOLE_REFERENCE.fullmatch(target.strip()) if isinstance(target, str) else None
            by_input = bool(match) and re.sub(r"^(?:inputs?|trigger)\.", "", match.group(1)) in keys
            if not project_type or project_type in made or not (project_type in picked or by_input):
                continue
            patch = config.get("patch") if isinstance(config.get("patch"), dict) else {}
            found.setdefault(project_type, {}).setdefault(str(slug), set()).update(map(str, patch))
    return found


def _schema_paths_with_choices(properties: Any, prefix: tuple[str, ...] = ()
                               ) -> list[tuple[tuple[str, ...], dict[str, str]]]:
    found = []
    for name, spec in properties.items() if isinstance(properties, dict) else ():
        if not isinstance(spec, dict):
            continue
        if choices := _schema_choices(spec):
            found.append(((*prefix, name), choices))
        elif isinstance(spec.get("properties"), dict):
            found += _schema_paths_with_choices(spec["properties"], (*prefix, name))
    return found


def project_field_choices(workflows: list[dict[str, Any]]) -> dict[str, dict[tuple[str, ...], dict[str, str]]]:
    """Per project type, the state fields that hold a fixed choice, with every value they can hold.

    A field holds a fixed choice when a ``workflow_project`` step's
    ``state_schema`` gives it an ``enum``, or a step writes it from a run
    input or an input wait whose schema does. A fixed text another step writes
    into such a field is one of its values too.
    """
    found: dict[str, dict[tuple[str, ...], dict[str, str]]] = {}
    literals: dict[str, dict[tuple[str, ...], set[str]]] = {}
    for workflow in workflows:
        refs = _workflow_choices(workflow)
        for step in workflow.get("steps") or []:
            if not isinstance(step, dict) or (step.get("type") or step.get("kind")) != "workflow_project":
                continue
            config = step.get("config") if isinstance(step.get("config"), dict) else {}
            project_type = str(config.get("project_type") or "").strip()
            if not project_type:
                continue
            fields = found.setdefault(project_type, {})
            schema = config.get("state_schema") if isinstance(config.get("state_schema"), dict) else {}
            for path, choices in _schema_paths_with_choices(schema.get("properties")):
                _add_choices(fields, path, choices)
            written = {**(config.get("state") if isinstance(config.get("state"), dict) else {}),
                       **(config.get("patch") if isinstance(config.get("patch"), dict) else {})}
            for name, value in written.items():
                if not isinstance(value, str):
                    continue
                match = _WHOLE_REFERENCE.fullmatch(value.strip())
                if match and match.group(1) in refs:
                    _add_choices(fields, (name,), refs[match.group(1)])
                elif "{{" not in value and value.strip():
                    literals.setdefault(project_type, {}).setdefault((name,), set()).add(value.strip())
    for project_type, paths in literals.items():
        for path, values in paths.items():
            if path in found.get(project_type, {}):
                _add_choices(found[project_type], path, {value: "" for value in sorted(values)})
    return {project_type: fields for project_type, fields in found.items() if fields}


def result_field_choices(workflows: list[dict[str, Any]]) -> dict[tuple[str, ...], dict[str, str]]:
    """The run variables of ``workflows`` that hold a fixed choice, by their path in a run's variables."""
    found: dict[tuple[str, ...], dict[str, str]] = {}
    for workflow in workflows:
        for ref, choices in _workflow_choices(workflow).items():
            _add_choices(found, tuple(ref.split(".")), choices)
    return found


def unlabeled_choice_issue(where: str, configured: dict[str, Any], choices: dict[str, str] | None,
                           *, stage_labels: dict[str, str] | None = None) -> str | None:
    """Why a block field would show people a fixed choice's stored value, if it would.

    A field shown as text or a status reads each value through its
    ``labels``; a status field also through the block's ``stage_labels``.
    """
    shown_as = configured.get("format", "text")
    if not choices or len(choices) > _MAX_FIELD_LABELS or shown_as not in {"text", "status"}:
        return None
    named = {**((stage_labels or {}) if shown_as == "status" else {}), **(configured.get("labels") or {})}
    missing = [value for value in choices if value not in named]
    if not missing:
        return None
    path = ".".join(configured["field"])
    suggested = {value: choices[value] or value.replace("_", " ").replace("-", " ").strip().capitalize()
                 for value in missing[:8]}
    status_fix = ', or format "status" with the block\'s stage_labels naming them' if stage_labels is not None else ""
    return (f"{where} shows its {configured.get('label') or path} field ({path}) as it is stored, but {path} holds "
            f"fixed choices ({', '.join(choices)}) and the field has no label for {', '.join(missing)}: give the "
            f"field labels for every value, e.g. labels: {json.dumps(suggested, ensure_ascii=False)}{status_fix}")


def project_display_issues(workflows: list[dict[str, Any]]) -> list[str]:
    """Projects a Workflow set makes without saying what a person sees of them.

    Staging 2026-11-08: a weekly post batch read by its whole brief, its status a stage key.
    """
    shown: dict[str, set[str]] = {}
    made: dict[str, list[str]] = {}
    for workflow in workflows:
        for row in _workflow_projects(workflow):
            shown.setdefault(row["project_type"], set()).update(row["display_fields"])
            if {"create", "claim"} & set(row["operations"]):
                made.setdefault(row["project_type"], []).append(str(workflow.get("slug")))
    return [f"workflows.{', '.join(slugs)}: {project_type} never saves display.title and display.status, so the App "
            "names it by its inputs and shows its stage key: have its workflow_project steps save state.display "
            "(title, status, and summary, media, deliverables where it makes them) from the step outputs"
            for project_type, slugs in made.items() if not {"title", "status"} <= shown[project_type]]


def project_structure_issues(workflows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Parent/child project structure a Workflow set gets wrong.

    A Flow that picks a project of one type (a hidden ``workflow_project_select``
    input) and makes a project of another makes children of that parent. The
    App can only list them per parent, and plan them, when the child records
    its ``parent_project_id``, the parent's plan list is written somewhere, and
    both keep a folder for their results.
    """
    projects: dict[str, dict[str, Any]] = {}
    for workflow in workflows:
        for row in _workflow_projects(workflow):
            merged = projects.setdefault(row["project_type"], {**row, "operations": [], "lists": [],
                                                                "written_lists": [], "has_folder": False,
                                                                "child_of_another_project": False})
            merged["operations"] += row["operations"]
            merged["lists"] += row["lists"]
            merged["written_lists"] += row["written_lists"]
            merged["has_folder"] = merged["has_folder"] or row["has_folder"]
            merged["child_of_another_project"] = merged["child_of_another_project"] or row["child_of_another_project"]
    issues: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for workflow in workflows:
        made = {row["project_type"] for row in _workflow_projects(workflow)
                if {"create", "claim"} & set(row["operations"])}
        for parent_type in {row.get("selects_project_type") for row in _workflow_inputs(workflow)} - {None}:
            parent = projects.get(parent_type)
            # Only a project that keeps a list (its plan) is a parent of what is made inside it.
            if parent is None or not parent["lists"]:
                continue
            for child_type in sorted(made - {parent_type}):
                if (parent_type, child_type) in seen:
                    continue
                seen.add((parent_type, child_type))
                child = projects[child_type]
                where = f"workflows.{workflow['slug']}"
                if not child["child_of_another_project"]:
                    issues.append({"severity": "P0", "where": where, "message": (
                        f"{child_type} is made inside a picked {parent_type}, but its create/claim state has no "
                        f"parent_project_id: set state.parent_project_id to the picked {parent_type}'s project_id "
                        "so the App can list each one under its parent.")})
                if not set(parent["lists"]) & set(parent["written_lists"]):
                    issues.append({"severity": "P1", "where": where, "message": (
                        f"{parent_type} declares the plan list {parent['lists']} but no step writes it: set it "
                        "from the planning step's output, or patch it with list_appends/list_upserts keyed by "
                        f"code, so every planned {child_type} shows before it is made.")})
                if not (parent["has_folder"] and child["has_folder"]):
                    issues.append({"severity": "P1", "where": where, "message": (
                        f"give {parent_type} and {child_type} a folder (child folder under "
                        "the parent's state.project_root) so the App can show each one's results.")})
    return issues


def _purpose(source: Any) -> dict[str, str]:
    """What the Workspace is for, in its own words: Draft fields or a Workspace."""
    read = source.get if isinstance(source, dict) else lambda key: getattr(source, key, None)
    values = {"name": read("name"), "kind": read("kind"), "context": read("operating_context"),
              "primary_work": read("primary_work"), "description": read("description")}
    return {key: str(value).strip()[:600] for key, value in values.items() if str(value or "").strip()}


def _service_roles(services: Any) -> list[dict[str, Any]]:
    """The people roles the work names: who owns each service and whether a person reviews it."""
    roles = []
    for row in services or []:
        if isinstance(row, dict) and str(row.get("owner_role") or "").strip():
            roles.append({"role": str(row["owner_role"]).strip(), "service": str(row.get("name") or row.get("key")
                                                                                   or row.get("service_key") or ""),
                          "reviewed_by_a_person": bool(row.get("human_review")) or row.get("autonomy_level") == "assisted"})
    return roles


def _approval_rules(rules: list[dict[str, Any]]) -> list[str]:
    """What the rules say a person must approve, and who."""
    return [str(rule.get("description") or rule.get("rule_key") or "approval required").strip()[:400]
            for rule in rules if rule.get("rule_type") == "approval_required"]


@dataclass
class AppDesignContext:
    """What one Workspace runs, in portable keys, and its current App page.

    It also says who the page is for: the Workspace's purpose, its people and
    the roles its work names, what its projects actually hold and how the page
    is used. A Draft has only the purpose and roles; the rest is live.
    """

    services: list[dict[str, str]] = field(default_factory=list)
    workflows: list[dict[str, Any]] = field(default_factory=list)
    automations: list[dict[str, str]] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    stats: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    approval_rules: bool = False
    app_view: dict[str, Any] | None = None
    # Pictures uploaded to this Workspace: left out of app_view, and they stay on the page.
    uploaded_pictures: list[str] = field(default_factory=list)
    # The Workspace's own cover, which a page shows when it names none.
    workspace_cover: bool = False
    purpose: dict[str, str] = field(default_factory=dict)
    people: list[dict[str, Any]] = field(default_factory=list)
    roles: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    # Open requests waiting on a person, by kind (approval, review, input, ...).
    waiting: dict[str, int] = field(default_factory=dict)
    live_projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    # Reads a candidate on the live Workspace (see ``workspace_app_selfcheck``); a Draft has nothing to read.
    read_probe: Callable[..., Awaitable[dict[str, dict[str, Any]] | None]] | None = field(default=None, repr=False)

    @classmethod
    def from_draft_fields(cls, fields: dict[str, Any]) -> "AppDesignContext":
        rules = [rule for rule in fields.get("rules") or [] if isinstance(rule, dict)]
        current = fields.get("app_view") if isinstance(fields.get("app_view"), dict) else {}
        # A Draft has no Agents yet; the Agent targets a Blueprint's App already
        # names stay usable, and new entries reach an Agent through agent:default.
        agents = sorted({str(target).removeprefix("agent:") for target in (current.get("targets") or {}).values()
                         if str(target).startswith("agent:") and target != "agent:default"})
        return cls(
            purpose=_purpose(fields),
            roles=_service_roles(fields.get("services")),
            approvals=_approval_rules(rules),
            agents=agents,
            services=[{"key": str(row.get("key") or row.get("service_key")), "name": str(row.get("name") or "")}
                      for row in fields.get("services") or []
                      if isinstance(row, dict) and (row.get("key") or row.get("service_key"))],
            workflows=[copy.deepcopy(row) for row in fields.get("workflows") or []
                       if isinstance(row, dict) and row.get("slug")],
            automations=[{"id": str(row["job_id"]), "name": str(row.get("name") or row["job_id"])}
                         for row in fields.get("scheduled_jobs") or [] if isinstance(row, dict) and row.get("job_id")],
            stats=[str(row["key"]) for row in fields.get("stats") or [] if isinstance(row, dict) and row.get("key")],
            goals=[str(row.get("title") or row.get("goal_key")) for row in fields.get("goals") or []
                   if isinstance(row, dict)],
            approval_rules=any(rule.get("rule_type") == "approval_required" for rule in rules),
            app_view=copy.deepcopy(fields["app_view"]) if isinstance(fields.get("app_view"), dict) else None,
        )

    def _workflow(self, slug: str) -> dict[str, Any] | None:
        return next((row for row in self.workflows if row.get("slug") == slug), None)

    def runnable_workflows(self) -> dict[str, str]:
        return {row["slug"]: row["slug"] for row in self.workflows if not row.get("internal")}

    def projects(self) -> dict[str, dict[str, Any]]:
        """Each project type the Workflows keep, merged across them, with the Flows that make it."""
        projects: dict[str, dict[str, Any]] = {}
        for workflow in self.workflows:
            # A Flow that stops for a person's decision or answer keeps its projects waiting on them.
            waits = _waits_on_person(workflow)
            for row in _workflow_projects(workflow):
                merged = projects.setdefault(row["project_type"], {
                    "project_type": row["project_type"], "made_by": [], "fields": {}, "document_fields": [],
                    "list_item_fields": {}, "has_folder": False, "waits_in": [], "display_fields": [], "date_fields": []})
                if {"create", "claim"} & set(row["operations"]):
                    merged["made_by"].append(workflow["slug"])
                    if waits:
                        merged["waits_in"].append(workflow["slug"])
                merged["fields"] = {**row["fields"], **merged["fields"]}
                for path, names in row["list_item_fields"].items():
                    _list_item_fields(merged, path, names)
                merged["document_fields"] += [path for path in row["document_fields"]
                                              if path not in merged["document_fields"]]
                merged["has_folder"] = merged["has_folder"] or row["has_folder"]
                merged["display_fields"] += [name for name in row["display_fields"] if name not in merged["display_fields"]]
                merged["date_fields"] += [name for name in row["date_fields"] if name not in merged["date_fields"]]
        return projects

    def coverage(self, config: dict[str, Any]) -> tuple[list[str], list[str]]:
        """Deliverables the page leaves out, and document ids or folders it shows as text.

        Returns (errors, warnings): text that stands for a document is an
        error; a project with a deliverable (a document or a folder) the page
        does not show is a warning, since a brief may leave it out on purpose.
        """
        projects = self.projects()
        sources = config["data_sources"]
        project_of = {name: projects.get(source.get("project_type")) for name, source in sources.items()
                      if source["type"] in {"workflow_projects", "related_projects"}}
        documents: dict[str, set[tuple[str, ...]]] = {}
        folders: set[str] = set()
        for source in sources.values():
            project = project_of.get(source.get("parent"))
            if project is None:
                continue
            if source["type"] == "project_documents":
                documents.setdefault(project["project_type"], set()).add(tuple(source["document_path"]))
            if source["type"] == "project_files":
                folders.add(project["project_type"])
                if source.get("results_only"):
                    # Each child's results, read from the parent's folder.
                    folders.update(child["project_type"] for name, child in project_of.items()
                                   if child is not None and sources[name].get("parent") == source["parent"])
        errors: list[str] = []
        unnamed: list[str] = []
        frames: list[str] = []
        fixed_inputs = {str(row["key"]) for workflow in self.workflows for row in _workflow_inputs(workflow)
                        if row.get("choices")}
        for name, source in sources.items():
            labels = source.get("label_fields")
            if project_of.get(name) is not None and labels and all(
                    _names_nothing(path[-1], fixed_inputs) for path in labels):
                # Local eval 2026-11-03: every growth report read "daily", a customer case its thread id.
                project = project_of[name]
                readable = [field for field, kind in project["fields"].items()
                            if (kind == "any" or "string" in (kind if isinstance(kind, list) else [kind]))
                            and [field] not in project["document_fields"]
                            and field not in {"history", "project_root"} and not _names_nothing(field, fixed_inputs)]
                problem = (f"source {name!r} names its records by {', '.join('.'.join(path) for path in labels)}, "
                           "which is an id, a status or a fixed option, so a person cannot tell them apart")
                if readable:
                    errors.append(f"{problem}: use {', '.join(readable[:8])} as label_fields")
                else:
                    # The page cannot fix this, and refusing it would cost the Workspace its App.
                    unnamed.append(f"{problem}, and the Workflow saves nothing readable: have the Workflow "
                                   "save a title in the project state")
        by_project_type = project_field_choices(self.workflows)
        by_records = project_record_fields(self.workflows)

        def choices_of(source_name: str) -> dict[tuple[str, ...], dict[str, str]]:
            source = sources.get(source_name) or {}
            if source.get("type") in {"workflow_projects", "related_projects"}:
                return by_project_type.get(source.get("project_type"), {})
            if source.get("type") == "workflow_results":
                slugs = {str(ref).removeprefix("workflow_definition:") for ref in source.get("producer_workflows") or []}
                return result_field_choices([row for row in self.workflows if row.get("slug") in slugs])
            return {}

        for component in config["components"]:
            context = component["context"]
            picked = project_of.get(context.get("source")) if context.get("kind") == "project" else None
            if picked is not None and not sources[context["source"]].get("label_fields") and "title" not in picked["display_fields"]:
                # Staging 2026-11-02: two article cards, both "Client Article".
                named = [name for name, kind in picked["fields"].items()
                         if (kind == "any" or "string" in (kind if isinstance(kind, list) else [kind]))
                         and [name] not in picked["document_fields"] and name not in {"stage", "history", "project_root"}]
                errors.append(
                    f"{component['key']} lets the person pick a {picked['project_type']}, but source "
                    f"{context['source']!r} has no label_fields, so every one reads "
                    f"{picked['project_type'].replace('_', ' ').title()!r}: name the 1–3 fields that tell them "
                    "apart as label_fields (and in fields)"
                    + (f" (they carry {', '.join(named[:8])})" if named else ""))
            # What a hidden view or block holds reaches nobody; a calendar shows the blocks it is made of.
            display = component.get("display") or {}
            calendar = display.get("calendar") or {}
            off_page = ({block["key"] for block in component["blocks"]} if display.get("hidden") else
                        set(display.get("hidden_blocks") or []) - {calendar.get(part) for part in (
                            "block", "rollup_block", "context_block")})
            for block in component["blocks"]:
                source = sources[block["source"]]
                listed = project_of.get(source.get("parent"))
                if (block["type"] == "records" and not block["fields"] and source["type"] == "project_records"
                        and source.get("record_schema") is None and listed is not None):
                    # A plain list item shows only its title and the fields a block names.
                    item_fields = listed["list_item_fields"].get(".".join(source["record_path"]), [])
                    if "title" not in item_fields:
                        errors.append(
                            f"{component['key']}.{block['key']} lists {'.'.join(source['record_path'])} with no "
                            "fields, and its items have no title, so each shows as a bare id: declare the item "
                            f"fields on source {block['source']!r} and show them as block fields"
                            + (f" (the items carry {', '.join(item_fields)})" if item_fields else ""))
                where = f"{component['key']}.{block['key']}"
                if block["key"] not in off_page:
                    # A fixed choice shows as it is stored unless the field names each value.
                    stage_labels = (block.get("stage_labels") or {}) if block["type"] in _STAGE_LABELED_BLOCKS else None
                    choices = choices_of(block["source"])
                    errors += [issue for item in block["fields"] if isinstance(item, dict)
                               if (issue := unlabeled_choice_issue(where, item, choices.get(tuple(item["field"])),
                                                                   stage_labels=stage_labels))]
                    for entry in (block.get("record_display") or {}).get("fields") or []:
                        if issue := unlabeled_choice_issue(where, entry,
                                                           choices_of(entry["source"]).get(tuple(entry["field"]))):
                            errors.append(issue)
                    # A platform the preview has no frame for shows as a plain post: say which, so the page
                    # either fixes the Workflow's names or knows it.
                    preview_fields = (block.get("preview") or {}).get("fields") or {}
                    platform_path = preview_fields.get("platform")
                    unframed = sorted(value for value in (choices.get(tuple(platform_path or ())) or {})
                                      if not preview_platform(value))
                    if unframed:
                        frames.append(f"{where} previews each record on its {'.'.join(platform_path)}, but "
                                      f"{', '.join(unframed)} {'is' if len(unframed) == 1 else 'are'} no platform "
                                      f"frame, so {'it shows' if len(unframed) == 1 else 'they show'} as a plain post: "
                                      f"name {'it' if len(unframed) == 1 else 'them'} as one of {', '.join(PREVIEW_PLATFORMS)}")
                    previewed_project = project_of.get(block["source"]) or project_of.get(
                        sources[block["source"]].get("parent"))
                    if previewed_project is not None:
                        # A post's pictures and clip are on the page once its preview shows them.
                        documents.setdefault(previewed_project["project_type"], set()).update(
                            tuple(preview_fields[role]) for role in ("images", "video", "cover") if role in preview_fields)
                project = project_of.get(block["source"])
                if project is None:
                    continue
                record_fields = by_records.get(project["project_type"], {})
                for item in block["fields"]:
                    path, shown_as = ((item["field"], item["format"]) if isinstance(item, dict) else (item, "text"))
                    picked = record_fields.get(tuple(path))
                    if picked and shown_as != "record":
                        # Staging 2026-11-08: each promotion card read its dishes as a list of ids.
                        errors.append(
                            f"{where} shows {'.'.join(path)} as {shown_as}, but it holds the ids of the {picked} "
                            f"records its Workflow picks: give the field format 'record' (project_type '{picked}') "
                            "so each shows by its name")
                    elif picked and isinstance(item, dict) and item.get("project_type") not in (None, picked):
                        errors.append(f"{where} reads {'.'.join(path)} as {item['project_type']} records, but its "
                                      f"Workflow picks {picked} records there: set project_type '{picked}'")
                    if path in project["document_fields"] and shown_as not in DOCUMENT_FIELD_FORMATS:
                        errors.append(
                            f"{where} shows {'.'.join(path)} as {shown_as}, but it holds a document id: show it "
                            f"with a documents block over a project_documents source (document_path {path}), or "
                            "give the field format 'document'")
                    elif path == ["project_root"]:
                        errors.append(f"{where} shows the folder path project_root as text: show the folder's files "
                                      "with a project_files source and a documents block")
                    if shown_as in DOCUMENT_FIELD_FORMATS:
                        documents.setdefault(project["project_type"], set()).add(tuple(path))
        # Local eval 2026-11-03: the studio's article waited for the owner's approval, but only the
        # per-article view showed it, so the front page had nothing to act on.
        front = {project_of[block["source"]]["project_type"] for component in config["components"]
                 if component["context"].get("kind") == "workspace" for block in component["blocks"]
                 if block["type"] == "progress" and project_of.get(block["source"]) is not None}
        for project_type, project in projects.items():
            if project["waits_in"] and project_type not in front:
                errors.append(
                    f"{', '.join(project['waits_in'])} stops for a person's decision on each {project_type}, but the "
                    f"front page shows no {project_type} waiting: add a progress block over the {project_type} "
                    "projects in the stages where they wait to a workspace-context component, first on the page")
        # The queue block is where a decision shows on the page, whatever else the page lists.
        if config.get("sections") is not None and not any(section["block"] == "queue" for section in config["sections"]):
            waiting = [str(row["slug"]) for row in self.workflows if _waits_on_person(row)]
            if waiting:
                errors.append(f"{', '.join(waiting)} stops for a person's decision, but the page has no queue block "
                              "to show it: keep the queue section")
        warnings: list[str] = [*unnamed, *frames]
        for name, source in sources.items():
            # The Workflow named its records for people; label_fields would show its inputs instead.
            if source.get("label_fields") and "title" in ((project_of.get(name) or {}).get("display_fields") or []):
                warnings.append(f"source {name!r} names its records by "
                                f"{', '.join('.'.join(path) for path in source['label_fields'])}, but its Workflow saves "
                                "display.title: drop label_fields so each reads by its own title")
        shown = {project["project_type"] for project in project_of.values() if project is not None}
        for project_type, project in projects.items():
            if project_type not in shown:
                if project["made_by"] and (project["document_fields"] or project["has_folder"]):
                    warnings.append(
                        f"{project_type} projects ({', '.join(project['made_by'])}) keep deliverables but are not on "
                        "the page: add a data view listing them with their stage, fields, documents and folder, "
                        "unless the brief leaves them out")
                continue
            if project["document_fields"] and not documents.get(project_type):
                warnings.append(
                    f"{project_type} keeps documents in {', '.join('.'.join(path) for path in project['document_fields'])} "
                    "but the page shows none: add a documents block over a project_documents source whose "
                    "document_path names the deliverable")
            if project["has_folder"] and project_type not in folders:
                warnings.append(f"{project_type} keeps a folder the page does not show: add a project_files source "
                                "and a documents block")
        return errors, warnings

    def audience(self) -> dict[str, Any]:
        """Who the page is for and what waits on them: what a guided start proposes from."""
        declared = {project_type: project["fields"] for project_type, project in self.projects().items()}
        live = {}
        for project_type, sample in self.live_projects.items():
            filled = sample.get("filled") or {}
            live[project_type] = {
                **sample,
                # Declared, but no project shown here has a value: a block for it would stay empty.
                "never_filled": sorted(name for name in declared.get(project_type, {}) if not filled.get(name)),
            }
        return {
            "purpose": self.purpose,
            "people": self.people,
            "roles": self.roles,
            "approvals": self.approvals,
            "waiting": self.waiting,
            "live_projects": live,
            "usage": self.usage,
        }

    def summary(self) -> dict[str, Any]:
        """What ``app_read_context`` returns to the sub-agent."""
        from packages.core.services.workspace_app_view import app_module_catalog, normalize_app_view_config

        return {
            **self.audience(),
            "keys": {
                "entries": [*(f"service:{row['key']}" for row in self.services),
                            *(f"workflow:{slug}" for slug in self.runnable_workflows()),
                            *(f"automation:{row['id']}" for row in self.automations),
                            "action:<your own id>"],
                "targets": [*(f"workflow:{slug}" for slug in self.runnable_workflows()),
                            *(f"agent:{slug}" for slug in self.agents), "agent:default",
                            *(f"automation:{row['id']}" for row in self.automations)],
                "producers": [f"workflow_definition:{row['slug']}" for row in self.workflows],
            },
            "services": self.services,
            "workflows": [{
                "slug": row["slug"], "name": row.get("name") or row["slug"],
                **({"description": row["description"]} if row.get("description") else {}),
                "runnable_from_app": not row.get("internal"),
                "inputs": _workflow_inputs(row),
                "projects": _workflow_projects(row),
            } for row in self.workflows],
            # What the page cannot show until the Workflows change; report it, never work around it.
            "workflow_structure_issues": project_structure_issues(self.workflows),
            "automations": self.automations,
            "agents": self.agents,
            "stats": self.stats,
            "goals": self.goals,
            "approval_rules": self.approval_rules,
            "current_app_view": normalize_app_view_config(self.app_view) if self.app_view else None,
            "uploaded_pictures": self.uploaded_pictures,
            "workspace_cover": self.workspace_cover,
            "module_catalog": app_module_catalog(),
        }

    # ── checks ──────────────────────────────────────────────────────────────

    def check(self, value: Any) -> tuple[dict[str, Any] | None, list[str], list[str]]:
        """Check a whole-page candidate. Returns (normalized, errors, warnings)."""
        from packages.core.blueprints.app_view import remap_app_view
        from packages.core.services.workspace_app_view import (
            app_target_kind_and_id,
            app_view_problems,
            normalize_app_view_config,
            validate_app_view_data_contract,
        )

        try:
            config = normalize_app_view_config(value)
        except ValueError as exc:
            return None, app_view_problems(exc), []
        workflows = self.runnable_workflows()
        definitions = {row["slug"]: row["slug"] for row in self.workflows}
        # The page reads well from here on, so everything below reports together.
        errors: list[str] = []
        warnings: list[str] = []
        try:
            remap_app_view(config, workflows=workflows, workflow_definitions=definitions,
                           agents={slug: slug for slug in self.agents},
                           automations={row["id"]: row["id"] for row in self.automations},
                           stat_keys=set(self.stats))
        except ValueError as exc:
            errors.extend(app_view_problems(exc))
        try:
            validate_app_view_data_contract(config["data_sources"], config["components"], catalog={
                "workflow_definitions": definitions, "workflow_bindings": workflows,
                "stat_keys": set(self.stats), "workflows": self.workflows,
            })
        except ValueError as exc:
            errors.extend(app_view_problems(exc))

        services = {row["key"] for row in self.services}
        automations = {row["id"] for row in self.automations}

        def known(key: str) -> bool:
            kind, identity = key.split(":", 1)
            return {"service": identity in services, "workflow": identity in workflows,
                    "automation": identity in automations, "action": key in config["entries"]}[kind]

        used = {config["hero"]} if config["hero"] else set()
        used.update(config["order"], config["hidden"])
        for name in ("labels", "descriptions", "forms", "prompts", "presets", "asks", "icons", "targets"):
            used.update(config.get(name) or {})
        errors.extend(f"{key} is not an entry of this Workspace; add it to entries or use a listed key"
                      for key in sorted(used) if not known(key))
        errors.extend(f"{key} has no target; give it a workflow, agent or automation target"
                      for key in config["entries"] if key not in config["targets"])

        def flow_of(key: str) -> str | None:
            target = config["targets"].get(key)
            if target is None:
                return key.removeprefix("workflow:") if key.startswith("workflow:") else None
            kind, identity = app_target_kind_and_id(key, target)
            return identity if kind == "workflow" else None

        def inputs_of(key: str) -> set[str] | None:
            slug = flow_of(key)
            workflow = self._workflow(slug) if slug else None
            return {row["key"] for row in _workflow_inputs(workflow)} if workflow else None

        for name in ("asks", "presets"):
            for key, fields in config[name].items():
                inputs = inputs_of(key)
                if inputs is None:
                    errors.append(f"{name}[{key}] needs an entry that runs a Workflow")
                    continue
                missing = sorted(set(fields) - inputs)
                if missing:
                    errors.append(f"{name}[{key}] names inputs the Workflow does not take: {', '.join(missing)}")

        card_actions: set[str] = set()
        for component in config["components"]:
            for block in component["blocks"]:
                for option, action in block_card_actions(block):
                    key = action["action"]
                    if option != "add_action":
                        card_actions.add(key)
                    where = f"{component['key']}.{block['key']}.{option}"
                    inputs = inputs_of(key)
                    if inputs is None:
                        errors.append(f"{where} opens {key}, which is not an entry that runs a Workflow")
                        continue
                    if option == "add_action":
                        # A list's Add button opens its entry as the tile does, so the entry may stay a tile too.
                        listed_type = (config["data_sources"].get(block["source"]) or {}).get("project_type")
                        slug = flow_of(key)
                        made = {row["project_type"] for row in _workflow_projects(self._workflow(slug) or {})
                                if {"create", "claim"} & set(row["operations"])} if slug else set()
                        if listed_type not in made:
                            errors.append(f"{where} opens {key}, which adds no {listed_type} records: open the "
                                          f"Workflow that creates the {listed_type} records the block lists")
                        continue
                    wanted = set(action["inputs"]) if option == "plan_action" else {
                        action["input"], *action.get("prefill", {}),
                        *(action[moment] for moment in ("time_input", "start_input", "end_input") if moment in action)}
                    missing = sorted(wanted - inputs)
                    if missing:
                        errors.append(f"{where} fills inputs {key}'s Workflow does not take: {', '.join(missing)}")
                    # One that asks first runs without its form, so the record must fill every required input.
                    slug = flow_of(key)
                    workflow = self._workflow(slug) if slug and action.get("confirm") else None
                    unfilled = sorted(row["key"] for row in _workflow_inputs(workflow) if row.get("required")
                                      and row["key"] not in wanted) if workflow else []
                    if unfilled:
                        errors.append(f"{where} asks first and then runs without its form, but nothing fills "
                                      f"{key}'s required {', '.join(unfilled)}: prefill them from the record or drop confirm")
        warnings.extend(f"{key} is opened from cards but still shows as a tile; add it to hidden"
                        for key in sorted(card_actions) if key not in config["hidden"])
        if config["hero"] and config["hero"] in config["hidden"]:
            errors.append("the hero entry cannot be hidden")
        for component in config["components"]:
            # Two tabs or lists over one source show the same projects twice.
            first: dict[tuple[str, str], str] = {}
            for block in component["blocks"]:
                same = first.setdefault((block["type"], block["source"]), block["key"])
                if same != block["key"]:
                    errors.append(
                        f"{component['key']}.{same} and {component['key']}.{block['key']} are both {block['type']} "
                        f"blocks over source {block['source']!r}, so they list the same projects: give each its own "
                        "source over the same projects limited to the stages it shows (stages: [...]), or drop one")
        shown_as_text, left_out = self.coverage(config)
        errors.extend(shown_as_text)
        warnings.extend(left_out)
        return config, errors, warnings

    def look(self, config: dict[str, Any]) -> tuple[list[str], list[str]]:
        """How a designed page looks: a picture layout needs a picture, and the page carries the brand,
        an icon per entry and a calendar for dated work. Only the designer's own candidates are held
        to it; a page a Blueprint ships keeps the look its author gave it."""
        errors: list[str] = []
        warnings: list[str] = []
        pictured = bool(config.get("cover_image_url") or self.uploaded_pictures or self.workspace_cover)
        wants_picture = [name for name, value in (("layout", config["layout"]), ("visual", config["visual"]))
                         if value in {"cover", "image", "motion"}]
        if wants_picture and not pictured:
            errors.append(f"{' and '.join(f'{name}: {config[name]}' for name in wants_picture)} needs a picture this "
                          "Workspace does not have yet, so the page would open on an empty panel: use layout feature "
                          "(or list) with visual auto; a cover comes once one is made")
        if not (config.get("theme") or {}).get("accent"):
            warnings.append("the page has no theme.accent: take the brand's colour from the brief, its Knowledge or "
                            "its pictures, or pick one that fits the trade, so covers and buttons carry it")
        services = {row["key"] for row in self.services}
        shown = [key for key in [*(f"service:{key}" for key in sorted(services)),
                                 *(f"workflow:{slug}" for slug in self.runnable_workflows()), *config["entries"]]
                 if key not in config["hidden"]]
        bare = [key for key in shown if key not in (config.get("icons") or {})]
        # Left as a warning, two designers in three submitted without icons (local, 2026-11-08).
        if len(shown) > 1 and bare:
            errors.append(f"{', '.join(bare)} {'has' if len(bare) == 1 else 'have'} no icon, so similar names get the "
                          "same glyph: give each shown entry its own from entry_icons")
        # A dated project shown only as a list: a calendar reads it better.
        sources = config["data_sources"]
        on_calendar = {sources[block["source"]].get("project_type")
                       for component in config["components"]
                       for block in component["blocks"]
                       if block["key"] == ((component.get("display") or {}).get("calendar") or {}).get("block")}
        listed = {sources[block["source"]].get("project_type")
                  for component in config["components"] for block in component["blocks"]
                  if sources.get(block["source"], {}).get("type") in {"workflow_projects", "related_projects"}}
        for project_type, project in sorted(self.projects().items()):
            if project["date_fields"] and project_type in listed and project_type not in on_calendar:
                warnings.append(
                    f"{project_type} is dated ({', '.join(project['date_fields'])}) but only listed: show it on a "
                    "calendar, with app_pattern_content_calendar when a period project holds it, otherwise a "
                    "workspace view whose display.calendar is a progress block over its workflow_projects ordered "
                    "by that date")
        # A social post shown only as a list of fields: the person approves words they cannot see as a post.
        previewed = {sources.get(sources[block["source"]].get("parent"), {}).get("project_type")
                     if sources[block["source"]]["type"] == "project_records" else sources[block["source"]].get("project_type")
                     for component in config["components"] for block in component["blocks"] if block.get("preview")}
        for project_type, project in sorted(self.projects().items()):
            words = [name for name in project["fields"] if name in _POST_TEXT_FIELDS]
            if words and "platform" in project["fields"] and project_type in listed and project_type not in previewed:
                warnings.append(
                    f"{project_type} is a social post ({', '.join(['platform', *words])}) but only listed: show it as "
                    "it will look with a block's preview {fields: {platform, caption, images or video, hashtags}}")
        # Dated amounts shown only as rows: a person cannot see where they are going without adding them up.
        charted = {sources[block["source"]].get("project_type") for component in config["components"]
                   for block in component["blocks"] if block["type"] == "chart"}
        for project_type, project in sorted(self.projects().items()):
            numbers = [name for name, kind in project["fields"].items()
                       if {"number", "integer"} & set(kind if isinstance(kind, list) else [kind])]
            if numbers and project["date_fields"] and project_type in listed and project_type not in charted:
                warnings.append(
                    f"{project_type} keeps amounts ({', '.join(numbers[:4])}) by date ({project['date_fields'][0]}) but "
                    "only lists them: a chart block shows the trend, e.g. chart {kind: line, x: {field: "
                    f"[{project['date_fields'][0]}], bucket: month}}, value: {{agg: sum, field: [{numbers[0]}]}}}}, or "
                    "add totals to its table")
        errors.extend(self._unreachable_record_changes(config, shown, listed))
        return errors, warnings

    def _unreachable_record_changes(self, config: dict[str, Any], shown: list[str], listed: set[Any]) -> list[str]:
        """Records the page lists that people add or change by hand, with no way on the page to do it.

        Staging 2026-11-09: a restaurant menu kept by hand got two chat entries, and its Add and Edit Flows
        were hidden, so the page listed dishes nobody could add or change from it. Once they showed, Add dish
        was still only an entry: the menu itself had no Add button, and a menu table without its status
        column showed dishes taken off the menu like the ones on it."""
        from packages.core.services.workspace_app_view import app_target_kind_and_id

        runnable = self.runnable_workflows()
        reachable = {key.removeprefix("workflow:") for key in shown if key.startswith("workflow:")}
        reachable |= {identity for key, target in (config.get("targets") or {}).items() if key not in config["hidden"]
                      for kind, identity in [app_target_kind_and_id(key, target)] if kind == "workflow"}
        sources = config["data_sources"]
        opened: dict[Any, set[str]] = {}
        added: dict[Any, set[str]] = {}
        lists_missing_add: dict[Any, list[str]] = {}
        for component in config["components"]:
            for block in component["blocks"]:
                source = sources.get(block["source"], {})
                project_type = source.get("project_type")
                if (block["type"] == "progress" and source.get("type") == "workflow_projects"
                        and not block.get("add_action")):
                    lists_missing_add.setdefault(project_type, []).append(f"{component['key']}.{block['key']}")
                for option, action in block_card_actions(block):
                    key = action["action"]
                    target = (config.get("targets") or {}).get(key)
                    slug = (app_target_kind_and_id(key, target)[1] if target is not None
                            else key.removeprefix("workflow:") if key.startswith("workflow:") else None)
                    if slug:
                        (added if option == "add_action" else opened).setdefault(project_type, set()).add(slug)
        changed_by_hand = _hand_changed_fields(self.workflows, runnable)
        errors = []
        for workflow in self.workflows:
            slug = workflow["slug"]
            if slug not in runnable:
                continue
            inputs = _workflow_inputs(workflow)
            asked = [row for row in inputs if not row["hidden"] and not row.get("selects_project_type")]
            for row in _workflow_projects(workflow):
                project_type = row["project_type"]
                if project_type not in listed:
                    continue
                picks_one = any(item.get("selects_project_type") == project_type for item in inputs)
                started = slug in reachable or any(slug in slugs for slugs in [*opened.values(), *added.values()])
                adds = {"create", "claim"} & set(row["operations"]) and asked and not picks_one
                # Records people both add and change by hand (a menu, a catalogue) are added from their own list.
                if adds and project_type in changed_by_hand and project_type in lists_missing_add:
                    for block_name in lists_missing_add.pop(project_type):
                        errors.append(
                            f"the page lists {project_type} records, which people add with workflow:{slug} and change by "
                            f"hand, but the list of them has no Add button: give {block_name} "
                            f'add_action: {{"action": "workflow:{slug}", "label": "Add a {project_type.replace("_", " ")}"}} '
                            "in the person's words (it may stay an entry too)")
                elif adds and not started:
                    errors.append(
                        f"the page lists {project_type} records, which people add with workflow:{slug}, but nothing on "
                        f"the page starts it: show it as an entry (the hero when adding is the main job), not hidden")
                elif "patch" in row["operations"] and picks_one and slug not in reachable \
                        and slug not in opened.get(project_type, set()):
                    errors.append(
                        f"workflow:{slug} changes one {project_type} the person picks, but no card that lists them opens "
                        f"it: put it on those cards as item_action or in item_actions (confirm when it takes one off)")
        errors += self._hidden_hand_statuses(config, changed_by_hand)
        return errors

    def _hidden_hand_statuses(self, config: dict[str, Any], changed_by_hand: dict[str, dict[str, set[str]]]) -> list[str]:
        """Lists that leave out a status people change by hand, so a record taken off reads like one in use.

        The stage always shows on a progress block; a status field the Workflows keep beside it does not."""
        sources = config["data_sources"]
        choices = project_field_choices(self.workflows)
        errors = []
        for component in config["components"]:
            for block in component["blocks"]:
                source = sources.get(block["source"], {})
                project_type = source.get("project_type")
                if block["type"] != "progress" or source.get("type") not in {"workflow_projects", "related_projects"}:
                    continue
                shown = {tuple(item["field"]): item.get("format") for item in block["fields"] if isinstance(item, dict)}
                changers: dict[str, list[str]] = {}
                for slug, fields in sorted((changed_by_hand.get(project_type) or {}).items()):
                    for name in fields:
                        if _STATUS_NAME.fullmatch(name) and shown.get((name,)) != "status":
                            changers.setdefault(name, []).append(f"workflow:{slug}")
                for name, slugs in sorted(changers.items()):
                    values = (choices.get(project_type) or {}).get((name,)) or {}
                    labels = {value: label or value.replace("_", " ").replace("-", " ").strip().capitalize()
                              for value, label in list(values.items())[:_MAX_FIELD_LABELS]}
                    suggested = {"field": [name], "label": "Status", "format": "status",
                                 **({"labels": labels} if labels else {})}
                    read = [name] in (source.get("fields") or [])
                    errors.append(
                        f"{component['key']}.{block['key']} lists {project_type} records whose {name} people change "
                        f"with {', '.join(slugs)}, but "
                        f"{'shows it as ' + str(shown[(name,)]) if (name,) in shown else 'leaves it out'}, so one taken "
                        f"off reads like one in use: give the block the field {json.dumps(suggested, ensure_ascii=False)} "
                        f"with a label for each value in the person's words"
                        + ("" if read else f", and add {name} to source {block['source']!r}'s fields"))
        return errors


# ── Project-tree pattern (recipe 4) ─────────────────────────────────────────

def project_tree_pattern(context: AppDesignContext, args: dict[str, Any]) -> dict[str, Any]:
    """The built-in series layout for one of this Workspace's parent/child Flows.

    Returns ``{"ok": True, "fragment": {...}}`` with the data view and the
    entries its cards open, or ``{"ok": False, "errors": [...]}``.
    """
    from packages.core.blueprints.builtin_app_views import project_tree_view

    # One Flow may make both parents and children, or each its own; Make and
    # Remake run whichever Flows take their inputs.
    slug = str(args.get("workflow") or "")
    slugs = {role: str(args.get(f"{role}_workflow") or slug) for role in ("parent", "make", "remake")}
    flows = {name: context._workflow(name) for name in {slug, *slugs.values()}}
    missing = sorted(name for name, flow in flows.items() if flow is None)
    if missing:
        return {"ok": False, "errors": [f"unknown workflow {name!r}" for name in missing]}

    def projects_of(name: str) -> dict[str, dict[str, Any]]:
        return {row["project_type"]: row for row in _workflow_projects(flows[name])}

    def inputs_of(name: str) -> set[str]:
        return {row["key"] for row in _workflow_inputs(flows[name])}

    parent_type, child_type = str(args.get("parent_project_type") or ""), str(args.get("child_project_type") or "")
    errors: list[str] = []
    parent, child = projects_of(slugs["parent"]).get(parent_type), projects_of(slug).get(child_type)
    if parent is None:
        errors.append(f"{slugs['parent']} keeps projects {sorted(projects_of(slugs['parent']))}; name the parent type "
                      "it creates, or set parent_workflow")
    if child is None:
        errors.append(f"{slug} keeps projects {sorted(projects_of(slug))}; name the child type it makes")
    plan_path = str(args.get("plan_path") or "")
    if parent is not None and plan_path not in parent["lists"]:
        errors.append(f"plan_path must be a list the parent declares: {parent['lists']}")

    def labeled(name: str, project: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
        rows = tuple((str(row.get("field")), str(row.get("label") or row.get("field")))
                     for row in args.get(name) or [] if isinstance(row, dict))
        if project is not None:
            errors.extend(f"{name}: {key} is not declared by {project['project_type']}"
                          for key, _ in rows if key not in project["fields"])
        return rows

    about, child_fields = labeled("about", parent), labeled("child_fields", child)
    stages = {str(key): str(label) for key, label in (args.get("stages") or {}).items()}
    done = str(args.get("done_stage") or "")
    if child is not None and (not stages or set(stages) - set(child["stages"])):
        errors.append(f"stages must label the child's stages {child['stages']}")
    if done not in stages:
        errors.append("done_stage must be one of stages")
    references = tuple((str(row.get("kind")), str(row.get("label") or row.get("kind")))
                       for row in args.get("references") or [] if isinstance(row, dict))
    errors.extend(f"references kind must be voices or images, not {kind!r}"
                  for kind, _ in references if kind not in {"voices", "images"})
    prefix = str(args.get("entry_prefix") or child_type.replace("_", "-") or "item")
    remake = args.get("remake") if isinstance(args.get("remake"), dict) else None
    make = args.get("make") if isinstance(args.get("make"), dict) else None
    if remake and remake.get("input") not in inputs_of(slugs["remake"]):
        errors.append(f"remake.input must be one of {slugs['remake']}'s inputs {sorted(inputs_of(slugs['remake']))}")
    make_inputs = {str(key): str(value) for key, value in ((make or {}).get("inputs") or {}).items()}
    if make and (not make_inputs or set(make_inputs) - inputs_of(slugs["make"])
                 or set(make_inputs.values()) - {"parent", "code"}):
        errors.append(f"make.inputs maps {slugs['make']}'s inputs {sorted(inputs_of(slugs['make']))} "
                      "to 'parent' or 'code'")
    if errors:
        return {"ok": False, "errors": errors}

    remake_key, make_key = f"action:{prefix}-remake", f"action:{prefix}-make"
    view = project_tree_view(
        slugs["parent"], child_flow=slug, parent=(parent_type, str(args.get("parent_label") or "Projects")),
        child=(child_type, str(args.get("child_label") or "Items")),
        about=about, child_fields=child_fields, stages=stages, done=done, references=references,
        remake=(remake_key, remake["input"], str(remake.get("label") or "Remake")) if remake else None,
        make=(make_key, str(make.get("label") or "Make this one"), make_inputs) if make else None,
        plan_path=plan_path,
    )
    entries: dict[str, Any] = {}
    asks: dict[str, list[str]] = {}
    if remake:
        entries[remake_key] = {"title": str(remake.get("title") or remake.get("label") or "Remake")}
        asks[remake_key] = [remake["input"]]
    if make:
        entries[make_key] = {"title": str(make.get("title") or make.get("label") or "Make this one")}
        asks[make_key] = list(make_inputs)
    return {"ok": True, "fragment": {
        **view,
        "entries": entries,
        "targets": {key: f"workflow:{slugs['remake' if key == remake_key else 'make']}" for key in entries},
        "asks": asks,
        "hidden": list(entries),
    }, "merge": "Add these keys to your page: merge entries, targets, asks and hidden with your own; "
                "data_sources and components are the whole tree view."}


# ── Content-calendar pattern (recipe 7) ─────────────────────────────────────

def content_calendar_pattern(context: AppDesignContext, args: dict[str, Any]) -> dict[str, Any]:
    """The built-in content calendar for a period project and its dated items.

    Returns ``{"ok": True, "fragment": {...}}`` with the data view, or
    ``{"ok": False, "errors": [...]}``.
    """
    from packages.core.blueprints.builtin_app_views import content_calendar_view

    slug = str(args.get("workflow") or "")
    period_slug = str(args.get("period_workflow") or slug)
    action = args.get("action") if isinstance(args.get("action"), dict) else None
    action_slug = str((action or {}).get("workflow") or "")
    flows = {name: context._workflow(name) for name in {slug, period_slug, *([action_slug] if action else [])}}
    missing = sorted(name for name, flow in flows.items() if flow is None)
    if missing:
        return {"ok": False, "errors": [f"unknown workflow {name!r}" for name in missing]}

    def projects_of(name: str) -> dict[str, dict[str, Any]]:
        return {row["project_type"]: row for row in _workflow_projects(flows[name])}

    period_type, item_type = str(args.get("period_project_type") or ""), str(args.get("item_project_type") or "")
    period, item = projects_of(period_slug).get(period_type), projects_of(slug).get(item_type)
    errors: list[str] = []
    if period is None:
        errors.append(f"{period_slug} keeps projects {sorted(projects_of(period_slug))}; name the period type it creates, "
                      "or set period_workflow")
    if item is None:
        errors.append(f"{slug} keeps projects {sorted(projects_of(slug))}; name the dated item type it makes")

    def declared(project: dict[str, Any] | None, field: str, what: str) -> None:
        if project is not None and field not in project["fields"]:
            errors.append(f"{what}: {field} is not declared by {project['project_type']}")

    parent_field, date_field = str(args.get("parent_field") or ""), str(args.get("date_field") or "")
    declared(item, parent_field, "parent_field")
    declared(item, date_field, "date_field")

    def labeled(name: str, project: dict[str, Any] | None) -> list[dict[str, Any]]:
        rows = [row for row in args.get(name) or [] if isinstance(row, dict)]
        for row in rows:
            declared(project, str(row.get("field")), name)
        return [{"label": str(row.get("label") or row.get("field")), "field": [str(row.get("field"))],
                 "format": str(row.get("format") or "text"), "hide_when_empty": True} for row in rows]

    item_fields, period_fields = labeled("item_fields", item), labeled("period_fields", period)
    errors.extend(f"item_fields format must be text, date, link or document, not {row['format']!r}"
                  for row in item_fields if row["format"] not in {"text", "date", "link", "document"})
    plan_field = str(args.get("plan_document_field") or "")
    if plan_field:
        declared(period, plan_field, "plan_document_field")
    stages = {str(key): str(label) for key, label in (args.get("stages") or {}).items()}
    done = str(args.get("done_stage") or "")
    attention = [str(stage) for stage in args.get("attention_stages") or []]
    if item is not None and (not stages or set(stages) - set(item["stages"])):
        errors.append(f"stages must label the item's stages {item['stages']}")
    if done not in stages:
        errors.append("done_stage must be one of stages")
    if set(attention) - set(stages):
        errors.append("attention_stages must be among stages")
    if action:
        inputs = {row["key"] for row in _workflow_inputs(flows[action_slug])}
        if action.get("input") not in inputs:
            errors.append(f"action.input must be one of {action_slug}'s inputs {sorted(inputs)}")
        if set(action.get("stages") or []) - set(stages):
            errors.append("action.stages must be among stages")
    if errors:
        return {"ok": False, "errors": errors}

    item_label = str(args.get("item_label") or "Items")
    period_label = str(args.get("period_label") or "Periods")
    view = content_calendar_view(
        key="content_calendar", title=str(args.get("title") or period_label), period_label=period_label,
        collection_title=str(args.get("collection_title") or period_label),
        period=("periods", {"type": "workflow_projects", "project_type": period_type,
                            "producer_workflows": [f"workflow_definition:{period_slug}"],
                            "fields": [["title"], *(row["field"] for row in period_fields),
                                       *([[plan_field]] if plan_field else [])],
                            "label_fields": [["title"]]}),
        slots=("items", {"type": "related_projects", "project_type": item_type, "parent": "periods",
                         "parent_state_path": [parent_field], "producer_workflows": [f"workflow_definition:{slug}"],
                         "fields": [["title"], [date_field], *(row["field"] for row in item_fields)],
                         "label_fields": [["title"]], "order": {"field": [date_field], "type": "datetime", "direction": "asc"}}),
        stage_labels=stages, attention_stages=attention,
        rollup={"key": "progress", "title": "Progress", "done_label": stages[done], "done_stages": [done],
                "working_stages": [stage for stage in stages if stage != done]},
        documents=({"key": "plan", "title": str(args.get("plan_document_label") or "Plan"), "source": "plan_documents"},)
        if plan_field else (),
        calendar={"key": "calendar", "title": item_label, "fields": [
            {"label": str(args.get("date_label") or "Date"), "field": [date_field], "format": "date", "hide_when_empty": False},
            *item_fields],
            **({"item_action": {"action": f"workflow:{action_slug}", "input": action["input"],
                                "label": str(action.get("label") or "Make this one"),
                                **({"stages": list(action["stages"])} if action.get("stages") else {})}} if action else {})},
        summary={"key": "about", "title": "Details", "fields": period_fields},
        group_titles={"overview": "Progress", "timeline": item_label, "reference": "Details"},
        sources={"plan_documents": {"type": "project_documents", "parent": "periods", "document_path": [plan_field],
                                    "document_role": "plan"}} if plan_field else None,
    )
    return {"ok": True, "fragment": view,
            "merge": "Add data_sources and components to your page; the calendar's action opens the Workflow's own entry."}


# ── Record-collection pattern (recipe 9) ────────────────────────────────────

# How a record field a collection Flow declares (its ``x-record-kind``) shows on the page.
_RECORD_FORMATS = {"text": "text", "long_text": "text", "number": "number", "money": "money", "boolean": "boolean",
                   "date": "date", "picture": "image", "choice": "text"}


def record_collection_pattern(context: AppDesignContext, args: dict[str, Any]) -> dict[str, Any]:
    """The list of a collection people keep by hand, with its Add, Change and Take-off Flows.

    Returns ``{"ok": True, "fragment": {...}}``: one Workspace view listing every record with its fields,
    an Add button in the list's header, a Change button that opens with the record's values, a Take-off button that asks first, filters on its
    fixed choices and stage, and a search on its name. ``{"ok": False, "errors": [...]}`` otherwise.
    """
    slugs = {role: str(args.get(f"{role}_workflow") or "") for role in ("add", "edit", "take_off")}
    flows = {role: context._workflow(slug) if slug else None for role, slug in slugs.items()}
    errors = [f"unknown {role}_workflow {slugs[role]!r}" for role, flow in flows.items() if flow is None]
    if errors:
        return {"ok": False, "errors": errors}
    create = next((step for step in flows["add"]["steps"] if isinstance(step, dict)
                   and (step.get("config") or {}).get("operation") == "create"), None)
    config = (create or {}).get("config") or {}
    properties = ((config.get("state_schema") or {}).get("properties") or {})
    kinds = {name: spec.get("x-record-kind") for name, spec in properties.items()
             if isinstance(spec, dict) and spec.get("x-record-kind")}
    item_type = str(config.get("project_type") or "")
    if not kinds or not item_type:
        return {"ok": False, "errors": [f"{slugs['add']} does not create records the record-collection Workflow "
                                        "pattern made: make them with wf_pattern_record_collection first"]}
    title_field = str(((config.get("state") or {}).get("display") or {}).get("title") or "").strip("{}") or next(iter(kinds))
    currency = str(args.get("currency") or "").upper()
    if "money" in kinds.values() and not re.fullmatch(r"[A-Z]{3}", currency):
        return {"ok": False, "errors": ["currency is required for the money fields: a 3-letter code such as CNY"]}
    edit_inputs = {row["key"] for row in _workflow_inputs(flows["edit"])}
    asked_as = {row["key"]: str(row.get("label") or "") for row in _workflow_inputs(flows["add"])}
    stages = [stage for stage in config.get("allowed_stages") or [] if isinstance(stage, str)]
    live, off = (stages + ["active", "retired"])[:2]
    item_word = str(args.get("item_word") or "item")

    def field(name: str) -> dict[str, Any]:
        spec = properties[name]
        given = args.get("labels") if isinstance(args.get("labels"), dict) else {}
        row = {"field": [name], "label": str(given.get(name) or asked_as.get(name) or name.replace("_", " ").capitalize()),
               "format": "status" if kinds[name] == "choice" and _STATUS_NAME.fullmatch(name) else _RECORD_FORMATS[kinds[name]],
               "hide_when_empty": True}
        if kinds[name] == "money":
            row["currency"] = currency
        if kinds[name] == "choice" and isinstance(spec.get("x-enum-labels"), dict):
            row["labels"] = {str(key): str(value) for key, value in spec["x-enum-labels"].items()}
        return row

    shown = [name for name in kinds if name != title_field]
    choices = [name for name in shown if kinds[name] == "choice"]
    texts = [title_field, *(name for name in shown if kinds[name] in {"text", "long_text"})]
    block = {
        "key": "records", "type": "progress", "source": "records", "limit": 50,
        "fields": [field(name) for name in shown][:12],
        "stage_labels": {live: str(args.get("live_words") or live.capitalize()),
                         off: str(args.get("off_words") or off.capitalize())},
        # Adding one starts from the list itself, as the Add entry's tile does.
        "add_action": {"action": f"workflow:{slugs['add']}", "label": str(args.get("add_label") or f"Add a {item_word}")},
        # The pattern's Change Flow opens a form holding the record's values; one asking for them as
        # run inputs (an older or hand-written one) has them filled from the card instead.
        "item_action": {"action": f"workflow:{slugs['edit']}", "input": "item_id",
                        "label": str(args.get("edit_label") or "Change"),
                        **({"prefill": prefill} if (prefill := {name: [name] for name in kinds if name in edit_inputs}) else {})},
        "item_actions": [{"action": f"workflow:{slugs['take_off']}", "input": "item_id",
                          "label": str(args.get("take_off_label") or "Take off"), "stages": [live],
                          "confirm": str(args.get("take_off_question") or f"Take this {item_word} off? It keeps its details.")}],
        "filters": [*({"field": [name], "label": field(name)["label"]} for name in choices[:2]), {"by": "stage", "label": "Status"}],
        "search": {"fields": [[name] for name in texts[:4]], "label": str(args.get("search_label") or f"Find a {item_word}")},
    }
    title = str(args.get("title") or f"{item_word.capitalize()}s")
    return {"ok": True, "fragment": {
        "data_sources": {"records": {"type": "workflow_projects", "project_type": item_type,
                                     "producer_workflows": [f"workflow_definition:{slug}" for slug in slugs.values()],
                                     # Each record reads by the display.title its Workflow saves.
                                     "fields": [[name] for name in kinds]}},
        "components": [{"key": "records", "type": "data_view", "title": title, "context": {"kind": "workspace"},
                        "blocks": [block]}],
        "hidden": [f"workflow:{slugs['edit']}", f"workflow:{slugs['take_off']}"],
        "suggested_hero": f"workflow:{slugs['add']}",
    }, "merge": "Add data_sources and components to your page and hidden to yours. The list's Add button opens "
                "the Add Flow; make it the hero or an entry too (suggested_hero) when adding is the main job, "
                "and give it a label in the person's words."}


# ── Sub-agent run ───────────────────────────────────────────────────────────

def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


APP_DESIGNER_TOOL_SCHEMAS = [
    _tool("app_read_context",
          "Read what this Workspace runs (Workflows with inputs and projects, services, Agents, Stats, Goals, "
          "Automations), who uses it (purpose, people, roles, what waits on them, live project data, usage), "
          "its current App page, the keys to use and the module catalog.", {}, []),
    _tool("app_pattern_project_tree",
          "Recipe 4: the built-in series layout for a parent project that keeps a plan list and the child projects "
          "made inside it, by one Workflow or several. Returns data_sources, components and the hidden entries its cards open.",
          {
              "workflow": {"type": "string", "description": "Slug of the Workflow that makes the children."},
              "parent_workflow": {"type": "string", "description": "Slug of the Workflow that creates the parents, "
                                  "when another one does."},
              "make_workflow": {"type": "string", "description": "Slug of the Workflow a planned card's Make runs, "
                                "when not the child Workflow."},
              "remake_workflow": {"type": "string", "description": "Slug of the Workflow a made card's Remake runs, "
                                  "when not the child Workflow."},
              "parent_project_type": {"type": "string"}, "child_project_type": {"type": "string"},
              "parent_label": {"type": "string", "description": "e.g. Series"},
              "child_label": {"type": "string", "description": "e.g. Episodes"},
              "plan_path": {"type": "string", "description": "The parent's list field that plans the children."},
              "about": {"type": "array", "items": {"type": "object", "properties": {
                  "field": {"type": "string"}, "label": {"type": "string"}}}},
              "child_fields": {"type": "array", "items": {"type": "object", "properties": {
                  "field": {"type": "string"}, "label": {"type": "string"}}}},
              "stages": {"type": "object", "description": "Child stage -> label, e.g. {in_production: Making, made: Ready}"},
              "done_stage": {"type": "string"},
              "references": {"type": "array", "items": {"type": "object", "properties": {
                  "kind": {"type": "string", "enum": ["voices", "images"]}, "label": {"type": "string"}}}},
              "entry_prefix": {"type": "string", "description": "Id prefix for the card entries, e.g. episode."},
              "remake": {"type": "object", "description": "{input:<input selecting a child>, label:'Remake', title}",
                         "properties": {"input": {"type": "string"}, "label": {"type": "string"},
                                        "title": {"type": "string"}}},
              "make": {"type": "object", "description": "{inputs:{<input>:'parent'|'code'}, label:'Make this episode', title}",
                       "properties": {"inputs": {"type": "object"}, "label": {"type": "string"},
                                      "title": {"type": "string"}}},
          },
          ["workflow", "parent_project_type", "child_project_type", "plan_path", "stages", "done_stage"]),
    _tool("app_pattern_content_calendar",
          "Recipe 7: the built-in content calendar for a period project (a campaign, a week) and the dated items "
          "inside it (what to post each day). Returns data_sources and components: progress and the plan document in "
          "the period's header, every item on a calendar on its date, the period's details.",
          {
              "workflow": {"type": "string", "description": "Slug of the Workflow that makes the dated items."},
              "period_workflow": {"type": "string", "description": "Slug of the Workflow that creates the periods, "
                                  "when another one does."},
              "period_project_type": {"type": "string"}, "item_project_type": {"type": "string"},
              "parent_field": {"type": "string", "description": "The item field holding its period's project id."},
              "date_field": {"type": "string", "description": "The item field holding its date: an ISO time, or a "
                             "plain YYYY-MM-DD for a whole day."},
              "title": {"type": "string"}, "period_label": {"type": "string", "description": "e.g. Week"},
              "collection_title": {"type": "string", "description": "e.g. Weeks"},
              "item_label": {"type": "string", "description": "e.g. Calendar"},
              "date_label": {"type": "string", "description": "e.g. Post on"},
              "item_fields": {"type": "array", "items": {"type": "object", "properties": {
                  "field": {"type": "string"}, "label": {"type": "string"},
                  "format": {"type": "string", "enum": ["text", "date", "link", "document"]}}}},
              "period_fields": {"type": "array", "items": {"type": "object", "properties": {
                  "field": {"type": "string"}, "label": {"type": "string"}}}},
              "plan_document_field": {"type": "string", "description": "The period field holding its plan document id."},
              "plan_document_label": {"type": "string"},
              "stages": {"type": "object", "description": "Item stage -> label, e.g. {idea: Needs a script, published: Published}"},
              "done_stage": {"type": "string"},
              "attention_stages": {"type": "array", "items": {"type": "string"}},
              "action": {"type": "object", "description": "A button on each item: {workflow, input:<input selecting the "
                         "item>, label, stages}", "properties": {
                             "workflow": {"type": "string"}, "input": {"type": "string"}, "label": {"type": "string"},
                             "stages": {"type": "array", "items": {"type": "string"}}}},
          },
          ["workflow", "period_project_type", "item_project_type", "parent_field", "date_field", "stages", "done_stage"]),
    _tool("app_pattern_record_collection",
          "Recipe 10: the list of a collection people keep by hand (a menu, a catalogue), made by the Workflows of "
          "wf_pattern_record_collection. Returns one Workspace view listing every record with its fields in their "
          "formats, an Add button on the list, a Change button that opens with the record's values, a Take-off button that asks first, "
          "filters on its fixed choices and stage, and a search on its name.",
          {"add_workflow": {"type": "string"}, "edit_workflow": {"type": "string"}, "take_off_workflow": {"type": "string"},
           "item_word": {"type": "string", "description": "e.g. dish"}, "title": {"type": "string", "description": "e.g. Menu"},
           "currency": {"type": "string", "description": "3-letter code for money fields, e.g. CNY"},
           "labels": {"type": "object", "description": "Field -> label in the person's words"},
           "live_words": {"type": "string", "description": "e.g. On the menu"}, "off_words": {"type": "string"},
           "add_label": {"type": "string", "description": "The list's Add button, e.g. Add a dish"},
           "edit_label": {"type": "string"}, "take_off_label": {"type": "string"},
           "take_off_question": {"type": "string"}, "search_label": {"type": "string"}},
          ["add_workflow", "edit_workflow", "take_off_workflow", "item_word"]),
    _tool("app_submit_candidate",
          "Submit the whole App page. Runs every check and reports every error it finds at once; a failing "
          "candidate is not kept, so fix them all and submit again. Only the last passing candidate is returned "
          "to the agent that asked.",
          {
              "app_view": {"type": "object", "description": "The whole page: eyebrow, title, description, layout, "
                           "visual, theme, cover_image_url, hero, order, hidden, labels, descriptions, forms, entries, "
                           "targets, prompts, presets, asks, sections, data_sources, components."},
              "recipes": {"type": "array", "items": {"type": "string"},
                          "description": "The recipes this page uses, e.g. ['one-flow studio', 'project tree']."},
          },
          ["app_view", "recipes"]),
]


@dataclass
class AppDesignResult:
    app_view: dict[str, Any] | None
    recipes: list[str]
    warnings: list[str]
    notes: str
    attempts: int
    self_check: dict[str, Any] | None = None

    @property
    def candidate(self) -> dict[str, Any] | None:
        return self.app_view

    @property
    def details(self) -> dict[str, Any]:
        from packages.core.services.workspace_app_selfcheck import compact_self_check

        return {"recipes": self.recipes,
                **({"self_check": compact_self_check(self.self_check)} if self.self_check else {})}


def app_designer_executor(context: AppDesignContext, state: dict[str, Any]):
    """In-memory tools: the run reads its snapshot and keeps its own candidate."""

    async def executor(name: str, args: dict[str, Any]) -> str:
        if name == "app_read_context":
            return json.dumps(context.summary(), ensure_ascii=False)
        if name == "app_pattern_project_tree":
            return json.dumps(project_tree_pattern(context, dict(args or {})), ensure_ascii=False)
        if name == "app_pattern_content_calendar":
            return json.dumps(content_calendar_pattern(context, dict(args or {})), ensure_ascii=False)
        if name == "app_pattern_record_collection":
            return json.dumps(record_collection_pattern(context, dict(args or {})), ensure_ascii=False)
        if name == "app_submit_candidate":
            state["attempts"] = state.get("attempts", 0) + 1
            config, errors, warnings = context.check((args or {}).get("app_view"))
            if config is not None:
                look_errors, look_warnings = context.look(config)
                errors, warnings = [*errors, *look_errors], [*warnings, *look_warnings]
            if errors:
                return json.dumps({"ok": False, "errors": errors}, ensure_ascii=False)
            from packages.core.config import get_settings
            from packages.core.services.workspace_app_selfcheck import self_check_candidate

            report = await self_check_candidate(context, config, warnings)
            # Reads warn until the audit has shown how often they are wrong about a page.
            if report["read_failures"] and get_settings().WORKSPACE_APP_SELF_CHECK_BLOCKS:
                return json.dumps({"ok": False, "errors": report["read_failures"]}, ensure_ascii=False)
            warnings = [*report["read_failures"], *warnings]
            state["candidate"] = config
            state["recipes"] = [str(item) for item in (args or {}).get("recipes") or []]
            state["warnings"] = warnings
            state["self_check"] = report
            return json.dumps({"ok": True, "warnings": warnings, "self_check": report["summary"],
                               "next": ("Fix the warnings and submit again, or reply with your design note." if warnings
                                        else "It passed: reply with your design note, without submitting again.")},
                              ensure_ascii=False)
        return json.dumps({"ok": False, "error": f"unknown tool: {name}"})

    return executor


async def run_app_designer(
    context: AppDesignContext,
    *,
    brief: str,
    feedback: str | None,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None,
    surface: Any,
    sent_back: dict[str, Any] | None = None,
) -> AppDesignResult:
    """Run the App design sub-agent once and return its last passing candidate."""
    result = await run_part_designer(
        APP_DESIGNER, context, brief=brief, feedback=feedback, entity_id=entity_id, user_id=user_id,
        workspace_id=workspace_id, surface=surface, sent_back=sent_back,
    )
    report = result.state.get("self_check")
    notes = "\n\n".join(filter(None, [result.notes, report["summary"] if report else None]))
    return AppDesignResult(app_view=result.candidate, recipes=result.state.get("recipes") or [],
                           warnings=result.warnings, notes=notes, attempts=result.attempts, self_check=report)


# ── Who uses the page (a live Workspace) ────────────────────────────────────

# Latest projects read per type, the window usage is counted over, chat asks shown.
_PROJECT_SAMPLE = 20
_USAGE_DAYS = 90
_RECENT_ASKS = 12


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def project_sample(stage_counts: list[tuple[str, str, int]], states: list[tuple[str, dict[str, Any]]]
                   ) -> dict[str, dict[str, Any]]:
    """What each project type actually holds: projects per stage, and over the latest
    ones, how many fill each field and how long their lists are. Never the values."""
    sample: dict[str, dict[str, Any]] = {}
    for project_type, stage, count in stage_counts:
        row = sample.setdefault(project_type, {"count": 0, "stages": {}, "sampled": 0, "filled": {}, "lists": {}})
        row["count"] += count
        row["stages"][stage] = row["stages"].get(stage, 0) + count
    for project_type, state in states:
        row = sample.get(project_type)
        if row is None:
            continue
        row["sampled"] += 1
        for name, value in (state if isinstance(state, dict) else {}).items():
            if _has_value(value):
                row["filled"][name] = row["filled"].get(name, 0) + 1
            if isinstance(value, list):
                sizes = row["lists"].setdefault(name, {"with_items": 0, "most_items": 0})
                sizes["with_items"] += bool(value)
                sizes["most_items"] = max(sizes["most_items"], len(value))
    return sample


def _person_name(display_name: Any, first_name: Any, last_name: Any) -> str:
    return (str(display_name or "").strip() or " ".join(str(part).strip() for part in (first_name, last_name)
                                                         if str(part or "").strip()) or "Member")


async def _workspace_people(db: Any, workspace: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The Workspace's members with their role, the roles they declared and whether they
    approve; and each member's name by user id."""
    from datetime import datetime, timezone

    from sqlalchemy import or_, select

    from packages.core.humans.service import ROLE_DEFAULT_AUTHORITY
    from packages.core.models.participant import ParticipantProfile
    from packages.core.models.user import User
    from packages.core.models.workspace import WorkspaceStaff

    rows = (await db.execute(select(
        WorkspaceStaff.user_id, WorkspaceStaff.role, User.display_name, User.first_name, User.last_name,
    ).join(User, User.id == WorkspaceStaff.user_id).where(
        WorkspaceStaff.workspace_id == workspace.id, WorkspaceStaff.status == "active",
        or_(WorkspaceStaff.expires_at.is_(None), WorkspaceStaff.expires_at > datetime.now(timezone.utc)),
        User.deleted_at.is_(None),
    ).order_by(WorkspaceStaff.added_at, WorkspaceStaff.id).limit(30))).all()
    profiles: dict[str, Any] = {}
    if rows:
        # The Workspace's own profile wins over the person's default one.
        for profile in (await db.execute(select(ParticipantProfile).where(
            ParticipantProfile.entity_id == workspace.entity_id,
            ParticipantProfile.user_id.in_([row.user_id for row in rows]),
            or_(ParticipantProfile.workspace_id == workspace.id, ParticipantProfile.workspace_id.is_(None)),
        ))).scalars():
            if profile.workspace_id or profile.user_id not in profiles:
                profiles[profile.user_id] = profile
    people, names = [], {}
    for row in rows:
        role = str(row.role or "").strip().lower()
        profile = profiles.get(row.user_id)
        approves = (profile.authority or {}).get("approve_tasks") if profile is not None else None
        names[row.user_id] = _person_name(row.display_name, row.first_name, row.last_name)
        people.append({
            "name": names[row.user_id], "workspace_role": role or "member",
            **({"roles": [str(item) for item in profile.roles]} if profile is not None and profile.roles else {}),
            "approves": approves if isinstance(approves, bool)
            else bool(ROLE_DEFAULT_AUTHORITY.get(role, {}).get("approve_tasks")),
        })
    return people, names


async def _waiting_on_people(db: Any, workspace: Any) -> dict[str, int]:
    """Open decisions by kind (workflow_approval, runtime_input, ...): what the App's queue lists."""
    from packages.core.workspace_chat.pending_actions import pending_actions_by_workspace

    decisions = (await pending_actions_by_workspace(
        db, entity_id=workspace.entity_id, workspace_ids=[workspace.id]))[workspace.id]
    waiting: dict[str, int] = {}
    for decision in decisions.values():
        kind = str((decision.get("action") or {}).get("kind") or "decision")
        waiting[kind] = waiting.get(kind, 0) + 1
    return waiting


async def _live_projects(db: Any, workspace: Any) -> dict[str, dict[str, Any]]:
    from sqlalchemy import func, select

    from packages.core.models.workflow import WorkflowProject

    scope = (WorkflowProject.entity_id == workspace.entity_id, WorkflowProject.workspace_id == workspace.id)
    stage_counts = (await db.execute(select(
        WorkflowProject.project_type, WorkflowProject.current_stage, func.count(),
    ).where(*scope).group_by(WorkflowProject.project_type, WorkflowProject.current_stage))).all()
    latest = select(WorkflowProject.project_type, WorkflowProject.state, func.row_number().over(
        partition_by=WorkflowProject.project_type,
        order_by=(WorkflowProject.updated_at.desc().nulls_last(), WorkflowProject.id.desc()),
    ).label("rank")).where(*scope).subquery()
    states = (await db.execute(select(latest.c.project_type, latest.c.state).where(
        latest.c.rank <= _PROJECT_SAMPLE))).all()
    return project_sample([tuple(row) for row in stage_counts], [tuple(row) for row in states])


async def _page_usage(db: Any, workspace: Any, *, portable: Any, names: dict[str, str]) -> dict[str, Any]:
    """Which App entries people start, and what they keep asking for in Workspace chat."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from packages.core.models.task import Conversation, Message
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_service import WORKSPACE_APP_ACTION_KEY
    from packages.core.services.workspace_app_sections import WORKSPACE_APP_ACTION_META_KEY

    since = datetime.now(timezone.utc) - timedelta(days=_USAGE_DAYS)
    started_from = WorkflowRun.trigger_data[WORKSPACE_APP_ACTION_KEY].as_string()
    asked_from = Message.meta[WORKSPACE_APP_ACTION_META_KEY].as_string()
    starts: dict[str, int] = {}
    for key, count in [*(await db.execute(select(started_from, func.count()).where(
        WorkflowRun.entity_id == workspace.entity_id, WorkflowRun.workspace_id == workspace.id,
        WorkflowRun.created_at >= since, started_from.is_not(None),
    ).group_by(started_from))).all(), *(await db.execute(select(asked_from, func.count()).join(
        Conversation, Conversation.id == Message.conversation_id,
    ).where(
        Conversation.entity_id == workspace.entity_id, Conversation.workspace_id == workspace.id,
        Message.role == "user", Message.created_at >= since, asked_from.is_not(None),
    ).group_by(asked_from))).all()]:
        starts[portable(key)] = starts.get(portable(key), 0) + int(count)
    asks = (await db.execute(select(Message.content, Message.meta).join(
        Conversation, Conversation.id == Message.conversation_id,
    ).where(
        Conversation.entity_id == workspace.entity_id, Conversation.workspace_id == workspace.id,
        Conversation.scope == "workspace_main", Message.role == "user", Message.author_kind == "user",
        Message.created_at >= since, asked_from.is_(None),
    ).order_by(Message.created_at.desc(), Message.id.desc()).limit(_RECENT_ASKS))).all()
    recent = [{**({"who": names[author]} if (author := (meta or {}).get("author_user_id")) in names else {}),
               "text": " ".join(str(content).split())[:200]}
              for content, meta in asks if str(content or "").strip()]
    usage: dict[str, Any] = {}
    if starts:
        usage["entry_starts"] = dict(sorted(starts.items(), key=lambda item: -item[1]))
    if recent:
        usage["recent_chat_asks"] = recent
    return {"days": _USAGE_DAYS, **usage} if usage else {}


async def workspace_app_design_context(db: Any, workspace: Any) -> AppDesignContext:
    """A live Workspace in the portable keys its Blueprint export uses."""
    from sqlalchemy import select

    from packages.core.blueprints.app_view import remap_workspace_app_view, without_local_pictures
    from packages.core.blueprints.exporter import (
        _export_workflows,
        _portable_agent_slug,
        _workspace_workflow_export_rows,
    )
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.models.workspace_stat import WorkspaceStat
    from packages.core.services.workspace_app_view import app_lists_scheduled_job

    exported = await _export_workflows(db, workspace.entity_id, workspace.id, include_skill_dependencies=False)
    model = dict(workspace.operating_model or {})
    current = None
    uploaded_pictures: list[str] = []
    if isinstance(model.get("app_view"), dict):
        page = without_local_pictures(model["app_view"])
        uploaded_pictures = [name for name in ("cover_image_url", "motion_cover_url")
                             if model["app_view"].get(name) and not page.get(name)]
        try:
            current = await remap_workspace_app_view(db, workspace, page, to_runtime=False)
        except ValueError:
            # A page that names something since removed is redesigned from scratch.
            logger.info("App of Workspace %s has stale references; designing without it", workspace.id)
    jobs = (await db.execute(select(ScheduledJob).where(
        ScheduledJob.workspace_id == workspace.id, ScheduledJob.entity_id == workspace.entity_id,
        ScheduledJob.agent_initiative_id.is_(None),
    ))).scalars().all()
    agents = (await db.execute(
        select(Agent).join(AgentSubscription, AgentSubscription.agent_id == Agent.id).where(
            AgentSubscription.workspace_id == workspace.id, AgentSubscription.entity_id == workspace.entity_id,
            AgentSubscription.status == "active",
        )
    )).scalars().all()
    stats = (await db.execute(select(WorkspaceStat.key).where(
        WorkspaceStat.workspace_id == workspace.id, WorkspaceStat.entity_id == workspace.entity_id,
        WorkspaceStat.status == "active",
    ))).scalars().all()
    rules = [rule for rule in model.get("rules") or [] if isinstance(rule, dict)]
    slug_of = {binding.id: slug for _, binding, slug, _ in
               await _workspace_workflow_export_rows(db, workspace.entity_id, workspace.id) if binding is not None}

    def portable(key: Any) -> str:
        kind, _, identity = str(key).partition(":")
        if kind == "workflow":
            return f"workflow:{slug_of.get(identity, identity)}"
        return f"automation:{identity.removesuffix(f'-{workspace.id[-8:]}')}" if kind == "automation" else str(key)

    people, names = await _workspace_people(db, workspace)
    return AppDesignContext(
        purpose=_purpose(workspace),
        people=people,
        roles=_service_roles(model.get("services")),
        approvals=_approval_rules(rules),
        waiting=await _waiting_on_people(db, workspace),
        live_projects=await _live_projects(db, workspace),
        usage=await _page_usage(db, workspace, portable=portable, names=names),
        services=[{"key": str(row.get("key") or row.get("service_key")), "name": str(row.get("name") or "")}
                  for row in model.get("services") or [] if isinstance(row, dict) and (row.get("key") or row.get("service_key"))],
        workflows=list(exported.payloads),
        automations=[{"id": job.job_id.removesuffix(f"-{workspace.id[-8:]}"), "name": job.name or job.job_id}
                     for job in jobs if app_lists_scheduled_job(job)],
        agents=sorted({_portable_agent_slug(agent) for agent in agents}),
        stats=sorted(set(stats)),
        goals=[str(row.get("title") or row.get("goal_key")) for row in model.get("goals") or [] if isinstance(row, dict)],
        approval_rules=any(rule.get("rule_type") == "approval_required" for rule in rules),
        app_view=current,
        uploaded_pictures=uploaded_pictures,
        workspace_cover=bool(workspace.cover_image_url),
    )


async def _sent_back_design(db: Any, workspace: Any, draft_id: str) -> dict[str, Any] | None:
    """The page an open design draft of this Workspace stages, in portable keys."""
    from packages.core.blueprints.app_view import remap_workspace_app_view, without_local_pictures
    from packages.core.constants.workspace_operations import WorkspaceOperationDraftStatus
    from packages.core.models.workspace import WorkspaceOperationDraft

    draft = await db.get(WorkspaceOperationDraft, draft_id)
    if (draft is None or draft.workspace_id != workspace.id or draft.entity_id != workspace.entity_id
            or draft.status != WorkspaceOperationDraftStatus.OPEN.value):
        return None
    page = ((draft.current_state or {}).get("operating_model") or {}).get("app_view")
    if not isinstance(page, dict):
        return None
    try:
        return {"value": await remap_workspace_app_view(db, workspace, without_local_pictures(page), to_runtime=False)}
    except ValueError:
        return None


async def design_workspace_app_view(
    db: Any,
    *,
    workspace_id: str,
    entity_id: str,
    user_id: str | None,
    brief: str,
    feedback: str | None = None,
    replaces_draft_id: str | None = None,
    audience: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Run the App designer on a live Workspace and stage its page for review.

    The candidate becomes an ordinary ``app_view.update`` operation draft: the
    agent that asked accepts it through that draft's review (the person
    approves it as any page change) or discards it. Nothing changes the live
    App until then. ``audience`` names who the page is for and what they act
    on first, as the person chose it (see ``workspace_app_audience``). In a
    chat (``conversation_id``) the replaced draft's review cards close with it.
    """
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.blueprints.app_view import remap_workspace_app_view
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_app_selfcheck import (
        attach_app_self_check,
        candidate_read_probe,
        compact_self_check,
    )
    from packages.core.services.workspace_operation_service import (
        create_operation_draft,
        discard_operation_draft,
        draft_to_dict,
        get_operation_draft,
        supersede_task_draft,
    )

    brief = (brief or "").strip()
    if not brief:
        return {"ok": False, "error": "brief is required: say what the person wants to do from the App"}
    if (audience or "").strip():
        brief += f"\n\nWho the page is for, and what they act on first: {audience.strip()}"
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None or workspace.entity_id != entity_id:
        return {"ok": False, "error": "workspace not found"}
    context = await workspace_app_design_context(db, workspace)
    context.read_probe = candidate_read_probe(workspace_id=workspace_id, entity_id=entity_id, user_id=user_id)
    sent_back = (await _sent_back_design(db, workspace, replaces_draft_id)
                 if replaces_draft_id and feedback else None)
    # No transaction stays open while the sub-agent works.
    await db.commit()
    result = await run_app_designer(
        context, brief=brief, feedback=feedback, entity_id=entity_id, user_id=user_id,
        workspace_id=workspace_id, surface=ChatSurface.WORKSPACE_CHAT, sent_back=sent_back,
    )
    if result.app_view is None:
        return {"ok": False, "error": "the App designer did not produce a page that passes its checks",
                "design_notes": result.notes, "attempts": result.attempts}
    workspace = await db.get(Workspace, workspace_id)
    runtime_view = await remap_workspace_app_view(db, workspace, result.app_view, to_runtime=True)
    update = app_view_replacement((workspace.operating_model or {}).get("app_view"), runtime_view)
    if context.app_view is None and not runtime_view.get("theme"):
        # Designed from scratch: it never saw the page's theme, so it did not drop it on purpose.
        update.pop("theme")
    replaced = await get_operation_draft(db, replaces_draft_id, entity_id, workspace_id) if replaces_draft_id else None
    if replaced is not None and conversation_id:
        await supersede_task_draft(db, replaced, conversation_id=conversation_id, user_id=user_id)
    elif replaced is not None:
        await discard_operation_draft(db, replaced.id, entity_id, workspace_id, user_id=user_id)
    draft = await create_operation_draft(
        db, workspace_id, entity_id, user_id=user_id, source_event_id="workspace_app_designer",
        initial_patches=[{"op": "app_view.update", "payload": {"app_view": update}}],
        origin_conversation_id=conversation_id,
    )
    if draft is None:
        return {"ok": False, "error": "workspace not found"}
    if result.self_check:
        attach_app_self_check(draft, result.self_check)
    return {
        "ok": True, "draft": draft_to_dict(draft), "candidate": app_view_candidate_summary(result.app_view),
        "recipes": result.recipes, "warnings": result.warnings, "design_notes": result.notes,
        "self_check": compact_self_check(result.self_check) if result.self_check else None,
    }


async def workspace_app_audience(db: Any, *, workspace_id: str, entity_id: str) -> dict[str, Any] | None:
    """Who a live Workspace's App page could be for, to ask the person before designing it.

    The purpose, people, roles and approvals say who uses the page; what waits
    on people, the projects by stage and the page's usage say what each of
    them acts on first. The current page's entries name what it offers now.
    """
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_app_view import normalize_app_view_config

    workspace = await db.get(Workspace, workspace_id)
    if workspace is None or workspace.entity_id != entity_id:
        return None
    context = await workspace_app_design_context(db, workspace)
    current = app_view_candidate_summary(normalize_app_view_config(context.app_view)) if context.app_view else None
    return {**context.audience(), "current_page": current}


def app_view_candidate_summary(config: dict[str, Any]) -> dict[str, Any]:
    """The candidate as the accepting agent reviews it: what shows and what it opens."""
    hidden = set(config["hidden"])
    return {
        "title": config["title"], "description": config["description"], "layout": config["layout"],
        "hero": config["hero"],
        "entries": [{"key": key, "label": config["labels"].get(key) or (config["entries"].get(key) or {}).get("title"),
                     "target": config["targets"].get(key), "hidden": key in hidden}
                    for key in dict.fromkeys([*config["order"], *config["entries"], *config["hidden"]])],
        "sections": [section["block"] for section in config["sections"]] if config["sections"] is not None else "default",
        "data_views": [{
            "key": component["key"], "context": component["context"].get("kind"),
            "blocks": [block["type"] for block in component["blocks"]],
            "card_actions": [action["action"] for block in component["blocks"] for _, action in block_card_actions(block)],
        } for component in config["components"]],
    }


def app_view_replacement(current: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    """An ``app_view.update`` payload that turns ``current`` into ``candidate``.

    Map fields merge in that operation, so the keys the candidate drops are
    sent as ``None``; translations are kept.
    """
    from packages.core.services.workspace_app_view import normalize_app_view_config

    before = normalize_app_view_config(current)
    # ``theme`` is left out of a config that sets none, and None clears it.
    update: dict[str, Any] = {key: candidate.get(key) for key in _APP_PAGE_FIELDS}
    update.update({key: candidate[key] for key in _APP_PICTURE_FIELDS if candidate.get(key)})
    for key in _APP_MAP_FIELDS:
        # ``icons`` is left out of a config that sets none.
        kept = candidate.get(key) or {}
        update[key] = {**{name: None for name in before.get(key) or {} if name not in kept}, **kept}
    return update


# ── Registry entry ──────────────────────────────────────────────────────────

def _app_blockers(context: AppDesignContext) -> dict[str, Any] | None:
    if not context.services and not context.workflows:
        return {"error": "save the services and Workflows before commanding the App design"}
    blocking = [issue for issue in project_structure_issues(context.workflows) if issue["severity"] == "P0"]
    if blocking:
        return {"error": "fix these Workflows with ws_propose_workflow before commanding the App design",
                "issues": blocking}
    return None


def _accept_app_view(fields: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    fields["app_view"] = config
    return {"hero": config["hero"], "entry_count": len(config["entries"]),
            "component_count": len(config["components"])}


async def _run_app_designer(context: AppDesignContext, **kwargs: Any) -> AppDesignResult:
    # Looked up when called, so a replaced ``run_app_designer`` is the one that runs.
    return await run_app_designer(context, **kwargs)


APP_DESIGNER = PartDesigner(
    part="app",
    title="App",
    subagent=SubAgentSpec(
        name=WORKSPACE_APP_DESIGNER_SUBAGENT,
        purpose="Design a Workspace App page from what the Workspace runs and submit a checked candidate.",
        profile=RuntimeProfile.WORKSPACE_ARCHITECT,
        allowed_tools=tuple(schema["function"]["name"] for schema in APP_DESIGNER_TOOL_SCHEMAS),
        output_contract={"format": "app_view_candidate", "required_fields": ("app_view", "design_notes")},
    ),
    skill="workspace-app-design",
    tool_schemas=tuple(APP_DESIGNER_TOOL_SCHEMAS),
    executor=app_designer_executor,
    context_from_draft=AppDesignContext.from_draft_fields,
    context_from_workspace=workspace_app_design_context,
    check=AppDesignContext.check,
    accept=_accept_app_view,
    summarize=app_view_candidate_summary,
    command_tool="ws_design_app_view",
    brief_hint="say what the person wants to do from the App",
    fallback="Command it again with ws_design_app_view; ws_lint_draft says when to stop trying.",
    blockers=_app_blockers,
    revise_note=lambda context: ("The page has a current App; keep what the brief does not ask to change."
                                 if context.app_view else None),
    run=_run_app_designer,
    lint_where=("app_view",),
    depends_on=("workflows",),
    legacy_slot=("app_view_candidate", "app_view"),
)
