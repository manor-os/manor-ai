"""Staff, department, role, and schedule models.

The ``Staff`` table is the unified people-who-do-work-for-the-entity model.
It covers employees, contractors, vendors, and other external service
providers, discriminated by ``kind``. Vendor-specific fields
(``company_name``, ``tax_id``, ``billing_rate`` etc.) stay NULL for
employees; user-facing fields (``user_id``, ``role_id``, ``department_id``)
stay NULL for vendors. This lets the maintenance coordinator assign a job
to a plumber and the ops manager assign a task to an employee through the
same assignment primitive.

Clients (people who *buy* from the entity) live in ``people.Client`` — a
separate concept.
"""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, Numeric, SmallInteger,
    String, Text, Time, func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, SoftDeleteMixin, TimestampMixin, generate_ulid


# ── Staff kinds ─────────────────────────────────────────────────────────────

STAFF_KIND_EMPLOYEE = "employee"     # has Manor login, on-payroll, full RBAC
STAFF_KIND_CONTRACTOR = "contractor" # individual, paid per job, may have login
STAFF_KIND_VENDOR = "vendor"         # external company; work orders + invoices
STAFF_KIND_EXTERNAL = "external"     # loose external collaborator / referral

STAFF_KINDS = (
    STAFF_KIND_EMPLOYEE,
    STAFF_KIND_CONTRACTOR,
    STAFF_KIND_VENDOR,
    STAFF_KIND_EXTERNAL,
)


class Department(Base, TimestampMixin, SoftDeleteMixin):
    """Organizational department within an entity."""
    __tablename__ = "departments"
    __table_args__ = (
        Index("ix_departments_entity", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("departments.id"), nullable=True,
    )
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class StaffRole(Base, TimestampMixin):
    """Role with a permission set, scoped to an entity."""
    __tablename__ = "staff_roles"
    __table_args__ = (
        Index("ix_staff_roles_entity", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    permissions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")


class Staff(Base, TimestampMixin, SoftDeleteMixin):
    """Unified staff model — employees, contractors, vendors, externals."""
    __tablename__ = "staff"
    __table_args__ = (
        Index("ix_staff_entity", "entity_id"),
        Index("ix_staff_user", "user_id"),
        Index("ix_staff_kind", "entity_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # Discriminator
    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=STAFF_KIND_EMPLOYEE
    )
    # STAFF_KIND_EMPLOYEE | CONTRACTOR | VENDOR | EXTERNAL

    # Identity (required for all kinds; for vendors this is typically the
    # primary contact person's name, with company_name populated separately).
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(50))
    avatar_url: Mapped[Optional[str]] = mapped_column(String(500))

    # Manor login (employees; rare for contractors; typically none for vendors)
    user_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("users.id"), nullable=True,
    )

    # Org placement (employees + some contractors)
    title: Mapped[Optional[str]] = mapped_column(String(255))
    department_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("departments.id"), nullable=True,
    )
    role_id: Mapped[Optional[str]] = mapped_column(
        String(26), ForeignKey("staff_roles.id"), nullable=True,
    )

    # Capabilities (applies across kinds)
    skills: Mapped[Optional[list]] = mapped_column(ARRAY(String), nullable=True)
    service_categories: Mapped[Optional[list]] = mapped_column(
        ARRAY(String), nullable=True
    )
    # For vendors — coarse taxonomy ("plumbing", "electrical", "cleaning").
    # Employees use `skills` for finer-grain capabilities.

    # Vendor / contractor commercials — NULL for employees
    company_name: Mapped[Optional[str]] = mapped_column(String(255))
    tax_id: Mapped[Optional[str]] = mapped_column(String(64))
    billing_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    billing_currency: Mapped[Optional[str]] = mapped_column(String(8))
    # Three-letter ISO (USD, AUD, EUR, GBP, CNY, ...)
    payment_terms: Mapped[Optional[str]] = mapped_column(String(64))
    # Free-form: "net 30", "on completion", "50% deposit" ...
    preferred_payment_method: Mapped[Optional[str]] = mapped_column(String(32))
    # "bank_transfer" | "stripe" | "check" | "cash" | ...

    # Contact / geo
    address: Mapped[Optional[str]] = mapped_column(String)
    website: Mapped[Optional[str]] = mapped_column(String(255))
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # Catch-all for kind-specific extension data (insurance policies, license
    # numbers, on-call preferences, etc.).
    meta: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # active | inactive | invited | archived


class StaffSchedule(Base, TimestampMixin):
    """Weekly recurring availability for a staff member."""
    __tablename__ = "staff_schedules"
    __table_args__ = (
        Index("ix_staff_schedules_staff", "staff_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    staff_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("staff.id"), nullable=False,
    )
    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 0=Mon … 6=Sun
    shift_start: Mapped[time] = mapped_column(Time, nullable=False)
    shift_end: Mapped[time] = mapped_column(Time, nullable=False)


class StaffScheduleAdjustment(Base, TimestampMixin):
    """One-off schedule override (PTO, shift swap, overtime, etc.)."""
    __tablename__ = "staff_schedule_adjustments"
    __table_args__ = (
        Index("ix_staff_adj_staff_date", "staff_id", "date"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    staff_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("staff.id"), nullable=False,
    )
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    adjustment_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # adjustment_type values: day_off, shift_change, overtime
    shift_start: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    shift_end: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(500))
