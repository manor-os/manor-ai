"""A successful CRM read needs a final reply without replaying the operation."""
import json
import pytest
from packages.core.ai.runtime.chat_evidence import ChatEvidenceContract
from packages.core.ai.runtime.final_response import FinalResponseAction

@pytest.mark.asyncio
async def test_reply_repair_explains_json_shape_and_preserves_current_crm_evidence():
    contract = ChatEvidenceContract()
    contract.observe_tool_result("manor", "crm-current", '{"ok":true,"items":[{"name":"Oloso"}]}', {"action":"list_clients"})
    decision = await contract.validate("目前有一个客户 Oloso。")
    assert decision.action is FinalResponseAction.REVISE_REPLY
    assert decision.diagnostic["error"] == "response_contract_invalid"
    message = decision.diagnostic["message"]
    assert "raw JSON object" in message and "Use no Markdown fences" in message
    assert "do not repeat settled effects or any tool call" in message
    assert decision.diagnostic["details"]["validation_error"]["reason"] == "invalid_json"
    aliases = decision.diagnostic["details"]["evidence_aliases"]
    assert aliases == [{"alias":"e1","call_id":"crm-current","operation":"list_clients","stage":"execution","result":"returned"}]
    candidate = {"kind":"answer","text":"目前有一个客户 Oloso。","outcome":{
        "status":"completed","evidence_refs":["e1"],"limitations":[],
        "claims":[{"operation":"list_clients","status":"completed","evidence_refs":["e1"]}]}}
    accepted = await contract.validate(json.dumps(candidate, ensure_ascii=False))
    assert accepted.action is FinalResponseAction.ACCEPT
    assert accepted.content == candidate["text"]
    assert len(contract.observations) == 1
    candidate["outcome"]["claims"][0]["operation"] = "prepare_client_delivery"
    assert (await contract.validate(json.dumps(candidate))).diagnostic["error"] == "operation_evidence_mismatch"

@pytest.mark.asyncio
async def test_format_hint_does_not_accept_unsupported_or_duplicate_claims():
    contract = ChatEvidenceContract()
    unsupported = {"kind":"answer","text":"已交付","outcome":{
        "status":"completed","evidence_refs":["invented"],"limitations":[],
        "claims":[{"operation":"prepare_client_delivery","status":"completed","evidence_refs":["invented"]}]}}
    assert (await contract.validate(json.dumps(unsupported))).action is not FinalResponseAction.ACCEPT
    duplicate = '{"kind":"answer","kind":"answer","text":"done","outcome":{"status":"answered","evidence_refs":[],"limitations":[]}}'
    assert (await contract.validate(duplicate)).diagnostic["error"] == "response_contract_invalid"
