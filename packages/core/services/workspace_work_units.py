"""What counts as one piece of work a person would recognize.

The run surfaces of the Workspace health roll-up count executions, which launch
review and Stats need. A work unit reads how a release does instead: a scheduled
tick that found nothing is no work, and one item must not count once per run it
took. ``workspace_health_rollup`` counts the units; this module says which they are.

A unit contained in a Task leaves its verdict to that Task, but only while the
Task can give one: see ``container_covers``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import Boolean, and_, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from packages.core.constants.execution import ScheduledDispatchKind
from packages.core.constants.workflow import WorkflowRunStatus
from packages.core.models.scheduler import ScheduledJobRun
from packages.core.models.task import Task
from packages.core.models.workflow import WorkflowRun
from packages.core.services.scheduled_run_lifecycle import EXECUTION_STATE_KEY
from packages.core.services.task_execution_origin import ORIGIN_TASK_KEY
from packages.core.services.workspace_health_metrics import FAILED, TASK_OUTCOMES, ids_param

WORKFLOW_CONTROL_STEP_TYPES = frozenset({
    # Enter or leave a run.
    "trigger", "webhook", "end", "stop", "respond", "note",
    # Route it.
    "condition", "switch", "merge", "wait",
    # Reshape values already in the run. ``code`` belongs here: Workflow patterns
    # use it to work out a date or list new items, and ``workflow_retry`` already
    # treats it as doing no work of its own.
    "transform", "filter", "aggregate", "datetime", "split", "limit", "sort", "dedupe", "code",
    # Keep a run's own records: leases and project state, grants, deadlines, receipts.
    "workflow_project", "workflow_action_grant", "workflow_effect_deadline",
    "browser_effect", "publication_receipt",
    # Coordinate other runs, which report for themselves.
    "subworkflow", "foreach_subworkflow",
    # An imported node with no equivalent: it is skipped unless it fails the run.
    "unsupported",
})
"""Step types that run no agent, model, tool or action. Any other type is work,
so a new step type is never silently dropped. ``stage``, ``parallel`` and ``loop``
are work because the inline steps they run are not recorded on the run."""


def workflow_run_is_work_unit(
    outcome: str, *, later_attempt: bool, inside_task: bool, ran_work: bool,
    stopped_on_child: bool, replaced_by_parent_retry: bool,
) -> bool:
    """Whether a finished Workflow run is one unit, judged by its own ``outcome``.

    Not a unit:

    * ``later_attempt``: a retry lineage is one unit, read from its latest attempt.
    * ``inside_task``: a Task covers the run or a run above it (``container_covers``).
    * ``replaced_by_parent_retry``: the failed or cancelled child of a parent
      attempt whose retry gave that step another child. A person retried one
      item; retrying a parent starts a new child lineage for it.
    * ``stopped_on_child``: it failed or was cancelled only because a child did.
      The child is the unit.
    * A run that did no work of its own and did not fail: an idle tick, or a
      parent that only coordinated children, which report for themselves.

    Any other failure counts even with no work step, such as an errored lease
    step: nothing else will report it. So does a parent whose worker went away
    while its child was still fine, since that child reports only its own outcome.
    """
    if later_attempt or inside_task or replaced_by_parent_retry or stopped_on_child:
        return False
    return outcome == FAILED or bool(ran_work)


# ── Scheduled runs ───────────────────────────────────────────────────

def scheduled_run_is_work_unit(outcome: str, *, handed_over: bool) -> bool:
    """A scheduled run that failed without handing anything over is one failed unit.

    Skill not found, no ``workflow_id``, a dispatch exception, no credits: the
    release can fail every tick this way, and no Task or Flow will report it. A run
    that handed work over is not a unit, whatever its outcome: a Task or Flow reports
    for itself. A successful run is not one either, since it says only that the job
    fired. Other job kinds (a Strategist review, a briefing, a Stat collection) stay
    out too: they did start, and their failure is the platform job's own.
    """
    return outcome == FAILED and not handed_over


def scheduled_run_handed_over_column() -> ColumnElement:
    """Whether a ``ScheduledJobRun``'s child really started.

    ``result.dispatch`` is replaced by the child's envelope only once the child
    exists; until then it is absent or the tick's own ``scheduler_parent`` handoff.
    An existing child has started only if the run records an execution state, which
    its admission and ``apply_scheduled_run_outcome`` write. ``dispatch_status`` proves
    nothing: after publishing fails for good, the recovery sweep marks the terminal
    run ``published`` without publishing, and its child never executes.
    """
    result = ScheduledJobRun.result
    kind = result["dispatch"]["kind"].astext
    return and_(
        kind.is_not(None),
        kind != ScheduledDispatchKind.SCHEDULER_PARENT.value,
        result[EXECUTION_STATE_KEY].astext.is_not(None),
    )


# ── Contained units ──────────────────────────────────────────────────

@dataclass(frozen=True)
class TaskContainer:
    workspace_id: Optional[str]
    finished_at: Optional[datetime]
    """``None`` while the Task is open, whatever older stamps it still holds."""


def container_covers(
    container: Optional[TaskContainer], *, workspace_id: Optional[str], finished_at: datetime,
) -> bool:
    """Whether a Task gives the verdict for a Flow or a Task contained in it.

    Only a Task that exists, in the unit's Workspace, and had not finished before
    the unit did. A Task that ended earlier has already been judged without this
    outcome, and another Workspace or a missing Task reports nothing here, so the
    unit counts for itself: a failure must not vanish. An id can also simply be
    stale, since a scheduled job keeps the last Task it ran and a retry copies the
    context it started with. While a Task is open it still covers the unit: a Flow
    failing inside a running Task whose agent then completes it is that Task's
    verdict.
    """
    if container is None or container.workspace_id != workspace_id:
        return False
    return container.finished_at is None or container.finished_at >= finished_at


def task_finished_column() -> ColumnElement:
    """When a finished Task finished. Read it only with a status in ``TASK_OUTCOMES``."""
    return func.coalesce(Task.completed_at, Task.status_changed_at, Task.updated_at, Task.created_at)


def task_origin_column() -> ColumnElement:
    """The Task whose run created this one. A subtask a person made has none."""
    return Task.details[ORIGIN_TASK_KEY].astext


async def _task_containers(db: AsyncSession, task_ids: Iterable[str]) -> dict[str, TaskContainer]:
    ids = sorted(set(task_ids))
    if not ids:
        return {}
    return {
        r.id: TaskContainer(r.workspace_id, r.finished_at if r.status in TASK_OUTCOMES else None)
        for r in (await db.execute(select(
            Task.id, Task.workspace_id, Task.status, task_finished_column().label("finished_at"),
        ).where(Task.id == ids_param(ids)))).all()
    }


async def tasks_inside_tasks(db: AsyncSession, rows: Iterable[Any]) -> set[str]:
    """Ids of Task ``rows`` whose origin Task covers them.

    ``rows`` have ``id``, ``workspace_id``, ``finished_at`` and ``origin_task_id``.
    """
    rows = [r for r in rows if r.origin_task_id]
    containers = await _task_containers(db, (r.origin_task_id for r in rows))
    return {
        r.id for r in rows
        if container_covers(containers.get(r.origin_task_id), workspace_id=r.workspace_id, finished_at=r.finished_at)
    }


def workflow_run_task_column() -> ColumnElement:
    """The Task a Workflow run names as running it, as ``run_workflow`` and the
    scheduler store it; ``runtime_workflow_run_context`` reads the same places."""
    trigger = WorkflowRun.trigger_data
    return func.nullif(func.coalesce(
        trigger["_workflow_runtime_context"]["task_id"].astext,
        trigger["runtime_context"]["task_id"].astext,
    ), "")


def workflow_run_parent_column() -> ColumnElement:
    return WorkflowRun.trigger_data["parent_run_id"].astext


async def runs_inside_tasks(db: AsyncSession, rows: Iterable[Any]) -> set[str]:
    """Ids of Workflow ``rows`` covered by the nearest Task named on them or above them.

    ``run_workflow`` names the Task on the run it starts and its child runs do not
    inherit that name, so a child finds it by walking up. A parent can have finished
    on another day or still be running, so it may not be among ``rows``.

    ``rows`` have ``id``, ``workspace_id``, ``finished_at``, ``task_id`` and
    ``parent_run_id``.
    """
    rows = list(rows)
    chain = {r.id: (r.task_id, r.parent_run_id) for r in rows}
    wanted = {r.parent_run_id for r in rows if r.parent_run_id} - chain.keys()
    while wanted:
        found = (await db.execute(select(
            WorkflowRun.id, workflow_run_task_column().label("task_id"),
            workflow_run_parent_column().label("parent_run_id"),
        ).where(WorkflowRun.id == ids_param(sorted(wanted))))).all()
        chain.update({r.id: (r.task_id, r.parent_run_id) for r in found})
        wanted = {r.parent_run_id for r in found if r.parent_run_id} - chain.keys()

    named: dict[str, str] = {}
    for r in rows:
        cursor, seen = r.id, set()
        while cursor in chain and cursor not in seen:
            seen.add(cursor)
            task_id, cursor = chain[cursor]
            if task_id:
                named[r.id] = task_id
                break
    containers = await _task_containers(db, named.values())
    return {
        r.id for r in rows
        if r.id in named
        and container_covers(containers.get(named[r.id]), workspace_id=r.workspace_id, finished_at=r.finished_at)
    }


# ── Facts about a run, computed in the database ──────────────────────

def _sql_text_list(values: Iterable[str]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(values))


_RUN = WorkflowRun.__tablename__
_STOPPED = _sql_text_list((WorkflowRunStatus.FAILED.value, WorkflowRunStatus.CANCELLED.value))
_LINEAGE_ROOT = f"COALESCE({_RUN}.lineage_root_run_id, {_RUN}.id)"
# A NULL in this array would make every ``<> ALL`` over it NULL, and a real work step not count.
_CONTROL_STEP_IDS = f"""ARRAY(
    SELECT node ->> 'id'
    FROM jsonb_array_elements(CASE jsonb_typeof({_RUN}.definition_snapshot -> 'nodes')
        WHEN 'array' THEN {_RUN}.definition_snapshot -> 'nodes' ELSE '[]'::jsonb END) AS node
    WHERE node ->> 'id' IS NOT NULL
      AND node ->> 'type' = ANY (ARRAY[{_sql_text_list(WORKFLOW_CONTROL_STEP_TYPES)}])
)"""


def workflow_run_work_facts() -> tuple[ColumnElement, ...]:
    """The ``workflow_run_is_work_unit`` facts as columns of a ``WorkflowRun`` select.

    They are computed in the database: a run's ``step_results`` can hold every
    step's output, and a 15-minute worker makes about 96 runs a day per Workspace.
    A step with no entry in ``definition_snapshot`` counts as work, like an unknown
    type. A cancelled run's current step counts too: a step stopped midway records
    no result, yet a person stopped real work.
    """
    later_attempt = f"""EXISTS (
        SELECT 1 FROM {_RUN} AS later
        WHERE later.lineage_root_run_id = {_LINEAGE_ROOT}
          AND (later.attempt_number, later.id) > ({_RUN}.attempt_number, {_RUN}.id)
    )"""
    ran_work = f"""(EXISTS (
        SELECT 1
        FROM jsonb_each(CASE jsonb_typeof({_RUN}.step_results)
            WHEN 'object' THEN {_RUN}.step_results ELSE '{{}}'::jsonb END) AS ran(step_id, result)
        WHERE ran.result ->> 'skipped' IS DISTINCT FROM 'true'
          AND ran.step_id <> ALL ({_CONTROL_STEP_IDS})
    ) OR ({_RUN}.status = '{WorkflowRunStatus.CANCELLED.value}'
          AND {_RUN}.current_step_id <> ALL ({_CONTROL_STEP_IDS})))"""
    # The step a run stopped at names its child runs: ``subrun_id`` for a
    # subworkflow, ``items`` for a foreach. A named child that is fine, or a
    # subworkflow step that failed before starting one, is the run's own failure.
    stopped_on_child = f"""CASE WHEN {_RUN}.status IN ({_STOPPED}) THEN COALESCE(
        {_RUN}.step_results -> {_RUN}.current_step_id::text -> 'items' @> '[{{"status": "failed"}}]'::jsonb
        OR {_RUN}.step_results -> {_RUN}.current_step_id::text -> 'items' @> '[{{"status": "cancelled"}}]'::jsonb
        OR EXISTS (
            SELECT 1 FROM {_RUN} AS child
            WHERE child.id = {_RUN}.step_results -> {_RUN}.current_step_id::text ->> 'subrun_id'
              AND child.status IN ({_STOPPED})
        ), false) ELSE false END"""
    # Reads the parent's newest retry: when that attempt's step names any child
    # but not this lineage, this child's item was started again. A retry that has
    # not reached the step yet names nothing, so this child still counts for now.
    # A retry can keep naming a failed child: a subworkflow step that handled the
    # failure (``on_error``) is inherited as done, and a foreach keeps an item it
    # does not restart.
    # Only a child's first attempt can read as replaced. A later attempt is a person
    # retrying that child directly, and it keeps the parent of the first attempt
    # whichever child the parent's retry names by then, so that name says nothing
    # about it: after a reconciled retry names attempt 2, a failed attempt 3 would
    # otherwise vanish. The trade-off is a possible over-count: a retried child whose
    # item a parent retry also started again counts as well. A count is preferred
    # over losing a failure.
    replaced_by_parent_retry = f"""CASE WHEN {_RUN}.status IN ({_STOPPED})
        AND {_RUN}.trigger_data ->> 'parent_run_id' IS NOT NULL THEN EXISTS (
        SELECT 1
        FROM {_RUN} AS parent
        CROSS JOIN LATERAL (
            SELECT newest.step_results -> ({_RUN}.trigger_data ->> 'parent_step_id') AS slot
            FROM {_RUN} AS newest
            WHERE newest.lineage_root_run_id = COALESCE(parent.lineage_root_run_id, parent.id)
              AND newest.attempt_number > parent.attempt_number
            ORDER BY newest.attempt_number DESC, newest.id DESC
            LIMIT 1
        ) AS retried
        WHERE parent.id = {_RUN}.trigger_data ->> 'parent_run_id'
          AND {_RUN}.attempt_number = 1
          AND (retried.slot ->> 'subrun_id' IS NOT NULL OR retried.slot -> 'items' IS NOT NULL)
          AND retried.slot ->> 'subrun_id' IS DISTINCT FROM {_LINEAGE_ROOT}
          AND NOT COALESCE(retried.slot -> 'items' @> jsonb_build_array(jsonb_build_object('subrun_id', {_LINEAGE_ROOT})), false)
    ) ELSE false END"""
    return tuple(
        literal_column(sql, Boolean).label(name)
        for name, sql in (
            ("later_attempt", later_attempt), ("ran_work", ran_work),
            ("stopped_on_child", stopped_on_child), ("replaced_by_parent_retry", replaced_by_parent_retry),
        )
    )
