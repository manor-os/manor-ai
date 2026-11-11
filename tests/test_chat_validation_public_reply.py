"""User-facing Chat failures do not publish internal outcome diagnostics."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.output_policy import (
    runtime_public_assistant_message_content,
    runtime_response_validation_failure_content,
)


@pytest.mark.parametrize("locale,expected", [
    ("zh", "抱歉，这次还没能确认你的请求是否完成。你可以让我继续跟进。"),
    ("zh-CN", "抱歉，这次还没能确认你的请求是否完成。你可以让我继续跟进。"),
    ("en", "Sorry, I couldn't confirm whether your request was completed. You can ask me to follow up."),
])
@pytest.mark.parametrize("reason", [
    "operation_evidence_mismatch", "outcome_claim_mismatch",
    "current_research_evidence_required", "operation_evidence_insufficient",
    "current_evidence_required", "response_contract_invalid", "unknown_reason",
])
def test_live_and_historical_failures_hide_diagnostics_without_losing_them(locale, expected, reason):
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services.chat_service import _chat_result_public_error, _chat_result_visible_content

    detail = {"response_locale": locale, "error": reason, "message": "Internal validation detail", "details": {
        "facts": [
            {"stage": "discovery", "result": "returned", "call_id": "private-call"},
            {"stage": "execution", "result": "returned", "operation": "submit_application"},
        ],
    }}
    meta = {"error": "final_response_validation_failed", "stop_reason": "error", "limit_detail": detail}
    original = deepcopy(meta)
    # Both old diagnostic prose and a rejected candidate must be reprojected.
    for stored in ("已保留本轮记录：1 次工具发现、3 次操作尝试", "已成功提交申请"):
        assert runtime_public_assistant_message_content(stored, meta) == expected
    result = SimpleNamespace(error=meta["error"], stop_reason="error", error_detail=detail, content="已成功提交申请")
    for surface in (ChatSurface.GLOBAL_OWNER_CHAT, ChatSurface.AGENT_DM, ChatSurface.WORKSPACE_CHAT):
        assert _chat_result_visible_content(result, surface) == expected
        assert _chat_result_public_error(result, surface) == expected
    assert meta == original


@pytest.mark.parametrize("detail", [None, [], "malformed", {"details": {"facts": "malformed"}}])
def test_legacy_failure_metadata_needs_no_diagnostic_shape(detail):
    assert runtime_response_validation_failure_content({
        "error": "final_response_validation_failed", "limit_detail": detail,
    }) == "Sorry, I couldn't confirm whether your request was completed. You can ask me to follow up."


@pytest.mark.parametrize("reason", ["sandbox_allocation_failed", "sandbox_queue_timeout"])
def test_resource_failure_keeps_model_summary_for_history_and_stream_end(reason):
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services.chat_service import _chat_result_public_error, _chat_result_visible_content

    summary = "I have your details, but the PDF could not be prepared because the file tools could not start."
    result = SimpleNamespace(error=reason, stop_reason="error", content=summary)
    for surface in (ChatSurface.GLOBAL_OWNER_CHAT, ChatSurface.AGENT_DM, ChatSurface.WORKSPACE_CHAT, ChatSurface.PUBLIC_CUSTOMER_CHAT):
        assert _chat_result_visible_content(result, surface) == summary
        assert _chat_result_public_error(result, surface) == summary


def test_validated_user_result_and_actionable_errors_are_unchanged():
    from packages.core.ai.runtime.output_policy import PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE

    content = "申请还未提交。请提供项目简介，我可以帮你准备申请材料。"
    assert runtime_public_assistant_message_content(content, {"stop_reason": "completed"}) == content
    assert runtime_public_assistant_message_content(PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE, {
        "error": "byok_provider_error", "stop_reason": "error",
    }) == PUBLIC_BYOK_PROVIDER_FAILURE_MESSAGE
    assert runtime_response_validation_failure_content(None) is None
    assert runtime_response_validation_failure_content({"error": "unrelated"}) is None


def test_contract_separates_user_answer_from_machine_evidence():
    from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract

    instructions = ChatEvidenceContract().instructions
    assert "Keep this bookkeeping out of\nuser-facing text" in instructions
    assert "Follow validation feedback if a reference or outcome needs" in instructions
    assert "Missing data is not a capability failure" in instructions
    assert "blocked needs an observed failure" in instructions


def test_unconfirmed_tool_error_keeps_public_summary():
    from packages.core.ai.runtime.output_policy import PUBLIC_TOOL_OUTCOME_UNCONFIRMED_MESSAGE
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services.chat_service import (
        _chat_result_public_error,
        _chat_result_visible_content,
    )

    summary = (
        "The Save click did not run because the page changed. "
        "I can read the current page and try again."
    )
    meta = {
        "error": "tool_error",
        "stop_reason": "error",
        "limit_detail": {
            "reason": "tool_outcome_unconfirmed",
            "source_tool": "mcp__chrome__click_element",
        },
    }
    assert runtime_public_assistant_message_content(summary, meta) == summary
    result = SimpleNamespace(
        error="tool_error", stop_reason="error",
        error_detail=meta["limit_detail"], content=summary,
    )
    for surface in (ChatSurface.GLOBAL_OWNER_CHAT, ChatSurface.AGENT_DM, ChatSurface.WORKSPACE_CHAT):
        assert _chat_result_visible_content(result, surface) == summary
    assert runtime_public_assistant_message_content("tool_error", meta) == PUBLIC_TOOL_OUTCOME_UNCONFIRMED_MESSAGE
    canned = SimpleNamespace(
        error="tool_error", stop_reason="error",
        error_detail=meta["limit_detail"], content="tool_error",
    )
    assert _chat_result_visible_content(canned, ChatSurface.WORKSPACE_CHAT) == PUBLIC_TOOL_OUTCOME_UNCONFIRMED_MESSAGE
    assert _chat_result_public_error(canned, ChatSurface.WORKSPACE_CHAT) == PUBLIC_TOOL_OUTCOME_UNCONFIRMED_MESSAGE


def test_authorization_and_permission_blocks_keep_typed_public_errors():
    import json

    from packages.core.ai.runtime.output_policy import (
        PUBLIC_AUTHORIZATION_BLOCKED_MESSAGE,
        PUBLIC_PERMISSION_DENIED_MESSAGE,
        PUBLIC_REQUEST_FAILURE_MESSAGE,
        PUBLIC_TOOL_FAILURE_MESSAGE,
        runtime_assistant_stream_error_content,
        runtime_public_tool_result,
    )
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services.chat_service import (
        _chat_result_public_error,
        _chat_result_visible_content,
    )

    envelope = {
        "error": "blocked_by_authorization_configuration",
        "message": "This tool has no declared execution-effect classification.",
        "matched_rule": "permission.tool.classification",
        "tool": "read_context_archive",
    }
    assert runtime_public_tool_result(envelope) == PUBLIC_AUTHORIZATION_BLOCKED_MESSAGE
    assert runtime_public_tool_result(json.dumps(envelope)) == PUBLIC_AUTHORIZATION_BLOCKED_MESSAGE
    assert runtime_assistant_stream_error_content(
        "blocked_by_authorization_configuration"
    ) == PUBLIC_AUTHORIZATION_BLOCKED_MESSAGE
    assert runtime_assistant_stream_error_content(
        json.dumps(envelope)
    ) == PUBLIC_AUTHORIZATION_BLOCKED_MESSAGE
    assert runtime_public_assistant_message_content(
        json.dumps(envelope),
        {"error": "tool_error", "stop_reason": "error"},
    ) == PUBLIC_AUTHORIZATION_BLOCKED_MESSAGE
    assert runtime_public_assistant_message_content(
        PUBLIC_REQUEST_FAILURE_MESSAGE,
        {"error": "blocked_by_authorization_configuration", "stop_reason": "error"},
    ) == PUBLIC_AUTHORIZATION_BLOCKED_MESSAGE
    canned = SimpleNamespace(
        error="blocked_by_authorization_configuration",
        stop_reason="error",
        error_detail=envelope,
        content=PUBLIC_REQUEST_FAILURE_MESSAGE,
    )
    assert _chat_result_visible_content(canned, ChatSurface.WORKSPACE_CHAT) == PUBLIC_AUTHORIZATION_BLOCKED_MESSAGE
    assert _chat_result_public_error(canned, ChatSurface.WORKSPACE_CHAT) == PUBLIC_AUTHORIZATION_BLOCKED_MESSAGE

    assert runtime_assistant_stream_error_content(
        "blocked_by_permission"
    ) == PUBLIC_PERMISSION_DENIED_MESSAGE
    assert runtime_public_tool_result(
        {"error": "blocked_by_permission", "tool": "bash"}
    ) == PUBLIC_PERMISSION_DENIED_MESSAGE
    assert runtime_public_tool_result(
        {"error": "private_failure", "reason": "private SQL"}
    ) == PUBLIC_TOOL_FAILURE_MESSAGE
    assert runtime_assistant_stream_error_content("consecutive_tool_errors") == PUBLIC_REQUEST_FAILURE_MESSAGE
    assert runtime_assistant_stream_error_content("boom") == PUBLIC_REQUEST_FAILURE_MESSAGE

def test_chrome_finalize_block_and_completed_skill_keep_public_truth():
    import json

    from packages.core.ai.runtime.output_policy import (
        PUBLIC_CHROME_TABS_ALREADY_FINALIZED_MESSAGE,
        PUBLIC_REQUEST_FAILURE_MESSAGE,
        PUBLIC_TOOL_BLOCKED_MESSAGE,
        PUBLIC_TOOL_FAILURE_MESSAGE,
        runtime_public_tool_result,
    )
    from packages.core.ai.runtime.streams import (
        runtime_tool_result_for_chat,
        runtime_tool_status_for_chat,
    )

    blocked = {
        "status": "blocked",
        "reason": "chrome_tabs_already_finalized",
        "blocked_tool": "mcp__chrome__tab_info",
        "message": (
            "mcp__chrome__finalize_tabs already finalized this Chrome Browser Group "
            "for the current Runtime run. Treat finalize_tabs as the final Chrome "
            "action of the turn."
        ),
        "next_step": "Start a new Chrome task/session before doing more browser work.",
    }
    blocked_json = json.dumps(blocked)
    assert runtime_public_tool_result(blocked) == PUBLIC_CHROME_TABS_ALREADY_FINALIZED_MESSAGE
    assert runtime_public_tool_result(blocked_json) == PUBLIC_CHROME_TABS_ALREADY_FINALIZED_MESSAGE
    assert runtime_tool_result_for_chat("mcp__chrome__tab_info", blocked_json) == (
        PUBLIC_CHROME_TABS_ALREADY_FINALIZED_MESSAGE
    )
    assert runtime_tool_status_for_chat(blocked_json) == "error"
    assert "mcp__" not in runtime_public_tool_result(blocked_json)
    assert runtime_public_tool_result(blocked_json) not in {
        PUBLIC_TOOL_FAILURE_MESSAGE,
        PUBLIC_REQUEST_FAILURE_MESSAGE,
    }

    unsafe_blocked = {
        "status": "blocked",
        "reason": "some_internal_block",
        "message": "mcp__chrome__click_element is not allowed now.",
    }
    assert runtime_public_tool_result(json.dumps(unsafe_blocked)) == PUBLIC_TOOL_BLOCKED_MESSAGE
    assert "mcp__" not in runtime_public_tool_result(json.dumps(unsafe_blocked))

    safe_blocked = {
        "status": "blocked",
        "reason": "workflow_scene_capture_preserve_tabs",
        "message": (
            "The approved Workflow scene has both durable capture receipts. "
            "Return those receipts now without finalizing tabs."
        ),
    }
    assert runtime_public_tool_result(json.dumps(safe_blocked)) == safe_blocked["message"]

    completed = {
        "status": "completed",
        "skill": "chrome",
        "summary": "Fleece Jacket and Backpack are in the cart.",
        "goal_status": "unverified",
        "evidence": [{
            "kind": "chrome_runtime",
            "last_tool": "mcp__chrome__finalize_tabs",
            "tabs_finalized": True,
        }],
        "execution": {
            "tools_used": ["mcp__chrome__read_page", "mcp__chrome__click_element"],
            "tool_observations": {
                "mcp__chrome__click_element": "clicked Add to cart without error",
            },
        },
    }
    completed_public = runtime_public_tool_result(json.dumps(completed))
    assert completed_public != PUBLIC_TOOL_FAILURE_MESSAGE
    assert completed_public != PUBLIC_REQUEST_FAILURE_MESSAGE
    assert "Fleece Jacket and Backpack are in the cart." in str(completed_public)

    traceback_failure = "Traceback (most recent call last):\nZeroDivisionError: division by zero"
    assert runtime_public_tool_result(traceback_failure) == PUBLIC_TOOL_FAILURE_MESSAGE


@pytest.mark.asyncio
async def test_format_repair_chatter_cannot_replace_the_user_answer():
    from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
    from packages.core.ai.runtime.final_response import FinalResponseAction

    contract = ChatEvidenceContract()
    def answer(text):
        return json.dumps({
            "kind": "answer", "text": text,
            "outcome": {"status": "answered", "evidence_refs": [], "limitations": []},
        }, ensure_ascii=False)

    rejected = await contract.validate(answer(
        "已收到。上一条回复未符合系统要求的 JSON 格式；本次将按规定返回完整 JSON。"
    ))
    assert rejected.action is FinalResponseAction.REVISE_REPLY
    assert rejected.diagnostic["error"] == "answer_text_internal_format_repair"

    accepted = await contract.validate(answer(
        "商业宣传片通常先展示产品，再用具体场景说明用途，最后给出清楚的品牌和行动提示。"
    ))
    assert accepted.action is FinalResponseAction.ACCEPT
