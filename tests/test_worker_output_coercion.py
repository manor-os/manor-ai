import json

from packages.core.workers.internal import (
    _coerce_llm_text_result,
    _infer_prompt_backed_fields,
    _merge_tool_backed_fields_for_schema,
)
from packages.core.ai.runtime import runtime_prompt_with_output_schema


def test_coerce_fenced_json_array_for_array_schema():
    schema = {"type": "array", "items": {"type": "object"}}
    content = 'Here is the result:\n```json\n[{"post_number": 1}]\n```'

    assert _coerce_llm_text_result(content, schema) == [{"post_number": 1}]


def test_coerce_text_to_result_object_for_result_schema():
    schema = {"type": "object", "properties": {"result": {"type": "string"}}}
    content = "# Archive\n\nFull markdown body"

    assert _coerce_llm_text_result(content, schema) == {"result": content}


def test_coerce_plain_text_for_string_schema():
    schema = {"type": "string"}
    content = "Short reply draft"

    assert _coerce_llm_text_result(content, schema) == content


def test_coerce_result_wrapped_array_for_array_schema():
    schema = {"type": "array", "items": {"type": "object"}}
    content = '{"result": [{"post_number": 1}]}'

    assert _coerce_llm_text_result(content, schema) == [{"post_number": 1}]


def test_coerce_plain_markdown_to_single_required_report_field():
    schema = {
        "type": "object",
        "required": ["trend_report"],
        "properties": {"trend_report": {"type": "string"}},
    }
    content = "# Trend Research\n\n- Founder build-in-public threads are working."

    assert _coerce_llm_text_result(content, schema) == {"trend_report": content}


def test_prompt_backed_post_text_is_copied_without_fabricating_url():
    schema = {
        "type": "object",
        "required": ["post_url", "post_text"],
        "properties": {
            "post_url": {"type": "string"},
            "post_text": {"type": "string"},
        },
    }
    prompt = """
    Here is the exact post text to publish:

    ---
    Every founder hits a wall.

    The useful move is to talk to ten users before you quit.
    ---
    """

    result = _infer_prompt_backed_fields({"text": "Published."}, prompt=prompt, schema=schema)

    assert result["post_text"].startswith("Every founder hits")
    assert "post_url" not in result


def test_tool_backed_tweet_publish_fields_are_merged_from_tool_result():
    schema = {
        "type": "object",
        "required": ["tweet_id", "published_at", "status"],
        "properties": {
            "tweet_id": {"type": "string"},
            "published_at": {"type": "string"},
            "status": {"type": "string"},
            "tweet_url": {"type": "string"},
        },
    }
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_publish",
                    "type": "function",
                    "function": {
                        "name": "mcp__twitter_x__create_tweet",
                        "arguments": json.dumps({"text": "We shipped a safer runtime."}),
                    },
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_publish",
            "content": json.dumps(
                {
                    "data": {
                        "id": "1999000000000000002",
                        "text": "We shipped a safer runtime.",
                        "created_at": "2026-05-21T13:00:00.000Z",
                        "edit_history_tweet_ids": ["1999000000000000002"],
                    },
                }
            ),
        },
    ]

    result = _merge_tool_backed_fields_for_schema(
        {"text": "Published successfully."},
        messages,
        schema=schema,
    )

    assert result["tweet_id"] == "1999000000000000002"
    assert result["published_at"] == "2026-05-21T13:00:00.000Z"
    assert result["status"] == "published"
    assert result["tweet_url"] == "https://x.com/i/web/status/1999000000000000002"


def test_prompt_with_output_schema_instructs_machine_readable_output():
    prompt = runtime_prompt_with_output_schema("Research trends.", {"type": "array"})

    assert "Return ONLY a value that conforms to this JSON Schema" in prompt
    assert '"type": "array"' in prompt


def test_llm_text_result_prefers_schema_matching_json_candidate():
    schema = {
        "type": "object",
        "required": ["scripts"],
        "properties": {
            "scripts": {
                "type": "array",
                "minItems": 2,
                "items": {"type": "object"},
            }
        },
    }
    first_script = {"script_id": "MF-01", "title": "First"}
    scripts = [first_script, {"script_id": "MF-02", "title": "Second"}]
    content = f"Input echo: {json.dumps(first_script)}\n\nFinal output:\n{json.dumps({'scripts': scripts})}"

    assert _coerce_llm_text_result(content, schema) == {"scripts": scripts}
