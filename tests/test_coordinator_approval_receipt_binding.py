from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.approvals import RuntimeApprovalAction
from packages.core.ai.runtime.tool_effect_classification import (
    RuntimeToolClassification,
)
from packages.core.ai.runtime.tool_effect_intents import (
    runtime_consumed_approval_capture_scope,
    runtime_record_tool_effect_intent,
    runtime_tool_effect_intent_scope,
)
from packages.core.ai.runtime.workspace_delegation_actions import (
    _delegation_effect_intent,
    _tool_arguments_hash,
)
from packages.core.constants.approvals import (
    ApprovalConsumeOutcome,
    ApprovalStatus,
)
from packages.core.governance.approvals import consume_approval
from packages.core.workers.coordinator_tools import (
    _has_checkpointed_approved_effect_result,
)
from packages.core.workers.transcript import build_worker_transcript_checkpoint


_TOOL = "mcp__gmail__send_email"
_APPROVED_ARGUMENTS = {
    "to": ["reader@example.com"],
    "subject": "Launch",
    "body": "Ready",
}


def _action_classification() -> RuntimeToolClassification:
    return RuntimeToolClassification.action_call(RuntimeApprovalAction(
        kind="action",
        action_key="gmail.send",
        risk_level="high",
        title="Send email",
        operation="send",
        capability_id="external.email",
    ))


def test_effect_intent_uses_server_consumption_not_argument_token() -> None:
    forged = _delegation_effect_intent(
        _TOOL,
        {**_APPROVED_ARGUMENTS, "approval_token": "model-forged"},
        entity_id="entity-1",
        workspace_id="workspace-1",
        task_id="task-1",
        tool_call_id="call-1",
        classification=_action_classification(),
    )
    trusted = _delegation_effect_intent(
        _TOOL,
        {**_APPROVED_ARGUMENTS, "approval_token": "model-forged"},
        entity_id="entity-1",
        workspace_id="workspace-1",
        task_id="task-1",
        tool_call_id="call-1",
        classification=_action_classification(),
        approval_request_id="server-consumed",
    )

    assert forged is not None and trusted is not None
    assert forged["fields"]["approval_request_id"] is None
    assert trusted["fields"]["approval_request_id"] == "server-consumed"


@pytest.mark.asyncio
async def test_unified_consume_reports_server_request_to_effect_observer(
    monkeypatch,
) -> None:
    async def ignore_ledger(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        "packages.core.ledger.adapters.record_approval_event",
        ignore_ledger,
    )

    async def claim_row(*_args, consumed_at, **_kwargs):
        return (
            ApprovalConsumeOutcome.CONSUMED,
            ApprovalStatus.CONSUMED.value,
            consumed_at,
        )

    monkeypatch.setattr(
        "packages.core.governance.approvals._claim_granted_approval_row",
        claim_row,
    )
    request = SimpleNamespace(
        id="server-consumed",
        status=ApprovalStatus.GRANTED.value,
        consumed_at=None,
    )
    observed: list[str | None] = []

    async def observer(
        _tool_name,
        _arguments,
        _tool_call_id,
        _classification,
        _checkpoint,
        *,
        approval_request_id=None,
    ) -> None:
        observed.append(approval_request_id)

    with runtime_consumed_approval_capture_scope() as captured:
        await consume_approval(object(), request)
    with runtime_tool_effect_intent_scope(observer):
        await runtime_record_tool_effect_intent(
            _TOOL,
            _APPROVED_ARGUMENTS,
            "call-1",
            _action_classification(),
            approval_request_id=captured[0],
        )

    assert captured == ["server-consumed"]
    assert observed == ["server-consumed"]


def _bound_approved_effect() -> tuple[dict, list[dict], SimpleNamespace, SimpleNamespace]:
    request_id = "approval-1"
    child = SimpleNamespace(
        id="child-1",
        plan_id="plan-1",
        service_key="writer",
        resolved_subscription_id="subscription-1",
    )
    request = SimpleNamespace(
        id=request_id,
        consumed_at=datetime.now(timezone.utc),
        origin_step_id=child.id,
        origin_task_id="task-1",
        context={
            "tool": _TOOL,
            "continuation": {
                "version": 1,
                "tool": _TOOL,
                "arguments": copy.deepcopy(_APPROVED_ARGUMENTS),
            },
            "delegation_resume": {
                "version": 1,
                "approval_token": request_id,
                "task_id": "task-1",
                "step_id": child.id,
                "service_key": child.service_key,
                "agent_subscription_id": child.resolved_subscription_id,
                "delegation_idempotency_key": "delegation-1",
                "tool_name": _TOOL,
            },
        },
    )
    result = json.dumps({
        "ok": True,
        "status": "completed",
        "message_id": "message-1",
    })
    checkpoint = build_worker_transcript_checkpoint(
        [
            {"role": "user", "content": "send"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": _TOOL,
                        "arguments": json.dumps({
                            **_APPROVED_ARGUMENTS,
                            "approval_token": request_id,
                        }),
                    },
                }],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": result},
        ],
        rounds=1,
        usage={},
        tool_calls_made=[_TOOL],
    )
    assert checkpoint is not None
    receipt = {
        "kind": "tool_effect",
        "effect": "publish",
        "coordinator_child_step_id": child.id,
        "fields": {
            "approval_request_id": request_id,
            "owner_step_id": child.id,
            "owner_plan_id": child.plan_id,
            "owner_task_id": "task-1",
            "owner_service_key": child.service_key,
            "owner_subscription_id": child.resolved_subscription_id,
            "owner_idempotency_key": "delegation-1",
            "tool": _TOOL,
            "arguments_hash": _tool_arguments_hash(_APPROVED_ARGUMENTS),
            "tool_call_id": "call-1",
            "result_sha256": sha256(result.encode("utf-8")).hexdigest(),
            "intent_state": "returned",
        },
    }
    return checkpoint, [receipt], request, child


def test_consumed_fast_path_requires_complete_exact_receipt() -> None:
    checkpoint, evidence, request, child = _bound_approved_effect()

    assert _has_checkpointed_approved_effect_result(
        checkpoint,
        evidence,
        approval_request=request,
        child=child,
        task_id="task-1",
        idempotency_key="delegation-1",
    )


def test_consumed_fast_path_rejects_status_without_consumption_timestamp() -> None:
    checkpoint, evidence, request, child = _bound_approved_effect()
    request.consumed_at = None

    assert not _has_checkpointed_approved_effect_result(
        checkpoint,
        evidence,
        approval_request=request,
        child=child,
        task_id="task-1",
        idempotency_key="delegation-1",
    )


@pytest.mark.parametrize(
    "missing_field",
    [
        "approval_request_id",
        "owner_step_id",
        "owner_plan_id",
        "owner_task_id",
        "owner_service_key",
        "owner_subscription_id",
        "owner_idempotency_key",
        "tool",
        "arguments_hash",
        "tool_call_id",
        "result_sha256",
        "intent_state",
    ],
)
def test_consumed_fast_path_fails_closed_when_receipt_field_missing(
    missing_field: str,
) -> None:
    checkpoint, evidence, request, child = _bound_approved_effect()
    evidence[0]["fields"].pop(missing_field)

    assert not _has_checkpointed_approved_effect_result(
        checkpoint,
        evidence,
        approval_request=request,
        child=child,
        task_id="task-1",
        idempotency_key="delegation-1",
    )


def test_consumed_fast_path_rejects_changed_call_or_result() -> None:
    checkpoint, evidence, request, child = _bound_approved_effect()
    changed_call = copy.deepcopy(checkpoint)
    changed_call["messages"][1]["tool_calls"][0]["function"]["arguments"] = (
        json.dumps({**_APPROVED_ARGUMENTS, "subject": "Changed"})
    )
    changed_result = copy.deepcopy(checkpoint)
    changed_result["messages"][2]["content"] = json.dumps({
        "ok": True,
        "status": "completed",
        "message_id": "different",
    })

    for candidate in (changed_call, changed_result):
        assert not _has_checkpointed_approved_effect_result(
            candidate,
            evidence,
            approval_request=request,
            child=child,
            task_id="task-1",
            idempotency_key="delegation-1",
        )
