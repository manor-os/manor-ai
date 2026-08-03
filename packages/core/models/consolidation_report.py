"""ConsolidationReport — one domain's factual digest for a review (M3).

Each Strategist review (M2 ``ReviewRun``) fans out to the domain
consolidators (M4). Every consolidator produces exactly one row here:
a deterministic, *observation-only* digest of what happened in the
review's frozen ledger window. Reports carry facts (metrics /
observations / uncertainties / coverage) — never recommendations; the
Pydantic contract in ``packages.core.consolidators.contract`` enforces
the forbidden-key blacklist before a row is written.

``input_hash`` = sha256 over (domain, analyzer_version, watermarks,
config revisions) — when the latest succeeded review already holds a
report with the same hash, ``run_all`` copies it instead of re-running
(``coverage.reused = true``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, generate_ulid


class ConsolidationReport(Base):
    __tablename__ = "consolidation_reports"
    __table_args__ = (
        # v1 has no scope sharding (scope is always {}), so the unique key is
        # (review_id, domain) — the spec's scope_hash component is deferred
        # until L2 sharded consolidators actually exist.
        UniqueConstraint("review_id", "domain", name="uq_consolidation_reports_review_domain"),
        Index("ix_consolidation_reports_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(26), nullable=False)
    review_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # goal|execution|automation_portfolio|artifact_knowledge|
    # human_participation|capacity_cost|risk_governance|learning_evidence
    domain: Mapped[str] = mapped_column(String(40), nullable=False)
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # L2 sharding slot; v1 = {}

    # complete|partial|failed
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    metrics: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    observations: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    relationships: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    uncertainties: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    evidence_refs: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    # {records_examined, records_missing_details, sources: {...}, reused}
    coverage: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    analyzer_version: Mapped[str] = mapped_column(String(60), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
