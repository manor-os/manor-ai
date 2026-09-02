import importlib
import json

import pytest

from packages.core.ai.agentic_loop import _compact_search_tools_result_for_context
from packages.core.ai.runtime.integration_setup_links import runtime_integration_setup_link
from packages.core.ai.runtime.tool_availability import runtime_mcp_credentials_unavailable_result
from packages.core.ai.runtime.tool_discovery import runtime_search_tools_payload


@pytest.mark.parametrize("provider", ["gmail", "google_calendar", "chrome", "custom-MCP"])
def test_setup_link_preserves_exact_catalog_identity(provider):
    result = runtime_integration_setup_link(provider)
    assert result["setup_url"] == f"/integrations?provider={provider}"
    assert f"]({result['setup_url']})" in result["setup_hint"]
    assert "does not connect" in result["setup_hint"]


@pytest.mark.parametrize("provider", ["", "../gmail", "gmail&provider=chrome", "gmail\n", "x" * 129])
def test_invalid_provider_cannot_create_setup_link(provider):
    assert runtime_integration_setup_link(provider) == {}


def test_unavailable_credentials_offer_setup_without_changing_failure_or_chrome_stop():
    for provider in ("gmail", "chrome"):
        result = json.loads(runtime_mcp_credentials_unavailable_result(
            provider=provider, tool_name="send_message", reason="Not connected", offer_setup=True,
        ))
        assert result["error"] == "credentials_unavailable"
        assert result["reason"] == "Not connected"
        assert result["setup_url"] == f"/integrations?provider={provider}"
        if provider == "chrome":
            assert result["stop_parent"] is True
            assert result["recommended_next_action"] == "pair_cli_worker"
    internal_error = json.loads(runtime_mcp_credentials_unavailable_result(
        provider="gmail", tool_name="send_message", reason="entity_id is required",
    ))
    assert "setup_url" not in internal_error


def test_discovery_includes_configuration_link_for_unavailable_exact_mcp():
    result = runtime_search_tools_payload(matches=[{
        "name": "mcp__gmail__send_message", "available": False, "reason": "not connected",
    }], query="send an email")
    assert result["loaded_tools"] == []
    assert result["integration_setup"][0]["setup_url"] == "/integrations?provider=gmail"


def test_many_unavailable_tools_share_one_setup_link_per_provider():
    result = runtime_search_tools_payload(matches=[{
        "name": f"mcp__gmail__action_{index}", "server_key": "gmail", "available": False,
    } for index in range(200)], query="email operations")
    assert len(result["unavailable_mcp"]) == 200
    assert len(result["integration_setup"]) == 1


def test_empty_discovery_links_only_known_unusable_provider_not_unknown_or_suppressed():
    for reason in ("not_usable", "unknown_server", "outside_active_user_intent"):
        result = runtime_search_tools_payload(matches=[], query="gmail", suppressed_mcp=[{
            "server_key": "gmail", "reason": reason,
        }])
        if reason == "not_usable":
            assert result["integration_setup"][0]["setup_url"] == "/integrations?provider=gmail"
        else:
            assert "integration_setup" not in result


@pytest.mark.parametrize("match_count", [0, 1, 200])
def test_setup_links_survive_search_context_compaction(match_count):
    providers = ["gmail", "chrome", "custom-MCP"]
    result = runtime_search_tools_payload(
        matches=[{
            "name": f"mcp__{provider}__action_{index}",
            "server_key": provider, "available": False,
        } for provider in providers for index in range(match_count)],
        query="check integration configuration",
        suppressed_mcp=[
            {"server_key": provider, "reason": "not_usable"}
            for provider in providers
        ] if not match_count else [],
    )

    compact = json.loads(_compact_search_tools_result_for_context(result, []))

    assert compact["integration_setup"] == [
        runtime_integration_setup_link(provider) for provider in providers
    ]
    assert compact["loaded_tools"] == []
    assert "matches" not in compact


@pytest.mark.parametrize("reason", [None, "unknown_server", "outside_active_user_intent"])
def test_compaction_does_not_invent_setup_links(reason):
    result = runtime_search_tools_payload(
        matches=[] if reason else [{"name": "mcp__gmail__list_messages", "available": True}],
        query="gmail",
        suppressed_mcp=[{"server_key": "gmail", "reason": reason}] if reason else [],
    )
    compact = json.loads(_compact_search_tools_result_for_context(result, result["loaded_tools"]))
    assert "integration_setup" not in compact
    assert compact["loaded_tools"] == result["loaded_tools"]


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_matches", [False, True])
async def test_agentic_loop_delivers_setup_link_to_next_model_round(monkeypatch, empty_matches):
    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    search_result = runtime_search_tools_payload(
        matches=[] if empty_matches else [{
            "name": "mcp__gmail__list_messages", "server_key": "gmail", "available": False,
        }],
        query="browse_server:gmail",
        suppressed_mcp=[{"server_key": "gmail", "reason": "not_usable"}] if empty_matches else [],
    )
    rounds = 0
    executed = []

    async def model_round(messages, tools, **kwargs):
        nonlocal rounds
        rounds += 1
        assert [tool["function"]["name"] for tool in tools] == ["search_tools"]
        if rounds == 1:
            return "", [{"id": "search_1", "name": "search_tools", "arguments": {"query": "browse_server:gmail"}}], {}
        tool_message = next(message for message in reversed(messages) if message["role"] == "tool")
        context = json.loads(tool_message["content"])
        assert context["integration_setup"] == [runtime_integration_setup_link("gmail")]
        assert context["loaded_tools"] == []
        return "Connect [Gmail](/integrations?provider=gmail)", [], {}

    async def execute(name, arguments):
        executed.append((name, arguments))
        return json.dumps(search_result)

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model_round)
    result = await loop_module.agentic_loop(
        system_prompt="Check configuration only; do not read or send email.",
        user_message="Is Gmail connected? Give me its setup link if needed.",
        tools=[{"type": "function", "function": {"name": "search_tools"}}],
        tool_executor=execute,
        max_rounds=2,
    )
    assert rounds == 2
    assert executed == [("search_tools", {"query": "browse_server:gmail"})]
    assert result.stop_reason == "completed"
    assert result.content == "Connect [Gmail](/integrations?provider=gmail)"
