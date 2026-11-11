"""The Agents design sub-agent: designs the Agent each service of a Workspace gets.

For each service it reuses an existing Agent or designs a custom one: a
reusable system prompt, the capabilities the shared Factory matcher selects
for the service (tools, Skills such as docx, MCP servers that may still wait
for a connection), and Skills to create from the person's method. It runs
the same matcher ``ws_search_capabilities`` runs, with no Draft lock, and
submits the matched plans with its design; it writes nothing.

Accepting the candidate stores each matched plan and then writes each Agent
through the lead's own tools (``ws_request_custom_agent``,
``ws_propose_agent_mapping``), which check it against the live catalog and
find missing Skills as they do for the lead. It is the ``agents`` part of
the part designer registry (``workspace_part_designers``).
"""
from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.subagents import WORKSPACE_AGENTS_DESIGNER_SUBAGENT, SubAgentSpec
from packages.core.services.workflow_contracts import workflow_step_label
from packages.core.services.workspace_part_designers import PARTS_COMMAND_TOOL, PartDesigner, run_part_designer

logger = logging.getLogger(__name__)

_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_MIN_PROMPT_CHARS = 120
_MIN_SKILL_PROMPT_CHARS = 120
_MAX_SKILL_SPECS = 4
# The match before the run and one more with a sharper intent; each costs a few model calls.
MAX_MATCHES_PER_SERVICE = 2
# Words a selected capability's id, name or description carries when it makes a deliverable.
DELIVERABLE_WORDS = {
    "docx": ("docx", "word document", "word file"), "pptx": ("pptx", "powerpoint", "slide"),
    "xlsx": ("xlsx", "excel", "spreadsheet"), "pdf": ("pdf",), "image": ("image",),
    "video": ("video",), "audio": ("audio", "speech", "voice"), "website": ("website", "site", "html"),
}
# Operating a browser covers every platform; its upload helper does not.
_BROWSER = re.compile(r"\bmcp[:_]chrome\b")


# ── Context ─────────────────────────────────────────────────────────────────

@dataclass
class AgentsDesignContext:
    """What one Workspace has for designing its Agents, in portable keys."""

    fields: dict[str, Any] = field(default_factory=dict)
    services: list[dict[str, Any]] = field(default_factory=list)
    spec: dict[str, Any] = field(default_factory=dict)
    # Existing Agents the services may reuse; known only while a designer runs.
    entity_agents: list[dict[str, Any]] | None = None
    # The actor's capability catalog; known only while a designer runs.
    catalog: Any = None
    entity_id: str = ""
    # (service_key, intent) -> {"plan", "reply"}: matches made for this run.
    matched: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_draft_fields(cls, fields: dict[str, Any]) -> "AgentsDesignContext":
        from packages.core.services.workspace_build_spec import spec_slice

        kept = {key: copy.deepcopy(value) for key, value in fields.items()
                if key not in {"_candidates", "_build_spec", "sources", "_blueprint_source_payload"}}
        kept["sources"] = [{key: copy.deepcopy(row[key]) for key in ("id", "name", "kind", "status") if key in row}
                           for row in fields.get("sources") or [] if isinstance(row, dict)]
        return cls(
            fields=kept,
            services=[copy.deepcopy(row) for row in fields.get("services") or []
                      if isinstance(row, dict) and row.get("service_key")],
            spec=spec_slice(fields, "agents"),
        )

    def spec_agent(self, service_key: str) -> dict[str, Any]:
        return next((row for row in self.spec.get("agents") or [] if row.get("service_key") == service_key), {})

    def service(self, service_key: str) -> dict[str, Any] | None:
        return next((row for row in self.services if row.get("service_key") == service_key), None)

    def capability_fingerprint(self, service_key: str, intent: str) -> str | None:
        """What a matched plan for this service stays valid for (as ``ws_request_custom_agent`` checks)."""
        from packages.core.ai.tools.workspace_arch_tools import _agent_capability_context, _stable_fingerprint

        service = self.service(service_key)
        if service is None:
            return None
        return _stable_fingerprint(_agent_capability_context(
            self.fields, service=service, service_key=service_key, intent=intent))

    def latest_match(self, service_key: str) -> dict[str, Any] | None:
        """The match this service's Agent has now: a sent-back design's, or the last one made."""
        matches = [match for key, match in self.matched.items() if key[0] == service_key and match.get("plan")]
        return matches[-1] if matches else None

    def uncovered_deliverables(self, service_key: str, plan: dict[str, Any],
                               specs: list[dict[str, Any]] = ()) -> list[str]:
        """The files the spec says this service makes that neither its capabilities nor its new Skills make."""
        labels = " ".join(str(label).lower() for label in plan.get("labels") or [])
        skill_text = " ".join(f"{spec.get('name')} {spec.get('system_prompt')}".lower() for spec in specs)
        return [deliverable for deliverable in self.spec_agent(service_key).get("deliverables") or []
                if not any(word in labels or word in skill_text
                           for word in DELIVERABLE_WORDS.get(deliverable, (deliverable,)))]

    def uncovered_platforms(self, service_key: str, reach: list[str]) -> list[str]:
        """The platforms the spec says this service works on that ``reach`` does not, when the catalog has a way."""
        if self.catalog is None:
            return []
        return platforms_out_of_reach(self.spec_agent(service_key).get("integrations") or [], reach, self.catalog)

    def agent_reach(self, agent: dict[str, Any]) -> list[str]:
        """What a reusable Agent's own bindings connect it to, in the catalog's words."""
        servers = {str(server) for server in agent.get("mcp_bindings") or []}
        skills = {str(skill.get("id")) for skill in agent.get("skill_bindings") or [] if isinstance(skill, dict)}
        return [candidate_label(candidate) for candidate in self.catalog.candidates if reaches_account(candidate) and (
            candidate.metadata.get("server_key") in servers or candidate.ref in servers or candidate.ref in skills)]

    def summary(self) -> dict[str, Any]:
        """What ``ag_read_context`` returns to the sub-agent."""
        from packages.core.services.workspace_draft_sources import draft_sources

        work: dict[str, list[dict[str, Any]]] = {}
        for workflow in self.fields.get("workflows") or []:
            for step in (workflow or {}).get("steps") or []:
                config = step.get("config") or {}
                if step.get("type") == "agent" and config.get("service_key"):
                    work.setdefault(config["service_key"], []).append({
                        "workflow": workflow.get("slug"), "step": workflow_step_label(step),
                        "expects": config.get("expects") or [], "prompt": str(config.get("prompt") or "")[:300]})
        mappings = {row.get("service_key"): row for row in self.fields.get("agent_mappings") or [] if isinstance(row, dict)}
        return {
            "workspace": {key: self.fields.get(key) for key in ("name", "kind", "primary_work", "operating_context")},
            "services": [{
                "service_key": row["service_key"], "name": row.get("name"), "description": row.get("description"),
                "required_capabilities": row.get("required_capabilities") or [],
                "handoff_to": row.get("handoff_to") or [], "human_review": bool(row.get("human_review")),
                "spec": self.spec_agent(row["service_key"]),
                "workflow_steps": work.get(row["service_key"], []),
                "current_agent": _mapping_summary(mappings.get(row["service_key"])),
            } for row in self.services],
            "rules": [{key: row.get(key) for key in ("rule_key", "rule_type", "description", "action_patterns", "scope")}
                      for row in self.fields.get("rules") or [] if isinstance(row, dict)],
            "integrations": self.spec.get("integrations") or [],
            "sources": [{"id": row.get("id"), "name": row.get("name")} for row in draft_sources(self.fields)
                        if row.get("status") == "ready"],
            "language": self.spec.get("language"),
        }

    # ── checks ──────────────────────────────────────────────────────────────

    def check(self, value: Any) -> tuple[dict[str, Any] | None, list[str], list[str]]:
        """Check a candidate {agents: [{service_key, existing_agent_id | agent design, plan}]}."""
        from packages.core.services.workspace_draft_sources import ready_source_ids

        if not isinstance(value, dict) or not isinstance(value.get("agents"), list):
            return None, ["the candidate must be an object with agents: one entry per service"], []
        errors: list[str] = []
        warnings: list[str] = []
        rows = [row for row in value["agents"] if isinstance(row, dict)]
        keys = [str(row.get("service_key") or "") for row in rows]
        wanted = [row["service_key"] for row in self.spec.get("agents") or []] or [row["service_key"] for row in self.services]
        for key in wanted:
            if key not in keys:
                errors.append(f"agents: service {key!r} has no Agent")
        readable = ready_source_ids(self.fields)
        normalized = []
        for row, key in zip(rows, keys):
            where = f"agents.{key}"
            if self.service(key) is None:
                errors.append(f"{where}: {key!r} is not a service of this Workspace")
                continue
            if keys.count(key) > 1:
                errors.append(f"{where}: listed more than once")
                continue
            existing = str(row.get("existing_agent_id") or "").strip()
            if existing:
                if not _ULID.match(existing):
                    errors.append(f"{where}: existing_agent_id must be an Agent id from ag_find_agents")
                elif self.entity_agents is not None and existing not in {a.get("id") for a in self.entity_agents}:
                    errors.append(f"{where}: {existing} is not an Agent you can reuse; pick one from ag_find_agents")
                elif self.catalog is not None and self.entity_agents is not None:
                    agent = next(a for a in self.entity_agents if a.get("id") == existing)
                    missing = self.uncovered_platforms(key, self.agent_reach(agent))
                    if missing:
                        errors.append(f"{where}: {agent.get('name') or existing} cannot reach {', '.join(missing)}, which "
                                      "this service works on, and records other Agents save do not replace that. Give "
                                      "this service a custom Agent with its matched capabilities")
                # Its match stays with it: a design sent back may give the service a custom Agent.
                normalized.append({"service_key": key, "existing_agent_id": existing,
                                   "rationale": str(row.get("rationale") or ""),
                                   **({"plan": copy.deepcopy(row["plan"])} if isinstance(row.get("plan"), dict) else {})})
                continue
            name = str(row.get("agent_name") or "").strip()
            prompt = str(row.get("system_prompt") or "").strip()
            if not name:
                errors.append(f"{where}: agent_name is required")
            if len(prompt) < _MIN_PROMPT_CHARS:
                errors.append(f"{where}: system_prompt describes the Agent's capability in 5-10 sentences")
            plan = row.get("plan")
            if not isinstance(plan, dict) or not plan.get("capability_ids"):
                errors.append(f"{where}: match its capabilities with ag_match_capabilities first")
                continue
            fingerprint = self.capability_fingerprint(key, str(plan.get("intent") or ""))
            if plan.get("context_fingerprint") != fingerprint:
                errors.append(f"{where}: the service or its rules changed since its capabilities were matched; "
                              "match them again")
            specs = [spec for spec in row.get("missing_skill_specs") or [] if isinstance(spec, dict)]
            if len(specs) > _MAX_SKILL_SPECS:
                errors.append(f"{where}: at most {_MAX_SKILL_SPECS} new Skills")
            for spec in specs:
                if not str(spec.get("name") or "").strip() or len(str(spec.get("system_prompt") or "")) < _MIN_SKILL_PROMPT_CHARS:
                    errors.append(f"{where}: each new Skill needs a name and a system_prompt with its procedure")
                unknown = [source for source in spec.get("source_ids") or [] if source not in readable]
                if unknown:
                    errors.append(f"{where}: Skill {spec.get('name')!r} cites unknown sources {unknown}")
            for deliverable in self.uncovered_deliverables(key, plan, specs):
                errors.append(f"{where}: it must make {deliverable} files, but no matched capability does; "
                              f"match again with intent naming {deliverable}, or add a Skill for it")
            missing = self.uncovered_platforms(key, plan.get("reach") or [])
            if missing:
                note = f"{where}: its capabilities do not reach {', '.join(missing)}, which it works on"
                # Matching again selects from the same catalog: after the last try the lead decides.
                if sum(1 for matched in self.matched if matched[0] == key) >= MAX_MATCHES_PER_SERVICE:
                    warnings.append(note)
                else:
                    errors.append(f"{note}; match again with an intent naming them")
            if plan.get("setup_required"):
                warnings.append(f"{where}: waits for a connection before it can use "
                                f"{', '.join(str(item.get('provider') or item.get('name') or item) for item in plan['setup_required'][:3])}")
            normalized.append({
                "service_key": key, "agent_name": name, "agent_description": str(row.get("agent_description") or ""),
                "system_prompt": prompt, "missing_skill_specs": [
                    {**{k: spec[k] for k in ("name", "system_prompt", "description") if spec.get(k)},
                     **({"source_ids": list(spec["source_ids"])} if spec.get("source_ids") else {})} for spec in specs],
                "rationale": str(row.get("rationale") or ""), "plan": copy.deepcopy(plan),
            })
        return (None if errors else {"agents": normalized}), errors, warnings


def candidate_label(candidate: Any) -> str:
    row = candidate.prompt_dict()
    return f"{row.get('id')} {row.get('name')} {row.get('description')}".lower()


def reaches_account(candidate: Any) -> bool:
    """Whether a capability works in a connected account, rather than on files that mention one."""
    from packages.core.services.agent_capability_catalog import AgentCapabilityKind

    return candidate.kind is AgentCapabilityKind.MCP_ACTION or any(
        str(tool).startswith("mcp__") for tool in candidate.metadata.get("declared_tools") or ())


def platforms_out_of_reach(platforms: list[str], reach: list[str], catalog: Any) -> list[str]:
    """The named platforms ``reach`` does not cover. One no capability names is worked through the browser, if any."""
    text = " ".join(reach)
    if _BROWSER.search(text):
        return []
    available = " ".join(candidate_label(candidate) for candidate in catalog.selectable_candidates()
                         if reaches_account(candidate))
    browser = bool(_BROWSER.search(available))

    def named(platform: str, words: str) -> bool:
        return any(re.search(rf"(?<![a-z0-9]){re.escape(form)}(?![a-z0-9])", words)
                   for form in {platform.lower(), platform.lower().replace("_", " ")})

    return [platform for platform in platforms if not named(platform, text) and (browser or named(platform, available))]


async def spec_platform_issues(db: Any, fields: dict[str, Any], *, entity_id: str, user_id: str) -> list[dict[str, Any]]:
    """Lint: the saved Agents, however they were made, against the platforms the spec says they work on."""
    from packages.core.ai.tools.workspace_arch_tools import _search_entity_agents
    from packages.core.services.agent_capability_catalog import AgentCapabilityCatalogFactory
    from packages.core.services.workspace_build_spec import spec_slice

    platforms = {row.get("service_key"): row.get("integrations") or []
                 for row in spec_slice(fields, "agents").get("agents") or [] if isinstance(row, dict)}
    if not any(platforms.values()):
        return []
    context = AgentsDesignContext(entity_id=entity_id)
    context.catalog = await AgentCapabilityCatalogFactory.create(db, entity_id=entity_id, user_id=user_id or "")
    listed = json.loads(await _search_entity_agents(db, entity_id=entity_id, user_id=user_id or ""))
    agents = {row.get("id"): row for row in listed.get("agents") or [] if isinstance(row, dict)}
    issues = []
    for mapping in fields.get("agent_mappings") or []:
        key = (mapping or {}).get("service_key")
        if not platforms.get(key):
            continue
        agent = (mapping.get("create_agent_draft") or agents.get(mapping.get("agent_id") or mapping.get("recommended_agent_id")))
        if not agent:
            continue
        missing = platforms_out_of_reach(platforms[key], context.agent_reach(_binding_rows(agent)), context.catalog)
        if missing:
            issues.append({"severity": "P1", "where": f"agent_mappings.{key}", "message": (
                f"the build spec says '{key}' works on {', '.join(missing)}, but its Agent cannot reach them: its "
                "Workflow steps will report them as unknown. Give it a custom Agent matched for them.")})
    return issues


def _binding_rows(agent: dict[str, Any]) -> dict[str, Any]:
    """An Agent's bindings in the listing's shape; a draft's custom Agent keeps Skills as bare ids."""
    return {"mcp_bindings": agent.get("mcp_bindings"), "skill_bindings": [
        row if isinstance(row, dict) else {"id": row} for row in agent.get("skill_bindings") or []]}


def _mapping_summary(mapping: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(mapping, dict):
        return None
    custom = mapping.get("create_agent_draft") or {}
    return {"strategy": mapping.get("strategy"),
            "agent": mapping.get("recommended_agent_name") or custom.get("agent_name"),
            "skills": len(custom.get("skill_bindings") or []), "tools": len(custom.get("tool_bindings") or [])}


async def workspace_agents_design_context(db: Any, workspace: Any) -> AgentsDesignContext:
    """A live Workspace's services and rules, in the keys a Draft keeps."""
    model = dict(workspace.operating_model or {})
    services = [{**row, "service_key": str(row.get("service_key") or row.get("key"))}
                for row in model.get("services") or [] if isinstance(row, dict) and (row.get("service_key") or row.get("key"))]
    fields = {"name": workspace.name, "kind": getattr(workspace, "kind", None),
              "primary_work": model.get("primary_work") or getattr(workspace, "description", ""),
              "operating_context": model.get("operating_context") or "", "services": services,
              "rules": list(model.get("rules") or [])}
    return AgentsDesignContext(fields=fields, services=services)


# ── Sub-agent tools ─────────────────────────────────────────────────────────

def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


_S = {"type": "string"}

AGENTS_DESIGNER_TOOL_SCHEMAS = [
    _tool("ag_read_context",
          "Read the Workspace's services (what each does, the Workflow steps it runs, what the spec says it makes), "
          "its rules, integrations and the sources the person attached.", {}, []),
    _tool("ag_find_agents",
          "List existing Agents a service could reuse as they are, with their tools and Skills.",
          {"query": {"type": "string", "description": "Words of the service's work; empty lists all"}}, []),
    _tool("ag_match_capabilities",
          "Match one service's capabilities with Manor's capability matcher: the tools, Skills and MCP servers its "
          "Agent needs, least privilege. Returns what it selected and what waits for a connection. Call it once per "
          "custom Agent; again with intent to add what is missing (e.g. 'saves the edited article as a Word .docx').",
          {"service_key": _S, "intent": {"type": "string", "description": "Extra requirements, in plain words"}},
          ["service_key"]),
    _tool("ag_submit_candidate",
          "Submit one Agent per service. Reuse: {service_key, existing_agent_id, rationale}. Custom: {service_key, "
          "agent_name, agent_description, system_prompt, missing_skill_specs?, rationale}; its matched capabilities "
          "come with it. Runs every check; a failing candidate is not kept, so fix the errors and submit again.",
          {"agents": {"type": "array", "items": {"type": "object", "properties": {
              "service_key": _S, "existing_agent_id": _S, "agent_name": _S, "agent_description": _S,
              "system_prompt": {"type": "string", "description": "5-10 sentences: the Agent's capability, reusable "
                                                                 "across Workspaces, ending with a scope guard"},
              "missing_skill_specs": {"type": "array", "items": {"type": "object", "properties": {
                  "name": _S, "description": _S, "system_prompt": _S,
                  "source_ids": {"type": "array", "items": _S}}}},
              "rationale": _S}}}},
          ["agents"]),
]


def default_intent(context: AgentsDesignContext, service_key: str) -> str:
    """What the spec says a service's Agent makes and where it works, as matching requirements."""
    spec = context.spec_agent(service_key)
    parts = []
    if spec.get("deliverables"):
        # The candidate check reads these words, so the review has to keep them visible.
        parts.append(f"Makes {', '.join(spec['deliverables'])} files: keep, for each, a capability whose id, name or "
                     "description names that kind of file, even when another selected capability could also make it.")
    if spec.get("integrations"):
        # The candidate check reads the selection for these too, and a whole-Workspace brief names other platforms.
        parts.append(f"Works on {', '.join(spec['integrations'])}: keep for each a Skill or MCP server that works in "
                     "that account, connected or not; one with neither of its own is worked through mcp:chrome. "
                     "Select no other platform's.")
    return " ".join(parts)


async def match_service(context: AgentsDesignContext, service_key: str, intent: str) -> dict[str, Any]:
    """Match one service's capabilities; kept on the context by (service, intent) for the run's tools."""
    from packages.core.ai.tools.workspace_arch_tools import (
        _agent_capability_catalog_fingerprint,
        _agent_capability_context,
        _agent_capability_prompt,
        _workspace_service_plan,
    )
    from packages.core.services.agent_generator import match_agent_capabilities

    key = (service_key, intent)
    if key in context.matched:
        return context.matched[key]
    service = context.service(service_key)
    if service is None:
        return {"reply": {"ok": False, "error": f"unknown service {service_key!r}"}}
    if context.catalog is None:
        return {"reply": {"ok": False, "error": "the capability catalog is not available"}}
    capability_context = _agent_capability_context(context.fields, service=service, service_key=service_key,
                                                    intent=intent)
    plan = await match_agent_capabilities(
        prompt=_agent_capability_prompt(capability_context, service_key=service_key),
        spec={"name": f"{service.get('name') or service_key} Agent",
              "description": str(service.get("description") or ""),
              "category": str(context.fields.get("category") or context.fields.get("kind") or "")},
        entity_id=context.entity_id, capability_catalog=context.catalog,
    )
    plan = _workspace_service_plan(context.catalog, plan, service)
    selected = set(plan.selected_catalog_ids)
    chosen = [candidate.prompt_dict() for candidate in context.catalog.candidates if candidate.catalog_id in selected]
    reach = [candidate_label(candidate) for candidate in context.catalog.candidates
             if candidate.catalog_id in selected and reaches_account(candidate)]
    context.matched[key] = {
        "plan": {
            "service_key": service_key, "agent_name": f"{service.get('name') or service_key} Agent", "intent": intent,
            "capability_ids": list(plan.selected_catalog_ids), "status": plan.status.value,
            "setup_required": [dict(item) for item in plan.setup_required],
            "context_fingerprint": context.capability_fingerprint(service_key, intent),
            "catalog_fingerprint": _agent_capability_catalog_fingerprint(context.catalog),
            "labels": [f"{row.get('id')} {row.get('name')} {row.get('description')}" for row in chosen],
            "reach": reach,
        },
        "reply": {"ok": True, "service_key": service_key, "intent": intent,
                  "selected": [{key: row.get(key) for key in ("id", "kind", "name", "readiness")} for row in chosen],
                  "waits_for_connection": [dict(item) for item in plan.setup_required]},
    }
    return context.matched[key]


async def prematch_services(context: AgentsDesignContext, concurrency: int) -> None:
    """Match every service at once with the spec's requirements, before the designer starts.

    The designer reads the result instead of matching one service after
    another; one that fails is left for the designer to match again.
    """
    import asyncio

    slots = asyncio.Semaphore(max(1, concurrency))

    async def one(service_key: str) -> None:
        if any(key[0] == service_key for key in context.matched):
            return
        async with slots:
            try:
                await match_service(context, service_key, default_intent(context, service_key))
            except Exception:  # noqa: BLE001 - the designer matches it again
                logger.warning("Agents designer could not pre-match %s", service_key, exc_info=True)

    await asyncio.gather(*(one(row["service_key"]) for row in context.services))


def reuse_matches(context: AgentsDesignContext, sent_back: dict[str, Any] | None) -> None:
    """Keep the sent-back design's matches that still fit the service and the catalog."""
    from packages.core.ai.tools.workspace_arch_tools import _agent_capability_catalog_fingerprint

    if context.catalog is None or not isinstance(sent_back, dict):
        return
    catalog = _agent_capability_catalog_fingerprint(context.catalog)
    for row in (sent_back.get("value") or {}).get("agents") or []:
        plan = row.get("plan") if isinstance(row, dict) else None
        if not isinstance(plan, dict) or plan.get("catalog_fingerprint") != catalog:
            continue
        key, intent = str(row.get("service_key") or ""), str(plan.get("intent") or "")
        if plan.get("context_fingerprint") == context.capability_fingerprint(key, intent):
            context.matched[(key, intent)] = {"plan": plan, "reply": {
                "ok": True, "service_key": key, "intent": intent, "selected": plan.get("labels") or [],
                "waits_for_connection": plan.get("setup_required") or []}}


def agents_designer_executor(context: AgentsDesignContext, state: dict[str, Any]):
    """The run's tools: they read the snapshot and the catalog and keep the run's own candidate."""
    plans: dict[str, dict[str, Any]] = state.setdefault("plans", {})

    async def match(service_key: str, intent: str) -> dict[str, Any]:
        intent = intent or default_intent(context, service_key)
        tried = [key for key in context.matched if key[0] == service_key]
        if (service_key, intent) not in context.matched and len(tried) >= MAX_MATCHES_PER_SERVICE:
            # Matching again selects from the same catalog; what it lacks is a new Skill.
            return {"ok": False, "error": f"{service_key} was matched {len(tried)} times; keep its best match and "
                                          "add a missing_skill_spec for what the catalog does not have"}
        matched = await match_service(context, service_key, intent)
        if matched.get("plan"):
            plans[service_key] = matched["plan"]
        return matched["reply"]

    def plan_for(service_key: str) -> dict[str, Any] | None:
        """The service's last match in this run, else its match before the run."""
        return plans.get(service_key) or (context.latest_match(service_key) or {}).get("plan")

    async def executor(name: str, args: dict[str, Any]) -> str:
        args = dict(args or {})
        if name == "ag_read_context":
            summary = context.summary()
            for row in summary["services"]:
                matched = context.latest_match(row["service_key"])
                if matched:
                    row["matched_capabilities"] = matched["reply"]
                    row["missing_deliverables"] = context.uncovered_deliverables(row["service_key"], matched["plan"])
                    row["missing_platforms"] = context.uncovered_platforms(row["service_key"], matched["plan"].get("reach") or [])
            return json.dumps(summary, ensure_ascii=False, default=str)
        if name == "ag_find_agents":
            words = [word for word in str(args.get("query") or "").lower().split() if word]
            agents = [row for row in context.entity_agents or []
                      if not words or any(word in json.dumps(row, ensure_ascii=False).lower() for word in words)]
            return json.dumps({"agents": agents[:20]}, ensure_ascii=False, default=str)
        if name == "ag_match_capabilities":
            try:
                reply = await match(str(args.get("service_key") or ""), str(args.get("intent") or "").strip())
            except Exception as exc:  # noqa: BLE001 - the designer may match again or reuse an Agent
                logger.warning("Agents designer capability match failed", exc_info=True)
                reply = {"ok": False, "error": f"matching failed: {exc}"}
            return json.dumps(reply, ensure_ascii=False, default=str)
        if name == "ag_submit_candidate":
            state["attempts"] = state.get("attempts", 0) + 1
            rows = [dict(row) for row in args.get("agents") or [] if isinstance(row, dict)]
            for row in rows:
                row["plan"] = plan_for(str(row.get("service_key") or ""))
            candidate, errors, warnings = context.check({"agents": rows})
            if errors:
                return json.dumps({"ok": False, "errors": errors}, ensure_ascii=False)
            state["candidate"], state["warnings"] = candidate, warnings
            return json.dumps({"ok": True, "warnings": warnings,
                               "next": "Reply with a two-line design note."}, ensure_ascii=False)
        return json.dumps({"ok": False, "error": f"unknown tool: {name}"})

    return executor


async def _run_agents_designer(context: AgentsDesignContext, **kwargs: Any):
    """Load the reusable Agents and the capability catalog, then run the designer."""
    from packages.core.ai.tools.workspace_arch_tools import _search_entity_agents
    from packages.core.database import async_session
    from packages.core.services.agent_capability_catalog import AgentCapabilityCatalogFactory

    entity_id, user_id = kwargs["entity_id"], kwargs.get("user_id")
    async with async_session() as db:
        context.catalog = await AgentCapabilityCatalogFactory.create(db, entity_id=entity_id, user_id=user_id or "")
        listed = json.loads(await _search_entity_agents(db, entity_id=entity_id, user_id=user_id or ""))
        # Building the catalog the first time seeds the built-in Skills; the plan names their ids.
        await db.commit()
    context.entity_id = entity_id
    context.entity_agents = [
        {key: row.get(key) for key in ("id", "name", "description", "tool_bindings", "skill_bindings", "mcp_bindings")}
        for row in listed.get("agents") or [] if isinstance(row, dict)]
    from packages.core.services.workspace_part_designers import DESIGN_CONCURRENCY

    # A design sent back keeps its matches; only a changed service is matched again.
    reuse_matches(context, kwargs.get("sent_back"))
    await prematch_services(context, DESIGN_CONCURRENCY)
    return await run_part_designer(AGENTS_DESIGNER, context, **kwargs)


# ── Accept: the lead's own write tools ──────────────────────────────────────

async def _apply_agents(db: Any, record: dict[str, Any], *, entity_id: str, user_id: str, draft_id: str,
                        draft_phase: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Store each matched plan, then write each Agent as ``ws_request_custom_agent`` does."""
    from packages.core.ai.tools import workspace_arch_tools as tools
    from packages.core.services.workspace_part_designers import drop_candidate, keep_candidate

    candidate = record.get("value") or {}
    async with draft_phase():
        draft = await tools._load_draft(db, draft_id, entity_id, user_id)
        if draft is None:
            return None, {"error": "draft not found"}
        fields = dict(draft.fields or {})
        _, errors, _ = AgentsDesignContext.from_draft_fields(fields).check(candidate)
        if errors:
            return None, {"error": "the Draft changed since this design was checked; command a new design",
                          "errors": errors}
        for row in candidate["agents"]:
            if row.get("plan") and not row.get("existing_agent_id"):
                plan = {key: value for key, value in row["plan"].items() if key not in {"labels", "reach"}}
                fields[tools._AGENT_CAPABILITY_PLANS_FIELD] = tools._replace_in_list(
                    fields.get(tools._AGENT_CAPABILITY_PLANS_FIELD) or [], "service_key", row["service_key"], plan)
        draft.fields = fields
        await tools._persist(db, draft)
    written: dict[str, Any] = {}
    failed: dict[str, Any] = {}
    applied = set(record.get("applied_services") or [])
    for row in candidate["agents"]:
        key = row["service_key"]
        if key in applied:
            continue
        if row.get("existing_agent_id"):
            async with draft_phase():
                raw = await tools._propose_agent_mapping(db, entity_id=entity_id, user_id=user_id, draft_id=draft_id,
                                                         service_key=key, agent_id=row["existing_agent_id"],
                                                         rationale=row.get("rationale", ""))
        else:
            raw = await tools._request_custom_agent(
                db, entity_id=entity_id, user_id=user_id, draft_id=draft_id, draft_phase=draft_phase,
                service_key=key, agent_name=row["agent_name"], agent_description=row.get("agent_description", ""),
                system_prompt=row["system_prompt"], missing_skill_specs=row.get("missing_skill_specs") or [],
                rationale=row.get("rationale", ""))
        reply = json.loads(raw) if isinstance(raw, str) else {"ok": False, "error": "no result"}
        (written if reply.get("ok") else failed)[key] = reply
    async with draft_phase():
        draft = await tools._load_draft(db, draft_id, entity_id, user_id)
        if draft is not None:
            fields = dict(draft.fields or {})
            if failed:
                left = {**record, "applied_services": sorted(applied | set(written))}
                keep_candidate(fields, "agents", left)
            else:
                drop_candidate(fields, "agents")
            draft.fields = fields
            await tools._persist(db, draft)
    summary = {"agents": {key: {k: reply.get(k) for k in ("strategy", "agent_name", "tool_bindings", "skill_bindings",
                                                          "mcp_bindings", "missing_skill_specs", "missing_integrations")
                                if reply.get(k) is not None} for key, reply in written.items()}}
    if failed:
        return summary, {"error": "some Agents could not be saved; the rest are kept",
                         "failed": {key: reply.get("error") for key, reply in failed.items()}, "accepted": summary}
    return summary, None


def agents_candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    return {"agents": [{"service_key": row["service_key"], **(
        {"reuses": row["existing_agent_id"]} if row.get("existing_agent_id") else {
            "agent_name": row["agent_name"], "capabilities": len((row.get("plan") or {}).get("capability_ids") or []),
            "waits_for_connection": [item.get("provider") or item.get("name") for item in
                                     (row.get("plan") or {}).get("setup_required") or []],
            "new_skills": [spec.get("name") for spec in row.get("missing_skill_specs") or []]})}
        for row in candidate["agents"]]}


def _agents_blockers(context: AgentsDesignContext) -> dict[str, Any] | None:
    if not context.services:
        return {"error": "save the services before commanding the Agents design"}
    return None


AGENTS_DESIGNER = PartDesigner(
    part="agents",
    title="Agents",
    subagent=SubAgentSpec(
        name=WORKSPACE_AGENTS_DESIGNER_SUBAGENT,
        purpose="Design the Agent each service of a Workspace gets, with matched capabilities, and submit it.",
        profile=RuntimeProfile.WORKSPACE_ARCHITECT,
        allowed_tools=tuple(schema["function"]["name"] for schema in AGENTS_DESIGNER_TOOL_SCHEMAS),
        output_contract={"format": "agents_candidate", "required_fields": ("agents", "design_notes")},
    ),
    skill="workspace-agents-design",
    tool_schemas=tuple(AGENTS_DESIGNER_TOOL_SCHEMAS),
    executor=agents_designer_executor,
    context_from_draft=AgentsDesignContext.from_draft_fields,
    context_from_workspace=workspace_agents_design_context,
    check=AgentsDesignContext.check,
    accept=lambda _fields, _candidate: {},
    apply=_apply_agents,
    summarize=agents_candidate_summary,
    command_tool=PARTS_COMMAND_TOOL,
    brief_hint="say what each role does, what it must make (e.g. Word files) and which platforms it works on",
    blockers=_agents_blockers,
    revise_note=lambda context: ("Some services already have an Agent; keep each the brief does not ask to change."
                                 if context.fields.get("agent_mappings") else None),
    run=_run_agents_designer,
    lint_where=("agent_mappings",),
    fallback="Design them yourself: ws_search_capabilities, then ws_request_custom_agent per service.",
)
