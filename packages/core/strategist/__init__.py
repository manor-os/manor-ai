"""Strategist — periodic / event-driven workspace reviewer.

Reads the workspace's:
  * active goals + their pace
  * recent tasks + outcomes
  * workspace memory (relevant entries by similarity)
  * recent owner messages

…and produces 2-5 task proposals (default mode: ``propose_only``).

Proposals are written as ``Task`` rows with ``status='proposed'`` —
the workspace chat shows them in a single proposal card with
[Approve all] [Pick] [Skip] buttons. On approve, the chat resolve
handler flips them to ``status='pending'``, which fires the existing
``task_service.update_task`` hook that calls the Planner.

Trigger sources:
  * ScheduledJob row (cron / interval) — set up at workspace setup
    time from ``operating_model.strategist.cadence``.
  * Manual via ``POST /api/v1/workspaces/{id}/strategist/run``.
  * Event-driven: when a goal's pace turns ``at_risk`` (M3 +).
"""
from packages.core.strategist.proposal import Proposal, ProposedTask
from packages.core.strategist.service import (
    run_review,
    StrategistError,
    approve_proposal,
    reject_proposal,
)
from packages.core.strategist.scheduling import (
    install_strategist_schedule,
    remove_strategist_schedule,
)

__all__ = [
    "Proposal",
    "ProposedTask",
    "run_review",
    "approve_proposal",
    "reject_proposal",
    "StrategistError",
    "install_strategist_schedule",
    "remove_strategist_schedule",
]
