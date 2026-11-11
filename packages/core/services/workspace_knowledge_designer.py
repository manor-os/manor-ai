"""The Knowledge design sub-agent: designs a Workspace's Knowledge nets and their seed documents.

A Knowledge net is a group of documents the Agents of some services read.
Each net the designer submits starts with documents: the sources the person
attached, or a starter document Manor writes after creation from the
outline the designer gives (sections it covers, in order). A net with
neither stays empty, so the check refuses it. It is the ``knowledge`` part
of the part designer registry (``workspace_part_designers``).
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.subagents import WORKSPACE_KNOWLEDGE_DESIGNER_SUBAGENT, SubAgentSpec
from packages.core.services.workspace_part_designers import PARTS_COMMAND_TOOL, PartDesigner

MAX_NETS = 6
MIN_OUTLINE_SECTIONS = 3
MAX_OUTLINE_SECTIONS = 12


@dataclass
class KnowledgeDesignContext:
    """What one Workspace has for designing its Knowledge, in portable keys."""

    services: list[dict[str, Any]] = field(default_factory=list)
    nets: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    spec: dict[str, Any] = field(default_factory=dict)
    workspace: dict[str, Any] = field(default_factory=dict)
    # The entity's Knowledge groups a net may start as a copy of; known only while a designer runs.
    templates: list[dict[str, Any]] | None = None

    @classmethod
    def from_draft_fields(cls, fields: dict[str, Any]) -> "KnowledgeDesignContext":
        from packages.core.services.workspace_build_spec import spec_slice
        from packages.core.services.workspace_draft_sources import draft_sources

        return cls(
            services=[{"service_key": row["service_key"], "name": row.get("name"), "description": row.get("description")}
                      for row in fields.get("services") or [] if isinstance(row, dict) and row.get("service_key")],
            nets=[copy.deepcopy(row) for row in fields.get("knowledge_attachments") or []
                  if isinstance(row, dict) and row.get("name")],
            sources=[{"id": row.get("id"), "name": row.get("name"), "kind": row.get("kind")}
                     for row in draft_sources(fields) if row.get("status") == "ready"],
            spec=spec_slice(fields, "knowledge"),
            workspace={key: fields.get(key) for key in ("name", "kind", "primary_work", "operating_context")},
        )

    def summary(self) -> dict[str, Any]:
        return {"workspace": self.workspace, "services": self.services, "sources": self.sources,
                "templates": self.templates or [],
                "planned": self.spec.get("knowledge") or [], "current_nets": [
                    {key: row.get(key) for key in ("name", "purpose", "linked_service_keys", "source_ids",
                                                   "template_group_id", "starter_outline")} for row in self.nets],
                "language": self.spec.get("language")}

    def check(self, value: Any) -> tuple[dict[str, Any] | None, list[str], list[str]]:
        """Check a candidate {nets: [{key?, name, purpose, linked_service_keys, source_ids, starter_outline}]}."""
        if not isinstance(value, dict) or not isinstance(value.get("nets"), list):
            return None, ["the candidate must be an object with nets"], []
        rows = [row for row in value["nets"] if isinstance(row, dict)]
        errors: list[str] = []
        warnings: list[str] = []
        if not rows:
            errors.append("the candidate has no Knowledge nets")
        if len(rows) > MAX_NETS:
            errors.append(f"at most {MAX_NETS} Knowledge nets; share one net between services that read the same")
        service_keys = {row["service_key"] for row in self.services}
        readable = {row["id"] for row in self.sources}
        names: list[str] = []
        normalized = []
        for row in rows:
            name = str(row.get("name") or "").strip()
            where = f"nets.{name or '?'}"
            if len(name) < 2:
                errors.append(f"{where}: name is required")
            elif name in names:
                errors.append(f"{where}: listed twice")
            names.append(name)
            purpose = str(row.get("purpose") or "").strip()
            if len(purpose) < 10:
                errors.append(f"{where}: purpose says what the Agents look up in it")
            linked = [str(key) for key in row.get("linked_service_keys") or []]
            if not linked:
                errors.append(f"{where}: name the services whose Agents read it")
            unknown = [key for key in linked if key not in service_keys]
            if unknown:
                errors.append(f"{where}: unknown services {unknown}")
            sources = [str(source) for source in row.get("source_ids") or []]
            missing = [source for source in sources if source not in readable]
            if missing:
                errors.append(f"{where}: {missing} are not ready sources of this Workspace")
            outline = [str(section).strip() for section in row.get("starter_outline") or [] if str(section).strip()]
            if outline and not MIN_OUTLINE_SECTIONS <= len(outline) <= MAX_OUTLINE_SECTIONS:
                errors.append(f"{where}: a starter outline has {MIN_OUTLINE_SECTIONS}-{MAX_OUTLINE_SECTIONS} sections")
            template = str(row.get("template_group_id") or "").strip()
            if template and self.templates is not None and template not in {item["id"] for item in self.templates}:
                errors.append(f"{where}: {template!r} is not one of the Knowledge groups it can copy")
            if not sources and not outline and not template:
                errors.append(f"{where}: it would start empty; attach the person's sources, copy a Knowledge group "
                              "or give the starter document an outline")
            normalized.append({"key": str(row.get("key") or ""), "name": name, "purpose": purpose,
                               "linked_service_keys": linked, "source_ids": sources, "starter_outline": outline,
                               **({"template_group_id": template} if template else {})})
        planned = self.spec.get("knowledge") or []
        for row in planned:
            if not any(net["key"] == row.get("key") or net["name"] == row.get("name") for net in normalized):
                errors.append(f"knowledge {row.get('key')!r} of the spec has no net")
            for source in row.get("source_ids") or []:
                if not any(source in net["source_ids"] for net in normalized):
                    errors.append(f"knowledge {row.get('key')!r}: source {source!r} is in no net")
        used = {source for net in normalized for source in net["source_ids"]}
        for source in self.sources:
            if source["id"] not in used:
                warnings.append(f"source {source['name']!r} is in no net; attach it where an Agent needs it")
        return (None if errors else {"nets": normalized}), errors, warnings


async def workspace_knowledge_design_context(db: Any, workspace: Any) -> KnowledgeDesignContext:
    """A live Workspace's services and Knowledge nets, in the keys a Draft keeps."""
    from sqlalchemy import select

    from packages.core.models.document import DocumentGroup

    model = dict(workspace.operating_model or {})
    groups = (await db.execute(select(DocumentGroup).where(
        DocumentGroup.workspace_id == workspace.id, DocumentGroup.entity_id == workspace.entity_id,
    ))).scalars().all()
    return KnowledgeDesignContext(
        services=[{"service_key": str(row.get("service_key") or row.get("key")), "name": row.get("name"),
                   "description": row.get("description")}
                  for row in model.get("services") or [] if isinstance(row, dict) and (row.get("service_key") or row.get("key"))],
        nets=[{"name": group.name, "purpose": (group.settings or {}).get("purpose", ""),
               "linked_service_keys": list((group.settings or {}).get("linked_service_keys") or [])}
              for group in groups if not (group.settings or {}).get("workspace_file_bucket")],
        workspace={"name": workspace.name, "primary_work": model.get("primary_work")},
    )


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


_S = {"type": "string"}
_LIST = {"type": "array", "items": {"type": "string"}}

KNOWLEDGE_DESIGNER_TOOL_SCHEMAS = [
    _tool("kn_read_context",
          "Read the Workspace's services, the sources the person attached, the team's Knowledge groups a net can "
          "copy (templates), the Knowledge the spec plans and the nets it has.", {}, []),
    _tool("kn_submit_candidate",
          "Submit the Knowledge nets. Each starts with documents: the person's sources (source_ids) and/or a starter "
          "document Manor writes after creation from starter_outline (3-12 section titles, in order, specific to "
          "this Workspace). Runs every check; a failing candidate is not kept, so fix the errors and submit again.",
          {"nets": {"type": "array", "items": {"type": "object", "properties": {
              "key": {"type": "string", "description": "The spec's knowledge key this net answers"},
              "name": _S, "purpose": {"type": "string", "description": "What the Agents look up in it"},
              "linked_service_keys": _LIST, "source_ids": _LIST,
              "template_group_id": {"type": "string", "description": "A Knowledge group from templates to copy"},
              "starter_outline": {**_LIST, "description": "Section titles of the starter document"}}}}},
          ["nets"]),
]


def knowledge_designer_executor(context: KnowledgeDesignContext, state: dict[str, Any]):
    async def executor(name: str, args: dict[str, Any]) -> str:
        if name == "kn_read_context":
            return json.dumps(context.summary(), ensure_ascii=False, default=str)
        if name == "kn_submit_candidate":
            state["attempts"] = state.get("attempts", 0) + 1
            candidate, errors, warnings = context.check(dict(args or {}))
            if errors:
                return json.dumps({"ok": False, "errors": errors}, ensure_ascii=False)
            state["candidate"], state["warnings"] = candidate, warnings
            return json.dumps({"ok": True, "warnings": warnings,
                               "next": "Fix the warnings and submit again, or reply with a two-line design note."},
                              ensure_ascii=False)
        return json.dumps({"ok": False, "error": f"unknown tool: {name}"})

    return executor


async def _run_knowledge_designer(context: KnowledgeDesignContext, **kwargs: Any):
    """Load the team's Knowledge groups a net may copy, then run the designer."""
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.document import DocumentGroup
    from packages.core.services.workspace_part_designers import run_part_designer

    async with async_session() as db:
        groups = (await db.execute(select(DocumentGroup).where(
            DocumentGroup.entity_id == kwargs["entity_id"], DocumentGroup.workspace_id.is_(None),
        ).limit(50))).scalars().all()
    context.templates = [{"id": group.id, "name": group.name, "purpose": (group.settings or {}).get("purpose", "")}
                         for group in groups if not (group.settings or {}).get("workspace_file_bucket")]
    return await run_part_designer(KNOWLEDGE_DESIGNER, context, **kwargs)


def _accept_knowledge(fields: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Write each net as ``ws_attach_knowledge`` does, with its starter outline."""
    from packages.core.ai.tools.workspace_arch_tools import (
        _reconcile_removed_channel_references,
        _scrub_new_item_for_removed_channels,
    )

    names = {net["name"] for net in candidate["nets"]}
    items = [row for row in fields.get("knowledge_attachments") or [] if (row or {}).get("name") not in names]
    for net in candidate["nets"]:
        items.append(_scrub_new_item_for_removed_channels({
            "name": net["name"], "purpose": net["purpose"],
            "mode": "clone_template" if net.get("template_group_id") else "create_new",
            "template_group_id": net.get("template_group_id"),
            "source_ids": net["source_ids"], "linked_service_keys": net["linked_service_keys"],
            "generate_starter_doc": bool(net["starter_outline"]), "starter_outline": net["starter_outline"],
            "approved": True, "rationale": "",
        }, fields))
    fields["knowledge_attachments"] = items
    _reconcile_removed_channel_references(fields)
    return {"nets": [{"name": net["name"], "sources": len(net["source_ids"]),
                      "starter_sections": len(net["starter_outline"])} for net in candidate["nets"]]}


def knowledge_candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    return {"nets": [{"name": net["name"], "for": net["linked_service_keys"], "sources": net["source_ids"],
                      "starter_outline": net["starter_outline"]} for net in candidate["nets"]]}


KNOWLEDGE_DESIGNER = PartDesigner(
    part="knowledge",
    title="Knowledge",
    subagent=SubAgentSpec(
        name=WORKSPACE_KNOWLEDGE_DESIGNER_SUBAGENT,
        purpose="Design a Workspace's Knowledge nets and the documents each starts with, and submit them.",
        profile=RuntimeProfile.WORKSPACE_ARCHITECT,
        allowed_tools=tuple(schema["function"]["name"] for schema in KNOWLEDGE_DESIGNER_TOOL_SCHEMAS),
        output_contract={"format": "knowledge_candidate", "required_fields": ("nets", "design_notes")},
    ),
    skill="workspace-knowledge-design",
    tool_schemas=tuple(KNOWLEDGE_DESIGNER_TOOL_SCHEMAS),
    executor=knowledge_designer_executor,
    context_from_draft=KnowledgeDesignContext.from_draft_fields,
    context_from_workspace=workspace_knowledge_design_context,
    check=KnowledgeDesignContext.check,
    accept=_accept_knowledge,
    summarize=knowledge_candidate_summary,
    command_tool=PARTS_COMMAND_TOOL,
    brief_hint="say what the Agents need to look up (brand voice, playbooks, product facts) and which sources hold it",
    blockers=lambda context: (None if context.services else {"error": "save the services before the Knowledge design"}),
    revise_note=lambda context: ("The Workspace has Knowledge nets; keep each the brief does not ask to change."
                                 if context.nets else None),
    run=_run_knowledge_designer,
    lint_where=("knowledge_attachments", "sources"),
    fallback="Attach them yourself with ws_attach_knowledge.",
)
