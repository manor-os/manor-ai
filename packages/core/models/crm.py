"""Company-scoped B2B CRM records. Follow-ups are human reminders, not runtime Tasks."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, SoftDeleteMixin, TimestampMixin, generate_ulid


class ClientActivity(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "client_activities"
    __table_args__ = (Index("ix_client_activity_scope", "entity_id", "client_id", "occurred_at"),)
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26))
    client_id: Mapped[str] = mapped_column(String(26))
    kind: Mapped[str] = mapped_column(String(20), default="note")
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(26))


class ClientOpportunity(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "client_opportunities"
    __table_args__ = (Index("ix_client_opportunity_scope", "entity_id", "client_id", "stage"),)
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26))
    client_id: Mapped[str] = mapped_column(String(26))
    title: Mapped[str] = mapped_column(String(255))
    stage: Mapped[str] = mapped_column(String(20), default="qualified")
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    probability: Mapped[int] = mapped_column(Integer, default=25)
    expected_close: Mapped[date | None] = mapped_column(Date)
    owner_id: Mapped[str | None] = mapped_column(String(26))
    description: Mapped[str] = mapped_column(Text, default="")
    loss_reason: Mapped[str] = mapped_column(String(1000), default="")
    created_by: Mapped[str] = mapped_column(String(26))


class ClientFollowup(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "client_followups"
    __table_args__ = (Index("ix_client_followup_due", "entity_id", "status", "due_at"),)
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26))
    client_id: Mapped[str] = mapped_column(String(26))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    owner_id: Mapped[str | None] = mapped_column(String(26))
    priority: Mapped[str] = mapped_column(String(10), default="normal")
    status: Mapped[str] = mapped_column(String(10), default="open")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(26))
