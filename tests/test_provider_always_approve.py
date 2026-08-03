"""Always approve for provider-required approvals.

A provider can demand its own per-action confirmation. Manor records the
operator's standing decision per provider AND action, then answers that gate
on their behalf — the provider keeps demanding, it just stops being the
operator's problem. Production 2026-07-27: the operator approved the same
`Chrome Fill Or Select` five times and nothing published — the card offered no
"Always approve", and after each approval the model improvised a second,
tokenless attempt that raised a fresh gate.

With a standing grant Manor now answers that gate itself — confirm, then retry
with the returned single-use token injected — inside the tool call, so the
model never sees an approval it cannot resolve.

The gate itself is untouched: without a grant, the card flow runs exactly as
before.
"""
from __future__ import annotations

import json

import pytest

from packages.core.ai.runtime import approval_service
from packages.core.ai.runtime.approval_service import (
    _provider_supports_always_approve,
    runtime_auto_confirm_provider_approval,
)

_ARGS = {"ref": "e1", "tabId": 895206222, "value": "Ship it."}


def _approval_required_result(approval_id: str = "approval-1785183097747913000-90e0") -> str:
    return json.dumps({
        "ok": False,
        "status": "approval_required",
        "approval_required": True,
        "provider": "chrome",
        "approvalId": approval_id,
        "expires_at": "2099-07-19T12:00:00Z",
        "target_label": "Post",
        "retry_action": {"name": "mcp__chrome__fill_or_select", "arguments": _ARGS},
    })


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


def _grant(monkeypatch, granted: bool):
    async def fake_grant(db, **kwargs):
        return granted

    monkeypatch.setattr(approval_service, "_provider_standing_grant", fake_grant)

    class _Session:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "packages.core.database.async_session", lambda: _Session(),
    )


def test_every_normalized_provider_supports_always_approve():
    """The operator's authority does not depend on who wrote the gate."""
    assert _provider_supports_always_approve("chrome") is True
    assert _provider_supports_always_approve("linkedin") is True
    assert _provider_supports_always_approve("") is False
    assert _provider_supports_always_approve(None) is False


@pytest.mark.asyncio
async def test_standing_grant_confirms_and_retries_with_the_token(monkeypatch):
    """The fix: one prior 'Always approve' resolves the gate in place."""
    _grant(monkeypatch, True)
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
async def test_without_a_grant_the_card_flow_is_untouched(monkeypatch):
    """No standing grant ⇒ no auto-confirm, no tool calls, original result."""
    _grant(monkeypatch, False)
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
    _grant(monkeypatch, True)
    execute, calls = _recorder()
    plain = json.dumps({"ok": True, "status": "filled"})

    out = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__fill_or_select",
        arguments=_ARGS, result=plain, execute=execute,
        entity_id="ENT", user_id="USR", workspace_id="WS",
    )
    assert out == plain
    assert calls == []


@pytest.mark.asyncio
async def test_confirm_without_a_token_falls_back_to_the_card(monkeypatch):
    """If the provider does not hand back a token there is nothing to retry
    with — surface the approval rather than firing an unauthorized action."""
    _grant(monkeypatch, True)
    execute, calls = _recorder(token=None)
    original = _approval_required_result()

    out = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__fill_or_select",
        arguments=_ARGS, result=original, execute=execute,
        entity_id="ENT", user_id="USR", workspace_id="WS",
    )
    assert [name for name, _ in calls] == ["mcp__chrome__confirm_action"]
    assert out == original


@pytest.mark.asyncio
async def test_executor_failure_falls_back_to_the_card(monkeypatch):
    _grant(monkeypatch, True)

    async def exploding(name: str, args: dict) -> str:
        raise RuntimeError("extension offline")

    original = _approval_required_result()
    out = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__fill_or_select",
        arguments=_ARGS, result=original, execute=exploding,
        entity_id="ENT", user_id="USR", workspace_id="WS",
    )
    assert out == original


@pytest.mark.asyncio
async def test_expired_provider_approval_is_not_auto_confirmed(monkeypatch):
    _grant(monkeypatch, True)
    execute, calls = _recorder()
    expired = json.dumps({
        "ok": False,
        "status": "approval_required",
        "approval_required": True,
        "provider": "chrome",
        "approvalId": "approval-1-abc",
        "expires_at": "2000-01-01T00:00:00Z",
        "retry_action": {"name": "mcp__chrome__fill_or_select", "arguments": _ARGS},
    })

    out = await runtime_auto_confirm_provider_approval(
        tool_name="mcp__chrome__fill_or_select",
        arguments=_ARGS, result=expired, execute=execute,
        entity_id="ENT", user_id="USR", workspace_id="WS",
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
    _grant(monkeypatch, True)
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
        entity_id="ENT", user_id="USR", workspace_id="WS",
    )

    # exactly one confirm + one retry, then stop
    assert calls == ["mcp__chrome__confirm_action", "mcp__chrome__fill_or_select"]
    assert json.loads(out)["status"] == "approval_required"
