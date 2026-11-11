"""request_input: suspend the turn on a question and resume with the answer."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.control import (
    HUMAN_INPUT_SUSPENSION_KIND,
    HUMAN_INPUT_SUSPENSION_REASON,
    RuntimeAgentCheckpoint,
    RuntimeToolSuspension,
    is_human_input_suspension_control,
)
from packages.core.ai.runtime.human_input import REQUEST_INPUT_JUSTIFICATION_SCHEMA
from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError
from packages.core.ai.tools import request_input_tool
from packages.core.ai.tools.request_input_tool import (
    human_input_answer_result,
    normalize_input_request,
)


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object", "properties": {}}},
    }


def _justification(kind: str = "human_decision", **extra) -> dict:
    return {
        "kind": kind,
        "why_human_required": "Only the user can choose which of the two suitable units they prefer.",
        "attempted_resolution": [{"source": "conversation", "finding": "The request lists two suitable units and no preference."}],
        **extra,
    }


#: Every new request_input call recommends one answer with a reason.
_REC = {"recommended": "4C", "recommendation_reason": "It is the cheaper of the two."}


# ── payload shaping ───────────────────────────────────────────────────────


def test_normalize_trims_dedupes_and_bounds_the_question() -> None:
    payload = normalize_input_request(
        question="  Which unit?  ",
        options=["4C", "4C", "", "5A"],
        fields=[
            {"name": "budget", "type": "number"},
            {"name": "budget", "type": "text"},  # duplicate name dropped
            {"label": "Move-in date", "type": "date", "required": False},
            {"name": "tier", "type": "select", "options": ["gold", "silver"]},
            "not-a-dict",
        ],
        context="  We found two matching units.  ",
    )
    assert payload["question"] == "Which unit?"
    assert payload["options"] == ["4C", "5A"]
    assert [f["name"] for f in payload["fields"]] == ["budget", "Move-in date", "tier"]
    assert payload["fields"][1] == {
        "name": "Move-in date", "label": "Move-in date", "type": "date", "required": False,
        # The form draws its control from this schema, the same set as a Workflow run form.
        "schema": {"type": "string", "format": "date", "title": "Move-in date"},
    }
    assert payload["fields"][2]["options"] == ["gold", "silver"]
    assert payload["context"] == "We found two matching units."


def test_a_file_field_asks_for_a_document_instead_of_a_wait_option() -> None:
    """An agent once offered "I'm uploading a document now - wait for it", which the card
    cannot carry out; a file field lets the person upload or pick the document."""
    from packages.core.ai.tools.request_input_tool import REQUEST_INPUT_SCHEMA

    payload = normalize_input_request(
        question="Which article should the carousel summarise?",
        fields=[{"name": "source", "label": "Source article", "type": "file"}],
    )
    assert payload["fields"][0]["type"] == "file"
    field_schema = REQUEST_INPUT_SCHEMA["function"]["parameters"]["properties"]["fields"]["items"]
    assert "file" in field_schema["properties"]["type"]["enum"]
    assert "`file` field" in REQUEST_INPUT_SCHEMA["function"]["description"]


def test_normalize_rejects_an_empty_question() -> None:
    with pytest.raises(ValueError):
        normalize_input_request(question="   ")


def test_missing_tool_guidance_acquires_or_blocks_never_asks_for_configuration() -> None:
    text = request_input_tool.REQUEST_INPUT_SCHEMA["function"]["description"]
    assert text.index("search_tools") < text.index("acquire_capabilities") < text.index("blocked")
    assert "complete_task(outcome=blocked)" in text
    assert "tool access or" in text and "configuration" in text
    # The old flow asked the operator for a reviewed configuration update.
    assert "deployment binding" not in text and "configuration review" not in text
    assert "cannot create a binding" in text


def test_new_call_schema_requires_human_only_justification_without_defaulting_it() -> None:
    parameters = request_input_tool.REQUEST_INPUT_SCHEMA["function"]["parameters"]
    assert "justification" in parameters["required"]
    assert parameters["properties"]["justification"] is REQUEST_INPUT_JUSTIFICATION_SCHEMA
    assert "default" not in REQUEST_INPUT_JUSTIFICATION_SCHEMA
    assert "justification" not in normalize_input_request(question="Historical question?")


@pytest.mark.asyncio
@pytest.mark.parametrize("justification,path,rule", [
    (None, "$", "required"),
    (_justification("internal_recovery"), "$.justification.kind", "enum"),
    (_justification("execution_verification"), "$.justification.kind", "enum"),
    (_justification(why_human_required="  "), "$.justification.why_human_required", "pattern"),
    (_justification(attempted_resolution=[]), "$.justification.attempted_resolution", "minItems"),
    (_justification("file_review"), "$.justification", "required"),
])
async def test_invalid_escalation_returns_same_loop_error_before_any_pause(justification, path, rule) -> None:
    result = await request_input_tool._request_input(
        question="Please confirm the result.", **_REC, justification=justification,
        _runtime_human_input_from_context=True,
    )
    assert isinstance(result, RuntimePreflightValidationError)
    error = json.loads(result)
    assert error["contract"] == "tool_error"
    assert error["error"] == "tool_input_validation_failed"
    assert error["details"]["path"] == path
    assert error["details"]["schema_rule"] == rule
    assert "expected" in error["details"]
    assert "authorization" not in error


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["human_fact", "human_material", "file_review"])
async def test_valid_human_fact_material_or_file_review_keeps_original_pause_contract(kind) -> None:
    justification = _justification(
        kind,
        why_human_required="The user must supply their unpublished content or their judgment of the draft.",
        attempted_resolution=[{"source": "workspace", "finding": "The workspace has the draft but no user decision or unpublished source material."}],
    )
    if kind == "file_review":
        justification["file_refs"] = ["/viewer/document-1"]
    result = await request_input_tool._request_input(
        question="Please review the draft's tone or supply your final copy.",
        **_REC, justification=justification, _runtime_human_input_from_context=True,
    )
    assert isinstance(result, RuntimeToolSuspension)
    assert result.kind == HUMAN_INPUT_SUSPENSION_KIND
    assert result.payload["justification"] == justification
    assert "authorization" not in result.to_dict()
    justification["attempted_resolution"][0]["finding"] = "mutated caller value"
    assert result.payload["justification"]["attempted_resolution"][0]["finding"] != "mutated caller value"


@pytest.mark.asyncio
async def test_justification_is_preserved_on_input_card_and_projection_not_as_authority() -> None:
    from packages.core.services import runtime_human_input

    question = normalize_input_request(question="Which unit?", justification=_justification())
    card = runtime_human_input._hitl_payload(question, conversation_id="conversation-1")
    assert card["justification"] == _justification()
    assert card["why"] == _justification()["why_human_required"]
    assert "scope" not in card and "authorization" not in card
    request = SimpleNamespace(
        id="request-1", status="pending", context={"question": question}, reason=question["question"],
        action_key=runtime_human_input.RUNTIME_INPUT_ACTION_KEY, hitl_type="input",
    )
    projection = await runtime_human_input.human_input_projection(
        SimpleNamespace(get=AsyncMock(return_value=request)),
        SimpleNamespace(human_input_request_id="request-1"),
    )
    assert projection["justification"] == _justification()
    legacy = runtime_human_input._hitl_payload({"question": "Old question?"}, conversation_id=None)
    assert "justification" not in legacy


# ── handler modes ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_suspends_when_the_runtime_can_park_the_turn() -> None:
    result = await request_input_tool._request_input(
        question="Which unit?",
        options=["4C", "5A"],
        **_REC, justification=_justification(),
        _runtime_human_input_from_context=True,
        _runtime_tool_call_id_from_context="call-1",
    )
    assert isinstance(result, RuntimeToolSuspension)
    assert result.kind == HUMAN_INPUT_SUSPENSION_KIND
    assert result.reason == HUMAN_INPUT_SUSPENSION_REASON
    assert result.payload == {
        "question": "Which unit?", "options": ["4C", "5A"], "justification": _justification(),
        "recommendation": {"option": "4C", "reason": _REC["recommendation_reason"]},
    }
    control = result.to_dict()
    assert is_human_input_suspension_control(control)
    assert control["payload"]["question"] == "Which unit?"
    assert datetime.fromisoformat(control["deadline_at"]) > datetime.now(timezone.utc)


def _setup(why: str) -> dict:
    return _justification(
        "authorization_setup", why_human_required=why,
        attempted_resolution=[{"source": "tool_result", "finding": "Capability acquisition is not offered in this loop."}],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,question,why", [
    (True, "Please configure generate_file for this Agent.",
     "The current Agent deployment requires an operator-reviewed configuration change."),
    # Prod Task 01M5DVK8G26J4P6GW5RB4RX87M, 2026-10-21.
    (True, "请确认部署配置回执,以便我继续交付。", "需要用户确认部署配置。"),
    (None, "Can you enable tool access to send_email for me?", "Only the user can grant the tool."),
])
async def test_tool_grant_question_is_refused_in_every_mode_before_any_pause(mode, question, why) -> None:
    result = await request_input_tool._request_input(
        question=question, **_REC, justification=_setup(why), _runtime_human_input_from_context=mode,
    )
    assert isinstance(result, RuntimePreflightValidationError)
    error = json.loads(result)
    assert error["contract"] == "tool_error"
    assert error["error"] == "tool_grant_not_requestable"
    assert "acquire_capabilities" in error["details"]["instruction"]
    assert "complete_task(outcome=blocked)" in error["details"]["instruction"]


@pytest.mark.asyncio
async def test_account_connection_still_pauses_and_other_kinds_are_not_scanned() -> None:
    connect = _setup("Sending needs the user's own Gmail account; only they can sign in.")
    result = await request_input_tool._request_input(
        question="Please connect your Gmail account in Integrations so I can send the reply.",
        **_REC, justification=connect, _runtime_human_input_from_context=True,
    )
    assert isinstance(result, RuntimeToolSuspension)
    assert result.payload["justification"] == connect
    # The guard is scoped to authorization_setup: a preference about a
    # configuration is still an ordinary human decision.
    result = await request_input_tool._request_input(
        question="Which report configuration do you prefer?", **_REC, justification=_justification(),
        _runtime_human_input_from_context=True,
    )
    assert isinstance(result, RuntimeToolSuspension)


@pytest.mark.asyncio
async def test_handler_reports_an_empty_question_as_an_error_not_a_pause() -> None:
    result = await request_input_tool._request_input(
        question="", **_REC, justification=_justification(), _runtime_human_input_from_context=True,
    )
    assert isinstance(result, RuntimePreflightValidationError)
    assert json.loads(result)["contract"] == "tool_error"
    assert json.loads(result)["details"]["path"] == "$.question"


@pytest.mark.asyncio
async def test_registered_execution_preserves_unavailable_input_preflight_type(monkeypatch) -> None:
    from packages.core.ai.runtime import tool_execution, tool_registry
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    pool.register("request_input", request_input_tool.REQUEST_INPUT_SCHEMA, request_input_tool._request_input)

    async def allow_without_database(**kwargs):
        return tool_execution.RuntimePreparedToolExecution(arguments=kwargs["arguments"])

    # Exercise the real registry, ToolPool, and handler-result wrappers; only
    # policy/DB preflight is stubbed because this is a no-effect chat question.
    monkeypatch.setattr(tool_registry, "_tool_pool", lambda: pool)
    monkeypatch.setattr(tool_execution, "runtime_prepare_tool_execution", allow_without_database)
    result = await tool_registry.runtime_execute_tool(
        "request_input", {"question": "Which unit?", **_REC, "justification": _justification()},
        allowed_tool_names={"request_input"},
    )

    assert isinstance(result, RuntimePreflightValidationError)
    assert json.loads(result)["error"] == "human_input_unavailable"


def test_answer_result_carries_answer_fields_and_author() -> None:
    parsed = json.loads(
        human_input_answer_result(
            answer=" 4C ", fields={"budget": "1200"}, choice="4C", answered_by="Amy",
        )
    )
    assert parsed["status"] == "answered"
    assert parsed["answer"] == "4C"
    assert parsed["fields"] == {"budget": "1200"}
    assert parsed["choice"] == "4C"
    assert parsed["answered_by"] == "Amy"


def test_yes_answer_to_tool_request_is_not_a_tool_grant() -> None:
    parsed = json.loads(human_input_answer_result(answer="yes"))
    assert parsed["answer"] == "yes"
    assert "grant" not in parsed and "authorized" not in parsed
    assert "does not create a tool binding" in parsed["instruction"]
    assert "configuration review" not in parsed["instruction"]
    assert "stop as blocked" in parsed["instruction"]


# ── checkpoint schema ─────────────────────────────────────────────────────


def test_checkpoint_v4_round_trips_the_pending_tool_result() -> None:
    checkpoint = RuntimeAgentCheckpoint(
        messages=[{"role": "system", "content": "s"}],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call={"id": "call-1", "name": "request_input"},
        pending_tool_result={"id": "call-1", "content": "{\"answer\": \"4C\"}"},
    )
    encoded = checkpoint.to_dict()
    assert encoded["schema_version"] == 4
    restored = RuntimeAgentCheckpoint.from_dict(encoded)
    assert restored.pending_tool_result == {"id": "call-1", "content": "{\"answer\": \"4C\"}"}


def test_older_checkpoints_never_carry_a_pending_tool_result() -> None:
    restored = RuntimeAgentCheckpoint.from_dict({
        "schema_version": 3,
        "messages": [],
        "usage": {},
        "rounds": 0,
        "tool_calls_made": [],
        "pending_tool_call": {"id": "call-1", "name": "request_input"},
        "pending_tool_result": {"id": "call-1", "content": "stale"},
    })
    assert restored.pending_tool_result is None


# ── loop integration ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("next_action", ["autonomous_read", "human_only_decision"])
async def test_internal_escalation_error_reaches_model_before_it_selects_next_step(monkeypatch, next_action):
    rounds = 0
    executed = []

    async def model(messages, *_args, **_kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            return "", [{
                "id": "invalid-ask", "name": "request_input",
                "arguments": {"question": "Did the system write the file?", **_REC, "justification": _justification("execution_verification")},
            }], {}
        if rounds == 2:
            feedback = json.loads(next(
                message["content"] for message in messages if message.get("tool_call_id") == "invalid-ask"
            ))
            assert feedback["contract"] == "tool_error"
            assert feedback["error"] == "tool_input_validation_failed"
            assert feedback["details"]["path"] == "$.justification.kind"
            if next_action == "human_only_decision":
                return "", [{
                    "id": "valid-ask", "name": "request_input",
                    "arguments": {"question": "Which unit?", **_REC, "justification": _justification()},
                }], {}
            return "", [{"id": "inspect-file", "name": "read_file", "arguments": {}}], {}
        assert rounds == 3
        return "The recorded file version confirms the write completed.", [], {}

    async def execute(name, arguments):
        executed.append(name)
        if name == "request_input":
            return await request_input_tool._request_input(**arguments)
        assert name == "read_file"
        return json.dumps({"status": "ok", "path": "draft.md", "version": 2})

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="Finish the document", human_input_suspension=True,
        tools=[request_input_tool.REQUEST_INPUT_SCHEMA, _tool("read_file")],
        tool_executor=execute, max_rounds=4,
    )
    if next_action == "human_only_decision":
        assert result.stop_reason == "waiting_resource"
        assert is_human_input_suspension_control(result.control)
        checkpoint = RuntimeAgentCheckpoint.from_dict(result.control["checkpoint"])
        assert checkpoint.pending_tool_call["id"] == "valid-ask"
        assert result.control["payload"]["justification"] == _justification()
        assert executed == ["request_input"]
    else:
        assert result.stop_reason == "completed"
        assert result.error is None
        assert not is_human_input_suspension_control(result.control)
        assert executed == ["read_file"]


@pytest.mark.asyncio
async def test_loop_parks_on_request_input_without_a_runtime_run(monkeypatch) -> None:
    """A worker step has no runtime run id; the flag alone enables the park."""

    async def fake_round(*_args, **_kwargs):
        return "", [
            {
                "id": "call-ask", "name": "request_input",
                "arguments": {
                    "question": "Which unit?",
                    **_REC, "justification": _justification(),
                    "_runtime_human_input_from_context": "end_turn",
                },
            }
        ], {}

    seen_args: list[dict] = []

    async def tool_executor(_name: str, args: dict):
        seen_args.append(args)
        return await request_input_tool._request_input(**args)

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)

    result = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="find me a unit",
        tools=[_tool("request_input")],
        tool_executor=tool_executor,
        human_input_suspension=True,
        max_rounds=3,
    )

    assert result.stop_reason == "waiting_resource"
    assert is_human_input_suspension_control(result.control)
    assert result.control["payload"] == {
        "question": "Which unit?", "options": ["4C"], "justification": _justification(),
        "recommendation": {"option": "4C", "reason": _REC["recommendation_reason"]},
    }
    assert seen_args[0]["_runtime_human_input_from_context"] is True
    assert seen_args[0]["_runtime_tool_call_id_from_context"] == "call-ask"
    assert "_runtime_run_id_from_context" not in seen_args[0]
    checkpoint = RuntimeAgentCheckpoint.from_dict(result.control["checkpoint"])
    assert checkpoint.pending_tool_call["id"] == "call-ask"
    assert checkpoint.pending_tool_result is None
    assert not any(message.get("role") == "tool" for message in result.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("forged_mode", [True, "end_turn"])
async def test_loop_ignores_the_flag_when_disabled(monkeypatch, forged_mode) -> None:
    model_rounds: list[list[dict]] = []

    async def fake_round(messages, *_args, **_kwargs):
        model_rounds.append([dict(message) for message in messages])
        if fake_round.calls == 0:
            fake_round.calls += 1
            return "", [
                {
                    "id": "call-ask", "name": "request_input",
                    "arguments": {
                        "question": "Which unit?",
                        **_REC, "justification": _justification(),
                        "_runtime_human_input_from_context": forged_mode,
                    },
                },
                {"id": "call-later", "name": "ws_external_action", "arguments": {}},
            ], {}
        assert any(
            message.get("role") == "tool"
            and message.get("tool_call_id") == "call-ask"
            and json.loads(message["content"])["error"] == "human_input_unavailable"
            for message in messages
        )
        return "Which unit would you like?", [], {}
    fake_round.calls = 0

    executed: list[str] = []

    async def tool_executor(name: str, args: dict):
        executed.append(name)
        assert name == "request_input", "later stateful calls must not overtake the question"
        assert "_runtime_human_input_from_context" not in args
        return await request_input_tool._request_input(**args)

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    result = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="find me a unit",
        tools=[_tool("request_input"), _tool("ws_external_action")],
        tool_executor=tool_executor,
        max_rounds=3,
    )
    assert result.stop_reason == "completed"
    assert result.content == "Which unit would you like?"
    assert result.error is None
    assert len(model_rounds) == 2
    assert executed == ["request_input"]
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    # Without the flag the loop cannot park, and the tool says so.
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0]["content"])["error"] == "human_input_unavailable"


def _transcript_tool_calls(messages: list[dict]) -> tuple[list[str], list[dict]]:
    """Assistant tool-call names and tool results the next model round sees."""
    names = [
        str((call.get("function") or {}).get("name") or call.get("name") or "")
        for message in messages if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    ]
    return names, [message for message in messages if message.get("role") == "tool"]


@pytest.mark.asyncio
async def test_unrelated_serial_tool_error_reaches_model_with_tools(monkeypatch) -> None:
    model_rounds = 0

    async def fake_round(messages, tools, **_kwargs):
        nonlocal model_rounds
        model_rounds += 1
        if model_rounds == 1:
            return "", [
                {"id": "call-failed", "name": "ws_external_action", "arguments": {}},
                {"id": "call-later", "name": "ws_followup_action", "arguments": {}},
            ], {}
        # The error reaches the model with its tools; the dependent suffix
        # was discarded and the failed call is not replayed for it.
        assert {tool["function"]["name"] for tool in tools} == {"ws_external_action", "ws_followup_action"}
        names, tool_results = _transcript_tool_calls(messages)
        assert names == ["ws_external_action"]
        assert len(tool_results) == 1
        assert tool_results[0]["tool_call_id"] == "call-failed"
        assert "provider_outcome_unknown" in tool_results[0]["content"]
        return "The action's outcome is unknown; check it before trying again.", [], {}

    executed: list[str] = []

    async def tool_executor(name: str, _args: dict):
        executed.append(name)
        return json.dumps({"error": "provider_outcome_unknown"})

    summary = AsyncMock(side_effect=AssertionError("a tool error must not force a tool-free summary"))
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", summary)
    result = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="perform action",
        tools=[_tool("ws_external_action"), _tool("ws_followup_action")],
        tool_executor=tool_executor,
        max_rounds=3,
    )

    assert result.stop_reason == "completed"
    assert result.error is None
    assert result.content == "The action's outcome is unknown; check it before trying again."
    assert model_rounds == 2
    assert executed == ["ws_external_action"]
    summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_host_classified_read_only_serial_error_stays_in_agent_loop(monkeypatch) -> None:
    model_rounds = 0
    executed: list[str] = []

    async def fake_round(*_args, **_kwargs):
        nonlocal model_rounds
        model_rounds += 1
        if model_rounds == 1:
            return "", [
                {"id": "read-failed", "name": "ws_get_draft", "arguments": {}},
                {"id": "write-suffix", "name": "ws_external_action", "arguments": {}},
            ], {}
        if model_rounds == 2:
            return "", [
                {"id": "read-retry", "name": "ws_get_draft", "arguments": {}},
            ], {}
        return "Recovered after correcting the read.", [], {}

    async def tool_executor(name: str, _args: dict):
        executed.append(name)
        if len(executed) == 1:
            return json.dumps({"error": "invalid_read_arguments"})
        return json.dumps({"draft": "ready"})

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="read the draft",
        tools=[_tool("ws_get_draft"), _tool("ws_external_action")],
        tool_executor=tool_executor, max_rounds=4,
    )

    assert result.stop_reason == "completed"
    assert result.content == "Recovered after correcting the read."
    assert model_rounds == 3
    assert executed == ["ws_get_draft", "ws_get_draft"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["normal", "forced", "resume"])
async def test_explicit_unknown_mutation_outcome_reaches_model_without_replay(monkeypatch, entry) -> None:
    uncertainty = {
        "status": "error",
        "error": "mutation_timeout_uncertain",
        "content": "The mutation outcome is unknown; observe and reconcile.",
        "stop_parent": True,
        "stop_reason": "uncertain_result",
        "terminal_failure": True,
        "retryable": False,
        "action_executed": "unknown",
    }
    batch = [
        {"id": "uncertain", "name": "mcp__chrome__run_sequence", "arguments": {}},
        {"id": "suffix", "name": "ws_followup_action", "arguments": {}},
    ]
    model_calls = 0
    executed = []

    async def model(messages, tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        if entry == "normal" and model_calls == 1:
            return "", batch, {}
        # The uncertainty is an observation: its evidence and the model's
        # tools stay, the suffix was discarded, and nothing replays it.
        assert {tool["function"]["name"] for tool in tools} == {"mcp__chrome__run_sequence", "ws_followup_action"}
        names, tool_results = _transcript_tool_calls(messages)
        assert names == ["mcp__chrome__run_sequence"]
        assert len(tool_results) == 1
        assert "mutation_timeout_uncertain" in tool_results[0]["content"]
        assert '"action_executed":"unknown"' in tool_results[0]["content"]
        return "The mutation outcome is unknown; I will not repeat it without checking.", [], {}

    async def execute(name, _arguments):
        executed.append(name)
        return json.dumps(uncertainty)

    summary = AsyncMock(side_effect=AssertionError("uncertainty must not force a tool-free summary"))
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", summary)
    kwargs = {}
    if entry == "forced":
        kwargs["forced_tool_calls"] = batch
    elif entry == "resume":
        pending, suffix = batch
        kwargs["resume_checkpoint"] = RuntimeAgentCheckpoint(
            messages=[{"role": "assistant", "content": "", "tool_calls": [
                {"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"]),
                }} for call in batch
            ]}],
            usage={}, rounds=1, tool_calls_made=[],
            pending_tool_call=pending, remaining_tool_calls=[suffix],
        ).to_dict()
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="perform the action",
        tools=[_tool("mcp__chrome__run_sequence"), _tool("ws_followup_action")],
        tool_executor=execute, max_rounds=3, **kwargs,
    )

    assert result.stop_reason == "completed"
    assert result.error is None
    assert result.content == "The mutation outcome is unknown; I will not repeat it without checking."
    assert model_calls == (2 if entry == "normal" else 1)
    assert executed == ["mcp__chrome__run_sequence"]
    summary.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["normal", "forced", "resume"])
async def test_failed_stop_parent_result_is_an_observation_for_the_model(monkeypatch, entry) -> None:
    failure = {
        "status": "failed",
        "error": "provider_action_failed",
        "content": "The provider action failed.",
        "stop_parent": True,
        "stop_reason": "provider_action_failed",
        "terminal_failure": True,
        "retryable": False,
    }
    batch = [
        {"id": "failed", "name": "ws_external_action", "arguments": {}},
        {"id": "suffix", "name": "ws_followup_action", "arguments": {}},
    ]
    model_calls = 0
    executed = []

    async def model(messages, tools, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        if entry == "normal" and model_calls == 1:
            return "", batch, {}
        assert {tool["function"]["name"] for tool in tools} == {"ws_external_action", "ws_followup_action"}
        names, tool_results = _transcript_tool_calls(messages)
        assert names == ["ws_external_action"]
        assert len(tool_results) == 1
        assert "provider_action_failed" in tool_results[0]["content"]
        return "The provider action failed; please review its current state.", [], {}

    async def execute(name, _arguments):
        executed.append(name)
        return json.dumps(failure)

    summary = AsyncMock(side_effect=AssertionError("stop_parent failure must not force a tool-free summary"))
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", summary)
    kwargs = {}
    if entry == "forced":
        kwargs["forced_tool_calls"] = batch
    elif entry == "resume":
        pending, suffix = batch
        kwargs["resume_checkpoint"] = RuntimeAgentCheckpoint(
            messages=[{"role": "assistant", "content": "", "tool_calls": [
                {"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"]),
                }} for call in batch
            ]}],
            usage={}, rounds=1, tool_calls_made=[],
            pending_tool_call=pending, remaining_tool_calls=[suffix],
        ).to_dict()
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="perform the action",
        tools=[_tool("ws_external_action"), _tool("ws_followup_action")],
        tool_executor=execute, max_rounds=3, **kwargs,
    )

    assert result.content == "The provider action failed; please review its current state."
    assert model_calls == (2 if entry == "normal" else 1)
    assert executed == ["ws_external_action"]
    assert result.stop_reason == "completed"
    assert result.error is None
    # The child's stop_parent failure did not become a parent stop control.
    assert not (result.control or {}).get("stop_parent")
    summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_appends_the_stored_answer_instead_of_rerunning_the_tool(monkeypatch) -> None:
    rounds: list[list[dict]] = []

    async def fake_round(messages, *_args, **_kwargs):
        rounds.append([dict(m) for m in messages])
        return "Booked unit 4C.", [], {}

    executed: list[str] = []

    async def tool_executor(name: str, _args: dict):
        executed.append(name)
        return "should not run"

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)

    answer = human_input_answer_result(answer="4C", answered_by="Amy")
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "find me a unit"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call-ask", "name": "request_input", "arguments": {"question": "Which unit?"}}
                ],
            },
        ],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call={"id": "call-ask", "name": "request_input", "arguments": {"question": "Which unit?"}},
        pending_tool_result={"id": "call-ask", "content": answer},
    )
    tool_ends: list[tuple] = []
    result = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="find me a unit",
        tools=[_tool("request_input")],
        tool_executor=tool_executor,
        resume_checkpoint=checkpoint.to_dict(),
        human_input_suspension=True,
        on_tool_end=lambda *args: tool_ends.append(args),
        max_rounds=5,
    )
    assert result.stop_reason == "completed"
    assert executed == [], "the parked tool call must not be re-executed"
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call-ask"
    assert json.loads(tool_messages[0]["content"])["answer"] == "4C"
    # The model saw the answer before producing its final text.
    assert any(m.get("role") == "tool" for m in rounds[0])
    assert tool_ends and tool_ends[0][0] == "request_input"


@pytest.mark.asyncio
async def test_answered_question_is_not_parked_again_and_its_answer_returns(monkeypatch) -> None:
    """Prod Task 01M5DVK8G26J4P6GW5RB4RX87M: the user answered 可以 and was asked again."""
    seen: list[list[dict]] = []

    async def fake_round(messages, *_args, **_kwargs):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            return "", [{"id": "call-again", "name": "request_input", "arguments": {
                "question": "Can I proceed with the platform admin's fix?", **_REC, "justification": _justification(),
            }}], {}
        return "Proceeding.", [], {}

    executed: list[str] = []

    async def tool_executor(name: str, _args: dict):
        executed.append(name)
        return "should not run"

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    asked = {"id": "call-ask", "name": "request_input", "arguments": {"question": "Can I proceed with the platform admin's fix"}}
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "deliver"},
            {"role": "assistant", "content": "", "tool_calls": [asked]},
        ],
        usage={}, rounds=1, tool_calls_made=[],
        pending_tool_call=asked,
        pending_tool_result={"id": "call-ask", "content": human_input_answer_result(answer="可以")},
    )
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="deliver", tools=[_tool("request_input")],
        tool_executor=tool_executor, resume_checkpoint=checkpoint.to_dict(),
        human_input_suspension=True, max_rounds=5,
    )
    assert result.stop_reason == "completed"
    assert executed == [], "a re-asked question never reaches a handler"
    refusal = json.loads(next(m for m in result.messages if m.get("tool_call_id") == "call-again")["content"])
    assert refusal["error"] == "human_input_already_answered"
    assert refusal["details"]["previous_answer"] == "可以"


@pytest.mark.asyncio
async def test_platform_fault_refuses_every_question_until_the_operation_recovers(monkeypatch) -> None:
    from packages.core.ai.runtime.tool_error_contract import tool_error_payload
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    calls = iter([
        [{"id": "c1", "name": "replan_task", "arguments": {}}],
        [{"id": "c2", "name": "request_input", "arguments": {
            "question": "Please apply the platform fix.", **_REC, "justification": _justification("human_material"),
        }}],
        [{"id": "c3", "name": "replan_task", "arguments": {}}],
        [{"id": "c4", "name": "request_input", "arguments": {
            "question": "Which unit?", **_REC, "justification": _justification(),
        }}],
    ])

    async def fake_round(*_args, **_kwargs):
        return "", next(calls), {}

    replans = iter([
        RuntimePreflightValidationError(json.dumps(tool_error_payload(
            tool_name="replan_task", code="invalid_partial_continuation",
            message="Existing Step inputs changed or have no pinned execution snapshot",
            internal_error=True,
        ))),
        json.dumps({"status": "repaired"}),
    ])
    executed: list[str] = []

    async def tool_executor(name: str, args: dict):
        executed.append(name)
        return next(replans) if name == "replan_task" else await request_input_tool._request_input(**args)

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="deliver",
        tools=[_tool("replan_task"), _tool("request_input")], tool_executor=tool_executor,
        tool_evidence=RuntimeToolEvidence(), human_input_suspension=True, max_rounds=6,
    )
    refusal = json.loads(next(m for m in result.messages if m.get("tool_call_id") == "c2")["content"])
    assert refusal["error"] == "human_input_platform_fault"
    assert refusal["details"]["platform_error"] == "invalid_partial_continuation"
    assert "complete_task(outcome=blocked)" in refusal["details"]["instruction"]
    # The successful replan resolved the fault: the next real question parks.
    assert executed == ["replan_task", "replan_task", "request_input"]
    assert is_human_input_suspension_control(result.control)


def test_every_human_park_producer_is_a_request_input_handler_behind_the_gate() -> None:
    """Guard the class: a new way to park on a person must pass the shared gate.

    The agentic loop gates every handler reached as ``request_input``. A new
    producer of a human-input park, or a loop that dispatches request_input
    itself (the retired end-turn mode), fails here until it is routed through
    the gate.
    """
    import ast
    from pathlib import Path

    producers: set[str] = set()
    gated: set[str] = set()
    end_turn_hosts: set[str] = set()
    for path in sorted(Path("packages").rglob("*.py")):
        if "tests" in path.parts:
            continue
        rel = path.as_posix()
        nodes = list(ast.walk(ast.parse(path.read_text())))
        for node in nodes:
            # A loop that dispatches request_input itself in end-turn mode.
            if isinstance(node, ast.Dict) and any(
                getattr(key, "value", None) == "_runtime_human_input_from_context"
                and getattr(value, "id", None) == "HUMAN_INPUT_MODE_END_TURN"
                for key, value in zip(node.keys, node.values)
            ):
                end_turn_hosts.add(rel)
            # RuntimeToolSuspension(kind=HUMAN_INPUT_SUSPENSION_KIND, ...) parks;
            # a {REQUEST_INPUT_RESULT_KEY: ...} result is the end-turn park.
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "RuntimeToolSuspension" and any(
                keyword.arg == "kind" and getattr(keyword.value, "id", None) == "HUMAN_INPUT_SUSPENSION_KIND"
                for keyword in node.keywords
            ) or isinstance(node, ast.Dict) and any(
                getattr(key, "id", None) == "REQUEST_INPUT_RESULT_KEY" for key in node.keys
            ):
                producers.add(rel)
            # A tool registered with the same gate (runtime_human_park_arguments).
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "register_human_park_tool":
                gated.add(rel)
    # Reached only as the ``request_input`` tool of an agentic loop, or as a
    # tool the loop gates the same way.
    assert "packages/core/ai/tools/request_input_tool.py" in producers
    assert producers - gated == {"packages/core/ai/tools/request_input_tool.py"}
    assert end_turn_hosts == set()


@pytest.mark.asyncio
async def test_resume_reruns_the_tool_when_the_stored_result_is_for_another_call(monkeypatch) -> None:
    async def fake_round(*_args, **_kwargs):
        return "done", [], {}

    executed: list[str] = []

    async def tool_executor(name: str, _args: dict):
        executed.append(name)
        return "fresh result"

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    checkpoint = RuntimeAgentCheckpoint(
        messages=[{"role": "system", "content": "system"}, {"role": "user", "content": "go"}],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call={"id": "call-2", "name": "workspace_search", "arguments": {}},
        pending_tool_result={"id": "call-1", "content": "stale"},
    )
    result = await loop_module.agentic_loop(
        system_prompt="system",
        user_message="go",
        tools=[_tool("workspace_search")],
        tool_executor=tool_executor,
        resume_checkpoint=checkpoint.to_dict(),
        max_rounds=5,
    )
    assert result.stop_reason == "completed"
    assert executed == ["workspace_search"]


def test_request_input_is_serialized_with_its_round() -> None:
    assert loop_module._requires_serial_tool_execution("request_input")


# ── delivery modes (review follow-up) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_is_honest_where_the_runtime_cannot_pause() -> None:
    """No flag: the tool must not claim the question was delivered."""
    result = await request_input_tool._request_input(question="Which unit?", **_REC, justification=_justification())
    assert isinstance(result, RuntimePreflightValidationError)
    parsed = json.loads(result)
    assert parsed["contract"] == "tool_error"
    assert parsed["status"] == "error"
    assert parsed["error"] == "human_input_unavailable"
    assert parsed["details"]["question"] == "Which unit?"


def test_services_import_the_runtime_bridge_not_the_tool() -> None:
    from pathlib import Path

    for rel in (
        "packages/core/services/runtime_human_input.py",
        "packages/core/services/task_input_resume.py",
    ):
        assert "packages.core.ai.tools.request_input_tool" not in Path(rel).read_text(), rel


@pytest.mark.asyncio
async def test_a_question_without_a_recommended_answer_is_refused_before_any_pause() -> None:
    result = await request_input_tool._request_input(
        question="Which unit?", options=["4C", "5A"], justification=_justification(),
        _runtime_human_input_from_context=True,
    )
    error = json.loads(result)
    assert error["error"] == "tool_input_validation_failed"
    assert error["details"]["schema_rule"] == "required"
    assert error["details"]["expected"] == "recommended"


def test_the_recommended_answer_leads_and_a_new_one_becomes_an_option() -> None:
    payload = normalize_input_request(
        question="Which unit?", options=["5A", "4C"], recommended="4C",
        recommendation_reason="It is the cheaper of the two.",
    )
    assert payload["options"] == ["4C", "5A"]
    assert payload["recommendation"] == {"option": "4C", "reason": "It is the cheaper of the two."}
    free = normalize_input_request(question="What is the client's email?", recommended="lena@studio.com",
                                   recommendation_reason="It is on the signed contract.")
    assert free["options"] == ["lena@studio.com"]
