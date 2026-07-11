"""Unit tests for ``packages.core.auth.authz`` — pure, no DB.

Two contracts pinned by these tests:

  * **Legacy fallthrough is identical to the pre-v1 behavior.** When the
    ``permissions_v1_enforce`` flag is OFF, every (role, verb) pair must
    return the same Decision as ``packages.core.permissions.has_permission``.

  * **Strict mode invariants from RFC §13.14 fire deterministically.** When
    the flag is ON, each invariant is hit by at least one targeted case.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import packages.core.auth.authz as authz
from packages.core.auth import (
    AgentActor,
    Resource,
    UserActor,
    authorize,
)
from packages.core.models import (
    Capability,
    Classification,
    ResourceType,
    Visibility,
)
from packages.core.permissions import Permission, has_permission


@pytest.fixture(autouse=True)
def _isolate_authz(monkeypatch):
    """Stub out the audit and DB-touching helpers so tests stay pure."""
    monkeypatch.setattr(authz, "_audit", AsyncMock(return_value=None))
    monkeypatch.setattr(authz, "user_has_permission", AsyncMock(return_value=False))
    yield


@pytest.fixture
def db():
    return AsyncMock()


# ── Legacy fallthrough (flag OFF) ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "member", "admin", "owner"])
@pytest.mark.parametrize("perm", list(Permission))
async def test_legacy_matches_role_table(db, monkeypatch, role, perm):
    """Flag-off authorize() must return exactly what has_permission() returns."""
    monkeypatch.setattr(authz, "_enforce_enabled", AsyncMock(return_value=False))
    actor = UserActor(user_id="U", entity_id="E", role=role)
    decision = await authorize(db, actor, perm)
    assert decision.allow == has_permission(role, perm), (
        f"legacy mismatch for ({role}, {perm.value}): got {decision.allow}, want {has_permission(role, perm)}"
    )


@pytest.mark.asyncio
async def test_legacy_falls_back_to_staff_role(db, monkeypatch):
    """When the role table denies, the StaffRole JSONB path is consulted."""
    monkeypatch.setattr(authz, "_enforce_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(authz, "user_has_permission", AsyncMock(return_value=True))
    actor = UserActor(user_id="U", entity_id="E", role="viewer")
    decision = await authorize(db, actor, Permission.ADMIN_BILLING)
    assert decision.allow is True
    assert decision.matched_rule == "legacy.staff_role"


@pytest.mark.asyncio
async def test_legacy_agent_inherits_invoker(db, monkeypatch):
    """Pre-v1 agents implicitly inherited invoker role; preserve that."""
    monkeypatch.setattr(authz, "_enforce_enabled", AsyncMock(return_value=False))
    agent = AgentActor(
        agent_id="A",
        invoker_user_id="U",
        entity_id="E",
        capabilities=frozenset(),
    )
    decision = await authorize(db, agent, "docs.read")
    assert decision.allow is True
    assert decision.matched_rule == "legacy.agent_inherits_invoker"




# ── Strict mode (flag ON): owner & invariants ────────────────────────────


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(authz, "_enforce_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        authz,
        "_layer2_workspace",
        AsyncMock(return_value=authz._deny("not member", "test.l2")),
    )
    monkeypatch.setattr(
        authz,
        "_layer3_grant",
        AsyncMock(return_value=authz._deny("no grant", "test.l3")),
    )


@pytest.mark.asyncio
async def test_owner_shortcut_bypasses_workspace(db, strict):
    actor = UserActor(user_id="U1", entity_id="E", role="member")
    res = Resource(
        type=ResourceType.DOCUMENT,
        id="D",
        entity_id="E",
        visibility=Visibility.WORKSPACE,
        classification=Classification.CONFIDENTIAL,
        owner_id="U1",
    )
    decision = await authorize(db, actor, Permission.DOCS_READ, res)
    assert decision.allow is True
    assert decision.matched_rule == "owner"


@pytest.mark.asyncio
async def test_invariant_1_restricted_cannot_be_public(db, strict):
    """RFC §13.14 invariant 1."""
    actor = UserActor(user_id="U1", entity_id="E", role="admin")
    res = Resource(
        type=ResourceType.DOCUMENT,
        id="D",
        entity_id="E",
        visibility=Visibility.PUBLIC,
        classification=Classification.RESTRICTED,
        owner_id="UX",
    )
    decision = await authorize(db, actor, Permission.DOCS_READ, res)
    assert decision.allow is False
    assert decision.matched_rule == "inv1"


@pytest.mark.asyncio
async def test_invariant_5_legal_hold_blocks_delete(db, strict):
    """Even admin cannot delete a doc on legal hold."""
    actor = UserActor(user_id="U1", entity_id="E", role="admin")
    res = Resource(
        type=ResourceType.DOCUMENT,
        id="D",
        entity_id="E",
        visibility=Visibility.WORKSPACE,
        classification=Classification.INTERNAL,
        owner_id="UX",
        legal_hold=True,
    )
    decision = await authorize(db, actor, Permission.DOCS_DELETE, res)
    assert decision.allow is False
    assert decision.matched_rule == "inv5"


@pytest.mark.asyncio
async def test_invariant_6_external_share_blocked_above_internal(db, strict):
    """Confidential+ external share requires explicit approval — stub denies."""
    actor = UserActor(user_id="U1", entity_id="E", role="admin")
    res = Resource(
        type=ResourceType.DOCUMENT,
        id="D",
        entity_id="E",
        visibility=Visibility.WORKSPACE,
        classification=Classification.CONFIDENTIAL,
        owner_id="UX",
    )
    decision = await authorize(db, actor, Capability.SHARE_EXTERNAL, res)
    assert decision.allow is False
    assert decision.matched_rule == "inv6"


@pytest.mark.asyncio
async def test_invariant_7_agent_cannot_read_restricted(db, strict):
    agent = AgentActor(
        agent_id="A",
        invoker_user_id="U",
        entity_id="E",
        capabilities=frozenset({Permission.DOCS_READ.value, Capability.VIEW}),
    )
    res = Resource(
        type=ResourceType.DOCUMENT,
        id="D",
        entity_id="E",
        visibility=Visibility.WORKSPACE,
        classification=Classification.RESTRICTED,
        owner_id="UX",
    )
    decision = await authorize(db, agent, Permission.DOCS_READ, res)
    assert decision.allow is False
    assert decision.matched_rule == "inv7.read"


@pytest.mark.asyncio
async def test_invariant_7_agent_cannot_share_external(db, strict):
    agent = AgentActor(
        agent_id="A",
        invoker_user_id="U",
        entity_id="E",
        capabilities=frozenset({Capability.SHARE_EXTERNAL}),
    )
    res = Resource(
        type=ResourceType.DOCUMENT,
        id="D",
        entity_id="E",
        visibility=Visibility.WORKSPACE,
        classification=Classification.INTERNAL,
        owner_id="UX",
    )
    decision = await authorize(db, agent, Capability.SHARE_EXTERNAL, res)
    assert decision.allow is False
    assert decision.matched_rule == "inv7.share"


@pytest.mark.asyncio
async def test_invariant_10_quarantine_hides_from_non_uploader(db, strict):
    actor = UserActor(user_id="U_other", entity_id="E", role="admin")
    res = Resource(
        type=ResourceType.DOCUMENT,
        id="D",
        entity_id="E",
        visibility=Visibility.ENTITY,
        classification=Classification.INTERNAL,
        owner_id="U_uploader",
        quarantine_status="quarantined",
    )
    decision = await authorize(db, actor, Permission.DOCS_READ, res)
    assert decision.allow is False
    assert decision.matched_rule == "inv10"


@pytest.mark.asyncio
async def test_invariant_10_uploader_still_sees_quarantine(db, strict):
    actor = UserActor(user_id="U_uploader", entity_id="E", role="member")
    res = Resource(
        type=ResourceType.DOCUMENT,
        id="D",
        entity_id="E",
        visibility=Visibility.ENTITY,
        classification=Classification.INTERNAL,
        owner_id="U_uploader",
        quarantine_status="pending_scan",
    )
    decision = await authorize(db, actor, Permission.DOCS_READ, res)
    assert decision.allow is True
    assert decision.matched_rule == "owner"


# ── Strict mode: positive paths ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_visibility_entity_allows_same_tenant(db, strict):
    actor = UserActor(user_id="U_other", entity_id="E", role="admin")
    res = Resource(
        type=ResourceType.DOCUMENT,
        id="D",
        entity_id="E",
        visibility=Visibility.ENTITY,
        classification=Classification.INTERNAL,
        owner_id="U_someone",
    )
    decision = await authorize(db, actor, Permission.DOCS_READ, res)
    assert decision.allow is True
    assert decision.matched_rule == "visibility.entity"


@pytest.mark.asyncio
async def test_workspace_membership_grants_access(db, monkeypatch):
    monkeypatch.setattr(authz, "_enforce_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(
        authz,
        "_layer2_workspace",
        AsyncMock(return_value=authz._allow("layer2.workspace.editor")),
    )
    actor = UserActor(user_id="U", entity_id="E", role="member")
    res = Resource(
        type=ResourceType.DOCUMENT,
        id="D",
        entity_id="E",
        workspace_id="W",
        visibility=Visibility.WORKSPACE,
        classification=Classification.INTERNAL,
        owner_id="UX",
    )
    decision = await authorize(db, actor, Permission.DOCS_READ, res)
    assert decision.allow is True
    assert decision.matched_rule == "layer2.workspace.editor"


@pytest.mark.asyncio
async def test_strict_denies_with_no_grant_no_membership(db, strict):
    actor = UserActor(user_id="U_stranger", entity_id="E", role="member")
    res = Resource(
        type=ResourceType.DOCUMENT,
        id="D",
        entity_id="E",
        workspace_id="W",
        visibility=Visibility.WORKSPACE,
        classification=Classification.INTERNAL,
        owner_id="UX",
    )
    decision = await authorize(db, actor, Permission.DOCS_READ, res)
    assert decision.allow is False
    assert "no matching grant" in decision.reason or decision.matched_rule == "test.l3"
