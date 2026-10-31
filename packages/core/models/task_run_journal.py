"""The append-only execution journal of the runs that work a Task.

A run row (the Task's root ``TaskCoordinatorRun`` today, its delegated child
runs later) keeps only its latest state: the checkpoint is rewritten every
round and compacted. The journal keeps what happened, one row per committed
transition of the run, written in the same transaction as the run row's CAS or
version bump, so a rolled-back transition leaves no row and a committed one
leaves exactly one.

``seq`` is the run row's ``state_version`` right after the journaled write. It
only ever grows, across wakes, parks and retries, so ``seq`` orders one run's
history by commit and ``(run_kind, run_id, seq)`` is the idempotency key (and
``ix_task_run_journal_run`` reads one run's history by ``(run_id, seq)``).
``created_at`` is ``clock_timestamp()`` and only interleaves different runs.

``payload`` is the model-visible view of the transition (bounded to 16 KB,
built by ``services.task_run_journal``). ``projected_at`` and
``projected_message_id`` are written by the Task comment projector (W4c), never
by the run's host.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class TaskRunKind(str, Enum):
    ROOT = "root"
    CHILD = "child"


class TaskRunJournalKind(str, Enum):
    """What one journal row records."""

    # A completed model round: its text, tool calls and usage.
    ROUND = "round"
    # The run revised its Task plan (update_task_plan).
    PLAN_REVISION = "plan_revision"
    # The run handed work to another agent (written from PR6).
    DELEGATE = "delegate"
    # The run parked: a timer, media jobs, a person, a capacity wait, a yield.
    PARK = "park"
    # A parked run woken: a person's answer or decision, a Task resume, a
    # settled media job, the stalled-wake backstop.
    WAKE = "wake"
    # A stopped run reopened for a new lifecycle (a Task retry).
    RETRY = "retry"
    # One delegated child run settled / a whole delegation batch settled (PR6).
    CHILD_SETTLED = "child_settled"
    BATCH_SETTLED = "batch_settled"
    # The run reached its terminal state (by itself, or because its Task was
    # cancelled or failed).
    TERMINAL = "terminal"


def _enum_check(column: str, values: type[Enum], name: str) -> CheckConstraint:
    allowed = ", ".join(f"'{member.value}'" for member in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


class TaskRunJournal(Base):
    """One committed transition of a run working a Task."""

    __tablename__ = "task_run_journal"
    __table_args__ = (
        UniqueConstraint("run_kind", "run_id", "seq", name="uq_task_run_journal_run_seq"),
        # A Task's history in commit order, and the projector's backlog.
        Index("ix_task_run_journal_task", "task_id", "created_at", "id"),
        Index(
            "ix_task_run_journal_unprojected", "task_id", "created_at", "id",
            postgresql_where=text("projected_at IS NULL"),
        ),
        Index("ix_task_run_journal_root_run", "root_run_id", "seq"),
        # One run's history by commit order, without knowing its run_kind.
        Index("ix_task_run_journal_run", "run_id", "seq"),
        _enum_check("run_kind", TaskRunKind, "ck_task_run_journal_run_kind"),
        _enum_check("kind", TaskRunJournalKind, "ck_task_run_journal_kind"),
        CheckConstraint("seq >= 0", name="ck_task_run_journal_seq"),
        CheckConstraint("depth >= 0", name="ck_task_run_journal_depth"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    task_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # None for a Task assigned directly to an Agent, outside any Workspace.
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))

    run_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    run_id: Mapped[str] = mapped_column(String(26), nullable=False)
    root_run_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # The direct parent run and its delegate_to_agent call (child runs only).
    parent_run_id: Mapped[Optional[str]] = mapped_column(String(26))
    parent_tool_call_id: Mapped[Optional[str]] = mapped_column(String(128))

    # The identity the run acts as: the projector comments as this agent.
    agent_id: Mapped[Optional[str]] = mapped_column(String(26))
    agent_subscription_id: Mapped[Optional[str]] = mapped_column(String(26))
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    lifecycle_generation: Mapped[str] = mapped_column(String(64), nullable=False)

    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    round: Mapped[Optional[int]] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"),
    )
    # The insert's wall clock, not the transaction start: rows of different
    # runs interleave by it. Within one run, order by ``seq``.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("clock_timestamp()"),
    )

    # Written by the Task comment projector (W4c) with the comment it posted.
    projected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    projected_message_id: Mapped[Optional[str]] = mapped_column(String(26))


__all__ = ["TaskRunJournal", "TaskRunJournalKind", "TaskRunKind"]
