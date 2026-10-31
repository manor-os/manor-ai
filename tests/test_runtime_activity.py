"""A Workflow step shows what its long Sandbox work is doing while it runs."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.services import runtime_activity


class _Redis:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, str]] = {}

    async def hget(self, key, field):
        return self.rows.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.rows.setdefault(key, {})[field] = value

    async def expire(self, key, seconds):
        assert seconds == runtime_activity.ACTIVITY_TTL_SECONDS

    async def hgetall(self, key):
        return dict(self.rows.get(key, {}))


@pytest.fixture
def redis(monkeypatch):
    from packages.core import cache

    fake = _Redis()

    async def get_redis():
        return fake

    monkeypatch.setattr(cache, "_get_redis", get_redis)
    return fake


def _event(sequence, kind, message="", **payload):
    return SimpleNamespace(sequence=sequence, type=kind, message=message, payload=payload)


def _status(status, *events, position=None, next_sequence=0):
    return SimpleNamespace(
        status=status, terminal=status in {"completed", "failed", "cancelled"}, events=list(events),
        next_sequence=next_sequence, queue_position=position, stdout="", stderr="", exit_code=0,
    )


class _Client:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.polls: list[dict] = []

    async def execution_status(self, sandbox_id, execution_id, **kwargs):
        self.polls.append(kwargs)
        return self.statuses.pop(0)


@pytest.mark.asyncio
async def test_a_watched_sandbox_command_reports_its_progress_on_the_step(redis):
    from packages.core.ai.tools.sandbox_tools import _await_execution

    context = SimpleNamespace(workflow_run_id="run-1", workflow_step_id="render")
    client = _Client([
        _status("running", _event(1, "progress", "Capturing frames", code="rendering_frames", done=30, total=300),
                next_sequence=1),
        _status("running",
                _event(2, "progress", "Capturing frames", code="rendering_frames", done=150, total=300),
                _event(3, "warning", "Retrying with fewer workers", code="retrying"),
                _event(4, "result", "done"),
                next_sequence=4),
        _status("completed", next_sequence=4),
    ])

    final, _ = await _await_execution(
        client, sandbox_id="sbx", execution_id="exe", first=_status("queued", position=2),
        budget_seconds=60, on_status=runtime_activity.sandbox_activity_recorder(context),
    )

    assert final.status == "completed"
    # The runner's long-poll wakes only when a command ends: a watched one is
    # asked every few seconds, for only the events it has not reported yet.
    assert [poll.get("after_sequence") for poll in client.polls] == [None, 1, 4]
    assert all(poll["wait_seconds"] <= 4 for poll in client.polls)
    activity = (await runtime_activity.read_activity("run-1"))["render"]
    assert [(entry["kind"], entry.get("code")) for entry in activity["events"]] == [
        ("waiting", "queued"),
        # Leaving the line is said, then the frame count moves in one entry.
        ("progress", "running"),
        ("progress", "rendering_frames"),
        ("warning", "retrying"),
        # Its end is recorded, so the step stops showing its last phase.
        ("progress", "finished"),
    ]
    assert activity["events"][0]["position"] == 2
    frames = activity["events"][2]
    assert (frames["done"], frames["total"], frames["percent"]) == (150, 300, 50)
    assert activity["events"][3]["message"] == "Retrying with fewer workers"


@pytest.mark.asyncio
async def test_a_command_outside_a_workflow_step_keeps_the_long_poll(redis):
    from packages.core.ai.tools.sandbox_tools import _await_execution

    client = _Client([_status("completed")])
    recorder = runtime_activity.sandbox_activity_recorder(SimpleNamespace(workflow_run_id="run-1"))

    await _await_execution(
        client, sandbox_id="sbx", execution_id="exe", first=_status("running"),
        budget_seconds=60, on_status=recorder,
    )

    assert recorder is None
    assert client.polls == [{"wait_seconds": 30.0}]
    assert redis.rows == {}


@pytest.mark.asyncio
async def test_run_rows_carry_their_step_activity_and_long_feeds_stay_bounded(redis):
    from apps.api.routers.workflows import _attach_step_activity

    scope = ("run-1", "render")
    for index in range(30):
        await runtime_activity.record_activity(
            scope, runtime_activity.activity_event(kind="warning", message=f"warning {index}"),
        )
    response = {"workflow_steps": [{"id": "plan", "status": "completed"}, {"id": "render", "status": "running"}]}

    await _attach_step_activity(response, SimpleNamespace(id="run-1"))

    plan, render = response["workflow_steps"]
    assert "activity" not in plan
    assert len(render["activity"]["events"]) == runtime_activity.MAX_ACTIVITY_EVENTS
    assert render["activity"]["current"]["message"] == "warning 29"
