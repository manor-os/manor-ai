"""Realtime startup preserves Agent identity within the selected model budget."""

import json

import pytest

from packages.core.ai.runtime.token_estimate import runtime_estimate_tokens_for_text
from packages.core.services.voice.live_agent import live_voice_session_factory
from packages.core.services.voice.realtime import (
    RealtimeRoute,
    SUPPORTED_REALTIME_MODELS,
    SUPPORTED_VERCEL_REALTIME_MODELS,
    build_realtime_session_update,
    live_voice_instruction_token_budget,
    realtime_model_token_limits,
)


@pytest.mark.parametrize(
    ("model", "context", "output", "instructions"),
    [
        ("gpt-realtime", 32_000, 4_096, 16_384),
        ("openai/gpt-realtime-2025-08-28", 32_000, 4_096, 16_384),
        ("openai/gpt-realtime-mini", 32_000, 4_096, 16_384),
        ("gpt-realtime-2", 128_000, 32_000, None),
        ("openai/gpt-realtime-2.1", 128_000, 32_000, None),
    ],
)
def test_realtime_limits_distinguish_context_from_known_instruction_cap(
    model, context, output, instructions
):
    limits = realtime_model_token_limits(model)

    assert limits.context_window == context
    assert limits.max_output_tokens == output
    assert limits.max_instructions_tokens == instructions
    assert build_realtime_session_update(model=model, live_agent=True)["session"][
        "max_output_tokens"
    ] == "inf"


@pytest.mark.parametrize(
    "model", sorted(SUPPORTED_REALTIME_MODELS | SUPPORTED_VERCEL_REALTIME_MODELS)
)
def test_every_supported_route_budgets_instructions_tools_and_audio_headroom(model):
    limits = realtime_model_token_limits(model)
    budget = live_voice_instruction_token_budget(model)
    tools = runtime_estimate_tokens_for_text(
        json.dumps(live_voice_session_factory.tools(), ensure_ascii=False, separators=(",", ":"))
    )

    assert budget > 0
    assert budget + tools < limits.context_window - limits.max_output_tokens
    if limits.max_instructions_tokens is not None:
        assert budget + tools < limits.max_instructions_tokens


def test_unknown_realtime_model_does_not_borrow_another_models_limits():
    with pytest.raises(RuntimeError, match="not configured"):
        live_voice_instruction_token_budget("openai/future-realtime-model")


def test_large_agent_prompt_uses_2x_context_without_legacy_instruction_cap():
    agent = "You are the loan specialist.\n" + "binding policy " * 6_000
    agent += "\nNever expose customer records outside their authorized scope."
    history = [{"role": "user", "content": "Explain mortgage amortization in detail."}]
    budget = live_voice_instruction_token_budget("gpt-realtime-2.1")

    rendered = live_voice_session_factory.instructions(agent, history, max_tokens=budget)

    assert runtime_estimate_tokens_for_text(agent) > 16_384
    assert agent in rendered
    assert history[0]["content"] in rendered
    assert runtime_estimate_tokens_for_text(rendered) <= budget
    with pytest.raises(RuntimeError, match="Current Agent instructions"):
        live_voice_session_factory.instructions(
            agent, history, max_tokens=live_voice_instruction_token_budget("gpt-realtime")
        )


@pytest.mark.parametrize("model", ["gpt-realtime", "gpt-realtime-2.1"])
def test_oversized_history_keeps_summary_agent_constraints_and_newest_turn(model):
    agent = "Keep the current Agent identity and all account access restrictions."
    summary = {"role": "system", "content": "Conversation summary: tour, lunch, then trails."}
    history = [summary] + [
        {"role": "user", "content": f"old-{index}: " + "计划细节 " * 1_500}
        for index in range(45)
    ]
    history.append({"role": "user", "content": "最新要求：中午吃饭后一直逛到傍晚。"})
    original = list(history)
    budget = live_voice_instruction_token_budget(model)
    assert runtime_estimate_tokens_for_text(
        live_voice_session_factory.instructions(agent, history)
    ) > budget

    rendered = live_voice_session_factory.instructions(agent, history, max_tokens=budget)

    assert runtime_estimate_tokens_for_text(rendered) <= budget
    assert agent in rendered
    assert summary["content"] in rendered
    assert history[-1]["content"] in rendered
    assert "old-0:" not in rendered
    assert history == original


def test_compacted_summary_and_inherited_constraints_are_never_silently_cut():
    agent = "Inherited authorization rules remain mandatory."
    summary = [{"role": "system", "content": "known facts " * 2_000}]

    with pytest.raises(RuntimeError, match="compacted conversation summary"):
        live_voice_session_factory.instructions(agent, summary, max_tokens=1_000)


def test_fitting_history_is_preserved_verbatim_and_budget_is_optional():
    agent = "You are the current travel Agent."
    history = [{"role": "user", "content": "hello / 你好 / hola"}]
    full = live_voice_session_factory.instructions(agent, history)

    assert live_voice_session_factory.instructions(
        agent, history, max_tokens=runtime_estimate_tokens_for_text(full)
    ) == full
    with pytest.raises(ValueError, match="must be positive"):
        live_voice_session_factory.instructions(agent, history, max_tokens=0)


def test_live_prompt_routes_current_facts_but_keeps_analysis_and_clear_intent_direct():
    prompt = live_voice_session_factory.instructions()
    delegate = live_voice_session_factory.tools()[0]["description"]

    assert "An explicit request to search, look up, or verify requires delegation" in prompt
    assert "Do not invent those details or give an unverified plan before delegating" in prompt
    assert "General planning principles and analysis of supplied facts" in prompt
    assert "never delegate solely because the answer is long" in prompt
    assert "Do not ask for confirmation when intent and required details are already clear" in prompt
    assert "substantial execution" not in prompt + delegate


def test_result_response_inherits_agent_and_allows_silence_without_execution_tools():
    prompt = live_voice_session_factory.instructions("You are Agent Lee.")
    response = live_voice_session_factory.result_response_event()["response"]

    assert "You are Agent Lee." in prompt
    assert "Decide whether a spoken reply is useful" in prompt
    assert "already answered" in prompt
    assert "instructions" not in response
    assert response["tool_choice"] == "auto"
    assert [tool["name"] for tool in response["tools"]] == ["wait_for_user"]


@pytest.mark.asyncio
async def test_vercel_result_wait_tool_is_restricted_then_foreground_tools_restore():
    from packages.core.services.voice import realtime

    class Socket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    connection = realtime._VercelRealtimeConnection(
        RealtimeRoute("test-key", "https://ai-gateway.vercel.sh/v1", "gpt-realtime-2.1", False)
    )
    socket = Socket()
    connection._socket = socket
    session_event = build_realtime_session_update(model="gpt-realtime-2.1", live_agent=True)
    inherited = live_voice_session_factory.instructions("You are Agent Lee.")
    session_event["session"]["instructions"] = inherited
    await connection.send(session_event)
    socket.sent.clear()

    await connection.send(live_voice_session_factory.result_response_event())
    await connection.send(live_voice_session_factory.response_event())

    result_config = socket.sent[0]["config"]
    assert result_config["instructions"] == inherited
    assert [tool["name"] for tool in result_config["tools"]] == ["wait_for_user"]
    assert result_config["providerOptions"]["tool_choice"] == "auto"
    assert "instructions" not in socket.sent[1]["options"]
    assert socket.sent[2]["config"]["tools"] == live_voice_session_factory.tools()
    assert socket.sent[2]["config"]["instructions"] == inherited


@pytest.mark.parametrize("name", ["wait_for_user", "delegate_to_main_agent"])
def test_result_can_wait_without_fabricating_user_text_but_cannot_delegate_empty(name):
    from packages.core.services.voice.realtime import parse_completed_bridge_call

    event = {
        "type": "response.done",
        "response": {
            "id": "result-response",
            "status": "completed",
            "output": [{
                "type": "function_call",
                "status": "completed",
                "name": name,
                "call_id": "result-wait",
                "arguments": '{"utterance":""}',
            }],
        },
    }

    call = parse_completed_bridge_call(event, seen_event_ids=set(), seen_call_ids=set())

    assert (call is not None) is (name == "wait_for_user")


@pytest.mark.parametrize(
    "body",
    ["ordinary " * 1_000, "你好世界" * 750, "😀" * 2_300, '"\\' * 2_000],
    ids=["ascii", "cjk", "emoji", "json-escaping"],
)
@pytest.mark.parametrize("provider", ["vercel", "openai"])
async def test_browser_startup_fits_wire_bytes_without_lowering_model_budget(body, provider):
    from unittest.mock import AsyncMock

    from packages.core.services.voice.browser import BrowserVoiceSession, browser_session_update
    from packages.core.services.voice import realtime
    from tests.test_browser_voice import Socket

    agent = "Fixture travel Agent. Never reveal unauthorized Workspace data."
    summary = {"role": "system", "content": "Summary: Boeing tour, lunch, then trails."}
    turns = [{"role": "user", "content": f"old-{index}: {body}"} for index in range(30)]
    latest = {"role": "user", "content": "最新要求：午餐后一直逛到傍晚。"}
    history = [summary, *turns, latest]
    original = json.dumps(history, ensure_ascii=False)
    model = "openai/gpt-realtime-2.1"
    full = live_voice_session_factory.instructions(agent, history)
    assert runtime_estimate_tokens_for_text(full) < live_voice_instruction_token_budget(model)

    def wire(instructions):
        event = browser_session_update(model, instructions=instructions, provider=provider)
        return json.dumps(
            realtime._vercel_session_update_event(event["session"]),
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")

    assert len(wire(full)) > realtime.VERCEL_REALTIME_MAX_MESSAGE_BYTES
    call = BrowserVoiceSession(
        Socket(),
        route=RealtimeRoute("test", "https://ai-gateway.vercel.sh/v1", model, True, provider=provider),
        entity_id="entity", user_id="user", conversation_id="conversation",
        workspace_id="workspace", agent_id="agent",
        check_access=AsyncMock(), agent=AsyncMock(),
        live_context=AsyncMock(return_value=(agent, history)),
    )

    rendered = await call._resolve_live_instructions("")

    if provider == "openai":
        assert rendered == full  # Vercel wire limits must not shrink native context.
    else:
        assert len(wire(rendered)) <= realtime.VERCEL_REALTIME_MAX_MESSAGE_BYTES
        assert "old-0:" not in rendered
        first = next(index for index in range(30) if f"old-{index}:" in rendered)
        assert rendered == live_voice_session_factory.instructions(agent, [summary, *turns[first:], latest])
        assert len(wire(live_voice_session_factory.instructions(agent, [summary, *turns[first-1:], latest]))) > realtime.VERCEL_REALTIME_MAX_MESSAGE_BYTES
    assert runtime_estimate_tokens_for_text(rendered) <= call.live_instruction_token_budget
    assert agent in rendered and summary["content"] in rendered and latest["content"] in rendered
    assert json.dumps(history, ensure_ascii=False) == original
    call.live_context.assert_awaited_once_with("")
    call.agent.assert_not_awaited()


@pytest.mark.parametrize("protected", ["agent", "summary"])
def test_transport_budget_never_silently_cuts_protected_identity_or_summary(protected):
    from packages.core.services.voice.live_agent import LiveVoiceContextTooLarge
    from packages.core.services.voice import realtime

    agent = "policy " * 40_000 if protected == "agent" else "Keep Agent identity."
    summary = [{"role": "system", "content": "facts " * 45_000}] if protected == "summary" else []

    with pytest.raises(LiveVoiceContextTooLarge, match="transport message limit"):
        live_voice_session_factory.instructions(
            agent, [*summary, {"role": "user", "content": "Recent request"}],
            fits_transport=lambda value: realtime.vercel_realtime_session_fits({"instructions": value}),
        )


def test_transport_budget_counts_config_and_double_json_escaping():
    from packages.core.services.voice import realtime

    agent = "Keep Agent identity."
    history = [{"role": "user", "content": '"\\' * 24_000}, {"role": "user", "content": "newest"}]
    full = live_voice_session_factory.instructions(agent, history)
    session = {"instructions": full, "tools": [{"description": "schema " * 15_000}]}
    assert len(full.encode("utf-8")) < realtime.VERCEL_REALTIME_MAX_MESSAGE_BYTES
    assert not realtime.vercel_realtime_session_fits(session)

    fitted = live_voice_session_factory.instructions(
        agent, history,
        fits_transport=lambda value: realtime.vercel_realtime_session_fits({**session, "instructions": value}),
    )

    assert fitted == live_voice_session_factory.instructions(agent, history[-1:])
