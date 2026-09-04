from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.constants.agent_capabilities import AGENT_CAPABILITY_SELECTION_LIMIT
from packages.core.models.base import generate_ulid
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.skill import Skill
from packages.core.models.user import User
from packages.core.models.workspace import AgentToolBinding, ToolDefinition
from packages.core.models.workspace_draft import WorkspaceDraft

from packages.core.services.agent_capability_catalog import (
    AgentCapabilityCandidate,
    AgentCapabilityCatalog,
    AgentCapabilityKind,
    AgentCapabilityPlanStatus,
    AgentCapabilitySelectionError,
)


def _catalog() -> AgentCapabilityCatalog:
    return AgentCapabilityCatalog(candidates=(
        AgentCapabilityCandidate(
            catalog_id="tool:invoke_skill",
            kind=AgentCapabilityKind.TOOL,
            ref="invoke_skill",
            name="Invoke Skill",
        ),
        AgentCapabilityCandidate(
            catalog_id="tool:web_search",
            kind=AgentCapabilityKind.TOOL,
            ref="web_search",
            name="Web Search",
        ),
        AgentCapabilityCandidate(
            catalog_id="capability:web.safe_search",
            kind=AgentCapabilityKind.BUSINESS_CAPABILITY,
            ref="web.safe_search",
            name="Safe web search",
            metadata={"tool_names": ("web_search",)},
        ),
        AgentCapabilityCandidate(
            catalog_id="skill:01SKILL",
            kind=AgentCapabilityKind.SKILL,
            ref="01SKILL",
            name="Research workflow",
        ),
        AgentCapabilityCandidate(
            catalog_id="mcp:gmail:list_messages",
            kind=AgentCapabilityKind.MCP_ACTION,
            ref="gmail",
            name="Gmail · List messages",
            readiness=AgentCapabilityPlanStatus.SETUP_REQUIRED,
            metadata={
                "server_key": "gmail",
                "action": "list_messages",
                "setup_required_reason": "Connect Gmail",
                "setup_kind": "oauth",
            },
        ),
        AgentCapabilityCandidate(
            catalog_id="mcp:gmail:send_message",
            kind=AgentCapabilityKind.MCP_ACTION,
            ref="gmail",
            name="Gmail · Send message",
            readiness=AgentCapabilityPlanStatus.SETUP_REQUIRED,
            metadata={
                "server_key": "gmail",
                "action": "send_message",
                "setup_required_reason": "Connect Gmail",
                "setup_kind": "oauth",
            },
        ),
    ))


def _fifty_selection_catalog() -> tuple[AgentCapabilityCatalog, list[str]]:
    candidates = [
        AgentCapabilityCandidate(
            catalog_id="tool:invoke_skill",
            kind=AgentCapabilityKind.TOOL,
            ref="invoke_skill",
            name="Invoke Skill",
        )
    ]
    selected: list[str] = []
    for index in range(15):
        catalog_id = f"tool:bulk_tool_{index}"
        selected.append(catalog_id)
        candidates.append(AgentCapabilityCandidate(
            catalog_id=catalog_id,
            kind=AgentCapabilityKind.TOOL,
            ref=f"bulk_tool_{index}",
            name=f"Bulk Tool {index}",
        ))
    for index in range(15):
        catalog_id = f"skill:01BULK{index:02d}"
        selected.append(catalog_id)
        candidates.append(AgentCapabilityCandidate(
            catalog_id=catalog_id,
            kind=AgentCapabilityKind.SKILL,
            ref=f"01BULK{index:02d}",
            name=f"Bulk Skill {index}",
        ))
    for index in range(20):
        action = f"action_{index}"
        catalog_id = f"mcp:bulk_email:{action}"
        selected.append(catalog_id)
        candidates.append(AgentCapabilityCandidate(
            catalog_id=catalog_id,
            kind=AgentCapabilityKind.MCP_ACTION,
            ref="bulk_email",
            name=f"Bulk Email · {action}",
            readiness=AgentCapabilityPlanStatus.SETUP_REQUIRED,
            actions=({"name": action, "effect": "read"},),
            metadata={
                "server_key": "bulk_email",
                "action": action,
                "setup_required_reason": "Connect bulk email",
                "setup_kind": "credentials",
            },
        ))
    return AgentCapabilityCatalog(candidates=tuple(candidates)), selected


def _large_selection_catalog(
    *,
    tool_count: int,
    skill_count: int = 0,
    mcp_server_count: int = 0,
    verbose: bool = False,
) -> tuple[AgentCapabilityCatalog, list[str]]:
    candidates: list[AgentCapabilityCandidate] = []
    selected: list[str] = []
    description = (
        "Use this exact capability to carry out one distinct service operation "
        "while preserving the complete workspace objective, applying its rules, "
        "consulting its knowledge, coordinating dependent tasks, recording the "
        "result, and escalating any approval-sensitive side effect before execution."
        if verbose else ""
    )
    for index in range(tool_count):
        ref = "invoke_skill" if index == 0 else f"capacity_tool_{index:03d}"
        catalog_id = f"tool:{ref}"
        selected.append(catalog_id)
        candidates.append(AgentCapabilityCandidate(
            catalog_id=catalog_id,
            kind=AgentCapabilityKind.TOOL,
            ref=ref,
            name=(
                f"Capacity Tool {index:03d} for a separately defined service operation"
                if verbose else f"Capacity Tool {index:03d}"
            ),
            description=description,
        ))
    for index in range(skill_count):
        skill_id = f"01CAPACITYSKILL{index:03d}"
        catalog_id = f"skill:{skill_id}"
        selected.append(catalog_id)
        candidates.append(AgentCapabilityCandidate(
            catalog_id=catalog_id,
            kind=AgentCapabilityKind.SKILL,
            ref=skill_id,
            name=(
                f"Capacity Skill {index:03d} for a separately defined service operation"
                if verbose else f"Capacity Skill {index:03d}"
            ),
            description=description,
        ))
    for index in range(mcp_server_count):
        server_key = f"capacity_mcp_{index:03d}"
        catalog_id = f"mcp:{server_key}"
        selected.append(catalog_id)
        candidates.append(AgentCapabilityCandidate(
            catalog_id=catalog_id,
            kind=AgentCapabilityKind.MCP_ACTION,
            ref=server_key,
            name=(
                f"Capacity MCP {index:03d} for a separately defined service operation"
                if verbose else f"Capacity MCP {index:03d}"
            ),
            description=description,
            metadata={"server_key": server_key, "all_actions": True},
        ))
    return AgentCapabilityCatalog(candidates=tuple(candidates)), selected


def test_capability_factory_resolves_exact_ids_and_mcp_action_allowlist() -> None:
    plan = _catalog().resolve([
        "capability:web.safe_search",
        "skill:01SKILL",
        "mcp:gmail:list_messages",
        "mcp:gmail:send_message",
        "mcp:gmail:list_messages",
    ])

    assert plan.selected_catalog_ids == (
        "capability:web.safe_search",
        "skill:01SKILL",
        "mcp:gmail:list_messages",
        "mcp:gmail:send_message",
    )
    assert plan.tool_names == ("web_search", "invoke_skill")
    assert plan.business_capability_ids == ("web.safe_search",)
    assert plan.skill_ids == ("01SKILL",)
    assert plan.mcp_server_keys == ("gmail",)
    assert plan.mcp_allowed_tools == {
        "gmail": ("list_messages", "send_message"),
    }
    assert plan.status is AgentCapabilityPlanStatus.SETUP_REQUIRED
    assert plan.setup_required == ({
        "catalog_id": "mcp:gmail:send_message",
        "provider": "gmail",
        "reason": "Connect Gmail",
        "setup_kind": "oauth",
    },)


def test_capability_factory_rejects_unknown_ids_and_skill_slugs() -> None:
    catalog = _catalog()

    with pytest.raises(AgentCapabilitySelectionError, match="unknown or inaccessible"):
        catalog.resolve(["tool:not_real"])

    with pytest.raises(AgentCapabilitySelectionError, match="skill:research-workflow"):
        catalog.resolve_exact_refs(skill_ids=["research-workflow"])


def test_capability_factory_accepts_two_hundred_ids_and_rejects_more() -> None:
    catalog, selected = _large_selection_catalog(tool_count=201)

    plan = catalog.resolve(selected[:AGENT_CAPABILITY_SELECTION_LIMIT])

    assert len(plan.selected_catalog_ids) == AGENT_CAPABILITY_SELECTION_LIMIT
    with pytest.raises(AgentCapabilitySelectionError, match="at most 200"):
        catalog.resolve(selected)


def test_agent_capability_limit_is_shared_by_provisioning_tool_schemas() -> None:
    from packages.core.ai.tools.agent_provisioning_tools import PROVISION_AGENT_SCHEMA
    from packages.core.ai.tools.workspace_arch_tools import REQUEST_CUSTOM_AGENT_SCHEMA
    from packages.core.services import agent_generator

    assert (
        PROVISION_AGENT_SCHEMA["function"]["parameters"]["properties"]
        ["capability_ids"]["maxItems"]
        == AGENT_CAPABILITY_SELECTION_LIMIT
    )
    assert "Return at most 200 ids." in agent_generator.AGENT_CAPABILITY_SCAN_SYSTEM_PROMPT
    assert "Return at most 200 ids." in agent_generator.AGENT_CAPABILITY_MATCH_SYSTEM_PROMPT
    request_properties = REQUEST_CUSTOM_AGENT_SCHEMA["function"]["parameters"][
        "properties"
    ]
    assert {
        "capability_ids",
        "tool_bindings",
        "business_capabilities",
        "skill_bindings",
        "mcp_bindings",
    }.isdisjoint(request_properties)


def test_workspace_architect_tools_are_serialized_within_one_model_round() -> None:
    from packages.core.ai.agentic_loop import _requires_serial_tool_execution

    assert _requires_serial_tool_execution("ws_propose_service") is True
    assert _requires_serial_tool_execution("ws_search_capabilities") is True
    assert _requires_serial_tool_execution("ws_request_custom_agent") is True
    assert _requires_serial_tool_execution("web_search") is False


def test_capability_factory_resolves_fifty_mixed_exact_ids() -> None:
    catalog, selected = _fifty_selection_catalog()

    plan = catalog.resolve(selected)

    assert len(plan.selected_catalog_ids) == 50
    assert len(plan.tool_names) == 16  # 15 direct tools plus invoke_skill
    assert len(plan.skill_ids) == 15
    assert plan.mcp_server_keys == ("bulk_email",)
    assert len(plan.mcp_allowed_tools["bulk_email"] or ()) == 20
    assert plan.status is AgentCapabilityPlanStatus.SETUP_REQUIRED
    mcp_payload = next(
        item for item in catalog.prompt_payload()
        if item["id"] == "mcp:bulk_email:action_0"
    )
    assert mcp_payload["effect"] == "read"
    assert "actions" not in mcp_payload




@pytest.mark.asyncio
async def test_agent_ai_uses_a_separate_semantic_capability_match(monkeypatch) -> None:
    from packages.core.services import agent_generator

    calls: list[list[dict[str, str]]] = []

    async def fake_completion(messages, **_kwargs):
        calls.append(messages)
        return SimpleNamespace(
            content='{"capability_ids":["capability:web.safe_search"]}'
        )

    monkeypatch.setattr(
        agent_generator,
        "runtime_execute_text_completion",
        fake_completion,
    )
    plan = await agent_generator._match_agent_capabilities(
        prompt=(
            "Research public sources and cite evidence. Stay read-only and do "
            "not send messages or modify external systems."
        ),
        spec={
            "name": "Research Agent",
            "description": "Compare evidence from public sources.",
            "category": "Research",
        },
        entity_id="01ENTITY",
        capability_catalog=_catalog(),
    )

    assert plan.selected_catalog_ids == ("capability:web.safe_search",)
    assert len(calls) == 2
    assert "semantic classification task, not keyword matching" in calls[0][0]["content"]
    assert "mcp:gmail:send_message" in calls[0][1]["content"]
    assert "mcp:gmail:send_message" not in calls[1][1]["content"]
    assert "same role as alternatives" in calls[1][0]["content"]


@pytest.mark.asyncio
async def test_agent_ai_semantic_match_supports_fifty_exact_ids(monkeypatch) -> None:
    from packages.core.services import agent_generator

    catalog, selected = _fifty_selection_catalog()
    completion_kwargs: list[dict] = []

    async def fake_completion(_messages, **kwargs):
        completion_kwargs.append(kwargs)
        return SimpleNamespace(content=json.dumps({"capability_ids": selected}))

    import json

    monkeypatch.setattr(
        agent_generator,
        "runtime_execute_text_completion",
        fake_completion,
    )
    plan = await agent_generator._match_agent_capabilities(
        prompt="Operate a broad workflow that requires all fifty declared capabilities.",
        spec={"name": "Large Operator", "description": "A broad operator."},
        entity_id="01ENTITY",
        capability_catalog=catalog,
    )

    assert len(plan.selected_catalog_ids) == 50
    assert len(completion_kwargs) == 2
    assert all(item["max_tokens"] == 8000 for item in completion_kwargs)


@pytest.mark.asyncio
async def test_agent_ai_semantic_match_supports_one_hundred_forty_exact_ids(
    monkeypatch,
) -> None:
    from packages.core.services import agent_generator

    catalog, selected = _large_selection_catalog(
        tool_count=120,
        skill_count=10,
        mcp_server_count=10,
        verbose=True,
    )
    payload_chars = len(agent_generator._compact_json(catalog.prompt_payload()))
    calls: list[list[dict[str, str]]] = []

    async def fake_completion(messages, **_kwargs):
        calls.append(messages)
        user_content = messages[1]["content"]
        if "Semantic scan chunk" in user_content:
            returned = [
                catalog_id for catalog_id in selected
                if f'"{catalog_id}"' in user_content
            ]
        else:
            returned = selected
        return SimpleNamespace(content=json.dumps({"capability_ids": returned}))

    import json

    monkeypatch.setattr(
        agent_generator,
        "runtime_execute_text_completion",
        fake_completion,
    )
    plan = await agent_generator._match_agent_capabilities(
        prompt=(
            "Operate a broad service that explicitly requires all 120 tools, "
            "10 MCP servers, and 10 skills in this catalog."
        ),
        spec={"name": "Capacity Operator", "description": "A broad operator."},
        entity_id="01ENTITY",
        capability_catalog=catalog,
    )

    assert 48_000 < payload_chars < 96_000
    assert len(calls) == 3  # two scans plus one final least-privilege review
    assert len(plan.selected_catalog_ids) == 140
    assert len(plan.tool_names) == 120
    assert len(plan.skill_ids) == 10
    assert len(plan.mcp_server_keys) == 10


@pytest.mark.asyncio
async def test_agent_ai_semantic_match_rejects_an_invented_id(monkeypatch) -> None:
    from packages.core.services import agent_generator

    async def fake_completion(_messages, **_kwargs):
        return SimpleNamespace(content='{"capability_ids":["tool:web_magic"]}')

    monkeypatch.setattr(
        agent_generator,
        "runtime_execute_text_completion",
        fake_completion,
    )

    with pytest.raises(AgentCapabilitySelectionError, match="unknown or inaccessible"):
        await agent_generator._match_agent_capabilities(
            prompt="Research public sources.",
            spec={"name": "Research Agent", "description": "Search public sources."},
            entity_id="01ENTITY",
            capability_catalog=_catalog(),
        )


@pytest.mark.asyncio
async def test_agent_ai_final_review_cannot_widen_the_shortlist(monkeypatch) -> None:
    from packages.core.services import agent_generator

    calls = 0

    async def fake_completion(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            selected = ["capability:web.safe_search"]
        else:
            # This id exists in the full catalog, but the scan did not recall it.
            selected = ["mcp:gmail:send_message"]
        return SimpleNamespace(content=json.dumps({"capability_ids": selected}))

    import json

    monkeypatch.setattr(
        agent_generator,
        "runtime_execute_text_completion",
        fake_completion,
    )

    with pytest.raises(AgentCapabilitySelectionError, match="unknown or inaccessible"):
        await agent_generator._match_agent_capabilities(
            prompt="Research public sources without sending messages.",
            spec={"name": "Research Agent", "description": "Search public sources."},
            entity_id="01ENTITY",
            capability_catalog=_catalog(),
        )

    assert calls == 3  # scan, final review, one bounded repair attempt


@pytest.mark.asyncio
async def test_agent_ai_scans_every_catalog_chunk_before_final_review(monkeypatch) -> None:
    from packages.core.services import agent_generator

    catalog = _catalog()
    required = {
        "capability:web.safe_search",
        "skill:01SKILL",
        "mcp:gmail:list_messages",
    }
    scanned_ids: list[str] = []
    calls: list[list[dict[str, str]]] = []

    async def fake_completion(messages, **_kwargs):
        calls.append(messages)
        user_content = messages[1]["content"]
        supplied = {
            candidate.catalog_id
            for candidate in catalog.candidates
            if candidate.catalog_id in user_content
        }
        if "Semantic scan chunk" in user_content:
            scanned_ids.extend(sorted(supplied))
            selected = sorted(required & supplied)
        else:
            selected = sorted(required)
        return SimpleNamespace(content=json.dumps({"capability_ids": selected}))

    import json

    monkeypatch.setattr(agent_generator, "_CAPABILITY_SCAN_MAX_ITEMS", 2)
    monkeypatch.setattr(
        agent_generator,
        "runtime_execute_text_completion",
        fake_completion,
    )
    plan = await agent_generator._match_agent_capabilities(
        prompt="Research public sources, then use Gmail to read supporting messages.",
        spec={"name": "Research Agent", "description": "Research with email evidence."},
        entity_id="01ENTITY",
        capability_catalog=catalog,
    )

    assert len(calls) == 4  # three catalog scans plus one final review
    assert sorted(scanned_ids) == sorted(
        candidate.catalog_id for candidate in catalog.candidates
    )
    assert plan.selected_catalog_ids == tuple(sorted(required))


@pytest.mark.asyncio
async def test_agent_ai_hierarchically_reduces_large_recalled_catalog(monkeypatch) -> None:
    from packages.core.services import agent_generator
    from packages.core.services.agent_capability_catalog import (
        AgentCapabilityCandidate,
        AgentCapabilityCatalog,
        AgentCapabilityKind,
    )

    candidates = tuple(
        AgentCapabilityCandidate(
            catalog_id=f"tool:catalog_action_{index:04d}",
            kind=AgentCapabilityKind.TOOL,
            ref=f"catalog_action_{index:04d}",
            name=f"Catalog action {index:04d}",
            description="Inspect one bounded catalog record.",
        )
        for index in range(250)
    )
    catalog = AgentCapabilityCatalog(candidates=candidates)
    call_sizes: list[int] = []

    async def fake_completion(messages, **_kwargs):
        user_content = messages[1]["content"]
        call_sizes.append(len(user_content))
        supplied = [
            candidate.catalog_id
            for candidate in candidates
            if candidate.catalog_id in user_content
        ]
        if "Semantic scan chunk" in user_content:
            selected = supplied
        elif "Least-privilege reduction" in user_content:
            selected = supplied[: agent_generator._CAPABILITY_REDUCTION_SELECTION_LIMIT]
        else:
            selected = supplied[:1]
        return SimpleNamespace(content=json.dumps({"capability_ids": selected}))

    import json

    monkeypatch.setattr(agent_generator, "_CAPABILITY_SCAN_MAX_ITEMS", 25)
    monkeypatch.setattr(agent_generator, "_CAPABILITY_REVIEW_MAX_ITEMS", 120)
    monkeypatch.setattr(agent_generator, "_CAPABILITY_REVIEW_MAX_CHARS", 48_000)
    monkeypatch.setattr(agent_generator, "_CAPABILITY_REDUCTION_SELECTION_LIMIT", 24)
    monkeypatch.setattr(
        agent_generator,
        "runtime_execute_text_completion",
        fake_completion,
    )

    plan = await agent_generator._match_agent_capabilities(
        prompt="Inspect the catalog safely.",
        spec={"name": "Catalog Agent", "description": "Inspect catalog records."},
        entity_id="01ENTITY",
        capability_catalog=catalog,
    )

    assert plan.selected_catalog_ids == ("tool:catalog_action_0000",)
    assert len(call_sizes) == 14  # ten scans, three reducers, one final review
    assert max(call_sizes) < 55_000


@pytest.mark.asyncio
async def test_agent_ai_rejects_catalog_beyond_bounded_scan_before_provider(
    monkeypatch,
) -> None:
    from packages.core.services import agent_generator
    from packages.core.services.agent_capability_catalog import (
        AgentCapabilityCandidate,
        AgentCapabilityCatalog,
        AgentCapabilityKind,
        AgentCapabilitySelectionError,
    )

    candidates = tuple(
        AgentCapabilityCandidate(
            catalog_id=f"tool:bounded_action_{index:02d}",
            kind=AgentCapabilityKind.TOOL,
            ref=f"bounded_action_{index:02d}",
            name=f"Bounded action {index:02d}",
        )
        for index in range(13)
    )
    calls = 0

    async def fake_completion(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(content='{"capability_ids": []}')

    monkeypatch.setattr(agent_generator, "_CAPABILITY_SCAN_MAX_ITEMS", 1)
    monkeypatch.setattr(agent_generator, "_CAPABILITY_SCAN_MAX_CHUNKS", 12)
    monkeypatch.setattr(
        agent_generator,
        "runtime_execute_text_completion",
        fake_completion,
    )

    with pytest.raises(
        AgentCapabilitySelectionError,
        match="bounded semantic scan capacity",
    ):
        await agent_generator._match_agent_capabilities(
            prompt="Inspect the bounded catalog.",
            spec={"name": "Bounded Agent", "description": "Inspect records."},
            entity_id="01ENTITY",
            capability_catalog=AgentCapabilityCatalog(candidates=candidates),
        )

    assert calls == 0


@pytest.mark.asyncio
async def test_agent_ai_provider_budget_stops_repair_before_an_extra_call(
    monkeypatch,
) -> None:
    from packages.core.services import agent_generator
    from packages.core.services.agent_capability_catalog import (
        AgentCapabilitySelectionError,
    )

    calls = 0

    async def invalid_completion(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(content="not-json")

    monkeypatch.setattr(agent_generator, "_CAPABILITY_PROVIDER_CALL_BUDGET", 1)
    monkeypatch.setattr(
        agent_generator,
        "runtime_execute_text_completion",
        invalid_completion,
    )

    with pytest.raises(
        AgentCapabilitySelectionError,
        match="bounded provider-call budget",
    ):
        await agent_generator._match_agent_capabilities(
            prompt="Use public search.",
            spec={"name": "Search Agent", "description": "Research public data."},
            entity_id="01ENTITY",
            capability_catalog=_catalog(),
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_agent_ai_factory_serializes_catalog_provider_calls(
    monkeypatch,
) -> None:
    import asyncio
    import json

    from packages.core.services import agent_generator

    catalog = _catalog()
    active = 0
    max_active = 0

    async def fake_completion(messages, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            content = messages[1]["content"]
            selected = [
                candidate.catalog_id
                for candidate in catalog.candidates
                if candidate.catalog_id in content
            ]
            return SimpleNamespace(
                content=json.dumps({"capability_ids": selected})
            )
        finally:
            active -= 1

    monkeypatch.setattr(agent_generator, "_CAPABILITY_SCAN_MAX_ITEMS", 1)
    monkeypatch.setattr(
        agent_generator,
        "runtime_execute_text_completion",
        fake_completion,
    )

    plan = await agent_generator._match_agent_capabilities(
        prompt="Use every declared test capability.",
        spec={"name": "Serialized Agent", "description": "Exercise the catalog."},
        entity_id="01ENTITY",
        capability_catalog=catalog,
    )

    assert len(plan.selected_catalog_ids) == len(catalog.candidates)
    assert max_active == 1


@pytest.mark.asyncio
async def test_workspace_custom_agent_resolves_exact_catalog_ids(db_session) -> None:
    from packages.core.ai.tools.workspace_arch_tools import (
        _request_custom_agent,
        _search_capabilities,
    )
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    entity_id = generate_ulid()
    user = User(
        id=generate_ulid(),
        entity_id=entity_id,
        email=f"capability-factory-{generate_ulid()}@example.com",
        password_hash="test",
        role="member",
        status="active",
    )
    skill = Skill(
        id=generate_ulid(),
        entity_id=entity_id,
        owner_user_id=user.id,
        visibility="entity",
        name="Exact Catalog Research",
        slug=f"exact-catalog-{generate_ulid()}",
        description="Run a structured research and evidence workflow.",
        system_prompt="Research, preserve sources, and produce a concise evidence report.",
        tools=[],
        is_public=False,
        status="active",
    )
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=user.id,
        fields=dict(DEFAULT_FIELDS),
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add_all([user, skill, draft])
    await db_session.flush()

    payload = await _search_capabilities(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        draft_id=draft.id,
    )
    import json

    catalog = json.loads(payload)["agent_capability_catalog"]
    selected_id = next(
        item["id"] for item in catalog if item["id"] == f"skill:{skill.id}"
    )
    result = json.loads(await _request_custom_agent(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        draft_id=draft.id,
        service_key="research",
        agent_name="Research Agent",
        system_prompt=(
            "You are a research specialist. Gather evidence, preserve sources, "
            "synthesize findings, state uncertainty, and remain within research scope."
        ),
        capability_ids=[selected_id],
    ))

    assert result["ok"] is True
    await db_session.flush()
    await db_session.refresh(draft)
    [mapping] = draft.fields["agent_mappings"]
    create_draft = mapping["create_agent_draft"]
    assert create_draft["skill_bindings"] == [skill.id]
    assert "invoke_skill" in create_draft["tool_bindings"]
    assert "capability_ids" not in create_draft


@pytest.mark.asyncio
async def test_workspace_service_capability_search_reuses_semantic_factory_and_is_compact(
    db_session,
    monkeypatch,
) -> None:
    import json

    from packages.core.ai.tools.workspace_arch_tools import (
        _request_custom_agent,
        _search_capabilities,
    )
    from packages.core.services import agent_generator
    from packages.core.services.agent_capability_catalog import (
        AgentCapabilityCatalogFactory,
    )
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    entity_id = generate_ulid()
    user = User(
        id=generate_ulid(),
        entity_id=entity_id,
        email=f"workspace-semantic-factory-{generate_ulid()}@example.com",
        password_hash="test",
        role="member",
        status="active",
    )
    fields = dict(DEFAULT_FIELDS)
    fields.update({
        "kind": "support_desk",
        "category": "Support",
        "operating_context": "An operator reviews every external side effect.",
        "primary_work": "Triage email and inspect related customer pages in Chrome.",
        "services": [{
            "service_key": "inbox_browser_operations",
            "name": "Inbox Browser Operations",
            "description": "Read email, prepare unsent drafts, and inspect web pages.",
            "autonomy_level": "supervised",
            "owner_role": "support_operator",
        }],
        "rules": [{
            "service_key": "inbox_browser_operations",
            "rule_type": "approval_required",
            "description": "Require approval before external side effects.",
        }],
    })
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=user.id,
        fields=fields,
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add_all([user, draft])
    await db_session.flush()

    raw_catalog, _ = _large_selection_catalog(tool_count=190, mcp_server_count=10)
    catalog = AgentCapabilityCatalog(
        candidates=raw_catalog.candidates,
        tools=tuple(
            {
                "name": f"capacity_tool_{index:03d}",
                "description": f"Tool {index}",
                "parameters": {"type": "object", "properties": {}},
            }
            for index in range(1, 190)
        ),
        integrations=tuple(
            {
                "mcp_server_key": f"capacity_mcp_{index:03d}",
                "name": f"MCP {index}",
            }
            for index in range(10)
        ),
    )

    async def fake_create(_cls, _db, **_kwargs):
        return catalog

    captured: dict[str, object] = {}

    async def fake_match(**kwargs):
        captured["provider_started_in_transaction"] = db_session.in_transaction()
        captured.update(kwargs)
        return catalog.resolve([
            "tool:capacity_tool_042",
            "mcp:capacity_mcp_003",
        ])

    monkeypatch.setattr(AgentCapabilityCatalogFactory, "create", classmethod(fake_create))
    monkeypatch.setattr(agent_generator, "match_agent_capabilities", fake_match)

    raw = await _search_capabilities(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        draft_id=draft.id,
        service_key="inbox_browser_operations",
        agent_name="Inbox Browser Agent",
        intent="Never send email.",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["agent_capability_plan"]["capability_ids"] == [
        "tool:capacity_tool_042",
        "mcp:capacity_mcp_003",
    ]
    assert [item["id"] for item in payload["agent_capability_catalog"]] == [
        "tool:capacity_tool_042",
        "mcp:capacity_mcp_003",
    ]
    assert payload["catalog_stats"] == {
        "total_candidates": 200,
        "selected_candidates": 2,
        "selection_limit": 200,
    }
    assert payload["selection_scope"] == {
        "service_key": "inbox_browser_operations",
        "agent_name": "Inbox Browser Agent",
    }
    assert len(raw) < 5_000
    assert captured["capability_catalog"] is catalog
    assert captured["provider_started_in_transaction"] is False
    assert db_session.in_transaction() is True
    assert "Never send email." in str(captured["prompt"])
    assert "Require approval" in str(captured["prompt"])

    request_payload = json.loads(await _request_custom_agent(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        draft_id=draft.id,
        service_key="inbox_browser_operations",
        agent_name="Inbox Browser Agent",
        system_prompt=(
            "You are Inbox Browser Agent. Read inbound email, prepare unsent "
            "drafts, inspect web pages, require approval for side effects, and "
            "stay strictly within inbox and browser operations."
        ),
        # Simulate the outer LLM shortening an exact action catalog id. The
        # server-side stored Factory plan remains authoritative.
        capability_ids=["mcp:capacity_mcp_003:invented_action"],
    ))
    assert request_payload["ok"] is True
    assert request_payload["capability_selection_source"] == "stored_factory_plan"
    await db_session.flush()
    await db_session.refresh(draft)
    [mapping] = draft.fields["agent_mappings"]
    assert mapping["create_agent_draft"]["tool_bindings"] == ["capacity_tool_042"]
    assert mapping["create_agent_draft"]["mcp_bindings"] == ["capacity_mcp_003"]

    # Fingerprints are a second line of defense for edits that bypass the
    # Architect helpers and therefore cannot proactively clear stored plans.
    changed_fields = dict(draft.fields)
    changed_fields["operating_context"] = "Agents may now send external replies."
    draft.fields = changed_fields
    stale_payload = json.loads(await _request_custom_agent(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        draft_id=draft.id,
        service_key="inbox_browser_operations",
        agent_name="Inbox Browser Agent",
        system_prompt=(
            "You are Inbox Browser Agent. Read inbound email, prepare replies, "
            "inspect web pages, and stay within inbox and browser operations."
        ),
    ))
    assert stale_payload["ok"] is False
    assert "stale" in stale_payload["error"]


@pytest.mark.asyncio
async def test_empty_stored_factory_plan_denies_legacy_agent_bindings(
    db_session,
    monkeypatch,
) -> None:
    import json

    from packages.core.ai.tools.workspace_arch_tools import (
        _request_custom_agent,
        _search_capabilities,
    )
    from packages.core.services import agent_generator
    from packages.core.services.agent_capability_catalog import (
        AgentCapabilityCatalogFactory,
    )
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    entity_id = generate_ulid()
    user = User(
        id=generate_ulid(),
        entity_id=entity_id,
        email=f"empty-capability-plan-{generate_ulid()}@example.com",
        password_hash="test",
        role="member",
        status="active",
    )
    fields = dict(DEFAULT_FIELDS)
    fields.update({
        "kind": "analysis",
        "operating_context": "No external side effects are allowed.",
        "primary_work": "Summarize information already available to the agent.",
        "services": [{
            "service_key": "read_only_service",
            "name": "Read-only Service",
            "description": "Summarize supplied information without external actions.",
            "autonomy_level": "supervised",
            "owner_role": "analyst",
        }],
    })
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=user.id,
        fields=fields,
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add_all([user, draft])
    await db_session.flush()

    async def fake_create(_cls, _db, **_kwargs):
        return _catalog()

    monkeypatch.setattr(
        AgentCapabilityCatalogFactory,
        "create",
        classmethod(fake_create),
    )

    async def fake_match(**_kwargs):
        return _catalog().resolve([])

    monkeypatch.setattr(agent_generator, "match_agent_capabilities", fake_match)
    search_result = json.loads(await _search_capabilities(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        draft_id=draft.id,
        service_key="read_only_service",
    ))
    assert search_result["ok"] is True
    assert search_result["agent_capability_plan"]["capability_ids"] == []

    result = json.loads(await _request_custom_agent(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        draft_id=draft.id,
        service_key="read_only_service",
        agent_name="Read-only Agent",
        system_prompt=(
            "You are a read-only specialist. Summarize available information, "
            "avoid every external action, and remain strictly within analysis scope."
        ),
        capability_ids=["tool:web_search"],
        tool_bindings=["web_search"],
        business_capabilities=["web.safe_search"],
        skill_bindings=["01SKILL"],
        mcp_bindings=["gmail"],
    ))

    assert result["ok"] is True
    assert result["capability_selection_source"] == "stored_factory_plan"
    await db_session.flush()
    await db_session.refresh(draft)
    [mapping] = draft.fields["agent_mappings"]
    create_draft = mapping["create_agent_draft"]
    assert create_draft["tool_bindings"] == []
    assert create_draft["business_capabilities"] == []
    assert create_draft["skill_bindings"] == []
    assert create_draft["mcp_bindings"] == []


@pytest.mark.asyncio
async def test_agent_capability_plans_expire_when_matching_context_changes(
    db_session,
) -> None:
    from packages.core.ai.tools.workspace_arch_tools import (
        _commit_basics,
        _propose_rule,
    )
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    entity_id = generate_ulid()
    user = User(
        id=generate_ulid(),
        entity_id=entity_id,
        email=f"capability-plan-expiry-{generate_ulid()}@example.com",
        password_hash="test",
        role="member",
        status="active",
    )
    fields = dict(DEFAULT_FIELDS)
    fields.update({
        "name": "Support",
        "primary_work": "Read inbound support requests.",
        "agent_capability_plans": [{
            "service_key": "support",
            "capability_ids": ["mcp:gmail:list_messages"],
        }],
    })
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=user.id,
        fields=fields,
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add_all([user, draft])
    await db_session.flush()

    await _commit_basics(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        draft_id=draft.id,
        name="Support renamed",
    )
    assert draft.fields["agent_capability_plans"]

    await _commit_basics(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        draft_id=draft.id,
        primary_work="Send replies to inbound support requests.",
    )
    assert draft.fields["agent_capability_plans"] == []

    next_fields = dict(draft.fields)
    next_fields["agent_capability_plans"] = [{
        "service_key": "support",
        "capability_ids": ["mcp:gmail:send_message"],
    }]
    draft.fields = next_fields
    await _propose_rule(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        draft_id=draft.id,
        rule_key="never_send",
        description="Never send an external reply without human approval.",
        scope="support",
        severity="block",
    )
    assert draft.fields["agent_capability_plans"] == []




@pytest.mark.asyncio
async def test_workspace_custom_agent_accepts_one_hundred_forty_capabilities(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.ai.tools.workspace_arch_tools import _request_custom_agent
    from packages.core.services.agent_capability_catalog import (
        AgentCapabilityCatalogFactory,
    )
    from packages.core.services.workspace_setup_service import DEFAULT_FIELDS

    entity_id = generate_ulid()
    user = User(
        id=generate_ulid(),
        entity_id=entity_id,
        email=f"capability-capacity-{generate_ulid()}@example.com",
        password_hash="test",
        role="member",
        status="active",
    )
    draft = WorkspaceDraft(
        entity_id=entity_id,
        user_id=user.id,
        fields=dict(DEFAULT_FIELDS),
        messages=[],
        missing=[],
        ready=False,
        status="active",
    )
    db_session.add_all([user, draft])
    await db_session.flush()
    catalog, selected = _large_selection_catalog(
        tool_count=120,
        skill_count=10,
        mcp_server_count=10,
    )

    async def fake_create(_cls, _db, **_kwargs):
        return catalog

    monkeypatch.setattr(
        AgentCapabilityCatalogFactory,
        "create",
        classmethod(fake_create),
    )
    import json

    result = json.loads(await _request_custom_agent(
        db_session,
        entity_id=entity_id,
        user_id=user.id,
        draft_id=draft.id,
        service_key="capacity_service",
        agent_name="Capacity Agent",
        system_prompt=(
            "You operate the capacity service with only its explicitly selected "
            "tools, MCP servers, and skills. Escalate unsafe or ambiguous actions."
        ),
        capability_ids=selected,
    ))

    assert result["ok"] is True
    await db_session.flush()
    await db_session.refresh(draft)
    [mapping] = draft.fields["agent_mappings"]
    create_draft = mapping["create_agent_draft"]
    assert len(create_draft["tool_bindings"]) == 120
    assert len(create_draft["skill_bindings"]) == 10
    assert len(create_draft["mcp_bindings"]) == 10


@pytest.mark.asyncio
async def test_mcp_materializer_persists_exact_action_allowlist(db_session) -> None:
    from packages.core.services.agent_provisioning_service import _bind_mcp_servers

    server = MCPServer(
        id=generate_ulid(),
        server_key=f"catalog_test_{generate_ulid().lower()}",
        name="Catalog Test",
        transport="http",
        endpoint="https://example.invalid/mcp",
        auth_type="none",
        status="active",
    )
    db_session.add(server)
    action_tools = [
        ToolDefinition(
            id=generate_ulid(),
            name=f"mcp__{server.server_key}__{action}",
            display_name=action.replace("_", " ").title(),
            category="mcp",
            status="active",
        )
        for action in ("list_items", "create_item", "delete_item")
    ]
    db_session.add_all(action_tools)
    await db_session.flush()
    agent_id = generate_ulid()
    warnings: list[str] = []

    bound = await _bind_mcp_servers(
        db_session,
        agent_id=agent_id,
        refs=[server.server_key],
        warnings=warnings,
        allowed_tools_by_server={server.server_key: ["list_items", "create_item"]},
    )
    await db_session.flush()
    binding = (await db_session.execute(
        select(AgentMCPBinding).where(
            AgentMCPBinding.agent_id == agent_id,
            AgentMCPBinding.mcp_server_id == server.id,
        )
    )).scalar_one()

    assert warnings == []
    assert bound == [server.server_key]
    assert binding.allowed_tools == ["create_item", "list_items"]
    mirrored_names = set((await db_session.execute(
        select(ToolDefinition.name)
        .join(AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id)
        .where(AgentToolBinding.agent_id == agent_id)
    )).scalars().all())
    assert mirrored_names == {
        f"mcp__{server.server_key}__create_item",
        f"mcp__{server.server_key}__list_items",
    }

    rebound = await _bind_mcp_servers(
        db_session,
        agent_id=agent_id,
        refs=[server.server_key],
        warnings=warnings,
        allowed_tools_by_server={server.server_key: ["list_items"]},
    )
    await db_session.flush()
    mirrored_names = set((await db_session.execute(
        select(ToolDefinition.name)
        .join(AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id)
        .where(AgentToolBinding.agent_id == agent_id)
    )).scalars().all())

    assert rebound == [server.server_key]
    assert binding.allowed_tools == ["list_items"]
    assert mirrored_names == {f"mcp__{server.server_key}__list_items"}


@pytest.mark.asyncio
async def test_mcp_materializer_merges_actions_across_capability_batches(
    db_session,
) -> None:
    from packages.core.services.agent_provisioning_service import (
        AgentMCPBindingUpdateMode,
        _bind_mcp_servers,
    )

    server = MCPServer(
        id=generate_ulid(),
        server_key=f"catalog_merge_{generate_ulid().lower()}",
        name="Catalog Merge",
        transport="http",
        endpoint="https://example.invalid/mcp",
        auth_type="none",
        status="active",
    )
    db_session.add(server)
    db_session.add_all([
        ToolDefinition(
            id=generate_ulid(),
            name=f"mcp__{server.server_key}__{action}",
            display_name=action.replace("_", " ").title(),
            category="mcp",
            status="active",
        )
        for action in ("list_items", "create_item", "delete_item")
    ])
    await db_session.flush()
    agent_id = generate_ulid()
    warnings: list[str] = []

    for action in ("list_items", "create_item"):
        await _bind_mcp_servers(
            db_session,
            agent_id=agent_id,
            refs=[server.server_key],
            warnings=warnings,
            allowed_tools_by_server={server.server_key: [action]},
            update_mode=AgentMCPBindingUpdateMode.MERGE,
        )
        await db_session.flush()

    binding = (await db_session.execute(
        select(AgentMCPBinding).where(
            AgentMCPBinding.agent_id == agent_id,
            AgentMCPBinding.mcp_server_id == server.id,
        )
    )).scalar_one()
    mirrored_names = set((await db_session.execute(
        select(ToolDefinition.name)
        .join(AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id)
        .where(AgentToolBinding.agent_id == agent_id)
    )).scalars().all())

    assert warnings == []
    assert binding.allowed_tools == ["create_item", "list_items"]
    assert mirrored_names == {
        f"mcp__{server.server_key}__create_item",
        f"mcp__{server.server_key}__list_items",
    }


@pytest.mark.asyncio
async def test_mcp_materializer_mirrors_all_actions_for_server_level_binding(
    db_session,
) -> None:
    from packages.core.services.agent_provisioning_service import _bind_mcp_servers

    server = MCPServer(
        id=generate_ulid(),
        server_key=f"catalog_all_{generate_ulid().lower()}",
        name="Catalog All",
        transport="http",
        endpoint="https://example.invalid/mcp",
        auth_type="none",
        status="active",
    )
    db_session.add(server)
    actions = ("list_items", "create_item", "delete_item")
    db_session.add_all([
        ToolDefinition(
            id=generate_ulid(),
            name=f"mcp__{server.server_key}__{action}",
            display_name=action.replace("_", " ").title(),
            category="mcp",
            status="active",
        )
        for action in actions
    ])
    await db_session.flush()
    agent_id = generate_ulid()
    warnings: list[str] = []

    await _bind_mcp_servers(
        db_session,
        agent_id=agent_id,
        refs=[server.server_key],
        warnings=warnings,
        allowed_tools_by_server={server.server_key: None},
    )
    await db_session.flush()

    binding = (await db_session.execute(
        select(AgentMCPBinding).where(
            AgentMCPBinding.agent_id == agent_id,
            AgentMCPBinding.mcp_server_id == server.id,
        )
    )).scalar_one()
    mirrored_names = set((await db_session.execute(
        select(ToolDefinition.name)
        .join(AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id)
        .where(AgentToolBinding.agent_id == agent_id)
    )).scalars().all())

    assert warnings == []
    assert binding.allowed_tools is None
    assert mirrored_names == {
        f"mcp__{server.server_key}__{action}" for action in actions
    }


@pytest.mark.asyncio
async def test_public_mcp_operations_are_mirrored_into_editor_catalog(db_session) -> None:
    from packages.core.services.agent_capability_catalog import (
        _ensure_integration_tool_definitions,
    )

    server = MCPServer(
        id=generate_ulid(),
        server_key=f"editor_catalog_{generate_ulid().lower()}",
        name="Editor Catalog",
        transport="http",
        endpoint="https://example.invalid/mcp",
        auth_type="none",
        status="active",
    )
    db_session.add(server)
    await db_session.flush()
    tool_name = f"mcp__{server.server_key}__create_draft"
    operations = {
        server.server_key: [{
            "name": "create_draft",
            "tool_name": tool_name,
            "label": "Create draft",
            "description": "Create an email draft without sending it.",
            "input_schema": {
                "type": "object",
                "properties": {"subject": {"type": "string"}},
            },
        }],
    }

    created = await _ensure_integration_tool_definitions(
        db_session,
        servers=[server],
        operations_by_server=operations,
    )
    tool = (await db_session.execute(
        select(ToolDefinition).where(ToolDefinition.name == tool_name)
    )).scalar_one()
    tool.status = "inactive"
    tool.description = "stale"
    created_again = await _ensure_integration_tool_definitions(
        db_session,
        servers=[server],
        operations_by_server=operations,
    )

    assert created == 1
    assert created_again == 0
    assert tool.status == "active"
    assert tool.description == "Create an email draft without sending it."
    assert tool.category == "mcp"
    assert tool.schema["function"]["parameters"]["properties"]["subject"] == {
        "type": "string"
    }


@pytest.mark.asyncio
async def test_account_discovered_action_stays_actor_scoped(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.services import (
        integration_operation_catalog,
        integration_resolution,
    )
    from packages.core.services.agent_capability_catalog import (
        AgentCapabilityCatalogFactory,
        materialize_agent_capability_plan,
    )

    server_key = f"actor_catalog_{generate_ulid().lower()}"
    server = MCPServer(
        id=generate_ulid(),
        server_key=server_key,
        name="Actor Catalog",
        transport="http",
        endpoint="https://example.invalid/mcp",
        auth_type="oauth2",
        status="active",
    )
    db_session.add(server)
    await db_session.flush()
    operation = {
        "name": "create_private_draft",
        "tool_name": f"mcp__{server_key}__create_private_draft",
        "label": "Create private draft",
        "description": "Create a draft available to the connected account.",
        "effect": "write",
        "resource": "draft",
        "input_schema": {
            "type": "object",
            "properties": {"subject": {"type": "string"}},
        },
    }
    public_operation = {
        **operation,
        "name": "list_public_drafts",
        "tool_name": f"mcp__{server_key}__list_public_drafts",
        "label": "List public drafts",
        "description": "List the provider's credential-free public draft schema.",
        "effect": "read",
    }

    async def fake_supported(_db):
        return {server_key}

    async def fake_readiness(*_args, **_kwargs):
        return {}

    async def fake_actor_catalog(*_args, **_kwargs):
        return [operation], "account_discovery"

    def fake_public_catalog(*_args, **_kwargs):
        return [public_operation], "managed_catalog"

    monkeypatch.setattr(
        integration_resolution,
        "supported_integration_provider_keys",
        fake_supported,
    )
    monkeypatch.setattr(
        integration_resolution,
        "integration_provider_readiness",
        fake_readiness,
    )
    monkeypatch.setattr(
        integration_operation_catalog,
        "actor_integration_operation_catalog",
        fake_actor_catalog,
    )
    monkeypatch.setattr(
        integration_operation_catalog,
        "integration_operation_catalog",
        fake_public_catalog,
    )

    entity_id = generate_ulid()
    user_id = generate_ulid()
    catalog = await AgentCapabilityCatalogFactory.create(
        db_session,
        entity_id=entity_id,
        user_id=user_id,
    )
    catalog_id = f"mcp:{server_key}:create_private_draft"
    plan = catalog.resolve([catalog_id])
    agent_id = generate_ulid()
    await materialize_agent_capability_plan(
        db_session,
        entity_id=entity_id,
        user_id=user_id,
        agent_id=agent_id,
        plan=plan,
    )

    binding = (await db_session.execute(
        select(AgentMCPBinding).where(
            AgentMCPBinding.agent_id == agent_id,
            AgentMCPBinding.mcp_server_id == server.id,
        )
    )).scalar_one()
    mirrored_names = set((await db_session.execute(
        select(ToolDefinition.name)
        .join(AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id)
        .where(AgentToolBinding.agent_id == agent_id)
    )).scalars())

    assert binding.allowed_tools == ["create_private_draft"]
    assert mirrored_names == set()
    assert (await db_session.execute(
        select(ToolDefinition).where(ToolDefinition.name == operation["tool_name"])
    )).scalar_one_or_none() is None
    public_tool = (await db_session.execute(
        select(ToolDefinition).where(
            ToolDefinition.name == public_operation["tool_name"]
        )
    )).scalar_one()
    assert public_tool.description == public_operation["description"]
