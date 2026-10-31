"""A manual retry never restarts a Task that is already running."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.services import task_retry_service as retry

pytestmark = pytest.mark.oss_smoke


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


@pytest.mark.asyncio
async def test_duplicate_manual_retry_cannot_renew_running_task(monkeypatch):
    from packages.core.services import workspace_access

    task = SimpleNamespace(
        id="task-1", entity_id="entity-1", workspace_id="workspace-1", status="in_progress",
        details={}, expected_output=None, owner_service_key="writer", actual_output=None,
    )
    db = SimpleNamespace(execute=AsyncMock(side_effect=[_Result(task)]), flush=AsyncMock())
    monkeypatch.setattr(workspace_access, "lock_workspace_access_boundary", AsyncMock(return_value=SimpleNamespace(
        status="active", deleted_at=None,
    )))
    resolve = AsyncMock()
    stage = AsyncMock()
    monkeypatch.setattr(retry, "resolve_task_execution", resolve)
    monkeypatch.setattr(retry, "stage_task_execution_intent", stage)

    with pytest.raises(retry.TaskRetryError, match="already in progress") as raised:
        await retry.prepare_task_retry(db, task=task, user_id="user", user_label="User")

    assert raised.value.status_code == 409
    assert task.status == "in_progress" and task.details == {}
    resolve.assert_not_awaited()
    stage.assert_not_awaited()
    db.flush.assert_not_awaited()
