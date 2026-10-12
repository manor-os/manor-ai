"""Plan Step responses preserve every JSONB output shape."""

from types import SimpleNamespace

import pytest

from apps.api.routers.plans import _to_step
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep


@pytest.mark.parametrize(
    "result",
    [
        "RUN-D: 11 + 13 + 17 = 41",
        [11, 13, 17],
        {"text": "RUN-D: 41"},
        41,
        True,
        None,
    ],
)
def test_step_response_accepts_jsonb_result_shapes(result):
    step = SimpleNamespace(
        id="step_1", plan_id="plan_1", step_key="run_d_final",
        kind="llm", service_key=None, resolved_subscription_id=None,
        resolved_agent_id=None, provider=None, action_key=None,
        integration_id=None, params={}, result=result, depends_on=[],
        step_status="done", risk_level="low", requires_approval=False,
        attempt_count=1, max_attempts=3, cost={}, error=None,
        human_input_prompt=None, human_input_response=None,
        started_at=None, finished_at=None,
    )

    response = _to_step(step)

    assert response.model_dump(mode="json")["result"] == result


@pytest.mark.asyncio
async def test_plan_steps_api_returns_scalar_step_result(client):
    from packages.core.database import async_session

    registered = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "step_scalar_result",
            "email": "step-scalar-result@example.com",
            "password": "pass123",
        },
    )
    assert registered.status_code == 200, registered.text
    identity = registered.json()
    plan_id, step_id = generate_ulid(), generate_ulid()
    async with async_session() as db:
        db.add(ExecutionPlan(
            id=plan_id, entity_id=identity["entity_id"],
            status="completed", execution_mode="live", plan_dag={},
        ))
        db.add(ExecutionStep(
            id=step_id, plan_id=plan_id, entity_id=identity["entity_id"],
            step_key="run_d_final", kind="llm", params={}, depends_on=[],
            step_status="done", result="RUN-D: 11 + 13 + 17 = 41",
        ))
        await db.commit()

    response = await client.get(
        f"/api/v1/plans/{plan_id}/steps",
        headers={"Authorization": f"Bearer {identity['access_token']}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()[0]["id"] == step_id
    assert response.json()[0]["result"] == "RUN-D: 11 + 13 + 17 = 41"
