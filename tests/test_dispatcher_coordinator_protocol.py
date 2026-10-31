from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from apps.api.routers.workers import _build_lease_dto_for_worker
from packages.core.constants.execution import (
    ACTION_EFFECT_GENERATION_PARAM_KEY,
    COORDINATOR_CHILD_PARAM_KEY,
    COORDINATOR_CHILD_RELEASED_PARAM_KEY,
    COORDINATOR_TYPED_CHILD_PARAM_KEY,
)
from packages.core.dispatcher.action_params import executable_action_params
from packages.core.dispatcher.service import (
    _expired_external_action_attempt_evidence,
    _resolve_subscription_for_step,
)
from packages.core.dispatcher.validation import SchemaError, validate_step_input
from packages.core.models.base import generate_ulid
from packages.core.models.workspace import AgentSubscription


def test_typed_action_uses_one_executable_params_projection() -> None:
    stored = {
        "message": "publish once",
        "payload": {"_nested": "provider-owned", "source_contract": "nested"},
        "source_contract": {"kind": "action"},
        COORDINATOR_CHILD_PARAM_KEY: True,
        COORDINATOR_TYPED_CHILD_PARAM_KEY: True,
        COORDINATOR_CHILD_RELEASED_PARAM_KEY: True,
        ACTION_EFFECT_GENERATION_PARAM_KEY: 7,
        "_private": "runtime-only",
    }
    expected = {
        "message": "publish once",
        "payload": {"_nested": "provider-owned", "source_contract": "nested"},
        "human_input_response": {"choice": "approve"},
    }
    assert executable_action_params(
        stored,
        human_input_response={"choice": "approve"},
    ) == expected

    step = SimpleNamespace(
        id="step-1",
        step_key="publish",
        plan_id="plan-1",
        workspace_id=None,
        params=stored,
        human_input_response={"choice": "approve"},
        resolved_subscription_id=None,
        service_key=None,
        kind="action",
        provider="example",
        action_key="publish",
        capability_id="external.publish",
        integration_id=None,
        expected_input_schema=None,
        expected_output_schema=None,
        risk_level="low",
        attempt_count=7,
    )
    lease = SimpleNamespace(
        id="lease-1",
        subscription_id=None,
        execution_claim_id="claim-1",
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit_usd=None,
    )
    dto = _build_lease_dto_for_worker(
        lease=lease,
        step=step,
        plan=SimpleNamespace(execution_mode="live"),
    )
    assert dto.params == expected

    evidence = _expired_external_action_attempt_evidence(lease, step)
    expected_hash = hashlib.sha256(json.dumps(
        expected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    assert evidence["fields"]["arguments_hash"] == expected_hash

    schema_step = SimpleNamespace(
        step_key="publish",
        expected_input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "payload": {"type": "object"},
                "human_input_response": {"type": "object"},
            },
            "required": ["message", "payload", "human_input_response"],
            "additionalProperties": False,
        },
    )
    validate_step_input(schema_step, expected)
    with pytest.raises(SchemaError):
        validate_step_input(schema_step, stored)
    assert stored["source_contract"] == {"kind": "action"}
    assert stored[ACTION_EFFECT_GENERATION_PARAM_KEY] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    ["missing", "entity", "workspace", "service", "agent", "inactive"],
)
async def test_persisted_subscription_pin_never_falls_back(
    db_session,
    mismatch,
) -> None:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    agent_id = generate_ulid()
    pinned_id = generate_ulid()
    local_fallback = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=generate_ulid(),
        workspace_id=workspace_id,
        name="Fallback agent",
        service_key="writer",
        status="active",
    )
    db_session.add(local_fallback)
    if mismatch != "missing":
        db_session.add(AgentSubscription(
            id=pinned_id,
            entity_id=(generate_ulid() if mismatch == "entity" else entity_id),
            agent_id=(generate_ulid() if mismatch == "agent" else agent_id),
            workspace_id=(
                generate_ulid() if mismatch == "workspace" else workspace_id
            ),
            name="Pinned agent",
            service_key=("reviewer" if mismatch == "service" else "writer"),
            status=("paused" if mismatch == "inactive" else "active"),
        ))
    await db_session.flush()

    resolved = await _resolve_subscription_for_step(
        db_session,
        SimpleNamespace(
            entity_id=entity_id,
            workspace_id=workspace_id,
            service_key="writer",
            resolved_subscription_id=pinned_id,
            resolved_agent_id=agent_id,
        ),
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_persisted_subscription_pin_requires_exact_agent(db_session) -> None:
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    agent_id = generate_ulid()
    pinned = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        agent_id=agent_id,
        workspace_id=workspace_id,
        name="Pinned agent",
        service_key="writer",
        status="active",
    )
    db_session.add(pinned)
    await db_session.flush()

    resolved = await _resolve_subscription_for_step(
        db_session,
        SimpleNamespace(
            entity_id=entity_id,
            workspace_id=workspace_id,
            service_key="writer",
            resolved_subscription_id=pinned.id,
            resolved_agent_id=agent_id,
        ),
    )

    assert resolved is not None
    assert resolved.id == pinned.id
