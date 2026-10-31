"""Strategist — periodic / event-driven workspace reviewer.

Reads the workspace's goals, services, recent and open tasks, execution
snapshot, memory, integrations, governance policy, missing setup and
outcome calibration (``context.gather_context``), and produces one
``Proposal``: up to 8 tasks (the prompt asks for 0-5) plus human
requests, workflow runs, experiments, retries and configuration changes.

Proposed tasks are written as ``Task`` rows with ``status='proposed'``.
``_wire_proposal_governance`` resolves the cohort's approval once: an
approval rule that allows it approves it as the system, otherwise the chat
shows a proposal card (approve all / approve selected / reject /
feedback). ``approve_proposal`` flips dependency-ready tasks to
``in_progress``, which stages the Task agent loop's start intent in
``task_service.update_task``; tasks waiting on predecessors stay
``pending`` until the dependency gate releases them.

Trigger sources (``triggers.ReviewTriggerKind``):
  * Scheduled: the ``sr:<workspace>`` ScheduledJob installed from
    ``Workspace.heartbeat_cadence`` while the workspace heartbeat runs.
  * Event: workspace created, a work batch completed, the last human
    commitment closed, or new agents / goals / integrations detected.
  * Human-requested: the ``workspace_request_strategist_review`` tool,
    proposal-card feedback, or the failure card's retry.
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
from packages.core.strategist.triggers import (
    ReviewTrigger,
    ReviewTriggerKind,
    classify_legacy_trigger,
)

__all__ = [
    "Proposal",
    "ProposedTask",
    "ReviewTrigger",
    "ReviewTriggerKind",
    "classify_legacy_trigger",
    "run_review",
    "approve_proposal",
    "reject_proposal",
    "StrategistError",
    "install_strategist_schedule",
    "remove_strategist_schedule",
]
