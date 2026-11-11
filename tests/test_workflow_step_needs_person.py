"""An Agent step missing what only a person can give stops the run, not the approval after it."""
import json
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from packages.core.ai import workflow_runner as runner_module
from packages.core.ai.agentic_loop import _detect_terminal_tool_result
from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.models.workflow import WorkflowRun
from packages.core.services.workflow_step_needs_person import (
    STEP_NEEDS_PERSON_TERMINAL_POLICY,
    STEP_NEEDS_PERSON_TOOL_NAME,
)
from tests.auth_helpers import register_user_and_get_token

ASK = {"missing": "Gmail is not connected, so the thread cannot be read.", "ask": "Connect Gmail."}


def _loop_result(content="done", evidence=None):
    return SimpleNamespace(content=content, usage={}, tool_calls_made=[], rounds=1, stop_reason="completed",
                           error=None, error_detail=None, tool_evidence=evidence)


def _stub_appendix(monkeypatch):
    async def appendix(*args, **kwargs):
        schema = {"type": "function", "function": {"name": "read_file"}}
        return SimpleNamespace(prompt_appendix="", tool_schemas=[schema],
                               allowed_tool_names={"read_file"}, envelope=None)

    monkeypatch.setattr(runner_module, "runtime_prepare_prompt_appendix_for_turn", appendix)


def test_the_call_ends_the_agent_loop_only_when_it_says_what_is_needed():
    call = {"name": STEP_NEEDS_PERSON_TOOL_NAME}
    stopped = _detect_terminal_tool_result(
        [(call, json.dumps({"ok": True, "status": "stopped"}))], STEP_NEEDS_PERSON_TERMINAL_POLICY, "task",
    )
    assert stopped and stopped["stop_reason"] == "workflow_step_needs_person"
    refused = json.dumps({"error": "Say what is missing and what the person should do."})
    assert _detect_terminal_tool_result([(call, refused)], STEP_NEEDS_PERSON_TERMINAL_POLICY, "task") is None


@pytest.mark.parametrize("expects", ["files", "effects"])
@pytest.mark.asyncio
async def test_a_stopped_step_owes_neither_its_outputs_nor_its_expects(monkeypatch, expects):
    """A customer reply that saves files and a publishing step that takes an action."""
    _stub_appendix(monkeypatch)
    requests = []

    async def loop(**kwargs):
        assert STEP_NEEDS_PERSON_TOOL_NAME in kwargs["allowed_tool_names"]
        assert kwargs["terminal_tool_result_policy"] == STEP_NEEDS_PERSON_TERMINAL_POLICY
        assert STEP_NEEDS_PERSON_TOOL_NAME in kwargs["system_prompt"]
        handler = kwargs["dynamic_tool_handlers"][STEP_NEEDS_PERSON_TOOL_NAME]
        assert "error" in json.loads(await handler({"missing": "", "ask": ""}))
        assert json.loads(await handler(ASK))["ok"] is True
        requests.append(ASK)
        return _loop_result(evidence={"receipts": [], "effects": [], "effect_state": "none"})

    monkeypatch.setattr(runner_module, "runtime_execute_workflow_agent_loop", loop)
    step = {"id": "work", "type": "agent", "config": {
        "prompt": "Work", "expects": [expects],
        "output_schema": {"type": "object", "properties": {"result_document_id": {"type": "string"}},
                          "required": ["result_document_id"]},
    }}
    run = WorkflowRun(id="run", entity_id="entity", variables={}, step_results={})

    result = await WorkflowRunner()._execute_step_safe(step, run, None)

    assert result["status"] == "completed"
    assert result["needs_person"] == ASK
    assert result["retry_state"]["retry_from_step_id"] == "work"
    assert result["retry_state"]["required_change"] == "Connect Gmail."

    stage = {"id": "stage", "type": "stage", "config": {
        "entry_operation_id": "work", "operations": [{**step, "next": ["exit"]}], "routes": {"exit": None},
    }}
    runner = WorkflowRunner()
    nested = await runner._execute_step_safe(stage, run, None)
    assert nested["needs_person"] == ASK
    assert nested["retry_state"]["retry_from_step_id"] == "stage"
    # A replacement owner of the same run reuses its durable stop receipt.
    assert (await runner._execute_step_safe(stage, run, None))["needs_person"] == ASK
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_a_step_that_already_acted_outward_is_not_offered_a_replay(monkeypatch):
    """Retry runs a stopped step again, which could send the same message twice."""
    _stub_appendix(monkeypatch)

    async def loop(**kwargs):
        await kwargs["dynamic_tool_handlers"][STEP_NEEDS_PERSON_TOOL_NAME](ASK)
        result = _loop_result(evidence=evidence)
        if evidence["effect_state"] == "unconfirmed":
            result.control = {"effect_state": "unconfirmed", "do_not_replay": True}
        return result

    monkeypatch.setattr(runner_module, "runtime_execute_workflow_agent_loop", loop)
    step = {"id": "send", "type": "agent", "config": {"prompt": "Send", "expects": ["effects"]}}
    run = WorkflowRun(id="run", entity_id="entity", variables={}, step_results={})

    # An uncertain call deliberately has no confirmed outward-effect receipt.
    for evidence in (
        {"receipts": [], "effects": [{"category": "communication"}], "effect_state": "confirmed"},
        {"receipts": [], "effects": [], "effect_state": "unconfirmed"},
    ):
        result = await WorkflowRunner()._execute_step_safe(step, run, None)
        assert result["status"] == "failed"
        assert result["do_not_replay"] is True and result["retry_blocked"] is True
        assert "needs_person" not in result


async def _start_reply_flow(client: AsyncClient, username: str, *, sibling: bool = False,
                            inline_stage: bool = False) -> tuple[dict, str]:
    response = await register_user_and_get_token(client, json={
        "username": username, "email": f"{username}@test.com",
        "password": "WorkflowApprovalTest123!", "entity_name": "Workflow Corp",
    })
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    steps = [
        {"id": "start", "type": "trigger", "next": ["draft", "peer"] if sibling else ["draft"]},
        {"id": "draft", "type": "agent", "config": {"prompt": "Draft the reply"}, "next": ["approve"]},
        {"id": "approve", "type": "wait", "config": {
            "wait_type": "approval", "message": "Approve this exact reply?", "options": ["approve", "revise"],
        }, "next": ["send"]},
        {"id": "send", "type": "agent", "config": {"prompt": "Send it"}, "next": ["end"]},
        *([{"id": "peer", "type": "agent", "config": {"prompt": "Independent work"}, "next": ["end"]}]
          if sibling else []),
        {"id": "end", "type": "end", "next": []},
    ]
    if inline_stage:
        steps[1] = {"id": "draft", "type": "stage", "config": {
            "entry_operation_id": "prepare", "operations": [
                {"id": "prepare", "type": "agent", "config": {"prompt": "Prepare the input"}, "next": ["make_reply"]},
                {"id": "make_reply", "type": "agent", "config": {"prompt": "Draft the reply"}, "next": ["ready"]},
            ], "routes": {"ready": "approve"},
        }, "next": ["approve"]}
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Customer reply", "steps": steps,
    })).json()
    run = (await client.post(f"/api/v1/workflows/{workflow['id']}/run", headers=headers,
                             json={"execute": False})).json()
    return headers, run["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("inline_stage", [False, True], ids=["agent", "stage"])
async def test_the_run_stops_before_the_approval_and_retry_reaches_it(
    client: AsyncClient, db_session, monkeypatch, inline_stage,
):
    _stub_appendix(monkeypatch)
    prompts: list[str] = []
    connected = False

    async def loop(**kwargs):
        prompts.append(kwargs["user_message"])
        if not connected and "Prepare the input" not in kwargs["user_message"]:
            await kwargs["dynamic_tool_handlers"][STEP_NEEDS_PERSON_TOOL_NAME](ASK)
        return _loop_result(content="The reply")

    monkeypatch.setattr(runner_module, "runtime_execute_workflow_agent_loop", loop)
    headers, run_id = await _start_reply_flow(client, "wfneedsperson", sibling=True, inline_stage=inline_stage)

    await WorkflowRunner().run(run_id)

    stopped = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert stopped["status"] == "completed", stopped.get("error")
    assert stopped["current_step_id"] == "draft"
    assert set(stopped["step_results"]) == {"start", "draft"}
    assert len(prompts) == (2 if inline_stage else 1)
    row = await db_session.get(WorkflowRun, run_id)
    await db_session.refresh(row)
    assert row.variables["__result"] == {
        "status": "needs_input", "summary": ASK["missing"], "next_step": ASK["ask"],
    }

    connected = True
    retried = await client.post(f"/api/v1/workflows/runs/{run_id}/retry", headers=headers,
                                json={"execute": False})
    assert retried.status_code == 201, retried.text
    retry_id = retried.json()["id"]

    await WorkflowRunner().run(retry_id)

    reached = (await client.get(f"/api/v1/workflows/runs/{retry_id}", headers=headers)).json()
    assert reached["status"] == "paused", reached.get("error")
    assert reached["current_step_id"] == "approve"
    assert "needs_person" not in reached["step_results"]["draft"]
    assert len(prompts) == (4 if inline_stage else 3)
    if inline_stage:
        assert sum("Prepare the input" in prompt for prompt in prompts) == 1


@pytest.mark.asyncio
async def test_a_child_run_stops_its_parent_through_the_failure_it_already_reads(
    client: AsyncClient, db_session, monkeypatch,
):
    _stub_appendix(monkeypatch)

    async def loop(**kwargs):
        await kwargs["dynamic_tool_handlers"][STEP_NEEDS_PERSON_TOOL_NAME](ASK)
        return _loop_result()

    monkeypatch.setattr(runner_module, "runtime_execute_workflow_agent_loop", loop)
    headers, run_id = await _start_reply_flow(client, "wfneedspersonchild")
    row = await db_session.get(WorkflowRun, run_id)
    row.trigger_data = {**(row.trigger_data or {}), "parent_run_id": "missing-parent", "parent_step_id": "child"}
    await db_session.commit()

    await WorkflowRunner().run(run_id)

    await db_session.refresh(row)
    assert row.status == "failed"
    assert row.error == ASK["missing"]
    assert "__result" not in (row.variables or {})


def test_a_saved_file_expectation_message_is_not_a_reason_to_wait_for_a_connection():
    from packages.core.services.workflow_connection_wait import _evidence

    assert _evidence({"code": "workflow_step_expectation_unmet", "error": "Step finished without a saved file."}) == ""
    assert _evidence({"code": "workflow_step_expectation_unmet", "error": "x", "output": "Not found"}) == "Not found"
    assert _evidence({"error": "Shopify is down"}) == "Shopify is down"

