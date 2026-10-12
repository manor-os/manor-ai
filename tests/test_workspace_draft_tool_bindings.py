"""Draft readiness must validate the bindings provisioning will materialize."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from packages.core.ai.runtime.capability_bindings import (
    WORKSPACE_CUSTOM_AGENT_BASE_TOOL_NAMES,
)
from packages.core.ai.runtime.task_requirements import (
    STRATEGIST_TASK_CAPABILITY_IDS,
)
from packages.core.ai.runtime.tool_registry import runtime_registered_tool_names
from packages.core.ai.tools import workspace_arch_tools
from packages.core.services.agent_provisioning_service import spec_from_create_agent_draft
from packages.core.services.agent_capability_catalog import (
    AgentCapabilityCatalog,
    AgentCapabilityCandidate,
    AgentCapabilityKind,
    AgentCapabilityPlanStatus,
)


def test_propose_service_schema_requires_canonical_capability_contract():
    parameters = workspace_arch_tools.PROPOSE_SERVICE_SCHEMA["function"]["parameters"]

    assert "required_capabilities" in parameters["required"]
    capability_schema = parameters["properties"]["required_capabilities"]
    assert capability_schema["type"] == "array"
    assert capability_schema["uniqueItems"] is True
    assert capability_schema["items"]["enum"] == list(STRATEGIST_TASK_CAPABILITY_IDS)


@pytest.mark.asyncio
async def test_propose_service_persists_requirements_without_treating_them_as_bindings(
    monkeypatch,
):
    draft = SimpleNamespace(fields={"agent_mappings": []})
    persist = AsyncMock()
    monkeypatch.setattr(workspace_arch_tools, "_load_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(workspace_arch_tools, "_persist", persist)

    invalid = json.loads(await workspace_arch_tools._propose_service(
        SimpleNamespace(), entity_id="entity", user_id="user", draft_id="draft",
        service_key="research", name="Research", description="Research verified sources.",
        autonomy_level="supervised", owner_role="researcher",
        required_capabilities=["made.up"],
    ))
    assert invalid["ok"] is False
    persist.assert_not_awaited()

    result = json.loads(await workspace_arch_tools._propose_service(
        SimpleNamespace(), entity_id="entity", user_id="user", draft_id="draft",
        service_key="research", name="Research", description="Research verified sources.",
        autonomy_level="supervised", owner_role="researcher",
        required_capabilities=["web.safe_search"],
    ))

    assert result["ok"] is True
    assert draft.fields["services"][0]["required_capabilities"] == ["web.safe_search"]
    assert "tool_bindings" not in draft.fields["services"][0]
    persist.assert_awaited_once()


def test_custom_agent_capability_projection_uses_tools_skills_and_exact_mcp_actions():
    mcp_server = SimpleNamespace(
        id="instagram-server",
        server_key="instagram",
        tools_cached=[{"name": "create_post", "description": "Create a social post"}],
    )

    capabilities = workspace_arch_tools._custom_agent_runtime_capability_ids({
        "tool_bindings": ["web_search"],
        "skill_bindings": ["research-method"],
        "mcp_bindings": ["instagram"],
        "mcp_allowed_tools": {"instagram": ["create_post"]},
    }, mcp_rows=[mcp_server])

    assert {"web.safe_search", "skill.invoke", "external.social"} <= capabilities


def test_workspace_creation_catalog_exposes_only_existing_integrations():
    ready = AgentCapabilityCandidate(
        catalog_id="mcp:slack", kind=AgentCapabilityKind.MCP_ACTION,
        ref="slack", name="Slack", metadata={"server_key": "slack"},
    )
    missing = AgentCapabilityCandidate(
        catalog_id="mcp:linkedin", kind=AgentCapabilityKind.MCP_ACTION,
        ref="linkedin", name="LinkedIn", readiness=AgentCapabilityPlanStatus.SETUP_REQUIRED,
        metadata={"server_key": "linkedin"},
    )
    catalog = AgentCapabilityCatalog(
        candidates=(ready, missing),
        integrations=(
            {"mcp_server_key": "slack"}, {"mcp_server_key": "linkedin"},
        ),
    )

    filtered = workspace_arch_tools._workspace_existing_integration_catalog(catalog)

    assert [candidate.catalog_id for candidate in filtered.candidates] == ["mcp:slack"]
    assert filtered.integrations == ({"mcp_server_key": "slack"},)
    assert [candidate.catalog_id for candidate in catalog.candidates] == ["mcp:slack", "mcp:linkedin"]


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [False, True])
async def test_workspace_creation_channel_requires_existing_integration(monkeypatch, connected):
    from packages.core.services import integration_service

    draft = SimpleNamespace(fields={
        "name": "Support", "kind": "operations",
        "operating_context": "Handle support requests", "primary_work": "Respond to users",
        "channel_config": {"primary_external_channel": {
            "channel_type": "slack", "purpose": "Receive support requests",
        }},
    })
    monkeypatch.setattr(workspace_arch_tools, "_load_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(
        integration_service, "get_integration_inventory",
        AsyncMock(return_value={"integrations": [
            {"provider": "slack", "ready": connected},
        ]}),
    )
    rows = Mock()
    rows.scalars.return_value.all.return_value = []
    db = SimpleNamespace(execute=AsyncMock(return_value=rows))

    result = json.loads(await workspace_arch_tools._lint_draft(
        db, draft_id="draft", entity_id="entity", user_id="user",
    ))

    channel_errors = [
        issue for issue in result["issues"]
        if issue["where"] == "channel_config.primary_external_channel"
        and issue["severity"] == "P0"
    ]
    assert bool(channel_errors) is not connected


def test_sandbox_agent_capability_expands_only_to_registered_tools():
    spec = spec_from_create_agent_draft({
        "business_capabilities": ["sandbox.execute"],
    })

    registered = set(runtime_registered_tool_names(include_undiscoverable=True))
    assert {"sandbox", "video_edit"} <= set(spec.tool_bindings)
    assert set(spec.tool_bindings) <= registered


@pytest.mark.asyncio
@pytest.mark.parametrize("binding, unavailable, expected_path, expected_ref", [
    ({"business_capabilities": ["sandbox.execute"]}, None, None, None),
    ({"business_capabilities": ["sandbox.execute"]}, "sandbox", "tool_bindings", "sandbox"),
    ({"tool_bindings": ["missing_tool"]}, None, "tool_bindings", "missing_tool"),
    ({"business_capabilities": ["missing.capability"]}, None, "business_capabilities", "missing.capability"),
    ({"skill_bindings": ["missing-skill"]}, None, "skill_bindings", "missing-skill"),
    ({"mcp_bindings": ["missing-server"]}, None, "mcp_bindings", "missing-server"),
    ({}, None, "bindings", None),
])
async def test_lint_checks_materialized_tools_without_erasing_invalid_bindings(
    monkeypatch, binding, unavailable, expected_path, expected_ref,
):
    from packages.core.ai.runtime import tool_registry
    from packages.core.services import integration_resolution

    draft = SimpleNamespace(fields={
        "name": "Evidence review",
        "kind": "operations",
        "operating_context": "Check sources before delivery",
        "primary_work": "Verify factual claims",
        "services": [{"service_key": "evidence_fact_check"}],
        "agent_mappings": [{
            "service_key": "evidence_fact_check",
            "strategy": "create_custom",
            "create_agent_draft": {
                "agent_name": "Evidence reviewer",
                "system_prompt": "Verify claims using original evidence.",
                **binding,
            },
        }],
    })
    monkeypatch.setattr(workspace_arch_tools, "_load_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(
        integration_resolution, "supported_integration_provider_keys",
        AsyncMock(return_value=set()),
    )
    registered = {*WORKSPACE_CUSTOM_AGENT_BASE_TOOL_NAMES, "sandbox", "video_edit", "invoke_skill"}
    if unavailable:
        registered.remove(unavailable)
    monkeypatch.setattr(
        tool_registry, "runtime_registered_tool_names", lambda **_kwargs: tuple(registered),
    )
    rows = Mock()
    rows.scalars.return_value.all.return_value = []
    db = SimpleNamespace(execute=AsyncMock(return_value=rows))

    result = json.loads(await workspace_arch_tools._lint_draft(
        db, draft_id="draft", entity_id="entity", user_id="user",
    ))

    errors = [issue for issue in result["issues"] if issue["severity"] == "P0"]
    if expected_path:
        assert result["ok_to_finalize"] is False
        assert [issue["where"] for issue in errors] == [
            f"agent_mappings.evidence_fact_check.{expected_path}",
        ]
        assert errors[0].get("binding_ref") == expected_ref
        if unavailable:
            assert unavailable in errors[0]["message"]
    else:
        assert result["ok_to_finalize"] is True
        assert errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_tools,expect_gap", [
    (["generate_file"], True),
    (["generate_file", "web_search"], False),
])
async def test_lint_requires_declared_service_capabilities_in_custom_agent_scope(
    monkeypatch, bound_tools, expect_gap,
):
    from packages.core.ai.runtime import tool_registry
    from packages.core.services import integration_resolution

    draft = SimpleNamespace(fields={
        "name": "Evidence research",
        "kind": "operations",
        "operating_context": "Research current evidence before delivery",
        "primary_work": "Find and verify current public sources",
        "services": [{
            "service_key": "research",
            "name": "Research",
            "description": "Find and verify current public sources.",
            "autonomy_level": "supervised",
            "owner_role": "researcher",
            "required_capabilities": ["web.safe_search"],
        }],
        "agent_mappings": [{
            "service_key": "research",
            "strategy": "create_custom",
            "create_agent_draft": {
                "agent_name": "Research Agent",
                "system_prompt": "Find current evidence and cite the source.",
                "tool_bindings": bound_tools,
            },
        }],
    })
    monkeypatch.setattr(workspace_arch_tools, "_load_draft", AsyncMock(return_value=draft))
    monkeypatch.setattr(
        integration_resolution, "supported_integration_provider_keys",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        tool_registry,
        "runtime_registered_tool_names",
        lambda **_kwargs: tuple({
            *WORKSPACE_CUSTOM_AGENT_BASE_TOOL_NAMES,
            "web_search",
        }),
    )
    rows = Mock()
    rows.scalars.return_value.all.return_value = []
    db = SimpleNamespace(execute=AsyncMock(return_value=rows))

    result = json.loads(await workspace_arch_tools._lint_draft(
        db, draft_id="draft", entity_id="entity", user_id="user",
    ))
    coverage_gaps = [
        issue for issue in result["issues"]
        if issue.get("missing_capabilities") == ["web.safe_search"]
    ]

    assert bool(coverage_gaps) is expect_gap
    if expect_gap:
        assert coverage_gaps[0]["severity"] == "P0"
        assert coverage_gaps[0]["where"] == "agent_mappings.research.tool_bindings"
        assert coverage_gaps[0]["binding_ref"] == "web.safe_search"
