"""Custom file payloads retain the schema the Agent actually submitted."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from packages.core.contracts.artifact_delivery import ARTIFACT_DELIVERY_SOURCE, artifact_submission_sha256
from packages.core.contracts.task_output import plan_output_contract_schema
from packages.core.dispatcher.output_coercion import coerce_step_output_for_schema
from packages.core.dispatcher.validation import SchemaError, validate_step_output


def _payload(document_id="doc", path="existing.md"):
    return {"files": [{"document_id": document_id, "fs_path": path, "open_url": f"/viewer/{document_id}"}], "summary": "Existing file verified"}


def _schema(payload, *, strict=True, const_only=False):
    file = payload["files"][0]
    return plan_output_contract_schema({
        "type": "object", "required": ["files", "summary"], "additionalProperties": not strict,
        "properties": {
            "summary": {"type": "string", "maxLength": 80},
            "files": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": list(file), "additionalProperties": not strict,
                "properties": {key: {"const": value, **({} if const_only else {"type": "string"})} for key, value in file.items()},
            }},
        },
    })


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("const_only", [False, True])
def test_declared_artifact_object_survives_coercion_exactly(strict, const_only):
    payload = _payload()
    schema = _schema(payload, strict=strict, const_only=const_only)
    before = copy.deepcopy(payload)

    coerced = coerce_step_output_for_schema(schema, payload, step_kind="subagent")

    assert coerced == before and payload == before
    validate_step_output(SimpleNamespace(step_key="reuse", expected_output_schema=schema), coerced)


@pytest.mark.parametrize("malformation", ["missing", "extra", "wrong_const", "wrong_type", "extra_invalid_item"])
def test_invalid_strict_artifact_submission_is_not_silently_repaired(malformation):
    payload = _payload()
    schema = _schema(payload)
    if malformation == "missing":
        del payload["files"][0]["open_url"]
    elif malformation == "extra":
        payload["files"][0]["unexpected"] = "must remain visible to validation"
    elif malformation == "wrong_const":
        payload["files"][0]["open_url"] = "/viewer/other"
    elif malformation == "wrong_type":
        payload["files"][0]["document_id"] = 123
    else:
        payload["files"].append({"url": "https://example.invalid/unsubmitted"})
    before = copy.deepcopy(payload)

    coerced = coerce_step_output_for_schema(schema, payload, step_kind="subagent")

    assert coerced == before and payload == before
    with pytest.raises(SchemaError):
        validate_step_output(SimpleNamespace(step_key="reuse", expected_output_schema=schema), coerced)


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_dispatcher_persists_exact_readonly_payload_and_matching_receipt(db_session, tmp_path, monkeypatch, strict):
    from packages.core.config import get_settings
    from packages.core.dispatcher.service import Dispatcher
    from packages.core.models.execution import ExecutionStep
    from tests.test_readonly_artifact_delivery import _seed

    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(get_settings(), "DEPLOYMENT_MODE", "oss")
    ids = await _seed(db_session, tmp_path)
    payload = _payload(ids["document_id"])
    step = await db_session.get(ExecutionStep, ids["step_id"])
    step.expected_output_schema = _schema(payload, strict=strict, const_only=not strict)
    await db_session.commit()

    lease = await Dispatcher().complete_lease(db_session, ids["lease_id"], result=payload)
    await db_session.commit()
    await db_session.refresh(step)

    assert lease.status == "completed" and step.step_status == "done"
    assert lease.result == payload and step.result == payload
    receipts = [ref for ref in step.evidence_refs or [] if ref.get("source") == ARTIFACT_DELIVERY_SOURCE]
    assert [ref["document_id"] for ref in receipts] == [ids["document_id"]]
    assert receipts[0]["fields"]["disposition"] == "reused"
    assert receipts[0]["fields"]["owner_step_id"] == step.id
    assert receipts[0]["fields"]["submission_sha256"] == artifact_submission_sha256(payload)
