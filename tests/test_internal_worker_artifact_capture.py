from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from packages.core.workers import internal


def test_collect_artifact_refs_from_subagent_tool_messages() -> None:
    messages = [
        {"role": "assistant", "content": "Calling image tool"},
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "kind": "image",
                    "image_url": "/api/v1/fs/ent/campaign-cover.png",
                    "fs_path": "Campaigns/campaign-cover.png",
                }
            ),
        },
    ]

    refs = internal._collect_artifact_refs_from_agent_messages(messages)

    assert {ref["source"] for ref in refs} >= {"image_url", "fs_path"}


def test_collect_artifact_refs_ignores_knowledge_sources() -> None:
    messages = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "context": "[Document 1] Unit Inventory",
                    "source_count": 1,
                    "sources": [{"document_id": "doc_1", "name": "Unit Inventory"}],
                    "documents": [
                        {
                            "id": "doc_1",
                            "name": "Unit Inventory",
                            "fs_path": "Unit Inventory & Availability.md",
                        }
                    ],
                }
            ),
        },
    ]

    assert internal._collect_artifact_refs_from_agent_messages(messages) == []


def test_pending_action_from_subagent_workspace_operation_tool_message() -> None:
    messages = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "__hitl__": True,
                    "approval_token": "draft_123",
                    "hitl": {
                        "id": "draft_123",
                        "type": "approval",
                        "prompt": "Apply this workspace operation draft?",
                        "action": "workspace.operation.apply",
                        "tool": "workspace_operation",
                        "options": ["approve", "reject"],
                    },
                    "operation": {
                        "kind": "workspace_operation_review",
                        "draft_id": "draft_123",
                        "changed_keys": ["rules"],
                    },
                }
            ),
        },
    ]

    pending = internal._pending_action_from_agent_messages(messages)

    assert pending is not None
    assert pending["kind"] == "workspace_operation_review"
    assert pending["draft_id"] == "draft_123"
    assert pending["action"] == "workspace.operation.apply"


def test_agentic_loop_error_is_not_misreported_as_schema_failure() -> None:
    result = SimpleNamespace(
        stop_reason="error",
        error="llm_call_failed",
        error_detail={"message": "No usable LLM API key found."},
        content="Fallback assistant text",
    )

    with pytest.raises(RuntimeError) as exc:
        internal._raise_if_agentic_loop_failed(result)

    assert "No usable LLM API key found" in str(exc.value)


def test_agentic_loop_generic_error_uses_content_detail() -> None:
    result = SimpleNamespace(
        stop_reason="error",
        error="llm_call_failed",
        error_detail=None,
        content="Sorry, the request failed.\n\nError detail: No usable LLM API key found.",
    )

    with pytest.raises(RuntimeError) as exc:
        internal._raise_if_agentic_loop_failed(result)

    assert "No usable LLM API key found" in str(exc.value)


def test_agentic_loop_max_rounds_with_content_is_usable() -> None:
    result = SimpleNamespace(
        stop_reason="max_rounds",
        error=None,
        error_detail=None,
        content='{"memo_text": "Internal memo ready."}',
    )

    internal._raise_if_agentic_loop_failed(result)


def test_merge_artifact_refs_preserves_text_and_adds_image_url() -> None:
    result = internal._merge_artifact_refs(
        {"text": "Generated the concept image."},
        [{"type": "image", "source": "image_url", "url": "/api/v1/fs/ent/a.png"}],
    )

    assert result["text"] == "Generated the concept image."
    assert result["image_url"] == "/api/v1/fs/ent/a.png"
    assert result["files"][0]["url"] == "/api/v1/fs/ent/a.png"
