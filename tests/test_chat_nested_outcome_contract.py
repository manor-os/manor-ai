"""Host evidence crosses delegation boundaries without granting replay authority."""
import json

import pytest

from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.final_response import FinalResponseAction
from packages.core.ai.runtime.skills import runtime_format_invoke_skill_result
from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence
from tests.test_runtime_capability_refresh import mock_runtime, _envelope, _loop_kwargs  # noqa: F401


def reply(status, claims, limitations=("Application has not been submitted",)):
    return json.dumps({"kind": "answer", "text": "Observed progress; submission remains unverified.", "outcome": {
        "status": status, "claims": claims, "limitations": list(limitations),
        "evidence_refs": list(dict.fromkeys(ref for claim in claims for ref in claim["evidence_refs"])),
    }})


def claim(operation, status, *refs):
    return {"operation": operation, "status": status, "evidence_refs": list(refs)}


def child_result(tool="search_tools", result=None):
    evidence = RuntimeToolEvidence()
    evidence.observe_tool_result(tool, "lookup", result or {
        "ok": True, "details": {"unavailable_provider_keys": ["chrome"], "failure_scope": "provider_discovery"},
    })
    return runtime_format_invoke_skill_result("chrome", {
        "skill": "chrome", "stop_reason": "completed", "content": "Untrusted child summary",
        "tools_used": [tool], "tool_evidence": evidence.checkpoint_state(),
        "chrome_outcome": {"status": "completed", "goal_status": "unverified"},
    })


@pytest.mark.asyncio
async def test_current_turn_alias_repairs_unknown_reference_without_minting_evidence():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("browse_web", "call_actual", '{"url":"https://example.test"}')
    invalid = await contract.validate(reply("completed", [claim("browse_web", "completed", "call_invented")]))
    assert invalid.action == FinalResponseAction.REVISE_REPLY
    assert invalid.diagnostic["details"]["evidence_aliases"] == [
        {"alias": "e1", "call_id": "call_actual", "operation": "browse_web", "stage": "execution", "result": "returned"}
    ]
    accepted = await contract.validate(reply("completed", [claim("browse_web", "completed", "e1")], ()))
    assert accepted.action == FinalResponseAction.ACCEPT
    wrong_operation = await contract.validate(reply("completed", [claim("write_file", "completed", "e1")], ()))
    # Reject the false write claim without reopening execution to repair text.
    assert wrong_operation.action == FinalResponseAction.REVISE_REPLY


@pytest.mark.asyncio
async def test_child_discovery_is_visible_but_cannot_prove_browser_or_goal_completion():
    contract = ChatEvidenceContract()
    result = child_result()
    contract.observe_tool_result("invoke_skill", "skill", result)
    assert contract.observations["skill"]["stage"] == "delegation"
    assert contract.observations["skill/lookup"]["unavailable_provider_keys"] == ["chrome"]
    claims = [claim("search_tools", "completed", "skill/lookup"), claim("submit_application", "not_attempted")]
    assert (await contract.validate(reply("partial", claims))).action == FinalResponseAction.ACCEPT
    assert (await contract.validate(reply("blocked", [claim("invoke_skill", "blocked", "skill")]))).action != FinalResponseAction.ACCEPT
    assert (await contract.validate(reply("completed", [claim("invoke_skill", "completed", "skill")], ()))).action != FinalResponseAction.ACCEPT
    assert json.loads(result)["execution"]["goal_status"] == "unverified"


@pytest.mark.asyncio
async def test_delegation_claim_with_observed_child_work_repairs_reply_without_reexecution():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("invoke_skill", "skill", child_result("read_file", {"content": "Observed file"}))
    invalid = await contract.validate(reply("completed", [
        claim("invoke_skill", "completed", "skill"),
        claim("read_file", "completed", "skill/lookup"),
    ], ()))
    assert invalid.action == FinalResponseAction.REVISE_REPLY
    assert invalid.diagnostic["error"] == "operation_evidence_insufficient"
    assert "replace" in invalid.diagnostic["message"].lower()
    accepted = await contract.validate(reply("completed", [claim("read_file", "completed", "skill/lookup")], ()))
    assert accepted.action == FinalResponseAction.ACCEPT


@pytest.mark.asyncio
@pytest.mark.parametrize("requires_execution", [False, True])
async def test_reply_citing_only_delegation_can_repair_using_its_observed_child(requires_execution):
    contract = ChatEvidenceContract(requires_execution=requires_execution)
    contract.observe_tool_result("invoke_skill", "skill", child_result("web_fetch", {"text": "Observed source"}))
    invalid = await contract.validate(reply("completed", [claim("invoke_skill", "completed", "skill")], ()))
    assert invalid.action == FinalResponseAction.REVISE_REPLY
    assert "child" in invalid.diagnostic["message"].lower()
    corrected = await contract.validate(reply("completed", [claim("web_fetch", "completed", "skill/lookup")], ()))
    assert corrected.action == FinalResponseAction.ACCEPT


@pytest.mark.asyncio
async def test_internal_child_read_cannot_repair_missing_external_research():
    contract = ChatEvidenceContract(requires_execution=True)
    contract.observe_tool_result("invoke_skill", "skill", child_result("read_file", {"text": "Internal notes"}))
    invalid = await contract.validate(reply("completed", [claim("invoke_skill", "completed", "skill")], ()))
    assert invalid.action == FinalResponseAction.CONTINUE_EXECUTION
    assert invalid.diagnostic["error"] == "current_research_evidence_required"


@pytest.mark.asyncio
async def test_delegation_without_observed_execution_does_not_enter_reply_only_repair():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("invoke_skill", "skill", child_result())
    invalid = await contract.validate(reply("completed", [claim("invoke_skill", "completed", "skill")], ()))
    assert invalid.action == FinalResponseAction.CONTINUE_EXECUTION


@pytest.mark.asyncio
async def test_reply_repair_does_not_borrow_execution_from_another_child():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("invoke_skill", "first", child_result("read_file", {"content": "Other file"}))
    contract.observe_tool_result("invoke_skill", "second", child_result())
    invalid = await contract.validate(reply("completed", [
        claim("invoke_skill", "completed", "second"),
        claim("read_file", "completed", "first/lookup"),
    ], ()))
    assert invalid.action == FinalResponseAction.CONTINUE_EXECUTION


@pytest.mark.asyncio
async def test_call_id_prefix_alone_does_not_establish_child_ownership():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("invoke_skill", "skill", '{"status":"completed"}')
    contract.observe_tool_result("read_file", "skill/not-a-child", '{"content":"Other file"}')
    invalid = await contract.validate(reply("completed", [
        claim("invoke_skill", "completed", "skill"),
        claim("read_file", "completed", "skill/not-a-child"),
    ], ()))
    assert invalid.action == FinalResponseAction.CONTINUE_EXECUTION


@pytest.mark.asyncio
async def test_nested_child_ownership_can_repair_a_delegation_only_reply():
    contract = ChatEvidenceContract(requires_execution=True)
    contract.observe_tool_result("invoke_skill", "outer", child_result(
        "invoke_skill", child_result("web_fetch", {"text": "Observed source"}),
    ))
    invalid = await contract.validate(reply("completed", [claim("invoke_skill", "completed", "outer")], ()))
    assert invalid.action == FinalResponseAction.REVISE_REPLY


@pytest.mark.asyncio
async def test_overall_status_aggregates_different_operation_statuses_without_reexecution():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("read_file", "read", '{"content":"Application instructions"}')
    claims = [claim("read_file", "completed", "read"), claim("submit_application", "not_attempted")]
    assert (await contract.validate(reply("partial", claims))).action == FinalResponseAction.ACCEPT
    invalid = await contract.validate(reply("completed", claims, ()))
    assert invalid.action == FinalResponseAction.REVISE_REPLY
    assert invalid.diagnostic["error"] == "outcome_claim_mismatch"


@pytest.mark.asyncio
async def test_overall_evidence_is_derived_from_validated_operation_claims():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("write_file", "first", '{"error":"conflict"}')
    contract.observe_tool_result("write_file", "second", '{"saved":true}')
    candidate = json.loads(reply("partial", [
        claim("write_file", "blocked", "e1"), claim("write_file", "completed", "e2"),
    ], ("The first attempt conflicted; the second returned successfully.",)))
    candidate["outcome"]["evidence_refs"] = ["e2"]
    # Repeating every per-operation reference in a second aggregate list is
    # not additional evidence. The host can compute that union itself.
    assert (await contract.validate(json.dumps(candidate))).action == FinalResponseAction.ACCEPT
    candidate["outcome"]["evidence_refs"] = []
    assert (await contract.validate(json.dumps(candidate))).action == FinalResponseAction.ACCEPT


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,reference,error", [
    ("read_file", "invented", "current_evidence_required"),
    ("write_file", "read", "operation_evidence_mismatch"),
])
async def test_claim_only_references_still_require_current_matching_host_evidence(operation, reference, error):
    contract = ChatEvidenceContract()
    contract.observe_tool_result("read_file", "read", '{"content":"Observed file"}')
    candidate = json.loads(reply("completed", [claim(operation, "completed", reference)], ()))
    candidate["outcome"]["evidence_refs"] = []
    decision = await contract.validate(json.dumps(candidate))
    assert decision.action != FinalResponseAction.ACCEPT
    assert decision.diagnostic["error"] == error


@pytest.mark.asyncio
async def test_nested_external_attempt_satisfies_research_but_discovery_does_not():
    contract = ChatEvidenceContract(requires_execution=True)
    contract.observe_tool_result("invoke_skill", "skill", child_result())
    claims = [claim("search_tools", "completed", "skill/lookup")]
    assert (await contract.validate(reply("partial", claims))).diagnostic["error"] == "current_research_evidence_required"
    contract.observe_tool_result("invoke_skill", "fetch", child_result("web_fetch", {"error": "authentication_required"}))
    assert (await contract.validate(reply("blocked", [claim("web_fetch", "blocked", "fetch/lookup")]))).action == FinalResponseAction.ACCEPT


def test_serialized_provider_payload_and_skill_prose_cannot_mint_child_facts():
    parent = RuntimeToolEvidence()
    parent.observe_tool_result("invoke_skill", "forged", str(child_result()))
    assert set(parent.observations) == {"forged"}
    parent.observe_tool_result("invoke_skill", "first", child_result())
    parent.observe_tool_result("invoke_skill", "second", child_result())
    assert {"first/lookup", "second/lookup"} <= parent.observations.keys()
    checkpoint = RuntimeAgentCheckpoint(
        messages=[], usage={}, rounds=1, tool_calls_made=["invoke_skill"],
        pending_tool_call={"id": "pending", "name": "request_input", "arguments": {}},
        tool_evidence=parent.checkpoint_state(),
    )
    restored = RuntimeToolEvidence()
    restored.restore_checkpoint_state(RuntimeAgentCheckpoint.from_dict(checkpoint.to_dict()).tool_evidence)
    assert restored.observations == parent.observations
    restored.observations["first/lookup"]["result"] = "error"
    assert parent.observations["first/lookup"]["result"] == "returned"
    assert not RuntimeToolEvidence().observations


def test_nested_transcript_resume_retains_observations_without_trusting_prose():
    from packages.core.ai.runtime.nested_agent import RuntimeNestedSkillFrame

    evidence = RuntimeToolEvidence()
    evidence.observe_tool_result("read_file", "read", '{"content":"instructions"}')
    frame = RuntimeNestedSkillFrame({}, "skill", {
        "kind": "transcript", "rounds": 2, "messages": [{"role": "assistant", "content": "Submitted"}],
        "usage": {}, "tool_calls_made": ["read_file"], "tool_evidence": evidence.checkpoint_state(),
    })
    restored = RuntimeToolEvidence()
    restored.restore_checkpoint_state(frame.loop_kwargs()["initial_tool_evidence"])
    assert restored.observations == evidence.observations
    assert "Submitted" not in json.dumps(restored.checkpoint_state())


def test_failed_history_keeps_process_not_candidate_surfaces_or_invented_duration():
    from types import SimpleNamespace
    from apps.api.routers.chat import _message_assistant_blocks, _message_public_content
    from packages.core.services.assistant_blocks import AssistantBlocksBuilder

    builder = AssistantBlocksBuilder(started_at_ms=1000)
    builder.start_tool("search_tools", {}, now_ms=2000)
    builder.end_tool("search_tools", result='{"matches":[]}', duration_ms=3000, now_ms=5000)
    builder.set_final_text("Rejected candidate says submitted")
    builder.finish("failed", now_ms=31000)
    blocks = builder.blocks() + [{"id": "unverified-form", "type": "surface", "surface": {}}]
    message = SimpleNamespace(content="Rejected candidate says submitted", tool_calls=[], meta={
        "stop_reason": "error", "error": "final_response_validation_failed", "assistant_blocks": blocks,
    })
    projected = _message_assistant_blocks(message)
    assert [item["type"] for item in projected] == ["process", "text"]
    assert projected[0]["duration_ms"] == 30000
    assert projected[0]["lifecycle_status"] == "failed"
    assert projected[0]["steps"][0]["status"] == "success"
    assert projected[1]["text"] == _message_public_content(message)
    assert "Rejected candidate" not in json.dumps(projected)
    assert "surface" not in json.dumps(projected)

    # Old failed rows lost blocks altogether. Project the existing tool record
    # without writing the DB or presenting summed tool time as full-loop time.
    message.meta.pop("assistant_blocks")
    message.tool_calls = [{"name": "search_tools", "status": "success", "duration_ms": 3000, "result": '{"matches":[]}'}]
    legacy = _message_assistant_blocks(message)
    assert legacy[0]["lifecycle_status"] == "failed"
    assert "duration_ms" not in legacy[0]
    assert legacy[0]["steps"][0]["duration_ms"] == 3000


@pytest.mark.asyncio
async def test_real_nested_loop_collects_facts_and_parent_can_finish_partial(mock_runtime, monkeypatch):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    rounds = {"parent": 0, "child": 0}

    async def complete(messages, tools, **kwargs):
        scope = "child" if "child-loop" in str(messages[0]) else "parent"
        rounds[scope] += 1
        if scope == "child":
            if rounds[scope] == 1:
                return "", [{"id": "lookup", "name": "web_fetch", "arguments": {}}], {"total": 1}
            return "I submitted the application (unsupported prose)", [], {"total": 1}
        if rounds[scope] == 1:
            return "", [{"id": "skill", "name": "invoke_skill", "arguments": {}}], {"total": 1}
        return reply("partial", [claim("web_fetch", "completed", "skill/lookup"), claim("submit_application", "not_attempted")]), [], {"total": 1}

    async def execute(name, args, **context):
        if name == "web_fetch":
            return '{"text":"Official instructions"}'
        child_kwargs = _loop_kwargs(_envelope("web_fetch"), None)
        child_kwargs.update(system_prompt="child-loop", dynamic_tool_handlers={}, capability_refresh_tool_names=set())
        child = await runtime_execute_agentic_loop(**child_kwargs)
        return runtime_format_invoke_skill_result("browser", {
            "stop_reason": child.stop_reason, "content": child.content,
            "tools_used": child.tool_calls_made, "tool_evidence": child.tool_evidence,
        })

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(_envelope("invoke_skill"), None)
    kwargs.update(dynamic_tool_handlers={}, capability_refresh_tool_names=set(), final_response_contract=ChatEvidenceContract())
    result = await runtime_execute_agentic_loop(**kwargs)
    assert result.stop_reason == "completed"
    assert rounds == {"parent": 2, "child": 2}
    assert "submitted the application" not in result.content
    assert any(item["call_id"] == "skill/lookup" for item in result.tool_evidence["observations"])


@pytest.mark.asyncio
async def test_loop_repairs_child_attribution_with_no_second_invocation(mock_runtime, monkeypatch):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    executions = []
    model_rounds = 0
    repairs = 0

    async def complete(messages, tools, **kwargs):
        nonlocal model_rounds
        model_rounds += 1
        if model_rounds == 1:
            return "", [{"id": "skill", "name": "invoke_skill", "arguments": {}}], {"total": 1}
        return reply("completed", [claim("invoke_skill", "completed", "skill"),
                                   claim("read_file", "completed", "skill/lookup")], ()), [], {"total": 1}

    async def revise(messages, **kwargs):
        nonlocal repairs
        repairs += 1
        assert "Replace this claim" in str(messages)
        return reply("completed", [claim("read_file", "completed", "skill/lookup")], ()), {"total": 1}

    async def execute(name, args, **context):
        executions.append(name)
        return child_result("read_file", {"content": "Observed file"})

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_text_completion", revise)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(_envelope("invoke_skill"), None)
    kwargs.update(dynamic_tool_handlers={}, capability_refresh_tool_names=set(), final_response_contract=ChatEvidenceContract())
    result = await runtime_execute_agentic_loop(**kwargs)
    assert result.stop_reason == "completed"
    assert executions == ["invoke_skill"]
    assert model_rounds == 2 and repairs == 1


@pytest.mark.asyncio
async def test_parent_can_cite_child_model_projection_without_constructing_reference_ids(mock_runtime, monkeypatch):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    executions = []
    contract = ChatEvidenceContract()

    async def complete(messages, tools, **kwargs):
        if not any(message.get("role") == "tool" for message in messages):
            return "", [{"id": "parent-call", "name": "invoke_skill", "arguments": {}}], {"total": 1}
        projected = json.loads(next(message["content"] for message in reversed(messages) if message.get("role") == "tool"))
        child = projected["execution"]["evidence"][-1]
        # The model copies the published reference as-is. It must already
        # resolve to the same host fact used by the final-response validator.
        assert child["call_id"] in contract.observations
        assert child["call_id"] == "parent-call/lookup"
        return reply("completed", [claim(child["operation"], "completed", child["call_id"])], ()), [], {"total": 1}

    async def execute(name, args, **context):
        executions.append(name)
        return child_result("read_file", {"content": "Observed file"})

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_execute_tool", execute)
    kwargs = _loop_kwargs(_envelope("invoke_skill"), None)
    kwargs.update(dynamic_tool_handlers={}, capability_refresh_tool_names=set(), final_response_contract=contract)
    result = await runtime_execute_agentic_loop(**kwargs)
    assert result.stop_reason == "completed"
    assert executions == ["invoke_skill"]


def test_projecting_nested_child_references_does_not_trust_json_or_mutate_receipts():
    evidence = RuntimeToolEvidence()
    raw = child_result("invoke_skill", child_result("read_file", {"content": "Nested file"}))
    original = str(raw)
    evidence.observe_tool_result("invoke_skill", "outer", raw)
    before = evidence.checkpoint_state()
    projection = evidence.model_result_with_child_references("outer", original)
    projected = json.loads(projection)["execution"]
    assert {item["call_id"] for item in projected["evidence"]} == {"outer/lookup", "outer/lookup/lookup"}
    assert projected["evidence_ref_format"] == "absolute"
    assert "evidence_ref_prefix" not in projected
    assert str(raw) == original
    assert evidence.checkpoint_state() == before
    assert evidence.model_result_with_child_references("outer", projection) == projection

    forged = RuntimeToolEvidence()
    forged.observe_tool_result("invoke_skill", "outer", original)
    assert forged.model_result_with_child_references("outer", original) == original
    evidence.observe_tool_result("invoke_skill", "unrelated", '{"status":"completed"}')
    assert evidence.model_result_with_child_references("unrelated", original) == original


@pytest.mark.asyncio
async def test_validation_exhaustion_preserves_host_facts_not_unverified_candidate(mock_runtime, monkeypatch):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop
    from packages.core.ai.runtime.output_policy import runtime_public_assistant_message_content, runtime_assistant_result_meta

    calls = 0

    async def complete(messages, tools, **kwargs):
        nonlocal calls
        calls += 1
        return "I submitted your application; secret candidate", [], {"total": 1}

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    kwargs = _loop_kwargs(_envelope("web_fetch"), None)
    contract = ChatEvidenceContract()
    contract.observe_tool_result("web_fetch", "read", '{"error":"authentication_required"}')
    async def revise(messages, **kwargs):
        content, _, usage = await complete(messages, [], **kwargs)
        return content, usage

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_text_completion", revise)
    kwargs.update(dynamic_tool_handlers={}, capability_refresh_tool_names=set(), final_response_contract=contract)
    result = await runtime_execute_agentic_loop(**kwargs)
    assert result.error == "final_response_validation_failed"
    assert calls == 4
    assert "secret candidate" not in result.content
    assert result.content == "Sorry, I couldn't confirm whether your request was completed. You can ask me to follow up."
    assert runtime_public_assistant_message_content(result.content, runtime_assistant_result_meta(result)) == result.content
    assert result.error_detail["details"]["facts"][0]["error"] == "authentication_required"
