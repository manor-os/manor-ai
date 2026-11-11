"""The Flow side of a content calendar: a plan's dated items, and making one of them.

``content_calendar_view`` (``builtin_app_views``) lays a calendar out. These
builders write the Workflow steps that fill it, for any plan of dated content
(an Instagram week, a newsletter month, a campaign):

* ``calendar_plan_steps`` saves a plan as one period project and its dated
  item projects, after the planning Flow has listed them;
* ``calendar_item_steps`` is the child Flow that saves one item: it finds the
  files the plan names for it and calls the item made only when one is there,
  and planning again never unmakes an item made earlier;
* ``calendar_producer_hook`` lets a Flow that makes content take one item as
  its input: the item's facts join the request, and when the Flow is done the
  item is marked made with the files it produced.

How a plan is read into items (a CSV, an agent's list) stays with the
scenario; each item handed over is a flat object of text fields plus
``stage`` and, per file, the path the plan named.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_OBJ = '''import json
def obj(v):
    if isinstance(v, str):
        try: return json.loads(v)
        except Exception: return {}
    return v if isinstance(v, dict) else {}
'''


@dataclass(frozen=True)
class CalendarFile:
    """A file an item can have, by the state field that keeps its Document id."""

    field: str  # e.g. "script_document_id"
    path_field: str  # where a plan names it by path, e.g. "script_path"
    step_name: str  # e.g. "Find the script"


@dataclass(frozen=True)
class CalendarSpec:
    """One plan of dated content: its period and item projects and their stages.

    ``stages`` run from the first, not yet made (``stages[0]``), through made
    (``made_stage``) to done. An item is made when its first ``files`` entry
    is there.
    """

    period_type: str
    item_type: str
    item_key_prefix: str  # project key of an item: prefix + its key field
    item_key_field: str
    parent_field: str  # the item field holding its period project's id
    date_field: str
    item_fields: tuple[str, ...]  # every text field an item keeps, parent/key/date included
    period_fields: tuple[str, ...]
    stages: tuple[str, ...]
    made_stage: str
    files: tuple[CalendarFile, ...] = ()
    period_stage: str = "planned"
    period_word: str = "period"
    item_word: str = "item"

    def item_schema(self) -> dict[str, Any]:
        return _schema([*self.item_fields, *(file.field for file in self.files)],
                       [self.parent_field, self.item_key_field, self.date_field])

    def period_schema(self) -> dict[str, Any]:
        return _schema(list(self.period_fields), [self.period_fields[0]])


def _schema(fields: list[str], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "required": required, "additionalProperties": False,
            "properties": {name: {"type": "string", "maxLength": 4000} for name in fields}}


def _code(step_id: str, name: str, code: str, output_var: str, nxt: str, output_format: str = "json") -> dict[str, Any]:
    return {"id": step_id, "type": "code", "name": name, "next": [nxt],
            "config": {"code": code, "language": "python", "output_var": output_var, "code_timeout": 20,
                       "allow_network": False, "output_format": output_format}}


def calendar_item_steps(spec: CalendarSpec) -> list[dict[str, Any]]:
    """The child Flow saving one planned item (``item``) on the calendar."""
    item = spec.item_word
    reads = [(f"{file.field.removesuffix('_document_id')}_read", file) for file in spec.files]
    describe = _OBJ + f'''def found(name):
    read = obj(inputs.get(name))
    return read.get("document_id") if isinstance(read.get("content"), str) else None
item = obj(inputs.get("item"))
state = {{k: str(item.get(k) or "") for k in {list(spec.item_fields)!r}}}
files ={{field: found(read) for read, field in {[(read, file.field) for read, file in reads]!r}}}
state.update({{field: value for field, value in files.items() if value}})
stage = item.get("stage") or {spec.made_stage!r}
# A plan may name a file it saw; the item reads as made only when that file is really there.
if stage == {spec.made_stage!r} and {bool(spec.files)!r} and not files.get({spec.files[0].field if spec.files else ""!r}):
    stage = {spec.stages[0]!r}
print(json.dumps({{"key": {spec.item_key_prefix!r} + state[{spec.item_key_field!r}], "stage": stage, "state": state}}))
'''
    keep = _OBJ + f'''planned = obj(inputs.get("{item}"))
project = obj(inputs.get("{item}_project"))
saved = obj(project.get("state"))
stage = planned.get("stage") or {spec.stages[0]!r}
made_by = {spec.files[0].field if spec.files else ""!r}
# An item made earlier stays made: planning the {spec.period_word} again does not unmake it.
if stage == {spec.stages[0]!r} and made_by and (saved.get(made_by) or obj(planned.get("state")).get(made_by)):
    stage = {spec.made_stage!r}
# Nor does it move an item already past made (scheduled, published) back.
order = {list(spec.stages)!r}
if project.get("current_stage") in order[order.index({spec.made_stage!r}) + 1:]:
    stage = project["current_stage"]
print(json.dumps({{"stage": stage}}))
'''
    schema = spec.item_schema()
    first = reads[0][0] if reads else f"{item}_state"
    steps: list[dict[str, Any]] = [
        {"id": "start", "type": "trigger", "name": "Start", "next": [first],
         "config": {"trigger_type": "manual", "run_inputs": []}}]
    for index, (read, file) in enumerate(reads):
        steps.append({"id": read, "type": "tool", "name": file.step_name,
                      "next": [reads[index + 1][0] if index + 1 < len(reads) else f"{item}_state"],
                      "config": {"tool": "read_file", "output_var": read, "on_error": "continue",
                                 "args": {"path": "{{item.%s}}" % file.path_field, "max_chars": 200}}})
    steps += [
        _code(f"{item}_state", f"Describe the {item}", describe, item, "claim"),
        {"id": "claim", "type": "workflow_project", "name": f"Find the {item}", "next": [f"{item}_stage"],
         "config": {"operation": "claim", "project_type": spec.item_type, "project_key": "{{%s.key}}" % item,
                    "current_stage": "{{%s.stage}}" % item, "state": "{{%s.state}}" % item, "state_schema": schema,
                    "schema_version": 1, "output_var": f"{item}_project"}},
        _code(f"{item}_stage", "Keep what was made", keep, f"{item}_stage", "save"),
        {"id": "save", "type": "workflow_project", "name": f"Save the {item}", "next": ["end"],
         "config": {"operation": "patch", "project_type": spec.item_type, "project_id": "{{%s_project.project_id}}" % item,
                    "expected_revision": "{{%s_project.revision}}" % item, "current_stage": "{{%s_stage.stage}}" % item,
                    "allowed_stages": list(spec.stages), "patch": "{{%s.state}}" % item, "state_schema": schema,
                    "schema_version": 1, "output_var": "saved"}},
        {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
    ]
    return steps


def calendar_plan_steps(spec: CalendarSpec, *, plan_var: str, item_workflow: str, then: str,
                        decision: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Steps saving ``plan_var`` (``{key, period, items}``) as a period and its items, then ``then``.

    ``item_workflow`` is the id of the Flow ``calendar_item_steps`` makes.
    Planning the same period again (the same ``key``) updates it in place.
    ``decision`` is the ``history_event`` keeping how the person decided on the plan.
    """
    period, item = spec.period_word, spec.item_word
    schema = spec.period_schema()
    with_parent = _OBJ + f'''period_id = obj(inputs.get("saved_{period}")).get("project_id") or obj(inputs.get("{period}_project")).get("project_id") or ""
print(json.dumps({{"items": [{{**entry, {spec.parent_field!r}: period_id}} for entry in obj(inputs.get({plan_var!r})).get("items") or []]}}))
'''
    prefix, key_field = spec.item_key_prefix, spec.item_key_field
    stale = _OBJ + f'''planned = {{{prefix!r} + str(entry.get({key_field!r}) or "") for entry in obj(inputs.get("{item}_items")).get("items") or []}}
items = []
for project in obj(inputs.get("{period}_{item}s")).get("projects") or []:
    key = str(project.get("project_key") or "")
    if key.startswith({prefix!r}) and key not in planned:
        # Everything it holds stays; only its place in the period goes.
        saved = obj(project.get("state"))
        items.append({{**{{field: str(saved.get(field) or "") for field in {list(spec.item_fields)!r}}},
                      {spec.parent_field!r}: "", "id": key, "stage": project.get("current_stage")}})
print(json.dumps({{"items": items}}))
'''
    return [
        {"id": f"claim_{period}", "type": "workflow_project", "name": f"Find the {period}", "next": [f"save_{period}"],
         "config": {"operation": "claim", "project_type": spec.period_type, "project_key": "{{%s.key}}" % plan_var,
                    "current_stage": spec.period_stage, "state": "{{%s.period}}" % plan_var, "state_schema": schema,
                    "schema_version": 1, "output_var": f"{period}_project"}},
        {"id": f"save_{period}", "type": "workflow_project", "name": f"Save the {period}", "next": [f"{item}_items"],
         "config": {"operation": "patch", "project_type": spec.period_type, "project_id": "{{%s_project.project_id}}" % period,
                    "expected_revision": "{{%s_project.revision}}" % period, "current_stage": spec.period_stage,
                    "allowed_stages": [spec.period_stage], "patch": "{{%s.period}}" % plan_var, "state_schema": schema,
                    "schema_version": 1, "output_var": f"saved_{period}",
                    **({"history_event": decision} if decision else {})}},
        _code(f"{item}_items", f"Date the {item}s", with_parent, f"{item}_items", f"save_{item}s"),
        {"id": f"save_{item}s", "type": "foreach_subworkflow", "name": f"Put the {item}s on the calendar",
         "next": [f"{period}_{item}s"],
         "config": {"over": "{{%s_items.items}}" % item, "item_key": "id", "item_var": "item", "output_var": f"saved_{item}s",
                    "concurrency": 3, "workflow_id": item_workflow, "continue_on_failure": True}},
        # Planning the period again replaces its items: one the new plan leaves out leaves the calendar.
        {"id": f"{period}_{item}s", "type": "workflow_project", "name": f"Read the {period}'s {item}s", "next": [f"stale_{item}s"],
         "config": {"operation": "list", "project_type": spec.item_type, "parent_id": "{{%s_project.project_id}}" % period,
                    "parent_state_key": spec.parent_field, "limit": 100, "state_schema": spec.item_schema(),
                    "schema_version": 1, "output_var": f"{period}_{item}s"}},
        _code(f"stale_{item}s", f"Find {item}s no longer planned", stale, f"stale_{item}s", f"unplan_{item}s"),
        # Tidying the calendar never fails the plan it follows.
        {"id": f"unplan_{item}s", "type": "foreach_subworkflow", "name": "Take them off the calendar", "next": [then],
         "config": {"over": "{{stale_%ss.items}}" % item, "item_key": "id", "item_var": "item", "output_var": f"unplanned_{item}s",
                    "concurrency": 3, "workflow_id": item_workflow, "continue_on_failure": True,
                    "on_error": "continue_error", "error_next": [then]}},
    ]


def calendar_producer_hook(spec: CalendarSpec, *, input_key: str, input_label: str, brief: tuple[tuple[str, str], ...],
                           then: str, done_next: str, made_files: dict[str, str],
                           format_field: str | None = None, format_choices: tuple[str, ...] = (),
                           format_var: str | None = None, decision: dict[str, str] | None = None) -> dict[str, Any]:
    """Let a Flow that makes content take one calendar item as its input.

    Returns ``{"run_input", "entry", "exit", "brief"}``:

    * ``run_input``: the optional item picker to add to the Flow's start;
    * ``entry``: steps to run right after start, ending at ``then``; they read
      the item and write ``brief`` (``(label, field)`` pairs) into
      ``{{<item>_brief.text}}``, which the Flow's prompts read after its own
      request; with ``format_field``, the item's format (one of
      ``format_choices``) replaces ``format_var``'s form default;
    * ``exit``: steps to run when the content is made, ending at
      ``done_next``; they mark the item ``made_stage`` and keep
      ``made_files`` (state field -> template of the made Document id), and
      ``decision``, the ``history_event`` of an approval of what was made.
    """
    item = spec.item_word
    schema = spec.item_schema()
    lines = repr([(label, field) for label, field in brief])
    brief_code = _OBJ + f'''planned = obj(obj(inputs.get({item!r})).get("state"))
lines = []
if planned:
    lines = ["", "Calendar {item} to make (from the plan):"] + [f"- {{label}}: {{planned.get(field, '')}}" for label, field in {lines}]
chosen = str(planned.get({format_field or ""!r}, "")).strip().lower()
print(json.dumps({{"text": "\\n".join(lines), "format": chosen if chosen in {list(format_choices)!r} else ""}}))
'''
    entry: list[dict[str, Any]] = [
        {"id": f"{item}_given", "type": "condition", "name": f"A calendar {item} was picked", "next": [],
         "config": {"expression": f"{input_key} != ''"}, "true_next": [f"get_{item}"], "false_next": [f"{item}_brief"]},
        {"id": f"get_{item}", "type": "workflow_project", "name": f"Open the calendar {item}", "next": [f"{item}_brief"],
         "config": {"operation": "get", "project_type": spec.item_type, "project_id": "{{%s}}" % input_key,
                    "state_schema": schema, "schema_version": 1, "output_var": item, "chat_projection": "hidden"}},
        _code(f"{item}_brief", f"Add the {item} to the request", brief_code, f"{item}_brief",
              f"{item}_format" if format_field and format_var else then),
    ]
    if format_field and format_var:
        entry.append(_code(f"{item}_format", f"Use the {item}'s format", _OBJ + f'''import sys
sys.stdout.write(obj(inputs.get("{item}_brief")).get("format") or str(inputs.get({format_var!r}) or {format_choices[0] if format_choices else ""!r}))
''', format_var, then, output_format="text"))
    exit_steps = [
        {"id": f"{item}_made", "type": "condition", "name": f"A calendar {item} was made", "next": [],
         "config": {"expression": f"{input_key} != ''"}, "true_next": [f"mark_{item}"], "false_next": [done_next]},
        {"id": f"mark_{item}", "type": "workflow_project", "name": f"Mark the {item} made", "next": [done_next],
         "config": {"operation": "patch", "project_type": spec.item_type, "project_id": "{{%s}}" % input_key,
                    "expected_revision": "{{%s.revision}}" % item, "current_stage": spec.made_stage,
                    "allowed_stages": list(spec.stages), "patch": dict(made_files), "state_schema": schema,
                    "schema_version": 1, "output_var": f"made_{item}",
                    **({"history_event": decision} if decision else {})}},
    ]
    run_input = {"key": input_key, "type": "string", "label": input_label, "hidden": False, "default": "", "required": False,
                 "target": input_key, "schema": {"type": "string", "x-ui": {
                     "control": "workflow_project_select", "project_type": spec.item_type,
                     "allowed_stages": list(spec.stages[:spec.stages.index(spec.made_stage) + 1])}}}
    return {"run_input": run_input, "entry": entry, "exit": exit_steps, "brief": "{{%s_brief.text}}" % item}
