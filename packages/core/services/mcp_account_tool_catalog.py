"""Account-scoped, credential-free MCP tool catalogs."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Iterable, Mapping

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.mcp import MCPAccountToolCatalog
from packages.core.services.integration_account_service import (
    IntegrationAccountKind,
    RuntimeIntegrationAccount,
    RuntimeIntegrationRegistry,
)
from packages.core.services.official_remote_mcp import (
    MCPActionEffect,
    OfficialRemoteMCPFactory,
)


class MCPToolCatalogSource(StrEnum):
    BUILTIN = "builtin"
    CACHE = "cache"
    ACCOUNT_DISCOVERY = "account_discovery"


MCP_ACCOUNT_TOOL_CATALOG_MAX_AGE = timedelta(hours=24)


def _iter_tools(cache: Any) -> Iterable[Any]:
    if isinstance(cache, Mapping):
        for key in ("tools", "items", "data"):
            if isinstance(cache.get(key), list):
                yield from cache[key]
                return
    elif isinstance(cache, (list, tuple)):
        yield from cache


class MCPAccountToolCatalogFactory:
    _EFFECT_RANK = {
        MCPActionEffect.READ: 0,
        MCPActionEffect.WRITE: 1,
        MCPActionEffect.DESTRUCTIVE: 2,
    }

    @staticmethod
    def input_schema(raw: Mapping[str, Any]) -> dict[str, Any]:
        """Return one tool's credential-free input contract."""

        for key in ("inputSchema", "input_schema", "parameters", "schema"):
            value = raw.get(key)
            if isinstance(value, Mapping):
                return deepcopy(dict(value))
        return {"type": "object", "properties": {}}

    @staticmethod
    def output_schema(raw: Mapping[str, Any]) -> dict[str, Any]:
        """Return one tool's credential-free output contract.

        An empty schema is a valid JSON Schema meaning "any output". Keeping
        that value in an account map also prevents a missing vendor contract
        from accidentally falling back to another account's stricter schema.
        """

        for key in (
            "outputSchema",
            "output_schema",
            "resultSchema",
            "result_schema",
            "returns",
        ):
            value = raw.get(key)
            if isinstance(value, Mapping):
                return deepcopy(dict(value))
        return {}

    @staticmethod
    def contract_signature(raw: Mapping[str, Any]) -> str:
        """Return the account-independent callable contract for one MCP tool."""

        annotations = raw.get("annotations")
        safety_annotations = {
            key: annotations[key]
            for key in ("destructiveHint", "readOnlyHint")
            if isinstance(annotations, Mapping) and key in annotations
        }
        return json.dumps(
            {
                "input": MCPAccountToolCatalogFactory.input_schema(raw),
                "output": MCPAccountToolCatalogFactory.output_schema(raw),
                "effect": raw.get("effect"),
                "safety_annotations": safety_annotations,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def runtime_input_schema(
        account_input_schemas: Mapping[str, Mapping[str, Any]],
        *,
        account_argument: str,
    ) -> dict[str, Any]:
        """Build one exact-account JSON Schema from incompatible contracts."""

        schemas = [
            (str(account_id), deepcopy(dict(schema)))
            for account_id, schema in account_input_schemas.items()
            if str(account_id or "").strip() and isinstance(schema, Mapping)
        ]
        if not schemas:
            return {"type": "object", "properties": {}}
        if len(schemas) == 1:
            return schemas[0][1]

        combined_properties: dict[str, Any] = {}
        branches: list[dict[str, Any]] = []
        for account_id, raw_schema in schemas:
            branch = deepcopy(raw_schema)
            branch["type"] = "object"
            properties = branch.get("properties")
            if not isinstance(properties, dict):
                properties = {}
                branch["properties"] = properties
            for name, definition in properties.items():
                existing = combined_properties.get(name)
                if existing is None:
                    combined_properties[name] = deepcopy(definition)
                elif existing != definition:
                    combined_properties[name] = {}
            properties[account_argument] = {
                "type": "string",
                "const": account_id,
            }
            required = [
                str(name)
                for name in branch.get("required", [])
                if str(name or "").strip()
            ]
            if account_argument not in required:
                required.append(account_argument)
            branch["required"] = required
            branches.append(branch)

        # The account selector is a Manor-owned control argument. Never inherit
        # a vendor definition for it into the shared top-level contract.
        combined_properties.pop(account_argument, None)

        return {
            "type": "object",
            "properties": combined_properties,
            "required": [account_argument],
            "oneOf": branches,
        }

    @staticmethod
    def conservative_effect(
        raw_tools: Iterable[Mapping[str, Any]],
    ) -> MCPActionEffect | None:
        """Return the strongest declared effect across account variants."""

        resolved: MCPActionEffect | None = None
        for raw in raw_tools:
            annotations = raw.get("annotations")
            if (
                isinstance(annotations, Mapping)
                and annotations.get("destructiveHint") is True
            ):
                candidate = MCPActionEffect.DESTRUCTIVE
            elif raw.get("effect") is None:
                continue
            else:
                try:
                    candidate = MCPActionEffect(str(raw.get("effect")))
                except ValueError:
                    candidate = MCPActionEffect.WRITE
            if (
                resolved is None
                or MCPAccountToolCatalogFactory._EFFECT_RANK[candidate]
                > MCPAccountToolCatalogFactory._EFFECT_RANK[resolved]
            ):
                resolved = candidate
        return resolved

    @staticmethod
    def is_usable(
        row: MCPAccountToolCatalog,
        *,
        endpoint: str,
        now: datetime,
        max_age: timedelta = MCP_ACCOUNT_TOOL_CATALOG_MAX_AGE,
    ) -> bool:
        cached_at = row.tools_cached_at
        if cached_at is None or str(row.endpoint or "").strip() != endpoint:
            return False
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=UTC)
        return cached_at >= now - max_age

    @staticmethod
    def values(
        *,
        provider: str,
        account: RuntimeIntegrationAccount,
        endpoint: str,
        raw_tools: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "id": generate_ulid(),
            "provider": provider,
            "oauth_account_id": account.id if account.kind is IntegrationAccountKind.OAUTH_ACCOUNT else None,
            "integration_id": account.id if account.kind is IntegrationAccountKind.INTEGRATION else None,
            "endpoint": endpoint,
            "tools_cached": OfficialRemoteMCPFactory.tools_cache(provider, discovered_tools=raw_tools),
            "tools_cached_at": datetime.now(UTC),
        }

    @staticmethod
    def merge(
        rows: Iterable[MCPAccountToolCatalog],
        *,
        account_order: Iterable[str] = (),
        account_options: Mapping[str, Mapping[str, Any]] | None = None,
        requires_explicit_account: bool = False,
    ) -> dict[str, Any]:
        priority = {account_id: index for index, account_id in enumerate(account_order)}
        ordered_rows = sorted(
            rows,
            key=lambda row: (
                priority.get(str(row.oauth_account_id or row.integration_id or ""), len(priority)),
                str(row.id),
            ),
        )
        tools_by_name: dict[str, dict[str, Any]] = {}
        contract_by_name: dict[str, str] = {}
        account_ids_by_name: dict[str, list[str]] = {}
        account_input_schemas_by_name: dict[str, dict[str, dict[str, Any]]] = {}
        account_output_schemas_by_name: dict[str, dict[str, dict[str, Any]]] = {}
        raw_tools_by_name: dict[str, list[Mapping[str, Any]]] = {}
        conflicting_names: set[str] = set()
        for row in ordered_rows:
            account_id = str(row.oauth_account_id or row.integration_id or "")
            for raw in _iter_tools(row.tools_cached):
                if not isinstance(raw, Mapping):
                    continue
                name = str(raw.get("name") or "").strip()
                if not name:
                    continue
                contract = MCPAccountToolCatalogFactory.contract_signature(raw)
                if name in contract_by_name and contract_by_name[name] != contract:
                    conflicting_names.add(name)
                tools_by_name.setdefault(name, dict(raw))
                raw_tools_by_name.setdefault(name, []).append(raw)
                contract_by_name.setdefault(name, contract)
                ids = account_ids_by_name.setdefault(name, [])
                if account_id and account_id not in ids:
                    ids.append(account_id)
                    account_input_schemas_by_name.setdefault(name, {})[account_id] = (
                        MCPAccountToolCatalogFactory.input_schema(raw)
                    )
                    account_output_schemas_by_name.setdefault(name, {})[account_id] = (
                        MCPAccountToolCatalogFactory.output_schema(raw)
                    )
        tools = []
        for name, raw in tools_by_name.items():
            tool = dict(raw)
            tool["account_ids"] = account_ids_by_name[name]
            if account_options:
                tool["account_options"] = [
                    dict(account_options[account_id])
                    for account_id in account_ids_by_name[name]
                    if account_id in account_options
                ]
            if name in conflicting_names:
                tool["account_input_schemas"] = account_input_schemas_by_name[name]
                tool["account_output_schemas"] = account_output_schemas_by_name[name]
                tool["supports_all_accounts"] = False
                effect = MCPAccountToolCatalogFactory.conservative_effect(
                    raw_tools_by_name[name]
                )
                if effect is not None:
                    tool["effect"] = effect.value
                if effect is MCPActionEffect.DESTRUCTIVE:
                    annotations = dict(tool.get("annotations") or {})
                    annotations["destructiveHint"] = True
                    tool["annotations"] = annotations
            if requires_explicit_account or name in conflicting_names:
                tool["requires_explicit_account"] = True
            tools.append(tool)
        return {"source": MCPToolCatalogSource.ACCOUNT_DISCOVERY.value, "tools": tools}


async def persist_mcp_account_tool_catalog(
    db: AsyncSession,
    *,
    provider: str,
    account: RuntimeIntegrationAccount,
    endpoint: str,
    raw_tools: Iterable[Mapping[str, Any]],
) -> None:
    values = MCPAccountToolCatalogFactory.values(
        provider=provider, account=account, endpoint=endpoint, raw_tools=raw_tools
    )
    constraint = (
        "uq_mcp_account_tool_catalogs_oauth"
        if account.kind is IntegrationAccountKind.OAUTH_ACCOUNT
        else "uq_mcp_account_tool_catalogs_integration"
    )
    statement = insert(MCPAccountToolCatalog).values(**values)
    await db.execute(statement.on_conflict_do_update(
        constraint=constraint,
        set_={
            "endpoint": statement.excluded.endpoint,
            "tools_cached": statement.excluded.tools_cached,
            "tools_cached_at": statement.excluded.tools_cached_at,
            "updated_at": datetime.now(UTC),
        },
    ))


async def invalidate_mcp_account_tool_catalog(
    db: AsyncSession,
    *,
    provider: str,
    account: RuntimeIntegrationAccount,
) -> None:
    """Remove a snapshot that live discovery could no longer verify."""

    account_column = (
        MCPAccountToolCatalog.oauth_account_id
        if account.kind is IntegrationAccountKind.OAUTH_ACCOUNT
        else MCPAccountToolCatalog.integration_id
    )
    await db.execute(delete(MCPAccountToolCatalog).where(
        MCPAccountToolCatalog.provider == provider,
        account_column == account.id,
    ))


async def actor_mcp_tool_cache(
    db: AsyncSession,
    *,
    provider: str,
    registry: RuntimeIntegrationRegistry,
    endpoint: str | None,
    now: datetime | None = None,
    max_age: timedelta = MCP_ACCOUNT_TOOL_CATALOG_MAX_AGE,
) -> dict[str, Any] | None:
    resolved_endpoint = str(endpoint or "").strip()
    if not resolved_endpoint:
        return None
    accounts = registry.accounts_for(provider)
    oauth_ids = [a.id for a in accounts if a.kind is IntegrationAccountKind.OAUTH_ACCOUNT]
    integration_ids = [a.id for a in accounts if a.kind is IntegrationAccountKind.INTEGRATION]
    clauses = []
    if oauth_ids:
        clauses.append(MCPAccountToolCatalog.oauth_account_id.in_(oauth_ids))
    if integration_ids:
        clauses.append(MCPAccountToolCatalog.integration_id.in_(integration_ids))
    if not clauses:
        return None
    resolved_now = now or datetime.now(UTC)
    cutoff = resolved_now - max_age
    rows = list((await db.execute(select(MCPAccountToolCatalog).where(
        MCPAccountToolCatalog.provider == provider,
        MCPAccountToolCatalog.endpoint == resolved_endpoint,
        MCPAccountToolCatalog.tools_cached_at.is_not(None),
        MCPAccountToolCatalog.tools_cached_at >= cutoff,
        or_(*clauses),
    ))).scalars().all())
    rows = [
        row
        for row in rows
        if MCPAccountToolCatalogFactory.is_usable(
            row,
            endpoint=resolved_endpoint,
            now=resolved_now,
            max_age=max_age,
        )
    ]
    binding = registry.integration(provider)
    cached_account_ids = {
        str(row.oauth_account_id or row.integration_id or "")
        for row in rows
    }
    missing_account_catalog = any(
        account.id not in cached_account_ids
        for account in accounts
    )
    return (
        MCPAccountToolCatalogFactory.merge(
            rows,
            account_order=(a.id for a in accounts),
            account_options={
                account.id: account.public_option()
                for account in accounts
            },
            requires_explicit_account=bool(
                (binding and binding.requires_explicit_account)
                or missing_account_catalog
            ),
        )
        if rows else None
    )


async def actor_provider_mcp_tool_caches(
    db: AsyncSession,
    *,
    provider_endpoints: Mapping[str, str],
    user_id: str,
    entity_id: str,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Load fresh catalogs only for accounts callable by the active actor."""

    endpoints = {
        str(provider).strip(): str(endpoint).strip()
        for provider, endpoint in provider_endpoints.items()
        if str(provider or "").strip() and str(endpoint or "").strip()
    }
    if not endpoints or not str(user_id or "").strip() or not str(entity_id or "").strip():
        return {}
    from packages.core.services.integration_account_service import (
        load_runtime_integration_registry,
    )

    registry = await load_runtime_integration_registry(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider_keys=endpoints,
    )
    caches: dict[str, dict[str, Any]] = {}
    for provider, endpoint in endpoints.items():
        cache = await actor_mcp_tool_cache(
            db,
            provider=provider,
            registry=registry,
            endpoint=endpoint,
            now=now,
        )
        if cache:
            caches[provider] = cache
    return caches
