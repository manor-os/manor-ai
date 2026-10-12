from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("byok", [False, True])
async def test_summary_records_its_own_usage_inside_suppressed_chat(monkeypatch, byok):
    from packages.core.ai import llm_client
    from packages.core.ai.runtime import memory
    from packages.core.services import plan_gate

    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    parent = llm_client.LLMBillingContext(
        entity_id="entity-chat", source="chat", suppress=True, durable_per_call=True,
    )
    parent_token = llm_client._billing_ctx_var.set(parent)
    byok_token = llm_client._is_byok_call.set(False)
    records = []
    gate = AsyncMock(side_effect=AssertionError("Native BYOK must not use platform credits"))
    monkeypatch.setattr(plan_gate, "check", gate)

    def record(billing, payload, duration, *, reserved_credits):
        records.append((billing, payload, reserved_credits))
        llm_client.release_billing_in_flight(billing)

    async def completion(_messages, **kwargs):
        assert kwargs["conversation_id"] == "conversation-summary"
        assert kwargs["user_id"] == "user-summary"
        # Model routing marks native credentials before preflight. Keep the
        # real credit guard and usage recorder beneath this provider stub.
        llm_client.mark_byok_from_metadata({
            "llm_api_key": "sk-test-native-key-1234567890",
            "llm_base_url": "https://api.openai.com/v1",
            "_resolved_model": "openai/gpt-4o-mini",
        } if byok else None)
        if byok:
            await llm_client._preflight_credit_check()
        usage = {"prompt": 20, "completion": 10, "total": 30, "cost_usd": 0.01}
        llm_client._record_llm_call(
            call_type="chat_completion", model="openai/gpt-4o-mini", usage=usage,
        )
        return SimpleNamespace(content="summary", usage=usage)

    monkeypatch.setattr(llm_client, "_schedule_billing_record", record)
    monkeypatch.setattr(memory, "runtime_execute_text_completion", completion)
    try:
        result = await memory.runtime_execute_conversation_summary_completion(
            entity_id="entity-summary", user_id="user-summary", workspace_id=None,
            conversation_id="conversation-summary", text_block="USER: keep originals",
        )

        assert result.content == "summary"
        assert llm_client._billing_ctx_var.get() is parent
        assert len(records) == 1
        billing, payload, reserved = records[0]
        assert billing.entity_id == "entity-summary"
        assert billing.conversation_id == "conversation-summary"
        assert billing.source == "conversation_summary"
        assert billing.suppress is False
        assert payload["byok"] is byok
        assert (reserved == 0) is byok
        gate.assert_not_awaited()
    finally:
        llm_client._billing_ctx_var.reset(parent_token)
        llm_client._is_byok_call.reset(byok_token)
