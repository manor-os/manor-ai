"""Workflow patterns: checked blocks of Workflow steps for recurring kinds of work.

Each builder returns portable (Blueprint) Workflows a Workspace can run as
they are, for any business that has that kind of work:

* ``content_calendar_flows``: a confirmed plan of dated content (a campaign's
  posts, an Instagram week, a newsletter month) saved as a period and its
  items, with optional making, scheduling and publishing of each item;
* ``schedule_expansion_flows``: once a plan is confirmed, each of its dated
  items is given the time it is due, and a worker run on a schedule starts
  the item's Flow when that time comes;
* ``channel_publish_flow``: one item published on one channel after the
  person approves it, within a deadline, ending in a verified receipt;
* ``recurring_publish_flows``: one item made and published on a fixed rhythm,
  with no plan to confirm (a post every Monday, a weekly newsletter);
* ``report_stat_flows``: a recurring report whose run assesses and records
  Stats from the Workspace's evidence;
* ``project_tree_flows``: a parent project that plans its parts and a Flow
  that makes one part inside it;
* ``source_watch_flows``: a Flow run on a schedule that finds what is new in a
  connected account and starts a Flow once for each new thing;
* ``record_collection_flows``: a collection people keep by hand (a menu, a
  catalogue, a list of offers): add one, change one starting from its values,
  take one off. No step runs a model.

Every builder returns ``{"workflows", "stats", "automations"}``. An entry of
``automations`` names a Workflow that must run on a schedule; scheduling it
is left to whoever owns the Workspace's Automations. A person's approval is
kept on the project it decides, as ``history_event``.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from packages.core.blueprints.content_calendar import (
    _OBJ,
    CalendarFile,
    CalendarSpec,
    _code,
    calendar_item_steps,
    calendar_plan_steps,
    calendar_producer_hook,
)

# How often a schedule worker looks for due items; lease_due leases one per run.
WORKER_CRON = "*/15 * * * *"
_TEXT = {"type": "string"}


def _flow(slug: str, name: str, description: str, steps: list[dict[str, Any]], *,
          internal: bool = False) -> dict[str, Any]:
    # The start step's inputs are also the Flow's, which the App reads.
    run_inputs = next(step["config"].get("run_inputs") or [] for step in steps if step["type"] == "trigger")
    for step in steps:
        if step["type"] in LOGIC_STEP_TYPES:
            step["meta"] = {**step.get("meta", {}), "pattern": pattern_mark(step)}
    return {"slug": slug, "name": name, "description": description, "steps": steps, "run_inputs": run_inputs,
            "trigger_type": "internal" if internal else "manual", "internal": internal}


# Steps that decide what a run does; a designer uses them only as a pattern built them.
LOGIC_STEP_TYPES = frozenset({"code", "transform", "switch", "tool", "condition"})


def pattern_mark(step: dict[str, Any]) -> str:
    """What a pattern built this step as; changing its routes or config changes the mark."""
    built = {key: step.get(key) for key in ("type", "config", "next", "true_next", "false_next")}
    return hashlib.sha256(json.dumps(built, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _guarded(step: dict[str, Any], guard: str) -> dict[str, Any]:
    """Say what this step enforces, for whoever reviews the design without reading its config."""
    step["meta"] = {**step.get("meta", {}), "guard": guard}
    return step


def _start(first: str, run_inputs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": "start", "type": "trigger", "name": "Start", "next": [first], "config": {"run_inputs": run_inputs}}


def _end(step_id: str = "done", name: str = "Done") -> dict[str, Any]:
    return {"id": step_id, "type": "end", "name": name, "next": [], "config": {}}


def _agent(step_id: str, name: str, service_key: str, prompt: str, nxt: str, *,
           output_schema: dict[str, Any] | None = None, expects: list[str] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"service_key": service_key, "prompt": prompt}
    if output_schema is not None:
        config.update(output_format="json", output_schema=output_schema)
    if expects:
        config["expects"] = list(expects)
    return {"id": step_id, "type": "agent", "name": name, "next": [nxt], "config": config}


def _approval(step_id: str, name: str, message: str, nxt: str, *, revise_from: str | None,
              options: tuple[str, ...] = ("approve", "revise"), asked: bool | str = True) -> dict[str, Any]:
    """An approval wait; ``asked`` as text says which rule or words of the person ask for it."""
    config: dict[str, Any] = {"wait_type": "approval", "message": message, "options": list(options),
                              "approval_values": ["approve"], "response_variable": f"{step_id}_decision"}
    if revise_from:
        config["revise_from"] = revise_from
    step = {"id": step_id, "type": "wait", "name": name, "next": [nxt], "config": config}
    if isinstance(asked, str) and asked.strip():
        step["meta"] = {"asked_by": asked.strip()}
    return step


def _decision(kind: str, approval_id: str) -> dict[str, str]:
    """The ``history_event`` that keeps what the person decided at ``approval_id``."""
    answer = f"{approval_id}_decision"
    return {"kind": kind, "decision": "{{%s.choice}}" % answer, "note": "{{%s.note}}" % answer}


def _input(key: str, label: str, *, required: bool = False, hidden: bool = False, kind: str = "string",
           schema: dict[str, Any] | None = None, default: Any = "") -> dict[str, Any]:
    row = {"key": key, "label": label, "type": kind, "required": required, "hidden": hidden}
    if not required:
        row["default"] = default
    if schema is not None:
        row["schema"] = schema
    return row


def _picker(key: str, label: str, project_type: str, stages: list[str] | None = None) -> dict[str, Any]:
    ui: dict[str, Any] = {"control": "workflow_project_select", "project_type": project_type}
    if stages:
        ui["allowed_stages"] = list(stages)
    return _input(key, label, hidden=True, schema={"type": "string", "x-ui": ui})


def _object(fields: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": fields, "required": required}


def _patch(step_id: str, name: str, spec_type: str, schema: dict[str, Any], *, project: str, revision: str,
           stage: str | None, allowed: list[str], patch: dict[str, Any], nxt: str,
           decision: dict[str, str] | None = None) -> dict[str, Any]:
    """Patch a project; ``stage`` None leaves its stage where it is, ``decision`` joins its decision log."""
    config: dict[str, Any] = {"operation": "patch", "project_type": spec_type, "project_id": project,
                              "expected_revision": revision, "patch": patch, "state_schema": schema,
                              "schema_version": 1, "output_var": step_id}
    if stage is not None:
        config.update(current_stage=stage, allowed_stages=allowed)
    if decision is not None:
        config["history_event"] = decision
    return {"id": step_id, "type": "workflow_project", "name": name, "next": [nxt], "config": config}


# ── Schedule expansion ──────────────────────────────────────────────────────

_HISTORY = {"type": "array", "items": {"type": "object"}}


def _with_fields(schema: dict[str, Any], *names: str) -> dict[str, Any]:
    """``schema`` that also keeps ``names`` (text), its decision log and the lease a schedule worker holds."""
    properties = {**schema.get("properties", {}), **{name: {"type": "string", "maxLength": 4000} for name in names},
                  "history": _HISTORY, "lease": {"type": ["object", "null"]}}
    return {**schema, "properties": properties}


def _one_schema_per_project(workflows: list[dict[str, Any]]) -> None:
    """Give every step of a project type the union of the state its Flows keep."""
    merged: dict[str, dict[str, Any]] = {}
    for workflow in workflows:
        for step in workflow["steps"]:
            config = step["config"]
            if step["type"] == "workflow_project" and isinstance(config.get("state_schema"), dict):
                schema = merged.setdefault(config["project_type"], {**config["state_schema"], "properties": {}})
                schema["properties"].update(config["state_schema"].get("properties") or {})
                if "history_event" in config:
                    schema["properties"]["history"] = _HISTORY
    for workflow in workflows:
        for step in workflow["steps"]:
            if step["type"] == "workflow_project" and "state_schema" in step["config"]:
                step["config"]["state_schema"] = merged[step["config"]["project_type"]]


def schedule_expansion_flows(*, slug: str, item_type: str, item_schema: dict[str, Any], date_field: str,
                             timezone: str, lead_minutes: int, waiting_stages: list[str], scheduled_stage: str,
                             started_stage: str, run_workflow: str, run_input: str,
                             parent_field: str | None = None, item_word: str = "item") -> dict[str, Any]:
    """Each item of a confirmed plan becomes due ``lead_minutes`` before its date.

    ``<slug>_schedule`` (internal) schedules one item: it gives an item in a
    ``waiting_stages`` stage its ``due_at`` and moves it to
    ``scheduled_stage``. ``expansion_steps`` (in the result) run it over a
    plan's items; ``<slug>_due`` is the worker an Automation runs every 15
    minutes: it leases one due item, moves it to ``started_stage`` and runs
    ``run_workflow`` with the item's project id as ``run_input``.
    """
    schema = _with_fields(item_schema, "due_at")
    stages = list(dict.fromkeys([*waiting_stages, scheduled_stage]))
    when = _OBJ + f'''from datetime import datetime, timedelta, timezone
item = obj(inputs.get("item"))
state = obj(item.get("state"))
raw = str(state.get({date_field!r}) or "").strip()
due = ""
try:
    at = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    if at is not None and at.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo
            at = at.replace(tzinfo=ZoneInfo({timezone!r}))
        except Exception:
            at = at.replace(tzinfo=timezone.utc)
    if at is not None:
        due = (at - timedelta(minutes={int(lead_minutes)})).astimezone(timezone.utc).isoformat()
except ValueError:
    due = ""
# Only an item still waiting is scheduled: one already made public keeps its stage.
print(json.dumps({{"schedule": bool(due) and item.get("current_stage") in {stages!r}, "due_at": due}}))
'''
    schedule_item = _flow(
        f"{slug}_schedule", f"Schedule one {item_word}",
        f"Give one planned {item_word} the time it is due, from its {date_field}.",
        [
            _start("when", [_input("item", item_word.capitalize(), required=True, kind="json")]),
            _code("when", f"Work out when the {item_word} is due", when, "when", "schedulable"),
            {"id": "schedulable", "type": "condition", "name": f"The {item_word} has a date", "next": [],
             "config": {"expression": "when.schedule == true"}, "true_next": ["scheduled"], "false_next": ["done"]},
            _patch("scheduled", f"Schedule the {item_word}", item_type, schema, project="{{item.project_id}}",
                   revision="{{item.revision}}", stage=scheduled_stage, allowed=stages,
                   patch={"due_at": "{{when.due_at}}"}, nxt="done"),
            _end(),
        ], internal=True)
    due_worker = _flow(
        f"{slug}_due", f"Start due {item_word}s",
        f"Runs on a schedule: starts the next {item_word} whose time has come.",
        [
            _start("lease", []),
            _guarded({"id": "lease", "type": "workflow_project", "name": f"Find a due {item_word}", "next": ["has_item"],
                      "config": {"operation": "lease_due", "project_type": item_type, "due_path": "due_at",
                                 "pending_stages": [scheduled_stage], "lease_path": "lease", "lease_seconds": 900,
                                 "state_schema": schema, "schema_version": 1, "output_var": "slot"}},
                     f"A {item_word} starts only once its due time has come, up to one worker interval late."),
            {"id": "has_item", "type": "condition", "name": f"A {item_word} is due", "next": [],
             "config": {"expression": "slot.has_project == true"}, "true_next": ["started"], "false_next": ["idle"]},
            # The lease ends before the item's Flow runs, so a Flow waiting for a person never holds it.
            {"id": "started", "type": "workflow_project", "name": f"Start the {item_word}", "next": ["run"],
             "config": {"operation": "complete_lease", "project_type": item_type,
                        "project_id": "{{slot.project.project_id}}", "lease_path": "lease",
                        "current_stage": started_stage, "patch": {}, "state_schema": schema,
                        "schema_version": 1, "output_var": "started"}},
            {"id": "run", "type": "subworkflow", "name": f"Run the {item_word}", "next": ["done"],
             "config": {"workflow_id": run_workflow, "inputs": [
                 {"key": run_input, "value": "{{slot.project.project_id}}"}], "output_var": "item_run"}},
            _end(),
            _end("idle", "Nothing due"),
        ])
    list_config: dict[str, Any] = {"operation": "list", "project_type": item_type, "limit": 100,
                                   "state_schema": schema, "schema_version": 1, "output_var": f"{item_word}s_to_schedule"}
    if parent_field:
        list_config.update(parent_state_key=parent_field)
    return {
        "workflows": [schedule_item, due_worker],
        "schema": schema,
        "list_config": list_config,
        "automations": [{"workflow": due_worker["slug"], "cron_expr": WORKER_CRON, "timezone": timezone,
                         "why": f"starts each {item_word} when it is due"}],
    }


def expansion_steps(expansion: dict[str, Any], *, parent_id: str | None, then: str,
                    item_word: str = "item") -> list[dict[str, Any]]:
    """Steps that schedule every item (of ``parent_id``, when given), then ``then``."""
    listed = f"{item_word}s_to_schedule"
    config = dict(expansion["list_config"])
    if parent_id is not None:
        config["parent_id"] = parent_id
    # ``listed`` is also the step id a plan Flow routes to.
    return [
        {"id": listed, "type": "workflow_project", "name": f"Read the planned {item_word}s",
         "next": [f"schedule_{item_word}s"], "config": config},
        {"id": f"schedule_{item_word}s", "type": "foreach_subworkflow", "name": f"Schedule each {item_word}",
         "next": [then], "config": {"over": "{{%s.projects}}" % listed, "item_key": "project_id", "item_var": "item",
                                    "output_var": f"scheduled_{item_word}s", "concurrency": 3,
                                    "workflow_id": expansion["workflows"][0]["slug"], "continue_on_failure": True}},
    ]


# ── Single-channel publish ──────────────────────────────────────────────────

# Reads a date or moment as text an Agent planned; what cannot be read bounds nothing.
_MOMENT = '''from datetime import datetime, time, timezone as _utc
def moment(raw, zone_name, end=False):
    raw = str(raw or "").strip()
    try:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    except ValueError:
        return None
    if at is None:
        return None
    if len(raw) == 10:
        at = datetime.combine(at.date(), time.max if end else time.min)
    if at.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo
            at = at.replace(tzinfo=ZoneInfo(zone_name))
        except Exception:
            at = at.replace(tzinfo=_utc.utc)
    return at
'''


def _outside_period(period: dict[str, str], period_word: str) -> str:
    """Code setting ``outside``: why now is not within the period ``state`` keeps, or nothing."""
    return f'''now = datetime.now(_utc.utc)
opens = moment(state.get({period["start"]!r}), {period["timezone"]!r})
closes = moment(state.get({period["end"]!r}), {period["timezone"]!r}, end=True)
outside = ""
if opens and now < opens:
    outside = "The {period_word} starts on " + str(state.get({period["start"]!r}))
elif closes and now > closes:
    outside = "The {period_word} ended on " + str(state.get({period["end"]!r}))
'''


def channel_publish_flow(*, slug: str, item_type: str, item_schema: dict[str, Any], input_key: str,
                         service_key: str, channel: str, prompt: str, ready_stages: list[str],
                         published_stage: str, failed_stage: str, window_hours: int,
                         action_keys: list[str], summary_fields: list[str], make_workflow: str | None = None,
                         unmade_stage: str | None = None, requires: list[str] | None = None,
                         item_word: str = "item", period: dict[str, str] | None = None) -> dict[str, Any]:
    """Publish one item on one channel: approved, within its window, with a verified receipt.

    The person approves the exact item; the approval becomes a grant scoped
    to that item and channel; no publishing action starts after the
    deadline (the item's ``due_at`` or now, plus ``window_hours``), and an
    approval that comes later sets the item aside with why; the agent's result
    counts only as a verified ``publication_receipt``, kept on the item with
    its link. With ``make_workflow``, an item still in ``unmade_stage`` is
    made first. ``requires`` are item fields that must be filled before the
    person is asked (the account to post from, the made file); an item missing
    one is set aside with what it lacks. ``period`` ({start, end, timezone,
    word}: the item fields keeping the dates of its period) does the same for
    an item outside them, before the person is asked and again after.
    """
    schema = _with_fields(item_schema, "due_at", "published_url", "external_id", "published_at", "publish_issue")
    stages = list(dict.fromkeys([*ready_stages, *([unmade_stage] if unmade_stage else []), published_stage,
                                 failed_stage]))
    facts = "\n".join(f"- {field}: {{{{item.state.{field}}}}}" for field in summary_fields)
    receipt = _object({
        "platform": _TEXT, "verification_status": {"type": "string", "enum": ["verified", "unverified", "failed"]},
        "external_id": _TEXT, "published_url": _TEXT, "published_at": _TEXT,
        "evidence": {"type": "array", "items": {"type": "object"}},
    }, ["platform", "verification_status"])
    publish_prompt = (
        f"{prompt.strip()}\n\nPublish this {item_word} on {channel}, exactly as approved:\n{facts}\n"
        f"Files are on the {item_word} ({{{{item.state}}}}). After publishing, open the published {item_word} and "
        "return {receipt:{platform, verification_status:'verified' only when you saw it live, external_id, "
        "published_url, published_at, evidence:[{kind, url}]}, payload:{what you published: text and file ids}}. "
        "If it could not be published, return verification_status 'failed' with the reason in evidence; never "
        "claim a link you did not see."
    )
    requires = list(dict.fromkeys(requires or []))
    undeclared = sorted(set(requires) - set(item_schema.get("properties") or {}))
    if undeclared:
        raise ValueError(f"requires names fields {item_type} does not keep: {', '.join(undeclared)}")
    ask = "ready" if requires or period else "approve_post"
    outside = _outside_period(period, period["word"]) if period else 'outside = ""\n'
    steps: list[dict[str, Any]] = [
        _start("item", [_picker(input_key, item_word.capitalize(), item_type, stages[:-2] or None)]),
        {"id": "item", "type": "workflow_project", "name": f"Open the {item_word}",
         "next": ["needs_making" if make_workflow else ask],
         "config": {"operation": "get", "project_type": item_type, "project_id": "{{%s}}" % input_key,
                    "state_schema": schema, "schema_version": 1, "output_var": "item"}},
    ]
    if make_workflow:
        steps += [
            {"id": "needs_making", "type": "condition", "name": f"The {item_word} is not made yet", "next": [],
             "config": {"expression": f"item.current_stage == '{unmade_stage}'"},
             "true_next": ["make_first"], "false_next": [ask]},
            {"id": "make_first", "type": "subworkflow", "name": f"Make the {item_word}", "next": ["made_item"],
             "config": {"workflow_id": make_workflow, "inputs": [{"key": input_key, "value": "{{%s}}" % input_key}],
                        "output_var": "making"}},
            {"id": "made_item", "type": "workflow_project", "name": f"Open the made {item_word}", "next": [ask],
             "config": {"operation": "get", "project_type": item_type, "project_id": "{{%s}}" % input_key,
                        "state_schema": schema, "schema_version": 1, "output_var": "item"}},
        ]
    if ask == "ready":
        # Read as the text the item keeps; nothing is parsed out of it but its period's dates.
        missing = _OBJ + _MOMENT + f'''state = obj(obj(inputs.get("item")).get("state"))
missing = [field for field in {requires!r} if not str(state.get(field) or "").strip()]
''' + outside + '''issue = outside or (("Not ready to publish: missing " + ", ".join(missing)) if missing else "")
print(json.dumps({"ok": not issue, "issue": issue}))
'''
        kept_out = ([f"missing {', '.join(requires)}"] if requires else []) + (
            [f"outside its {period['word']}'s dates"] if period else [])
        steps += [
            _guarded(_code("ready", f"Check the {item_word} is ready", missing, "ready", "is_ready"),
                     f"Set aside with the reason, before anyone is asked, when {' or '.join(kept_out)}."),
            {"id": "is_ready", "type": "condition", "name": "Ready to publish", "next": [],
             "config": {"expression": "ready.ok == true"}, "true_next": ["approve_post"], "false_next": ["not_ready"]},
            _patch("not_ready", f"Set the {item_word} aside", item_type, schema, project="{{item.project_id}}",
                   revision="{{item.revision}}", stage=failed_stage, allowed=stages,
                   patch={"publish_issue": "{{ready.issue}}"}, nxt="done"),
        ]
    on_time = _OBJ + _MOMENT + '''state = obj(obj(inputs.get("item")).get("state"))
''' + outside + f'''deadline = moment(inputs.get("publish_by"), "UTC")
late = "Approved after its publishing deadline ({window_hours} hours from its time)" if deadline and datetime.now(_utc.utc) >= deadline else ""
issue = outside or late
print(json.dumps({{"ok": not issue, "issue": ("Not published: " + issue) if issue else ""}}))
'''
    steps += [
        _guarded(_approval("approve_post", f"Approve the {channel} post",
                           f"Publish this {item_word} on {channel}?\n{facts}", "approved",
                           revise_from=None, options=("approve", "skip")),
                 f"The person approves each {item_word}; the approval allows only this {item_word} on {channel}."),
        {"id": "approved", "type": "condition", "name": "Approved", "next": [],
         # The decision is kept as {choice} or as the choice itself.
         "config": {"expression": "approve_post_decision.choice == 'approve' or approve_post_decision == 'approve'"},
         "true_next": ["publish_by"], "false_next": ["keep_skipped"]},
        _patch("keep_skipped", "Keep that it was skipped", item_type, schema, project="{{item.project_id}}",
               revision="{{item.revision}}", stage=None, allowed=[], patch={}, nxt="skipped",
               decision=_decision("publish_review", "approve_post")),
        {"id": "publish_by", "type": "datetime", "name": "Work out the publishing deadline", "next": ["on_time"],
         "config": {"operation": "add", "value": "{{item.state.due_at}}", "amount": int(window_hours),
                    "unit": "hours", "output_var": "publish_by"}},
        _guarded(_code("on_time", "Check it is still on time", on_time, "on_time", "is_on_time"),
                 f"An approval after the deadline ({int(window_hours)} hours from the {item_word}'s time)"
                 + (f" or outside its {period['word']}'s dates" if period else "")
                 + f" publishes nothing: the {item_word} is set aside with the reason."),
        {"id": "is_on_time", "type": "condition", "name": "Still on time", "next": [],
         "config": {"expression": "on_time.ok == true"}, "true_next": ["grant"], "false_next": ["too_late"]},
        _patch("too_late", f"Set the {item_word} aside", item_type, schema, project="{{item.project_id}}",
               revision="{{item.revision}}", stage=failed_stage, allowed=stages,
               patch={"publish_issue": "{{on_time.issue}}"}, nxt="done",
               decision=_decision("publish_review", "approve_post")),
        {"id": "grant", "type": "workflow_action_grant", "name": f"Allow this {channel} post", "next": ["deadline"],
         "config": {"operation": "create", "approval_step_id": "approve_post", "project_id": "{{item.project_id}}",
                    "grant_type": f"{channel}_publication", "ttl_seconds": int(window_hours) * 3600,
                    "scope": {"channel": channel, "project_id": "{{item.project_id}}"},
                    "scope_schema": _object({"channel": {"const": channel}, "project_id": _TEXT},
                                            ["channel", "project_id"]),
                    "output_var": "grant"}},
        {"id": "deadline", "type": "workflow_effect_deadline", "name": "No publishing after the deadline",
         "next": ["publish"], "config": {"deadline_at": "{{publish_by}}", "action_keys": list(action_keys),
                                         "output_var": "deadline"}},
        _agent("publish", f"Publish on {channel}", service_key, publish_prompt, "receipt",
               output_schema=_object({"receipt": receipt, "payload": {"type": "object"}}, ["receipt", "payload"]),
               expects=["effects"]),
        _guarded({"id": "receipt", "type": "publication_receipt", "name": "Check the published post", "next": ["published"],
                  "config": {"receipt": "{{publish.receipt}}", "payload": "{{publish.payload}}", "require_verified": True,
                             "output_var": "receipt", "on_error": "continue_error", "error_next": ["not_published"]}},
                 "Counts as published only with a verified receipt and its real link; otherwise set aside with the reason."),
        _patch("published", "Keep the published link", item_type, schema, project="{{item.project_id}}",
               revision="{{item.revision}}", stage=published_stage, allowed=stages,
               patch={"published_url": "{{receipt.published_url}}", "external_id": "{{receipt.external_id}}",
                      "published_at": "{{receipt.published_at}}", "publish_issue": ""}, nxt="done",
               decision=_decision("publish_review", "approve_post")),
        _patch("not_published", "Keep why it was not published", item_type, schema, project="{{item.project_id}}",
               revision="{{item.revision}}", stage=failed_stage, allowed=stages,
               patch={"publish_issue": f"Not published on {channel}: the post could not be verified live."},
               nxt="done", decision=_decision("publish_review", "approve_post")),
        _end("skipped", "Skipped"),
        _end(),
    ]
    return _flow(slug, f"Publish on {channel}", f"Publish one approved {item_word} on {channel} and keep its link.",
                 steps)


# ── Content calendar ────────────────────────────────────────────────────────

def _publish_channels(publish: dict[str, Any] | None) -> list[dict[str, Any]]:
    """``publish`` as one entry per channel; options outside ``channels`` apply to each."""
    if not publish:
        return []
    shared = {key: value for key, value in publish.items() if key not in {"channels", "channel_field"}}
    rows = publish.get("channels") or [shared]
    channels = []
    for row in rows:
        merged = {**shared, **(row if isinstance(row, dict) else {"channel": row})}
        name = re.sub(r"[^a-z0-9]+", "_", str(merged.get("channel") or "").strip().lower()).strip("_")
        if not name:
            raise ValueError("each publish channel needs a name, e.g. instagram")
        channels.append({**merged, "channel": name})
    if len({row["channel"] for row in channels}) != len(channels):
        raise ValueError("publish names a channel twice")
    return channels


def _channel_router(*, slug: str, spec: CalendarSpec, channel_field: str, flows: dict[str, str],
                    failed_stage: str) -> dict[str, Any]:
    """Run the publishing Flow of an item's channel; an item on no set-up channel is set aside."""
    item, input_key = spec.item_word, f"{spec.item_word}_id"
    schema = _with_fields(spec.item_schema(), "publish_issue")
    channels = list(flows)
    steps: list[dict[str, Any]] = [
        _start("item", [_picker(input_key, item.capitalize(), spec.item_type)]),
        {"id": "item", "type": "workflow_project", "name": f"Open the {item}", "next": [f"on_{channels[0]}"],
         "config": {"operation": "get", "project_type": spec.item_type, "project_id": "{{%s}}" % input_key,
                    "state_schema": schema, "schema_version": 1, "output_var": "item"}},
    ]
    for index, channel in enumerate(channels):
        following = f"on_{channels[index + 1]}" if index + 1 < len(channels) else "no_channel"
        steps += [
            {"id": f"on_{channel}", "type": "condition", "name": f"Posted on {channel}", "next": [],
             "config": {"expression": f"item.state.{channel_field} == '{channel}'"},
             "true_next": [f"publish_{channel}"], "false_next": [following]},
            {"id": f"publish_{channel}", "type": "subworkflow", "name": f"Publish on {channel}", "next": ["done"],
             "config": {"workflow_id": flows[channel], "inputs": [{"key": input_key, "value": "{{%s}}" % input_key}],
                        "output_var": f"published_{channel}"}},
        ]
    steps += [
        _patch("no_channel", f"Set the {item} aside", spec.item_type, schema, project="{{item.project_id}}",
               revision="{{item.revision}}", stage=failed_stage, allowed=list(spec.stages),
               patch={"publish_issue": f"No publishing is set up for this {item}'s channel ({', '.join(channels)} are)."},
               nxt="done"),
        _end(),
    ]
    return _flow(slug, f"Publish a {item}", f"Publish one {item} on its own channel.", steps)


def content_calendar_flows(spec: CalendarSpec, *, slug: str, name: str, service_key: str, plan_prompt: str,
                           approve_plan: bool | str = True, period_key_prefix: str = "",
                           make: dict[str, Any] | None = None, schedule: dict[str, Any] | None = None,
                           publish: dict[str, Any] | None = None) -> dict[str, Any]:
    """A planning Flow for dated content and the Flows that work each item.

    ``<slug>_plan`` asks an Agent for the plan ({key, summary, period,
    items}), waits for the person to confirm it, and saves it on the
    calendar through ``<slug>_item``. ``make`` ({service_key, prompt,
    file_field, approve}) adds ``<slug>_make``, which makes one item;
    ``publish`` ({service_key, channel, prompt, window_hours, action_keys,
    within_period}) adds ``<slug>_publish``; ``schedule`` ({timezone,
    lead_minutes}) makes each confirmed item due before its date and starts
    its publishing (or making) Flow then. ``within_period`` (true, or {start,
    end} naming the period fields with its dates) keeps those dates on each
    item, so an item outside them is not published.
    """
    item, period = spec.item_word, spec.period_word
    text_fields = [field for field in spec.item_fields if field != spec.parent_field]
    bounds = {}
    within = (publish or {}).get("within_period")
    if within:
        named = within if isinstance(within, dict) else {}
        bounds = {"not_before": str(named.get("start") or "start"), "not_after": str(named.get("end") or "end")}
        unknown = sorted(set(bounds.values()) - set(spec.period_fields))
        if unknown:
            raise ValueError(f"within_period names fields the {period} does not keep: {', '.join(unknown)}")
    files = spec.files
    if make and not files:
        field = str(make.get("file_field") or f"{item}_document_id")
        files = (CalendarFile(field, field.removesuffix("_document_id") + "_path", f"Find the made {item}"),)
    published_stage = str((publish or {}).get("published_stage") or "published")
    failed_stage = str((publish or {}).get("failed_stage") or "needs_attention")
    stages = list(spec.stages)
    if schedule:
        stages = [*stages, "scheduled", "started"]
    if publish:
        stages = [*stages, published_stage, failed_stage]
    spec = replace(spec, files=files, stages=tuple(dict.fromkeys(stages)), item_fields=(*spec.item_fields, *bounds))
    item_slug, plan_slug, make_slug = f"{slug}_item", f"{slug}_plan", f"{slug}_make"
    workflows: list[dict[str, Any]] = []
    automations: list[dict[str, Any]] = []

    item_steps = calendar_item_steps(spec)
    item_steps[0]["config"] = {"run_inputs": [_input("item", item.capitalize(), required=True, kind="json")]}
    workflows.append(_flow(item_slug, f"Put one {item} on the calendar",
                           f"Save one planned {item} of a {period} on the calendar.", item_steps, internal=True))

    channels = _publish_channels(publish)
    channel_field = str((publish or {}).get("channel_field") or "channel") if len(channels) > 1 else None
    if channel_field and channel_field not in text_fields:
        raise ValueError(f"publishing on several channels routes by an item field: add {channel_field!r} to item_fields")
    item_properties: dict[str, Any] = {field: _TEXT for field in text_fields}
    if channel_field:
        # The plan names a channel exactly as the Flows route by it.
        item_properties[channel_field] = {"type": "string", "enum": [row["channel"] for row in channels]}
    plan_schema = _object({
        "key": {"type": "string", "description": f"Stable id of this {period}, e.g. 2026-W45"},
        "summary": {"type": "string", "description": "The plan in a few lines, for the person to confirm"},
        "period": _object({field: _TEXT for field in spec.period_fields}, [spec.period_fields[0]]),
        "items": {"type": "array", "items": _object(item_properties, [spec.item_key_field, spec.date_field,
                                                                      *([channel_field] if channel_field else [])])},
    }, ["key", "summary", "period", "items"])
    plan_contract = (
        f"\n\nReturn the {period} plan as JSON: key (a stable id for the {period}), summary (what you plan, "
        f"in a few lines), period {{{', '.join(spec.period_fields)}}}, and items, one per dated {item}, each with "
        f"{', '.join(text_fields)}. {spec.item_key_field} is unique within the {period}; {spec.date_field} is an "
        "ISO date or date and time."
    )
    normalize = _OBJ + f'''plan = obj(inputs.get("plan"))
period = obj(plan.get("period"))
items, seen = [], set()
for entry in plan.get("items") or []:
    code = str(entry.get({spec.item_key_field!r}) or "").strip() if isinstance(entry, dict) else ""
    if not code or code in seen:
        continue
    seen.add(code)
    row = {{field: str(entry.get(field) or "") for field in {text_fields!r}}}
    if {channel_field!r}:
        row[{channel_field!r}] = row[{channel_field!r}].strip().lower()
    row.update({{"id": code, "stage": {spec.stages[0]!r}}})
    row.update({{field: str(period.get(source) or "") for field, source in {bounds!r}.items()}})
    items.append(row)
print(json.dumps({{"key": {period_key_prefix!r} + str(plan.get("key") or period.get({spec.period_fields[0]!r}) or ""),
                  "period": {{field: str(period.get(field) or "") for field in {list(spec.period_fields)!r}}},
                  "items": items}}))
'''
    after_plan = f"{item}s_to_schedule" if schedule else "done"
    plan_steps: list[dict[str, Any]] = [
        _start("plan", [_input("request", f"What this {period} should cover", required=True)]),
        _agent("plan", f"Plan the {period}", service_key, plan_prompt.strip() + "\n\nRequest: {{request}}" + plan_contract,
               "approve_plan" if approve_plan else "calendar_plan", output_schema=plan_schema),
    ]
    if approve_plan:
        plan_steps.append(_guarded(
            _approval("approve_plan", f"Confirm the {period} plan",
                      f"Approve this {period} plan, or send it back to revise?\n\n{{{{plan.summary}}}}", "calendar_plan",
                      revise_from="plan", asked=approve_plan),
            f"Nothing reaches the calendar, so nothing is made or published, until the person confirms the {period} plan."))
    plan_steps.append(_code("calendar_plan", f"Prepare the {period}'s {item}s", normalize, "calendar_plan",
                            f"claim_{period}"))
    plan_steps += calendar_plan_steps(spec, plan_var="calendar_plan", item_workflow=item_slug, then=after_plan,
                                      decision=_decision("plan_review", "approve_plan") if approve_plan else None)

    if make:
        file = spec.files[0]
        hook = calendar_producer_hook(
            spec, input_key=f"{item}_id", input_label=item.capitalize(),
            brief=tuple((field.replace("_", " ").capitalize(), field) for field in text_fields),
            then="make", done_next="done", made_files={file.field: "{{make.%s}}" % file.field},
            decision=_decision("made_review", "approve_made") if make.get("approve") else None,
        )
        make_steps: list[dict[str, Any]] = [
            _start(hook["entry"][0]["id"], [hook["run_input"], _input("request", "Anything to add")]),
            *hook["entry"],
            _agent("make", f"Make the {item}", str(make.get("service_key") or service_key),
                   str(make.get("prompt") or f"Make this {item}.").strip() + "\n\n{{request}}\n" + hook["brief"]
                   + f"\n\nSave the finished {item} as a file and return its document id as {file.field}.",
                   "approve_made" if make.get("approve") else hook["exit"][0]["id"], expects=["files"]),
        ]
        if make.get("approve"):
            make_steps.append(_approval("approve_made", f"Review the {item}", f"Approve this {item}?",
                                        hook["exit"][0]["id"], revise_from="make", asked=make["approve"]))
        make_steps += [*hook["exit"], _end()]
        workflows.append(_flow(make_slug, f"Make a {item}", f"Make one {item} of the calendar, or one asked for.",
                               make_steps))

    publish_slug = None
    if publish:
        publish_slug = f"{slug}_publish"
        ready = [spec.made_stage, *(["started"] if schedule else [])]
        flows = {}
        for row in channels:
            flows[row["channel"]] = f"{slug}_publish_{row['channel']}" if channel_field else publish_slug
            workflows.append(channel_publish_flow(
                slug=flows[row["channel"]], item_type=spec.item_type, item_schema=spec.item_schema(),
                input_key=f"{item}_id", service_key=str(row.get("service_key") or service_key), channel=row["channel"],
                prompt=str(row.get("prompt") or ""), ready_stages=ready, published_stage=published_stage,
                failed_stage=failed_stage, window_hours=int(row.get("window_hours") or 24),
                action_keys=list(row.get("action_keys") or ["social_post.publish"]),
                summary_fields=[field for field in text_fields if field != spec.item_key_field][:6],
                make_workflow=make_slug if make else None, unmade_stage=spec.stages[0] if make else None,
                requires=[*(row.get("requires") or []), *([spec.files[0].field] if make else [])], item_word=item,
                period={"start": "not_before", "end": "not_after", "word": period,
                        "timezone": str((schedule or {}).get("timezone") or "UTC")} if bounds else None,
            ))
        if channel_field:
            workflows.append(_channel_router(slug=publish_slug, spec=spec, channel_field=channel_field, flows=flows,
                                             failed_stage=failed_stage))

    if schedule:
        run_workflow = publish_slug or (make_slug if make else None)
        if run_workflow is None:
            raise ValueError("schedule needs make or publish: the Flow each due item starts")
        expansion = schedule_expansion_flows(
            slug=slug, item_type=spec.item_type, item_schema=spec.item_schema(), date_field=spec.date_field,
            timezone=str(schedule.get("timezone") or "UTC"), lead_minutes=int(schedule.get("lead_minutes") or 0),
            waiting_stages=[spec.stages[0], spec.made_stage], scheduled_stage="scheduled", started_stage="started",
            run_workflow=run_workflow, run_input=f"{item}_id", parent_field=spec.parent_field, item_word=item,
        )
        workflows += expansion["workflows"]
        automations += expansion["automations"]
        plan_steps += expansion_steps(expansion, parent_id="{{%s_project.project_id}}" % period, then="done",
                                      item_word=item)
    plan_steps.append(_end())
    workflows.insert(0, _flow(plan_slug, name, f"Plan a {period} of dated {item}s and put them on the calendar.",
                              plan_steps))
    _one_schema_per_project(workflows)
    return {"workflows": workflows, "stats": [], "automations": automations}


# ── Publishing on a fixed rhythm ────────────────────────────────────────────

def recurring_publish_flows(*, slug: str, name: str, service_key: str, make_prompt: str, channel: str, cron_expr: str,
                            timezone: str, publish_prompt: str = "", item_word: str = "item",
                            item_fields: list[str] | None = None, requires: list[str] | None = None,
                            window_hours: int = 24, action_keys: list[str] | None = None) -> dict[str, Any]:
    """One item made and published on each occurrence of a rhythm, with no plan to confirm.

    ``<slug>``, which an Automation runs on ``cron_expr``, keeps one project
    per scheduler occurrence (a replay finds it and makes nothing; manual
    launches without an occurrence share the current hour),
    asks an Agent to make the item, and runs ``<slug>_publish``
    (``channel_publish_flow``): the person approves it, then it is published
    with a verified receipt.
    """
    channel = _publish_channels({"channel": channel})[0]["channel"]
    item_type, publish_slug, input_key = f"{slug}_{item_word}", f"{slug}_publish", f"{item_word}_id"
    file_field = f"{item_word}_document_id"
    fields = [field for field in dict.fromkeys(item_fields or ["title"]) if field not in {"key", file_field}]
    schema = _object({"key": _TEXT, "due_at": _TEXT, **{field: _TEXT for field in dict.fromkeys(["title", *fields])},
                      file_field: _TEXT}, ["key"])
    occurrence = _OBJ + f'''from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo({timezone!r}))
except Exception:
    now = datetime.now(timezone.utc)
key = str(obj(inputs.get("start")).get("period_key") or inputs.get("period_key") or "").strip() or now.strftime("%Y-%m-%dT%H")
print(json.dumps({{"key": key, "at": now.isoformat(), "date": now.strftime("%Y-%m-%d %H:%M")}}))
'''
    make_steps = [
        _start("occurrence", [_input("request", "Anything to add")]),
        _code("occurrence", f"Work out which {item_word} this is", occurrence, "occurrence", "claim"),
        {"id": "claim", "type": "workflow_project", "name": f"Keep the {item_word}", "next": ["is_new"],
         "config": {"operation": "claim", "project_type": item_type, "project_key": f"{slug}:{{{{occurrence.key}}}}",
                    "current_stage": "making",
                    "state": {"key": "{{occurrence.key}}", "due_at": "{{occurrence.at}}",
                              "title": f"{item_word.capitalize()} · {{{{occurrence.date}}}}"},
                    "state_schema": schema, "schema_version": 1, "output_var": "kept"}},
        _guarded({"id": "is_new", "type": "condition", "name": f"The {item_word} is new", "next": [],
                  "config": {"expression": "kept.claimed == true"}, "true_next": ["make"], "false_next": ["seen"]},
                 f"One {item_word} per occurrence of the rhythm: replaying that occurrence makes nothing."),
        _agent("make", f"Make the {item_word}", service_key,
               make_prompt.strip() + "\n\n{{request}}\n\nThis is the " + item_word + " for {{occurrence.date}}. Save the "
               f"finished {item_word} as a file and return its document id as {file_field}, with "
               f"{', '.join(fields)}.", "made",
               output_schema=_object({**{field: _TEXT for field in fields}, file_field: _TEXT}, [*fields, file_field]),
               expects=["files"]),
        _patch("made", f"Keep the made {item_word}", item_type, schema, project="{{kept.project_id}}",
               revision="{{kept.revision}}", stage="made", allowed=["making", "made"],
               patch={**{field: "{{make.%s}}" % field for field in fields}, file_field: "{{make.%s}}" % file_field},
               nxt="publish"),
        {"id": "publish", "type": "subworkflow", "name": f"Publish the {item_word}", "next": ["done"],
         "config": {"workflow_id": publish_slug, "inputs": [{"key": input_key, "value": "{{kept.project_id}}"}],
                    "output_var": "publishing"}},
        _end(),
        {"id": "seen", "type": "end", "name": "Already made", "next": [],
         "config": {"completion_message": f"This occurrence's {item_word} is already made; nothing new was made."}},
    ]
    workflows = [
        _flow(slug, name, f"Runs on a schedule: makes one {item_word} and has it approved and published on {channel}.",
              make_steps),
        channel_publish_flow(
            slug=publish_slug, item_type=item_type, item_schema=schema, input_key=input_key, service_key=service_key,
            channel=channel, prompt=publish_prompt, ready_stages=["made"], published_stage="published",
            failed_stage="needs_attention", window_hours=int(window_hours),
            action_keys=list(action_keys or ["social_post.publish"]), summary_fields=fields[:6],
            requires=[*(requires or []), file_field], item_word=item_word),
    ]
    _one_schema_per_project(workflows)
    return {"workflows": workflows, "stats": [],
            "automations": [{"workflow": slug, "cron_expr": cron_expr, "timezone": timezone,
                             "why": f"makes each {item_word} on the rhythm the person asked for"}]}


# ── Report that records Stats ───────────────────────────────────────────────

def report_stat_flows(*, slug: str, name: str, service_key: str, prompt: str, stats: list[dict[str, Any]],
                      cron_expr: str, timezone: str, report_word: str = "report") -> dict[str, Any]:
    """A recurring report whose run records each Stat from the Workspace's evidence.

    ``stats`` are ``{key, name, rubric, unit?, maximum?}``: each becomes an
    ``agent_review`` Stat assessed by the report's ``assess`` step, which may
    record a number only from evidence it read (missing evidence is
    ``insufficient_evidence``, never zero). The ``write`` step then saves the
    report as a file.
    """
    keys = [str(stat["key"]) for stat in stats]
    definitions = []
    for stat in stats:
        scale: dict[str, Any] = {"minimum": 0}
        if stat.get("maximum") is not None:
            scale["maximum"] = stat["maximum"]
        definitions.append({
            "key": str(stat["key"]), "name": str(stat.get("name") or stat["key"]),
            "description": str(stat["rubric"]), "value_type": "number", "unit": stat.get("unit") or "count",
            "window": "latest", "collector_type": "agent_review", "goal_eligible": True,
            "collector_config": {"workflow_slug": slug, "assessment": {
                "subject": "workspace", "rubric": str(stat["rubric"]), "scale": scale, "missing_evidence": "unknown"}},
        })
    assess_prompt = (
        f"{prompt.strip()}\n\nRead get_stat_status, then the Workspace's evidence for this {report_word} "
        f"(its files, projects and results). For each of {', '.join(keys)}, follow its rubric and record it "
        "with record_stat_assessment, using this run's id plus the metric key as the idempotency key. A number "
        "needs evidence you read; when there is none, record insufficient_evidence with value null and the gaps."
    )
    write_prompt = (
        f"Write the {report_word} from what was just recorded and the evidence behind it: the numbers with what "
        "changed, what worked, what to do next, and the gaps. Save it as a file."
    )
    steps = [
        _start("assess", []),
        {"id": "assess", "type": "agent", "name": "Record the numbers", "next": ["write"],
         "config": {"service_key": service_key, "prompt": assess_prompt, "stat_assessment_keys": keys}},
        _agent("write", f"Write the {report_word}", service_key, write_prompt, "done", expects=["files"]),
        _end(),
    ]
    return {
        "workflows": [_flow(slug, name, f"Record this period's numbers and write the {report_word}.", steps)],
        "stats": definitions,
        "automations": [{"workflow": slug, "cron_expr": cron_expr, "timezone": timezone,
                         "why": f"writes the {report_word} and records its numbers"}],
    }


# ── Project tree ────────────────────────────────────────────────────────────

def project_tree_flows(*, slug: str, name: str, parent_type: str, child_type: str, plan_service_key: str,
                       plan_prompt: str, make_service_key: str, make_prompt: str, parent_word: str = "project",
                       child_word: str = "part", child_fields: list[str] | None = None, approve_plan: bool | str = True,
                       approve_made: bool | str = False) -> dict[str, Any]:
    """A parent project that plans its parts, and a Flow that makes one part inside it.

    ``<slug>_plan`` saves the plan as a ``parent_type`` project with a folder
    and its planned ``parts`` list; ``<slug>_make`` makes one planned part
    (hidden inputs: the parent and the part's code) as a ``child_type``
    project in a folder under the parent's, then marks it made in the plan.
    This is the shape the App's project tree lays out.
    """
    fields = [field for field in (child_fields or ["title", "summary"]) if field not in {"code"}]
    part = _object({"code": _TEXT, **{field: _TEXT for field in fields}}, ["code", "title"] if "title" in fields else ["code"])
    parent_schema = _object({"title": _TEXT, "summary": _TEXT, "project_root": _TEXT, "history": _HISTORY,
                             "parts": {"type": "array", "items": part}}, ["title"])
    child_schema = _object({"parent_project_id": _TEXT, "code": _TEXT, "project_root": _TEXT, "history": _HISTORY,
                            "result_document_id": _TEXT, **{field: _TEXT for field in fields}}, ["parent_project_id", "code"])
    plan_contract = (
        f"\n\nReturn JSON: key (stable id of this {parent_word}), title, summary (for the person to confirm), and "
        f"parts, one per {child_word} to make, each with a unique code (E01, E02 ...) and {', '.join(fields)}."
    )
    plan_steps = [
        _start("plan", [_input("request", f"What this {parent_word} is", required=True)]),
        _agent("plan", f"Plan the {parent_word}", plan_service_key,
               plan_prompt.strip() + "\n\nRequest: {{request}}" + plan_contract,
               "approve_plan" if approve_plan else "save_plan",
               output_schema=_object({"key": _TEXT, "title": _TEXT, "summary": _TEXT,
                                      "parts": {"type": "array", "items": part}}, ["key", "title", "summary", "parts"])),
    ]
    if approve_plan:
        plan_steps.append(_approval("approve_plan", f"Confirm the {parent_word} plan",
                                    f"Approve this plan, or send it back to revise?\n\n{{{{plan.summary}}}}", "save_plan",
                                    revise_from="plan", asked=approve_plan))
    plan_steps.append(
        {"id": "save_plan", "type": "workflow_project", "name": f"Save the {parent_word}",
         "next": ["keep_decision" if approve_plan else "done"],
         "config": {"operation": "claim", "project_type": parent_type, "project_key": "{{plan.key}}",
                    "current_stage": "planned", "folder": {"name": "{{plan.title}}"},
                    "state": {"title": "{{plan.title}}", "summary": "{{plan.summary}}", "parts": "{{plan.parts}}"},
                    "state_schema": parent_schema, "schema_version": 1, "output_var": "parent"}})
    if approve_plan:
        plan_steps.append(_patch("keep_decision", "Keep the decision", parent_type, parent_schema,
                                 project="{{parent.project_id}}", revision="{{parent.revision}}", stage=None, allowed=[],
                                 patch={}, nxt="done", decision=_decision("plan_review", "approve_plan")))
    plan_steps.append(_end())
    parent_input, code_input = f"{parent_type}_id", f"{child_word}_code"
    make_steps = [
        _start("parent", [_picker(parent_input, parent_word.capitalize(), parent_type),
                          _input(code_input, f"{child_word.capitalize()} code", hidden=True),
                          _input("request", "Anything to add")]),
        {"id": "parent", "type": "workflow_project", "name": f"Open the {parent_word}", "next": ["part_spec"],
         "config": {"operation": "get", "project_type": parent_type, "project_id": "{{%s}}" % parent_input,
                    "state_schema": parent_schema, "schema_version": 1, "output_var": "parent"}},
        _code("part_spec", f"Find the planned {child_word}", _OBJ + f'''parent = obj(inputs.get("parent"))
code = str(inputs.get({code_input!r}) or "")
planned = next((row for row in obj(parent.get("state")).get("parts") or [] if isinstance(row, dict) and str(row.get("code")) == code), {{}})
print(json.dumps({{"code": code, **{{field: str(planned.get(field) or "") for field in {fields!r}}}}}))
''', "part_spec", "claim_part"),
        {"id": "claim_part", "type": "workflow_project", "name": f"Start the {child_word}", "next": ["make"],
         "config": {"operation": "claim", "project_type": child_type,
                    "project_key": "{{parent.project_id}}:{{part_spec.code}}", "current_stage": "in_production",
                    "folder": {"name": "{{part_spec.code}}", "under": "{{parent.state.project_root}}"},
                    "state": {"parent_project_id": "{{parent.project_id}}", "code": "{{part_spec.code}}",
                              **{field: "{{part_spec.%s}}" % field for field in fields}},
                    "state_schema": child_schema, "schema_version": 1, "output_var": "part"}},
        _agent("make", f"Make the {child_word}", make_service_key,
               make_prompt.strip() + f"\n\n{parent_word.capitalize()}: {{{{parent.state.title}}}}\n"
               f"{child_word.capitalize()}: {{{{part_spec}}}}\n{{{{request}}}}\n\nSave the finished {child_word} in "
               "{{part.state.project_root}} and return its document id as result_document_id.",
               "approve_made" if approve_made else "made", expects=["files"]),
    ]
    if approve_made:
        make_steps.append(_approval("approve_made", f"Review the {child_word}", f"Approve this {child_word}?",
                                    "made", revise_from="make", asked=approve_made))
    make_steps += [
        _patch("made", f"Mark the {child_word} made", child_type, child_schema, project="{{part.project_id}}",
               revision="{{part.revision}}", stage="made", allowed=["in_production", "made"],
               patch={"result_document_id": "{{make.result_document_id}}"}, nxt="plan_updated",
               decision=_decision("made_review", "approve_made") if approve_made else None),
        {"id": "plan_updated", "type": "workflow_project", "name": f"Mark it made in the plan", "next": ["done"],
         "config": {"operation": "patch", "project_type": parent_type, "project_id": "{{parent.project_id}}",
                    "expected_revision": "{{parent.revision}}", "current_stage": "planned",
                    "list_upserts": [{"path": "parts", "key": "code", "item": {
                        "code": "{{part_spec.code}}", "status": "made", "project_id": "{{part.project_id}}"}}],
                    "patch": {}, "state_schema": parent_schema, "schema_version": 1, "output_var": "plan_updated"}},
        _end(),
    ]
    return {
        "workflows": [
            _flow(f"{slug}_plan", name, f"Plan a {parent_word} and the {child_word}s to make in it.", plan_steps),
            _flow(f"{slug}_make", f"Make a {child_word}", f"Make one planned {child_word} of a {parent_word}.", make_steps),
        ],
        "stats": [],
        "automations": [],
    }


# ── Watching a connected source ─────────────────────────────────────────────

@dataclass(frozen=True)
class SourceFeed:
    """A read-only call that lists what is in an account, so looking at it needs no model."""

    provider: str
    label: str
    tool: str
    items: str
    # The source and item words this call answers: Outlook's mail, not its calendar.
    about: str
    args: dict[str, Any] = field(default_factory=dict)
    key: str = "id"


_MAIL = r"e-?mail|mail|inbox|message"
SOURCE_FEEDS = (
    SourceFeed("gmail", "Gmail", "mcp__gmail__list_messages", "messages", rf"\bgmail\b.*\b({_MAIL})|\b({_MAIL})s?\b.*\bgmail\b",
               {"query": "in:inbox newer_than:2d", "max_results": 25, "include_details": True}),
    # Only what tells one message from another: its reply is cut at 12,000 characters.
    SourceFeed("outlook", "Outlook", "mcp__outlook__list_messages", "value", rf"\boutlook\b.*\b({_MAIL})|\b({_MAIL})s?\b.*\boutlook\b",
               {"top": 25, "select": "id,subject,from,receivedDateTime"}),
    SourceFeed("facebook", "Instagram comments", "mcp__facebook__list_recent_instagram_comments", "comments",
               r"\binstagram\b.*\bcomments?\b|\bcomments?\b.*\binstagram\b"),
    SourceFeed("linkedin", "LinkedIn comments", "mcp__linkedin__list_recent_post_comments", "comments",
               r"\blinkedin\b.*\bcomments?\b|\bcomments?\b.*\blinkedin\b"),
)
# Every id a watch remembers; older ones have left the feed's window by then.
_SEEN_LIMIT = 500


def source_feed_for(source: str, item_word: str) -> SourceFeed | None:
    """The feed that lists these things, if any: a source with none is read by its Agent."""
    words = f"{source} {item_word}".lower()
    return next((feed for feed in SOURCE_FEEDS if re.search(feed.about, words)), None)


def _source_read_steps(feed: SourceFeed, *, slug: str, source: str, item_word: str) -> list[dict[str, Any]]:
    """Read the account and compare it with the ids seen before; up to ``has_new`` no model or sandbox runs."""
    seen_schema = _object({"seen": {"type": "array", "items": _TEXT}}, ["seen"])
    return [
        {"id": "cursor", "type": "workflow_project", "name": f"Find the {item_word}s seen before", "next": ["read"],
         "config": {"operation": "claim", "project_type": f"{slug}_seen", "project_key": f"{slug}:seen",
                    "current_stage": "watching", "state": {"seen": []}, "state_schema": seen_schema,
                    "schema_version": 1, "output_var": "cursor"}},
        {"id": "read", "type": "tool", "name": f"Read {source}", "next": ["read_it"],
         # Connected is not watched: the default account only, not every account its owner has.
         "config": {"tool": feed.tool, "args": dict(feed.args),
                    "new_items": {"items": feed.items, "key": feed.key, "seen": "{{cursor.state.seen}}",
                                  "limit": _SEEN_LIMIT},
                    "output_var": "read", "output_format": "json", "on_error": "continue"}},
        {"id": "read_it", "type": "condition", "name": f"{source} could be read", "next": [],
         "config": {"expression": "read.could_read == true"}, "true_next": ["has_new"], "false_next": ["unread"]},
        {"id": "has_new", "type": "condition", "name": f"Something new in {source}", "next": [],
         "config": {"expression": "read.has_new == true"}, "true_next": ["find"], "false_next": ["nothing_new"]},
        _end("nothing_new", "Nothing new"),
    ]


def source_watch_flows(*, slug: str, name: str, service_key: str, source: str, prompt: str, item_word: str,
                       item_fields: list[str], run_workflow: str, run_input: str, cron_expr: str,
                       timezone: str, feed: SourceFeed | None = None) -> dict[str, Any]:
    """Start ``run_workflow`` once for each new thing in a connected account.

    No channel hands a new email or comment to a Workflow, so a Workflow for
    one starts here: ``<slug>_watch``, which an Automation runs, finds what is
    new in ``source``; ``<slug>_one`` (internal) claims each by the id the
    source gives it, so one seen before never starts twice, and runs
    ``run_workflow`` with that id as ``run_input``. Each thing stays as a
    project with how it ended.

    With a ``feed`` a look reads the account itself and calls the Agent only
    with what it has not seen, so a look with nothing new costs no model call.
    Without one the Agent does the looking, on every run.
    """
    fields = [field for field in dict.fromkeys(item_fields) if field != "key"]
    item_type = f"{slug}_{item_word}"
    schema = _object({"key": _TEXT, "handle_issue": _TEXT, **{field: _TEXT for field in fields}}, ["key"])
    found = _object({"could_read": {"type": "boolean"},
                     "items": {"type": "array", "items": _object(
                         {"key": _TEXT, **{field: _TEXT for field in fields}}, ["key"])}},
                    ["could_read", "items"])
    find_prompt = (
        f"{prompt.strip()}\n\nLook in {source} for each new {item_word}. Only read: answer nothing and change nothing. "
        f"Return could_read (false when {source} could not be read) and items, one per {item_word}: key (the id "
        f"{source} gives it, exactly), {', '.join(fields) or 'nothing else'}. Return no items when nothing is new."
    )
    if feed is not None:
        find_prompt = (
            f"{prompt.strip()}\n\nThese {item_word}s are new in {source}:\n{{{{read.items}}}}\n\nOnly read: answer "
            f"nothing and change nothing. Return could_read true and items, one for each of these this Workspace "
            f"handles: key (its id, exactly), {', '.join(fields) or 'nothing else'}. Return no items when none of them is."
        )
    eligible = ""
    if feed is not None:
        eligible = f'eligible = {{row[{feed.key!r}] for row in obj(inputs.get("read")).get("items", [])}}\n'
    eligible_check = " and key in eligible" if feed is not None else ""
    keep_new = _OBJ + eligible + f'''items, seen = [], set()
for entry in obj(inputs.get("find")).get("items") or []:
    key = str(entry.get("key") or "").strip() if isinstance(entry, dict) else ""
    if key and key not in seen{eligible_check}:
        seen.add(key)
        items.append({{"key": key, **{{field: str(entry.get(field) or "") for field in {fields!r}}}}})
print(json.dumps({{"items": items}}))
'''
    if feed is None:
        looking = [
            _start("find", []),
            _agent("find", f"Look for new {item_word}s", service_key, find_prompt, "read_it", output_schema=found),
            {"id": "read_it", "type": "condition", "name": f"{source} could be read", "next": [],
             "config": {"expression": "find.could_read == true"}, "true_next": ["new_items"], "false_next": ["unread"]},
            _code("new_items", f"List the new {item_word}s", keep_new, "new_items", "each"),
        ]
    else:
        remember = _patch("remember", f"Remember the {item_word}s seen", f"{slug}_seen",
                          _object({"seen": {"type": "array", "items": _TEXT}}, ["seen"]), project="{{cursor.project_id}}",
                          revision="{{cursor.revision}}", stage=None, allowed=[], patch={"seen": "{{read.seen}}"}, nxt="done")
        # A cursor conflict can repeat a classification; the child claim still starts each item once.
        remember["config"]["on_error"] = "continue"
        looking = [
            _start("cursor", []),
            *_source_read_steps(feed, slug=slug, source=source, item_word=item_word),
            _agent("find", f"Pick the new {item_word}s to handle", service_key, find_prompt, "classified", output_schema=found),
            {"id": "classified", "type": "condition", "name": f"The new {item_word}s were classified", "next": [],
             "config": {"expression": "find.could_read == true"}, "true_next": ["new_items"], "false_next": ["unread"]},
            _code("new_items", f"List the new {item_word}s", keep_new, "new_items", "each"),
            remember,
        ]
    watch = _flow(f"{slug}_watch", name, f"Runs on a schedule: looks in {source} for each new {item_word} and starts its Flow.", [
        *looking,
        {"id": "each", "type": "foreach_subworkflow", "name": f"Start each new {item_word}",
         "next": ["remember" if feed is not None else "done"],
         "config": {"over": "{{new_items.items}}", "item_key": "key", "item_var": "item", "output_var": "started",
                    "concurrency": 3, "workflow_id": f"{slug}_one", "continue_on_failure": feed is None}},
        {"id": "unread", "type": "end", "name": "Could not look", "next": [],
         "config": {"completion_message": f"{source} could not be read; it is looked at again on the next run."}},
        _end(),
    ])
    one = _flow(f"{slug}_one", f"Start one {item_word}", f"Start the Flow of one new {item_word}, once.", [
        _start("claim", [_input("item", item_word.capitalize(), required=True, kind="json")]),
        {"id": "claim", "type": "workflow_project", "name": f"Keep the {item_word}", "next": ["is_new"],
         "config": {"operation": "claim", "project_type": item_type, "project_key": f"{slug}:{{{{item.key}}}}",
                    "current_stage": "started", "state": {"key": "{{item.key}}",
                                                          **{field: "{{item.%s}}" % field for field in fields}},
                    "state_schema": schema, "schema_version": 1, "output_var": "kept"}},
        _guarded({"id": "is_new", "type": "condition", "name": f"The {item_word} is new", "next": [],
                  "config": {"expression": "kept.claimed == true"}, "true_next": ["run"], "false_next": ["seen"]},
                 f"Each {item_word} starts once, by the id {source} gives it, whatever else is still waiting."),
        {"id": "run", "type": "subworkflow", "name": f"Handle the {item_word}", "next": ["handled"],
         "config": {"workflow_id": run_workflow, "inputs": [{"key": run_input, "value": "{{item.key}}"}],
                    "output_var": "handling", "on_error": "continue_error", "error_next": ["not_handled"]}},
        _patch("handled", f"Mark the {item_word} handled", item_type, schema, project="{{kept.project_id}}",
               revision="{{kept.revision}}", stage="handled", allowed=["started", "handled"], patch={}, nxt="done"),
        _patch("not_handled", f"Keep that the {item_word} needs attention", item_type, schema,
               project="{{kept.project_id}}", revision="{{kept.revision}}", stage="needs_attention",
               allowed=["started", "needs_attention"],
               patch={"handle_issue": f"Its Flow did not finish; this {item_word} is not picked up again."}, nxt="done"),
        _end("seen", "Seen before"),
        _end(),
    ], internal=True)
    return {"workflows": [watch, one], "stats": [],
            "automations": [{"workflow": watch["slug"], "cron_expr": cron_expr, "timezone": timezone,
                             "why": f"looks in {source} for each new {item_word}"}]}


def summarize_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    """What a pattern made, compact: the designer reads this, not every step."""
    return {
        "slug": workflow["slug"], "name": workflow.get("name"), "internal": bool(workflow.get("internal")),
        "inputs": [row.get("key") for row in workflow.get("run_inputs") or
                   next((step.get("config", {}).get("run_inputs") or [] for step in workflow["steps"]
                         if step.get("type") == "trigger"), [])],
        "steps": [f"{step['id']}:{step['type']}" for step in workflow["steps"]],
        "agent_steps": {step["id"]: step["config"].get("service_key") for step in workflow["steps"]
                        if step.get("type") == "agent"},
        "projects": sorted({step["config"].get("project_type") for step in workflow["steps"]
                            if step.get("type") == "workflow_project"}),
    }


def review_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    """``summarize_workflow`` plus what a reviewer would otherwise ask for: what its steps enforce and ask."""
    steps = [step for step in workflow["steps"] if isinstance(step, dict)]
    guards = [(step.get("meta") or {}).get("guard") for step in steps]
    asks = {step["id"]: str((step.get("config") or {}).get("message") or "") for step in steps
            if step.get("type") == "wait"}
    return {**summarize_workflow(workflow), **({"guards": [guard for guard in guards if guard]} if any(guards) else {}),
            **({"approvals": asks} if asks else {})}


# ── Record collection ───────────────────────────────────────────────────────

# What each kind of field is stored as, and asked with.
_RECORD_KINDS: dict[str, dict[str, Any]] = {
    "text": {"type": "string", "maxLength": 300},
    "long_text": {"type": "string", "maxLength": 4000, "x-ui": {"control": "textarea"}},
    "number": {"type": "number"},
    "money": {"type": "number", "minimum": 0},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "picture": {"type": "array", "maxItems": 1, "items": {"type": "object"},
                "x-ui": {"control": "documents", "accept": "image/*"}},
}


@dataclass(frozen=True)
class RecordField:
    """One field of a collection's records; ``choices`` (value -> words) makes it a fixed choice."""
    key: str
    label: str
    kind: str = "text"
    required: bool = False
    choices: tuple[tuple[str, str], ...] = ()
    #: For kind ``records``: the records it picks (a promotion's dishes).
    project_type: str = ""


def _record_schema(record: RecordField) -> dict[str, Any]:
    """The field's schema; ``x-record-kind`` tells a page how to show it (a price as money, a photo as a picture)."""
    if record.choices:
        return {"type": "string", "enum": [value for value, _ in record.choices],
                "x-enum-labels": dict(record.choices), "x-record-kind": "choice"}
    if record.kind == "records":
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", record.project_type):
            raise ValueError(f"field {record.key!r} of kind records needs the project_type of the records it picks")
        return {"type": "array", "items": {"type": "string"}, "x-record-kind": "records",
                "x-ui": {"control": "workflow_project_select", "project_type": record.project_type}}
    if record.kind not in _RECORD_KINDS:
        raise ValueError(f"field {record.key!r} kind must be one of {', '.join(_RECORD_KINDS)}, not {record.kind!r}")
    return {**_RECORD_KINDS[record.kind], "x-record-kind": record.kind}


def record_collection_flows(*, slug: str, item_type: str, item_word: str, fields: list[RecordField], title_field: str,
                            live_stage: str = "active", off_stage: str = "retired", live_words: str = "Active",
                            off_words: str = "Taken off", then_workflow: str = "",
                            then_input: str = "") -> dict[str, Any]:
    """Add, change and take off the records of one collection, each a Workflow a page's buttons open.

    Every record is a project of ``item_type`` whose ``display`` names it by ``title_field``, says where it
    stands and shows its picture. Changing one opens a form holding its stored values (from the App or
    chat alike); a field left out keeps its value. Taking one off moves it to ``off_stage``.
    ``then_workflow`` runs right after a record is added, with its id as ``then_input``: what adding one
    also makes (a promotion's poster and copy) is made then, not from a second button.
    """
    if bool(then_workflow) != bool(then_input):
        raise ValueError("then_workflow and then_input go together: the Flow to run and its input for the record id")
    keys = [record.key for record in fields]
    if not fields or len(set(keys)) != len(keys) or not all(re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key) for key in keys):
        raise ValueError("fields must be distinct snake_case keys")
    if title_field not in keys:
        raise ValueError(f"title_field {title_field!r} must be one of the fields")
    pictures = [record.key for record in fields if record.kind == "picture" and not record.choices]
    properties = {record.key: _record_schema(record) for record in fields}
    schema = {"type": "object", "properties": {**properties, "display": {"type": "object"}}}
    stages = [live_stage, off_stage]
    item = item_word.capitalize()

    def display(status: str | None, prefix: str = "") -> dict[str, Any]:
        return {"title": "{{%s%s}}" % (prefix, title_field), **({"status": status} if status else {}),
                **({"media": "{{%s%s}}" % (prefix, pictures[0])} if pictures else {})}

    def rows() -> list[dict[str, Any]]:
        # A file is asked as json with the documents control in its schema, which the run form reads.
        return [{"key": record.key, "label": record.label,
                 "type": "json" if _record_schema(record)["type"] == "array" else _record_schema(record)["type"],
                 "required": record.required, "hidden": False, "schema": _record_schema(record)}
                for record in fields]

    values = {record.key: "{{%s}}" % record.key for record in fields}
    # A change opens a form already holding the record's values; what the person leaves out keeps its value.
    changes = {"type": "object", "properties": {
        record.key: {**_record_schema(record), "title": record.label, "default": "{{item.state.%s}}" % record.key}
        for record in fields}}
    add = _flow(f"{slug}_add", f"Add a {item_word}", f"Add a {item_word} with its details.", [
        _start("save", rows()),
        {"id": "save", "type": "workflow_project", "name": f"Save the {item_word}", "next": ["then" if then_workflow else "done"],
         "config": {"operation": "create", "project_type": item_type, "schema_version": 1, "state_schema": schema,
                    "current_stage": live_stage, "allowed_stages": stages,
                    "state": {**values, "display": display(live_words)}, "output_var": "save"}},
        *([{"id": "then", "type": "subworkflow", "name": f"Make what a new {item_word} needs", "next": ["done"],
            "config": {"workflow_id": then_workflow, "inputs": [{"key": then_input, "value": "{{save.project_id}}"}],
                       "output_var": "made"}}] if then_workflow else []),
        _end(),
    ])
    picker = _picker("item_id", item, item_type)
    picker["required"] = True
    picker.pop("default", None)
    edit = _flow(f"{slug}_edit", f"Change a {item_word}", f"Change a {item_word}'s details.", [
        _start("item", [picker]),
        {"id": "item", "type": "workflow_project", "name": f"Open the {item_word}", "next": ["changes"],
         "config": {"operation": "get", "project_type": item_type, "project_id": "{{item_id}}",
                    "state_schema": schema, "schema_version": 1, "output_var": "item"}},
        {"id": "changes", "type": "wait", "name": f"Change the {item_word}", "next": ["save"],
         "config": {"wait_type": "input", "message": f"Change what this {item_word} says.",
                    "response_variable": "changes", "input_schema": changes}},
        _patch("save", f"Save the changes", item_type, schema, project="{{item_id}}", revision="{{item.revision}}",
               stage=None, allowed=stages,
               patch={**{record.key: "{{changes.%s}}" % record.key for record in fields},
                      "display": display(None, "changes.")}, nxt="done"),
        _end(),
    ])
    take_off = _flow(f"{slug}_take_off", f"Take a {item_word} off", f"Take a {item_word} off; it keeps its details.", [
        _start("item", [picker]),
        {"id": "item", "type": "workflow_project", "name": f"Open the {item_word}", "next": ["save"],
         "config": {"operation": "get", "project_type": item_type, "project_id": "{{item_id}}",
                    "state_schema": schema, "schema_version": 1, "output_var": "item"}},
        _patch("save", f"Take it off", item_type, schema, project="{{item_id}}", revision="{{item.revision}}",
               stage=off_stage, allowed=stages, patch={"display": {"status": off_words}}, nxt="done"),
        _end(),
    ])
    return {"workflows": [add, edit, take_off], "stats": [], "automations": []}



# The collection's arguments, as both the Workflows designer and the Architect take them.
RECORD_COLLECTION_PARAMETERS: dict[str, Any] = {
    "slug": {"type": "string"}, "item_project_type": {"type": "string"},
    "item_word": {"type": "string", "description": "e.g. dish, product, offer"},
    "title_field": {"type": "string", "description": "The field that names a record, e.g. name"},
    "fields": {"type": "array", "description": "The record's fields, in the order the form asks them",
               "items": {"type": "object", "properties": {
                   "key": {"type": "string"}, "label": {"type": "string"}, "required": {"type": "boolean"},
                   "kind": {"type": "string", "enum": sorted([*_RECORD_KINDS, "records"])},
                   "project_type": {"type": "string", "description": "kind records: the records it picks, e.g. dish"},
                   "choices": {"type": "array", "description": "A fixed choice (spice, category): each value and its words",
                               "items": {"type": "object", "properties": {"value": {"type": "string"},
                                                                          "label": {"type": "string"}}}}},
                   "required": ["key", "label"]}},
    "live_stage": {"type": "string"}, "off_stage": {"type": "string"},
    "live_words": {"type": "string", "description": "Where a live record stands, in the person's words: On the menu"},
    "off_words": {"type": "string", "description": "Where a taken-off one stands: Off the menu"},
    "then_workflow": {"type": "string", "description": "A Flow of this draft to run right after a record is added, "
                                                       "when adding one also makes something (a promotion's poster "
                                                       "and copy). Write it first; it takes the record id"},
    "then_input": {"type": "string", "description": "then_workflow's run input that takes the record id"},
}
RECORD_COLLECTION_REQUIRED = ["slug", "item_project_type", "item_word", "title_field", "fields"]


def record_collection_flows_from_args(args: dict[str, Any]) -> dict[str, Any]:
    """``record_collection_flows`` from a tool call's arguments (RECORD_COLLECTION_PARAMETERS)."""
    def text(name: str, default: str = "") -> str:
        return str(args.get(name) or default).strip()

    slug = text("slug")
    fields = [RecordField(
        key=str(row.get("key") or ""), label=str(row.get("label") or row.get("key") or ""),
        kind=str(row.get("kind") or "text"), required=bool(row.get("required")),
        choices=tuple((str(choice.get("value")), str(choice.get("label") or choice.get("value")))
                      for choice in row.get("choices") or [] if isinstance(choice, dict) and choice.get("value")),
        project_type=str(row.get("project_type") or "").strip())
        for row in args.get("fields") or [] if isinstance(row, dict)]
    return record_collection_flows(
        slug=slug, item_type=text("item_project_type", f"{slug}_item"), item_word=text("item_word", "item"),
        fields=fields, title_field=text("title_field"),
        live_stage=text("live_stage", "active"), off_stage=text("off_stage", "retired"),
        live_words=text("live_words", "Active"), off_words=text("off_words", "Taken off"),
        then_workflow=text("then_workflow"), then_input=text("then_input"),
    )


# The watch's arguments, as both the Workflows designer and the Architect take them.
SOURCE_WATCH_PARAMETERS: dict[str, Any] = {
    "slug": {"type": "string"}, "name": {"type": "string"},
    "service_key": {"type": "string", "description": "Service whose Agent can read the source"},
    "source": {"type": "string", "description": "e.g. the Gmail inbox, comments on the Instagram posts. Name the "
                                                "platform: Gmail, Outlook, Instagram comments and LinkedIn "
                                                "comments are read directly"},
    "prompt": {"type": "string", "description": "What counts: which new things this Workspace handles"},
    "item_word": {"type": "string", "description": "e.g. email, comment"},
    "item_fields": {"type": "array", "items": {"type": "string"},
                    "description": "Text fields kept for the person besides its id, e.g. sender, subject"},
    "run_workflow": {"type": "string"}, "run_input": {"type": "string"},
    "cron_expr": {"type": "string", "description": "How often to look: what the person chose, else every hour. "
                                                  "A source read directly calls the Agent only when something is new; "
                                                  "any other runs it on every look"},
    "timezone": {"type": "string"},
}
SOURCE_WATCH_REQUIRED = ["slug", "service_key", "source", "prompt", "item_word", "run_workflow", "run_input", "timezone"]


def source_watch_flows_from_args(args: dict[str, Any]) -> dict[str, Any]:
    """``source_watch_flows`` from a tool call's arguments (SOURCE_WATCH_PARAMETERS)."""
    from packages.core.services.integration_service import coming_soon_servers

    def text(name: str, default: str = "") -> str:
        return str(args.get(name) or default).strip()

    item, source = text("item_word", "item"), text("source")
    feed = source_feed_for(source, item)
    if feed is not None and feed.provider in coming_soon_servers():
        feed = None
    return source_watch_flows(
        slug=text("slug"), name=text("name", f"Look for new {item}s"), service_key=text("service_key"),
        source=source, prompt=text("prompt"), item_word=item,
        item_fields=[str(value).strip() for value in args.get("item_fields") or [] if str(value or "").strip()],
        run_workflow=text("run_workflow"), run_input=text("run_input"),
        cron_expr=text("cron_expr", "0 * * * *"), timezone=text("timezone", "UTC"), feed=feed,
    )
