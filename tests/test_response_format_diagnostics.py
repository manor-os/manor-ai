"""Provider format fallback remains diagnosable after host reply recovery."""
import json

import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract


pytestmark = pytest.mark.unit
_FALLBACK = {"type": "json_schema", "reason": "unsupported_format"}


@pytest.mark.parametrize("unexpected", [
    None, "PRIVATE_PROVIDER_REPLY", ["json_schema"],
    {"type": "json_object", "reason": "unsupported_format"},
    {"type": "json_schema", "reason": "PRIVATE_PROVIDER_REPLY"},
])
def test_usage_keeps_only_controlled_format_fallback_fields(unexpected):
    total = {}
    loop_module._add_usage(total, {"response_format_fallback": unexpected})
    assert "response_format_fallback" not in total

    loop_module._add_usage(total, {"response_format_fallback": {
        **_FALLBACK, "provider_reply": "PRIVATE_PROVIDER_REPLY",
    }})
    loop_module._add_usage(total, {"response_format_fallback": unexpected})
    loop_module._add_usage(total, {"prompt": 3, "completion": 2, "total": 5})
    assert total["response_format_fallback"] == _FALLBACK
    assert "PRIVATE_PROVIDER_REPLY" not in json.dumps(total)
    assert (total["prompt"], total["completion"], total["total"]) == (3, 2, 5)


@pytest.mark.asyncio
async def test_repaired_loop_retains_earlier_provider_format_fallback(monkeypatch):
    calls = []

    async def no_credit():
        return None

    async def complete(messages, **kwargs):
        calls.append(kwargs)
        usage = {"prompt": 2, "completion": 1, "total": 3}
        if len(calls) == 1:
            return "Invalid reply format", {**usage, "response_format_fallback": {
                **_FALLBACK, "provider_reply": "PRIVATE_PROVIDER_REPLY",
            }}
        assert len(calls) == 2
        feedback = json.loads(messages[-1]["content"])
        assert feedback["error"] == "response_contract_invalid"
        return json.dumps({"response": {
            "kind": "answer", "text": "The requested explanation.",
            "outcome": {"status": "answered", "evidence_refs": [], "limitations": [], "claims": []},
        }}), usage

    monkeypatch.setattr(loop_module, "_preflight_credit_check", no_credit)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", complete)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="Explain the result", tools=[], tool_executor=None,
        final_response_contract=ChatEvidenceContract(), max_rounds=3,
    )

    assert result.error is None and result.stop_reason == "completed"
    assert result.content == "The requested explanation."
    assert result.tool_calls_made == [] and len(calls) == 2
    assert result.usage["response_format_fallback"] == _FALLBACK
    assert "PRIVATE_PROVIDER_REPLY" not in json.dumps(result.usage)
    assert (result.usage["prompt"], result.usage["completion"], result.usage["total"]) == (4, 2, 6)
