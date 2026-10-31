from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Iterable

from packages.core.ai.runtime.approvals import runtime_capability_id_for_action_key
from packages.core.ai.runtime.capabilities import (
    capabilities_for_tool_names,
    capability_for_id,
)
from packages.core.constants.integrations import INTEGRATION_ACCOUNT_SELECTION_ARGUMENT
from packages.core.services.official_remote_mcp import (
    MCPActionEffect,
    OfficialRemoteMCPActionPolicyFactory,
)
from packages.core.services.integration_account_service import (
    IntegrationAccountFanoutResultFactory,
    IntegrationAccountSelectionMode,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimePlannerActionBinding:
    provider: str
    action_key: str
    capability_id: str | None = None
    capability_name: str | None = None
    risk_level: str | None = None
    required_approval: bool | None = None
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    effect: MCPActionEffect | None = None
    account_ids: tuple[str, ...] = ()
    account_options: tuple[dict[str, Any], ...] = ()
    account_input_schemas: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    account_output_schemas: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    requires_explicit_account: bool = False
    supports_all_accounts: bool = False

    def input_schema_for(
        self,
        account_id: str | None,
        *,
        selection: str | None = None,
    ) -> dict[str, Any] | None:
        if (
            str(selection or "").strip().lower()
            == IntegrationAccountSelectionMode.ALL.value
        ):
            return IntegrationAccountFanoutResultFactory.input_schema(
                self.input_schema
            )
        selected = str(account_id or "").strip()
        if selected and selected in self.account_input_schemas:
            return self.account_input_schemas[selected]
        return self.input_schema

    def output_schema_for(
        self,
        account_id: str | None,
        *,
        selection: str | None = None,
    ) -> dict[str, Any] | None:
        if (
            str(selection or "").strip().lower()
            == IntegrationAccountSelectionMode.ALL.value
        ):
            return IntegrationAccountFanoutResultFactory.output_schema(
                self.output_schema
            )
        selected = str(account_id or "").strip()
        if selected and selected in self.account_output_schemas:
            return self.account_output_schemas[selected]
        return self.output_schema

    def to_dict(self, *, include_schema: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "action_key": self.action_key,
        }
        if self.description:
            payload["description"] = self.description
        if self.capability_id:
            payload["capability_id"] = self.capability_id
        if self.capability_name:
            payload["capability_name"] = self.capability_name
        if self.risk_level:
            payload["risk_level"] = self.risk_level
        if self.required_approval is not None:
            payload["required_approval"] = self.required_approval
        if self.effect is not None:
            payload["effect"] = self.effect.value
        if self.account_ids:
            payload["account_ids"] = list(self.account_ids)
        if self.account_options:
            payload["account_options"] = [dict(item) for item in self.account_options]
        if self.requires_explicit_account:
            payload["requires_explicit_account"] = True
        payload["supports_all_accounts"] = self.supports_all_accounts
        if self.input_schema is not None:
            payload["has_input_schema"] = True
            payload["parameters"] = _schema_parameter_names(self.input_schema)
            if include_schema:
                payload["input_schema"] = self.input_schema
                if self.account_input_schemas:
                    payload["account_input_schemas"] = {
                        account_id: dict(schema)
                        for account_id, schema in self.account_input_schemas.items()
                    }
        if self.output_schema is not None:
            payload["has_output_schema"] = True
            if include_schema:
                payload["output_schema"] = self.output_schema
                if self.account_output_schemas:
                    payload["account_output_schemas"] = {
                        account_id: dict(schema)
                        for account_id, schema in self.account_output_schemas.items()
                    }
        return payload


@dataclass(frozen=True)
class RuntimePlannerCapabilityCatalogEntry:
    capability_id: str
    name: str
    description: str
    risk_level: str
    required_approval: bool
    provider_actions: tuple[RuntimePlannerActionBinding, ...] = ()
    platform_tools: tuple[str, ...] = ()
    skills: tuple[dict[str, Any], ...] = ()
    sources: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "capability_id": self.capability_id,
            "name": self.name,
            "description": self.description,
            "risk_level": self.risk_level,
            "required_approval": self.required_approval,
        }
        if self.provider_actions:
            payload["provider_actions"] = [binding.to_dict() for binding in self.provider_actions]
        if self.platform_tools:
            payload["platform_tools"] = list(self.platform_tools)
        if self.skills:
            payload["skills"] = list(self.skills)
        if self.sources:
            payload["sources"] = list(self.sources)
        return payload


def runtime_planner_action_bindings(
    provider_actions: dict[str, list[str]] | None,
    *,
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> tuple[RuntimePlannerActionBinding, ...]:
    bindings: list[RuntimePlannerActionBinding] = []
    for provider, actions in (provider_actions or {}).items():
        provider_key = str(provider or "").strip()
        if not provider_key:
            continue
        action_specs = (provider_action_specs or {}).get(provider_key) or {}
        for action in _unique_clean_strings(actions):
            capability = capability_for_id(
                runtime_capability_id_for_action_key(action, provider=provider_key)
            )
            spec = action_specs.get(action) or {}
            try:
                effect = MCPActionEffect(str(spec.get("effect") or ""))
            except ValueError:
                effect = None
            effect_policy = (
                OfficialRemoteMCPActionPolicyFactory.create(provider_key, effect)
                if effect is not None
                else None
            )
            bindings.append(
                RuntimePlannerActionBinding(
                    provider=provider_key,
                    action_key=action,
                    capability_id=capability.id if capability else None,
                    capability_name=capability.name if capability else None,
                    risk_level=(
                        effect_policy.risk_level
                        if effect_policy is not None
                        else (capability.risk_level if capability else None)
                    ),
                    required_approval=(
                        effect_policy.required_approval
                        if effect_policy is not None
                        else (capability.required_approval if capability else None)
                    ),
                    description=_clean_description(spec.get("description")),
                    input_schema=_first_schema(
                        spec.get("input_schema"),
                        spec.get("parameters"),
                    ),
                    output_schema=_first_schema(
                        spec.get("output_schema"),
                        spec.get("result_schema"),
                    ),
                    effect=effect,
                    account_ids=tuple(
                        str(account_id).strip()
                        for account_id in spec.get("account_ids", ())
                        if str(account_id or "").strip()
                    ),
                    account_options=tuple(
                        dict(option)
                        for option in spec.get("account_options", ())
                        if isinstance(option, dict)
                    ),
                    account_input_schemas=_schema_mapping(
                        spec.get("account_input_schemas")
                    ),
                    account_output_schemas=_schema_mapping(
                        spec.get("account_output_schemas")
                    ),
                    requires_explicit_account=(
                        spec.get("requires_explicit_account") is True
                    ),
                    supports_all_accounts=(
                        spec["supports_all_accounts"]
                        if isinstance(spec.get("supports_all_accounts"), bool)
                        else effect is MCPActionEffect.READ
                    ),
                )
            )
    return tuple(sorted(bindings, key=lambda item: (item.capability_id or "", item.provider, item.action_key)))


def runtime_planner_action_binding_for(
    *,
    provider: str,
    action_key: str,
    provider_actions: dict[str, list[str]] | None,
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> RuntimePlannerActionBinding | None:
    provider_key = str(provider or "").strip()
    action = str(action_key or "").strip()
    if not provider_key or not action:
        return None
    for binding in runtime_planner_action_bindings(
        provider_actions,
        provider_action_specs=provider_action_specs,
    ):
        if binding.provider == provider_key and binding.action_key == action:
            return binding
    return None


def runtime_planner_action_specs_from_tools_cached(
    tools_cached: Any,
) -> dict[str, dict[str, Any]]:
    """Normalize MCP/OpenAI-style cached tool specs into action specs.

    The cache shape is intentionally loose because built-in MCP wrappers,
    remote MCP ``tools/list``, and older seed data do not all use the same
    field names. Runtime owns this normalization so planners can depend on a
    stable action binding catalog instead of re-parsing every provider shape.
    """
    specs: dict[str, dict[str, Any]] = {}
    for raw_tool in _iter_cached_tool_specs(tools_cached):
        spec = _normalize_cached_tool_spec(raw_tool)
        action_key = str(spec.get("action_key") or "").strip()
        if not action_key:
            continue
        specs[action_key] = spec
    return specs


def runtime_apply_action_binding_schemas_to_steps(
    steps: Iterable[Any],
    *,
    provider_actions: dict[str, list[str]] | None,
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> int:
    """Attach Runtime action binding schemas to mutable plan-like steps.

    Planner output can omit ``expected_input_schema`` / ``expected_output_schema``
    after using ``get_tool_schema`` because the provider/action binding already
    carries the schema. This helper lets callers hydrate those fields from the
    Runtime catalog before materializing ExecutionStep rows.
    """
    attached = 0
    for step in steps or ():
        if str(getattr(step, "kind", "") or "") != "action":
            continue
        binding = runtime_planner_action_binding_for(
            provider=str(getattr(step, "provider", "") or ""),
            action_key=str(getattr(step, "action_key", "") or ""),
            provider_actions=provider_actions,
            provider_action_specs=provider_action_specs,
        )
        if binding is None:
            continue
        account_id = str(getattr(step, "integration_id", "") or "").strip()
        selection = (getattr(step, "params", None) or {}).get(
            INTEGRATION_ACCOUNT_SELECTION_ARGUMENT
        )
        input_schema = binding.input_schema_for(
            account_id,
            selection=selection,
        )
        output_schema = binding.output_schema_for(
            account_id,
            selection=selection,
        )
        if input_schema is not None:
            if getattr(step, "expected_input_schema", None) != input_schema:
                attached += 1
            setattr(step, "expected_input_schema", input_schema)
        if output_schema is not None:
            if getattr(step, "expected_output_schema", None) != output_schema:
                attached += 1
            setattr(step, "expected_output_schema", output_schema)
    return attached


def runtime_planner_capability_catalog(
    *,
    provider_actions: dict[str, list[str]] | None = None,
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
    platform_tools: Iterable[str] | None = None,
    skills: Iterable[dict[str, Any]] | None = None,
) -> tuple[RuntimePlannerCapabilityCatalogEntry, ...]:
    grouped_actions: dict[str, list[RuntimePlannerActionBinding]] = {}
    for binding in runtime_planner_action_bindings(
        provider_actions,
        provider_action_specs=provider_action_specs,
    ):
        if binding.capability_id:
            grouped_actions.setdefault(binding.capability_id, []).append(binding)

    grouped_tools: dict[str, set[str]] = {}
    tool_names = set(_unique_clean_strings(platform_tools))
    for capability in capabilities_for_tool_names(tool_names):
        grouped_tools.setdefault(capability.id, set()).update(
            name for name in capability.tool_names if name in tool_names
        )

    skill_list = tuple(_normalized_skill_dicts(skills))
    skill_capability_ids = {"skill.invoke"} if skill_list else set()

    capability_ids = sorted(set(grouped_actions) | set(grouped_tools) | skill_capability_ids)
    entries: list[RuntimePlannerCapabilityCatalogEntry] = []
    for capability_id in capability_ids:
        capability = capability_for_id(capability_id)
        if capability is None:
            continue
        sources: list[str] = []
        if grouped_actions.get(capability_id):
            sources.append("provider_action")
        if grouped_tools.get(capability_id):
            sources.append("platform_tool")
        entry_skills = skill_list if capability_id == "skill.invoke" else ()
        if entry_skills:
            sources.append("skill")
        entries.append(
            RuntimePlannerCapabilityCatalogEntry(
                capability_id=capability.id,
                name=capability.name,
                description=capability.description,
                risk_level=capability.risk_level,
                required_approval=capability.required_approval,
                provider_actions=tuple(grouped_actions.get(capability_id, ())),
                platform_tools=tuple(sorted(grouped_tools.get(capability_id, ()))),
                skills=entry_skills,
                sources=tuple(sources),
            )
        )
    return tuple(entries)


def runtime_planner_capability_catalog_dicts(
    *,
    provider_actions: dict[str, list[str]] | None = None,
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
    platform_tools: Iterable[str] | None = None,
    skills: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return [
        entry.to_dict()
        for entry in runtime_planner_capability_catalog(
            provider_actions=provider_actions,
            provider_action_specs=provider_action_specs,
            platform_tools=platform_tools,
            skills=skills,
        )
    ]


def runtime_planner_capability_catalog_text(
    *,
    provider_actions: dict[str, list[str]] | None = None,
    provider_action_specs: dict[str, dict[str, dict[str, Any]]] | None = None,
    platform_tools: Iterable[str] | None = None,
    skills: Iterable[dict[str, Any]] | None = None,
) -> str:
    entries = runtime_planner_capability_catalog(
        provider_actions=provider_actions,
        provider_action_specs=provider_action_specs,
        platform_tools=platform_tools,
        skills=skills,
    )
    if not entries:
        return "  (no runtime capabilities available)"
    lines: list[str] = []
    for entry in entries:
        approval = "approval" if entry.required_approval else "auto"
        lines.append(f"  - {entry.capability_id} ({entry.risk_level}, {approval}): {entry.name}")
        if entry.provider_actions:
            actions = ", ".join(
                f"{binding.provider}.{binding.action_key}"
                for binding in entry.provider_actions[:8]
            )
            lines.append(f"    actions: {actions}")
        if entry.platform_tools:
            lines.append(f"    platform_tools: {', '.join(entry.platform_tools[:8])}")
        if entry.skills:
            skill_names = ", ".join(str(skill.get("name") or skill.get("slug")) for skill in entry.skills[:8])
            lines.append(f"    skills: {skill_names}")
    return "\n".join(lines)


def _unique_clean_strings(values: Iterable[Any] | None) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values or ():
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def _iter_cached_tool_specs(tools_cached: Any) -> Iterable[Any]:
    if isinstance(tools_cached, dict):
        for key in ("tools", "items", "data"):
            nested = tools_cached.get(key)
            if isinstance(nested, list):
                yield from nested
                return
        for key, value in tools_cached.items():
            if isinstance(value, dict):
                spec = dict(value)
                spec.setdefault("name", key)
                yield spec
            elif isinstance(value, str):
                yield {"name": key, "description": value}
        return
    if isinstance(tools_cached, list):
        yield from tools_cached
        return
    if isinstance(tools_cached, tuple):
        yield from tools_cached


def _normalize_cached_tool_spec(raw_tool: Any) -> dict[str, Any]:
    if isinstance(raw_tool, str):
        return {"action_key": raw_tool.strip()}
    if not isinstance(raw_tool, dict):
        return {}

    fn = raw_tool.get("function") if isinstance(raw_tool.get("function"), dict) else {}
    action_key = (
        fn.get("name")
        or raw_tool.get("name")
        or raw_tool.get("action_key")
        or raw_tool.get("tool")
    )
    action = str(action_key or "").strip()
    if not action:
        return {}

    input_schema = _first_schema(
        fn.get("parameters"),
        raw_tool.get("parameters"),
        raw_tool.get("inputSchema"),
        raw_tool.get("input_schema"),
        raw_tool.get("schema"),
    )
    output_schema = _first_schema(
        raw_tool.get("outputSchema"),
        raw_tool.get("output_schema"),
        raw_tool.get("resultSchema"),
        raw_tool.get("result_schema"),
        raw_tool.get("returns"),
    )
    return {
        "action_key": action,
        "description": _clean_description(
            fn.get("description") or raw_tool.get("description")
        ),
        "input_schema": input_schema,
        "output_schema": output_schema,
        "effect": str(raw_tool.get("effect") or "").strip().lower() or None,
        "account_ids": [
            str(account_id).strip()
            for account_id in raw_tool.get("account_ids", ())
            if str(account_id or "").strip()
        ],
        "account_options": [
            dict(option)
            for option in raw_tool.get("account_options", ())
            if isinstance(option, dict)
        ],
        "account_input_schemas": _schema_mapping(
            raw_tool.get("account_input_schemas")
        ),
        "account_output_schemas": _schema_mapping(
            raw_tool.get("account_output_schemas")
        ),
        "requires_explicit_account": (
            raw_tool.get("requires_explicit_account") is True
        ),
        "supports_all_accounts": (
            raw_tool.get("supports_all_accounts")
            if isinstance(raw_tool.get("supports_all_accounts"), bool)
            else None
        ),
    }


def _first_schema(*values: Any) -> dict[str, Any] | None:
    for value in values:
        schema = _schema_if_dict(value)
        if schema is not None:
            return schema
    return None


def _schema_if_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _schema_mapping(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(account_id): dict(schema)
        for account_id, schema in value.items()
        if str(account_id or "").strip() and isinstance(schema, Mapping)
    }


def _schema_parameter_names(schema: dict[str, Any] | None) -> list[str]:
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if isinstance(properties, dict):
        return [str(name) for name in properties.keys()]
    return []


def _clean_description(value: Any) -> str | None:
    description = str(value or "").strip()
    return description[:240] if description else None


def _normalized_skill_dicts(values: Iterable[dict[str, Any]] | None) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    for value in values or ():
        if not isinstance(value, dict):
            continue
        name = str(value.get("name") or value.get("slug") or "").strip()
        if not name:
            continue
        skill = {
            "name": name,
            "slug": str(value.get("slug") or name).strip(),
        }
        description = str(value.get("description") or "").strip()
        if description:
            skill["description"] = description[:180]
        out.append(skill)
    return tuple(out)
