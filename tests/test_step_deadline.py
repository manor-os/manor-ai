"""Explicit per-step runtime deadline resolution.

The Dispatcher stamps each worker-job lease with the Step's
``max_runtime_seconds`` so the worker enforces it in-process. These tests pin
how that budget resolves: step -> plan -> workspace -> default (the same
layering ``retry_policy`` uses), with unusable values falling through and
oversized values clamped to the ceiling.
"""
from __future__ import annotations

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.workspace import Workspace
from packages.core.services.step_deadline import (
    DEFAULT_MAX_RUNTIME_SECONDS,
    MAX_MAX_RUNTIME_SECONDS,
    merge_max_runtime_configs,
    resolve_step_deadline,
)


# ── resolution order ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("workspace_cfg", "plan_cfg", "step_cfg", "expected_seconds", "expected_source"),
    [
        (None, None, None, DEFAULT_MAX_RUNTIME_SECONDS, "default"),
        (900, None, None, 900, "workspace"),
        (900, 1800, None, 1800, "plan"),
        (900, 1800, 60, 60, "step"),
        (None, None, 60, 60, "step"),
        # Unusable values fall through to the next-lower layer.
        ("nonsense", 1800, None, 1800, "plan"),
        (900, None, 0, 900, "workspace"),
        (None, None, -5, DEFAULT_MAX_RUNTIME_SECONDS, "default"),
        # Configured values above the ceiling are clamped, which is what
        # keeps the celery backstop strictly above every step deadline.
        (None, None, 99 * 3600, MAX_MAX_RUNTIME_SECONDS, "step"),
    ],
)
def test_max_runtime_resolution_order(
    workspace_cfg, plan_cfg, step_cfg, expected_seconds, expected_source
):
    deadline = merge_max_runtime_configs(
        ("workspace", workspace_cfg),
        ("plan", plan_cfg),
        ("step", step_cfg),
    )
    assert deadline.max_runtime_seconds == expected_seconds
    assert deadline.source == expected_source


@pytest.mark.asyncio
async def test_resolve_step_deadline_reads_workspace_plan_and_step_rows(db_session):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    plan_id = generate_ulid()

    db_session.add_all([
        Workspace(
            id=workspace_id, entity_id=entity_id, name="Long runs", status="active",
            settings={"execution_policy": {"max_runtime_seconds": 900}},
        ),
        ExecutionPlan(
            id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
            status="running", execution_mode="live", approval_required=False,
            plan_dag={"steps": [], "metadata": {"max_runtime_seconds": 1800}},
        ),
    ])
    step = ExecutionStep(
        id=generate_ulid(), plan_id=plan_id, entity_id=entity_id,
        workspace_id=workspace_id, step_key="render_video", kind="subagent",
        params={}, depends_on=[], step_status="pending",
    )
    db_session.add(step)
    await db_session.flush()

    # plan beats workspace
    resolved = await resolve_step_deadline(db_session, step)
    assert (resolved.max_runtime_seconds, resolved.source) == (1800, "plan")

    # step beats plan
    step.params = {"max_runtime_seconds": 120}
    await db_session.flush()
    resolved = await resolve_step_deadline(db_session, step)
    assert (resolved.max_runtime_seconds, resolved.source) == (120, "step")
