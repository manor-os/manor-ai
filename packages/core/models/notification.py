"""Notification model."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.constants.notification_types import (
    NotificationDeliveryStatus,
    NotificationDispatchStatus,
    NotificationOutboxStatus,
)

from .base import Base, generate_ulid


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user", "user_id", "created_at"),
        Index("ix_notifications_workspace", "workspace_id", "created_at"),
        Index(
            "uq_notifications_recipient_idempotency",
            "entity_id", "user_id", "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_notifications_due",
            "dispatch_status", "deliver_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(500))
    content: Mapped[Optional[str]] = mapped_column(String)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Scheduled delivery. ``deliver_at`` is the wall-clock target; the
    # row is created up-front so it survives broker restarts and can be
    # cancelled, but external channel fan-out is deferred until the
    # sweeper picks it up.
    #
    # ``dispatch_status`` lifecycle:
    #   - ``dispatched`` (default) — the row is live on the bell; external
    #     channel status is owned by NotificationOutboxEvent
    #   - ``pending``                — future delivery; in-app row is
    #     hidden from the bell list and the sweeper will pick it up
    #   - ``canceled``               — producer revoked before delivery
    deliver_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    dispatch_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=NotificationDispatchStatus.DISPATCHED.value,
        server_default=NotificationDispatchStatus.DISPATCHED.value,
    )


class NotificationOutboxEvent(Base):
    """Durable external fan-out intent for one logical notification.

    The parent ``Notification`` is the in-app source of truth. This row is
    claimed with a lease and may be retried at least once without creating a
    second parent notification.
    """

    __tablename__ = "notification_outbox_events"
    __table_args__ = (
        Index(
            "uq_notification_outbox_notification",
            "notification_id",
            unique=True,
        ),
        Index(
            "ix_notification_outbox_due",
            "status", "available_at",
        ),
        Index(
            "ix_notification_outbox_lease",
            "status", "locked_until",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    notification_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        nullable=False,
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=NotificationOutboxStatus.PENDING.value,
        server_default=NotificationOutboxStatus.PENDING.value,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[Optional[str]] = mapped_column(String(26))
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


class NotificationDelivery(Base):
    """One per (notification, external channel) — tracks both dispatch + callback.

    Notifications fan out from one logical event to N channels. A delivery
    row exists for every external (non in-app) channel the dispatcher
    selected; in-app delivery is implicit via the parent ``Notification``
    row itself.

    When a notification carries ``actions`` (e.g. "approve"/"reject" for a
    HITL approval), the delivery row stores those action keys + the
    callback handler to fire when the user replies. The channel gateway's
    inbound path checks for an open delivery on the conversation before
    running the agent — if the inbound text matches an action key, we
    dispatch the callback and short-circuit the agent run.

    ``status`` lifecycle:
      - ``pending``  — created at dispatch time; awaiting user response
      - ``sent``     — adapter accepted the outbound; still awaiting reply
      - ``resolved`` — user replied with a recognised action key
      - ``expired``  — past ``expires_at`` without a reply
      - ``canceled`` — producer cancelled (e.g. task auto-resolved server-side)
      - ``failed``   — adapter rejected the outbound; no callback expected
    """
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        Index("ix_notif_delivery_notification", "notification_id"),
        Index(
            "ix_notif_delivery_open_by_conv",
            "conversation_id", "status",
        ),
        Index(
            "ix_notif_delivery_open_by_contact",
            "channel_contact_id", "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    notification_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # Resolved channel target — copied off the ChannelContact at dispatch
    # time so the row is still useful after the contact is renamed/blocked.
    channel_contact_id: Mapped[str] = mapped_column(String(26), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(30), nullable=False)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(26))

    # MessageLog row written when the adapter shipped the text.
    message_log_id: Mapped[Optional[str]] = mapped_column(String(26))

    # Actions the user can reply with. Each entry: {"key": "approve",
    # "label": "Approve", "synonyms": ["yes", "ok"]}. ``key`` is what we
    # match the inbound text against (case-insensitive); ``synonyms`` are
    # optional shortcuts.
    actions: Mapped[Optional[list]] = mapped_column(JSONB)

    # Callback the inbound handler dispatches when an action matches.
    # ``kind`` is the key registered via
    # ``notification_callbacks.register_callback``; ``payload`` is the
    # caller-supplied context (task id, workspace id, etc.) the handler
    # needs to act on the response.
    callback_kind: Mapped[Optional[str]] = mapped_column(String(64))
    callback_payload: Mapped[Optional[dict]] = mapped_column(JSONB)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=NotificationDeliveryStatus.PENDING.value,
    )
    resolved_action_key: Mapped[Optional[str]] = mapped_column(String(64))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[Optional[str]] = mapped_column(String)

    # Optional TTL. Producers default to 24h via the service helper; the
    # column allows row-level overrides for short-lived events (OTPs etc.).
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )
