from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from packages.core.services.workspace_autonomy import (
    WorkspaceAutonomyState,
    workspace_allows_manual_work,
    workspace_autonomy_state,
)


@pytest.mark.parametrize("status,heartbeat,manual,autonomy", [
    ("active", True, True, WorkspaceAutonomyState.RUNNING),
    ("active", False, True, WorkspaceAutonomyState.DISABLED),
    ("paused", True, True, WorkspaceAutonomyState.PAUSED),
    ("paused", False, True, WorkspaceAutonomyState.PAUSED),
    ("draft", True, False, WorkspaceAutonomyState.INACTIVE),
    ("archived", True, False, WorkspaceAutonomyState.INACTIVE),
])
def test_manual_availability_is_not_autonomy(status, heartbeat, manual, autonomy):
    workspace = SimpleNamespace(status=status, heartbeat_enabled=heartbeat, deleted_at=None)
    assert workspace_allows_manual_work(workspace) is manual
    assert workspace_autonomy_state(workspace) is autonomy


def test_deleted_and_missing_workspace_never_admit_manual_work():
    assert not workspace_allows_manual_work(None)
    for status in ("active", "paused"):
        workspace = SimpleNamespace(
            status=status, heartbeat_enabled=True, deleted_at=datetime.now(timezone.utc),
        )
        assert not workspace_allows_manual_work(workspace)
        assert workspace_autonomy_state(workspace) is WorkspaceAutonomyState.DELETED
