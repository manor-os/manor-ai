"""Provider continuations obey the current unified approval policy.

An exact saved action exception may answer a provider confirmation and retry
with its single-use token. Pending requests, explicit denial and required
human interaction still stop execution. Tests isolate storage and the provider
executor while using the real shared policy service and evaluator.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime import approval_service
from packages.core.ai.runtime.approval_service import (
    _provider_supports_always_approve,
    runtime_auto_confirm_provider_approval,
)
from packages.core.proposals.external_authorization import (
    proposal_youtube_public_request_matches,
    proposal_youtube_public_upload_entry_request_matches,
)

_ARGS = {"ref": "e1", "tabId": 895206222, "value": "Ship it."}


def _approval_required_result(approval_id: str = "approval-1785183097747913000-90e0") -> str:
    return json.dumps(
        {
            "ok": False,
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": approval_id,
            "expires_at": "2099-07-19T12:00:00Z",
            "target_label": "Post",
            "retry_action": {"name": "mcp__chrome__fill_or_select", "arguments": _ARGS},
        }
    )


def _recorder(*, token: str | None = "approval-token-abc"):
    """Stand-in tool executor: records calls, answers confirm with a token."""
    calls: list[tuple[str, dict]] = []

    async def execute(name: str, args: dict) -> str:
        calls.append((name, args))
        if name == "mcp__chrome__confirm_action":
            payload = {"ok": True, "status": "approved"}
            if token:
                payload["approvalToken"] = token
            return json.dumps(payload)
        return json.dumps({"ok": True, "status": "filled"})

    return execute, calls


def _policy(monkeypatch, granted: bool, *, seen: dict | None = None, effect="allow", pending=False):
    from packages.core.governance import service as governance
    from packages.core.governance.approval_policy import ApprovalScope
    from packages.core.governance.approval_policy_service import build_policy_chain
    from packages.core.governance.policy import WorkspacePolicy

    rules = [{"id": "provider-exception", "effect": effect, "selector": {
        "kind": "action", "action_key": "chrome.action"}}] if granted else []
    chain = build_policy_chain(scope=ApprovalScope(kind="workspace", id="WS", entity_id="ENT"),
        root={"revision": 1, "policy": {"mode": "risk_based", "rules": rules}},
        local={"root_entity_id": "ENT", "revision": 1, "policy": {"mode": None, "rules": []}})
    history = [SimpleNamespace(status="pending")] if pending else []
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: history))
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(entity_id="ENT")),
        execute=AsyncMock(return_value=result))
    monkeypatch.setattr(governance, "get_policy", AsyncMock(return_value=WorkspacePolicy()))
    monkeypatch.setattr("packages.core.governance.approval_policy_service.load_policy_chain",
        AsyncMock(return_value=chain))
    if seen is not None:
        original = approval_service._standing_grant
        async def capture_policy_scope(db, **kwargs):
            seen.update(kwargs)
            return await original(db, **kwargs)
        monkeypatch.setattr(approval_service, "_standing_grant", capture_policy_scope)

    class Session:
        async def __aenter__(self): return db
        async def __aexit__(self, *exc): return False
    monkeypatch.setattr("packages.core.database.async_session", Session)


def test_normalized_provider_supports_policy_driven_continuation():
    """The operator's authority does not depend on who wrote the gate."""
    assert _provider_supports_always_approve("chrome") is True
    assert _provider_supports_always_approve("linkedin") is True
    assert _provider_supports_always_approve("") is False
    assert _provider_supports_always_approve(None) is False


def _proposal_authorization() -> dict[str, object]:
    return {
        "version": 1,
        "authorization_id": "proposal-item:publish-task",
        "workspace_id": "workspace-1",
        "task_id": "publish-task",
        "provider": "youtube",
        "action": "publish_video",
        "destination": "studio.youtube.com",
        "visibility": "public",
        "intended_channel": "paired_chrome_signed_in_channel",
        "max_executions": 1,
    }


def _youtube_browser_request(*, target_label: str) -> dict[str, object]:
    return {
        "provider": "chrome",
        "confirmation_mode": "always_action_time",
        "policy_category": "representational_communication",
        "target_label": target_label,
        "retry_tool": "mcp__chrome__click_element",
        "url": "https://studio.youtube.com/channel/demo/videos/upload",
    }


def test_proposal_exact_grant_is_limited_to_bound_youtube_actions():
    authorization = _proposal_authorization()

    assert proposal_youtube_public_request_matches(
        authorization,
        _youtube_browser_request(target_label="Publish"),
        task_id="publish-task",
        workspace_id="workspace-1",
    ) is True
    assert proposal_youtube_public_request_matches(
        authorization,
        {
            **_youtube_browser_request(target_label="Publish"),
            "policy_category": "file_upload",
        },
        task_id="publish-task",
        workspace_id="workspace-1",
    ) is False
    assert proposal_youtube_public_request_matches(
        authorization,
        _youtube_browser_request(target_label="Publish"),
        task_id="another-task",
        workspace_id="workspace-1",
    ) is False


def test_proposal_exact_grant_requires_the_upload_entry_action():
    authorization = _proposal_authorization()

    assert proposal_youtube_public_upload_entry_request_matches(
        authorization,
        _youtube_browser_request(target_label="Upload videos"),
        task_id="publish-task",
        workspace_id="workspace-1",
    ) is True
    assert proposal_youtube_public_upload_entry_request_matches(
        authorization,
        _youtube_browser_request(target_label="Publish"),
        task_id="publish-task",
        workspace_id="workspace-1",
    ) is False
    assert proposal_youtube_public_upload_entry_request_matches(
        authorization,
        {
            **_youtube_browser_request(target_label="Upload videos"),
            "url": "https://evil.example/upload",
        },
        task_id="publish-task",
        workspace_id="workspace-1",
    ) is False


def test_proposal_exact_grant_rejects_unbound_workspace():
    settings = {
        **_proposal_authorization(),
    }

    assert proposal_youtube_public_request_matches(
        settings,
        _youtube_browser_request(target_label="Publish"),
        task_id="publish-task",
        workspace_id="workspace-2",
    ) is False


@pytest.mark.asyncio
async def test_exact_saved_rule_confirms_and_retries_with_the_token(monkeypatch):
    """A saved exact rule permits the provider continuation."""
    _policy(monkeypatch, True)
    execute, calls = _recorder()

    out = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__fill_or_select",
        arguments=_ARGS,
        result=_approval_required_result(),
        execute=execute,
        entity_id="ENT",
        user_id="USR",
        workspace_id="WS",
    )

    assert [name for name, _ in calls] == [
        "mcp__chrome__confirm_action",
        "mcp__chrome__fill_or_select",
    ]
    # the retry carries the single-use token the confirm handed back
    assert calls[1][1]["approvalToken"] == "approval-token-abc"
    assert calls[1][1]["ref"] == "e1"
    # the model sees the completed action, not the approval demand
    assert json.loads(out)["status"] == "filled"


@pytest.mark.asyncio
async def test_exact_saved_rule_retries_the_exact_action_once(monkeypatch):
    """An exact rule cannot alter the provider tab/ref/payload or loop."""
    _policy(monkeypatch, True)
    execute, calls = _recorder()

    payload = json.loads(_approval_required_result())
    payload["confirmation_mode"] = "preapproval_allowed"
    payload["policy_category"] = "youtube_upload_start"
    out = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__fill_or_select",
        arguments={**_ARGS, "approvalToken": "stale-token"},
        result=json.dumps(payload),
        execute=execute,
        entity_id="ENT",
        user_id="USR",
        workspace_id="WS",
    )

    assert json.loads(out)["status"] == "filled"
    assert [name for name, _ in calls] == [
        "mcp__chrome__confirm_action",
        "mcp__chrome__fill_or_select",
    ]
    assert calls[1][1] == {**_ARGS, "approvalToken": "approval-token-abc"}
    assert calls[0][1] == {
        "approvalId": "approval-1785183097747913000-90e0",
        "confirmation_mode": "preapproval_allowed",
        "policy_category": "youtube_upload_start",
        "preapproved": False,
    }


@pytest.mark.asyncio
async def test_canonical_action_scope_reaches_shared_policy(monkeypatch):
    seen: dict = {}
    _policy(monkeypatch, False, seen=seen)
    execute, calls = _recorder()

    payload = json.loads(_approval_required_result())
    payload["action_key"] = "chrome.fill_or_select"
    payload["resource_id"] = "tab:1"
    original = json.dumps(payload)
    out = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__fill_or_select",
        arguments={"ref": "e1", "tabId": 1, "value": "x"},
        result=original,
        execute=execute,
        entity_id="ENT",
        user_id="USR",
        workspace_id="WS",
    )

    assert out == original
    assert calls == []
    assert seen == {
        "entity_id": "ENT",
        "workspace_id": "WS",
        "user_id": "USR",
        "action_key": "chrome.fill_or_select",
        "resource_id": "tab:1",
        "capability_id": "chrome.action",
    }


@pytest.mark.asyncio
async def test_without_an_allow_rule_the_card_flow_is_untouched(monkeypatch):
    """The risk-based mode asks for this unknown provider impact."""
    _policy(monkeypatch, False)
    execute, calls = _recorder()
    original = _approval_required_result()

    out = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__fill_or_select",
        arguments=_ARGS,
        result=original,
        execute=execute,
        entity_id="ENT",
        user_id="USR",
        workspace_id="WS",
    )

    assert calls == []
    assert out == original


@pytest.mark.asyncio
async def test_successful_results_pass_straight_through(monkeypatch):
    _policy(monkeypatch, True)
    execute, calls = _recorder()
    plain = json.dumps({"ok": True, "status": "filled"})

    out = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__fill_or_select",
        arguments=_ARGS,
        result=plain,
        execute=execute,
        entity_id="ENT",
        user_id="USR",
        workspace_id="WS",
    )
    assert out == plain
    assert calls == []


@pytest.mark.asyncio
async def test_confirm_without_a_token_falls_back_to_the_card(monkeypatch):
    """If the provider does not hand back a token there is nothing to retry
    with — surface the approval rather than firing an unauthorized action."""
    _policy(monkeypatch, True)
    execute, calls = _recorder(token=None)
    original = _approval_required_result()

    out = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__fill_or_select",
        arguments=_ARGS,
        result=original,
        execute=execute,
        entity_id="ENT",
        user_id="USR",
        workspace_id="WS",
    )
    assert [name for name, _ in calls] == ["mcp__chrome__confirm_action"]
    assert out == original


@pytest.mark.asyncio
async def test_executor_failure_falls_back_to_the_card(monkeypatch):
    _policy(monkeypatch, True)

    async def exploding(name: str, args: dict) -> str:
        raise RuntimeError("extension offline")

    original = _approval_required_result()
    out = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__fill_or_select",
        arguments=_ARGS,
        result=original,
        execute=exploding,
        entity_id="ENT",
        user_id="USR",
        workspace_id="WS",
    )
    assert out == original


@pytest.mark.asyncio
async def test_expired_provider_approval_is_not_auto_confirmed(monkeypatch):
    _policy(monkeypatch, True)
    execute, calls = _recorder()
    expired = json.dumps(
        {
            "ok": False,
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": "approval-1-abc",
            "expires_at": "2000-01-01T00:00:00Z",
            "retry_action": {"name": "mcp__chrome__fill_or_select", "arguments": _ARGS},
        }
    )

    out = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__fill_or_select",
        arguments=_ARGS,
        result=expired,
        execute=execute,
        entity_id="ENT",
        user_id="USR",
        workspace_id="WS",
    )
    assert calls == []
    assert out == expired


@pytest.mark.asyncio
async def test_a_gated_retry_does_not_recurse(monkeypatch):
    """The retry's own result must not re-enter auto-confirm.

    Feeding it back through the wrapper made a still-gated retry confirm and
    retry again, forever — the run hung rather than failing. One auto-answer
    per tool call; if the retry is still gated, hand it to the card flow.
    """
    _policy(monkeypatch, True)
    calls: list[str] = []

    async def always_gated(name: str, args: dict) -> str:
        calls.append(name)
        if name == "mcp__chrome__confirm_action":
            return json.dumps({"ok": True, "approvalToken": "approval-token-abc"})
        # the retry is gated too — the pathological case
        return _approval_required_result("approval-2-def")

    out = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__fill_or_select",
        arguments=_ARGS,
        result=_approval_required_result(),
        execute=always_gated,
        entity_id="ENT",
        user_id="USR",
        workspace_id="WS",
    )

    # exactly one confirm + one retry, then stop
    assert calls == ["mcp__chrome__confirm_action", "mcp__chrome__fill_or_select"]
    assert json.loads(out)["status"] == "approval_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["pending", "deny", "human"])
async def test_saved_provider_rule_cannot_bypass_existing_or_human_requirements(monkeypatch, blocker):
    _policy(monkeypatch, True, effect="deny" if blocker == "deny" else "allow",
        pending=blocker == "pending")
    execute, calls = _recorder()
    payload = json.loads(_approval_required_result())
    if blocker == "human":
        payload["confirmation_mode"] = "handoff_required"
        payload["policy_category"] = "captcha"
    original = json.dumps(payload)
    out = await runtime_auto_confirm_provider_approval(tool_name="mcp__chrome__fill_or_select",
        arguments=_ARGS, result=original, execute=execute,
        entity_id="ENT", user_id="USR", workspace_id="WS")
    assert calls == []
    assert out == original
