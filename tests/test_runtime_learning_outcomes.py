"""Regressions from real multi-turn context/learning browser scenarios."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.services import runtime_learning as learning
from packages.core.ai.runtime.streams import runtime_tool_error_result


def receipt(result, *, action="delegate_service", **extra):
    return {"name": "manor", "arguments": {"action": action}, "result": result, **extra}


@pytest.mark.parametrize("receipts, expected", [
    ([receipt(runtime_tool_error_result("missing action"))] * 4, "failed"),
    ([receipt('{"status":"failed","error":"not written"}')], "failed"),
    ([receipt('{"status":"unknown"}')], "partial"),
    ([receipt('{"status":"queued"}')], "partial"),
    ([receipt('{"status":"waiting_human"}')], "blocked"),
    ([receipt('{"success":false}')], "failed"),
    ([receipt('{"status":"completed"}'), receipt('{"status":"failed"}')], "partial"),
    ([receipt('{"status":"completed"}')], "succeeded"),
    ([receipt("redacted", raw_result='{"error":"validation failed"}')], "failed"),
])
def test_final_answer_does_not_turn_failed_or_unfinished_work_into_success(receipts, expected):
    status, _ = learning.chat_learning_outcome(
        stop_reason="completed", has_content=True, error=None,
        tools=["manor"] * len(receipts), tool_results=receipts,
    )
    assert status == expected
    drafts = learning.build_chat_learning_candidate_drafts(
        user_message="Review the draft", assistant_content="Here is a fallback draft",
        tool_calls_made=["manor"], tool_results=receipts, status=status,
        stop_reason="completed", repeated_tool_pattern_count=4,
    )
    positive = {d.candidate_type for d in drafts} & {"skill", "tool_experience"}
    assert bool(positive) is (expected == "succeeded")


@pytest.mark.parametrize("stop_reason,error,expected", [
    ("cancelled", None, "cancelled"),
    ("completed", "provider failure", "failed"),
    ("hitl_required", None, "blocked"),
])
def test_run_control_outcomes_override_visible_content(stop_reason, error, expected):
    status, _ = learning.chat_learning_outcome(
        stop_reason=stop_reason, has_content=True, error=error, tools=[], tool_results=[],
    )
    assert status == expected


def test_missing_receipt_is_unknown_and_error_after_excerpt_is_not_lost():
    status, counts = learning.chat_learning_outcome(
        stop_reason="completed", has_content=True, error=None,
        tools=["manor", "manor"],
        tool_results=[receipt('{"padding":"' + "x" * 2000 + '","error":"failed"}')],
    )
    assert status == "partial"
    assert counts == {"succeeded": 0, "failed": 1, "blocked": 0, "unknown": 1}


def test_action_keys_separate_operations_and_legacy_claims():
    first = learning._tool_pattern_key(["manor"], [receipt("ok")])
    second = learning._tool_pattern_key(["manor"], [receipt("ok", action="create_task")])
    legacy = learning._tool_pattern_key(["manor"])
    assert len({first, second, legacy}) == 3


def test_nested_workspace_actions_are_distinct():
    receipts = [
        {"name": "manor", "arguments": {"action": "workspace", "params": {"action": action}}}
        for action in ("delegate_service", "create_task")
    ]
    assert learning._tool_pattern_key(["manor"], receipts[:1]) != learning._tool_pattern_key(["manor"], receipts[1:])


@pytest.mark.parametrize("message,expected", [
    ("以后你的工作方式是先列依据，再写待确认。本轮只确认收到，不调用工具或改文件。",
     "以后你的工作方式是先列依据，再写待确认。"),
    ("以后你负责客户接待，本轮不要调用工具。", "以后你负责客户接待"),
    ("From now on your role is reviewer. For this turn only, do not use tools.",
     "From now on your role is reviewer."),
    ("以后你负责审核；不要编造销量。本轮只确认。以后默认标记估算。",
     "以后你负责审核；不要编造销量。以后默认标记估算。"),
    ("本轮你是审核员，必须先汇总，不要调用工具。", ""),
    ("你是审核员，请检查这篇文章。", ""),
    ("请分析以下文档：以后你负责审核。", ""),
    ("```\n以后你负责审核。\n```", ""),
    ("以后你负责审核。" + "完整且不可截断的资格条件" * 80, ""),
])
def test_extracts_only_complete_explicit_durable_guidance(message, expected):
    assert learning._durable_user_guidance(message) == expected
    drafts = learning.build_chat_learning_candidate_drafts(
        user_message=message, assistant_content="收到", tool_calls_made=[],
        status="succeeded", stop_reason="completed",
    )
    for draft in drafts:
        if draft.candidate_type == "agent_profile_patch":
            assert draft.payload["profile_update"] == expected
            assert "本轮" not in draft.payload["profile_update"]
        assert draft.candidate_type != "memory"
    if not expected:
        assert drafts == []


def test_cancelled_role_guidance_cannot_auto_apply():
    drafts = learning.build_chat_learning_candidate_drafts(
        user_message="以后你负责客户接待。", assistant_content="", tool_calls_made=[],
        status="cancelled", stop_reason="cancelled",
    )
    profile = next(d for d in drafts if d.candidate_type == "agent_profile_patch")
    assert profile.payload["auto_apply_eligible"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", [None, "master", "manor-master", "_master"])
async def test_master_aliases_apply_without_requiring_a_custom_agent_row(agent_id):
    db = AsyncMock()
    await learning._ensure_agent_access(db, entity_id="entity", agent_id=agent_id)
    db.execute.assert_not_awaited()
    from packages.core.services.agent_files import effective_agent_id
    assert effective_agent_id(agent_id) == "_master"
    assert effective_agent_id("custom-agent") == "custom-agent"


@pytest.mark.asyncio
async def test_custom_agent_still_requires_matching_owner():
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(entity_id="other"))
    with pytest.raises(ValueError, match="inaccessible"):
        await learning._ensure_agent_access(db, entity_id="entity", agent_id="custom-agent")


@pytest.mark.asyncio
async def test_recording_persists_receipt_outcomes_and_source_manifest(monkeypatch):
    captured = {}

    async def record(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="evidence")

    monkeypatch.setattr(learning, "record_runtime_evidence", record)
    monkeypatch.setattr(learning, "_recent_tool_pattern_count", AsyncMock(return_value=4))
    monkeypatch.setattr(learning, "_recent_failure_count", AsyncMock(return_value=0))
    _, drafts = await learning.record_chat_run_evidence(
        AsyncMock(), entity_id="entity", user_message="Review draft", assistant_content="Fallback",
        stop_reason="completed", tool_calls_made=["manor"],
        tool_results=[receipt('{"status":"failed","error":"missing action"}')],
        context_sources=[{"source": "memory/AGENT.md", "source_digest": "abc", "loaded_tokens": 42,
                          "content": "must not duplicate private source bytes"}],
    )
    assert captured["status"] == "failed"
    assert captured["details"]["stop_reason"] == "completed"
    assert captured["details"]["outcome_reason"] == "tool_execution_failed"
    assert captured["metrics"]["tool_outcomes"]["failed"] == 1
    assert captured["details"]["tool_results"][0]["outcome"] == "failed"
    assert captured["details"]["context_sources"] == [
        {"source": "memory/AGENT.md", "source_digest": "abc", "loaded_tokens": 42},
    ]
    assert drafts == []


@pytest.mark.asyncio
async def test_chat_context_bridge_captures_rendered_sections(monkeypatch):
    record = AsyncMock(return_value=(SimpleNamespace(id="evidence"), []))
    monkeypatch.setattr(learning, "record_chat_run_evidence", record)
    ctx = SimpleNamespace(runtime_context_sections=[{
        "source": "workspace_agent:entity:workspace:_master:AGENT.md",
        "content": "private instructions", "source_digest": "digest", "loaded_tokens": 25,
    }])
    await learning.record_chat_runtime_learning(
        AsyncMock(), entity_id="entity", user_id="user", agent_id="manor-master",
        conversation_id="conversation", message_id="message", message="Request",
        result=SimpleNamespace(content="Done"), ctx=ctx, trace=None, tool_results=[],
    )
    sources = record.await_args.kwargs["context_sources"]
    assert sources == [{
        "source": "workspace_agent:entity:workspace:_master:AGENT.md",
        "source_digest": "digest", "loaded_tokens": 25,
    }]
