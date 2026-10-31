from __future__ import annotations

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
)
from packages.core.constants.approvals import (
    ApprovalConsumeOutcome,
    ApprovalStatus,
)
from packages.core.governance.approvals import consume_approval


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
