import json

import pytest

from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
from packages.core.ai.runtime.final_response import FinalResponseAction
from tests.test_runtime_capability_refresh import mock_runtime, _envelope, _loop_kwargs  # noqa: F401


def answer(status, refs=(), limitations=(), *, operation="web_search"):
    return json.dumps({"kind": "answer", "text": "Observed result", "outcome": {
        "status": status, "evidence_refs": list(refs), "limitations": list(limitations),
        "claims": [{"operation": operation, "status": status, "evidence_refs": list(refs)}] if status in {"completed", "partial", "blocked"} else [],
    }})


@pytest.mark.asyncio
async def test_disconnected_discovery_and_old_tasks_do_not_prove_current_inability():
    contract = ChatEvidenceContract(requires_execution=True)
    contract.observe_tool_result("search_tools", "discovery", json.dumps({"ok": True, "suppressed_mcp": ["apify"]}))
    contract.observe_tool_result("manor", "old-task", '{"status":"blocked"}', {"action": "search"})
    decision = await contract.validate(answer("blocked", ["discovery", "old-task"], ["Apify disconnected"]))
    assert decision.action == FinalResponseAction.CONTINUE_EXECUTION
    assert decision.diagnostic["error"] == "current_research_evidence_required"
    contract.observe_tool_result("web_fetch", "attempt", '{"error":"authentication_required","message":"Login required"}')
    decision = await contract.validate(answer("blocked", ["attempt"], ["This page requires login; latest reel remains unverified"], operation="web_fetch"))
    assert decision.action == FinalResponseAction.ACCEPT


@pytest.mark.asyncio
async def test_no_attempt_cannot_hide_in_answer_and_forged_refs_are_rejected():
    contract = ChatEvidenceContract(requires_execution=True)
    assert (await contract.validate(answer("answered"))).action == FinalResponseAction.CONTINUE_EXECUTION
    assert (await contract.validate(answer("completed", ["invented"])) ).diagnostic["error"] == "current_evidence_required"


@pytest.mark.asyncio
async def test_current_evidence_survives_checkpoint_but_not_new_turn():
    contract = ChatEvidenceContract(requires_execution=True)
    contract.observe_tool_result("web_search", "lookup", '{"results":[]}')
    resumed = ChatEvidenceContract()
    resumed.restore_checkpoint_state(contract.checkpoint_state())
    assert (await resumed.validate(answer("partial", ["lookup"], ["No latest post verified"]))).action == FinalResponseAction.ACCEPT
    new_turn = ChatEvidenceContract(requires_execution=True)
    assert (await new_turn.validate(answer("completed", ["lookup"]))).action == FinalResponseAction.CONTINUE_EXECUTION


@pytest.mark.asyncio
async def test_explanations_need_no_fake_execution_and_failed_attempts_not_completion():
    contract = ChatEvidenceContract()
    assert (await contract.validate(answer("answered"))).action == FinalResponseAction.ACCEPT
    contract.observe_tool_result("web_fetch", "failed", '{"error":"timeout"}')
    assert (await contract.validate(answer("completed", ["failed"]))).diagnostic["error"] == "completion_evidence_missing"


@pytest.mark.asyncio
async def test_real_apply_denial_is_current_evidence_not_a_reason_to_force_retrieval():
    contract = ChatEvidenceContract(requires_execution=True)
    contract.observe_tool_result("acquire_capabilities", "denied", '{"error":"capability_approval_unavailable"}', {"action": "apply"})
    assert (await contract.validate(answer("blocked", ["denied"], ["The requested installation was declined; no retrieval was performed"], operation="acquire_capabilities.apply"))).action == FinalResponseAction.ACCEPT
    assert (await contract.validate(answer("completed", ["denied"]))).action == FinalResponseAction.CONTINUE_EXECUTION
    discovery = ChatEvidenceContract(requires_execution=True)
    discovery.observe_tool_result("acquire_capabilities", "candidate", '{"error":"credentials_unavailable"}', {"action": "prepare"})
    assert (await discovery.validate(answer("blocked", ["candidate"], ["One candidate is disconnected"]))).action == FinalResponseAction.CONTINUE_EXECUTION


def test_search_compaction_preserves_separate_assessment_and_availability():
    from packages.core.ai.agentic_loop import _compact_search_tools_result_for_context

    result = {"matches": [{"name": "web_search", "kind": "tool", "assessment": {"fit": "partial"},
                            "availability": {"execution_verified": False}}],
              "discovery": {"capability_availability": "not_established"}}
    compact = json.loads(_compact_search_tools_result_for_context(result, ["web_search"]))
    assert compact["discovery"] == result["discovery"]
    assert compact["capability_assessments"][0]["availability"]["execution_verified"] is False


@pytest.mark.asyncio
async def test_chat_streams_tool_round_progress_before_execution_without_leaking_final_candidates(monkeypatch):
    import packages.core.ai.agentic_loop as loop

    events, calls = [], []
    opening = "我先查询资料，确认哪些结果符合条件。"
    progress = "第一轮没有匹配结果，我会缩小范围再查询。"

    async def stream(event, payload):
        events.append((event, payload))

    async def completion(messages, tools, **kwargs):
        round_number = len(calls) + 1
        text = json.dumps({"kind": "progress", "text": opening if round_number == 1 else progress}) if round_number < 3 else answer("completed", ["lookup-1", "lookup-2"])
        await kwargs["stream_handler"]("text_delta", {"content": text})
        if round_number < 3:
            return text, [{"id": f"lookup-{round_number}", "name": "web_search", "arguments": {}}], {}
        return text, None, {}

    async def execute(name, args):
        expected = opening if not calls else progress
        assert events[-1] == ("text_delta", {"content": expected})
        calls.append(name)
        return '{"results":[]}'

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", completion)
    result = await loop.agentic_loop(
        system_prompt="system", user_message="帮我查询", tools=[{
            "type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}},
        }], tool_executor=execute, stream_handler=stream, max_rounds=4,
        final_response_contract=ChatEvidenceContract(),
    )
    assert result.content == "Observed result"
    assert calls == ["web_search", "web_search"]
    assert [payload["content"] for event, payload in events if event == "text_delta"] == [opening, progress]
    assert events[-1][0] == "final_response_start"


@pytest.mark.parametrize("content", [
    "Unvalidated answer", '{"kind":"answer","text":"Done"}',
    '{"kind":"progress","text":"Done","outcome":{"status":"completed"}}',
    '{"kind":"progress","kind":"answer","text":"Done"}',
    '{"kind":"progress","text":""}',
])
def test_final_candidates_and_invalid_progress_cannot_enter_live_process(content):
    assert ChatEvidenceContract().tool_progress_text(content) is None


@pytest.mark.asyncio
async def test_loop_rejects_stale_blocker_then_runs_tool_and_returns_scoped_result(mock_runtime, monkeypatch, caplog):  # noqa: F811
    import logging

    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    caplog.set_level(logging.INFO, logger="packages.core.ai.agentic_loop")
    calls, rounds = [], 0

    async def execute(name, args, **context):
        calls.append(name)
        return '{"error":"authentication_required","message":"This specific page requires login"}'

    async def completion(messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            return answer("answered"), [], {"total": 1}
        if rounds == 2:
            assert "current_research_evidence_required" in json.dumps(messages)
            return "", [{"id": "current-attempt", "name": "web_fetch", "arguments": {"url": "https://example.com"}}], {"total": 1}
        return answer("partial", ["current-attempt"], ["Only this URL was attempted; latest post remains unverified"], operation="web_fetch"), [], {"total": 1}

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(_envelope("web_fetch"), None)
    kwargs["dynamic_tool_handlers"] = {}
    kwargs["capability_refresh_tool_names"] = set()
    kwargs["final_response_contract"] = ChatEvidenceContract(requires_execution=True)
    result = await runtime_execute_agentic_loop(**kwargs)
    assert result.content == "Observed result"
    assert result.stop_reason == "completed"
    assert calls == ["web_fetch"]
    assert rounds == 3
    # Each rejected reply costs a model round; the log names the rule.
    rejected = [r.getMessage() for r in caplog.records if "Final response rejected" in r.getMessage()]
    assert len(rejected) == 1
    assert "current_research_evidence_required" in rejected[0]
