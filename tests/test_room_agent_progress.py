from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from packages.core.services.room_agent_progress import room_agent_process_blocks


@pytest.mark.parametrize("status,lifecycle", [
    ("pending", "waiting"), ("running", "running"), ("waiting_approval", "waiting"),
    ("succeeded", "completed"), ("failed", "failed"), ("interrupted", "cancelled"),
])
def test_room_progress_uses_run_outcome_and_does_not_publish_candidates(status, lifecycle):
    run = SimpleNamespace(
        id="run", status=status, started_at=datetime.fromtimestamp(10, timezone.utc),
        completed_at=datetime.fromtimestamp(14, timezone.utc) if status in {"succeeded", "failed", "interrupted"} else None,
        checkpoint={"pending_reply": {"content": "Private candidate"}},
        assistant_blocks=[{"type": "process", "lifecycle_status": "completed", "steps": [{
            "id": "tool", "name": "web_search", "status": "success",
            "started_at_ms": 11000, "ended_at_ms": 12000, "duration_ms": 1000,
            "arguments_preview": "private query", "result_preview": "private result",
        }]}, {"type": "text", "text": "Private candidate"}],
    )
    process, = room_agent_process_blocks(run)
    assert process["lifecycle_status"] == lifecycle
    assert process["steps"] == [{"id": "tool", "name": "web_search", "status": "success",
                                 "started_at_ms": 11000, "ended_at_ms": 12000, "duration_ms": 1000}]
    assert "private" not in str(process).lower()
    assert process["started_at_ms"] == 10000
    if run.completed_at:
        assert process["duration_ms"] == 4000
        assert process["ended_at_ms"] == 14000
    else:
        assert "ended_at_ms" not in process


def test_legacy_group_reply_retains_completion_without_inventing_tool_events():
    process, = room_agent_process_blocks(SimpleNamespace(
        id="run", status="succeeded", assistant_blocks=None, started_at=None, completed_at=None,
    ))
    assert process["lifecycle_status"] == "completed"
    assert process["steps"] == []
    assert "duration_ms" not in process
