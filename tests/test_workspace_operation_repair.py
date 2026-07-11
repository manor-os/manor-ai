from __future__ import annotations

import logging

import pytest
from sqlalchemy import update

from packages.core.models.base import generate_ulid
from packages.core.models.workspace import Workspace
from packages.core.services import workspace_operation_repair


@pytest.mark.asyncio
async def test_startup_repair_logs_original_workspace_after_rollback(
    db_session,
    monkeypatch,
    caplog,
):
    await db_session.execute(
        update(Workspace).values(
            settings={
                workspace_operation_repair._REPAIR_KEY: {
                    "completed": True,
                    "result": {"skipped": "test isolation"},
                }
            }
        )
    )
    await db_session.commit()

    workspace_id = generate_ulid()
    workspace = Workspace(
        id=workspace_id,
        entity_id=generate_ulid(),
        name="Repair Failure",
        status="active",
        heartbeat_enabled=True,
        heartbeat_cadence="daily",
        operating_model={"goals": [{"title": "Annual goal"}]},
        settings={},
    )
    db_session.add(workspace)
    await db_session.commit()

    async def fail_repair(*args, **kwargs):
        raise ValueError("unsupported measurement_cadence='yearly'")

    monkeypatch.setattr(
        workspace_operation_repair,
        "repair_workspace_operation_runtime",
        fail_repair,
    )

    with caplog.at_level(logging.WARNING, logger=workspace_operation_repair.logger.name):
        report = await workspace_operation_repair.repair_workspace_operation_runtime_backfill(
            db_session,
            limit=10,
        )

    assert report.errors == 1
    assert f"workspace={workspace_id}" in caplog.text
