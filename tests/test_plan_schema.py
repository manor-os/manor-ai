"""Runtime capability catalog and action-binding schema helpers."""


def test_runtime_planner_capability_catalog_groups_actions_tools_and_skills():
    from packages.core.ai.runtime import runtime_planner_capability_catalog_dicts

    catalog = runtime_planner_capability_catalog_dicts(
        provider_actions={"twitter_x": ["publish_tweet", "search_tweets"]},
        platform_tools=["generate_file"],
        skills=[{"slug": "draft-posts", "name": "Draft Posts"}],
    )
    by_id = {entry["capability_id"]: entry for entry in catalog}

    assert by_id["external.social"]["provider_actions"][0]["action_key"] == "publish_tweet"
    assert "file.write" in by_id
    assert by_id["file.write"]["platform_tools"] == ["generate_file"]
    assert by_id["skill.invoke"]["skills"][0]["name"] == "Draft Posts"
    assert "search_tweets" not in str(catalog)


def test_runtime_planner_action_binding_carries_cached_tool_schema():
    from packages.core.ai.runtime import (
        runtime_planner_action_bindings,
        runtime_planner_action_specs_from_tools_cached,
    )

    specs = runtime_planner_action_specs_from_tools_cached(
        [
            {
                "name": "publish_tweet",
                "description": "Publish a tweet.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {"tweet_id": {"type": "string"}},
                },
                "account_ids": ["account-default", "account-secondary"],
                "requires_explicit_account": True,
                "supports_all_accounts": False,
                "account_input_schemas": {
                    "account-default": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "account-secondary": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                        "required": ["status"],
                    },
                },
                "account_output_schemas": {
                    "account-default": {
                        "type": "object",
                        "properties": {"tweet_id": {"type": "string"}},
                    },
                    "account-secondary": {
                        "type": "object",
                        "properties": {"post_id": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_message",
                    "description": "Send a message.",
                    "parameters": {
                        "type": "object",
                        "properties": {"to": {"type": "string"}},
                    },
                },
            },
        ]
    )

    bindings = runtime_planner_action_bindings(
        {"twitter_x": ["publish_tweet"], "gmail": ["send_message"]},
        provider_action_specs={"twitter_x": specs, "gmail": specs},
    )
    by_action = {binding.action_key: binding for binding in bindings}

    publish = by_action["publish_tweet"].to_dict(include_schema=True)
    send = by_action["send_message"].to_dict()

    assert publish["description"] == "Publish a tweet."
    assert publish["parameters"] == ["text"]
    assert publish["input_schema"]["required"] == ["text"]
    assert publish["output_schema"]["properties"]["tweet_id"]["type"] == "string"
    assert publish["account_ids"] == ["account-default", "account-secondary"]
    assert publish["requires_explicit_account"] is True
    assert publish["supports_all_accounts"] is False
    assert publish["account_input_schemas"]["account-secondary"]["required"] == [
        "status"
    ]
    assert publish["account_output_schemas"]["account-secondary"]["properties"][
        "post_id"
    ]["type"] == "string"
    assert by_action["publish_tweet"].input_schema_for("account-secondary")[
        "required"
    ] == ["status"]
    assert by_action["publish_tweet"].output_schema_for("account-secondary")[
        "properties"
    ]["post_id"]["type"] == "string"
    assert send["description"] == "Send a message."
    assert send["parameters"] == ["to"]


def test_runtime_planner_all_account_binding_uses_aggregate_output_schema():
    from jsonschema import validate

    from packages.core.ai.runtime.planning import RuntimePlannerActionBinding

    binding = RuntimePlannerActionBinding(
        provider="stripe",
        action_key="list_customers",
        output_schema={
            "type": "object",
            "required": ["customer_id"],
            "properties": {"customer_id": {"type": "string"}},
        },
        supports_all_accounts=True,
    )

    schema = binding.output_schema_for(None, selection="all")

    assert schema["x-manor-integration-account-fanout"] is True
    result_schema = schema["properties"]["results"]["items"]["properties"][
        "result"
    ]
    assert result_schema["anyOf"][0]["required"] == ["customer_id"]
    validate(
        {
            "integration_account_selection": "all",
            "status": "partial",
            "total_account_count": 2,
            "account_count": 2,
            "failed_count": 1,
            "result_truncated_count": 0,
            "omitted_account_count": 0,
            "results": [
                {
                    "integration_account_id": "account-ok",
                    "ok": True,
                    "result": {"customer_id": "cus_1"},
                },
                {
                    "integration_account_id": "account-failed",
                    "ok": False,
                    "result": {
                        "error": "account_call_failed",
                        "reason": "Provider unavailable",
                    },
                },
            ],
        },
        schema,
    )


def test_runtime_planner_all_account_binding_augments_strict_vendor_input_schema():
    from jsonschema import validate

    from packages.core.ai.runtime.planning import RuntimePlannerActionBinding

    binding = RuntimePlannerActionBinding(
        provider="stripe",
        action_key="list_customers",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        supports_all_accounts=True,
    )

    schema = binding.input_schema_for(None, selection="all")

    assert schema["additionalProperties"] is False
    assert schema["properties"]["integration_account_selection"] == {
        "type": "string",
        "const": "all",
    }
    validate(
        {"query": "active customers", "integration_account_selection": "all"},
        schema,
    )
