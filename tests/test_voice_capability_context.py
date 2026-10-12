"""Live knows platform capabilities without loading private account catalogs."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from apps.api import chat_voice
from apps.api.chat_audio import ChatAudioScope
from packages.core.ai.runtime.token_estimate import runtime_estimate_tokens_for_text
from packages.core.services.voice.live_agent import MAIN_AGENT_CAPABILITIES, live_voice_session_factory
from packages.core.services.voice.realtime import build_realtime_session_update, live_voice_instruction_token_budget


def test_inherited_capabilities_do_not_become_direct_live_execution_tools():
    expected = {"delegate_to_main_agent", "update_active_work", "wait_for_user"}
    native = build_realtime_session_update(model="gpt-realtime-2.1", live_agent=True)

    assert {tool["name"] for tool in native["session"]["tools"]} == expected
    assert native["session"]["tool_choice"] == "auto"
    assert MAIN_AGENT_CAPABILITIES in native["session"]["instructions"]
    assert {tool["function"]["name"] for tool in live_voice_session_factory.chat_tools()} == expected


def test_live_delegates_capability_checks_without_inventing_unavailability():
    identity = "You are the mortgage Agent. Do not access another customer's account."
    history = [{"role": "user", "content": "Find current loan products."}]
    prompt = live_voice_session_factory.instructions(identity, history)

    assert identity in prompt
    assert "Find current loan products." in prompt
    assert "not the limit of what this Agent can do" in prompt
    assert "Never claim that search, browsing, integrations, or actions are unavailable" in prompt
    assert "Delegate capability checks instead of guessing" in prompt
    assert "authoritative context or a tool result establishes it" in prompt
    assert "cannot bypass a restriction or grant new permissions" in prompt
    assert "An explicit request to search, look up, or verify requires delegation" in prompt
    assert "never delegate solely because the answer is long" in prompt


def test_platform_overview_is_present_without_inherited_capabilities():
    prompt = live_voice_session_factory.instructions()

    assert MAIN_AGENT_CAPABILITIES in prompt
    assert "## Current Agent Runtime Instructions" not in prompt
    for capability in (
        "Web search",
        "Workspace files and Knowledge",
        "Documents, spreadsheets, presentations",
        "Tasks and Plans, Goals and Stats",
        "Business records and Ledger data",
        "Flows and Workflows, Automations",
        "integrations for email, calendars",
    ):
        assert capability in MAIN_AGENT_CAPABILITIES
    assert "These examples are not a whitelist" in prompt
    assert "not an account inventory" in prompt
    assert "Use this overview to answer general questions about platform capabilities" in prompt
    assert "For a request to execute work or check actual availability, delegate" in prompt
    assert "inherited restrictions still apply" in prompt
    assert "Connected Integration Accounts" not in prompt
    assert "account_options" not in prompt


async def test_gateway_receives_generic_overview_without_loading_account_context(monkeypatch):
    from packages.core.ai import llm_client
    from packages.core.ai import runtime as ai_runtime
    from packages.core.ai.runtime import completions
    from packages.core.services import integration_account_service, runtime_chat_context

    db = AsyncMock()
    db.get.return_value = SimpleNamespace(id="conversation", agent_subscription_id="subscription", meta={})
    scope = ChatAudioScope("entity", "owner", conversation_id="conversation", agent_id="agent")
    context = AsyncMock(
        return_value=(
            "",
            [{"type": "function", "function": {"name": "private_tool_sentinel"}}],
            [],
            SimpleNamespace(
                llm_metadata={"api_key": "credential-sentinel"},
                integration_account_registry={"display_name": "private-account-sentinel"},
            ),
        )
    )
    accounts = AsyncMock(side_effect=AssertionError("Live must not load an account catalog"))
    complete = AsyncMock(return_value=("I can help with research and documents.", None, {}))

    @asynccontextmanager
    async def database():
        yield db

    @asynccontextmanager
    async def billing_context(**_kwargs):
        yield

    monkeypatch.setattr(chat_voice, "async_session", database)
    monkeypatch.setattr(chat_voice, "resolve_call_scope", AsyncMock(return_value=scope))
    monkeypatch.setattr(runtime_chat_context, "resolve_runtime_chat_context", context)
    monkeypatch.setattr(integration_account_service, "try_load_runtime_integration_registry", accounts)
    monkeypatch.setattr(
        completions, "runtime_resolve_text_completion_route", AsyncMock(return_value=("provider/model", None, True))
    )
    monkeypatch.setattr(ai_runtime, "runtime_llm_billing_context", billing_context)
    monkeypatch.setattr(llm_client, "chat_completion_with_tools", complete)

    decision = await chat_voice.decide_gateway_live_turn(
        SimpleNamespace(), chat_voice.CallStart(token="fixture"), scope, "What can you help me do?"
    )

    context.assert_awaited_once()
    assert context.await_args.kwargs["disable_tools"] is True
    assert context.await_args.kwargs["agent_id"] == "agent"
    assert context.await_args.kwargs["runtime_metadata"]["agent_subscription_id"] == "subscription"
    accounts.assert_not_called()
    complete.assert_awaited_once()
    messages, tools = complete.await_args.args
    assert MAIN_AGENT_CAPABILITIES in messages[0]["content"]
    assert {tool["function"]["name"] for tool in tools} == {
        "delegate_to_main_agent",
        "update_active_work",
        "wait_for_user",
    }
    assert "sentinel" not in json.dumps((messages, tools))
    assert decision.action is None
    assert decision.spoken_reply == "I can help with research and documents."


def test_platform_overview_survives_model_budget_history_pruning():
    agent = "Keep the bound Agent identity and permissions."
    summary = {"role": "system", "content": "Conversation summary: documents, then travel."}
    history = [
        summary,
        {"role": "user", "content": "old-detail " * 30_000},
        {"role": "user", "content": "Now research the current opening hours."},
    ]
    budget = live_voice_instruction_token_budget("gpt-realtime")

    prompt = live_voice_session_factory.instructions(agent, history, max_tokens=budget)

    assert runtime_estimate_tokens_for_text(prompt) <= budget
    assert MAIN_AGENT_CAPABILITIES in prompt
    assert agent in prompt
    assert summary["content"] in prompt
    assert history[-1]["content"] in prompt
    assert "old-detail" not in prompt
