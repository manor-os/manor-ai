"""Custom file payloads retain the schema the Agent actually submitted."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from packages.core.contracts.artifact_delivery import reused_artifact_snapshots
from packages.core.contracts.task_output import output_contract_for_schema, plan_output_contract_schema
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
async def test_worker_validation_accepts_exact_readonly_file_submission():
    from packages.core.workers import internal
    from tests.test_readonly_artifact_delivery import REUSE

    payload = _payload()
    schema = _schema(payload)
    snapshot = {"kind": "subagent", "params": copy.deepcopy(REUSE), "expected_output_schema": schema}
    result, value_kind = await internal._build_subagent_step_result(
        snapshot, submit_payload={"summary": "Verified", "result": payload}, content="",
        messages=[], artifact_refs=[], original_prompt="Read and deliver the existing file",
        output_contract=output_contract_for_schema(schema),
    )

    assert result == payload
    assert internal._output_contract_errors(schema, result, step_kind="subagent", task_output_value_kind=value_kind) == []


@pytest.mark.asyncio
async def test_readonly_worker_loop_preserves_business_summary_and_other_root_fields(monkeypatch):
    from packages.core.services import artifact_delivery
    from packages.core.workers import internal
    from tests import test_robust_worker_exec_subagent as worker_tests
    from tests.test_readonly_artifact_delivery import REUSE

    worker_tests.worker_env.__wrapped__(monkeypatch)
    payload = _payload()
    payload.update(summary_zh="已经核实原始文件", confirmed_props=["coffee cup", "filter paper"])
    schema = _schema(payload)
    schema["properties"].update(summary_zh={"type": "string"}, confirmed_props={"const": payload["confirmed_props"]})
    schema["required"].extend(["summary_zh", "confirmed_props"])
    verified = []

    async def verify(_db, **kwargs):
        verified.append(kwargs["refs"])
        return SimpleNamespace()

    async def run(**kwargs):
        reply = await kwargs["dynamic_tool_handlers"]["submit_result"]({
            "summary": "Control-layer summary differs from business summary",
            "result": copy.deepcopy(payload),
        })
        assert json.loads(reply)["ok"] is True
        return SimpleNamespace(result=worker_tests._loop_result(), run=None)

    async def unexpected_correction(**kwargs):
        pytest.fail("A schema-valid submission must not require another model round")

    monkeypatch.setattr(artifact_delivery, "verify_artifact_delivery_refs", verify)
    monkeypatch.setattr(internal, "runtime_execute_worker_subagent_loop", run)
    monkeypatch.setattr("packages.core.ai.runtime.runtime_execute_worker_subagent_followup", unexpected_correction)
    outcome = await internal._exec_subagent(worker_tests._step(
        schema, kind="subagent", params={"prompt": "Deliver the existing file without changing it", **REUSE},
    ))

    assert verified == [payload["files"]]
    assert outcome["result"] == payload


@pytest.mark.asyncio
async def test_control_summary_does_not_substitute_for_missing_business_summary():
    from packages.core.workers import internal
    from tests.test_readonly_artifact_delivery import REUSE

    payload = _payload()
    schema = _schema(payload)
    del payload["summary"]
    result, value_kind = await internal._build_subagent_step_result(
        {"params": REUSE, "expected_output_schema": schema},
        submit_payload={"summary": "Only a control summary", "result": payload}, content="",
        messages=[], artifact_refs=[], original_prompt="Deliver an existing file",
        output_contract=output_contract_for_schema(schema),
    )

    assert result == payload
    errors = internal._output_contract_errors(schema, result, step_kind="subagent", task_output_value_kind=value_kind)
    assert len(errors) == 1 and errors[0]["path"] == "$" and "summary" in errors[0]["message"]


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
    assert reused_artifact_snapshots(step)[0]["document_id"] == ids["document_id"]
