from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.setup_preflight import BlueprintSetupPreflightFactory
from packages.core.constants.blueprints import BlueprintInstallRequirementKind
from packages.core.models.base import generate_ulid
from packages.core.models.mcp import MCPServer
from packages.core.services.provider_keys import canonical_provider_key


@pytest.mark.parametrize(
    ("auth_type", "status", "expected_ready"),
    [
        ("none", "active", True),
        ("no_auth", "active", True),
        ("none", "inactive", False),
        ("api_key", "active", False),
        (None, None, False),
    ],
)
async def test_preflight_preserves_registered_provider_spelling(
    db_session: AsyncSession,
    auth_type: str | None,
    status: str | None,
    expected_ready: bool,
):
    server_slug = f"blueprint-mcp-{generate_ulid()}"
    if auth_type is not None:
        db_session.add(MCPServer(
            server_key=server_slug,
            name="Blueprint MCP",
            transport="builtin",
            auth_type=auth_type,
            status=status,
        ))
        await db_session.flush()

    result = await BlueprintSetupPreflightFactory.from_contract(
        db_session,
        contract={"requires": {"mcp_servers": [{"slug": server_slug}]}},
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
    )

    assert result.ready is expected_ready
    assert len(result.requirements) == 1
    requirement = result.requirements[0]
    assert requirement.kind == BlueprintInstallRequirementKind.INTEGRATION
    assert requirement.provider == canonical_provider_key(server_slug)
    assert requirement.required is True
    assert requirement.ready is expected_ready
    assert result.blocking_requirements == (() if expected_ready else (requirement,))


async def test_preflight_required_dependency_blocks_live_but_not_simulation(
    db_session: AsyncSession,
):
    server_slug = f"blueprint-live-only-{generate_ulid()}"
    result = await BlueprintSetupPreflightFactory.from_contract(
        db_session,
        contract={
            "requires": {
                "mcp_servers": [{"slug": server_slug, "required": True}],
            },
        },
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
    )

    [requirement] = result.requirements
    assert requirement.required is True
    assert requirement.ready is False
    assert result.ready_for_mode("simulate") is True
    assert result.blocking_requirements_for_mode("simulate") == ()
    assert result.ready_for_mode("live") is False
    assert result.blocking_requirements_for_mode("live") == (requirement,)


async def test_preflight_required_runtime_dependency_can_defer_live_install_gate(
    db_session: AsyncSession,
):
    server_slug = f"blueprint-runtime-required-{generate_ulid()}"
    result = await BlueprintSetupPreflightFactory.from_contract(
        db_session,
        contract={
            "requires": {
                "mcp_servers": [{
                    "slug": server_slug,
                    "required": True,
                    "install_blocking": False,
                }],
            },
        },
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
    )

    [requirement] = result.requirements
    assert requirement.required is True
    assert requirement.install_blocking is False
    assert requirement.ready is False
    assert result.ready_for_mode("live") is True
    assert result.blocking_requirements_for_mode("live") == ()


async def test_preflight_checks_all_spellings_before_merging_requirements(
    db_session: AsyncSession,
):
    server_slug = f"blueprint-mcp-{generate_ulid()}"
    canonical_slug = canonical_provider_key(server_slug)
    db_session.add(MCPServer(
        server_key=server_slug,
        name="Blueprint MCP",
        transport="builtin",
        auth_type="none",
        status="active",
    ))
    await db_session.flush()

    result = await BlueprintSetupPreflightFactory.from_contract(
        db_session,
        contract={"requires": {"mcp_servers": [
            {
                "slug": canonical_slug,
                "required": False,
                "config_fields_to_set": ["project"],
            },
            {
                "slug": server_slug,
                "required": True,
                "config_fields_to_set": ["project", "region"],
            },
        ]}},
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
    )

    assert result.ready is True
    assert len(result.requirements) == 1
    requirement = result.requirements[0]
    assert requirement.provider == canonical_slug
    assert requirement.required is True
    assert requirement.config_fields_to_set == ("project", "region")
