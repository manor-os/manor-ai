"""Who cancelled a Task or Workflow run — feeds the user-cancel quality
signal (workspace health metrics design §9.2)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.constants.task import CancelSource
from packages.core.services.task_state_machine import apply_task_status_transition
from packages.core.services.workflow_run_control import cancel_run

ROOT = Path(__file__).resolve().parents[1]


def _task(status="in_progress"):
    return SimpleNamespace(
        status=status, status_changed_at=None, updated_at=None,
        started_at=None, completed_at=None, cancel_source=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_kind,explicit,expected", [
    ("user", None, CancelSource.USER.value),
    ("system", None, CancelSource.SYSTEM.value),
    ("agent", None, CancelSource.SYSTEM.value),
    ("system", CancelSource.PROPOSAL.value, CancelSource.PROPOSAL.value),
])
async def test_task_cancel_records_its_source(actor_kind, explicit, expected):
    task = _task()
    await apply_task_status_transition(
        task, "cancelled", actor_kind=actor_kind, cancel_source=explicit,
    )
    assert task.cancel_source == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_kind,explicit,expected", [
    # A withdrawn proposal never became work, whoever withdraws it ...
    ("system", None, CancelSource.PROPOSAL.value),
    ("user", None, CancelSource.PROPOSAL.value),
    ("system", CancelSource.SYSTEM.value, CancelSource.PROPOSAL.value),
    # ... unless the caller explicitly records a person's cancel.
    ("user", CancelSource.USER.value, CancelSource.USER.value),
])
async def test_cancelling_a_proposed_task_is_a_proposal_cancel(actor_kind, explicit, expected):
    task = _task(status="proposed")
    await apply_task_status_transition(
        task, "cancelled", actor_kind=actor_kind, cancel_source=explicit,
    )
    assert task.cancel_source == expected


@pytest.mark.asyncio
async def test_an_agent_cancel_in_a_user_started_turn_is_not_a_user_cancel(db_session):
    """A foreground turn is user-requested execution, but the agent chose
    to cancel: only a person's direct request is a user cancel."""
    from packages.core.ai.runtime.manor_actions import runtime_manor_update_task
    from tests.task_manual_helpers import task_manual_scenario

    scenario = await task_manual_scenario(db_session)
    result = json.loads(await runtime_manor_update_task(
        db_session, entity_id=scenario.entity_id, user_id=scenario.user.id,
        params={"task_id": scenario.task.id, "status": "cancelled"},
        runtime_tool_kwargs={"_runtime_envelope_from_context": SimpleNamespace(
            profile="workspace_operator", entity_id=scenario.entity_id,
            workspace_id=scenario.workspace.id, task_id=None, user_id=scenario.user.id,
        )},
    ))
    assert result.get("status") == "cancelled", result
    await db_session.refresh(scenario.task)
    assert scenario.task.cancel_source == CancelSource.SYSTEM.value


@pytest.mark.asyncio
async def test_reopening_a_cancelled_task_clears_its_source():
    task = _task()
    await apply_task_status_transition(task, "cancelled", actor_kind="user")
    await apply_task_status_transition(task, "pending")
    assert task.cancel_source is None


def test_workflow_cancel_records_its_source():
    run = SimpleNamespace(status="running", trigger_data={}, completed_at=None, cancel_source=None)
    cancel_run(run, actor_id="u1", source=CancelSource.USER)
    assert run.cancel_source == CancelSource.USER.value


# Every Workflow run cancel goes through cancel_run, so none can skip the
# source. Scans the whole tree, not one file (memory: guard the class).
_CANCELLED = r"""(?:["']cancelled["']|\w+\.CANCELLED\b(?:\.value)?)"""
_STATUS_ASSIGN = re.compile(rf"\.status\s*=\s*{_CANCELLED}")
_UPDATE_RUN = re.compile(r"\bupdate\(\s*WorkflowRun\s*\)")
_ANY_UPDATE = re.compile(r"\bupdate\(")
_VALUES_STATUS = re.compile(rf"""(?:\bstatus\s*=|["']status["']\s*:)\s*{_CANCELLED}""")
_WORKFLOW_RUN_FILES = re.compile(r"\bWorkflowRun\b")
_CANCEL_RUN_HOME = "packages/core/services/workflow_run_control.py"
# (path, stripped line) → why a hit is not a Workflow run cancel.
_NOT_A_RUN = {
    ("packages/core/services/proposal_workflow_runs.py",
     "item.status = ProposalItemStatus.CANCELLED"):
        "a ProposalItemRecord mirroring its run's state, not the run itself",
}


def _values_args(text: str, start: int) -> tuple[int, str]:
    """(offset, source) of the first ``.values(...)`` call after ``start``,
    stopping at the end of the update statement's parentheses."""
    at = text.find(".values(", start)
    if at < 0:
        return -1, ""
    depth, i = 0, at + len(".values")
    for i in range(at + len(".values"), len(text)):
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        if depth == 0:
            break
    return at, text[at:i + 1]


def _run_cancel_offenders(rel: str, text: str) -> list[str]:
    def line_of(offset: int) -> int:
        return text.count("\n", 0, offset) + 1

    lines = text.splitlines()
    offenders = [
        line_of(m.start()) for m in _STATUS_ASSIGN.finditer(text)
        if (rel, lines[line_of(m.start()) - 1].strip()) not in _NOT_A_RUN
    ]
    for m in _UPDATE_RUN.finditer(text):
        nxt = _ANY_UPDATE.search(text, m.end())  # .values() must belong to this update
        at, args = _values_args(text, m.end())
        if at >= 0 and (nxt is None or at < nxt.start()):
            offenders += [line_of(at + v.start()) for v in _VALUES_STATUS.finditer(args)]
    return [f"{rel}:{n}" for n in sorted(set(offenders))]


def test_workflow_runs_are_only_cancelled_through_cancel_run():
    offenders = []
    for path in [*ROOT.glob("packages/**/*.py"), *ROOT.glob("apps/api/**/*.py")]:
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if rel == _CANCEL_RUN_HOME or not _WORKFLOW_RUN_FILES.search(text):
            continue
        offenders += _run_cancel_offenders(rel, text)
    assert offenders == [], f"cancel Workflow runs via workflow_run_control.cancel_run: {offenders}"


@pytest.mark.parametrize("source", [
    'run.status = "cancelled"',
    "row.status = WorkflowRunStatus.CANCELLED",
    "wr.status = WorkflowRunStatus.CANCELLED.value",
    'await db.execute(update(WorkflowRun).where(WorkflowRun.id == x).values(\n    status="cancelled",\n))',
    "await db.execute(update(WorkflowRun).values({'status': WorkflowRunStatus.CANCELLED.value}))",
])
def test_the_run_cancel_guard_sees_every_form(source):
    assert _run_cancel_offenders("x.py", f"from m import WorkflowRun\n{source}\n")


def test_the_run_cancel_guard_allows_listed_lines_and_other_updates():
    rel, line = next(iter(_NOT_A_RUN))
    assert _run_cancel_offenders(rel, f"WorkflowRun\n    {line}\n") == []
    assert _run_cancel_offenders(
        "x.py", "update(WorkflowRun).where(x)\nupdate(Task).values(status='cancelled')\n",
    ) == []


