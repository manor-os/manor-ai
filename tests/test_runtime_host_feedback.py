"""Runtime feedback must never read as something the user typed.

Staging 2026-09-30: a follow-up question ("which option is best?") was answered
from the previous turn's search results. The evidence contract rejected the
historical call IDs and the loop appended the raw tool_error JSON as a bare
user-role message, so the model replied "you just sent a system error".
"""
import json

import pytest

from packages.core.ai.agentic_loop import _estimate_context_attribution
from packages.core.ai.runtime.agentic_loop_prompts import (
    RUNTIME_HOST_FEEDBACK_KEY,
    runtime_host_feedback_message,
    runtime_is_host_feedback_message,
)
from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
from packages.core.ai.runtime.final_response import FinalResponseAction
from tests.test_runtime_capability_refresh import mock_runtime, _envelope, _loop_kwargs  # noqa: F401

HISTORICAL_REF = "call_mBKCYOjX5GzNtQnhxjlQwtuR"


def reply(status, refs=(), claims=None, text="RV site is the cheapest option."):
    return json.dumps({"kind": "answer", "text": text, "outcome": {
        "status": status, "evidence_refs": list(refs),
        "limitations": [] if status in {"answered", "completed"} else ["Listings not re-verified"],
        "claims": claims if claims is not None else [],
    }})


def test_structured_feedback_keeps_every_diagnostic_key_and_is_marked():
    message = runtime_host_feedback_message({"error": "current_evidence_required", "message": "m"})
    payload = json.loads(message["content"])
    assert message["role"] == "user"
    assert next(iter(payload)) == RUNTIME_HOST_FEEDBACK_KEY
    assert "not by the user" in payload[RUNTIME_HOST_FEEDBACK_KEY]
    assert payload["error"] == "current_evidence_required" and payload["message"] == "m"
    assert runtime_is_host_feedback_message(message)


def test_text_feedback_is_enveloped_and_user_text_is_not_mistaken_for_it():
    message = runtime_host_feedback_message("[System: retry]")
    assert message["content"].startswith("<runtime-feedback ")
    assert "[System: retry]" in message["content"]
    assert runtime_is_host_feedback_message(message)
    for content in (
        "那种方式最好？",
        '{"error": "current_evidence_required"}',
        '{"runtime_feedback": "user typed this"}',
    ):
        assert not runtime_is_host_feedback_message({"role": "user", "content": content})
    assert not runtime_is_host_feedback_message({**message, "role": "assistant"})


def test_context_attribution_does_not_count_runtime_feedback_as_user_input():
    user_request = "那种方式最好？确保每月支出1000以下 我还有两只猫"
    feedback = runtime_host_feedback_message({"error": "current_evidence_required", "message": "x" * 800})
    attribution = _estimate_context_attribution([
        {"role": "system", "content": "system"},
        {"role": "user", "content": user_request},
        {"role": "assistant", "content": reply("completed")},
        feedback,
    ], None)
    assert attribution["user_input_chars"] == len(user_request)
    assert attribution["runtime_feedback_chars"] == len(feedback["content"])


@pytest.mark.asyncio
async def test_discussion_answer_citing_earlier_turn_is_accepted_without_the_citation():
    claims = [{"operation": "web_search", "status": "completed", "evidence_refs": [HISTORICAL_REF]}]
    decision = await ChatEvidenceContract().validate(reply("answered", [HISTORICAL_REF], claims))
    assert decision.action == FinalResponseAction.ACCEPT
    assert decision.outcome["evidence_refs"] == []
    assert decision.outcome["claims"] == []


@pytest.mark.asyncio
async def test_discussion_answer_keeps_current_evidence_and_drops_only_history():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("web_search", "fresh", '{"results":[{"title":"RV park"}]}')
    decision = await contract.validate(reply("answered", ["fresh", HISTORICAL_REF]))
    assert decision.action == FinalResponseAction.ACCEPT
    assert decision.outcome["evidence_refs"] == ["fresh"]


@pytest.mark.asyncio
async def test_completion_claims_from_history_still_require_current_evidence():
    claims = [{"operation": "web_search", "status": "completed", "evidence_refs": [HISTORICAL_REF]}]
    decision = await ChatEvidenceContract().validate(reply("completed", [HISTORICAL_REF], claims))
    assert decision.action == FinalResponseAction.CONTINUE_EXECUTION
    assert decision.diagnostic["error"] == "current_evidence_required"
    assert HISTORICAL_REF in decision.diagnostic["message"]
    assert "status answered" in decision.diagnostic["message"]


@pytest.mark.asyncio
async def test_research_turn_cannot_answer_from_history_alone():
    decision = await ChatEvidenceContract(requires_execution=True).validate(
        reply("answered", [HISTORICAL_REF])
    )
    assert decision.action == FinalResponseAction.CONTINUE_EXECUTION
    assert decision.diagnostic["error"] == "current_research_evidence_required"


@pytest.mark.asyncio
async def test_follow_up_from_history_is_answered_in_one_round(mock_runtime, monkeypatch):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    seen = []

    async def complete(messages, tools, **kwargs):
        seen.append(list(messages))
        return reply("answered", [HISTORICAL_REF]), [], {"total": 1}

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    kwargs = _loop_kwargs(_envelope("web_search"), None)
    kwargs.update(dynamic_tool_handlers={}, capability_refresh_tool_names=set(), final_response_contract=ChatEvidenceContract(), max_rounds=8)
    result = await runtime_execute_agentic_loop(**kwargs)
    assert result.stop_reason == "completed"
    assert result.rounds == 1 and len(seen) == 1
    assert result.content == "RV site is the cheapest option."


@pytest.mark.asyncio
async def test_rejected_reply_feedback_reaches_the_model_marked_as_runtime(mock_runtime, monkeypatch):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    seen = []
    claims = [{"operation": "web_search", "status": "completed", "evidence_refs": [HISTORICAL_REF]}]

    async def complete(messages, tools, **kwargs):
        seen.append(list(messages))
        if len(seen) == 1:
            return reply("completed", [HISTORICAL_REF], claims), [], {"total": 1}
        return reply("answered"), [], {"total": 1}

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    kwargs = _loop_kwargs(_envelope("web_search"), None)
    kwargs.update(dynamic_tool_handlers={}, capability_refresh_tool_names=set(), final_response_contract=ChatEvidenceContract(), max_rounds=8)
    result = await runtime_execute_agentic_loop(**kwargs)
    assert result.stop_reason == "completed"
    feedback = seen[1][-1]
    assert runtime_is_host_feedback_message(feedback)
    assert json.loads(feedback["content"])["error"] == "current_evidence_required"
    real_user_turns = [
        message for message in seen[1]
        if message.get("role") == "user" and not runtime_is_host_feedback_message(message)
    ]
    assert real_user_turns == [message for message in seen[0] if message.get("role") == "user"]


def test_language_guidance_stays_next_to_the_real_request_after_feedback():
    from packages.core.ai.runtime.response_language import RuntimeResponseLanguage, response_language_messages

    request = {"role": "user", "content": "那种方式最好？"}
    feedback = runtime_host_feedback_message({"error": "current_evidence_required"})
    messages = response_language_messages([
        {"role": "system", "content": "system"},
        request,
        {"role": "assistant", "content": reply("completed")},
        feedback,
    ], RuntimeResponseLanguage("entity", "user", locale="zh-CN"))
    guidance_index = next(
        index for index, message in enumerate(messages)
        if message.get("role") == "system" and index > 0
    )
    assert messages[guidance_index + 1] is request
    assert messages[-1] is feedback
