"""Always allow persists workspace-level governance auto-approve rules."""

from __future__ import annotations

import asyncio

import packages.core.governance.service as gov
from packages.core.governance.policy import WorkspacePolicy


def test_add_auto_approve_action_appends_and_persists(monkeypatch):
    captured: dict = {}

    async def fake_get_policy(db, workspace_id):
        return WorkspacePolicy(auto_approve_actions=["existing.action"])

    async def fake_update_policy(db, *, entity_id, workspace_id, policy, changed_by=None, change_summary=None):
        captured["policy"] = policy
        captured["workspace_id"] = workspace_id

    monkeypatch.setattr(gov, "get_policy", fake_get_policy)
    monkeypatch.setattr(gov, "update_policy", fake_update_policy)

    added = asyncio.run(
        gov.add_auto_approve_action(
            None,
            entity_id="ent",
            workspace_id="ws",
            action_key="file.write",
            changed_by="user",
        )
    )

    assert added is True
    assert captured["workspace_id"] == "ws"
    assert "file.write" in captured["policy"].auto_approve_actions
    assert "existing.action" in captured["policy"].auto_approve_actions  # preserved


def test_add_auto_approve_action_is_idempotent(monkeypatch):
    called = {"update": False}

    async def fake_get_policy(db, workspace_id):
        return WorkspacePolicy(auto_approve_actions=["file.write"])

    async def fake_update_policy(db, **kwargs):
        called["update"] = True

    monkeypatch.setattr(gov, "get_policy", fake_get_policy)
    monkeypatch.setattr(gov, "update_policy", fake_update_policy)

    added = asyncio.run(
        gov.add_auto_approve_action(
            None,
            entity_id="ent",
            workspace_id="ws",
            action_key="file.write",
            changed_by="user",
        )
    )

    assert added is False
    assert called["update"] is False  # already present → no revision written


def test_add_auto_approve_action_noops_without_action_or_workspace(monkeypatch):
    monkeypatch.setattr(gov, "get_policy", None)  # must not be called
    assert asyncio.run(gov.add_auto_approve_action(None, entity_id="e", workspace_id="", action_key="x")) is False
    assert asyncio.run(gov.add_auto_approve_action(None, entity_id="e", workspace_id="w", action_key="")) is False


def test_add_auto_approve_capability_appends_and_persists(monkeypatch):
    captured: dict = {}

    async def fake_get_policy(db, workspace_id):
        return WorkspacePolicy(auto_approve_capabilities=["existing.capability"])

    async def fake_update_policy(db, *, entity_id, workspace_id, policy, changed_by=None, change_summary=None):
        captured["policy"] = policy
        captured["workspace_id"] = workspace_id
        captured["change_summary"] = change_summary

    monkeypatch.setattr(gov, "get_policy", fake_get_policy)
    monkeypatch.setattr(gov, "update_policy", fake_update_policy)

    added = asyncio.run(
        gov.add_auto_approve_capability(
            None,
            entity_id="ent",
            workspace_id="ws",
            capability_id="file.write",
            changed_by="user",
        )
    )

    assert added is True
    assert captured["workspace_id"] == "ws"
    assert "file.write" in captured["policy"].auto_approve_capabilities
    assert "existing.capability" in captured["policy"].auto_approve_capabilities
    assert captured["change_summary"] == "always-approve capability: file.write"


def test_add_auto_approve_capability_is_idempotent(monkeypatch):
    called = {"update": False}

    async def fake_get_policy(db, workspace_id):
        return WorkspacePolicy(auto_approve_capabilities=["file.write"])

    async def fake_update_policy(db, **kwargs):
        called["update"] = True

    monkeypatch.setattr(gov, "get_policy", fake_get_policy)
    monkeypatch.setattr(gov, "update_policy", fake_update_policy)

    added = asyncio.run(
        gov.add_auto_approve_capability(
            None,
            entity_id="ent",
            workspace_id="ws",
            capability_id="file.write",
            changed_by="user",
        )
    )

    assert added is False
    assert called["update"] is False


def test_governance_hitl_card_offers_always_approve_for_capability(monkeypatch):
    captured: dict = {}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def commit(self):
            captured["committed"] = True

    async def fake_post_message(db, **kwargs):
        captured["message"] = kwargs

    monkeypatch.setattr("packages.core.database.async_session", lambda: FakeSession())
    monkeypatch.setattr("packages.core.workspace_chat.service.post_message", fake_post_message)

    asyncio.run(
        gov.post_hitl_card(
            entity_id="ent",
            workspace_id="ws",
            plan_id="plan",
            step_id="step",
            step_key="generate_pdf",
            kind="subagent",
            action_key=None,
            capability_id="file.write",
            matched_rule="file.write",
            reason="file.write requires approval",
        )
    )

    pending = captured["message"]["pending_action"]
    assert pending["action"] is None
    assert pending["capability_id"] == "file.write"
    assert pending["options"] == ["approve", "always_approve", "reject"]
    assert captured["committed"] is True
