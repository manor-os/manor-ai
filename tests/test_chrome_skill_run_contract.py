from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

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
