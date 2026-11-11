"""The Workflows design sub-agent: designs a Workspace's Workflows and Stats on command.

The Workspace Architect does not write long Workflow graphs itself. It sends
this sub-agent a brief and accepts or sends back the candidate it returns.
The sub-agent composes the Workflows from the patterns in
``blueprints.workflow_patterns`` (a content calendar, a schedule, a channel
publish, a report that records Stats, a project tree) and from its own
steps, and submits a candidate that has passed the checks creation applies.

It designs in the portable (Blueprint) key space: Workflow slugs, service
keys and Stat keys. It is the ``workflows`` part of the part designer
registry (``workspace_part_designers``).
"""
from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.blueprints.workflow_patterns import (
    RECORD_COLLECTION_PARAMETERS,
    RECORD_COLLECTION_REQUIRED,
    SOURCE_WATCH_PARAMETERS,
    SOURCE_WATCH_REQUIRED,
)
from packages.core.ai.runtime.subagents import WORKSPACE_WORKFLOW_DESIGNER_SUBAGENT, SubAgentSpec
from packages.core.services.workflow_project_state_guidance import FOLLOWABLE_PROJECT_STATE
from packages.core.services.workflow_run_input_guidance import RUN_INPUT_TYPES, run_input_issues
from packages.core.services.workspace_part_designers import PartDesigner, run_part_designer

logger = logging.getLogger(__name__)


# ── Context ─────────────────────────────────────────────────────────────────

@dataclass
class WorkflowDesignContext:
    """What one Workspace has, in portable keys, for designing its Workflows."""

    services: list[dict[str, Any]] = field(default_factory=list)
    workflows: list[dict[str, Any]] = field(default_factory=list)
    stats: list[dict[str, Any]] = field(default_factory=list)
    scheduled_jobs: list[dict[str, Any]] = field(default_factory=list)
    integrations: list[str] = field(default_factory=list)
    # The lead's build spec: the Workflow slugs and Stat keys the design must have.
    spec: dict[str, Any] = field(default_factory=dict)
    # What the person's rules say; a rule can ask for an approval.
    rules: list[dict[str, Any]] = field(default_factory=list)
    # The brief and feedback of this run, which an approval may quote. None when
    # a candidate is checked again on accept: its quotes were checked on submit.
    asked_words: str | None = None

    @classmethod
    def from_draft_fields(cls, fields: dict[str, Any]) -> "WorkflowDesignContext":
        from packages.core.services.workspace_build_spec import spec_slice

        return cls(
            services=[{"service_key": str(row["service_key"]), "name": str(row.get("name") or ""),
                       "description": str(row.get("description") or ""),
                       "handoff_to": list(row.get("handoff_to") or []), "human_review": bool(row.get("human_review"))}
                      for row in fields.get("services") or [] if isinstance(row, dict) and row.get("service_key")],
            workflows=[copy.deepcopy(row) for row in fields.get("workflows") or []
                       if isinstance(row, dict) and row.get("slug")],
            stats=[copy.deepcopy(row) for row in fields.get("stats") or [] if isinstance(row, dict) and row.get("key")],
            scheduled_jobs=[copy.deepcopy(row) for row in fields.get("scheduled_jobs") or [] if isinstance(row, dict)],
            integrations=sorted({str(row.get("provider") or row.get("integration") or "")
                                 for row in fields.get("flagged_integrations") or [] if isinstance(row, dict)} - {""}),
            spec=spec_slice(fields, "workflows"),
            rules=[copy.deepcopy(row) for row in fields.get("rules") or [] if isinstance(row, dict)],
        )

    def summary(self, library: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """What ``wf_read_context`` returns to the sub-agent."""
        from packages.core.blueprints.workflow_patterns import summarize_workflow

        return {
            "services": self.services,
            "workflows": [summarize_workflow(row) for row in self.workflows],
            "pattern_workflows": [summarize_workflow(row) for row in library.values()],
            "stats": [{"key": row["key"], "name": row.get("name"), "collector_type": row.get("collector_type")}
                      for row in self.stats],
            "automations": [{"job_id": row.get("job_id"),
                             "workflow": (row.get("execution_target") or {}).get("workflow_slug")}
                            for row in self.scheduled_jobs],
            "integrations_waiting_for_connection": self.integrations,
            "rules": [{name: row[name] for name in ("rule_key", "rule_type", "scope", "description", "action_patterns")
                       if row.get(name)} for row in self.rules],
            **({"spec": self.spec} if self.spec else {}),
        }

    def fields(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Draft fields as they would be with ``candidate`` accepted."""
        return {
            "services": copy.deepcopy(self.services),
            "workflows": _merged(self.workflows, candidate.get("workflows") or [], "slug",
                                 removed=candidate.get("remove_workflows") or []),
            "stats": _merged(self.stats, candidate.get("stats") or [], "key"),
            "scheduled_jobs": copy.deepcopy(self.scheduled_jobs),
        }

    # ── checks ──────────────────────────────────────────────────────────────

    def check(self, value: Any) -> tuple[dict[str, Any] | None, list[str], list[str]]:
        """Check a candidate {workflows, stats, automations, remove_workflows}."""
        from packages.core.ai.tools.workspace_arch_tools import (
            DraftWorkflowError,
            draft_workflow,
            unstartable_workflow_issues,
            workflow_process_issues,
        )
        from packages.core.blueprints.installer import validate_portable_recipe_resources
        from packages.core.services.workspace_app_designer import project_display_issues, project_structure_issues
        from packages.core.services.workspace_goal_measurements import resolve_draft_goal_measurements

        if not isinstance(value, dict):
            return None, ["the candidate must be an object with workflows, stats and automations"], []
        workflows = [row for row in value.get("workflows") or [] if isinstance(row, dict)]
        if not workflows and not value.get("stats"):
            return None, ["the candidate has no workflows or stats"], []
        errors: list[str] = []
        service_keys = {row["service_key"] for row in self.services}
        others = _merged(self.workflows, workflows, "slug")
        normalized = []
        for row in workflows:
            try:
                normalized.append(draft_workflow(row, workflows=others, service_keys=service_keys))
            except DraftWorkflowError as exc:
                details = "".join(f"; {key}: {detail}" for key, detail in exc.details.items())
                errors.append(f"workflows.{row.get('slug')}: {exc}{details}")
        candidate = {
            "workflows": normalized,
            "stats": [copy.deepcopy(row) for row in value.get("stats") or [] if isinstance(row, dict)],
            "automations": [dict(row) for row in value.get("automations") or [] if isinstance(row, dict)],
            "remove_workflows": [str(slug) for slug in value.get("remove_workflows") or []],
        }
        if errors:
            return None, errors, []
        fields = self.fields(candidate)
        try:
            validate_portable_recipe_resources(fields)
            resolve_draft_goal_measurements({"stats": fields["stats"], "goals": []})
        except Exception as exc:
            errors.append(str(exc))
        errors.extend(_assessment_issues(fields))
        errors.extend(logic_step_issues(fields["workflows"], checked={row["slug"] for row in normalized}))
        errors.extend(chat_post_step_issues(fields["workflows"]))
        errors.extend(project_state_issues(fields["workflows"], checked={row["slug"] for row in normalized}))
        errors.extend(run_input_issues(fields["workflows"], checked={row["slug"] for row in normalized}))
        errors.extend(self.approval_issues(fields["workflows"], checked={row["slug"] for row in normalized}))
        warnings: list[str] = []
        for issue in [*workflow_process_issues(fields), *project_structure_issues(fields["workflows"])]:
            (errors if issue["severity"] == "P0" else warnings).append(f"{issue['where']}: {issue['message']}")
        warnings.extend(project_display_issues(fields["workflows"]))
        scheduled = {(row.get("execution_target") or {}).get("workflow_slug") for row in self.scheduled_jobs}
        planned = {str(row.get("workflow")) for row in candidate["automations"]}
        for slug in sorted(_reviewed_workflows(fields["stats"]) - scheduled - planned):
            errors.append(f"{slug} records Stats, so it runs on a schedule: list it in automations")
        errors.extend(f"{issue['where']}: {issue['message']}" for issue in unstartable_workflow_issues(fields, planned=planned))
        known = {row["slug"] for row in fields["workflows"]}
        errors.extend(f"automations: {row.get('workflow')!r} is not a Workflow of this design"
                      for row in candidate["automations"] if row.get("workflow") not in known)
        errors.extend(_scheduled_twice(fields["workflows"], candidate["automations"], self.scheduled_jobs))
        errors.extend(self.spec_issues(fields))
        return (None if errors else candidate), errors, warnings

    def spec_issues(self, fields: dict[str, Any]) -> list[str]:
        """What the build spec names that the design lacks: a pattern's Workflows share its slug as a prefix."""
        slugs = {row["slug"] for row in fields["workflows"]}
        issues = [f"the spec's Workflow {row['slug']!r} ({row.get('purpose') or 'planned'}) has no Workflow named "
                  f"{row['slug']!r} or {row['slug']}_<part>"
                  for row in self.spec.get("workflows") or []
                  if not any(slug == row["slug"] or slug.startswith(f"{row['slug']}_") for slug in slugs)]
        stat_keys = {row["key"] for row in fields["stats"]}
        issues.extend(f"the spec's Stat {row['key']!r} is not recorded by any Workflow"
                      for row in self.spec.get("stats") or [] if row.get("workflow") and row["key"] not in stat_keys)
        by_slug = {row["slug"]: row for row in fields["workflows"]}
        for planned in self.spec.get("workflows") or []:
            pending = {slug for slug in slugs if slug == planned["slug"] or slug.startswith(f"{planned['slug']}_")}
            visited, published = set(), set()
            while pending:
                slug = pending.pop()
                visited.add(slug)
                for step in by_slug[slug].get("steps") or []:
                    config = step.get("config") or {}
                    if step.get("type") == "workflow_action_grant":
                        published.add(_channel_name((config.get("scope") or {}).get("channel")))
                    if step.get("type") in {"subworkflow", "foreach_subworkflow"}:
                        target = str(config.get("workflow_id") or "")
                        if target in by_slug and target not in visited:
                            pending.add(target)
            issues.extend(f"the spec's Workflow {planned['slug']!r} publishes to {channel}, and nothing in its "
                          "Workflow family does: keep or add its publishing (publish.channels, wf_pattern_channel_publish, "
                          "wf_pattern_recurring_publish). Only the lead changes what the spec promises; what your tools "
                          "cannot add to that publishing, name in your design note"
                          for channel in planned.get("publishes_to") or [] if _channel_name(channel) not in published)
        return issues

    def approval_issues(self, workflows: list[dict[str, Any]], *, checked: set[str]) -> list[str]:
        """Approval waits nothing shows the person asked for: each stops the work until someone answers.

        An approval stays when a rule or the person's words ask for it, whatever
        it gates. What shows that, without reading the brief for meaning:

        * the step's ``meta.asked_by``: ``rule:<rule_key>`` of a rule covering
          this Workflow's services, or words quoted from the brief or feedback;
        * it is what a publishing grant is made from (``workflow_action_grant``):
          the publish patterns' approval, which nothing goes out without;
        * it was already on a saved Workflow, or the spec plans an approval stage
          for this Workflow (the publishing grant uses one of its stages);
        * the service before it has ``human_review`` and no other approval does
          that yet: the flag asks for a review of that service's results, not
          one on every Workflow it runs.
        """
        rules = {str(row.get("rule_key")): row for row in self.rules}
        saved = {(row["slug"], step.get("id")) for row in self.workflows for step in _approval_waits(row)}
        reviewed = {row["service_key"] for row in self.services if row.get("human_review")}
        stages = {row["slug"]: sum(1 for stage in row.get("stages") or [] if stage.get("kind") == "approval")
                  for row in self.spec.get("workflows") or []}

        def planned_as(slug: str) -> str | None:
            return next((name for name in stages if slug == name or slug.startswith(f"{name}_")), None)

        # A publishing grant's approval is one of the stages the spec plans, however many channels it has.
        for name in {planned_as(row["slug"]) for row in workflows if _granted_approvals(row)} - {None}:
            stages[name] -= 1
        # What a publishing Flow runs first is reviewed there anyway, right before it goes out.
        before_publishing = {str((step.get("config") or {}).get("workflow_id")) for row in workflows if _granted_approvals(row)
                             for step in row.get("steps") or [] if step.get("type") == "subworkflow"}
        issues: list[str] = []
        met: set[str] = set()
        unexplained: list[tuple[dict[str, Any], dict[str, Any], set[str]]] = []
        for workflow in workflows:
            slug, granted = workflow["slug"], _granted_approvals(workflow)
            for step in _approval_waits(workflow):
                before = _services_before(workflow, step)
                asked_by = str((step.get("meta") or {}).get("asked_by") or "").strip()
                where = f"workflows.{slug}.{step.get('id')}"
                if asked_by:
                    issue = self._asked_by_issue(asked_by, rules, workflow)
                    if issue and slug in checked:
                        issues.append(f"{where}: {issue}")
                elif not (step.get("id") in granted or (slug, step.get("id")) in saved):
                    plan = planned_as(slug)
                    if plan is None or stages[plan] <= 0:
                        unexplained.append((workflow, step, before))
                        continue
                    stages[plan] -= 1
                met |= before
        for workflow, step, before in unexplained:
            due = set() if workflow["slug"] in before_publishing else before & reviewed - met
            met |= due
            if not due and workflow["slug"] in checked:
                issues.append(
                    f"workflows.{workflow['slug']}.{step.get('id')}: nothing shows the person asked for this approval, "
                    "so it stops the work for nothing: remove it (patterns: approve_plan:false, no make.approve or "
                    "approve_made). If a rule or the person's words ask for it, keep it and say which: "
                    "meta:{asked_by:'rule:<rule_key>'} or asked_by with the exact words from your brief (patterns: "
                    "pass that text as approve_plan, make.approve or approve_made).")
        return issues

    def _asked_by_issue(self, asked_by: str, rules: dict[str, dict[str, Any]], workflow: dict[str, Any]) -> str | None:
        if asked_by.startswith("rule:"):
            rule = rules.get(asked_by.removeprefix("rule:").strip())
            services = {str((step.get("config") or {}).get("service_key")) for step in workflow.get("steps") or []
                        if step.get("type") == "agent"}
            if rule is None:
                return f"asked_by names {asked_by!r}, but the rules are {sorted(rules)}"
            if str(rule.get("scope") or "all") not in {"all", *services}:
                return f"asked_by names {asked_by!r}, which covers {rule.get('scope')!r}, a service this Workflow does not run"
            return None
        if self.asked_words is not None and _plain(asked_by) not in _plain(self.asked_words):
            return (f"asked_by {asked_by!r} is not in your brief or feedback: quote their exact words, name a rule "
                    "(rule:<rule_key>), or remove this approval")
        return None


def _plain(text: str) -> str:
    return " ".join(text.split()).lower()


def _channel_name(channel: Any) -> str:
    """A channel as the publish patterns name it."""
    return re.sub(r"[^a-z0-9]+", "_", str(channel or "").strip().lower()).strip("_")


def _scheduled_twice(workflows: list[dict[str, Any]], automations: list[dict[str, Any]],
                     jobs: list[dict[str, Any]]) -> list[str]:
    """A Workflow on the same schedule as one that also runs it, so each of its runs happens twice."""
    schedules = {str(row.get("workflow")): row.get("cron_expr") for row in automations if row.get("cron_expr")}
    schedules.update({str((row.get("execution_target") or {}).get("workflow_slug")): row.get("cron_expr")
                      for row in jobs if row.get("cron_expr")})
    return [f"automations: {step['config']['workflow_id']} runs on {schedules[workflow['slug']]!r} by itself and inside "
            f"{workflow['slug']}, on the same schedule, so it runs twice each time. Remove {workflow['slug']}: a "
            "pattern's <slug>_<part> Workflows already meet the spec's <slug>, and its Automation runs them"
            for workflow in workflows if workflow["slug"] in schedules for step in workflow.get("steps") or []
            if step.get("type") in {"subworkflow", "foreach_subworkflow"}
            and schedules.get(str((step.get("config") or {}).get("workflow_id"))) == schedules[workflow["slug"]]]


def _approval_waits(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Waits asking a person to approve something; asking them to confirm what they did is not one."""
    from packages.core.services.workflow_contracts import workflow_wait_is_confirmation

    return [step for step in workflow.get("steps") or [] if isinstance(step, dict) and step.get("type") == "wait"
            and str((step.get("config") or {}).get("wait_type") or "approval") == "approval"
            and not workflow_wait_is_confirmation(step.get("config"))]


def _granted_approvals(workflow: dict[str, Any]) -> set[str]:
    """Approval steps a publishing grant of this Workflow is made from."""
    return {str((step.get("config") or {}).get("approval_step_id")) for step in workflow.get("steps") or []
            if isinstance(step, dict) and step.get("type") == "workflow_action_grant"}


def _reachable(workflow: dict[str, Any], start: str) -> set[str]:
    """The steps after ``start`` on any route."""
    edges = {step.get("id"): [*(step.get("next") or []), *(step.get("true_next") or []), *(step.get("false_next") or [])]
             for step in workflow.get("steps") or [] if isinstance(step, dict)}
    seen: set[str] = set()
    pending = list(edges.get(start) or [])
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            pending.extend(edges.get(current) or [])
    return seen


def _services_before(workflow: dict[str, Any], wait: dict[str, Any]) -> set[str]:
    """The services whose Agent steps run before ``wait``: the results it reviews."""
    return {str((step.get("config") or {}).get("service_key")) for step in workflow.get("steps") or []
            if isinstance(step, dict) and step.get("type") == "agent"
            and wait.get("id") in _reachable(workflow, step.get("id"))}


def _fixed_fields(workflows: list[dict[str, Any]]) -> set[str]:
    """Fields some schema of the design limits to an ``enum`` or ``const``."""
    fixed: set[str] = set()

    def walk(schema: Any) -> None:
        if not isinstance(schema, dict):
            return
        # A runtime schema ("properties": "{{post_fields}}") declares nothing yet.
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, spec in properties.items():
                if isinstance(spec, dict) and ("enum" in spec or "const" in spec):
                    fixed.add(str(name))
                walk(spec)
        walk(schema.get("items"))
        for key in ("allOf", "anyOf", "oneOf"):
            parts = schema.get(key)
            if isinstance(parts, list):
                for part in parts:
                    walk(part)

    for workflow in workflows:
        for step in workflow.get("steps") or []:
            for key in ("state_schema", "output_schema", "scope_schema", "input_schema"):
                walk((step.get("config") or {}).get(key))
    return fixed


# What a condition may compare to a quoted value: a project's stage, a person's choice, a fixed field.
_STRING_COMPARISON = re.compile(
    r"([A-Za-z_][\w.]*)\s*(?:==|!=)\s*['\"]|['\"]\s*(?:==|!=)\s*([A-Za-z_][\w.]*)")


def logic_step_issues(workflows: list[dict[str, Any]], *, checked: set[str] | None = None) -> list[str]:
    """Logic the designer wrote itself instead of taking from a pattern.

    Code, transform, switch and tool steps come only from patterns, as they
    built them. A condition the designer writes compares text only to a
    project's stage, a person's choice or a field limited to fixed values:
    words an Agent writes match a value only by chance ('Instagram' is not
    'instagram'). ``checked`` limits the Workflows checked; all of them say
    which fields are fixed.
    """
    from packages.core.blueprints.workflow_patterns import LOGIC_STEP_TYPES, pattern_mark

    fixed = _fixed_fields(workflows) | {"current_stage", "choice"}
    choices = {str((step.get("config") or {}).get("response_variable") or "") for workflow in workflows
               for step in workflow.get("steps") or [] if step.get("type") == "wait"} - {""}
    issues = []
    for workflow in workflows:
        if checked is not None and workflow.get("slug") not in checked:
            continue
        for step in workflow.get("steps") or []:
            kind = step.get("type")
            if kind not in LOGIC_STEP_TYPES or (step.get("meta") or {}).get("pattern") == pattern_mark(step):
                continue
            where = f"workflows.{workflow.get('slug')}.{step.get('id')}"
            if kind != "condition":
                issues.append(f"{where}: you wrote a {kind} step yourself; {kind} steps come only from patterns, "
                              "unchanged. Use a pattern option for this (publish.requires, publish.channels, "
                              "schedule, make). If no option does it, leave out only this check, keep the work it "
                              "would guard running, and say in the design note what is missing")
                continue
            free = sorted({name for match in _STRING_COMPARISON.finditer(str((step.get("config") or {}).get("expression") or ""))
                           for name in match.groups() if name and name.split(".")[-1] not in fixed
                           and name not in choices})
            if free:
                issues.append(f"{where}: the condition compares {', '.join(free)} to an exact value, but it is text an "
                              "Agent writes in words, so it matches only by chance; compare a stage, a person's "
                              "choice or a field with an enum (several publish channels: publish.channels)")
    return issues


# Field names that hold a file, a decision log, or a moment the run reached.
_FILE_FIELD = re.compile(r"(?:^|_)(?:file|doc|document)(?:_id)?$")
_FILES_FIELD = re.compile(r"(?:^|_)(?:(?:files|docs|documents)(?:_ids?)?|(?:file|doc|document)_ids)$")
_HISTORY_FIELD = re.compile(r"(?:^|_)history$")
_MOMENT_FIELD = re.compile(r"(?:_at|timestamp|_time)$")


_CHAT_POST = re.compile(
    r"\b(?:internal|workspace|team)(?:[ -](?:team|workspace))?[ -]chat\b|内部聊天|工作区聊天|团队聊天|工作空间聊天",
    re.IGNORECASE,
)


def chat_post_step_issues(workflows: list[dict[str, Any]]) -> list[str]:
    """Agent steps that must complete a chat post, which no Agent has a tool for.

    Local eval 2026-11-03: a saved report ended "failed" on every run, including its
    scheduled one, at a step asked to share it in the internal chat.
    """
    issues: list[str] = []
    for workflow in workflows:
        for step in workflow.get("steps") or []:
            config = step.get("config") if isinstance(step, dict) else None
            if (step or {}).get("type") != "agent" or not isinstance(config, dict):
                continue
            expects = config.get("expects") or []
            if "effects" in expects and _CHAT_POST.search(str(config.get("prompt") or "")):
                issues.append(
                    f"{workflow.get('slug')}.{step.get('id')} asks an Agent to post to the Workspace chat and requires a "
                    "completed action, but an Agent has no tool for that chat, so every run fails there: remove that from "
                    "its prompt and add a notify step {channel:'workspace_chat', message:'...'} after it")
    return issues


def project_state_issues(workflows: list[dict[str, Any]], *, checked: set[str] | None = None) -> list[str]:
    """Project fields kept so a person following the work cannot use them.

    The rules of ``FOLLOWABLE_PROJECT_STATE``: a file an Agent saved is kept
    as ``<name>_document_id`` (any other name shows as text), the decision log
    is ``history_event`` (not built by an Agent or code, and not left out after
    an approval a later patch could record), and a moment the run reaches comes
    from a ``datetime`` step (not an Agent's report).
    """
    from packages.core.ai.workflow_runner import is_document_id_field, workflow_output_field_reads

    def values(node: Any, path: tuple[str, ...], schema: dict) -> Any:
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        for name, value in node.items():
            field_schema = properties.get(name)
            field_schema = field_schema if isinstance(field_schema, dict) else {}
            if isinstance(value, dict):
                yield from values(value, (*path, str(name)), field_schema)
            elif isinstance(value, str):
                yield (*path, str(name)), value, field_schema

    issues = []
    for workflow in workflows:
        if checked is not None and workflow.get("slug") not in checked:
            continue
        steps = [step for step in workflow.get("steps") or [] if isinstance(step, dict)]
        kinds = {step.get("id"): step.get("type") for step in steps}
        for step in steps:
            config = step.get("config") if isinstance(step.get("config"), dict) else {}
            if isinstance(config.get("output_var"), str) and config["output_var"]:
                kinds[config["output_var"]] = step.get("type")
        logged = json.dumps([(step.get("config") or {}).get("history_event") for step in steps], default=str)
        patches = {step.get("id") for step in steps if step.get("type") == "workflow_project"
                   and (step.get("config") or {}).get("operation") == "patch"}
        for wait in _approval_waits(workflow):
            answer = str((wait.get("config") or {}).get("response_variable") or "").strip()
            if patches & _reachable(workflow, wait.get("id")) and not (answer and f"{{{{{answer}." in logged):
                issues.append(f"workflows.{workflow.get('slug')}.{wait.get('id')}: this decision never reaches the "
                              "project's decision log: give the wait a response_variable and put history_event:{kind, "
                              "decision:'{{<response_variable>.choice}}', note:'{{<response_variable>.note}}'} on the "
                              "patch after it (with history:{type:'array'} in state_schema)")
        for step in steps:
            config = step.get("config") if isinstance(step.get("config"), dict) else {}
            if step.get("type") != "workflow_project":
                continue
            schema = config.get("state_schema") if isinstance(config.get("state_schema"), dict) else {}
            for key in ("state", "patch"):
                for path, value, field_schema in values(
                    config.get(key) if isinstance(config.get(key), dict) else {}, (), schema,
                ):
                    name = path[-1]
                    reads = workflow_output_field_reads([{"id": step.get("id"), "config": {"value": value}}])
                    made_by = {kinds.get(source) for source in reads}
                    where = f"workflows.{workflow.get('slug')}.{step.get('id')}.{key}.{'.'.join(path)}"
                    if "agent" in made_by and _FILE_FIELD.search(name) and not is_document_id_field(name):
                        stem = _FILE_FIELD.sub("", name) or "deliverable"
                        issues.append(f"{where} keeps a file an Agent saved under a name the App shows as text: "
                                      f"name it {stem}_document_id and read {{{{<agent step>.{stem}_document_id}}}}")
                    elif "agent" in made_by and _FILES_FIELD.search(name):
                        issues.append(f"{where} keeps a list of files an Agent saved, which the App shows as text: "
                                      "save them in the project's folder (the App lists it), or keep each one as "
                                      "its own <name>_document_id")
                    elif _HISTORY_FIELD.search(name) and made_by & {"agent", "code"}:
                        issues.append(f"{where} has a step build the decision log: put history_event:{{kind, "
                                      "decision:'{{<response_variable>.choice}}', note:'{{<response_variable>.note}}'} "
                                      "on the patch after the approval; the runner appends it with its time")
                    # Numeric *_time fields can measure elapsed time; *_at and timestamp still name moments.
                    elif (_MOMENT_FIELD.search(name) and "agent" in made_by
                          and not (name.endswith("_time") and field_schema.get("type") in ("number", "integer"))):
                        issues.append(f"{where} keeps a time an Agent reports: take it from a datetime step "
                                      "(config:{operation:'now'}) read as {{<that step>}}")
    return issues


def _merged(current: list[dict[str, Any]], new: list[dict[str, Any]], key: str, *,
            removed: list[str] = ()) -> list[dict[str, Any]]:
    rows = {row[key]: copy.deepcopy(row) for row in current if row.get(key) not in removed}
    rows.update({row[key]: copy.deepcopy(row) for row in new if row.get(key)})
    return list(rows.values())


def _reviewed_workflows(stats: list[dict[str, Any]]) -> set[str]:
    return {str((row.get("collector_config") or {}).get("workflow_slug")) for row in stats
            if row.get("collector_type") == "agent_review"}


def _assessment_issues(fields: dict[str, Any]) -> list[str]:
    """An assessed Stat needs exactly one Agent step of its Workflow to record it."""
    workflows = {row["slug"]: row for row in fields["workflows"]}
    services = {row["service_key"] for row in fields["services"]}
    issues = []
    for stat in fields["stats"]:
        if stat.get("collector_type") != "agent_review":
            continue
        slug = (stat.get("collector_config") or {}).get("workflow_slug")
        producers = [step for step in (workflows.get(slug) or {}).get("steps") or []
                     if stat["key"] in ((step.get("config") or {}).get("stat_assessment_keys") or [])]
        if len(producers) != 1 or producers[0].get("type") != "agent":
            issues.append(f"stats.{stat['key']}: {slug!r} needs exactly one agent step listing it in stat_assessment_keys")
        elif (producers[0].get("config") or {}).get("service_key") not in services:
            issues.append(f"stats.{stat['key']}: its assessment step must run a service of this draft")
    return issues


async def workspace_workflow_design_context(db: Any, workspace: Any) -> WorkflowDesignContext:
    """A live Workspace in the portable keys its Blueprint export uses."""
    from sqlalchemy import select

    from packages.core.blueprints.exporter import _export_workflows
    from packages.core.models.workspace_stat import WorkspaceStat

    exported = await _export_workflows(db, workspace.entity_id, workspace.id, include_skill_dependencies=False)
    model = dict(workspace.operating_model or {})
    stats = (await db.execute(select(WorkspaceStat).where(
        WorkspaceStat.workspace_id == workspace.id, WorkspaceStat.entity_id == workspace.entity_id,
        WorkspaceStat.status == "active",
    ))).scalars().all()
    return WorkflowDesignContext(
        services=[{"service_key": str(row.get("service_key") or row.get("key")), "name": str(row.get("name") or ""),
                   "description": str(row.get("description") or ""), "handoff_to": list(row.get("handoff_to") or []),
                   "human_review": bool(row.get("human_review"))}
                  for row in model.get("services") or [] if isinstance(row, dict)
                  and (row.get("service_key") or row.get("key"))],
        workflows=list(exported.payloads),
        stats=[{"key": row.key, "name": row.name, "collector_type": row.collector_type,
                "collector_config": dict(row.collector_config or {})} for row in stats],
        rules=[dict(row) for row in model.get("rules") or [] if isinstance(row, dict)],
    )


# ── Pattern tools ───────────────────────────────────────────────────────────

def _project_schema(context: WorkflowDesignContext, library: dict[str, dict[str, Any]], project_type: str) -> dict[str, Any] | None:
    """The state a project type keeps, read from the Workflows that write it."""
    properties: dict[str, Any] = {}
    for workflow in [*context.workflows, *library.values()]:
        for step in workflow.get("steps") or []:
            config = step.get("config") or {}
            schema = config.get("state_schema")
            if (step.get("type") == "workflow_project" and config.get("project_type") == project_type
                    and isinstance(schema, dict) and isinstance(schema.get("properties"), dict)):
                properties.update(schema["properties"])
    return {"type": "object", "properties": properties} if properties else None


def _text(args: dict[str, Any], name: str, default: str = "") -> str:
    return str(args.get(name) or default).strip()


def _texts(args: dict[str, Any], name: str) -> list[str]:
    return [str(value).strip() for value in args.get(name) or [] if str(value or "").strip()]


def _approve(value: Any, *, default: bool) -> bool | str:
    """An approval option: on or off, or as text the rule or words that ask for it."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default if value is None else bool(value)


def run_pattern(context: WorkflowDesignContext, library: dict[str, dict[str, Any]], name: str,
                args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Build one pattern: (the reply to the designer, the pattern's result).

    The result's Workflows join ``library``, for the candidate to use by slug.
    """
    reply, result = _build_pattern(context, library, name, args)
    if result is None:
        return reply, None
    for workflow in result["workflows"]:
        library[workflow["slug"]] = workflow
    from packages.core.blueprints.workflow_patterns import summarize_workflow

    # Checked as part of the whole design, so what does not fit the Workspace yet shows now.
    _, errors, warnings = context.check({**result, "workflows": list(library.values())})
    return {
        "ok": True, "workflows": [summarize_workflow(row) for row in result["workflows"]],
        "stats": [row["key"] for row in result["stats"]], "automations": result["automations"],
        **({"design_issues": errors} if errors else {}), **({"warnings": warnings} if warnings else {}),
        "next": "Use these by slug in wf_submit_candidate (use_patterns); their Stats and Automations come with "
                "them. Read one with wf_get_workflow to change a step. Fix design_issues before submitting.",
    }, result


def _build_pattern(context: WorkflowDesignContext, library: dict[str, dict[str, Any]], name: str,
                   args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    from packages.core.blueprints import workflow_patterns as patterns
    from packages.core.blueprints.content_calendar import CalendarSpec

    slug = _text(args, "slug")
    if not slug:
        return {"ok": False, "errors": ["slug is required: a snake_case prefix for the pattern's Workflows"]}, None
    try:
        if name == "wf_pattern_content_calendar":
            item, period = _text(args, "item_word", "item"), _text(args, "period_word", "period")
            fields = _texts(args, "item_fields") or ["title"]
            key_field, date_field = f"{item}_key", _text(args, "date_field", "date")
            parent_field = f"{period}_id"
            spec = CalendarSpec(
                period_type=_text(args, "period_project_type", f"{slug}_{period}"),
                item_type=_text(args, "item_project_type", f"{slug}_{item}"),
                item_key_prefix=f"{slug}:{item}:", item_key_field=key_field, parent_field=parent_field,
                date_field=date_field,
                item_fields=tuple(dict.fromkeys([parent_field, key_field, date_field, *fields])),
                period_fields=tuple(dict.fromkeys(["title", "start", "end", *_texts(args, "period_fields")])),
                stages=(_text(args, "planned_stage", "planned"), _text(args, "made_stage", "made")),
                made_stage=_text(args, "made_stage", "made"), period_word=period, item_word=item,
            )
            result = patterns.content_calendar_flows(
                spec, slug=slug, name=_text(args, "name", f"Plan a {period}"), service_key=_text(args, "service_key"),
                plan_prompt=_text(args, "plan_prompt", f"Plan the {period}."),
                approve_plan=_approve(args.get("approve_plan"), default=True), make=args.get("make") or None,
                schedule=args.get("schedule") or None, publish=args.get("publish") or None,
            )
        elif name == "wf_pattern_channel_publish":
            item_type = _text(args, "item_project_type")
            schema = _project_schema(context, library, item_type)
            if schema is None:
                return {"ok": False, "errors": [f"no Workflow declares state_schema.properties for {item_type!r}"]}, None
            result = {"workflows": [patterns.channel_publish_flow(
                slug=slug, item_type=item_type, item_schema=schema, input_key=_text(args, "input_key", "item_id"),
                service_key=_text(args, "service_key"), channel=_text(args, "channel"), prompt=_text(args, "prompt"),
                ready_stages=_texts(args, "ready_stages"), published_stage=_text(args, "published_stage", "published"),
                failed_stage=_text(args, "failed_stage", "needs_attention"),
                window_hours=int(args.get("window_hours") or 24),
                action_keys=_texts(args, "action_keys") or ["social_post.publish"],
                summary_fields=_texts(args, "summary_fields"), requires=_texts(args, "requires"),
                item_word=_text(args, "item_word", "item"),
            )], "stats": [], "automations": []}
        elif name == "wf_pattern_schedule_expansion":
            item_type = _text(args, "item_project_type")
            schema = _project_schema(context, library, item_type)
            if schema is None:
                return {"ok": False, "errors": [f"no Workflow declares state_schema.properties for {item_type!r}"]}, None
            item = _text(args, "item_word", "item")
            parent_field = _text(args, "parent_field") or None
            expansion = patterns.schedule_expansion_flows(
                slug=slug, item_type=item_type, item_schema=schema, date_field=_text(args, "date_field"),
                timezone=_text(args, "timezone", "UTC"), lead_minutes=int(args.get("lead_minutes") or 0),
                waiting_stages=_texts(args, "waiting_stages"), scheduled_stage=_text(args, "scheduled_stage", "scheduled"),
                started_stage=_text(args, "started_stage", "started"), run_workflow=_text(args, "run_workflow"),
                run_input=_text(args, "run_input", "item_id"), parent_field=parent_field, item_word=item,
            )
            parent_type = _text(args, "parent_project_type")
            start_inputs = ([patterns._picker("parent_id", "Plan", parent_type)] if parent_field and parent_type else [])
            steps = patterns.expansion_steps(expansion, parent_id="{{parent_id}}" if start_inputs else None,
                                             then="done", item_word=item)
            schedule_all = patterns._flow(
                f"{slug}_schedule_all", f"Schedule the {item}s", f"Give every planned {item} the time it is due.",
                [patterns._start(steps[0]["id"], start_inputs), *steps, patterns._end()])
            result = {"workflows": [*expansion["workflows"], schedule_all], "stats": [],
                      "automations": expansion["automations"]}
        elif name == "wf_pattern_recurring_publish":
            result = patterns.recurring_publish_flows(
                slug=slug, name=_text(args, "name", "Make and publish"), service_key=_text(args, "service_key"),
                make_prompt=_text(args, "make_prompt"), channel=_text(args, "channel"),
                publish_prompt=_text(args, "publish_prompt"), cron_expr=_text(args, "cron_expr"),
                timezone=_text(args, "timezone", "UTC"), item_word=_text(args, "item_word", "item"),
                item_fields=_texts(args, "item_fields"), requires=_texts(args, "requires"),
                window_hours=int(args.get("window_hours") or 24),
                action_keys=_texts(args, "action_keys") or None,
            )
        elif name == "wf_pattern_report_stat":
            result = patterns.report_stat_flows(
                slug=slug, name=_text(args, "name", "Weekly report"), service_key=_text(args, "service_key"),
                prompt=_text(args, "prompt"), stats=[row for row in args.get("stats") or [] if isinstance(row, dict)],
                cron_expr=_text(args, "cron_expr", "0 9 * * 1"), timezone=_text(args, "timezone", "UTC"),
                report_word=_text(args, "report_word", "report"),
            )
        elif name == "wf_pattern_project_tree":
            result = patterns.project_tree_flows(
                slug=slug, name=_text(args, "name", "Plan"), parent_type=_text(args, "parent_project_type"),
                child_type=_text(args, "child_project_type"), plan_service_key=_text(args, "plan_service_key"),
                plan_prompt=_text(args, "plan_prompt"), make_service_key=_text(args, "make_service_key"),
                make_prompt=_text(args, "make_prompt"), parent_word=_text(args, "parent_word", "project"),
                child_word=_text(args, "child_word", "part"), child_fields=_texts(args, "child_fields") or None,
                approve_plan=_approve(args.get("approve_plan"), default=True),
                approve_made=_approve(args.get("approve_made"), default=False),
            )
        elif name == "wf_pattern_source_watch":
            result = patterns.source_watch_flows_from_args({**args, "slug": slug})
        elif name == "wf_pattern_record_collection":
            result = patterns.record_collection_flows_from_args({**args, "slug": slug})
        else:
            return {"ok": False, "errors": [f"unknown pattern {name}"]}, None
    except (KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "errors": [f"the pattern could not be built: {exc}"]}, None
    return {}, result


# ── Sub-agent tools ─────────────────────────────────────────────────────────

def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


_S = {"type": "string"}
_LIST = {"type": "array", "items": {"type": "string"}}
_ASKED = "false when nobody asked for this approval; else true, or the rule (rule:<rule_key>) or words that ask for it"

WORKFLOW_DESIGNER_TOOL_SCHEMAS = [
    _tool("wf_read_context",
          "Read the Workspace's services (with hand-offs and reviews), its Workflows, Stats, Automations, integrations "
          "waiting for connection, and the Workflows your patterns made.", {}, []),
    _tool("wf_pattern_content_calendar",
          "Pattern: a plan of dated content the person confirms as a whole (a campaign's posts, a newsletter month) "
          "saved as a period project and its dated items. A rhythm with no plan to confirm is "
          "wf_pattern_recurring_publish. Options: make (one Flow makes an item), schedule (each confirmed item becomes "
          "due before its date and starts its Flow then), publish (one channel, approved per item, verified receipt).",
          {"slug": _S, "name": _S, "service_key": {"type": "string", "description": "Service whose Agent plans"},
           "plan_prompt": _S, "period_word": {"type": "string", "description": "e.g. campaign, week, month"},
           "item_word": {"type": "string", "description": "e.g. post, issue"},
           "item_fields": {**_LIST, "description": "Text fields each item keeps besides its key and date, e.g. title, channel, angle"},
           "date_field": {"type": "string", "description": "e.g. post_at"}, "period_fields": _LIST,
           "approve_plan": {"description": _ASKED},
           "make": {"type": "object", "description": "{service_key, prompt, approve: only when asked for, the rule or words that ask}"},
           "schedule": {"type": "object", "description": "{timezone (IANA), lead_minutes}"},
           "publish": {"type": "object", "description": "{service_key, channel, prompt, window_hours, action_keys, requires: item fields that must be filled first, e.g. account_id, within_period: true when the plan has dates its items must stay inside ({start, end}: the period_fields holding them)}; several channels: {channels:[{channel, service_key?, prompt?, action_keys?, requires?}], channel_field:'channel', ...options for all}"}},
          ["slug", "service_key", "plan_prompt", "period_word", "item_word", "item_fields", "date_field"]),
    _tool("wf_pattern_channel_publish",
          "Pattern: publish one item of an existing project type on one channel: the person approves it, the "
          "approval becomes a grant for that item, nothing publishes after the deadline, and only a verified receipt "
          "marks it published.",
          {"slug": _S, "item_project_type": _S, "service_key": _S, "channel": _S, "prompt": _S,
           "ready_stages": _LIST, "published_stage": _S, "failed_stage": _S, "window_hours": {"type": "integer"},
           "requires": {**_LIST, "description": "Item fields that must be filled before the person is asked, e.g. account_id"},
           "action_keys": _LIST, "summary_fields": _LIST, "input_key": _S, "item_word": _S},
          ["slug", "item_project_type", "service_key", "channel", "ready_stages"]),
    _tool("wf_pattern_schedule_expansion",
          "Pattern: once a plan is confirmed, give each dated item of an existing project type the time it is due "
          "and run a worker every 15 minutes that starts run_workflow for each due item.",
          {"slug": _S, "item_project_type": _S, "date_field": _S, "timezone": _S, "lead_minutes": {"type": "integer"},
           "waiting_stages": _LIST, "scheduled_stage": _S, "started_stage": _S, "run_workflow": _S, "run_input": _S,
           "parent_field": _S, "parent_project_type": _S, "item_word": _S},
          ["slug", "item_project_type", "date_field", "timezone", "waiting_stages", "run_workflow", "run_input"]),
    _tool("wf_pattern_recurring_publish",
          "Pattern: one item made and published on a fixed rhythm, with no plan to confirm (a post every Monday, "
          "Wednesday and Friday, a weekly newsletter). An Automation runs it at each time; an Agent makes the item, the "
          "person approves it, and it is published with a verified receipt. It keeps one project per occurrence, with "
          "no calendar and no worker. One channel per call.",
          {"slug": _S, "name": _S, "service_key": _S, "make_prompt": _S, "channel": _S, "publish_prompt": _S,
           "cron_expr": {"type": "string", "description": "The times the person named, e.g. 0 9 * * 1,3,5"},
           "timezone": _S, "item_word": {"type": "string", "description": "e.g. post, issue"},
           "item_fields": {**_LIST, "description": "Text fields the Agent returns with the file, e.g. title, caption"},
           "requires": {**_LIST, "description": "item_fields that must be filled before the person is asked"},
           "window_hours": {"type": "integer"}, "action_keys": _LIST},
          ["slug", "service_key", "make_prompt", "channel", "cron_expr", "timezone"]),
    _tool("wf_pattern_report_stat",
          "Pattern: a recurring report whose run records Stats from the Workspace's evidence, then saves the report.",
          {"slug": _S, "name": _S, "service_key": _S, "prompt": _S, "cron_expr": _S, "timezone": _S,
           "report_word": _S,
           "stats": {"type": "array", "items": {"type": "object", "properties": {
               "key": _S, "name": _S, "rubric": _S, "unit": _S, "maximum": {"type": "number"}}}}},
          ["slug", "service_key", "prompt", "stats", "cron_expr", "timezone"]),
    _tool("wf_pattern_project_tree",
          "Pattern: a parent project that plans its parts (a series and its episodes, a course and its lessons) and "
          "a Flow that makes one planned part inside it.",
          {"slug": _S, "name": _S, "parent_project_type": _S, "child_project_type": _S, "plan_service_key": _S,
           "plan_prompt": _S, "make_service_key": _S, "make_prompt": _S, "parent_word": _S, "child_word": _S,
           "child_fields": _LIST, "approve_plan": {"description": _ASKED}, "approve_made": {"description": _ASKED}},
          ["slug", "parent_project_type", "child_project_type", "plan_service_key", "plan_prompt",
           "make_service_key", "make_prompt"]),
    _tool("wf_pattern_source_watch",
          "Pattern: work that starts when something new arrives in a connected account (a customer's email, a comment "
          "on a post). No channel hands those to a Workflow, so an Automation looks for what is new and starts "
          "run_workflow (you write it; it takes the thing's id as run_input) once for each new thing.",
          SOURCE_WATCH_PARAMETERS, SOURCE_WATCH_REQUIRED),
    _tool("wf_pattern_record_collection",
          "Pattern: a collection people keep by hand and change from the App (a menu's dishes, a catalogue's "
          "products, a list of offers). Makes <slug>_add, <slug>_edit (starts from the record's values; a field left "
          "out keeps its value) and <slug>_take_off (moves it to off_stage). No step runs a model, so each costs nothing.",
          RECORD_COLLECTION_PARAMETERS, RECORD_COLLECTION_REQUIRED),
    _tool("wf_get_workflow", "Read every step of one Workflow (the Workspace's or a pattern's) to change it.",
          {"slug": _S}, ["slug"]),
    _tool("wf_submit_candidate",
          "Submit the design: the pattern Workflows to keep (use_patterns), Workflows you wrote or changed "
          "(workflows; no code, transform, switch or tool step of your own, and pattern ones unchanged), Stats, and "
          "the Automations its Workflows need. Runs every check; a failing candidate is not kept, so fix the errors "
          "and submit again. Only the last passing candidate is returned.",
          {"use_patterns": {**_LIST, "description": "Slugs of pattern Workflows, as they are"},
           "workflows": {"type": "array", "items": {"type": "object"},
                         "description": "Whole Workflows {slug, name, description, run_inputs, internal, trigger_type, steps}"},
           "stats": {"type": "array", "items": {"type": "object"}},
           "automations": {"type": "array", "items": {"type": "object"},
                           "description": "{workflow, cron_expr, timezone, why} for each Workflow that runs on a schedule"},
           "remove_workflows": _LIST},
          []),
]


def workflow_designer_executor(context: WorkflowDesignContext, state: dict[str, Any]):
    """In-memory tools: the run reads its snapshot and keeps its own candidate."""
    library: dict[str, dict[str, Any]] = state.setdefault("library", {})
    stats: dict[str, dict[str, Any]] = state.setdefault("pattern_stats", {})
    automations: dict[str, dict[str, Any]] = state.setdefault("pattern_automations", {})

    async def executor(name: str, args: dict[str, Any]) -> str:
        args = dict(args or {})
        if name == "wf_read_context":
            return json.dumps(context.summary(library), ensure_ascii=False)
        if name.startswith("wf_pattern_"):
            reply, result = run_pattern(context, library, name, args)
            for row in (result or {}).get("stats") or []:
                stats[row["key"]] = row
            for row in (result or {}).get("automations") or []:
                automations[row["workflow"]] = row
            return json.dumps(reply, ensure_ascii=False)
        if name == "wf_get_workflow":
            slug = str(args.get("slug") or "")
            workflow = library.get(slug) or next((row for row in context.workflows if row["slug"] == slug), None)
            return json.dumps(workflow or {"ok": False, "error": f"unknown workflow {slug!r}"}, ensure_ascii=False)
        if name == "wf_submit_candidate":
            state["attempts"] = state.get("attempts", 0) + 1
            used = [str(slug) for slug in args.get("use_patterns") or []]
            unknown = [slug for slug in used if slug not in library]
            if unknown:
                return json.dumps({"ok": False, "errors": [f"no pattern made {slug!r}" for slug in unknown]})
            written = [row for row in args.get("workflows") or [] if isinstance(row, dict)]
            chosen = {slug: library[slug] for slug in used}
            chosen.update({str(row.get("slug")): row for row in written})
            chosen_stats = {key: row for key, row in stats.items()
                            if (row.get("collector_config") or {}).get("workflow_slug") in {*chosen, None}}
            chosen_stats.update({str(row.get("key")): row for row in args.get("stats") or [] if isinstance(row, dict)})
            chosen_automations = {slug: row for slug, row in automations.items() if slug in chosen}
            chosen_automations.update({str(row.get("workflow")): row for row in args.get("automations") or []
                                       if isinstance(row, dict)})
            candidate, errors, warnings = context.check({
                "workflows": list(chosen.values()), "stats": list(chosen_stats.values()),
                "automations": list(chosen_automations.values()),
                "remove_workflows": args.get("remove_workflows") or [],
            })
            if errors:
                return json.dumps({"ok": False, "errors": errors}, ensure_ascii=False)
            state["candidate"] = candidate
            state["warnings"] = warnings
            state["details"] = {"automations_needed": candidate["automations"]}
            return json.dumps({"ok": True, "warnings": warnings,
                               "next": "Fix the warnings and submit again, or reply with your design note."},
                              ensure_ascii=False)
        return json.dumps({"ok": False, "error": f"unknown tool: {name}"})

    return executor


# ── Registry entry ──────────────────────────────────────────────────────────

def _workflow_blockers(context: WorkflowDesignContext) -> dict[str, Any] | None:
    if not context.services:
        return {"error": "save the services before commanding the Workflows design"}
    return None


def _accept_workflows(fields: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    removed = set(candidate.get("remove_workflows") or [])
    fields["workflows"] = _merged([row for row in fields.get("workflows") or [] if isinstance(row, dict)],
                                  candidate["workflows"], "slug", removed=list(removed))
    if candidate["stats"]:
        fields["stats"] = _merged([row for row in fields.get("stats") or [] if isinstance(row, dict)],
                                  candidate["stats"], "key")
    # Kept for the Automations designer until an Automation runs each.
    from packages.core.services.workspace_automations_designer import NEEDED_FIELD

    needed = _merged([row for row in fields.get(NEEDED_FIELD) or [] if isinstance(row, dict)
                      and row.get("workflow") not in removed], candidate["automations"], "workflow")
    if needed:
        fields[NEEDED_FIELD] = needed
    return {"workflows": [row["slug"] for row in candidate["workflows"]],
            "stats": [row["key"] for row in candidate["stats"]], "removed": sorted(removed),
            "automations_needed": candidate["automations"]}


def workflows_candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    """The candidate as the lead reviews it: each Workflow's shape, not its every step."""
    from packages.core.blueprints.workflow_patterns import review_workflow

    return {"workflows": [review_workflow(row) for row in candidate["workflows"]],
            "stats": [{"key": row["key"], "name": row.get("name")} for row in candidate["stats"]],
            "automations_needed": candidate["automations"], "remove_workflows": candidate["remove_workflows"]}


def _revise_workflows(candidate: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """A sent-back design as its summary, with its Workflows in the run's library: reading them all costs every call."""
    state = {"library": {row["slug"]: copy.deepcopy(row) for row in candidate["workflows"]},
             "pattern_stats": {row["key"]: copy.deepcopy(row) for row in candidate["stats"]},
             "pattern_automations": {row["workflow"]: copy.deepcopy(row) for row in candidate["automations"]}}
    return {**workflows_candidate_summary(candidate), "already_made": (
        "Each of these Workflows is ready: keep it by slug in use_patterns, read it with wf_get_workflow, and rebuild "
        "or rewrite only what the feedback changes. Its Stats and Automations come with it.")}, state


async def _run_workflow_designer(context: WorkflowDesignContext, **kwargs: Any):
    # An approval may quote what this run was told, the design it revises included.
    context.asked_words = "\n".join(str(text or "") for text in (
        kwargs.get("brief"), kwargs.get("feedback"), (kwargs.get("sent_back") or {}).get("brief")))
    # Looked up when called, so a replaced ``run_part_designer`` is the one that runs;
    # ``design_wave`` retries a run that fails for a technical reason.
    return await run_part_designer(WORKFLOWS_DESIGNER, context, **kwargs)


WORKFLOWS_DESIGNER = PartDesigner(
    part="workflows",
    title="Workflows",
    subagent=SubAgentSpec(
        name=WORKSPACE_WORKFLOW_DESIGNER_SUBAGENT,
        purpose="Design a Workspace's Workflows and Stats from its services and submit a checked candidate.",
        profile=RuntimeProfile.WORKSPACE_ARCHITECT,
        allowed_tools=tuple(schema["function"]["name"] for schema in WORKFLOW_DESIGNER_TOOL_SCHEMAS),
        output_contract={"format": "workflows_candidate", "required_fields": ("workflows", "design_notes")},
    ),
    skill="workspace-workflow-design",
    tool_schemas=tuple(WORKFLOW_DESIGNER_TOOL_SCHEMAS),
    executor=workflow_designer_executor,
    context_from_draft=WorkflowDesignContext.from_draft_fields,
    context_from_workspace=workspace_workflow_design_context,
    check=WorkflowDesignContext.check,
    accept=_accept_workflows,
    summarize=workflows_candidate_summary,
    command_tool="ws_design_workflows",
    brief_hint=("say what work repeats, who does each stage, what the person approves, what is dated or tracked, "
                "and what a person gives each run (files they upload, fixed options, records they pick or change)"),
    blockers=_workflow_blockers,
    revise_note=lambda context: ("The Workspace already has Workflows; keep what the brief does not ask to change."
                                 if context.workflows else None),
    run=_run_workflow_designer,
    revision=_revise_workflows,
    lint_where=("workflows", "stats", "services"),
    fallback="Design them yourself with ws_propose_workflow and ws_propose_stat.",
    shared_guidance=(("What a project keeps", FOLLOWABLE_PROJECT_STATE), ("What a run asks for", RUN_INPUT_TYPES)),
)
