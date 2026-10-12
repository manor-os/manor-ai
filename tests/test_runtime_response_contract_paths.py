"""Every model output lane carries its contract without reopening tool effects."""
import json

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime import agentic_llm
from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract

pytestmark = pytest.mark.unit


def wrapped_answer(status="answered", ref=None):
    refs = [ref] if ref else []
    return json.dumps({"response": {"kind": "answer", "text": "The observed result.", "outcome": {
        "status": status, "evidence_refs": refs,
        "limitations": [] if status in {"answered", "completed"} else ["Lookup remains incomplete."],
        "claims": [{"operation": "web_fetch", "status": status, "evidence_refs": refs}]
        if status != "answered" else [],
    }}})


def schema():
    return {"type": "function", "function": {"name": "web_fetch", "parameters": {"type": "object"}}}


@pytest.fixture
def no_credit(monkeypatch):
    async def check():
        return None

    monkeypatch.setattr(loop_module, "_preflight_credit_check", check)


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["tool", "text", "final"])
@pytest.mark.parametrize("structured", [False, True])
async def test_completion_helpers_forward_only_requested_response_format(monkeypatch, lane, structured):
    calls = []
    response_format = ChatEvidenceContract().response_format if structured else None

    async def provider(messages, *args, **kwargs):
        calls.append(kwargs)
        if structured:
            assert kwargs["response_format"] == response_format
        else:
            assert "response_format" not in kwargs
        return ("reply", None, {}) if lane == "tool" else ("reply", {})

    monkeypatch.setattr(agentic_llm, "chat_completion", provider)
    monkeypatch.setattr(agentic_llm, "chat_completion_with_tools", provider)
    helper = getattr(agentic_llm, f"runtime_execute_agentic_{'round_' if lane != 'final' else ''}{lane}_completion")
    args = ([{"role": "user", "content": "hello"}], [schema()]) if lane == "tool" else ([{"role": "user", "content": "hello"}],)
    await helper(*args, temperature=0, response_format=response_format)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_first_tool_round_uses_progress_schema_and_final_still_validates(monkeypatch, no_credit):
    contract = ChatEvidenceContract(requires_execution=True)
    rounds, effects, events = [], [], []

    async def provider(messages, tools, **kwargs):
        rounds.append(kwargs)
        assert kwargs["response_format"] == contract.tool_response_format
        if len(rounds) == 1:
            return '{"response":{"kind":"progress","text":"Reading the current page."}}', [
                {"id": "fetch", "name": "web_fetch", "arguments": {}},
            ], {}
        return wrapped_answer("completed", "fetch"), None, {}

    async def execute(name, args):
        effects.append(name)
        return '{"content":"Observed webpage"}'

    async def stream(event, payload):
        events.append((event, payload))

    monkeypatch.setattr(agentic_llm, "chat_completion_with_tools", provider)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="Look up the current page", tools=[schema()],
        tool_executor=execute, stream_handler=stream, final_response_contract=contract, max_rounds=4,
    )
    assert result.stop_reason == "completed" and result.error is None
    assert result.content == "The observed result."
    assert result.outcome["evidence_refs"] == ["fetch"]
    assert effects == ["web_fetch"] and len(rounds) == 2
    assert [(event, payload["content"]) for event, payload in events if event == "text_delta"] == [
        ("text_delta", "Reading the current page."),
    ]


@pytest.mark.asyncio
async def test_first_no_tool_round_uses_final_schema(monkeypatch, no_credit):
    contract = ChatEvidenceContract()
    calls = []

    async def provider(messages, **kwargs):
        calls.append(kwargs)
        assert kwargs["response_format"] == contract.response_format
        return wrapped_answer(), {}

    monkeypatch.setattr(agentic_llm, "chat_completion", provider)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="Explain the last result", tools=[],
        tool_executor=None, final_response_contract=contract, max_rounds=3,
    )
    assert result.stop_reason == "completed" and result.content == "The observed result."
    assert result.outcome["status"] == "answered"
    assert len(calls) == 1 and result.tool_calls_made == []


@pytest.mark.asyncio
async def test_approved_single_attempt_repair_retains_feedback_without_replaying_effect(monkeypatch, no_credit):
    contract = ChatEvidenceContract(requires_execution=True)
    summaries, effects = [], []

    async def provider(messages, **kwargs):
        summaries.append(messages)
        assert kwargs["response_format"] == contract.response_format
        if len(summaries) == 1:
            return "A reply with an invalid format", {}
        assert len(summaries) == 2
        assert messages[-1]["role"] == "user"
        feedback = json.loads(messages[-1]["content"])
        assert feedback["error"] == "response_contract_invalid"
        assert feedback["details"]["validation_error"]["reason"] == "invalid_json"
        # dev preserves the existing transcript through approval continuations.
        assert "Old private history" in str(messages)
        return wrapped_answer("completed", "e1"), {}

    async def tool_provider(*args, **kwargs):
        raise AssertionError("An approved single attempt must not restore callable tools")

    async def execute(name, args):
        effects.append(name)
        return '{"content":"Observed webpage"}'

    monkeypatch.setattr(agentic_llm, "chat_completion", provider)
    monkeypatch.setattr(agentic_llm, "chat_completion_with_tools", tool_provider)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="Report the approved lookup", tools=[schema()],
        tool_executor=execute, final_response_contract=contract, max_rounds=4,
        initial_messages=[{"role": "user", "content": "Old private history"}],
        forced_tool_calls=[{"id": "approved-fetch", "name": "web_fetch", "arguments": {},
                            "disable_followup_tools": True}],
    )
    assert result.stop_reason == "completed" and result.error is None
    assert result.content == "The observed result."
    assert len(summaries) == 2 and effects == ["web_fetch"]


@pytest.mark.asyncio
async def test_single_attempt_output_schema_repair_keeps_its_existing_feedback(monkeypatch, no_credit):
    summaries, effects = [], []

    async def provider(messages, **kwargs):
        summaries.append(messages)
        if len(summaries) == 1:
            return '{"wrong":"field"}', {}
        assert len(summaries) == 2
        assert messages[-1]["role"] == "user"
        assert "result" in messages[-1]["content"]
        return '{"result":"observed"}', {}

    async def execute(name, args):
        effects.append(name)
        return '{"content":"Observed webpage"}'

    monkeypatch.setattr(agentic_llm, "chat_completion", provider)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="Report the approved lookup", tools=[schema()],
        tool_executor=execute, max_rounds=4, output_schema={
            "type": "object", "required": ["result"], "additionalProperties": False,
            "properties": {"result": {"type": "string"}},
        }, forced_tool_calls=[{"name": "web_fetch", "arguments": {}, "disable_followup_tools": True}],
    )
    assert result.stop_reason == "completed" and result.error is None
    assert result.content == '{"result":"observed"}'
    assert len(summaries) == 2 and effects == ["web_fetch"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_max_rounds_final_uses_final_schema_and_never_reopens_execution(monkeypatch, no_credit, invalid):
    contract = ChatEvidenceContract()
    tools, finals, effects = [], [], []

    async def tool_provider(messages, schemas, **kwargs):
        tools.append(kwargs)
        assert kwargs["response_format"] == contract.tool_response_format
        return "", [{"id": "fetch", "name": "web_fetch", "arguments": {}}], {}

    async def provider(messages, **kwargs):
        finals.append(kwargs)
        assert kwargs["response_format"] == contract.response_format
        candidate = '{"response":{"kind":"progress","text":"Still working"}}' if invalid else wrapped_answer("partial", "fetch")
        return candidate, {}

    async def execute(name, args):
        effects.append(name)
        return '{"content":"Observed webpage"}'

    monkeypatch.setattr(agentic_llm, "chat_completion_with_tools", tool_provider)
    monkeypatch.setattr(agentic_llm, "chat_completion", provider)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="Look up the current page", tools=[schema()],
        tool_executor=execute, final_response_contract=contract, max_rounds=1,
    )
    assert len(tools) == 1 and len(finals) == 1 and effects == ["web_fetch"]
    if invalid:
        assert result.error == "final_response_validation_failed"
        assert result.content == "" and result.outcome is None
    else:
        assert result.stop_reason == "max_rounds" and result.error is None
        assert result.outcome["status"] == "partial"
        assert result.content == "The observed result."
