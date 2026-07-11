"""Goals layer — CRUD, measurement, pace computation, scheduling.

Layout:
  service       — Goal / GoalMeasurement / GoalTaskLink CRUD; the Goals
                  router and the agent's create_goal tool both go through
                  this module.
  pace          — pure pace_status computation. No DB / no IO.
  measurement   — measure_goal(): resolve adapter → call → record →
                  recompute pace → emit events.
  measurers     — per-provider metric extractors (twitter_x, stripe, ...).
  scheduling    — install / remove the ScheduledJob row that fires the
                  Celery measurement task on cadence.
"""
from packages.core.goals.service import (
    create_goal,
    get_goal,
    list_goals,
    update_goal,
    delete_goal,
    record_measurement,
    list_measurements,
    link_task_to_goal,
)
from packages.core.goals.pace import compute_pace
from packages.core.goals.measurement import measure_goal, MeasurementError

__all__ = [
    "create_goal",
    "get_goal",
    "list_goals",
    "update_goal",
    "delete_goal",
    "record_measurement",
    "list_measurements",
    "link_task_to_goal",
    "compute_pace",
    "measure_goal",
    "MeasurementError",
]
