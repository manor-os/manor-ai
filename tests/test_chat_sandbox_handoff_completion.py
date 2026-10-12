"""A sandbox skill handoff is the agent's to finish within the same turn.

Incident (staging, 2026-10-04): "给我写个PPT" → generate_file returned the pptx
``sandbox_ready`` handoff → the model answered ``partial`` ("has started but
has not produced a verified downloadable file yet") without a single sandbox
call, the contract accepted it with 97 of 100 rounds left, and the run was
marked completed. Nothing was running afterwards. A handoff is not progress to
report: until the deliverable is saved or a step actually fails, the turn must
not return to the user.
"""

from __future__ import annotations

import json

import pytest

from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
from packages.core.ai.runtime.final_response import FinalResponseAction, FinalResponseContext

HANDOFF = json.dumps({
    "status": "sandbox_ready",
    "skill": "pptx",
    "content": "✓ Sandbox ready for skill **pptx**\n\nsandbox_id: 998b86cf3c11",
    "sandbox_id": "998b86cf3c11",
    "loaded_tools": ["sandbox"],
})
EXEC_OK = "initialized henry_lee_cases\n[exit_code: 0]"
SAVED_FINAL = json.dumps({
    "saved": True, "saved_to_knowledge": True, "display_as_artifact": True, "artifact_role": "final",
    "document_id": "doc_1", "name": "李昌钰_案件介绍.pptx", "fs_path": "Documents/李昌钰_案件介绍.pptx",
})
SAVED_INTERMEDIATE = json.dumps({
    "saved": True, "saved_to_knowledge": False, "display_as_artifact": False, "artifact_role": "intermediate",
    "name": "outline.json", "fs_path": "Documents/outline.json", "result_url": "/api/v1/fs/e/Documents/outline.json",
})
SCRIPT_FAILED = json.dumps({"ok": False, "error": {
    "code": "sandbox_execution_failed", "message": "Sandbox exec failed: export.py missing",
}})


def answer(status, refs=(), limitations=(), *, operation="generate_file", claim_status=None):
    return json.dumps({"kind": "answer", "text": "回复", "outcome": {
        "status": status, "evidence_refs": list(refs), "limitations": list(limitations),
        "claims": [{"operation": operation, "status": claim_status or status, "evidence_refs": list(refs)}]
        if status in {"completed", "partial", "blocked"} else [],
    }})


def _handed_off(tool: str = "generate_file", arguments: dict | None = None) -> ChatEvidenceContract:
    contract = ChatEvidenceContract()
    contract.observe_tool_result(tool, "handoff", HANDOFF, arguments or {"kind": "presentation"})
    return contract


def _sandbox(contract: ChatEvidenceContract, call_id: str, action: str, result: str, **params) -> None:
    contract.observe_tool_result(
        "sandbox", call_id, result, {"action": action, "params": {"sandbox_id": "998b86cf3c11", **params}},
    )


@pytest.mark.asyncio
async def test_incident_partial_right_after_handoff_must_continue_in_the_same_turn():
    contract = _handed_off()

    decision = await contract.validate(answer("partial", ["handoff"], ["No final verified PPTX file is available yet."]))

    assert decision.action == FinalResponseAction.CONTINUE_EXECUTION
    assert decision.diagnostic["error"] == "sandbox_handoff_undelivered"
    message = decision.diagnostic["message"]
    assert "pptx" in message and "998b86cf3c11" in message and "save_result" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "answered", "not_attempted"])
async def test_no_status_closes_an_undelivered_handoff(status):
    contract = _handed_off()

    decision = await contract.validate(answer(status, ["handoff"] if status == "completed" else [], ["pending"]))

    assert decision.action == FinalResponseAction.CONTINUE_EXECUTION
    assert decision.diagnostic["error"] == "sandbox_handoff_undelivered"


@pytest.mark.asyncio
async def test_successful_steps_without_a_saved_result_are_still_mid_way():
    contract = _handed_off()
    _sandbox(contract, "init", "exec", EXEC_OK, command="python3 scripts/project_manager.py init deck")
    _sandbox(contract, "outline", "save_result", SAVED_INTERMEDIATE, file_path="/skill/outline.json",
             filename="outline.json", artifact_role="intermediate")

    decision = await contract.validate(answer("partial", ["init"], ["Deck not exported yet"], operation="sandbox_exec"))

    assert decision.diagnostic["error"] == "sandbox_handoff_undelivered"


@pytest.mark.asyncio
async def test_saving_the_final_artifact_delivers_the_handoff():
    contract = _handed_off()
    _sandbox(contract, "init", "exec", EXEC_OK, command="python3 scripts/project_manager.py init deck")
    _sandbox(contract, "save", "save_result", SAVED_FINAL, file_path="/skill/exports/deck.pptx",
             filename="李昌钰_案件介绍.pptx", artifact_role="final")

    decision = await contract.validate(answer("completed", ["save"], operation="sandbox_save_result"))

    assert decision.action == FinalResponseAction.ACCEPT


@pytest.mark.asyncio
async def test_a_file_registered_in_knowledge_is_delivered_even_without_the_final_role():
    """E2E finding (xlsx/pdf, 2026-10-04): the model saved the deliverable without
    artifact_role, the tool defaulted it to "intermediate" but still registered it in
    Knowledge, and the guard sent the turn back. The model re-opened the sandbox and
    saved a duplicate (2026年度销售预算_1.xlsx). A file the user can open is delivered."""

    contract = _handed_off()
    registered = json.loads(SAVED_FINAL)
    registered.update(artifact_role="intermediate", display_as_artifact=False)
    _sandbox(contract, "save", "save_result", json.dumps(registered), file_path="/skill/exports/deck.pptx",
             filename="李昌钰_案件介绍.pptx")

    decision = await contract.validate(answer("completed", ["save"], operation="sandbox_save_result"))

    assert decision.action == FinalResponseAction.ACCEPT


@pytest.mark.asyncio
async def test_an_observed_failure_lets_the_agent_report_the_blocker_honestly():
    contract = _handed_off()
    _sandbox(contract, "export", "exec", SCRIPT_FAILED, command="python3 scripts/export.py")

    blocked = await contract.validate(answer("blocked", ["export"], ["export.py is missing from the skill bundle"],
                                             operation="sandbox_exec"))
    assert blocked.action == FinalResponseAction.ACCEPT

    # A failure is a reason to explain, never to claim the deck exists.
    claimed = await contract.validate(answer("completed", ["handoff"]))
    assert claimed.action == FinalResponseAction.CONTINUE_EXECUTION
    assert claimed.diagnostic["error"] == "sandbox_handoff_undelivered"


@pytest.mark.asyncio
async def test_invoke_skill_handoff_is_held_to_the_same_rule():
    contract = _handed_off("invoke_skill", {"skill": "pptx"})

    decision = await contract.validate(answer("answered"))

    assert decision.diagnostic["error"] == "sandbox_handoff_undelivered"


@pytest.mark.asyncio
async def test_pending_handoff_survives_the_durable_resume_checkpoint():
    """Sandbox admission suspends the run; the handoff lands on the resumed side."""

    contract = _handed_off()
    resumed = ChatEvidenceContract()
    resumed.restore_checkpoint_state(json.loads(json.dumps(contract.checkpoint_state())))

    decision = await resumed.validate(answer("partial", ["handoff"], ["not built"]))

    assert decision.diagnostic["error"] == "sandbox_handoff_undelivered"


@pytest.mark.asyncio
async def test_exhausted_round_budget_still_ends_the_turn():
    """When the loop can no longer execute, the honest partial stands."""

    contract = _handed_off()

    decision = await contract.validate_with_context(
        answer("partial", ["handoff"], ["Ran out of steps before exporting the deck"]),
        FinalResponseContext(allow_execution=False, stop_reason="max_rounds"),
    )

    assert decision.action == FinalResponseAction.ACCEPT
    assert decision.stop_reason == "max_rounds"


@pytest.mark.asyncio
async def test_turn_without_a_handoff_is_unaffected():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("web_search", "lookup", '{"results":[]}')

    decision = await contract.validate(answer("partial", ["lookup"], ["No source found"], operation="web_search"))

    assert decision.action == FinalResponseAction.ACCEPT


def test_handoff_contract_tells_the_agent_to_finish_before_replying():
    from types import SimpleNamespace

    from packages.core.services.skill_service import _sandbox_skill_runtime_contract

    text = _sandbox_skill_runtime_contract(
        skill=SimpleNamespace(name="pptx"), sandbox_id="sbx", skill_info_parts=[],
        expected_workspace_volume=None, skill_instructions="", skill_input="",
    )
    assert "same turn" in text and "save_result" in text


def test_handoff_contract_asks_for_step_by_step_planning_kept_private():
    from types import SimpleNamespace

    from packages.core.services.skill_service import _sandbox_skill_runtime_contract

    text = _sandbox_skill_runtime_contract(
        skill=SimpleNamespace(name="pptx"), sandbox_id="sbx", skill_info_parts=[],
        expected_workspace_volume=None, skill_instructions="", skill_input="",
    )
    contract = text.split("## Sandbox Environment")[0]
    assert "Think step by step" in contract
    # The plan covers the whole route to the saved file, and stays out of user-visible text.
    assert "save_result" in contract.split("Think step by step")[1]
    assert "private" in contract.split("Think step by step")[1]


def _loop_tools():
    return [
        {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
        for name in ("generate_file", "sandbox")
    ]


async def _tool(name, args):
    if name == "generate_file":
        return HANDOFF
    action = args.get("action")
    if action == "exec":
        return EXEC_OK
    if action == "save_result":
        return SAVED_FINAL
    raise AssertionError(f"unexpected tool call {name} {args}")


@pytest.mark.asyncio
async def test_loop_replay_premature_partial_is_sent_back_and_the_deck_gets_built(monkeypatch):
    import packages.core.ai.agentic_loop as loop

    seen_feedback = []
    rounds = 0

    async def completion(messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            return json.dumps({"kind": "progress", "text": "我来制作 PPT。"}), [
                {"id": "handoff", "name": "generate_file", "arguments": {"kind": "presentation"}},
            ], {}
        if rounds == 2:
            # The incident's reply: report the handoff as progress and stop.
            return answer("partial", ["handoff"], ["No final verified PPTX file is available yet."]), None, {}
        if rounds == 3:
            seen_feedback.append("sandbox_handoff_undelivered" in json.dumps(messages, ensure_ascii=False))
            return json.dumps({"kind": "progress", "text": "继续制作。"}), [{
                "id": "init", "name": "sandbox",
                "arguments": {"action": "exec", "params": {"sandbox_id": "998b86cf3c11", "command": "init"}},
            }], {}
        if rounds == 4:
            return json.dumps({"kind": "progress", "text": "保存文件。"}), [{
                "id": "save", "name": "sandbox",
                "arguments": {"action": "save_result", "params": {
                    "sandbox_id": "998b86cf3c11", "file_path": "/skill/exports/deck.pptx",
                    "filename": "deck.pptx", "artifact_role": "final",
                }},
            }], {}
        return answer("completed", ["save"], operation="sandbox_save_result"), None, {}

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", completion)
    result = await loop.agentic_loop(
        system_prompt="system", user_message="给我写个PPT", tools=_loop_tools(),
        tool_executor=_tool, max_rounds=20, final_response_contract=ChatEvidenceContract(),
    )

    assert seen_feedback == [True]
    assert result.stop_reason == "completed"
    assert result.content == "回复"
    assert result.tool_calls_made == ["generate_file", "sandbox", "sandbox"]
    assert rounds == 5


@pytest.mark.asyncio
async def test_loop_never_marks_an_abandoned_handoff_as_completed(monkeypatch):
    import packages.core.ai.agentic_loop as loop

    rounds = 0

    async def completion(messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            return "", [{"id": "handoff", "name": "generate_file", "arguments": {"kind": "presentation"}}], {}
        return answer("partial", ["handoff"], ["Not built yet"]), None, {}

    monkeypatch.setattr(loop, "runtime_execute_agentic_round_tool_completion", completion)
    result = await loop.agentic_loop(
        system_prompt="system", user_message="给我写个PPT", tools=_loop_tools(),
        tool_executor=_tool, max_rounds=20, final_response_contract=ChatEvidenceContract(),
    )

    assert result.stop_reason != "completed"
    assert rounds < 20


def test_manor_agent_tool_guidance_asks_for_step_by_step_work_through_handoffs():
    """The Manor agent's own runtime prompt carries the same rule, not only the handoff text."""

    from packages.core.ai.runtime.prompt_guidance import runtime_tool_usage_guidance

    guidance = runtime_tool_usage_guidance(tool_names=("generate_file", "sandbox"), has_tools=True)

    assert "Think step by step" in guidance
    step = guidance.split("Think step by step")[1].split("\n- ")[0]
    assert "private" in step
    assert "sandbox_ready" in step and "same turn" in step
