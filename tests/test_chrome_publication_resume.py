"""Publication consent survives fresh reads; tab cleanup has separate consent."""
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from packages.core.ai.agentic_loop import agentic_loop
from packages.core.ai.runtime.approval_messages import (
    approval_args_hash, runtime_approval_continuation, runtime_approval_runtime_metadata,
)
from packages.core.ai.runtime.tool_results import RuntimeNoEffectToolResult
from packages.core.models.hitl_request import HitlRequest
from tests.test_runtime_direct_chat_approval_preference import _setup
from tests.test_runtime_guard_unified_store import _runtime_envelope


PUBLISH = "mcp__chrome__click_element"
READ = "mcp__chrome__read_page"
FINALIZE = "mcp__chrome__finalize_tabs"
ARGS = {
    "tabId": 42, "ref": "e17", "snapshot_id": "before-approval",
    "integration_account_id": "account-one",
    "expect": {"kind": "page_state", "state": {"text": "Published"}},
    "operation_intent": {"effect": "publish", "publication": {
        "destination_url": "https://example.test/compose", "account": "author",
        "text": "Approved post", "document_ids": [],
    }},
}


@pytest.mark.oss_regression
def test_fresh_read_proof_requires_an_unexecuted_publication():
    payload = {"status": "read_page_required", "reason": "snapshot_mismatch", "action_executed": False}
    proof = RuntimeNoEffectToolResult.chrome_publication_fresh_read(json.dumps(payload))
    assert proof.matches_call(PUBLISH, ARGS)
    assert not proof.matches_call(READ, {"tabId": 42})
    assert not isinstance(str(proof), RuntimeNoEffectToolResult)
    # Host proof is action_executed=False. Status/reason/receipt are Chrome
    # diagnostics, not the no-effect predicate.
    for variant in (
        {**payload, "reason": "target_moved"},
        {**payload, "status": "action_failed", "reason": "network_error"},
        {**payload, "action_receipt": {"execution": "not_executed"}},
    ):
        assert RuntimeNoEffectToolResult.chrome_publication_fresh_read(
            json.dumps(variant),
        ).matches_call(PUBLISH, ARGS)
    omitted = {key: value for key, value in payload.items() if key != "action_executed"}
    for changed in ({**payload, "action_executed": True}, omitted):
        with pytest.raises(ValueError):
            RuntimeNoEffectToolResult.chrome_publication_fresh_read(json.dumps(changed))


def test_publication_consent_survives_fresh_locator_and_expectation():
    fresh = {**ARGS, "ref": "e91", "snapshot_id": "after-approval",
             "expect": {"kind": "page_state", "state": {"text": "Saved"}}}
    assert approval_args_hash(ARGS, tool_name=PUBLISH) == approval_args_hash(fresh, tool_name=PUBLISH)
    for field, value in (("text", "Different post"), ("account", "other"),
                         ("destination_url", "https://other.test"), ("document_ids", ["other"])):
        changed = {**fresh, "operation_intent": {"effect": "publish", "publication": {
            **ARGS["operation_intent"]["publication"], field: value,
        }}}
        assert approval_args_hash(ARGS, tool_name=PUBLISH) != approval_args_hash(changed, tool_name=PUBLISH)


def test_publication_resume_preserves_context_and_returns_control_to_agent():
    continuation = runtime_approval_continuation(PUBLISH, ARGS)
    assert continuation["arguments"]["snapshot_id"] == "before-approval"
    metadata = runtime_approval_runtime_metadata({"tool": PUBLISH, "continuation": continuation}, "grant")
    assert not metadata.get("forced_tool_calls")
    assert metadata["approved_tool_call"]["arguments"]["approval_token"] == "grant"
    assert {PUBLISH, READ, FINALIZE} <= set(metadata["extra_tool_names"])




@pytest.mark.asyncio
@pytest.mark.oss_regression
async def test_typed_target_moved_publication_lets_model_reread(monkeypatch):
    payload = {
        "status": "read_page_required", "reason": "target_moved", "action_executed": False,
        "action_receipt": {"execution": "not_executed"},
    }
    proof = RuntimeNoEffectToolResult.chrome_publication_fresh_read(json.dumps(payload))
    calls = []

    async def execute(name, _arguments):
        calls.append(name)
        if name == PUBLISH:
            return proof
        return json.dumps({"ok": True, "snapshot_id": "after-move"})

    schemas = [{"type": "function", "function": {"name": name, "description": name,
               "parameters": {"type": "object", "properties": {}}}} for name in (PUBLISH, READ)]
    completions = [
        ("", [{"id": "c1", "name": PUBLISH, "arguments": ARGS}], {"total": 1}),
        ("", [{"id": "c2", "name": READ, "arguments": {"tabId": 42}}], {"total": 1}),
        ("The page moved; I reread it before retrying.", None, {"total": 1}),
    ]
    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
               new=AsyncMock(side_effect=completions)), patch(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion", new_callable=AsyncMock,
    ) as text_only:
        result = await agentic_loop(
            system_prompt="Continue the approved publication.", user_message="Publish the video privately.",
            tools=schemas, tool_executor=execute, max_rounds=6,
        )
    assert result.stop_reason == "completed"
    assert result.error is None
    assert calls == [PUBLISH, READ]
    text_only.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.oss_regression
async def test_executed_target_moved_json_stays_unconfirmed():
    calls = []
    seen = []

    async def execute(name, _arguments):
        calls.append(name)
        if name == READ:
            return json.dumps({"ok": True, "snapshot_id": "after-move"})
        return json.dumps({
            "ok": False, "status": "read_page_required", "reason": "target_moved",
            "action_executed": True,
        })

    schemas = [{"type": "function", "function": {"name": name, "description": name,
               "parameters": {"type": "object", "properties": {}}}} for name in (PUBLISH, READ)]
    completions = [
        ("", [{"id": "c1", "name": PUBLISH, "arguments": ARGS}], {"total": 1}),
        ("", [{"id": "c2", "name": READ, "arguments": {"tabId": 42}}], {"total": 1}),
        ("I could not confirm whether the click ran.", None, {"total": 1}),
    ]

    async def model(messages, tools, **_kwargs):
        seen.append((json.dumps(messages), {tool["function"]["name"] for tool in tools or []}))
        return completions[len(seen) - 1]

    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion",
               new=model), patch(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion", new_callable=AsyncMock,
    ) as text_only:
        result = await agentic_loop(
            system_prompt="Continue the approved publication.", user_message="Publish the video privately.",
            tools=schemas, tool_executor=execute, max_rounds=6,
        )
    # Without host no-effect proof the executed click is never settled or
    # replayed: the model sees the original evidence, keeps its tools, reads
    # the page, and reports the unconfirmed outcome itself.
    assert result.stop_reason == "completed"
    assert result.error is None
    assert result.content == "I could not confirm whether the click ran."
    assert calls == [PUBLISH, READ]
    assert len(seen) == 3
    followup_messages, followup_tools = seen[1]
    assert "target_moved" in followup_messages
    assert '\\"action_executed\\":true' in followup_messages
    assert followup_tools == {PUBLISH, READ}
    text_only.assert_not_awaited()
