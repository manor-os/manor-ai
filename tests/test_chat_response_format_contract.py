"""Reply format recovery reaches the provider without repeating completed work."""
from copy import deepcopy
import json

from jsonschema import Draft202012Validator
import pytest

import packages.core.ai.agentic_loop as loop_module
from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
from packages.core.ai.runtime.final_response import FinalResponseAction
from packages.core.ai.runtime.workspace_response import WorkspaceChatResponseContract
from packages.core.services.workspace_execution_evidence import ExecutionEvidenceScope

pytestmark = pytest.mark.unit


def answer():
    return {"kind": "answer", "text": "The example product provides beverage mixing equipment.", "outcome": {
        "status": "answered", "evidence_refs": [], "limitations": [], "claims": [],
    }}


def validator(contract):
    response_format = contract.response_format
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["type"] == "object"
    assert "anyOf" not in schema and "oneOf" not in schema
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_answer_schema_includes_operation_specific_outcome():
    schema_validator = validator(ChatEvidenceContract())
    candidate = answer()
    candidate["outcome"] = {
        "status": "completed", "evidence_refs": ["fetch"], "limitations": [], "claims": [
            {"operation": "web_fetch", "status": "completed", "evidence_refs": ["fetch"]},
        ],
    }
    schema_validator.validate({"response": candidate})


@pytest.mark.parametrize("workspace", [False, True])
def test_provider_schema_requires_all_object_fields_and_no_root_union(workspace):
    scope = ExecutionEvidenceScope("entity", "workspace", "user", "conversation", "message")
    inner = WorkspaceChatResponseContract(scope) if workspace else None
    contract = ChatEvidenceContract(inner=inner)

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
            assert not {"oneOf", "const", "minLength", "maxLength", "maxItems"} & set(node)
            for child in node.values():
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)

    for response_format in (contract.response_format, contract.tool_response_format):
        check(response_format["json_schema"]["schema"])


@pytest.mark.asyncio
async def test_progress_envelope_only_valid_for_tool_rounds():
    contract = ChatEvidenceContract()
    candidate = {"response": {"kind": "progress", "text": "Checking the public page."}}
    Draft202012Validator(contract.tool_response_format["json_schema"]["schema"]).validate(candidate)
    assert list(validator(contract).iter_errors(candidate))
    assert contract.tool_progress_text(json.dumps(candidate)) == "Checking the public page."
    assert (await contract.validate(json.dumps(candidate))).action is FinalResponseAction.REVISE_REPLY


@pytest.mark.asyncio
async def test_transport_envelope_cannot_hide_extra_fields_or_unverified_evidence():
    contract = ChatEvidenceContract(requires_execution=True)
    candidate = answer()
    candidate["outcome"].update(status="completed", evidence_refs=["invented"], claims=[{
        "operation": "web_fetch", "status": "completed", "evidence_refs": ["invented"],
    }])
    validator(contract).validate({"response": candidate})
    rejected = await contract.validate(json.dumps({"response": candidate}))
    assert rejected.action is not FinalResponseAction.ACCEPT
    for invalid in ({"response": answer(), "extra": "hidden"},
                    {"response": {"response": answer()}}, {"response": []}):
        assert list(validator(contract).iter_errors(invalid))
        assert (await contract.validate(json.dumps(invalid))).action is not FinalResponseAction.ACCEPT


@pytest.mark.parametrize("field,value", [
    ("kind", "progress"), ("text", 123), ("extra", "uncontracted"),
])
def test_answer_schema_rejects_non_answer_and_uncontracted_fields(field, value):
    candidate = answer()
    candidate[field] = value
    assert list(validator(ChatEvidenceContract()).iter_errors({"response": candidate}))


@pytest.mark.parametrize("field,value", [
    ("status", "succeeded"), ("evidence_refs", "fetch"),
    ("claims", [{"operation": "web_fetch", "status": "answered", "evidence_refs": []}]),
    ("extra", "uncontracted"),
])
def test_answer_schema_rejects_invalid_outcome_fields(field, value):
    candidate = answer()
    candidate["outcome"][field] = value
    assert list(validator(ChatEvidenceContract()).iter_errors({"response": candidate}))


@pytest.mark.parametrize("field", ["kind", "text", "outcome"])
def test_answer_schema_requires_the_reply_envelope(field):
    candidate = answer()
    del candidate[field]
    assert list(validator(ChatEvidenceContract()).iter_errors({"response": candidate}))


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_text", [False, True])
async def test_host_validation_retains_text_and_limitation_bounds(empty_text):
    candidate = answer()
    if empty_text:
        candidate["text"] = ""
    else:
        candidate["outcome"]["limitations"] = [""]
    decision = await ChatEvidenceContract().validate(json.dumps(candidate))
    assert decision.action is FinalResponseAction.REVISE_REPLY
    assert decision.content == ""


@pytest.mark.asyncio
async def test_workspace_schema_preserves_host_verified_execution_summary():
    scope = ExecutionEvidenceScope("entity", "workspace", "user", "conversation", "message")
    fact = {"kind": "task", "id": "task", "revision": "revision", "title": "Research",
            "state": "accepted", "url": "/tasks/task", "origin": "current_turn"}

    async def read(current_scope, references):
        assert current_scope == scope
        assert references == [{"kind": "task", "id": "task", "scope": "current_turn", "revision": "revision"}]
        return [fact]

    contract = ChatEvidenceContract(inner=WorkspaceChatResponseContract(scope, reader=read))
    candidate = {"kind": "execution_summary", "references": [
        {"kind": "task", "id": "task", "scope": "current_turn", "revision": "revision"},
    ]}
    schema_validator = validator(contract)
    schema = contract.response_format["json_schema"]["schema"]
    assert schema["required"] == ["response"]
    branches = schema["properties"]["response"]["anyOf"]
    assert [branch["required"] for branch in branches] == [
        ["kind", "text", "outcome"], ["kind", "references"],
    ]
    assert list(schema_validator.iter_errors({"response": {"kind": "answer"}}))
    schema_validator.validate({"response": answer()})
    schema_validator.validate({"response": candidate})
    assert list(schema_validator.iter_errors({"response": {**candidate, "status": "running"}}))
    for invalid in ({**candidate, "text": "Already completed"}, {**candidate, "outcome": answer()["outcome"]}):
        assert list(schema_validator.iter_errors({"response": invalid}))
        rejected = await contract.validate(json.dumps({"response": invalid}))
        assert rejected.action is FinalResponseAction.REVISE_REPLY
        assert rejected.content == ""
    decision = await contract.validate(json.dumps({"response": candidate}))
    assert decision.action is FinalResponseAction.ACCEPT
    assert "waiting for dispatch" in decision.content
    assert decision.evidence == (fact,)
    discussion = await contract.validate(json.dumps({"response": answer()}))
    assert discussion.action is FinalResponseAction.ACCEPT
    assert discussion.content == answer()["text"]
    assert discussion.outcome == answer()["outcome"]


@pytest.mark.asyncio
async def test_json_parse_failure_reports_exact_location_without_candidate_content():
    candidate = '{"kind":"answer",\n"text":"PRIVATE_REQUEST_CONTENT",\n"outcome": invalid}'
    with pytest.raises(json.JSONDecodeError) as caught:
        json.loads(candidate)
    error = caught.value
    decision = await ChatEvidenceContract().validate(candidate)
    assert decision.action is FinalResponseAction.REVISE_REPLY
    assert decision.diagnostic["error"] == "response_contract_invalid"
    assert decision.diagnostic["details"]["validation_error"] == {
        "reason": "invalid_json", "message": error.msg,
        "line": error.lineno, "column": error.colno, "offset": error.pos,
    }
    assert "PRIVATE_REQUEST_CONTENT" not in json.dumps(decision.diagnostic)
    assert decision.content == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [False, True])
@pytest.mark.parametrize("candidate,reason", [
    ('{"kind":"answer","kind":"answer","text":"PRIVATE_REQUEST_CONTENT"}', "duplicate_json_field"),
    ('["PRIVATE_REQUEST_CONTENT"]', "json_object_required"),
    ("PRIVATE_REQUEST_CONTENT" * 7000, "reply_too_large"),
    (None, "invalid_reply_type"),
    ('{"value":' + '9' * 5000 + '}', "invalid_json_value"),
    ('{"value":NaN}', "invalid_json_value"),
    ('{"value":Infinity}', "invalid_json_value"),
], ids=["duplicate", "non-object", "oversize", "wrong-type", "large-int", "nan", "infinity"])
async def test_invalid_reply_diagnostic_distinguishes_parse_failure_classes(candidate, reason, workspace):
    scope = ExecutionEvidenceScope("entity", "workspace", "user", "conversation", "message")
    contract = WorkspaceChatResponseContract(scope) if workspace else ChatEvidenceContract()
    decision = await contract.validate(candidate)
    assert decision.action is FinalResponseAction.REVISE_REPLY
    assert decision.diagnostic["error"] == "response_contract_invalid"
    assert decision.diagnostic["details"]["validation_error"]["reason"] == reason
    assert "PRIVATE_REQUEST_CONTENT" not in json.dumps(decision.diagnostic)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["prose", "missing_outcome", "wrong_text_type", "extra_field"])
async def test_reply_repair_sends_schema_to_provider_and_never_repeats_tools(monkeypatch, invalid_kind):
    from packages.core.ai.runtime import agentic_llm

    tool_rounds, repair_requests, executed = [], [], []
    contract = ChatEvidenceContract(requires_execution=True)

    async def model(messages, tools, **kwargs):
        tool_rounds.append(deepcopy(messages))
        if len(tool_rounds) == 1:
            return "", [{"id": "fetch", "name": "web_fetch", "arguments": {}}], {}
        assert len(tool_rounds) == 2
        invalid = answer()
        if invalid_kind == "prose":
            return invalid["text"], None, {}
        if invalid_kind == "missing_outcome":
            del invalid["outcome"]
        elif invalid_kind == "wrong_text_type":
            invalid["text"] = ["PRIVATE_REQUEST_CONTENT"]
        else:
            invalid["PRIVATE_REQUEST_CONTENT"] = "extra"
        return json.dumps(invalid), None, {}

    async def provider(messages, **kwargs):
        repair_requests.append(deepcopy(kwargs))
        assert kwargs["response_format"] == contract.response_format
        assert kwargs["response_format"]["type"] == "json_schema"
        diagnostic = json.loads(messages[-1]["content"])
        expected_code = "response_outcome_required" if invalid_kind == "missing_outcome" else "response_contract_invalid"
        assert diagnostic["error"] == expected_code
        assert diagnostic["details"]["validation_error"]["reason"] == (
            "invalid_json" if invalid_kind == "prose" else "schema_validation_failed"
        )
        assert "PRIVATE_REQUEST_CONTENT" not in json.dumps(diagnostic)
        candidate = answer()
        candidate["outcome"] = {
            "status": "completed", "evidence_refs": ["fetch"], "limitations": [], "claims": [
                {"operation": "web_fetch", "status": "completed", "evidence_refs": ["fetch"]},
            ],
        }
        validator(contract).validate({"response": candidate})
        return json.dumps({"response": candidate}), {}

    async def execute(name, args):
        executed.append(name)
        return '{"content":"The example product provides beverage mixing equipment."}'

    async def no_credit():
        return None

    monkeypatch.setattr(loop_module, "_preflight_credit_check", no_credit)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(agentic_llm, "chat_completion", provider)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="Look up Oloso", tools=[
            {"type": "function", "function": {"name": "web_fetch", "parameters": {"type": "object"}}},
        ], tool_executor=execute, final_response_contract=contract, max_rounds=4,
    )
    assert result.stop_reason == "completed"
    assert result.error is None
    assert result.content == answer()["text"]
    assert result.outcome["evidence_refs"] == ["fetch"]
    assert len(tool_rounds) == 2 and len(repair_requests) == 1
    assert executed == ["web_fetch"]


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate,path,rule,details", [
    ({"kind": "answer"}, [], "required", {"missing_fields": ["text", "outcome"]}),
    ({**answer(), "text": ["PRIVATE_REQUEST_CONTENT"]}, ["text"], "type", {"expected": "string", "actual_type": "list"}),
    ({**answer(), "PRIVATE_REQUEST_CONTENT": "extra"}, [], "additionalProperties", {"unexpected_field_count": 1}),
    ({**answer(), "outcome": {**answer()["outcome"], "status": "PRIVATE_REQUEST_CONTENT"}}, ["outcome", "status"], "enum", {}),
    ({**answer(), "outcome": {**answer()["outcome"], "claims": [{}]}}, ["outcome", "claims", 0], "required", {"missing_fields": ["operation", "status", "evidence_refs"]}),
    ({**answer(), "outcome": {**answer()["outcome"], "limitations": ["PRIVATE_REQUEST_CONTENT" * 100]}}, ["outcome", "limitations", 0], "maxLength", {"expected": 2000}),
])
async def test_schema_diagnostic_precedes_execution_and_omits_rejected_values(candidate, path, rule, details):
    contract = ChatEvidenceContract(requires_execution=True)
    decision = await contract.validate(json.dumps({"response": candidate}))
    assert decision.action is FinalResponseAction.REVISE_REPLY
    diagnostic = decision.diagnostic["details"]["validation_error"]
    assert diagnostic["reason"] == "schema_validation_failed"
    assert diagnostic["path"] == path
    assert diagnostic["rule"] == rule
    for key, value in details.items():
        assert diagnostic[key] == value
    assert "PRIVATE_REQUEST_CONTENT" not in json.dumps(decision.diagnostic)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("candidate,path,rule,details", [
    ({"kind": "execution_summary"}, [], "required", {"missing_fields": ["references"]}),
    ({"kind": "execution_summary", "references": [{"kind": "task", "id": "task"}]}, ["references", 0], "required", {"missing_fields": ["scope"]}),
    ({"kind": "execution_summary", "references": [{"kind": "task", "id": "task", "scope": "PRIVATE_REQUEST_CONTENT"}]}, ["references", 0, "scope"], "enum", {"expected": ["current_turn", "existing_record"]}),
    ({"kind": "PRIVATE_REQUEST_CONTENT"}, ["kind"], "enum", {"expected": ["answer", "execution_summary"]}),
    ({}, [], "required", {"missing_fields": ["kind"]}),
])
async def test_workspace_schema_diagnostic_selects_reply_kind_before_reading_evidence(candidate, path, rule, details, wrapped):
    async def read(*args):
        raise AssertionError("Malformed references must not trigger evidence reads")

    scope = ExecutionEvidenceScope("entity", "workspace", "user", "conversation", "message")
    contract = WorkspaceChatResponseContract(scope, reader=read)
    if wrapped:
        contract = ChatEvidenceContract(inner=contract, requires_execution=True)
    decision = await contract.validate(json.dumps({"response": candidate}))
    assert decision.action is FinalResponseAction.REVISE_REPLY
    diagnostic = decision.diagnostic["details"]["validation_error"]
    assert diagnostic == {"reason": "schema_validation_failed", "path": path, "rule": rule, **details}
    assert "PRIVATE_REQUEST_CONTENT" not in json.dumps(decision.diagnostic)


@pytest.mark.asyncio
async def test_exhausted_schema_repair_retains_field_diagnostic_without_reopening_tools(monkeypatch):
    contract = ChatEvidenceContract(requires_execution=True)
    malformed = answer()
    del malformed["text"]
    calls = {"tools": 0, "repair": 0}

    async def model(messages, tools, **kwargs):
        calls["tools"] += 1
        assert calls["tools"] == 1
        return json.dumps(malformed), None, {}

    async def repair(messages, **kwargs):
        calls["repair"] += 1
        assert kwargs["response_format"] == contract.response_format
        assert "tools" not in kwargs and "tool_choice" not in kwargs
        return json.dumps(malformed), {}

    async def execute(*args):
        raise AssertionError("A malformed reply cannot authorize tool execution")

    async def no_credit():
        return None

    monkeypatch.setattr(loop_module, "_preflight_credit_check", no_credit)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", model)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", repair)
    result = await loop_module.agentic_loop(
        system_prompt="system", user_message="Read the public page", tools=[
            {"type": "function", "function": {"name": "web_fetch", "parameters": {"type": "object"}}},
        ], tool_executor=execute, final_response_contract=contract, max_rounds=6,
    )
    assert calls == {"tools": 1, "repair": 3}
    assert result.stop_reason == "error"
    assert result.error == "final_response_validation_failed"
    assert result.content == ""
    assert result.error_detail["details"]["validation_error"] == {
        "reason": "schema_validation_failed", "path": [], "rule": "required", "missing_fields": ["text"],
    }
