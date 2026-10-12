"""Continuation corrects missing work without replaying effects or hiding limits."""
import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
from packages.core.ai.runtime.final_response import FinalResponseAction
from tests.test_runtime_capability_refresh import mock_runtime, _envelope, _loop_kwargs  # noqa: F401


def reply(status, operation="read_file", ref=None, text="Observed result"):
    refs = [ref] if ref else []
    return json.dumps({"kind": "answer", "text": text, "outcome": {
        "status": status, "evidence_refs": refs,
        "limitations": [] if status in {"completed", "answered"} else ["Reading not completed"],
        "claims": [] if status == "answered" else [
            {"operation": operation, "status": status, "evidence_refs": refs},
        ],
    }})


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["mcp__chrome__read_page", "read_file", "web_fetch"])
async def test_unattempted_work_returns_to_the_model(operation):
    decision = await ChatEvidenceContract().validate(reply("not_attempted", operation))
    assert decision.action == FinalResponseAction.CONTINUE_EXECUTION
    assert decision.diagnostic["error"] == "current_request_unresolved"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Here is the plan", "It was not submitted", "Stopped", "Explaining the result"])
async def test_discussion_status_and_stop_allow_zero_calls(text):
    decision = await ChatEvidenceContract().validate(reply("answered", text=text))
    assert decision.action == FinalResponseAction.ACCEPT


@pytest.mark.asyncio
async def test_partial_does_not_execute_unrequested_submission():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("read_file", "read", '{"content":"instructions"}')
    candidate = json.loads(reply("partial", ref="read"))
    candidate["outcome"]["claims"][0]["status"] = "completed"
    candidate["outcome"]["claims"].append({"operation": "submit_application", "status": "not_attempted", "evidence_refs": []})
    assert (await contract.validate(json.dumps(candidate))).action == FinalResponseAction.ACCEPT


@pytest.mark.asyncio
async def test_operation_mismatch_repairs_reply_and_never_accepts_read_as_write():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("read_file", "read", '{"content":"instructions"}')
    for operation in ("read_file inspect fixture", "write_file"):
        decision = await contract.validate(reply("completed", operation, "read"))
        assert decision.action == FinalResponseAction.REVISE_REPLY
    assert (await contract.validate(reply("completed", ref="read"))).action == FinalResponseAction.ACCEPT


@pytest.mark.asyncio
async def test_accepted_outcome_has_canonical_refs_and_survives_metadata():
    from packages.core.ai.runtime.output_policy import runtime_assistant_result_meta

    contract = ChatEvidenceContract()
    contract.observe_tool_result("read_file", "actual-read", '{"content":"instructions"}')
    decision = await contract.validate(reply("completed", ref="e1"))
    assert decision.outcome["evidence_refs"] == ["actual-read"]
    assert decision.outcome["claims"][0]["evidence_refs"] == ["actual-read"]
    meta = runtime_assistant_result_meta(SimpleNamespace(outcome=decision.outcome))
    assert meta["outcome"] == decision.outcome


@pytest.mark.asyncio
async def test_loop_continues_then_repairs_without_repeating_effect(mock_runtime, monkeypatch):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    rounds, repairs, calls = 0, 0, []

    async def complete(messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        # Three execution corrections must not consume the reply-repair budget.
        if rounds <= 3:
            return reply("not_attempted"), [], {"total": 1}
        if rounds == 4:
            return "", [{"id": "read", "name": "read_file", "arguments": {"path": "/fixture"}}], {"total": 1}
        return reply("completed", "read_file inspect fixture", "read"), [], {"total": 1}

    async def revise(messages, **kwargs):
        nonlocal repairs
        repairs += 1
        assert "operation_evidence_mismatch" in str(messages)
        return reply("completed", ref="read"), {"total": 1}

    async def execute(name, args, **kwargs):
        calls.append(name)
        return '{"content":"fixture"}'

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_text_completion", revise)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(_envelope("read_file"), None)
    kwargs.update(dynamic_tool_handlers={}, capability_refresh_tool_names=set(), final_response_contract=ChatEvidenceContract(), max_rounds=8)
    result = await runtime_execute_agentic_loop(**kwargs)
    assert result.stop_reason == "completed"
    assert calls == ["read_file"]
    assert rounds == 5 and repairs == 1
    assert result.outcome["status"] == "completed"


@pytest.mark.asyncio
async def test_correction_exhaustion_never_persists_rejected_outcome(mock_runtime, monkeypatch):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop
    from packages.core.ai.runtime.output_policy import runtime_assistant_result_meta

    calls = 0

    async def complete(messages, tools, **kwargs):
        nonlocal calls
        calls += 1
        return reply("not_attempted", text="Unaccepted candidate"), [], {"total": 1}

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    kwargs = _loop_kwargs(_envelope("read_file"), None)
    kwargs.update(dynamic_tool_handlers={}, capability_refresh_tool_names=set(), final_response_contract=ChatEvidenceContract(), max_rounds=8)
    result = await runtime_execute_agentic_loop(**kwargs)
    assert result.error == "final_response_validation_failed"
    assert calls == 4 and result.tool_calls_made == []
    assert result.outcome is None
    assert "outcome" not in runtime_assistant_result_meta(result)
    assert "Unaccepted candidate" not in result.content


@pytest.mark.asyncio
async def test_new_tool_observation_allows_reply_repair_after_early_format_errors(mock_runtime, monkeypatch):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    tool_rounds, repairs, calls = 0, 0, []

    async def complete(messages, tools, **kwargs):
        nonlocal tool_rounds
        tool_rounds += 1
        if tool_rounds == 1:
            return "invalid reply", [], {"total": 1}
        if tool_rounds == 2:
            return "", [{"id": "read", "name": "read_file", "arguments": {"path": "/fixture"}}], {"total": 1}
        return reply("completed", ref="mistyped-call-id"), [], {"total": 1}

    async def revise(messages, **kwargs):
        nonlocal repairs
        repairs += 1
        if repairs < 3:
            return "invalid reply", {"total": 1}
        if repairs == 3:
            return reply("not_attempted"), {"total": 1}
        assert "current_evidence_required" in str(messages)
        return reply("completed", ref="read"), {"total": 1}

    async def execute(name, args, **kwargs):
        calls.append(name)
        return '{"content":"fixture"}'

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_text_completion", revise)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(_envelope("read_file"), None)
    kwargs.update(dynamic_tool_handlers={}, capability_refresh_tool_names=set(), final_response_contract=ChatEvidenceContract(), max_rounds=9)
    result = await runtime_execute_agentic_loop(**kwargs)

    assert result.stop_reason == "completed"
    assert result.outcome["status"] == "completed"
    assert result.outcome["evidence_refs"] == ["read"]
    assert calls == ["read_file"]
    assert repairs == 4 and tool_rounds == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_lane", [False, True])
@pytest.mark.parametrize("research", [False, True])
async def test_both_round_limit_exits_keep_honest_unfinished_outcome(mock_runtime, monkeypatch, summary_lane, research):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    async def complete(messages, tools, **kwargs):
        if summary_lane:
            return "", [{"id": "lookup", "name": "read_file", "arguments": {}}], {"total": 1}
        return reply("not_attempted"), [], {"total": 1}

    async def summarize(messages, **kwargs):
        return reply("not_attempted"), {"total": 1}

    async def execute(*args, **kwargs):
        return '{"content":"unrelated fixture"}'

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_text_completion", summarize)
    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_final_completion", summarize)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(_envelope("read_file"), None)
    kwargs.update(dynamic_tool_handlers={}, capability_refresh_tool_names=set(), final_response_contract=ChatEvidenceContract(requires_execution=research), max_rounds=1)
    result = await runtime_execute_agentic_loop(**kwargs)
    assert (result.stop_reason, result.error) == ("max_rounds", None)
    assert result.outcome["status"] == "not_attempted"


@pytest.mark.asyncio
async def test_terminal_workspace_summary_reports_host_facts_without_reopening_research():
    from packages.core.ai.runtime.final_response import FinalResponseContext
    from packages.core.ai.runtime.workspace_response import WorkspaceChatResponseContract

    async def read_facts(scope, refs):
        return [{"id": "task", "revision": "r1", "origin": "existing_record",
                 "state": "not_started", "title": "Research", "url": "/tasks/task"}]

    contract = ChatEvidenceContract(
        inner=WorkspaceChatResponseContract(None, reader=read_facts), requires_execution=True,
    )
    candidate = json.dumps({"kind": "execution_summary", "references": [
        {"kind": "task", "id": "task", "scope": "existing_record", "revision": "r1"},
    ]})
    assert (await contract.validate(candidate)).action == FinalResponseAction.CONTINUE_EXECUTION
    decision = await contract.validate_with_context(
        candidate, FinalResponseContext(allow_execution=False, stop_reason="max_rounds"),
    )
    assert decision.action == FinalResponseAction.ACCEPT
    assert decision.stop_reason == "max_rounds"
    assert "no execution recorded" in decision.content


@pytest.mark.parametrize("outcome_status,stop_reason,expected", [
    ("partial", "completed", "partial"),
    ("not_attempted", "completed", "partial"),
    ("blocked", "completed", "blocked"),
    ("completed", "cancelled", "cancelled"),
    ("answered", "completed", "succeeded"),
])
def test_learning_preserves_validated_goal_outcome(outcome_status, stop_reason, expected):
    from packages.core.services.runtime_learning import chat_learning_outcome

    status, _ = chat_learning_outcome(
        stop_reason=stop_reason, has_content=True, error=None, tools=[], tool_results=[],
        outcome={"status": outcome_status, "evidence_refs": [], "limitations": []},
    )
    assert status == expected
