"""A reply goes to whoever did the work, and it clears the waiting status.

A production task asked for a stickman video. The plan ran under the
Stickman Video Producer, the supervisor stopped it as incomplete, and the
user replied "check why, then try again". Two things then went wrong:

  * the reply was answered by the master agent, not the producer. The
    master has none of the video skill, so instead of retrying the
    pipeline it globbed around the filesystem and merged the existing
    silent clips into a different video;

  * the task stayed in ``waiting_on_customer``. The work had been redone
    and reported, and the status still said it was waiting for the user.

Both come from the same wrong assumption: that a task with no ``agent_id``
has no agent. It does — plan-driven work resolves an agent per step and
never copies it back up to the task, and a task with no agent at all runs
under the master agent. "No assignment" is not "nobody".
"""
from __future__ import annotations

import inspect

import pytest

from packages.core.services.task_service import task_executing_agent_id


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """Records whether the step lookup was needed, and answers it."""

    def __init__(self, step_agent_id=None):
        self._step_agent_id = step_agent_id
        self.queried = False

    async def execute(self, _query):
        self.queried = True
        return _FakeResult(self._step_agent_id)


class _Task:
    def __init__(self, task_id="T1", agent_id=None):
        self.id = task_id
        self.agent_id = agent_id


# ── Who answers ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_assigned_agent_wins_without_a_lookup():
    db = _FakeDB(step_agent_id="01STEPAGENT")
    assert await task_executing_agent_id(db, _Task(agent_id="01ASSIGNED")) == "01ASSIGNED"
    assert not db.queried, "the task's own agent needs no step lookup"


@pytest.mark.asyncio
async def test_an_unassigned_task_falls_back_to_the_step_that_ran():
    """The production case: agent_id is null, but a step recorded the
    producer that executed it."""
    db = _FakeDB(step_agent_id="01KY62ATMW8FBGJ3SARVDRK3DW")
    resolved = await task_executing_agent_id(db, _Task())
    assert resolved == "01KY62ATMW8FBGJ3SARVDRK3DW"


@pytest.mark.asyncio
@pytest.mark.parametrize("step_agent", [None, "", "   "])
async def test_nothing_to_resolve_means_the_master_agent(step_agent):
    """None is the master agent — a real answer, not an absence."""
    assert await task_executing_agent_id(_FakeDB(step_agent), _Task()) is None


@pytest.mark.asyncio
async def test_blank_assignment_is_not_an_assignment():
    db = _FakeDB(step_agent_id="01STEPAGENT")
    assert await task_executing_agent_id(db, _Task(agent_id="   ")) == "01STEPAGENT"


# ── The two call sites ────────────────────────────────────────────────


def test_the_comment_thread_resolves_the_working_agent():
    from packages.core.services import workspace_runtime

    source = inspect.getsource(workspace_runtime)
    assert "task_executing_agent_id(db, task)" in source, (
        "a task comment must be answered by the agent that did the work"
    )
    assert "responding_agent_id = task.agent_id" not in source, (
        "task.agent_id alone sends plan-driven replies to the master agent"
    )


def test_resuming_no_longer_requires_an_assigned_agent():
    from apps.api.routers import tasks as tasks_router

    source = inspect.getsource(tasks_router)
    assert "has_agent = task.agent_id or is_master_agent" not in source, (
        "this gate is False for an unassigned task, so the reply never "
        "resumed it and the status stayed waiting_on_customer"
    )
    assert "if task.status == TaskStatus.WAITING_ON_CUSTOMER:" in source


def test_the_resume_dispatches_to_the_working_agent():
    from apps.api.routers import tasks as tasks_router

    source = inspect.getsource(tasks_router)
    assert "dispatch_id = await task_executing_agent_id(db, task) or MANOR_AGENT_ID" in source
    assert "dispatch_id = task.agent_id or MANOR_AGENT_ID" not in source
