"""Workspace delegation receipts are derived from settled tool results.

A delegated Workspace service run reports evidence, artifacts and Knowledge
receipts only from tool calls whose results confirm a terminal success; model
text and unsettled or generic-id payloads never count as delivery proof.
"""
from __future__ import annotations

import json

import pytest


def test_workspace_delegation_receipts_are_tool_derived_and_typed():
    from packages.core.ai.runtime import workspace_delegation_actions as actions

    messages = [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "mcp__twitter_x__post_tweet",
                    "arguments": json.dumps({"text": "launch"}),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": json.dumps({
                "ok": True,
                "tweet_id": "tweet-1",
                "tweet_url": "https://x.example/tweet-1",
                "document_id": "doc-1",
            }),
        },
    ]

    evidence, artifacts, knowledge = actions._delegation_receipts(
        messages,
        entity_id="ent-1",
        workspace_id="ws-1",
        task_id="task-1",
    )

    assert any(item.get("effect") == "publish" for item in evidence)
    assert any(item.get("document_id") == "doc-1" for item in artifacts)
    assert knowledge == [{
        "type": "knowledge_document",
        "source": "mcp__twitter_x__post_tweet",
        "document_id": "doc-1",
    }]
    assert actions._typed_delegation_result('{"summary":"done"}', None) == {
        "summary": "done",
    }
    schema = {
        "type": "object",
        "required": ["summary"],
        "properties": {"summary": {"type": "string"}},
        "additionalProperties": False,
    }
    assert actions._delegation_schema_error({"summary": "done"}, schema) is None
    assert "$.summary" in actions._delegation_schema_error(
        {"summary": 3}, schema
    )


def test_workspace_task_creation_is_an_effect_not_a_file_receipt():
    from packages.core.ai.runtime import workspace_delegation_actions as actions

    messages = [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "create-task-call", "type": "function",
                "function": {
                    "name": "workspace_create_task",
                    "arguments": json.dumps({"title": "Create V3 Smoke 交付.md"}),
                },
            }],
        },
        {
            "role": "tool", "tool_call_id": "create-task-call",
            "content": json.dumps({
                "created": True,
                "task": {
                    "id": "child-task",
                    "title": "Create and register Knowledge document: V3 Smoke 交付.md",
                    "description": "Save /api/v1/fs/ent-1/Workspaces/Demo/source.md",
                },
            }),
        },
    ]

    evidence, artifacts, knowledge = actions._delegation_receipts(
        messages, entity_id="ent-1", workspace_id="ws-1", task_id="parent-task",
    )

    assert any(ref.get("kind") == "tool_effect" for ref in evidence)
    assert artifacts == []
    assert knowledge == []


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "mcp__facebook__create_live_video",
            {"page_id": "page-1", "status": "UNPUBLISHED"},
        ),
        (
            "mcp__facebook__end_live_video",
            {"page_id": "page-1", "live_video_id": "video-1"},
        ),
    ],
)
def test_workspace_delegation_receipts_do_not_promote_external_mutations_to_publish(
    tool_name,
    arguments,
):
    from packages.core.ai.runtime import workspace_delegation_actions as actions

    evidence, _, _ = actions._delegation_receipts(
        [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments),
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": json.dumps({
                    "ok": True,
                    "status": "completed",
                    "id": "effect-1",
                }),
            },
        ],
        entity_id="ent-1",
        workspace_id="ws-1",
        task_id="task-1",
    )

    effects = [item for item in evidence if item.get("kind") == "tool_effect"]
    assert len(effects) == 1
    assert effects[0]["effect"] == "action"
    assert effects[0]["fields"]["operation"] in {"create", "modify"}


def test_workspace_delegation_receipts_treat_send_as_a_delivered_effect():
    from packages.core.ai.runtime import workspace_delegation_actions as actions

    evidence, _, _ = actions._delegation_receipts(
        [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "mcp__gmail__send_email",
                        "arguments": json.dumps({
                            "to": ["reader@example.com"],
                            "subject": "Launch",
                            "body": "Ready",
                        }),
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": json.dumps({"ok": True, "message_id": "message-1"}),
            },
        ],
        entity_id="ent-1",
        workspace_id="ws-1",
        task_id="task-1",
    )

    effects = [item for item in evidence if item.get("kind") == "tool_effect"]
    assert len(effects) == 1
    assert effects[0]["effect"] == "publish"
    assert effects[0]["fields"]["operation"] == "send"


def _runtime_tool_effects(
    tool_name: str,
    arguments: dict,
    payload: dict,
) -> list[dict]:
    from packages.core.ai.runtime import workspace_delegation_actions as actions

    evidence, _, _ = actions._delegation_receipts(
        [
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call-status",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments),
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call-status",
                "content": json.dumps(payload),
            },
        ],
        entity_id="ent-1",
        workspace_id="ws-1",
        task_id="task-1",
    )
    return [item for item in evidence if item.get("kind") == "tool_effect"]


def _gmail_send_effects(payload: dict) -> list[dict]:
    return _runtime_tool_effects(
        "mcp__gmail__send_email",
        {
            "to": ["reader@example.com"],
            "subject": "Launch",
            "body": "Ready",
        },
        payload,
    )


@pytest.mark.parametrize(
    "status",
    ["denied", "rejected", "expired", "timeout", "unknown"],
)
def test_workspace_delegation_receipts_reject_non_success_terminal_statuses(status):
    effects = _gmail_send_effects({
        "ok": True,
        "status": status,
        "message_id": "must-not-authorize-delivery",
    })

    assert effects == []


def test_workspace_delegation_receipts_fail_closed_on_missing_status_and_generic_id():
    assert _gmail_send_effects({
        "ok": True,
        "id": "request-1",
    }) == []


@pytest.mark.parametrize("status", ["pending", "queued", "processing"])
def test_workspace_delegation_receipts_do_not_treat_async_generic_id_as_terminal(status):
    assert _gmail_send_effects({
        "ok": True,
        "status": status,
        "id": "async-job-1",
    }) == []


@pytest.mark.parametrize(
    "status",
    ["created_pending", "not_dispatched", "provider_new_state"],
)
def test_unsettled_tool_results_create_no_artifact_or_knowledge_receipts(status):
    from packages.core.ai.runtime import workspace_delegation_actions as actions

    payload = {
        "ok": True,
        "status": status,
        "created": True,
        "path": "outputs/not-delivered.pdf",
        "url": "https://artifacts.example/not-delivered.pdf",
        "document_id": "not-delivered-document",
    }
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call-unsettled",
                "type": "function",
                "function": {
                    "name": "save_sandbox_file",
                    "arguments": json.dumps({
                        "path": "outputs/not-delivered.pdf",
                    }),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call-unsettled",
            "content": json.dumps(payload),
        },
    ]

    _, artifacts, knowledge = actions._delegation_receipts(
        messages,
        entity_id="ent-1",
        workspace_id="ws-1",
        task_id="task-1",
    )

    assert artifacts == []
    assert knowledge == []


def test_workspace_delegation_receipts_do_not_use_generic_id_as_delivery_proof():
    assert _gmail_send_effects({
        "ok": True,
        "status": "completed",
        "id": "request-1",
    }) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True},
        {"saved": True, "path": "outputs/report.pdf"},
        {"document_id": "document-1"},
    ],
    ids=["ok", "saved_path", "document_id"],
)
def test_workspace_delegation_receipts_accept_explicit_non_delivery_postconditions(
    payload,
):
    effects = _runtime_tool_effects(
        "save_sandbox_file",
        {"path": "outputs/report.pdf"},
        payload,
    )

    assert len(effects) == 1
    assert effects[0]["effect"] == "action"
    assert effects[0]["fields"]["operation"] not in {"publish", "send"}
    assert effects[0]["fields"]["delivery_confirmed"] is False


def test_workspace_delegation_receipts_reject_generic_id_only_for_other_actions():
    assert _runtime_tool_effects(
        "save_sandbox_file",
        {"path": "outputs/report.pdf"},
        {"id": "async-job-1"},
    ) == []


def test_workspace_delegation_receipts_accept_verified_typed_postcondition():
    effects = _gmail_send_effects({
        "ok": True,
        "id": "request-1",
        "postcondition": {
            "schema_version": "publication-receipt/v1",
            "platform": "gmail",
            "verification_status": "verified",
            "external_id": "message-typed-1",
            "published_at": "2026-09-19T00:00:00Z",
            "verified_at": "2026-09-19T00:00:01Z",
            "payload_hash": f"sha256:{'a' * 64}",
            "fallback_used": "none",
            "evidence": [{"kind": "message_id", "value": "message-typed-1"}],
            "attempts": [],
        },
    })

    assert len(effects) == 1
    assert effects[0]["effect"] == "publish"
    assert effects[0]["fields"]["delivery_confirmed"] is True
    assert effects[0]["fields"]["postcondition"]["verification_status"] == "verified"
