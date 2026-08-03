"""Proposal / ProposalItem — the persisted Strategist decision cohort (M7).

One ``proposals`` row per Strategist review that produced at least one
item, plus one ``proposal_items`` row per proposed unit of work. For
``kind="task"`` the item is a bookkeeping + governance layer over the
existing ``Task(status='proposed')`` bridge; the other kinds drive their
own execution: ``human_request`` opens a HumanCommitment,
``experiment`` starts an Experiment, and the three change kinds
(``automation_change`` / ``workflow_change`` / ``goal_change``) are
applied to their canonical row by
``packages.core.proposals.change_executor`` under a revision CAS.

Naming: the ORM classes are ``ProposalRecord`` / ``ProposalItemRecord``
(NOT ``Proposal``) to avoid import confusion with the pydantic
``Proposal`` LLM-output schema in ``packages.core.strategist.proposal``.
The table names stay ``proposals`` / ``proposal_items`` per the design
doc.

No DB-level FKs — matching the repo convention (see e.g.
``consolidation_reports.review_id``); referential integrity is owned by
the service layer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class ProposalRecord(Base):
    __tablename__ = "proposals"
    __table_args__ = (
        Index("ix_proposals_review_id", "review_id"),
        Index("ix_proposals_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    # ReviewRun.id on the v2 path (the cohort's strategist_review_id).
    review_id: Mapped[str] = mapped_column(String(64), nullable=False)

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # open | resolved | expired
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ProposalItemRecord(Base):
    __tablename__ = "proposal_items"
    __table_args__ = (
        UniqueConstraint("proposal_id", "item_key", name="uq_proposal_items_proposal_item_key"),
        Index("ix_proposal_items_proposal", "proposal_id"),
        Index("ix_proposal_items_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    proposal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # Stable key within the proposal; dependency references use it.
    item_key: Mapped[str] = mapped_column(String(40), nullable=False)
    # task | human_request | automation_change | workflow_change | goal_change | experiment
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    # Per-kind pydantic model dump (see packages.core.proposals.schema).
    # For kind="task" this is the ProposedTask dump + {"task_id": <Task.id>}.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {report_refs: [...], evidence_refs: [...]} — validate-if-present in v1.
    basis: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    correlation_key: Mapped[Optional[str]] = mapped_column(String(96), nullable=True)
    # Validator-computed, never model-self-reported.
    risk_level: Mapped[str] = mapped_column(String(12), nullable=False, default="low")
    # M8 approval-catalog mapping (e.g. "workspace.proposal.task").
    action_key: Mapped[str] = mapped_column(String(120), nullable=False)
    depends_on_item_keys: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)

    # proposed | approved | rejected | expired | executing | succeeded | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    approval_request_id: Mapped[Optional[str]] = mapped_column(String(26), nullable=True)
    # {decided_by, decision, reason_code, comment, decided_at}
    decision: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # CAS precheck slot for change-kind items (unused in v1 task items).
    expected_revision: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Execution root once dispatched (v1: the strategist work-batch id).
    execution_root_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
