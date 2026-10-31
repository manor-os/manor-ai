"""Durable delegated child runs of a Task agent loop.

When a Task's agent loop delegates work to another Workspace agent, each
delegated item becomes one row here. The row is a durable run like the Task's
root ``TaskCoordinatorRun``: it shares the run-row protocol in
``services.durable_run`` (stage, claim, renew, CAS checkpoint, settle,
release) and its own outbox wake (``task-agent-child:{id}``).

A child run is addressed by its own id, which is also the delegation's
``run_id`` the parent model sees and ``read_delegation_result`` reads. It
points at the Task's single root run (``root_run_id``) and at its direct
parent (``parent_ref``): the root run itself, or another child row when a
depth-1 child delegates one level further (``parent_child_id``).

Identity (subscription, agent, service) is frozen when the row is created and
re-checked on every wake. ``lifecycle_generation`` moves with the root run's
(a Task pause/resume rebinds every non-terminal row together).

``uq_task_agent_child_runs_live_agent`` lets one agent have at most one live
run per Task (the durable form of the chat delegation session's busy lock);
it also rules out an A -> B -> A cycle, because A is still live while it
waits for B.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid
from .task_coordinator import TaskCoordinatorPhase


# The deepest a child run may sit under the root run: the root delegates to
# depth 1, which may delegate once more to depth 2. Equal to the chat
# delegation limit (ai.runtime.workspace_delegation_actions.MAX_DELEGATION_DEPTH;
# tests/test_task_agent_child_runs.py keeps them equal).
TASK_AGENT_CHILD_MAX_DEPTH = 2

# A child run uses the Task run phases, except waiting_plan (a Plan/Step DAG
# coordinator phase that an agent loop never enters).
TASK_AGENT_CHILD_PHASES = frozenset(
    phase.value for phase in TaskCoordinatorPhase
    if phase is not TaskCoordinatorPhase.WAITING_PLAN
)
TASK_AGENT_CHILD_TERMINAL_PHASES = TaskCoordinatorPhase.terminal()
TASK_AGENT_CHILD_LIVE_PHASES = TASK_AGENT_CHILD_PHASES - TASK_AGENT_CHILD_TERMINAL_PHASES


def _in(values) -> str:
    return ", ".join(f"'{value}'" for value in sorted(values))


class TaskAgentChildRun(Base, TimestampMixin):
    """One delegated agent run inside a Task, driven wake by wake."""

    __tablename__ = "task_agent_child_runs"
    __table_args__ = (
        # Parking a delegation inserts its rows idempotently: a replayed park
        # never makes a second row for the same call item.
        UniqueConstraint(
            "parent_ref", "parent_tool_call_id", "item_index",
            name="uq_task_agent_child_runs_call",
        ),
        # One live run per agent per Task (see the module docstring).
        Index(
            "uq_task_agent_child_runs_live_agent",
            "task_id", "agent_subscription_id",
            unique=True,
            postgresql_where=text(
                f"phase NOT IN ({_in(TASK_AGENT_CHILD_TERMINAL_PHASES)})"
            ),
        ),
        Index("ix_task_agent_child_runs_task_phase", "task_id", "phase"),
        # Recovery scans, the same shape as ix_task_coordinator_runs_recovery.
        Index(
            "ix_task_agent_child_runs_recovery",
            "phase", "wake_generation", "updated_at",
        ),
        # The agent's previous run in this Task (transcript carry-over and the
        # do_not_replay check).
        Index(
            "ix_task_agent_child_runs_agent_history",
            "task_id", "agent_subscription_id", text("created_at DESC"),
        ),
        Index("ix_task_agent_child_runs_root", "root_run_id"),
        CheckConstraint("state_version >= 0", name="ck_task_agent_child_runs_version"),
        CheckConstraint(
            "(claim_token IS NULL AND claim_generation IS NULL AND claim_expires_at IS NULL) "
            "OR (claim_token IS NOT NULL AND claim_generation IS NOT NULL "
            "AND claim_expires_at IS NOT NULL)",
            name="ck_task_agent_child_runs_claim",
        ),
        CheckConstraint(
            "item_index >= 0 AND item_index < batch_size",
            name="ck_task_agent_child_runs_item",
        ),
        CheckConstraint(
            f"depth >= 1 AND depth <= {TASK_AGENT_CHILD_MAX_DEPTH}",
            name="ck_task_agent_child_runs_depth",
        ),
        # A depth-1 child's parent is the root run; a deeper child's parent is
        # the child row named by parent_child_id.
        CheckConstraint(
            "(parent_child_id IS NULL AND depth = 1 AND parent_ref = root_run_id) "
            "OR (parent_child_id IS NOT NULL AND depth > 1 AND parent_child_id = parent_ref)",
            name="ck_task_agent_child_runs_parent",
        ),
        CheckConstraint(
            f"phase IN ({_in(TASK_AGENT_CHILD_PHASES)})",
            name="ck_task_agent_child_runs_phase",
        ),
    )

    # Also the delegation's run_id (model-visible; read_delegation_result).
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    task_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # The Task's TaskCoordinatorRun.id.
    root_run_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # The direct parent run: root_run_id, or the parent child row's id.
    parent_ref: Mapped[str] = mapped_column(String(26), nullable=False)
    # Set (= parent_ref) only when the direct parent is itself a child row.
    parent_child_id: Mapped[Optional[str]] = mapped_column(String(26))
    # The parent's delegate_to_agent call this run answers.
    parent_tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Results return to the parent in input order.
    item_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    batch_size: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    # Frozen at creation and re-validated on every wake.
    agent_subscription_id: Mapped[str] = mapped_column(String(26), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(26), nullable=False)
    service_key: Mapped[Optional[str]] = mapped_column(String(100))
    agent_label: Mapped[Optional[str]] = mapped_column(String(255))
    # The same agent's previous run in this Task, whose transcript this one
    # continues (the chat session's memory; continue_interrupted).
    previous_child_id: Mapped[Optional[str]] = mapped_column(String(26))

    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    answer_max_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    continue_interrupted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )

    phase: Mapped[str] = mapped_column(
        String(24), nullable=False, default=TaskCoordinatorPhase.READY.value,
        server_default=TaskCoordinatorPhase.READY.value,
    )
    # The chat delegation run_status vocabulary (completed, failed, blocked,
    # cancelled, partial ...), written with the terminal phase.
    outcome_status: Mapped[Optional[str]] = mapped_column(String(24))
    # The chat session's ``interrupted`` state: set on do_not_replay, a
    # cancellation, exhausted repair or a host-decided failure. The next
    # delegation to this agent must then pass continue_interrupted.
    interrupted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )
    lifecycle_generation: Mapped[str] = mapped_column(String(64), nullable=False)

    # RuntimeRunState snapshot, child host slots and any parked wait. Kept
    # after the terminal write as the final transcript a later run continues.
    checkpoint: Mapped[Optional[dict]] = mapped_column(JSONB(none_as_null=True))
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # The same claim/wake fencing as TaskCoordinatorRun.
    wake_generation: Mapped[Optional[str]] = mapped_column(String(26))
    claim_token: Mapped[Optional[str]] = mapped_column(String(64))
    claim_generation: Mapped[Optional[str]] = mapped_column(String(26))
    claim_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # The shared delegation result payload (same shape as chat delegation).
    result: Mapped[Optional[dict]] = mapped_column(JSONB(none_as_null=True))
    # Cumulative usage, for attribution only (billing happens per call).
    usage: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"),
    )
    terminal_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
