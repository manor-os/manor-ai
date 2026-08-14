from packages.core.services.integration_operation_catalog import (
    integration_operation_catalog,
    normalize_mcp_operations,
)


def test_normalize_mcp_operations_preserves_schema_and_classifies_effect():
    operations = normalize_mcp_operations(
        "gmail",
        [
            {
                "name": "send_message",
                "description": "Send an email.",
                "inputSchema": {
                    "type": "object",
                    "required": ["to", "body"],
                    "properties": {
                        "to": {"type": "string"},
                        "body": {"type": "string"},
                    },
                },
            },
            {
                "name": "delete_draft",
                "parameters": {
                    "type": "object",
                    "required": ["draft_id"],
                    "properties": {"draft_id": {"type": "string"}},
                },
            },
        ],
    )

    by_name = {item["name"]: item for item in operations}
    assert by_name["send_message"] == {
        "name": "send_message",
        "label": "Send message",
        "resource": "Message",
        "description": "Send an email.",
        "effect": "write",
        "input_schema": {
            "type": "object",
            "required": ["to", "body"],
            "properties": {
                "to": {"type": "string"},
                "body": {"type": "string"},
            },
        },
    }
    assert by_name["delete_draft"]["resource"] == "Draft"
    assert by_name["delete_draft"]["effect"] == "destructive"


def test_normalize_mcp_operations_accepts_openai_function_and_full_tool_name():
    operations = normalize_mcp_operations(
        "github",
        [
            {
                "type": "function",
                "function": {
                    "name": "mcp__github__list_issues",
                    "description": "List repository issues.",
                    "parameters": {
                        "type": "object",
                        "properties": {"repo": {"type": "string"}},
                    },
                },
            },
            {"name": "mcp__github__list_issues"},
        ],
    )

    assert len(operations) == 1
    assert operations[0]["name"] == "list_issues"
    assert operations[0]["resource"] == "Issue"
    assert operations[0]["effect"] == "read"
    assert operations[0]["input_schema"]["required"] == []


def test_integration_operation_catalog_uses_executable_builtin_surface():
    operations, source = integration_operation_catalog(
        server_key="gmail",
        transport="builtin",
        tools_cached={},
    )

    send_message = next(item for item in operations if item["name"] == "send_message")
    assert source == "builtin"
    assert send_message["input_schema"]["required"] == ["to", "subject", "body"]
    assert "body" in send_message["input_schema"]["properties"]


def test_integration_operation_catalog_normalizes_remote_cache():
    operations, source = integration_operation_catalog(
        server_key="linear",
        transport="http",
        tools_cached={
            "tools": [
                {
                    "name": "create_issue",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}},
                        "required": ["title"],
                    },
                },
            ],
        },
    )

    assert source == "cache"
    assert operations[0]["name"] == "create_issue"
    assert operations[0]["resource"] == "Issue"
