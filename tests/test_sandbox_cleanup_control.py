"""Sandbox teardown is scoped runtime control, never a user deletion approval."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
from packages.core.ai.tools import sandbox_tools


@pytest.mark.parametrize("name,args", [
    ("sandbox", {"action": "destroy", "params": {"sandbox_id": "owned"}}),
    ("sandbox", {"action": "destroy", "sandbox_id": "owned"}),
    ("sandbox_destroy", {"sandbox_id": "owned"}),
])
def test_cleanup_has_no_approval_action_but_still_requires_sandbox_authorization(name, args):
    result = classify_runtime_tool(name, args)
    assert result.effect is RuntimeToolEffect.CONTROL
    assert result.action is None
    assert result.authorization.capability_id == "sandbox.execute"
    assert result.authorization.access.value == "control"


def test_cleanup_exception_does_not_relax_execution_or_user_deletions():
    from tests.approval_policy_test_utils import evaluate_runtime_action

    execution = classify_runtime_tool("sandbox", {"action": "exec", "params": {"command": "python script.py"}})
    assert execution.effect is RuntimeToolEffect.ACTION
    deletion = classify_runtime_tool("manor", {"action": "delete_document", "params": {"document_id": "doc"}})
    assert deletion.effect is RuntimeToolEffect.ACTION
    assert evaluate_runtime_action(deletion.action).outcome == "ask"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["sandbox_id", "entity_id", "user_id", "agent_id"])
async def test_cleanup_rejects_foreign_owner_before_runner_io(monkeypatch, changed):
    context = {"sandbox_id": "owned", "entity_id": "entity", "user_id": "user", "agent_id": "agent"}
    context[changed] = "foreign"
    monkeypatch.setattr(sandbox_tools, "_load_ctx", AsyncMock(return_value=context))
    client = AsyncMock(side_effect=AssertionError("Foreign cleanup reached runner"))
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", client)
    release = AsyncMock(side_effect=AssertionError("Foreign cleanup reached the session service"))
    monkeypatch.setattr(sandbox_tools, "release_sandbox_session", release)
    result = json.loads(await sandbox_tools._sandbox_destroy(
        entity_id="entity", user_id="user", conversation_id="conversation", sandbox_id="owned", _agent_id_from_context="agent",
    ))
    assert result["error"]["code"] == "sandbox_access_denied"
    client.assert_not_called()
    release.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_without_a_binding_is_an_idempotent_release(monkeypatch):
    """No binding for this scope means the sandbox was already released:
    releasing twice is benign and still never reaches the runner."""
    monkeypatch.setattr(sandbox_tools, "_load_ctx", AsyncMock(return_value=None))
    client = AsyncMock(side_effect=AssertionError("Cleanup without a binding reached runner"))
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", client)
    release = AsyncMock(side_effect=AssertionError("Cleanup without a binding reached the session service"))
    monkeypatch.setattr(sandbox_tools, "release_sandbox_session", release)
    result = await sandbox_tools._sandbox_destroy(
        entity_id="entity", user_id="user", conversation_id="conversation", sandbox_id="owned", _agent_id_from_context="agent",
    )
    assert "already released" in result
    client.assert_not_called()
    release.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_releases_owned_runtime_resources(monkeypatch):
    """Cleanup hands the owned sandbox to the owner-session release: the
    binding is dropped and the container stays warm for the same owner (the
    session service, not the tool, decides whether to destroy it)."""
    context = {"sandbox_id": "owned", "entity_id": "entity", "user_id": "user", "agent_id": "agent"}
    monkeypatch.setattr(sandbox_tools, "_load_ctx", AsyncMock(return_value=context))
    client = AsyncMock(side_effect=AssertionError("The tool must not talk to the runner directly"))
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", client)
    release = AsyncMock(return_value="kept")
    monkeypatch.setattr(sandbox_tools, "release_sandbox_session", release)
    result = await sandbox_tools._sandbox_destroy(
        entity_id="entity", user_id="user", conversation_id="conversation", sandbox_id="owned", _agent_id_from_context="agent",
    )
    assert "released for reuse" in result
    client.assert_not_called()
    release.assert_awaited_once()
    kwargs = release.await_args.kwargs
    assert kwargs["sandbox_id"] == "owned"
    assert kwargs["conversation_id"] == "conversation"
    assert kwargs["owner"].entity_id == "entity" and kwargs["owner"].user_id == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [False, True])
async def test_runtime_cleanup_does_not_create_hitl_but_still_checks_permission(db_session, monkeypatch, workspace):
    from sqlalchemy import select

    from packages.core import database
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.user import User
    from tests.test_runtime_guard_unified_store import _fixture, _guard, _SessionContext

    ids = await _fixture(db_session, with_workspace=workspace)
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db_session))
    arguments = {"action": "destroy", "params": {"sandbox_id": "owned"}}
    assert await _guard(ids, arguments, name="sandbox") is None
    assert not (await db_session.scalars(select(HitlRequest).where(HitlRequest.entity_id == ids["entity_id"]))).all()

    actor = await db_session.get(User, ids["user_id"])
    actor.status = "inactive"
    await db_session.flush()
    result = json.loads(await _guard(ids, arguments, name="sandbox"))
    assert result["error"] == "blocked_by_permission"
