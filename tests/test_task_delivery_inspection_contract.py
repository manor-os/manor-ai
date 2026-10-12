"""Delivery navigation is not citation addressing or permission to replay work."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from jsonschema import Draft202012Validator
import pytest

from packages.core.ai import llm_client
from packages.core.contracts.task_delivery_inspection import (
    DELIVERY_INSPECTION_PARAMETERS,
    DeliveryInspectionError,
    DeliveryInspectionRequest,
    parse_delivery_inspection,
)
from packages.core.services import task_acceptance_review as acceptance


def _snapshot():
    return acceptance.acceptance_snapshot({
        "task": {"description": "Inspect every saved result."},
        "plan_id": "plan-1",
        "steps": [{"key": "repair", "kind": "subagent", "status": "done",
                   "result": {"text": "Verified result", "nested": {"answer": 42}},
                   "delivery_contract": {"schema": {"type": "object"}}}],
        "artifacts": [{"document_id": "doc-1", "text": "Verified artifact"}],
        "plan_dag": {"metadata": {"acceptance_contract": {"criteria": [{
            "key": "written", "description": "A saved file exists.",
        }]}}},
        "verification": {"issues": [{"code": "example_diagnostic"}]},
    })


def test_defaults_and_provider_null_are_explicit_not_inferred_from_citations():
    assert parse_delivery_inspection({}) == DeliveryInspectionRequest()
    assert parse_delivery_inspection({"kind": "step", "pointer": None}).as_arguments() == {
        "kind": "step", "index": 0, "offset": 0, "pointer": None,
    }
    original = {"kind": "step", "index": 0, "offset": 0, "pointer": "/steps/0/result"}
    frozen = deepcopy(original)
    with pytest.raises(DeliveryInspectionError) as caught:
        parse_delivery_inspection(original)
    payload = json.loads(caught.value.to_tool_result())
    assert payload["contract"] == "tool_error"
    assert payload["error"] == "invalid_inspection"
    assert payload["details"] == {
        "path": "$.pointer", "schema_rule": "forbidden_for_kind",
        "expected": {"kind": "step", "pointer": None},
    }
    assert original == frozen
    assert "recommended_next_calls" not in payload


@pytest.mark.parametrize("arguments,path,rule", [
    (None, "$", "type"),
    ([], "$", "type"),
    ({"kind": "other"}, "$.kind", "enum"),
    ({"kind": None}, "$.kind", "enum"),
    ({"index": "0"}, "$.index", "type"),
    ({"index": True}, "$.index", "type"),
    ({"index": 0.0}, "$.index", "type"),
    ({"index": -1}, "$.index", "minimum"),
    ({"offset": "0"}, "$.offset", "type"),
    ({"offset": False}, "$.offset", "type"),
    ({"offset": 0.0}, "$.offset", "type"),
    ({"offset": -1}, "$.offset", "minimum"),
    ({"pointer": 3}, "$.pointer", "type"),
    ({"unexpected": "secret"}, "$", "additionalProperties"),
    ({"approval_token": "secret"}, "$", "additionalProperties"),
    ({"kind": "manifest", "index": 1}, "$.index", "const"),
    ({"kind": "request", "index": 1}, "$.index", "const"),
    ({"kind": "evidence", "index": 1, "pointer": "/steps/0/result/text"}, "$.index", "const"),
    ({"kind": "evidence"}, "$.pointer", "required_for_kind"),
    ({"kind": "evidence", "pointer": None}, "$.pointer", "required_for_kind"),
    ({"kind": "evidence", "pointer": ""}, "$.pointer", "required_for_kind"),
])
def test_rejected_arguments_have_shared_constraints_without_values(arguments, path, rule):
    with pytest.raises(DeliveryInspectionError) as caught:
        parse_delivery_inspection(arguments)
    error = json.loads(caught.value.to_tool_result())
    assert error["details"]["path"] == path
    assert error["details"]["schema_rule"] == rule
    assert "expected" in error["details"]
    assert "secret" not in json.dumps(error)


@pytest.mark.parametrize("kind", [
    "manifest", "request", "criterion", "step", "artifact", "citations", "verification", "contract",
])
def test_every_non_evidence_page_rejects_pointer_and_accepts_null(kind):
    snapshot = _snapshot()
    with pytest.raises(DeliveryInspectionError) as caught:
        acceptance.inspect_task_delivery(snapshot, kind=kind, pointer="/steps/0/result/text")
    assert caught.value.details["path"] == "$.pointer"
    assert acceptance.inspect_task_delivery(snapshot, kind=kind, pointer=None)["status"] == "ok"


@pytest.mark.parametrize("arguments,path,rule", [
    ({"kind": "manifest", "offset": 10}, "$.offset", "maximum"),
    ({"kind": "citations", "offset": 10}, "$.offset", "maximum"),
    ({"kind": "request", "offset": 1000}, "$.offset", "maximum"),
    ({"kind": "step", "index": 9}, "$.index", "exclusiveMaximum"),
    ({"kind": "artifact", "index": 9}, "$.index", "exclusiveMaximum"),
    ({"kind": "citations", "index": 9}, "$.index", "exclusiveMaximum"),
    ({"kind": "criterion", "index": 9}, "$.index", "exclusiveMaximum"),
    ({"kind": "verification", "index": 9}, "$.index", "exclusiveMaximum"),
    ({"kind": "contract", "index": 9}, "$.index", "available_contract"),
    ({"kind": "evidence", "pointer": "/steps/0/result"}, "$.pointer", "scalar_evidence"),
    ({"kind": "evidence", "pointer": "/steps/0/result/missing"}, "$.pointer", "available_evidence_leaf"),
    ({"kind": "evidence", "pointer": "/task/description"}, "$.pointer", "available_evidence_leaf"),
    ({"kind": "evidence", "pointer": "/steps/0/result/~invalid"}, "$.pointer", "available_evidence_leaf"),
])
def test_snapshot_address_errors_remain_structured(arguments, path, rule):
    with pytest.raises(DeliveryInspectionError) as caught:
        acceptance.inspect_task_delivery(_snapshot(), **arguments)
    assert caught.value.details["path"] == path
    assert caught.value.details["schema_rule"] == rule
    assert "Verified result" not in caught.value.to_tool_result()


def test_required_page_addresses_round_trip_without_parsing_checkpoint_ids():
    snapshot = _snapshot()
    pages = acceptance.required_delivery_inspection_requests(snapshot)
    assert [page["page_id"] for page in pages] == acceptance.required_delivery_inspections(snapshot)
    assert {page["arguments"]["kind"] for page in pages} == {
        "manifest", "step", "artifact", "verification", "contract",
    }
    for page in pages:
        request = parse_delivery_inspection(page["arguments"])
        result = acceptance.inspect_task_delivery(snapshot, **request.as_arguments())
        assert result["status"] == "ok"
        assert len(json.dumps(result, ensure_ascii=False)) <= acceptance.INSPECTION_RESULT_MAX_CHARS
    leaf = acceptance.inspect_task_delivery(
        snapshot, kind="evidence", pointer="/steps/0/result/nested/answer",
    )
    assert leaf["content"] == "42" and leaf["value_type"] == "number"


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_actual_llm_transport_keeps_flat_schema_and_optional_pointer(monkeypatch, native):
    tool = {"type": "function", "function": {
        "name": "inspect_task_delivery", "description": "Inspect read-only delivery pages.",
        "parameters": deepcopy(DELIVERY_INSPECTION_PARAMETERS),
    }}
    original = deepcopy(tool)
    captured = []
    base_url = "https://api.anthropic.com/v1" if native else "https://openrouter.ai/api/v1"
    monkeypatch.setattr(llm_client, "resolve_llm_routing_for_model", AsyncMock(return_value=SimpleNamespace(
        api_key=("sk-ant-" if native else "sk-or-") + "x" * 40,
        base_url=base_url, provider="anthropic" if native else "openrouter", source="test",
    )))
    monkeypatch.setattr(llm_client, "_preflight_credit_check", AsyncMock())
    monkeypatch.setattr(llm_client, "_llm_stream_transport_enabled", lambda: False)
    monkeypatch.setattr(llm_client, "_apply_prompt_cache", lambda _payload: False)
    monkeypatch.setattr(llm_client, "_record_llm_call", lambda **_kwargs: None)

    async def post(url, headers, payload, **_kwargs):
        captured.append(deepcopy(payload))
        body = ({"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                 "usage": {"input_tokens": 1, "output_tokens": 1}}
                if native else {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        return httpx.Response(200, request=httpx.Request("POST", url), json=body)

    monkeypatch.setattr(llm_client, "_post_with_retry", post)
    content, calls, usage = await llm_client.chat_completion_with_tools(
        [{"role": "user", "content": "Inspect delivery"}], [tool], model="anthropic/claude-sonnet-4.6",
    )
    assert content == "ok", usage
    assert calls is None and len(captured) == 1
    schema = (captured[0]["tools"][0]["input_schema"] if native
              else captured[0]["tools"][0]["function"]["parameters"])
    assert schema == original["function"]["parameters"] and tool == original
    assert not {"anyOf", "oneOf", "allOf"}.intersection(schema)
    assert "pointer" not in schema.get("required", [])
    validator = Draft202012Validator(schema)
    assert validator.is_valid({})
    assert validator.is_valid({"kind": "step", "index": 0, "offset": 0, "pointer": None})
    assert "only" in schema["properties"]["pointer"]["description"]
