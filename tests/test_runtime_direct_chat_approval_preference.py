"""Direct Chat's explicit ask preference must gate otherwise allowed writes.

The browser PMS test caught an ignored preference: the real maintenance write
completed even though the owner had saved ask-each-time. These tests retain
the real actor authorization, persisted preferences and unified HITL lifecycle.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import pytest
from sqlalchemy import select

from packages.core import database
from tests.approval_settings_test_utils import (
    action_rule, set_personal_test_approval, set_workspace_test_approval,
)
from packages.core.ai.runtime.approval_service import (
    guard_runtime_tool_action,
    resolve_runtime_approval_message,
)
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.constants.runtime_principal import RuntimePrincipalKind
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.user import User
from tests.test_runtime_guard_unified_store import (
    _SessionContext,
    _fixture,
    _runtime_envelope,
)


WRITE = "mcp__manor_pms__create_maintenance_request"
ACTION = "manor_pms.create_maintenance_request"
READ = "mcp__manor_pms__list_properties"
ARGS = {"property_id": 1, "title": "Leaking tap", "description": "Bathroom tap", "category": "plumbing"}


async def _setup(db, monkeypatch, mode="ask", *, with_workspace=False, action=ACTION):
    monkeypatch.setattr(database, "async_session", lambda: _SessionContext(db))
    ids = await _fixture(db, with_workspace=with_workspace)
    setter = set_workspace_test_approval if with_workspace else set_personal_test_approval
    scope = {"workspace_id": ids["workspace_id"]} if with_workspace else {}
    await setter(db, entity_id=ids["entity_id"], actor_id=ids["user_id"],
        mode="risk_based", user_rules=[action_rule(action, effect=mode)] if mode else [], **scope)
    await db.commit()
    return ids


async def _guard(ids, arguments=None, *, name=WRITE, operation="primary"):
    envelope = _runtime_envelope(ids, name=name)
    if not ids["workspace_id"]:
        envelope = replace(
            envelope,
            surface=ChatSurface.GLOBAL_OWNER_CHAT,
            profile=RuntimeProfile.OWNER_COPILOT,
            principal=replace(envelope.principal, kind=RuntimePrincipalKind.OWNER),
        )
    return await guard_runtime_tool_action(
        name=name, arguments=dict(ARGS if arguments is None else arguments),
        **ids, runtime_envelope=envelope,
        operation_id=f"test:{ids['conversation_id']}:{operation}",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "allow", "deny", "ask"])
async def test_direct_chat_medium_write_respects_persisted_preference(db_session, monkeypatch, mode):
    ids = await _setup(db_session, monkeypatch, mode)
    result = await _guard(ids)
    if mode == "allow":
        assert result is None
    elif mode == "deny":
        assert json.loads(result)["error"] == "blocked_by_governance"
    else:
        payload = json.loads(result or "{}")
        assert payload.get("__hitl__") is True, payload
        request = await db_session.get(HitlRequest, payload["approval_token"])
        assert request.status == "pending"
        assert request.action_key == ACTION
        assert request.origin_conversation_id == ids["conversation_id"]
        assert request.context["policy_evaluation"]["outcome"] == "ask"


@pytest.mark.asyncio
async def test_direct_chat_ask_reuses_pending_then_consumes_approval_once(db_session, monkeypatch):
    ids = await _setup(db_session, monkeypatch)
    first = json.loads(await _guard(ids) or "{}")
    assert first.get("__hitl__") is True, first
    token = first["approval_token"]
    assert json.loads(await _guard(ids))["approval_token"] == token
    result = await resolve_runtime_approval_message(
        db_session, conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], hitl_id=token, action="approve",
    )
    assert result and "Retry the exact same tool call" in result
    assert await _guard(ids, {**ARGS, "approval_token": token}) is None
    row = await db_session.get(HitlRequest, token)
    await db_session.refresh(row)
    assert row.status == "consumed"
    next_request = json.loads(await _guard(ids, operation="new-intent") or "{}")
    assert next_request.get("__hitl__") is True, next_request
    assert next_request["approval_token"] != token


@pytest.mark.asyncio
async def test_direct_chat_rejection_and_retired_always_preserve_exact_pending_request(db_session, monkeypatch):
    ids = await _setup(db_session, monkeypatch)
    first = json.loads(await _guard(ids))
    rejected = await resolve_runtime_approval_message(db_session,
        conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], hitl_id=first["approval_token"], action="reject")
    assert rejected and "rejected" in rejected
    fresh = json.loads(await _guard(ids, operation="new-intent"))
    assert fresh["__hitl__"] is True
    assert fresh["approval_token"] != first["approval_token"]
    with pytest.raises(ValueError, match="[Aa]lways|approval.settings"):
        await resolve_runtime_approval_message(db_session,
            conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
            user_id=ids["user_id"], hitl_id=fresh["approval_token"], action="always_approve")
    assert (await db_session.get(HitlRequest, fresh["approval_token"])).status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ask", "deny"])
async def test_direct_chat_exact_rules_apply_to_reads_as_well_as_writes(db_session, monkeypatch, mode):
    ids = await _setup(db_session, monkeypatch, mode, action=f"tool.{READ}.read")
    result = json.loads(await _guard(ids, {}, name=READ) or "{}")
    assert result.get("error") == ("blocked_by_governance" if mode == "deny" else "approval_required")


@pytest.mark.asyncio
async def test_direct_chat_preference_does_not_replace_workspace_governance(db_session, monkeypatch):
    ids = await _setup(db_session, monkeypatch, with_workspace=True)
    assert json.loads(await _guard(ids))["error"] == "approval_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ask", "allow"])
async def test_direct_chat_preference_cannot_bypass_revoked_actor(db_session, monkeypatch, mode):
    ids = await _setup(db_session, monkeypatch, mode)
    user = await db_session.get(User, ids["user_id"])
    user.status = "inactive"
    await db_session.commit()
    result = json.loads(await _guard(ids) or "{}")
    assert result.get("error") == "blocked_by_permission", result
    assert not result.get("__hitl__")




@pytest.mark.asyncio
async def test_chrome_final_publication_ignores_automated_runtime_preauthorization(db_session, monkeypatch):
    from packages.core.ai.runtime import approval_service

    name = "mcp__chrome__click_element"
    ids = await _setup(db_session, monkeypatch, mode=None, with_workspace=True)
    envelope = _runtime_envelope(ids, name=name)

    monkeypatch.setattr(approval_service, "_is_automated_runtime_surface", lambda _: True)
    async def current_binding(*_args, **_kwargs):
        return {"connection_id": "browser-one", "credential_revision": "revision-one",
                "connection_epoch": "epoch-one", "documents": []}
    monkeypatch.setattr(approval_service, "_chrome_publication_binding", current_binding)
    args = {
        "tabId": 42, "ref": "e17", "snapshot_id": "snap-current", "integration_account_id": "account-one",
        "_workflow_run_id_from_context": "workflow-run",
        "operation_intent": {
            "effect": "publish",
            "publication": {
                "destination_url": "https://example.test/compose",
                "account": "test-author", "text": "Complete post text", "document_ids": [],
            },
        },
    }
    result = json.loads(await guard_runtime_tool_action(
        operation_id=f"test:{ids['conversation_id']}:primary",
        name=name, arguments=args, **ids, runtime_envelope=envelope,
    ) or "{}")
    assert result.get("__hitl__") is True, result
    assert result["hitl"]["action"] == "chrome.publication.publish"


@pytest.mark.asyncio
async def test_chrome_publication_without_review_scope_cannot_create_approval(db_session, monkeypatch):
    name = "mcp__chrome__click_element"
    ids = await _setup(db_session, monkeypatch, "allow", action="chrome.publication.publish")
    envelope = _runtime_envelope(ids, name=name)
    result = json.loads(await guard_runtime_tool_action(
        operation_id=f"test:{ids['conversation_id']}:primary",
        name=name,
        arguments={"tabId": 42, "ref": "e17", "operation_intent": {"effect": "publish"}},
        **ids, runtime_envelope=envelope,
    ) or "{}")
    assert result["error"] == "invalid_publication_intent"
    assert not result.get("__hitl__")


@pytest.mark.asyncio
async def test_chrome_publication_without_browser_binding_cannot_create_approval(db_session, monkeypatch):
    name = "mcp__chrome__click_element"
    ids = await _setup(db_session, monkeypatch, "allow", action="chrome.publication.publish")
    result = json.loads(await guard_runtime_tool_action(
        operation_id=f"test:{ids['conversation_id']}:primary",
        name=name, arguments={"tabId": 42, "ref": "e17", "operation_intent": {
            "effect": "publish", "publication": {
                "destination_url": "https://example.test/compose", "account": "test-author",
                "text": "Complete post text", "document_ids": [],
            },
        }}, **ids, runtime_envelope=_runtime_envelope(ids, name=name),
    ) or "{}")
    assert not result.get("__hitl__")
    assert result["error"] == "approval_continuation_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["The complete post text shown for approval.", "", "Long post " * 500],
                         ids=["text", "attachment-only", "long-text"])
async def test_chrome_publication_card_shows_exact_review_scope(db_session, monkeypatch, text):
    from packages.core.ai.runtime import approval_service

    async def current_binding(*_args, **_kwargs):
        return {"connection_id": "browser-one", "credential_revision": "revision-one",
                "connection_epoch": "epoch-one", "documents": []}
    monkeypatch.setattr(approval_service, "_chrome_publication_binding", current_binding)
    name = "mcp__chrome__click_element"
    ids = await _setup(db_session, monkeypatch, "ask", action="chrome.publication.publish")
    result = json.loads(await guard_runtime_tool_action(
        operation_id=f"test:{ids['conversation_id']}:primary",
        name=name,
        arguments={
            "expect": {"kind": "page_state", "state": {"text": "Video published"}},
            "tabId": 42, "ref": "e17", "integration_account_id": "account-one", "operation_intent": {
                "effect": "publish", "publication": {
                    "destination_url": "https://example.test/compose",
                    "account": "test-author", "text": text, "document_ids": [],
                },
            },
        },
        **ids, runtime_envelope=_runtime_envelope(ids, name=name),
    ) or "{}")
    assert result["hitl"]["review"] == {
        "destination_url": "https://example.test/compose",
        "account": "test-author", "text": text, "document_ids": [], "documents": [],
    }
    assert result["hitl"]["content"] == text.strip()[:1800].strip()
    assert "Video published" not in result["hitl"]["prompt"]
    if text:
        assert text[:100] in result["hitl"]["prompt"]




@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["connection_id", "connection_epoch", "credential_revision", "documents"])
async def test_chrome_publication_resume_rejects_changed_browser_or_file_binding(db_session, monkeypatch, changed_field):
    from packages.core.ai.runtime import approval_service

    name = "mcp__chrome__click_element"
    ids = await _setup(db_session, monkeypatch, "ask", action="chrome.publication.publish")
    binding = [{
        "connection_id": "browser-one", "credential_revision": "revision-one",
        "connection_epoch": "epoch-one", "documents": [{"document_id": "doc-one", "sha256": "original"}],
    }]

    async def current_binding(*_args, **_kwargs):
        return binding[0]

    monkeypatch.setattr(approval_service, "_chrome_publication_binding", current_binding)
    args = {
        "tabId": 42, "ref": "e17", "integration_account_id": "account-one",
        "operation_intent": {"effect": "publish", "publication": {
            "destination_url": "https://example.test/compose", "account": "test-author",
            "text": "Complete post text", "document_ids": ["doc-one"],
        }},
    }
    envelope = _runtime_envelope(ids, name=name)
    first = json.loads(await guard_runtime_tool_action(
        operation_id=f"test:{ids['conversation_id']}:primary",
        name=name, arguments=args, **ids, runtime_envelope=envelope,
    ) or "{}")
    assert first["hitl"]["review"]["documents"] == binding[0]["documents"]
    token = first["approval_token"]
    assert await resolve_runtime_approval_message(
        db_session, conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], hitl_id=token, action="approve",
    )
    binding[0] = {**binding[0], changed_field: (
        [{"document_id": "doc-one", "sha256": "changed"}] if changed_field == "documents" else "changed"
    )}
    blocked = json.loads(await guard_runtime_tool_action(
        operation_id=f"test:{ids['conversation_id']}:primary",
        name=name, arguments={**args, "approval_token": token},
        **ids, runtime_envelope=envelope,
    ) or "{}")
    assert blocked["error"] == "approval_payload_changed"
    row = await db_session.get(HitlRequest, token)
    assert row.status == "expired"






@pytest.mark.asyncio
async def test_rejected_chrome_publication_cannot_switch_to_keyboard_without_new_review(db_session, monkeypatch):
    from packages.core.ai.runtime import approval_service

    async def current_binding(*_args, **_kwargs):
        return {"connection_id": "browser-one", "credential_revision": "revision-one",
                "connection_epoch": "epoch-one", "documents": []}

    monkeypatch.setattr(approval_service, "_chrome_publication_binding", current_binding)
    ids = await _setup(db_session, monkeypatch, "ask", action="chrome.publication.publish")
    publication = {"effect": "publish", "publication": {
        "destination_url": "https://example.test/compose", "account": "test-author",
        "text": "Complete post text", "document_ids": [],
    }}
    click_name = "mcp__chrome__click_element"
    first = json.loads(await guard_runtime_tool_action(
        operation_id=f"test:{ids['conversation_id']}:primary",
        name=click_name, arguments={"ref": "e17", "operation_intent": publication},
        **ids, runtime_envelope=_runtime_envelope(ids, name=click_name),
    ) or "{}")
    assert first["__hitl__"] is True
    assert await resolve_runtime_approval_message(
        db_session, conversation_id=ids["conversation_id"], entity_id=ids["entity_id"],
        user_id=ids["user_id"], hitl_id=first["approval_token"], action="reject",
    )

    keyboard_name = "mcp__chrome__press_key"
    second = json.loads(await guard_runtime_tool_action(
        operation_id=f"test:{ids['conversation_id']}:keyboard-intent",
        name=keyboard_name,
        arguments={"key": "Enter", "fallbackReason": "Focused composer", "operation_intent": publication},
        **ids, runtime_envelope=_runtime_envelope(ids, name=keyboard_name),
    ) or "{}")
    assert second["__hitl__"] is True
    assert second["hitl"]["action"] == "chrome.publication.publish"
    assert second["approval_token"] != first["approval_token"]
