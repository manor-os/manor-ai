from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from packages.core.ai.runtime.chrome_run import (
    ChromeSkillOutcome,
    ChromeSkillRunState,
    chrome_outcome_for_result,
    commit_chrome_runtime_state,
    chrome_runtime_state_for_child,
)
from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.principals import RuntimePrincipal, RuntimePrincipalKind
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.skills import (
    _prompt_skill_runtime_envelope,
    runtime_format_invoke_skill_result,
)
from packages.core.ai.runtime.skill_capability_companion import (
    trusted_skill_capability_companion,
)
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.services.skill_service import invoke_skill






def _envelope(metadata: dict) -> RuntimeEnvelope:
    return RuntimeEnvelope(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        principal=RuntimePrincipal(
            kind=RuntimePrincipalKind.OWNER,
            entity_id="ent-1",
            actor_user_id="user-1",
            execution_user_id="user-1",
        ),
        profile=RuntimeProfile.OWNER_COPILOT,
        entity_id="ent-1",
        user_id="user-1",
        conversation_id="conv-1",
        metadata=metadata,
    )


def _tool_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_chrome_skill_outcome_serializes_resumable_run_without_ephemeral_refs() -> None:
    state = ChromeSkillRunState(
        run_id="chrome-run-1",
        status="needs_approval",
        goal="发布当前草稿",
        browser_group={"group_id": "group-1"},
        control_epoch="epoch-1",
        pending_action={
            "action_signature": "sig-1",
            "target_fingerprint": {"role": "button", "name": "发布"},
            "approval_id": "approval-1",
            "approvalId": "approval-2",
            "provider_approval_id": "approval-3",
        },
        resume_context={"next": "confirm_action_then_retry_once"},
    )
    outcome = ChromeSkillOutcome.from_state(
        state,
        summary="等待发布确认",
        evidence=[{"kind": "page_state", "url": "https://example.test/editor"}],
    )

    payload = outcome.to_dict()

    assert payload["status"] == "needs_approval"
    assert payload["run_id"] == "chrome-run-1"
    assert "approval_id" not in payload["pending_action"]
    assert "approvalId" not in payload["pending_action"]
    assert "provider_approval_id" not in payload["pending_action"]
    assert "ref" not in json.dumps(payload)
    assert "snapshot_id" not in json.dumps(payload)


def test_prompt_skill_child_envelope_preserves_chrome_runtime_state() -> None:
    envelope = _envelope(
        {
            "chrome_runtime_contract_v1": {
                "run_id": "chrome-run-1",
                "latest_snapshot_id": "snapshot-1",
                "control_epoch": "epoch-1",
            }
        }
    )

    child = _prompt_skill_runtime_envelope(
        envelope,
        allowed_tool_names={"mcp__chrome__read_page"},
        skill_tool_names={"mcp__chrome__read_page"},
    )

    assert child is not None
    assert child.metadata["chrome_runtime_contract_v1"]["run_id"] == "chrome-run-1"
    assert child.metadata["chrome_runtime_contract_v1"]["latest_snapshot_id"] == "snapshot-1"


@pytest.mark.asyncio
async def test_second_fresh_chrome_skill_invocation_reloads_discovered_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed Chrome Skill handoff does not terminate a later invocation."""

    skill = SimpleNamespace(
        id="chrome-skill",
        entity_id=None,
        name="chrome",
        slug="chrome",
        status="active",
        system_prompt="Use Chrome tools.",
        tools=["search_tools", "mcp__chrome__finalize_tabs"],
        config={"discoverable_tool_prefixes": ["mcp__chrome__"]},
    )
    parent = _envelope({
        "chrome_runtime_contract_v1": {
            "run_id": "chrome-run-invocations",
            "group_id": "group-invocations",
            "control_epoch": "epoch-invocations",
            "chrome_policy_connection_id": "connection-invocations",
            "action_receipts": {"existing": {"receipt": {"status": "verified"}}},
        }
    })
    invocation = 0
    seen_tool_names: list[list[str]] = []

    async def fake_get_skill(_db, _skill_id):
        return skill

    async def fake_eligible(*_args, **_kwargs):
        return True, None

    async def fake_release(_db):
        return None

    async def fake_skill_loop(**kwargs):
        nonlocal invocation
        invocation += 1
        current_invocation = invocation
        rounds = 0
        child_metadata = kwargs["runtime_envelope"].metadata

        async def completion(_messages, tools, **_kwargs):
            nonlocal rounds
            rounds += 1
            names = [schema["function"]["name"] for schema in tools]
            seen_tool_names.append(names)
            if current_invocation == 1 and rounds == 1:
                assert "mcp__chrome__finalize_tabs" in names
                return "", [{"id": "finish", "name": "mcp__chrome__finalize_tabs", "arguments": {}}], {"total": 1}
            if current_invocation == 2 and rounds == 1:
                assert "mcp__chrome__finalize_tabs" in names
                return "", [{"id": "discover", "name": "search_tools", "arguments": {"query": "read chrome page"}}], {"total": 1}
            if current_invocation == 2 and rounds == 2:
                assert "mcp__chrome__read_page" in names
                return "", [{"id": "read", "name": "mcp__chrome__read_page", "arguments": {}}], {"total": 1}
            return "done", None, {"total": 1}

        async def executor(name, _arguments):
            if name == "mcp__chrome__finalize_tabs":
                state = child_metadata.setdefault("chrome_runtime_contract_v1", {})
                state["finalized"] = True
                return json.dumps({"ok": True, "status": "finalized"})
            if name == "search_tools":
                return json.dumps({
                    "loaded_tools": ["mcp__chrome__read_page"],
                    "matches": [{
                        "name": "mcp__chrome__read_page",
                        "available": True,
                        "schema": _tool_schema("mcp__chrome__read_page"),
                    }],
                })
            if name == "mcp__chrome__read_page":
                return json.dumps({"ok": True, "url": "https://example.test"})
            raise AssertionError(f"Unexpected tool: {name}")

        from packages.core.ai.agentic_loop import agentic_loop

        with patch(
            "packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
            completion,
        ):
            return await agentic_loop(
                system_prompt=kwargs["system_prompt"],
                user_message=kwargs["user_message"],
                tools=kwargs["tools"],
                tool_executor=executor,
                tool_schema_resolver=lambda name: _tool_schema(name),
                metadata=child_metadata,
                max_rounds=4,
            )

    monkeypatch.setattr("packages.core.services.skill_service.get_skill", fake_get_skill)
    monkeypatch.setattr("packages.core.ai.runtime.skills.runtime_skill_is_eligible", fake_eligible)
    monkeypatch.setattr("packages.core.services.skill_service.release_read_transaction", fake_release)
    monkeypatch.setattr("packages.core.services.skill_service._load_prompt_skill_extra_files", lambda *_args: [])
    monkeypatch.setattr("packages.core.ai.runtime.runtime_execute_skill_agent_loop", fake_skill_loop)
    # This exercises the shared nested-loop contract with a simulated provider.
    # The exported tree deliberately has no built-in Chrome provider catalog.
    provider_tools = {"search_tools", "mcp__chrome__finalize_tabs", "mcp__chrome__read_page"}
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_registry.runtime_registered_tool_names",
        lambda **_kwargs: provider_tools,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_registry.runtime_tool_schemas_for_names",
        lambda names: [_tool_schema(name) for name in names if name in provider_tools],
    )

    first = await invoke_skill(
        MagicMock(), "chrome", "entity-1", "first browser task",
        user_id="user-1", runtime_envelope=parent,
        allowed_tool_names={"search_tools", "mcp__chrome__finalize_tabs", "mcp__chrome__read_page"},
    )
    second = await invoke_skill(
        MagicMock(), "chrome", "entity-1", "second browser task",
        user_id="user-1", runtime_envelope=parent,
        allowed_tool_names={"search_tools", "mcp__chrome__finalize_tabs", "mcp__chrome__read_page"},
    )

    assert "chrome_outcome" in first, first
    assert first["chrome_outcome"]["evidence"][0]["tabs_finalized"] is True
    assert second["chrome_outcome"]["evidence"][0]["tabs_finalized"] is False
    assert invocation == 2
    assert any("mcp__chrome__read_page" in names for names in seen_tool_names)
    state = parent.metadata["chrome_runtime_contract_v1"]
    assert state["group_id"] == "group-invocations"
    assert state["control_epoch"] == "epoch-invocations"
    assert state["chrome_policy_connection_id"] == "connection-invocations"
    assert state["action_receipts"] == {"existing": {"receipt": {"status": "verified"}}}


@pytest.mark.asyncio
@pytest.mark.parametrize(("resume_kwargs", "keeps_finalized"), [
    pytest.param({"runtime_checkpoint": {"messages": []}}, True, id="runtime-checkpoint"),
    pytest.param({"hitl_resume_checkpoint": True}, False, id="checkpoint-capture"),
])
async def test_chrome_skill_only_checkpoint_resume_keeps_finalized_state(
    monkeypatch: pytest.MonkeyPatch,
    resume_kwargs: dict,
    keeps_finalized: bool,
) -> None:
    skill = SimpleNamespace(
        id="chrome-resume-skill", entity_id=None, name="chrome", slug="chrome",
        status="active", system_prompt="Use Chrome.", tools=[], config={},
    )
    parent = _envelope({"chrome_runtime_contract_v1": {
        "run_id": "chrome-resume", "finalized": True, "status": "completed",
        "control_epoch": "epoch-resume", "control_interruption": {"kind": "pause"},
    }})
    observed: list[dict] = []

    async def fake_loop(**kwargs):
        observed.append(dict(kwargs["runtime_envelope"].metadata["chrome_runtime_contract_v1"]))
        return SimpleNamespace(
            content="paused", usage={}, tool_calls_made=[], rounds=0,
            messages=[], stop_reason="completed", error=None, control={}, tool_evidence={},
        )

    monkeypatch.setattr("packages.core.services.skill_service.get_skill", AsyncMock(return_value=skill))
    monkeypatch.setattr("packages.core.ai.runtime.skills.runtime_skill_is_eligible", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr("packages.core.services.skill_service.release_read_transaction", AsyncMock())
    monkeypatch.setattr("packages.core.services.skill_service._load_prompt_skill_extra_files", lambda *_args: [])
    monkeypatch.setattr("packages.core.ai.runtime.runtime_execute_skill_agent_loop", fake_loop)

    await invoke_skill(
        MagicMock(), "chrome", "entity-1", "resume browser task", user_id="user-1",
        runtime_envelope=parent,
        allowed_tool_names={"mcp__chrome__read_page"},
        **resume_kwargs,
    )

    assert ("finalized" in observed[0]) is keeps_finalized
    assert observed[0].get("status") == ("completed" if keeps_finalized else None)
    assert observed[0]["control_interruption"] == {"kind": "pause"}


@pytest.mark.asyncio
async def test_nested_chrome_skill_resume_is_not_a_fresh_task() -> None:
    from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
    from packages.core.ai.runtime.nested_agent import (
        runtime_nested_checkpoint_scope,
        runtime_nested_skill_resume_active,
        runtime_nested_skill_scope,
    )
    from packages.core.ai.runtime.tool_effect_intents import runtime_tool_effect_checkpoint_scope

    parent = RuntimeAgentCheckpoint(
        messages=[], usage={}, rounds=1, tool_calls_made=[],
        pending_tool_call={"id": "invoke-chrome", "name": "invoke_skill", "arguments": {"skill": "chrome"}},
    ).to_dict()
    saved: list[dict] = []

    async def persist(tree, _messages):
        saved.append(tree)

    with (
        runtime_nested_checkpoint_scope(persist),
        runtime_tool_effect_checkpoint_scope({"parent": parent}),
        runtime_nested_skill_scope("chrome-skill") as frame,
    ):
        await frame.on_round_end(1, [], {}, [], {})

    with (
        runtime_nested_checkpoint_scope(persist),
        runtime_tool_effect_checkpoint_scope({"parent": saved[-1]}),
    ):
        assert runtime_nested_skill_resume_active() is True


@pytest.mark.asyncio
async def test_parent_failure_retains_completed_child_evidence_without_settling_effect():
    from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
    from packages.core.ai.runtime.nested_agent import runtime_execute_with_nested_approval_checkpoints, runtime_nested_skill_scope
    from packages.core.ai.runtime.tool_effect_intents import runtime_tool_effect_checkpoint_scope

    parent = RuntimeAgentCheckpoint(messages=[], usage={}, rounds=1, tool_calls_made=[],
        pending_tool_call={"id": "invoke-child", "name": "invoke_skill", "arguments": {"skill": "chrome"}}).to_dict()

    async def fail_after_child():
        with runtime_tool_effect_checkpoint_scope({"parent": parent}), runtime_nested_skill_scope("chrome-skill") as frame:
            await frame.complete({"skill": "chrome", "content": "Existing draft: draft-42",
                "chrome_outcome": {"status": "completed", "artifacts": [{"document_id": "draft-proof"}]},
                "stop_reason": "completed"}, [])
        return SimpleNamespace(stop_reason="error", control={"effect_state": "unconfirmed", "do_not_replay": True})

    result = await runtime_execute_with_nested_approval_checkpoints(fail_after_child)
    assert result.control["effect_state"] == "unconfirmed" and result.control["do_not_replay"] is True
    evidence = result.control["child_skill_results"][0]
    assert evidence["tool_call_id"] == "invoke-child"
    assert evidence["result"]["chrome_outcome"]["artifacts"] == [{"document_id": "draft-proof"}]
    assert evidence["result"]["content"] == "Existing draft: draft-42"


def test_runtime_format_invoke_skill_result_returns_typed_chrome_outcome() -> None:
    result = runtime_format_invoke_skill_result(
        "chrome",
        {
            "skill": "chrome",
            "chrome_outcome": {
                "status": "needs_user",
                "run_id": "chrome-run-2",
                "summary": "需要用户登录",
                "resume_context": {"next": "reobserve"},
            },
        },
    )

    payload = json.loads(result)

    assert payload["status"] == "needs_user"
    assert payload["skill"] == "chrome"
    assert payload["run_id"] == "chrome-run-2"
    assert payload["resume_context"]["next"] == "reobserve"


def test_runtime_format_chrome_outcome_preserves_terminal_harness_control() -> None:
    result = runtime_format_invoke_skill_result(
        "chrome",
        {
            "skill": "chrome",
            "stop_reason": "chrome_cli_worker_not_paired",
            "stop_parent": True,
            "notice_key": "chrome_unavailable",
            "replace_visible_text": True,
            "control": {"stop_parent": True, "source_tool": "mcp__chrome__read_page"},
            "chrome_outcome": {"status": "failed", "run_id": "chrome-run-terminal"},
        },
    )

    payload = json.loads(result)

    assert payload["run_id"] == "chrome-run-terminal"
    assert payload["stop_parent"] is True
    assert payload["stop_reason"] == "chrome_cli_worker_not_paired"
    assert payload["notice_key"] == "chrome_unavailable"
    assert payload["control"]["source_tool"] == "mcp__chrome__read_page"


def test_runtime_format_chrome_outcome_does_not_propagate_capability_blocker() -> None:
    result = runtime_format_invoke_skill_result(
        "chrome",
        {
            "skill": "chrome",
            "stop_reason": "completed",
            "stop_parent": False,
            "control": {
                "kind": "tool_error",
                "blocked_capability": "chrome",
                "error_reason": "chrome_cli_worker_not_paired",
                "setup_url": "/integrations?provider=chrome",
            },
            "chrome_outcome": {
                "status": "blocked",
                "run_id": "chrome-run-blocked",
            },
        },
    )

    payload = json.loads(result)

    assert payload["status"] == "blocked"
    assert payload["stop_parent"] is False
    assert payload["stop_reason"] == "completed"
    assert payload["control"]["kind"] == "tool_error"


def test_completed_chrome_turn_preserves_blocked_capability_outcome() -> None:
    outcome = chrome_outcome_for_result(
        {},
        skill="chrome",
        goal="打开网页",
        content="请先重新连接 Chrome。",
        stop_reason="completed",
        control={
            "kind": "tool_error",
            "blocked_capability": "chrome",
            "error_reason": "chrome_cli_worker_not_paired",
            "setup_url": "/integrations?provider=chrome",
        },
    )

    assert outcome.status == "blocked"
    assert outcome.goal_status == "blocked"
    assert outcome.retryable is True
    assert outcome.do_not_replay is False
    assert outcome.resume_context["stop_reason"] == "completed"
    assert outcome.resume_context["block_reason"] == "chrome_cli_worker_not_paired"
    assert outcome.resume_context["setup_url"] == "/integrations?provider=chrome"
    assert outcome.resume_context["replay_allowed"] is True


def test_incomplete_chrome_run_is_not_replayed_without_explicit_recovery() -> None:
    for stop_reason in ("max_rounds", "cancelled"):
        outcome = chrome_outcome_for_result(
            {},
            skill="chrome",
            goal="Upload the approved video",
            content="",
            stop_reason=stop_reason,
        ).to_dict()

        assert outcome["status"] == "blocked"
        assert outcome["retryable"] is False
        assert outcome["do_not_replay"] is True
        assert outcome["resume_context"]["replay_allowed"] is False


def test_explicit_uncertain_chrome_effect_is_not_replayable() -> None:
    outcome = chrome_outcome_for_result(
        {},
        skill="chrome",
        goal="Upload the approved video",
        content="The upload result is uncertain.",
        stop_reason="uncertain_result",
        control={"retryable": False},
    ).to_dict()

    assert outcome["status"] == "failed"
    assert outcome["retryable"] is False
    assert outcome["do_not_replay"] is True
    assert outcome["resume_context"]["replay_allowed"] is False


def test_claim_tab_leftover_pending_does_not_pause_completed_chrome_run() -> None:
    metadata = {
        "chrome_runtime_contract_v1": {
            "run_id": "chrome-run-claim",
            "last_tool": "mcp__chrome__finalize_tabs",
            "pending_chrome_confirmation": {
                "approval_id": "",
                "tool_name": "mcp__chrome__claim_tab",
                "confirmation_mode": "no_confirmation",
                "policy_category": "ordinary_page_action",
            },
        }
    }

    outcome = chrome_outcome_for_result(
        metadata,
        skill="chrome",
        goal="Save the approved YouTube draft",
        content="Video saved.",
        stop_reason="completed",
    ).to_dict()

    assert outcome["status"] == "completed"
    assert outcome["pending_action"] is None
    assert outcome["retryable"] is False
    assert metadata["chrome_runtime_contract_v1"].get("pending_chrome_confirmation") is None


def test_real_publish_pending_still_needs_approval_after_completed_stop_reason() -> None:
    outcome = chrome_outcome_for_result(
        {
            "chrome_runtime_contract_v1": {
                "run_id": "chrome-run-publish",
                "pending_chrome_confirmation": {
                    "approval_id": "approval-publish-1",
                    "tool_name": "mcp__chrome__click_element",
                    "confirmation_mode": "always_action_time",
                    "policy_category": "representational_communication",
                },
            }
        },
        skill="chrome",
        goal="Publish the approved YouTube draft",
        content="Waiting for Publish confirmation.",
        stop_reason="completed",
    ).to_dict()

    assert outcome["status"] == "needs_approval"
    assert outcome["pending_action"]["tool_name"] == "mcp__chrome__click_element"
    assert outcome["pending_action"]["confirmation_mode"] == "always_action_time"
    assert outcome["retryable"] is True
    assert outcome["resume_context"]["next"] == "confirm_action_then_retry_once"


def test_chrome_child_state_is_committed_back_to_the_parent_harness() -> None:
    parent_metadata = {
        "chrome_runtime_contract_v1": {
            "run_id": "chrome-run-4",
            "control_epoch": "epoch-4",
            "chrome_call_count": 1,
        }
    }
    child_metadata = {
        "chrome_runtime_contract_v1": {
            "run_id": "chrome-run-4",
            "control_epoch": "epoch-4",
            "chrome_call_count": 3,
            "pending_chrome_confirmation": {"approval_id": "approval-4"},
            "pending_observation_after_action": {"next_required_tool": "mcp__chrome__read_page"},
        }
    }

    committed = commit_chrome_runtime_state(parent_metadata, child_metadata)

    assert committed["run_id"] == "chrome-run-4"
    assert parent_metadata["chrome_runtime_contract_v1"]["chrome_call_count"] == 3
    assert parent_metadata["chrome_runtime_contract_v1"]["pending_chrome_confirmation"] == {
        "approval_id": "approval-4"
    }
    assert chrome_runtime_state_for_child(parent_metadata)["pending_observation_after_action"] == {
        "next_required_tool": "mcp__chrome__read_page"
    }


def test_chrome_runtime_state_for_child_keeps_governance_fields() -> None:
    metadata = {
        "chrome_runtime_contract_v1": {
            "run_id": "chrome-run-3",
            "control_epoch": "epoch-3",
            "pending_confirmation": {"approval_id": "approval-3"},
            "finalized": False,
        }
    }

    state = chrome_runtime_state_for_child(metadata)

    assert state["run_id"] == "chrome-run-3"
    assert state["control_epoch"] == "epoch-3"
    assert state["pending_confirmation"]["approval_id"] == "approval-3"
    assert state["finalized"] is False
