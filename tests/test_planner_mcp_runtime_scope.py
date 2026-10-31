"""Service MCP catalogs must describe each service's executable runtime scope."""

from packages.core.ai.runtime.tool_bindings import RuntimeMCPProviderToolScope
from packages.core.services.agent_effective_capabilities import (
    _filter_service_provider_actions_to_runtime_scope,
)


def test_cached_actions_respect_exact_names_and_disabled_provider_scope():
    actions = {"gmail": ["list_messages", "send_message"], "youtube": ["publish_video"]}
    specs = {
        provider: {action: {"name": action} for action in names}
        for provider, names in actions.items()
    }

    filtered, filtered_specs = _filter_service_provider_actions_to_runtime_scope(
        actions,
        specs,
        exact_tool_names={"mcp__gmail__send_message"},
        provider_scopes=(
            RuntimeMCPProviderToolScope("gmail", frozenset({"list_messages"})),
        ),
    )

    assert filtered == {"gmail": ["list_messages", "send_message"]}
    assert filtered_specs == {"gmail": specs["gmail"]}


def test_dynamic_provider_wildcard_keeps_actions_before_static_schema_registration():
    actions = {"apify": ["run_actor", "get_result"]}
    specs = {"apify": {action: {"name": action} for action in actions["apify"]}}

    filtered, filtered_specs = _filter_service_provider_actions_to_runtime_scope(
        actions,
        specs,
        exact_tool_names=set(),
        provider_scopes=(RuntimeMCPProviderToolScope("apify", None),),
    )

    assert filtered == actions
    assert filtered_specs == specs


def test_same_cached_provider_does_not_transfer_actions_between_services():
    actions = {"gmail": ["list_messages", "send_message"]}
    specs = {"gmail": {action: {"name": action} for action in actions["gmail"]}}
    reader, _ = _filter_service_provider_actions_to_runtime_scope(
        actions,
        specs,
        exact_tool_names=set(),
        provider_scopes=(
            RuntimeMCPProviderToolScope("gmail", frozenset({"list_messages"})),
        ),
    )
    writer, _ = _filter_service_provider_actions_to_runtime_scope(
        actions,
        specs,
        exact_tool_names=set(),
        provider_scopes=(
            RuntimeMCPProviderToolScope("gmail", frozenset({"send_message"})),
        ),
    )

    assert reader == {"gmail": ["list_messages"]}
    assert writer == {"gmail": ["send_message"]}
