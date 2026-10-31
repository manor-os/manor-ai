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
"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

APP_DESIGN_SKILL_DIR = Path(__file__).resolve().parents[1] / "ai" / "skills" / "workspace-app-design"
# Map fields of an App config: a whole-page candidate removes the keys it drops.
_APP_MAP_FIELDS = ("labels", "descriptions", "forms", "targets", "entries", "prompts", "presets", "asks",
                   "icons", "data_sources")
_APP_PAGE_FIELDS = ("eyebrow", "title", "description", "hero", "order", "hidden", "visual", "layout",
                    "components", "sections")


# ── Context ─────────────────────────────────────────────────────────────────

def _workflow_inputs(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """The inputs a run of this Workflow takes, with how the App can fill them."""
    rows = workflow.get("run_inputs")
    if not isinstance(rows, list) or not rows:
        variables = workflow.get("variables") or []
        rows = ([{"key": key, **(value if isinstance(value, dict) else {})} for key, value in variables.items()]
                if isinstance(variables, dict) else [row for row in variables if isinstance(row, dict)])
    inputs = []
    for row in rows:
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


def _workflow_projects(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """The projects a Workflow keeps, read from its ``workflow_project`` steps."""
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
            "stages": [], "has_folder": False, "child_of_another_project": False, "remade_in_takes": False,
        })
        operation = str(config.get("operation") or "")
        if operation and operation not in project["operations"]:
            project["operations"].append(operation)
        properties = ((config.get("state_schema") or {}).get("properties") or {})
        for name, spec in properties.items():
            kind = spec.get("type") if isinstance(spec, dict) else None
            project["fields"].setdefault(name, kind or "any")
            if kind == "array" and name not in project["lists"]:
                project["lists"].append(name)
        stage = config.get("current_stage")
        if isinstance(stage, str) and stage and stage not in project["stages"]:
            project["stages"].append(stage)
        project["has_folder"] = project["has_folder"] or isinstance(config.get("folder"), dict)
        state = config.get("state") if isinstance(config.get("state"), dict) else {}
        project["child_of_another_project"] = project["child_of_another_project"] or "parent_project_id" in state
        # A list is filled when a step appends or upserts into it, or sets it from a run value.
        written = [update.get("path") for key in ("list_appends", "list_upserts")
                   for update in config.get(key) or [] if isinstance(update, dict)]
        written += [name for name, value in {**state, **(config.get("patch") or {})}.items()
                    if isinstance(value, str) and "{{" in value]
        for name in written:
            if isinstance(name, str) and name not in project["written_lists"]:
                project["written_lists"].append(name)
        project["remade_in_takes"] = project["remade_in_takes"] or config.get("production") == "take"
    return list(projects.values())


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


@dataclass
class AppDesignContext:
    """What one Workspace runs, in portable keys, and its current App page."""

    services: list[dict[str, str]] = field(default_factory=list)
    workflows: list[dict[str, Any]] = field(default_factory=list)
    automations: list[dict[str, str]] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    stats: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    approval_rules: bool = False
    app_view: dict[str, Any] | None = None

    @classmethod
    def from_draft_fields(cls, fields: dict[str, Any]) -> "AppDesignContext":
        rules = [rule for rule in fields.get("rules") or [] if isinstance(rule, dict)]
        current = fields.get("app_view") if isinstance(fields.get("app_view"), dict) else {}
        # A Draft has no Agents yet; the Agent targets a Blueprint's App already
        # names stay usable, and new entries reach an Agent through agent:default.
        agents = sorted({str(target).removeprefix("agent:") for target in (current.get("targets") or {}).values()
                         if str(target).startswith("agent:") and target != "agent:default"})
        return cls(
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

    def summary(self) -> dict[str, Any]:
        """What ``app_read_context`` returns to the sub-agent."""
        from packages.core.services.workspace_app_view import app_module_catalog, normalize_app_view_config

        return {
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
            "module_catalog": app_module_catalog(),
        }

    # ── checks ──────────────────────────────────────────────────────────────

    def check(self, value: Any) -> tuple[dict[str, Any] | None, list[str], list[str]]:
        """Check a whole-page candidate. Returns (normalized, errors, warnings)."""
        from packages.core.blueprints.app_view import remap_app_view
        from packages.core.services.workspace_app_view import (
            app_target_kind_and_id,
            normalize_app_view_config,
            validate_app_view_data_contract,
        )

        try:
            config = normalize_app_view_config(value)
        except ValueError as exc:
            return None, [str(exc)], []
        workflows = self.runnable_workflows()
        definitions = {row["slug"]: row["slug"] for row in self.workflows}
        try:
            remap_app_view(config, workflows=workflows, workflow_definitions=definitions,
                           agents={slug: slug for slug in self.agents},
                           automations={row["id"]: row["id"] for row in self.automations},
                           stat_keys=set(self.stats))
            validate_app_view_data_contract(config["data_sources"], config["components"], catalog={
                "workflow_definitions": definitions, "workflow_bindings": workflows,
                "stat_keys": set(self.stats), "workflows": self.workflows,
            })
        except ValueError as exc:
            return None, [str(exc)], []

        errors: list[str] = []
        warnings: list[str] = []
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
                for option in ("item_action", "plan_action"):
                    action = block.get(option)
                    if not action:
                        continue
                    key = action["action"]
                    card_actions.add(key)
                    inputs = inputs_of(key)
                    where = f"{component['key']}.{block['key']}.{option}"
                    if inputs is None:
                        errors.append(f"{where} opens {key}, which is not an entry that runs a Workflow")
                        continue
                    wanted = {action["input"]} if option == "item_action" else set(action["inputs"])
                    missing = sorted(wanted - inputs)
                    if missing:
                        errors.append(f"{where} fills inputs {key}'s Workflow does not take: {', '.join(missing)}")
        warnings.extend(f"{key} is opened from cards but still shows as a tile; add it to hidden"
                        for key in sorted(card_actions) if key not in config["hidden"])
        if config["hero"] and config["hero"] in config["hidden"]:
            errors.append("the hero entry cannot be hidden")
        return config, errors, warnings


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


# ── Sub-agent run ───────────────────────────────────────────────────────────

def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


APP_DESIGNER_TOOL_SCHEMAS = [
    _tool("app_read_context",
          "Read what this Workspace runs (Workflows with inputs and projects, services, Agents, Stats, Goals, "
          "Automations), its current App page, the keys to use and the module catalog.", {}, []),
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
    _tool("app_submit_candidate",
          "Submit the whole App page. Runs every check; a failing candidate is not kept, so fix the errors and "
          "submit again. Only the last passing candidate is returned to the agent that asked.",
          {
              "app_view": {"type": "object", "description": "The whole page: eyebrow, title, description, layout, "
                           "visual, hero, order, hidden, labels, descriptions, forms, entries, targets, prompts, "
                           "presets, asks, sections, data_sources, components."},
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


def _design_system_prompt() -> str:
    text = (APP_DESIGN_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip()


def app_designer_executor(context: AppDesignContext, state: dict[str, Any]):
    """In-memory tools: the run reads its snapshot and keeps its own candidate."""

    async def executor(name: str, args: dict[str, Any]) -> str:
        if name == "app_read_context":
            return json.dumps(context.summary(), ensure_ascii=False)
        if name == "app_pattern_project_tree":
            return json.dumps(project_tree_pattern(context, dict(args or {})), ensure_ascii=False)
        if name == "app_submit_candidate":
            state["attempts"] = state.get("attempts", 0) + 1
            config, errors, warnings = context.check((args or {}).get("app_view"))
            if errors:
                return json.dumps({"ok": False, "errors": errors}, ensure_ascii=False)
            state["candidate"] = config
            state["recipes"] = [str(item) for item in (args or {}).get("recipes") or []]
            state["warnings"] = warnings
            return json.dumps({"ok": True, "warnings": warnings,
                               "next": "Fix the warnings and submit again, or reply with your design note."},
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
) -> AppDesignResult:
    """Run the App design sub-agent once and return its last passing candidate."""
    from packages.core.ai.runtime import (
        runtime_execute_subagent_loop,
        runtime_prepare_local_tool_surface_for_turn,
        runtime_request_for_surface_turn,
    )
    from packages.core.ai.runtime.subagents import WORKSPACE_APP_DESIGNER_SUBAGENT

    request = runtime_request_for_surface_turn(
        surface=surface, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
        message=brief, legacy_path="services.workspace_app_designer",
    )
    surface_result = runtime_prepare_local_tool_surface_for_turn(request, tool_schemas=APP_DESIGNER_TOOL_SCHEMAS)
    schemas = {schema["function"]["name"]: schema for schema in surface_result.tool_schemas}
    state: dict[str, Any] = {}
    message = f"Brief:\n{brief.strip()}"
    if feedback and feedback.strip():
        message += f"\n\nYour last design was sent back with this feedback:\n{feedback.strip()}"
    if context.app_view:
        message += "\n\nThe page has a current App; keep what the brief does not ask to change."
    loop = await runtime_execute_subagent_loop(
        runtime_envelope=surface_result.envelope,
        requested_name=WORKSPACE_APP_DESIGNER_SUBAGENT,
        system_prompt=_design_system_prompt(),
        user_message=message,
        tools=surface_result.tool_schemas,
        allowed_tool_names=surface_result.allowed_tool_names,
        tool_executor=app_designer_executor(context, state),
        tool_schema_resolver=lambda name: schemas.get(str(name or "")),
        entity_id=entity_id,
        user_id=user_id,
        agent_id=None,
        workspace_id=workspace_id,
        conversation_id=None,
        active_user_message=brief,
    )
    return AppDesignResult(
        app_view=state.get("candidate"),
        recipes=state.get("recipes") or [],
        warnings=state.get("warnings") or [],
        notes=str(getattr(loop.result, "content", "") or "").strip(),
        attempts=int(state.get("attempts") or 0),
    )


async def workspace_app_design_context(db: Any, workspace: Any) -> AppDesignContext:
    """A live Workspace in the portable keys its Blueprint export uses."""
    from sqlalchemy import select

    from packages.core.blueprints.app_view import remap_workspace_app_view
    from packages.core.blueprints.exporter import _export_workflows, _portable_agent_slug
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workspace import Agent, AgentSubscription
    from packages.core.models.workspace_stat import WorkspaceStat
    from packages.core.services.workspace_app_view import app_lists_scheduled_job

    exported = await _export_workflows(db, workspace.entity_id, workspace.id, include_skill_dependencies=False)
    model = dict(workspace.operating_model or {})
    current = None
    if isinstance(model.get("app_view"), dict):
        try:
            current = await remap_workspace_app_view(db, workspace, model["app_view"], to_runtime=False)
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
    return AppDesignContext(
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
    )


async def design_workspace_app_view(
    db: Any,
    *,
    workspace_id: str,
    entity_id: str,
    user_id: str | None,
    brief: str,
    feedback: str | None = None,
    replaces_draft_id: str | None = None,
) -> dict[str, Any]:
    """Run the App designer on a live Workspace and stage its page for review.

    The candidate becomes an ordinary ``app_view.update`` operation draft: the
    agent that asked accepts it through that draft's review (the person
    approves it as any page change) or discards it. Nothing changes the live
    App until then.
    """
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.blueprints.app_view import remap_workspace_app_view
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_operation_service import (
        create_operation_draft,
        discard_operation_draft,
        draft_to_dict,
    )

    brief = (brief or "").strip()
    if not brief:
        return {"ok": False, "error": "brief is required: say what the person wants to do from the App"}
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None or workspace.entity_id != entity_id:
        return {"ok": False, "error": "workspace not found"}
    context = await workspace_app_design_context(db, workspace)
    # No transaction stays open while the sub-agent works.
    await db.commit()
    result = await run_app_designer(
        context, brief=brief, feedback=feedback, entity_id=entity_id, user_id=user_id,
        workspace_id=workspace_id, surface=ChatSurface.WORKSPACE_CHAT,
    )
    if result.app_view is None:
        return {"ok": False, "error": "the App designer did not produce a page that passes its checks",
                "design_notes": result.notes, "attempts": result.attempts}
    workspace = await db.get(Workspace, workspace_id)
    runtime_view = await remap_workspace_app_view(db, workspace, result.app_view, to_runtime=True)
    update = app_view_replacement((workspace.operating_model or {}).get("app_view"), runtime_view)
    if replaces_draft_id:
        await discard_operation_draft(db, replaces_draft_id, entity_id, workspace_id, user_id=user_id)
    draft = await create_operation_draft(
        db, workspace_id, entity_id, user_id=user_id, source_event_id="workspace_app_designer",
        initial_patches=[{"op": "app_view.update", "payload": {"app_view": update}}],
    )
    if draft is None:
        return {"ok": False, "error": "workspace not found"}
    return {
        "ok": True, "draft": draft_to_dict(draft), "candidate": app_view_candidate_summary(result.app_view),
        "recipes": result.recipes, "warnings": result.warnings, "design_notes": result.notes,
    }


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
            "card_actions": [block[option]["action"] for block in component["blocks"]
                             for option in ("item_action", "plan_action") if block.get(option)],
        } for component in config["components"]],
    }


def app_view_replacement(current: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    """An ``app_view.update`` payload that turns ``current`` into ``candidate``.

    Map fields merge in that operation, so the keys the candidate drops are
    sent as ``None``; translations are kept.
    """
    from packages.core.services.workspace_app_view import normalize_app_view_config

    before = normalize_app_view_config(current)
    update: dict[str, Any] = {key: candidate[key] for key in _APP_PAGE_FIELDS}
    for key in _APP_MAP_FIELDS:
        # ``icons`` is left out of a config that sets none.
        kept = candidate.get(key) or {}
        update[key] = {**{name: None for name in before.get(key) or {} if name not in kept}, **kept}
    return update
