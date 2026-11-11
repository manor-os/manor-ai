"""The Automations design sub-agent: designs what a Workspace runs on a clock.

It schedules what the Workflows need (a report that records Stats, a worker
that starts due items -- ``automations_needed``, which accepting the
Workflows design keeps) and every routine the person named. A cadence is one
of a few checked options (every N minutes, daily, weekdays, weekly, monthly,
or the cadence a Workflow pattern asked for), turned into the scheduler's
fields here; the designer writes no cron of its own. It is the
``automations`` part of the part designer registry
(``workspace_part_designers``).
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.subagents import WORKSPACE_AUTOMATIONS_DESIGNER_SUBAGENT, SubAgentSpec
from packages.core.services.workspace_part_designers import PARTS_COMMAND_TOOL, PartDesigner

# Draft field: the schedules accepted Workflows need, until an Automation runs each.
NEEDED_FIELD = "_automations_needed"
CADENCES = ("every_minutes", "daily", "weekdays", "weekly", "monthly", "as_needed")
MAX_AUTOMATIONS = 12
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass
class AutomationsDesignContext:
    """What one Workspace has for designing its Automations, in portable keys."""

    services: list[str] = field(default_factory=list)
    workflows: list[dict[str, Any]] = field(default_factory=list)
    automations: list[dict[str, Any]] = field(default_factory=list)
    scheduled_jobs: list[dict[str, Any]] = field(default_factory=list)
    needed: list[dict[str, Any]] = field(default_factory=list)
    spec: dict[str, Any] = field(default_factory=dict)
    timezone: str = "UTC"

    @classmethod
    def from_draft_fields(cls, fields: dict[str, Any]) -> "AutomationsDesignContext":
        from packages.core.services.workspace_build_spec import spec_slice

        spec = spec_slice(fields, "automations")
        return cls(
            services=[str(row["service_key"]) for row in fields.get("services") or []
                      if isinstance(row, dict) and row.get("service_key")],
            workflows=[copy.deepcopy(row) for row in fields.get("workflows") or [] if isinstance(row, dict) and row.get("slug")],
            automations=[copy.deepcopy(row) for row in fields.get("automations") or [] if isinstance(row, dict)],
            scheduled_jobs=[copy.deepcopy(row) for row in fields.get("scheduled_jobs") or [] if isinstance(row, dict)],
            needed=[copy.deepcopy(row) for row in fields.get(NEEDED_FIELD) or [] if isinstance(row, dict)],
            spec=spec, timezone=str(spec.get("timezone") or "UTC"),
        )

    def scheduled_workflows(self) -> set[str]:
        return {str((row.get("execution_target") or {}).get("workflow_slug") or "") for row in self.scheduled_jobs} - {""}

    def summary(self) -> dict[str, Any]:
        return {
            "timezone": self.timezone, "services": self.services,
            "workflows": [{"slug": row["slug"], "name": row.get("name"), "internal": bool(row.get("internal")),
                           "needs_input": _required_inputs(row)} for row in self.workflows],
            "needed": self.needed, "planned": self.spec.get("automations") or [],
            "routines": self.spec.get("routines") or [],
            "current": [{"automation_key": row.get("automation_key"), "trigger": row.get("trigger"),
                         "cron_expr": row.get("cron_expr"), "timezone": row.get("timezone")} for row in self.automations],
        }

    def check(self, value: Any) -> tuple[dict[str, Any] | None, list[str], list[str]]:
        """Check a candidate {automations: [{key, description, workflow | service_key, cadence, timezone}]}."""
        from packages.core.ai.tools.workspace_arch_tools import _native_scheduled_job
        from packages.core.blueprints.installer import validate_portable_recipe_resources
        from packages.core.schedule_cadence import normalize_timezone, validate_cron_expression

        if not isinstance(value, dict) or not isinstance(value.get("automations"), list):
            return None, ["the candidate must be an object with automations"], []
        rows = [row for row in value["automations"] if isinstance(row, dict)]
        errors: list[str] = []
        if len(rows) > MAX_AUTOMATIONS:
            errors.append(f"at most {MAX_AUTOMATIONS} Automations")
        workflows = {row["slug"]: row for row in self.workflows}
        needed = {str(row.get("workflow")): row for row in self.needed}
        keys: list[str] = []
        normalized = []
        for row in rows:
            key = str(row.get("key") or "").strip()
            where = f"automations.{key or '?'}"
            if not _KEY.match(key):
                errors.append(f"{where}: key must be snake_case")
            elif key in keys:
                errors.append(f"{where}: listed twice")
            keys.append(key)
            workflow, service = str(row.get("workflow") or "").strip(), str(row.get("service_key") or "").strip()
            if workflow:
                if workflow not in workflows:
                    errors.append(f"{where}: {workflow!r} is not a Workflow of this Workspace")
                elif _required_inputs(workflows[workflow]):
                    errors.append(f"{where}: {workflow!r} needs {_required_inputs(workflows[workflow])} from a person "
                                  "each run, so a schedule cannot start it")
            elif service not in self.services:
                errors.append(f"{where}: give the workflow it runs, or the service whose Agent runs it")
            description = str(row.get("description") or "").strip()
            if len(description) < 10:
                errors.append(f"{where}: description says what each run does")
            try:
                timezone = normalize_timezone(row.get("timezone") or self.timezone)
            except ValueError:
                errors.append(f"{where}: timezone {row.get('timezone')!r} is not an IANA time zone")
                continue
            schedule, problem = _schedule(row.get("cadence"), needed.get(workflow))
            if problem:
                errors.append(f"{where}: {problem}")
                continue
            if schedule.get("cron_expr"):
                try:
                    validate_cron_expression(schedule["cron_expr"])
                except ValueError as exc:
                    errors.append(f"{where}: {exc}")
                    continue
            schedule["timezone"] = timezone
            entry = {"automation_key": key, "description": description,
                     "trigger": str(row.get("said") or _describe(row.get("cadence"), timezone)),
                     "service_key": service or str((workflows.get(workflow) or {}).get("service_key") or ""), **schedule}
            if workflow:
                entry["scheduled_job"] = _native_scheduled_job(
                    {"job_id": key, "name": str(row.get("name") or "") or None, "execution_type": "workflow",
                     "execution_target": {"workflow_slug": workflow}, "description": description},
                    key, schedule, entry["service_key"])
                entry["scheduled_job"] = {k: v for k, v in entry["scheduled_job"].items() if v is not None}
            normalized.append(entry)
        if errors:
            return None, errors, []
        jobs = [entry["scheduled_job"] for entry in normalized if entry.get("scheduled_job")]
        try:
            validate_portable_recipe_resources({"workflows": self.workflows, "scheduled_jobs": jobs})
        except Exception as exc:  # noqa: BLE001 - reported to the designer to fix
            errors.append(str(exc))
        covered = self.scheduled_workflows() | {str((entry.get("scheduled_job") or {}).get("execution_target", {})
                                                    .get("workflow_slug") or "") for entry in normalized}
        for workflow in needed:
            if workflow not in covered:
                errors.append(f"{workflow!r} must run on a schedule ({needed[workflow].get('why') or 'its Workflow needs it'}); "
                              "add an Automation for it")
        # An Automation may do a planned one's work (a calendar's due worker posts on the planned days).
        covered = set(keys) | {str(key) for row in rows for key in row.get("covers") or []}
        for planned in self.spec.get("automations") or []:
            if planned.get("key") not in covered:
                errors.append(f"the spec's automation {planned.get('key')!r} ({planned.get('cadence')}) is missing; "
                              "add it, or name it in covers of the Automation that does its work")
        for routine in self.spec.get("routines") or []:
            if routine.get("automation") and routine["automation"] not in covered:
                errors.append(f"the person asked for {routine.get('said')!r}; schedule it as {routine['automation']!r} "
                              "or name it in covers of the Automation that does it")
        # The input is kept so the candidate is checked again as submitted; ``entries`` is what is written.
        return (None if errors else {"automations": [dict(row) for row in rows], "entries": normalized}), errors, []


def _required_inputs(workflow: dict[str, Any]) -> list[str]:
    return [str(row.get("key")) for row in workflow.get("run_inputs") or []
            if isinstance(row, dict) and row.get("required") and row.get("default") in (None, "")]


def _clock(value: Any) -> tuple[int, int] | None:
    match = _TIME.match(str(value or "09:00").strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def _schedule(cadence: Any, needed: dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    """The scheduler fields for one checked cadence option, or why it is not one."""
    if not isinstance(cadence, dict) or cadence.get("kind") not in CADENCES:
        return {}, f"cadence.kind is one of {list(CADENCES)}"
    kind = cadence["kind"]
    if kind == "as_needed":
        if not needed or not (needed.get("cron_expr") or needed.get("every_seconds")):
            return {}, "as_needed copies a Workflow pattern's own schedule; this Workflow has none, choose a cadence"
        if needed.get("cron_expr"):
            return {"schedule_kind": "cron", "cron_expr": str(needed["cron_expr"])}, None
        return {"schedule_kind": "every", "every_seconds": float(needed["every_seconds"])}, None
    if kind == "every_minutes":
        minutes = int(cadence.get("minutes") or 0)
        if not 5 <= minutes <= 1440:
            return {}, "every_minutes takes minutes between 5 and 1440"
        return {"schedule_kind": "every", "every_seconds": float(minutes * 60)}, None
    clock = _clock(cadence.get("at"))
    if clock is None:
        return {}, "at is a 24-hour time, e.g. '09:00'"
    hour, minute = clock
    if kind == "daily":
        return {"schedule_kind": "cron", "cron_expr": f"{minute} {hour} * * *"}, None
    if kind == "weekdays":
        return {"schedule_kind": "cron", "cron_expr": f"{minute} {hour} * * 1-5"}, None
    if kind == "weekly":
        days = sorted({int(day) for day in cadence.get("days") or [] if str(day).isdigit() and 1 <= int(day) <= 7})
        if not days:
            return {}, "weekly takes days, 1 (Monday) to 7 (Sunday)"
        return {"schedule_kind": "cron", "cron_expr": f"{minute} {hour} * * {','.join(str(d % 7) for d in days)}"}, None
    day = int(cadence.get("day_of_month") or 1)
    if not 1 <= day <= 28:
        return {}, "monthly takes day_of_month between 1 and 28 (every month has it)"
    return {"schedule_kind": "cron", "cron_expr": f"{minute} {hour} {day} * *"}, None


def _describe(cadence: Any, timezone: str) -> str:
    cadence = cadence if isinstance(cadence, dict) else {}
    kind = cadence.get("kind")
    at = str(cadence.get("at") or "09:00")
    text = {
        "every_minutes": f"every {cadence.get('minutes')} minutes", "daily": f"daily at {at}",
        "weekdays": f"weekdays at {at}", "monthly": f"day {cadence.get('day_of_month') or 1} of each month at {at}",
        "weekly": f"weekly on days {cadence.get('days')} at {at}", "as_needed": "on the Workflow's own schedule",
    }.get(kind, "on a schedule")
    return f"{text} ({timezone})"


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


_S = {"type": "string"}

AUTOMATIONS_DESIGNER_TOOL_SCHEMAS = [
    _tool("au_read_context",
          "Read the Workspace's time zone, Workflows (and which need a person's input each run), what must run on a "
          "schedule (needed), the Automations the spec plans, the routines the person named, and current schedules.",
          {}, []),
    _tool("au_submit_candidate",
          "Submit the Automations. Each runs a Workflow (workflow) or a service's Agent (service_key) on a cadence: "
          "{kind:'every_minutes', minutes} | {kind:'daily'|'weekdays', at:'HH:MM'} | {kind:'weekly', days:[1-7, "
          "1=Monday], at} | {kind:'monthly', day_of_month:1-28, at} | {kind:'as_needed'} (the cadence the Workflow's "
          "pattern asked for, listed in needed). Runs every check; a failing candidate is not kept.",
          {"automations": {"type": "array", "items": {"type": "object", "properties": {
              "key": _S, "name": _S, "description": _S, "workflow": _S, "service_key": _S,
              "covers": {"type": "array", "items": _S,
                         "description": "Keys of planned Automations this one does the work of"},
              "said": {"type": "string", "description": "The routine in the person's words, if they named it"},
              "timezone": {"type": "string", "description": "IANA zone; omit for the Workspace's"},
              "cadence": {"type": "object", "properties": {
                  "kind": {"type": "string", "enum": list(CADENCES)}, "minutes": {"type": "integer"},
                  "at": _S, "days": {"type": "array", "items": {"type": "integer"}},
                  "day_of_month": {"type": "integer"}}, "required": ["kind"]}}}}},
          ["automations"]),
]


def automations_designer_executor(context: AutomationsDesignContext, state: dict[str, Any]):
    async def executor(name: str, args: dict[str, Any]) -> str:
        if name == "au_read_context":
            return json.dumps(context.summary(), ensure_ascii=False, default=str)
        if name == "au_submit_candidate":
            state["attempts"] = state.get("attempts", 0) + 1
            candidate, errors, warnings = context.check(dict(args or {}))
            if errors:
                return json.dumps({"ok": False, "errors": errors}, ensure_ascii=False)
            state["candidate"], state["warnings"] = candidate, warnings
            return json.dumps({"ok": True, "schedules": [
                {key: row.get(key) for key in ("automation_key", "cron_expr", "every_seconds", "timezone")}
                for row in candidate["entries"]], "next": "Reply with a two-line design note."}, ensure_ascii=False)
        return json.dumps({"ok": False, "error": f"unknown tool: {name}"})

    return executor


def _accept_automations(fields: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Write each Automation and its scheduled Job as ``ws_propose_automation`` does."""
    from packages.core.ai.tools.workspace_arch_tools import (
        _automation_signature,
        _reconcile_removed_channel_references,
        _replace_in_list,
    )

    automations = [row for row in fields.get("automations") or [] if isinstance(row, dict)]
    jobs = [row for row in fields.get("scheduled_jobs") or [] if isinstance(row, dict)]
    for entry in candidate["entries"]:
        automation = {key: value for key, value in entry.items() if key != "scheduled_job"}
        automations = _replace_in_list(
            [row for row in automations if _automation_signature(row) != _automation_signature(automation)],
            "automation_key", automation["automation_key"], automation)
        if entry.get("scheduled_job"):
            jobs = _replace_in_list(jobs, "job_id", entry["scheduled_job"]["job_id"], copy.deepcopy(entry["scheduled_job"]))
    fields["automations"] = automations
    fields["scheduled_jobs"] = jobs
    scheduled = {str((row.get("execution_target") or {}).get("workflow_slug") or "") for row in jobs}
    left = [row for row in fields.get(NEEDED_FIELD) or [] if str((row or {}).get("workflow")) not in scheduled]
    if left:
        fields[NEEDED_FIELD] = left
    else:
        fields.pop(NEEDED_FIELD, None)
    _reconcile_removed_channel_references(fields)
    return {"automations": [{key: row.get(key) for key in ("automation_key", "cron_expr", "every_seconds", "timezone")}
                            for row in candidate["entries"]]}


def automations_candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    return {"automations": [{"key": row["automation_key"], "runs": (row.get("scheduled_job") or {}).get(
        "execution_target", {}).get("workflow_slug") or row.get("service_key"), "when": row.get("trigger"),
        "cron_expr": row.get("cron_expr"), "every_seconds": row.get("every_seconds"), "timezone": row.get("timezone")}
        for row in candidate["entries"]]}


async def workspace_automations_design_context(db: Any, workspace: Any) -> AutomationsDesignContext:
    """A live Workspace's Workflows and scheduled Jobs, in the keys a Draft keeps."""
    from sqlalchemy import select

    from packages.core.blueprints.exporter import _export_workflows
    from packages.core.models.scheduler import ScheduledJob

    exported = await _export_workflows(db, workspace.entity_id, workspace.id, include_skill_dependencies=False)
    slugs = {str(row.get("id") or ""): row.get("slug") for row in exported.payloads}
    jobs = (await db.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == workspace.id))).scalars().all()
    model = dict(workspace.operating_model or {})
    return AutomationsDesignContext(
        services=[str(row.get("service_key") or row.get("key")) for row in model.get("services") or []
                  if isinstance(row, dict) and (row.get("service_key") or row.get("key"))],
        workflows=list(exported.payloads),
        scheduled_jobs=[{"job_id": job.name, "execution_target": {
            "workflow_slug": slugs.get(str((job.execution_target or {}).get("workflow_id") or ""))}} for job in jobs],
        timezone=str(getattr(workspace, "timezone", None) or "UTC"),
    )


def _automations_needed(context: AutomationsDesignContext) -> bool:
    return bool(context.needed or context.spec.get("automations") or context.spec.get("routines"))


AUTOMATIONS_DESIGNER = PartDesigner(
    part="automations",
    title="Automations",
    subagent=SubAgentSpec(
        name=WORKSPACE_AUTOMATIONS_DESIGNER_SUBAGENT,
        purpose="Design what a Workspace runs on a clock and submit checked schedules.",
        profile=RuntimeProfile.WORKSPACE_ARCHITECT,
        allowed_tools=tuple(schema["function"]["name"] for schema in AUTOMATIONS_DESIGNER_TOOL_SCHEMAS),
        output_contract={"format": "automations_candidate", "required_fields": ("automations", "design_notes")},
    ),
    skill="workspace-automations-design",
    tool_schemas=tuple(AUTOMATIONS_DESIGNER_TOOL_SCHEMAS),
    executor=automations_designer_executor,
    context_from_draft=AutomationsDesignContext.from_draft_fields,
    context_from_workspace=workspace_automations_design_context,
    check=AutomationsDesignContext.check,
    accept=_accept_automations,
    summarize=automations_candidate_summary,
    command_tool=PARTS_COMMAND_TOOL,
    brief_hint="quote each routine the person named with its time, and their time zone",
    blockers=lambda context: (None if context.services else {"error": "save the services before the Automations design"}),
    revise_note=lambda context: ("The Workspace already has schedules; keep each the brief does not ask to change."
                                 if context.automations else None),
    lint_where=("automations", "scheduled_jobs"),
    depends_on=("workflows",),
    fallback="Schedule them yourself with ws_propose_automation.",
    needed=_automations_needed,
)
