"""Workspace-local integration exclusions, independent of shared accounts/Agents."""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.workspace import Workspace
from packages.core.services.provider_keys import canonical_provider_key


def disabled_workspace_providers(operating_model: object) -> frozenset[str]:
    scope = operating_model.get("integration_scope") if isinstance(operating_model, Mapping) else None
    disabled = scope.get("disabled_providers") if isinstance(scope, Mapping) else None
    return frozenset(
        key for provider in (disabled if isinstance(disabled, list) else [])
        if isinstance(provider, str) and (key := canonical_provider_key(provider))
    )


def integration_provider_for_tool(name: object) -> str:
    parts = str(name or "").split("__", 2)
    return canonical_provider_key(parts[1]) if len(parts) == 3 and parts[0] == "mcp" else ""


def integration_provider_for_requirement(requirement: Mapping) -> str:
    """Only integration/MCP requirements; channel and session policy is separate."""
    kind = requirement.get("kind")
    payload = requirement.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    if kind == "integration_provider":
        return canonical_provider_key(requirement.get("provider"))
    if kind == "missing_integration":
        return canonical_provider_key(payload.get("provider"))
    if kind in {"mcp_server", "mcp_configuration"}:
        return canonical_provider_key(payload.get("server_slug"))
    return ""


async def load_disabled_workspace_providers(
    db: AsyncSession, *, workspace_id: str, entity_id: str,
) -> frozenset[str]:
    row = (await db.execute(select(Workspace.operating_model).where(
        Workspace.id == workspace_id,
        Workspace.entity_id == entity_id,
        Workspace.deleted_at.is_(None),
    ))).one_or_none()
    if row is None:
        raise ValueError("Workspace integration policy is unavailable in this scope.")
    return disabled_workspace_providers(row[0])
