"""Every internal principal can use the Sandbox; external principals cannot.

Product decision 2026-10-06. The guards derive their cases from the
RuntimeProfile / ChatSurface enums and the surface->profile map, so a profile
or surface added later is covered without editing this file.
"""
from __future__ import annotations

import logging

import pytest

from packages.core.ai.runtime.capabilities import (
    CORE_CAPABILITIES,
    allowed_tools_for_profile,
    tool_names_for_capability_ids,
)
from packages.core.ai.runtime.principals import resolve_runtime_principal
from packages.core.ai.runtime.profiles import (
    EXTERNAL_CHAT_SURFACES,
    EXTERNAL_RUNTIME_PROFILES,
    INTERNAL_CHAT_SURFACES,
    INTERNAL_RUNTIME_PROFILES,
    RuntimeProfile,
    profile_for_surface,
    runtime_surface_is_internal,
)
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.constants.runtime_principal import RuntimePrincipalKind


_EXTERNAL_PRINCIPAL_KINDS = {
    RuntimePrincipalKind.EXTERNAL_CONTACT,
    RuntimePrincipalKind.ANONYMOUS_PUBLIC,
}


def test_profiles_and_surfaces_partition_into_internal_and_external() -> None:
    assert set(INTERNAL_RUNTIME_PROFILES) | EXTERNAL_RUNTIME_PROFILES == set(RuntimeProfile)
    assert not set(INTERNAL_RUNTIME_PROFILES) & EXTERNAL_RUNTIME_PROFILES
    assert INTERNAL_CHAT_SURFACES | EXTERNAL_CHAT_SURFACES == set(ChatSurface)
    assert not INTERNAL_CHAT_SURFACES & EXTERNAL_CHAT_SURFACES


@pytest.mark.parametrize("surface", list(ChatSurface), ids=lambda s: s.value)
def test_external_surfaces_are_exactly_the_external_principal_surfaces(surface) -> None:
    # The canonical external set must agree with the principal resolver: a
    # surface is external iff its turns run as an outside contact/visitor.
    principal = resolve_runtime_principal(
        surface=surface, entity_id="entity", user_id="user", agent_id="agent",
    )
    assert (principal.kind in _EXTERNAL_PRINCIPAL_KINDS) == (surface in EXTERNAL_CHAT_SURFACES)
    assert runtime_surface_is_internal(surface) == (surface not in EXTERNAL_CHAT_SURFACES)


@pytest.mark.parametrize("profile", list(RuntimeProfile), ids=lambda p: p.value)
def test_sandbox_execute_capability_follows_the_internal_profile_set(profile) -> None:
    internal = profile not in EXTERNAL_RUNTIME_PROFILES
    assert (profile in CORE_CAPABILITIES["sandbox.execute"].profiles) is internal
    granted = tool_names_for_capability_ids({"sandbox.execute"}, profile=profile)
    assert ("sandbox" in granted) is internal
    hard_allowlist = allowed_tools_for_profile(profile)
    # ``None`` = a legacy internal profile without a hard schema allowlist.
    reachable = hard_allowlist is None or "sandbox" in hard_allowlist
    assert reachable is internal


def test_surface_internal_predicate_fails_closed_on_unknown_names() -> None:
    assert runtime_surface_is_internal(None)
    assert not runtime_surface_is_internal("not_a_surface")
    for surface in ChatSurface:
        assert runtime_surface_is_internal(surface.value) == (
            profile_for_surface(surface) not in EXTERNAL_RUNTIME_PROFILES
        )


# ── Tool surface: reachable without a binding, schema deferred ──────────


def _resolved_surface(surface: ChatSurface, *, is_master: bool):
    """Run one surface through the production tool-surface pipeline.

    Registry visibility (``runtime_tool_registry_surface_from_schemas``) feeds
    the RuntimeResolver stages that build the run's envelope, exactly as
    ``runtime_prepare_prompt_context`` does. The non-master Agent has no
    tool bindings at all.
    """
    from packages.core.ai.runtime.entrypoints import runtime_request_for_surface_turn
    from packages.core.ai.runtime.profiles import tool_profile_for_surface
    from packages.core.ai.runtime.resolver import RuntimeResolver
    from packages.core.ai.runtime.tool_registry import runtime_registered_tool_schemas
    from packages.core.ai.runtime.tool_visibility import (
        runtime_tool_registry_surface_from_schemas,
    )

    tool_profile = tool_profile_for_surface(surface)
    registry = runtime_tool_registry_surface_from_schemas(
        runtime_registered_tool_schemas(),
        bound_tool_names=None if is_master else set(),
        is_master=is_master,
        mcp_allowed_names=None if is_master else set(),
        tool_profile=tool_profile,
    )
    request = runtime_request_for_surface_turn(
        surface=surface,
        entity_id="entity",
        user_id="user",
        agent_id=None if is_master else "agent-without-bindings",
        workspace_id="workspace" if tool_profile else None,
    )
    resolved = RuntimeResolver().resolve_tool_surface(
        request,
        tool_profile=tool_profile,
        tool_schemas=registry.prompt_schemas,
        allowed_tool_names=registry.visible_tool_names,
    )
    return registry, resolved


def _schema_name(schema: dict) -> str:
    return str((schema.get("function") or schema).get("name") or "")


@pytest.mark.parametrize("is_master", [True, False], ids=["master", "unbound_agent"])
@pytest.mark.parametrize("surface", list(ChatSurface), ids=lambda s: s.value)
def test_every_internal_surface_reaches_the_sandbox_and_no_external_one_does(
    surface, is_master,
) -> None:
    from packages.core.ai.runtime.policies import check_runtime_tool_policy

    internal = surface in INTERNAL_CHAT_SURFACES
    registry, resolved = _resolved_surface(surface, is_master=is_master)
    envelope = resolved.envelope
    assert ("sandbox" in envelope.effective_allowed_tool_names()) is internal
    assert check_runtime_tool_policy(envelope=envelope, tool_name="sandbox").allowed is internal
    if internal and not is_master:
        # Default-deferred: discoverable through search_tools / call_tool,
        # without its schema in every unbound Agent's prompt.
        assert "sandbox" in registry.deferred_tool_names
        assert "sandbox" not in {_schema_name(schema) for schema in resolved.tool_schemas}
        assert {"search_tools", "call_tool"} <= envelope.effective_allowed_tool_names()


def test_default_granted_tools_are_internal_only_capabilities() -> None:
    from packages.core.ai.runtime.capabilities import capability_tool_name
    from packages.core.ai.runtime.composite_tools import SANDBOX_LEGACY_TOOL_BY_ACTION
    from packages.core.ai.runtime.tool_visibility import (
        AGENT_DEFAULT_DEFERRED_TOOLS,
        AGENT_DEFAULT_GRANTED_TOOLS,
        runtime_tool_is_internal_default_grant,
    )

    assert "sandbox" in AGENT_DEFAULT_GRANTED_TOOLS <= AGENT_DEFAULT_DEFERRED_TOOLS
    for name in AGENT_DEFAULT_GRANTED_TOOLS:
        owners = [
            capability for capability in CORE_CAPABILITIES.values()
            if name in capability.tool_names
        ]
        assert owners and all(
            not set(capability.profiles) & EXTERNAL_RUNTIME_PROFILES for capability in owners
        ), name
    # Every legacy per-action sandbox name resolves to the composite grant.
    for leaf in SANDBOX_LEGACY_TOOL_BY_ACTION.values():
        assert capability_tool_name(leaf) == "sandbox"
        assert runtime_tool_is_internal_default_grant(leaf)


# ── Authorization boundary: no durable binding, identity still checked ──


_SANDBOX_CALLS = {
    "create": {"action": "create"},
    "exec": {"action": "exec", "params": {"sandbox_id": "sbx", "command": "ls"}},
    "read_file": {"action": "read_file", "params": {"sandbox_id": "sbx", "path": "out.txt"}},
}
_INTERNAL_SURFACES = sorted(INTERNAL_CHAT_SURFACES, key=lambda s: s.value)
_EXTERNAL_SURFACES = sorted(EXTERNAL_CHAT_SURFACES, key=lambda s: s.value)


async def _seed_agent(db, **overrides):
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Agent

    agent = Agent(
        id=generate_ulid(), entity_id=generate_ulid(), name="Unbound analyst",
        status="active", **overrides,
    )
    db.add(agent)
    await db.flush()
    return agent


async def _authorize_sandbox(db, agent, call, **overrides):
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.services.runtime_authorization import authorize_runtime_action

    tool_name = overrides.pop("tool_name", "sandbox")
    authorization = classify_runtime_tool("sandbox", _SANDBOX_CALLS[call]).authorization
    kwargs = dict(
        entity_id=overrides.pop("entity_id", agent.entity_id), user_id=None, workspace_id=None,
        action_key=authorization.action_key, capability_id=authorization.capability_id,
        access=authorization.access, principal_kind="delegated", principal_agent_id=agent.id,
        tool_name=tool_name, bound_tool_names={"search_tools", "call_tool", tool_name},
        runtime_surface=ChatSurface.AGENT_DM,
    )
    kwargs.update(overrides)
    kwargs.setdefault("principal_execution_user_id", kwargs["user_id"])
    return await authorize_runtime_action(db, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("call", list(_SANDBOX_CALLS))
async def test_unbound_delegated_agent_uses_the_sandbox_on_every_internal_surface(
    db_session, call,
) -> None:
    # No AgentToolBinding row and no Skill handoff exist for this Agent.
    agent = await _seed_agent(db_session)
    for surface in [None, *_INTERNAL_SURFACES]:
        decision = await _authorize_sandbox(db_session, agent, call, runtime_surface=surface)
        assert decision.allowed, (surface, decision)


@pytest.mark.asyncio
async def test_unbound_delegated_agent_uses_every_legacy_sandbox_name(db_session) -> None:
    from packages.core.ai.runtime.composite_tools import SANDBOX_LEGACY_TOOL_BY_ACTION

    agent = await _seed_agent(db_session)
    for leaf in SANDBOX_LEGACY_TOOL_BY_ACTION.values():
        decision = await _authorize_sandbox(db_session, agent, "exec", tool_name=leaf)
        assert decision.allowed, (leaf, decision)


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["create", "read_file"])
async def test_sandbox_default_grant_keeps_membership_and_chat_use(
    db_session, monkeypatch, call,
) -> None:
    from packages.core import permissions
    from packages.core.permissions import Permission
    from packages.core.services import resource_access

    agent = await _seed_agent(db_session)
    role = {"value": None}
    granted: dict[str, object] = {"allowed": False, "required": None}

    async def fake_role(_db, *, user_id, entity_id, role_name=None, **_kwargs):
        return role["value"]

    async def fake_permission(_db, _user_id, _entity_id, _role, required):
        granted["required"] = required
        return granted["allowed"]

    monkeypatch.setattr(resource_access, "resolve_user_role", fake_role)
    monkeypatch.setattr(permissions, "user_has_effective_permission", fake_permission)

    decision = await _authorize_sandbox(db_session, agent, call, user_id="user-1")
    assert not decision.allowed
    assert decision.matched_rule == "permission.entity.membership"

    role["value"] = "member"
    decision = await _authorize_sandbox(db_session, agent, call, user_id="user-1")
    assert not decision.allowed
    assert granted["required"] is Permission.CHAT_USE

    granted["allowed"] = True
    assert (await _authorize_sandbox(db_session, agent, call, user_id="user-1")).allowed

    mismatch = await _authorize_sandbox(
        db_session, agent, call, user_id="user-1", principal_execution_user_id="user-2",
    )
    assert mismatch.matched_rule == "permission.runtime.identity_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["foreign_agent", "inactive_agent", "deleted_agent", "no_agent"])
async def test_sandbox_default_grant_keeps_agent_identity_checks(db_session, invalid) -> None:
    from datetime import datetime, timezone

    from packages.core.models.base import generate_ulid

    agent = await _seed_agent(db_session)
    overrides = {}
    if invalid == "foreign_agent":
        overrides["entity_id"] = generate_ulid()
    elif invalid == "inactive_agent":
        agent.status = "inactive"
    elif invalid == "deleted_agent":
        agent.deleted_at = datetime.now(timezone.utc)
    else:
        overrides["principal_agent_id"] = None
    await db_session.flush()
    for call in _SANDBOX_CALLS:
        decision = await _authorize_sandbox(db_session, agent, call, **overrides)
        assert not decision.allowed
        assert decision.matched_rule == "permission.agent.identity"


@pytest.mark.asyncio
async def test_sandbox_default_grant_does_not_widen_a_narrowed_loop(db_session) -> None:
    agent = await _seed_agent(db_session)
    decision = await _authorize_sandbox(
        db_session, agent, "exec", bound_tool_names={"search_tools", "call_tool"},
    )
    assert not decision.allowed
    assert decision.matched_rule == "permission.agent_tool_binding"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [*_EXTERNAL_SURFACES, "unknown_surface"], ids=str)
async def test_external_principals_and_surfaces_never_use_the_sandbox(db_session, surface) -> None:
    agent = await _seed_agent(db_session)
    for call in _SANDBOX_CALLS:
        for kind in _EXTERNAL_PRINCIPAL_KINDS:
            decision = await _authorize_sandbox(
                db_session, agent, call, principal_kind=kind.value, runtime_surface=surface,
            )
            assert not decision.allowed, (kind, call)
        # An Agent identity on an external or unrecognized surface gets no
        # implicit grant either.
        decision = await _authorize_sandbox(db_session, agent, call, runtime_surface=surface)
        assert not decision.allowed, (surface, call)


def _agent_envelope(surface: ChatSurface, agent_id: str, entity_id: str):
    from packages.core.ai.runtime.entrypoints import runtime_request_for_surface_turn
    from packages.core.ai.runtime.resolver import RuntimeResolver

    registry, _ = _resolved_surface(surface, is_master=False)
    request = runtime_request_for_surface_turn(
        surface=surface, entity_id=entity_id, user_id=None, agent_id=agent_id,
    )
    return RuntimeResolver().resolve_tool_surface(
        request,
        tool_schemas=registry.prompt_schemas,
        allowed_tool_names=registry.visible_tool_names,
    ).envelope


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [ChatSurface.AGENT_DM, ChatSurface.SCHEDULED_AGENT_RUN], ids=str)
async def test_execution_gate_admits_an_unbound_agent_sandbox(db_session, monkeypatch, caplog, surface) -> None:
    import json
    from contextlib import asynccontextmanager

    from packages.core import database
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approval_service import guard_runtime_tool_action

    agent = await _seed_agent(db_session)

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(database, "async_session", session)
    envelope = _agent_envelope(surface, agent.id, agent.entity_id)
    assert envelope.principal.kind is RuntimePrincipalKind.AGENT

    async def guard(call, runtime_envelope):
        arguments = dict(_SANDBOX_CALLS[call])
        return await guard_runtime_tool_action(
            name="sandbox", arguments=arguments, entity_id=agent.entity_id, user_id="",
            workspace_id=None, conversation_id="conversation-1",
            runtime_envelope=runtime_envelope,
            classification=classify_runtime_tool("sandbox", arguments),
        )

    # Creation is a Runtime control: it passes the boundary and never asks.
    assert await guard("create", envelope) is None
    # A command passes the permission boundary and continues to the policy
    # stage. This calls the guard directly with no call identity, which every
    # agent loop supplies; reaching the check is a platform fault, logged as one.
    caplog.clear()
    assert json.loads(await guard("exec", envelope))["error"] == "approval_operation_identity_missing"
    assert any(
        record.levelno == logging.ERROR and "missing run identity" in record.getMessage()
        for record in caplog.records
    )

    external = _agent_envelope(ChatSurface.PUBLIC_CUSTOMER_CHAT, agent.id, agent.entity_id)
    for call in ("create", "exec"):
        assert json.loads(await guard(call, external))["error"] == "blocked_by_permission"
