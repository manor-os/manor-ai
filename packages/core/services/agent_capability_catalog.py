"""Actor-scoped capability catalog and deterministic Agent binding factory.

The LLM is responsible for semantic selection.  This module is deliberately
boring after that point: catalog ids are exact, actor visibility is checked
while the snapshot is built, and materialisation never falls back to fuzzy
names or a different resource with a similar slug.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.constants.agent_capabilities import AGENT_CAPABILITY_SELECTION_LIMIT
from packages.core.models.base import generate_ulid
from packages.core.models.mcp import MCPServer
from packages.core.models.permission import ResourceType
from packages.core.models.skill import Skill
from packages.core.models.workspace import ToolDefinition
from packages.core.services.provider_keys import canonical_provider_key
from packages.core.services.resource_access import (
    ResourceDescriptor,
    readable_resource_ids,
)


class AgentCapabilityKind(StrEnum):
    BUSINESS_CAPABILITY = "business_capability"
    TOOL = "tool"
    SKILL = "skill"
    MCP_ACTION = "mcp_action"


_SERVER_LEVEL_MCP_PROVIDERS = frozenset({
})


class AgentCapabilityPlanStatus(StrEnum):
    READY = "ready"
    SETUP_REQUIRED = "setup_required"


class AgentCapabilitySelectionError(ValueError):
    """Raised before any Agent row is written when a selection is invalid."""


@dataclass(frozen=True)
class AgentCapabilityCandidate:
    catalog_id: str
    kind: AgentCapabilityKind
    ref: str
    name: str
    description: str = ""
    readiness: AgentCapabilityPlanStatus = AgentCapabilityPlanStatus.READY
    actions: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def prompt_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.catalog_id,
            "kind": self.kind.value,
            "name": self.name,
            "description": self.description[:220],
            "readiness": self.readiness.value,
            **(
                {"setup_required_reason": self.metadata["setup_required_reason"]}
                if self.metadata.get("setup_required_reason")
                else {}
            ),
        }
        if (
            self.kind is AgentCapabilityKind.MCP_ACTION
            and self.actions
            and self.metadata.get("action")
        ):
            # The catalog already has one candidate per MCP action, so emitting
            # that full operation again under ``actions`` nearly doubles the
            # prompt.  Keep only the execution effect needed for safe semantic
            # selection; the exact action name is encoded in the catalog id.
            effect = str(self.actions[0].get("effect") or "").strip()
            if effect:
                payload["effect"] = effect
        elif self.actions:
            payload["actions"] = list(self.actions)
        return payload


@dataclass(frozen=True)
class AgentCapabilityPlan:
    selected_catalog_ids: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    business_capability_ids: tuple[str, ...] = ()
    skill_ids: tuple[str, ...] = ()
    mcp_server_keys: tuple[str, ...] = ()
    # None means the server has no discoverable operation list, so the
    # binding inherits the server's allowlist.  A tuple is an exact action
    # allowlist selected from the actor's current operation catalog.
    mcp_allowed_tools: dict[str, tuple[str, ...] | None] = field(default_factory=dict)
    setup_required: tuple[dict[str, Any], ...] = ()

    @property
    def status(self) -> AgentCapabilityPlanStatus:
        if self.setup_required:
            return AgentCapabilityPlanStatus.SETUP_REQUIRED
        return AgentCapabilityPlanStatus.READY

    def public_dict(self) -> dict[str, Any]:
        return {
            "capability_ids": list(self.selected_catalog_ids),
            "status": self.status.value,
            "tool_bindings": list(self.tool_names),
            "business_capabilities": list(self.business_capability_ids),
            "skill_bindings": list(self.skill_ids),
            "mcp_bindings": list(self.mcp_server_keys),
            "mcp_allowed_tools": {
                key: (list(value) if value is not None else None)
                for key, value in self.mcp_allowed_tools.items()
            },
            "setup_required": [dict(item) for item in self.setup_required],
        }


@dataclass(frozen=True)
class AgentCapabilityCatalog:
    candidates: tuple[AgentCapabilityCandidate, ...]
    business_capabilities: tuple[dict[str, Any], ...] = ()
    tools: tuple[dict[str, Any], ...] = ()
    skills: tuple[dict[str, Any], ...] = ()
    integrations: tuple[dict[str, Any], ...] = ()

    @property
    def by_id(self) -> dict[str, AgentCapabilityCandidate]:
        return {candidate.catalog_id: candidate for candidate in self.candidates}

    def prompt_payload(self) -> list[dict[str, Any]]:
        return [candidate.prompt_dict() for candidate in self.candidates]

    def workspace_payload(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "business_capabilities": [dict(item) for item in self.business_capabilities],
            "tools": [dict(item) for item in self.tools],
            "skills": [dict(item) for item in self.skills],
            "integrations": [dict(item) for item in self.integrations],
        }

    def resolve(self, catalog_ids: Iterable[object]) -> AgentCapabilityPlan:
        selected_ids = _unique_strings(catalog_ids)
        if len(selected_ids) > AGENT_CAPABILITY_SELECTION_LIMIT:
            raise AgentCapabilitySelectionError(
                f"at most {AGENT_CAPABILITY_SELECTION_LIMIT} capability ids may be selected"
            )

        candidates_by_id = self.by_id
        unknown = [catalog_id for catalog_id in selected_ids if catalog_id not in candidates_by_id]
        if unknown:
            raise AgentCapabilitySelectionError(
                "unknown or inaccessible capability ids: " + ", ".join(unknown)
            )

        tool_names: list[str] = []
        business_ids: list[str] = []
        skill_ids: list[str] = []
        mcp_actions: dict[str, set[str] | None] = {}
        setup_by_key: dict[str, dict[str, Any]] = {}

        def add_tool(name: object) -> None:
            value = str(name or "").strip()
            if value and value not in tool_names:
                tool_names.append(value)

        for catalog_id in selected_ids:
            candidate = candidates_by_id[catalog_id]
            if candidate.kind is AgentCapabilityKind.TOOL:
                add_tool(candidate.ref)
            elif candidate.kind is AgentCapabilityKind.BUSINESS_CAPABILITY:
                if candidate.ref not in business_ids:
                    business_ids.append(candidate.ref)
                for name in candidate.metadata.get("tool_names") or ():
                    add_tool(name)
            elif candidate.kind is AgentCapabilityKind.SKILL:
                if candidate.ref not in skill_ids:
                    skill_ids.append(candidate.ref)
            elif candidate.kind is AgentCapabilityKind.MCP_ACTION:
                server_key = str(candidate.metadata.get("server_key") or candidate.ref)
                action = str(candidate.metadata.get("action") or "").strip()
                if not action:
                    mcp_actions[server_key] = None
                elif server_key not in mcp_actions:
                    mcp_actions[server_key] = {action}
                elif mcp_actions[server_key] is not None:
                    assert isinstance(mcp_actions[server_key], set)
                    mcp_actions[server_key].add(action)

            if candidate.readiness is AgentCapabilityPlanStatus.SETUP_REQUIRED:
                setup_key = str(candidate.metadata.get("server_key") or candidate.catalog_id)
                setup_by_key[setup_key] = {
                    "catalog_id": candidate.catalog_id,
                    "provider": candidate.metadata.get("server_key"),
                    "reason": candidate.metadata.get("setup_required_reason") or "connection required",
                    "setup_kind": candidate.metadata.get("setup_kind"),
                }

        if skill_ids:
            invoke_skill = next(
                (
                    candidate
                    for candidate in self.candidates
                    if candidate.kind is AgentCapabilityKind.TOOL
                    and candidate.ref == "invoke_skill"
                ),
                None,
            )
            if invoke_skill is None:
                raise AgentCapabilitySelectionError(
                    "selected Skills are not runnable because invoke_skill is unavailable"
                )
            add_tool("invoke_skill")

        allowed_tools = {
            server_key: (
                tuple(sorted(actions)) if isinstance(actions, set) else None
            )
            for server_key, actions in sorted(mcp_actions.items())
        }
        return AgentCapabilityPlan(
            selected_catalog_ids=tuple(selected_ids),
            tool_names=tuple(tool_names),
            business_capability_ids=tuple(business_ids),
            skill_ids=tuple(skill_ids),
            mcp_server_keys=tuple(allowed_tools),
            mcp_allowed_tools=allowed_tools,
            setup_required=tuple(setup_by_key.values()),
        )

    def resolve_exact_refs(
        self,
        *,
        tool_names: Iterable[object] = (),
        business_capability_ids: Iterable[object] = (),
        skill_ids: Iterable[object] = (),
        mcp_server_keys: Iterable[object] = (),
    ) -> AgentCapabilityPlan:
        """Translate legacy exact refs without fuzzy names or Skill slugs."""

        requested: list[str] = []
        missing: list[str] = []

        def add_matches(kind: AgentCapabilityKind, ref: object, label: str) -> None:
            value = str(ref or "").strip()
            matches = [
                candidate.catalog_id
                for candidate in self.candidates
                if candidate.kind is kind and candidate.ref == value
            ]
            if matches:
                requested.extend(matches)
            elif value:
                missing.append(f"{label}:{value}")

        for ref in _unique_strings(tool_names):
            add_matches(AgentCapabilityKind.TOOL, ref, "tool")
        for ref in _unique_strings(business_capability_ids):
            add_matches(AgentCapabilityKind.BUSINESS_CAPABILITY, ref, "capability")
        for ref in _unique_strings(skill_ids):
            add_matches(AgentCapabilityKind.SKILL, ref, "skill")
        for ref in _unique_strings(mcp_server_keys):
            add_matches(AgentCapabilityKind.MCP_ACTION, ref, "mcp")

        if missing:
            raise AgentCapabilitySelectionError(
                "unknown or inaccessible exact refs: " + ", ".join(missing)
            )
        return self.resolve(requested)


async def _ensure_integration_tool_definitions(
    db: AsyncSession,
    *,
    servers: Iterable[MCPServer],
    operations_by_server: dict[str, list[dict[str, Any]]],
) -> int:
    """Mirror only public MCP metadata into the global Agent editor catalog.

    Runtime invocation still resolves through the MCP registry and
    AgentMCPBinding. Actor/account-discovered schemas must never be passed here:
    ToolDefinition has no tenant owner and is exposed through a global catalog.
    Missing rows use an atomic upsert so concurrent catalog reads cannot race on
    the unique tool name.
    """

    values_by_name: dict[str, dict[str, Any]] = {}
    for server in servers:
        for operation in operations_by_server.get(server.server_key, []):
            tool_name = str(operation.get("tool_name") or "").strip()
            parts = tool_name.split("__", 2)
            if (
                len(tool_name) > 100
                or len(parts) != 3
                or parts[0] != "mcp"
                or canonical_provider_key(parts[1])
                != canonical_provider_key(server.server_key)
            ):
                continue
            description = str(operation.get("description") or "").strip()
            input_schema = operation.get("input_schema")
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}}
            values_by_name[tool_name] = {
                "id": generate_ulid(),
                "name": tool_name,
                "display_name": str(
                    f"{server.name} · {operation.get('label') or operation.get('name')}"
                )[:200],
                "description": description or None,
                "category": "mcp",
                "schema": {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": description,
                        "parameters": input_schema,
                    },
                },
                "status": "active",
            }
    if not values_by_name:
        return 0

    existing_by_name = {
        row.name: row
        for row in (await db.execute(
            select(ToolDefinition).where(
                ToolDefinition.name.in_(tuple(values_by_name))
            )
        )).scalars().all()
    }
    for name, existing in existing_by_name.items():
        for attribute_name, value in values_by_name[name].items():
            if attribute_name not in {"id", "name"}:
                setattr(existing, attribute_name, value)

    missing_values = [
        values for name, values in values_by_name.items()
        if name not in existing_by_name
    ]
    if missing_values:
        statement = pg_insert(ToolDefinition).values(missing_values)
        excluded = statement.excluded
        await db.execute(statement.on_conflict_do_update(
            index_elements=[ToolDefinition.name],
            set_={
                "display_name": excluded.display_name,
                "description": excluded.description,
                "category": excluded.category,
                "schema": excluded.schema,
                "status": excluded.status,
                "updated_at": func.now(),
            },
        ))
    await db.flush()
    return len(missing_values)


class AgentCapabilityCatalogFactory:
    """Build one uncached, actor-scoped view over existing registries/caches."""

    @classmethod
    async def create(
        cls,
        db: AsyncSession,
        *,
        entity_id: str,
        user_id: str,
        profile: RuntimeProfile = RuntimeProfile.WORKSPACE_OPERATOR,
    ) -> AgentCapabilityCatalog:
        from packages.core.ai.runtime import runtime_tool_is_eager_for_profile
        from packages.core.ai.runtime.capabilities import (
            CORE_CAPABILITIES,
            tool_names_for_capability_ids,
        )
        from packages.core.ai.runtime.tool_registry import runtime_registered_tool_schemas
        from packages.core.services.agent_service import ensure_runtime_tool_definitions
        from packages.core.services.builtin_skill_loader import seed_builtin_skills
        from packages.core.services.integration_operation_catalog import (
            actor_integration_operation_catalog,
            integration_operation_catalog,
        )
        from packages.core.services.integration_resolution import (
            integration_provider_readiness,
            supported_integration_provider_keys,
        )

        await ensure_runtime_tool_definitions(db)
        await seed_builtin_skills(db)
        registered_schemas = dict(runtime_registered_tool_schemas())
        tool_rows = list((await db.execute(
            select(ToolDefinition).where(ToolDefinition.status == "active")
        )).scalars().all())

        candidates: list[AgentCapabilityCandidate] = []
        tools_out: list[dict[str, Any]] = []
        bindable_tool_names: set[str] = set()
        for tool in sorted(tool_rows, key=lambda item: item.name):
            name = str(tool.name or "").strip()
            if not name or name.startswith("mcp__") or name.startswith("ws_"):
                continue
            schema = registered_schemas.get(name)
            if not isinstance(schema, dict):
                continue
            function = schema.get("function") if isinstance(schema.get("function"), dict) else {}
            description = str(function.get("description") or tool.description or "")[:240]
            bindable_tool_names.add(name)
            tools_out.append({
                "name": name,
                "description": description,
                "parameters": function.get("parameters") or {},
                "always_loaded": runtime_tool_is_eager_for_profile(name, is_master=True),
            })
            candidates.append(AgentCapabilityCandidate(
                catalog_id=f"tool:{name}",
                kind=AgentCapabilityKind.TOOL,
                ref=name,
                name=str(tool.display_name or name.replace("_", " ").title()),
                description=description,
                metadata={"tool_definition_id": tool.id},
            ))

        business_out: list[dict[str, Any]] = []
        for capability in sorted(CORE_CAPABILITIES.values(), key=lambda item: item.id):
            if capability.id in {"workspace.architect", "file.patch"}:
                continue
            expanded = tuple(
                name
                for name in tool_names_for_capability_ids({capability.id}, profile=profile)
                if name in bindable_tool_names
            )
            if not expanded:
                continue
            item = {
                "id": capability.id,
                "name": capability.name,
                "description": capability.description,
                "tool_names": list(expanded),
                "risk_level": capability.risk_level,
                "required_approval": capability.required_approval,
            }
            business_out.append(item)
            candidates.append(AgentCapabilityCandidate(
                catalog_id=f"capability:{capability.id}",
                kind=AgentCapabilityKind.BUSINESS_CAPABILITY,
                ref=capability.id,
                name=capability.name,
                description=capability.description,
                actions=tuple({"name": name} for name in expanded),
                metadata={"tool_names": expanded},
            ))

        skill_rows = list((await db.execute(
            select(Skill).where(
                Skill.status == "active",
                or_(
                    Skill.entity_id == entity_id,
                    and_(Skill.entity_id.is_(None), Skill.is_public.is_(True)),
                ),
            )
        )).scalars().all())
        entity_skill_rows = [skill for skill in skill_rows if skill.entity_id is not None]
        readable_skill_ids = (
            await readable_resource_ids(
                db,
                descriptors=[
                    ResourceDescriptor.from_row(skill, ResourceType.SKILL)
                    for skill in entity_skill_rows
                ],
                entity_id=entity_id,
                user_id=user_id,
            )
            if user_id else {skill.id for skill in entity_skill_rows}
        )
        visible_skills = [
            skill
            for skill in skill_rows
            if skill.entity_id is None or skill.id in readable_skill_ids
        ]

        mcp_rows = list((await db.execute(
            select(MCPServer).where(MCPServer.status == "active")
        )).scalars().all())
        supported = await supported_integration_provider_keys(db)
        mcp_rows = [
            server for server in mcp_rows
            if canonical_provider_key(server.server_key) in supported
        ]
        readiness = await integration_provider_readiness(
            db,
            entity_id=entity_id,
            user_id=user_id or None,
            provider_keys=[server.server_key for server in mcp_rows],
        )

        integration_operations: dict[str, tuple[list[dict[str, Any]], str]] = {}
        public_editor_operations: dict[str, list[dict[str, Any]]] = {}
        for server in sorted(mcp_rows, key=lambda item: item.server_key):
            public_operations, public_source = integration_operation_catalog(
                server_key=server.server_key,
                transport=server.transport,
                tools_cached=server.tools_cached,
            )
            if user_id:
                operations, source = await actor_integration_operation_catalog(
                    db,
                    user_id=user_id,
                    entity_id=entity_id,
                    server_key=server.server_key,
                    transport=server.transport,
                    endpoint=server.endpoint,
                    tools_cached=server.tools_cached,
                )
            else:
                operations, source = public_operations, public_source
            integration_operations[server.server_key] = (operations, source)
            # ToolDefinition is global. Only the credential-free public server
            # catalog may enter it; actor/account discovery stays in the scoped
            # candidate snapshot and later AgentMCPBinding allowlist.
            public_editor_operations[server.server_key] = public_operations
        await _ensure_integration_tool_definitions(
            db,
            servers=mcp_rows,
            operations_by_server=public_editor_operations,
        )

        skills_out: list[dict[str, Any]] = []
        skills_by_provider: dict[str, list[dict[str, Any]]] = {}
        for skill in sorted(visible_skills, key=lambda item: (item.name or "", item.id)):
            required_providers = sorted(_mcp_provider_keys(skill.tools or []))
            dependencies: list[dict[str, Any]] = []
            skill_status = AgentCapabilityPlanStatus.READY
            first_setup: dict[str, Any] = {}
            for provider in required_providers:
                state = readiness.get(canonical_provider_key(provider))
                ready = bool(state and state.ready)
                dependency = {
                    "provider": canonical_provider_key(provider),
                    "connection_state": "ready" if ready else "setup_required",
                    "setup_required_reason": state.reason if state and not ready else "",
                    "setup_kind": state.setup_kind if state else None,
                }
                dependencies.append(dependency)
                if not ready and skill_status is AgentCapabilityPlanStatus.READY:
                    skill_status = AgentCapabilityPlanStatus.SETUP_REQUIRED
                    first_setup = {
                        "server_key": canonical_provider_key(provider),
                        "setup_required_reason": dependency["setup_required_reason"],
                        "setup_kind": dependency["setup_kind"],
                    }
            item = {
                "id": skill.id,
                "slug": skill.slug,
                "name": skill.name,
                "description": str(skill.description or "")[:200],
                "instructions_excerpt": str(skill.system_prompt or "")[:600],
                "tools": list(skill.tools or []),
                "scope": "entity" if skill.entity_id else "public",
                "required_integrations": dependencies,
            }
            skills_out.append(item)
            candidates.append(AgentCapabilityCandidate(
                catalog_id=f"skill:{skill.id}",
                kind=AgentCapabilityKind.SKILL,
                ref=skill.id,
                name=str(skill.display_name or skill.name),
                description=str(skill.description or ""),
                readiness=skill_status,
                actions=tuple({"name": name} for name in (skill.tools or [])),
                metadata={"slug": skill.slug, **first_setup},
            ))
            for provider in required_providers:
                skills_by_provider.setdefault(canonical_provider_key(provider), []).append({
                    "id": skill.id,
                    "slug": skill.slug,
                    "name": skill.name,
                    "description": str(skill.description or "")[:200],
                })

        integrations_out: list[dict[str, Any]] = []
        for server in sorted(mcp_rows, key=lambda item: item.server_key):
            provider = canonical_provider_key(server.server_key)
            state = readiness.get(provider)
            is_ready = bool(state and state.ready)
            plan_status = (
                AgentCapabilityPlanStatus.READY
                if is_ready else AgentCapabilityPlanStatus.SETUP_REQUIRED
            )
            operations, source = integration_operations[server.server_key]
            compact_operations = [
                {
                    "name": operation["name"],
                    "tool_name": operation["tool_name"],
                    "label": operation["label"],
                    "description": str(operation.get("description") or "")[:180],
                    "effect": operation["effect"],
                }
                for operation in operations
            ]
            setup_metadata = {
                "server_key": server.server_key,
                "setup_required_reason": state.reason if state and not is_ready else "",
                "setup_kind": state.setup_kind if state else None,
            }
            if compact_operations and provider in _SERVER_LEVEL_MCP_PROVIDERS:
                candidates.append(AgentCapabilityCandidate(
                    catalog_id=f"mcp:{server.server_key}",
                    kind=AgentCapabilityKind.MCP_ACTION,
                    ref=server.server_key,
                    name=server.name,
                    description=str(server.description or ""),
                    readiness=plan_status,
                    actions=tuple(dict(operation) for operation in compact_operations),
                    metadata={**setup_metadata, "all_actions": True},
                ))
            elif compact_operations:
                for operation in compact_operations:
                    action = str(operation["name"])
                    candidates.append(AgentCapabilityCandidate(
                        catalog_id=f"mcp:{server.server_key}:{action}",
                        kind=AgentCapabilityKind.MCP_ACTION,
                        ref=server.server_key,
                        name=f"{server.name} · {operation['label']}",
                        description=operation["description"],
                        readiness=plan_status,
                        actions=(dict(operation),),
                        metadata={**setup_metadata, "action": action},
                    ))
            else:
                candidates.append(AgentCapabilityCandidate(
                    catalog_id=f"mcp:{server.server_key}",
                    kind=AgentCapabilityKind.MCP_ACTION,
                    ref=server.server_key,
                    name=server.name,
                    description=str(server.description or ""),
                    readiness=plan_status,
                    metadata=setup_metadata,
                ))
            integrations_out.append({
                "mcp_server_key": server.server_key,
                "name": server.name,
                "description": str(server.description or "")[:200],
                "auth_type": server.auth_type,
                "active_integration": is_ready,
                "connection_state": "ready" if is_ready else "setup_required",
                "connection_scope": state.scope if state else "none",
                "setup_required_reason": state.reason if state and not is_ready else "",
                "setup_kind": state.setup_kind if state else None,
                "catalog_source": source,
                "tools": compact_operations,
                "related_skills": skills_by_provider.get(provider, []),
            })

        candidates.sort(key=lambda item: (item.kind.value, item.catalog_id))
        return AgentCapabilityCatalog(
            candidates=tuple(candidates),
            business_capabilities=tuple(business_out),
            tools=tuple(tools_out),
            skills=tuple(skills_out),
            integrations=tuple(integrations_out),
        )


async def materialize_agent_capability_plan(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    agent_id: str,
    plan: AgentCapabilityPlan,
) -> dict[str, list[str]]:
    """Bind a previously validated plan in the caller-owned transaction."""

    from packages.core.services.agent_provisioning_service import (
        AgentMCPBindingUpdateMode,
        _bind_existing_skills,
        _bind_mcp_servers,
        _bind_tools,
    )

    warnings: list[str] = []
    bound_tools = await _bind_tools(
        db,
        agent_id=agent_id,
        tool_names=list(plan.tool_names),
        warnings=warnings,
    )
    bound_skills, missing_skills = await _bind_existing_skills(
        db,
        agent_id=agent_id,
        entity_id=entity_id,
        workspace_id=None,
        refs=list(plan.skill_ids),
        requester_user_id=user_id,
    )
    warnings.extend(f"skill not found: {ref}" for ref in missing_skills)
    bound_mcp = await _bind_mcp_servers(
        db,
        agent_id=agent_id,
        refs=list(plan.mcp_server_keys),
        warnings=warnings,
        allowed_tools_by_server=plan.mcp_allowed_tools,
        update_mode=AgentMCPBindingUpdateMode.MERGE,
    )
    if warnings:
        raise AgentCapabilitySelectionError("; ".join(warnings))
    await db.flush()
    return {
        "tools": bound_tools,
        "skills": bound_skills,
        "mcp_servers": bound_mcp,
    }


def _unique_strings(values: Iterable[object]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _mcp_provider_keys(tool_names: Iterable[object]) -> set[str]:
    providers: set[str] = set()
    for raw_name in tool_names or ():
        parts = str(raw_name or "").split("__", 2)
        if len(parts) == 3 and parts[0] == "mcp" and parts[1]:
            providers.add(canonical_provider_key(parts[1]))
    return {provider for provider in providers if provider}


__all__ = [
    "AgentCapabilityCandidate",
    "AgentCapabilityCatalog",
    "AgentCapabilityCatalogFactory",
    "AgentCapabilityKind",
    "AgentCapabilityPlan",
    "AgentCapabilityPlanStatus",
    "AgentCapabilitySelectionError",
    "materialize_agent_capability_plan",
]
