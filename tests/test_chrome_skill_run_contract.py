from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from packages.core.ai.runtime.chrome_run import (
    ChromeSkillOutcome,
    ChromeSkillRunState,
    chrome_outcome_for_result,
    commit_chrome_runtime_state,
    chrome_outcome_for_result,
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
from packages.core.ai.runtime.tool_availability import (
    runtime_mcp_credentials_unavailable_result,
)


ROOT = Path(__file__).parents[1]


def test_chrome_skill_is_a_capability_companion_not_a_required_policy() -> None:
    config = json.loads(
        (ROOT / "packages/core/ai/skills/chrome/config.json").read_text(
            encoding="utf-8"
        )
    )

    assert "invocation_policy" not in config
    assert "capability_companion" not in config

    companion = trusted_skill_capability_companion(
        SimpleNamespace(
            entity_id=None,
            slug="chrome",
            name="chrome",
            config={"source": "builtin"},
        )
    )
    assert companion is not None
    assert companion.tool_prefixes == ("mcp__chrome__",)


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
    assert payload["pending_action"]["approval_id"] == "approval-1"
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
    assert outcome.resume_context["stop_reason"] == "completed"
    assert outcome.resume_context["block_reason"] == "chrome_cli_worker_not_paired"
    assert outcome.resume_context["setup_url"] == "/integrations?provider=chrome"


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
