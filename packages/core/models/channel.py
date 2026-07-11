"""Communication channel models — unified abstraction for email, SMS, voice, WhatsApp, etc.

Ported from Java backend:
  - ClientEmailConfig (SMTP/IMAP settings)
  - SysSourceConfig (provider credentials + extra config)
  - SysSourceMailMessage (mail message log)
  - TwilioServiceImpl (SMS/voice via Twilio)
  - WhatsAppAnnouncement / WhatsAppAnnouncementRecipient (broadcast messaging)
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, Index, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


# ---------------------------------------------------------------------------
# Channel configuration (replaces ClientEmailConfig + SysSourceConfig)
# ---------------------------------------------------------------------------

class ChannelConfig(Base, TimestampMixin):
    """Channel integration configuration (email SMTP, Twilio, WhatsApp Cloud, etc.).

    The *config* JSONB holds provider-specific settings (host, port, webhook URL, ...),
    while *credentials* JSONB holds secrets (password, API token, account SID, ...).
    Credentials should be encrypted at rest via application-level encryption.

    Maps to Java:
      - ClientEmailConfig  -> channel_type='email', provider='smtp'
      - SysSourceConfig    -> channel_type varies, provider varies
    """
    __tablename__ = "channel_configs"
    __table_args__ = (
        Index("ix_channel_configs_entity", "entity_id"),
        Index("ix_channel_configs_type", "entity_id", "channel_type"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))

    # Channel classification
    channel_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # email | sms | voice | whatsapp | telegram | webchat
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    # smtp | twilio | whatsapp_cloud | telegram_bot | ...

    name: Mapped[Optional[str]] = mapped_column(String(255))  # friendly label

    # Provider-specific settings (non-secret)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # e.g. {"smtp_host": "...", "smtp_port": 587, "imap_host": "...", "from_email": "..."}

    # Secrets — read via CredentialService.lease_channel_config which routes
    # legacy_jsonb (this column) and vault-encrypted refs (credential_ref)
    # transparently. New writes should call store_channel_config which clears
    # this field and populates credential_ref.
    credentials: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    credential_ref: Mapped[Optional[str]] = mapped_column(Text)
    credential_scheme: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="legacy_jsonb",
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # active | inactive | error


# ---------------------------------------------------------------------------
# Channel contact — per-channel identity anchor (à la Chatwoot ContactInbox)
# ---------------------------------------------------------------------------

class ChannelContact(Base, TimestampMixin):
    """Per-channel identity for a person the bot has talked to.

    One row per (channel_config, source_id) pair. ``source_id`` is shaped
    by the channel:
      - email       → email address
      - telegram    → Telegram user id
      - whatsapp    → phone number without '+'
      - twilio_sms  → E.164 phone
      - wechat      → OpenID
      - discord     → Discord user id
      - slack       → Slack user id
      - inapp       → internal user_id

    Purpose:
      1. **Inbound dedup** — the (channel_config_id, source_id) unique
         index makes "same sender again" idempotent.
      2. **Conversation anchor** — Conversations key off channel_contact_id
         so history is stable even if the display name changes.
      3. **Future cross-channel identity** — the optional ``contact_id``
         FK lets a Person row stitch together the same human across
         WhatsApp + email + Telegram later without touching messages.
    """
    __tablename__ = "channel_contacts"
    __table_args__ = (
        Index(
            "uq_channel_contact_source",
            "channel_config_id", "source_id",
            unique=True,
        ),
        Index("ix_channel_contacts_entity", "entity_id"),
        Index("ix_channel_contacts_contact", "contact_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    channel_config_id: Mapped[str] = mapped_column(String(26), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # telegram | wechat | whatsapp | email | slack | discord | twilio_sms | …

    # Channel-native identity — always present, shape depends on channel
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Optional display bits captured at first contact
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    username: Mapped[Optional[str]] = mapped_column(String(255))
    # Channel-specific extras (avatar url, locale, phone country, …)
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    # Optional link to a unified Contact / Person row — null until a
    # manual or heuristic merge connects this identity to a known person.
    contact_id: Mapped[Optional[str]] = mapped_column(String(26))

    # Optional link to a Manor ``User`` — set when the sender claimed
    # this channel identity via the Profile → Channels flow. When set,
    # the gateway plumbs ``user_id`` into the Runtime Harness tool executor so MCP
    # tools resolve the user's personal OAuth tokens + role permissions
    # (exactly as if they were chatting in the web UI).
    user_id: Mapped[Optional[str]] = mapped_column(String(26))

    # Effective role for permission checks during a channel conversation.
    # - "external"  (default) — unauthenticated sender, read-only tools,
    #                          no destructive actions
    # - "member"             — treat as a regular entity member
    # - "admin"              — full entity admin
    # - or any custom role key from StaffRole.slug
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="external")

    # Per-sender subscription pin — lets a shared channel route each
    # contact to a specific (agent × workspace) subscription. NULL means
    # "fall through to the Channel binding's default subscription".
    agent_subscription_id: Mapped[Optional[str]] = mapped_column(String(26))

    # Authz knobs — block abusive senders without nuking the history
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # active | blocked

    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------------------
# Channel link token — one-time secret for end-user claim flows
# ---------------------------------------------------------------------------

class ChannelLinkToken(Base, TimestampMixin):
    """Short-lived token a user generates from Settings to claim a
    ``ChannelContact`` as their own.

    Flow:

      1. User clicks "Connect Telegram" → POST creates a row with
         ``token`` (random 12-char), ``user_id``, ``entity_id``,
         ``channel_type`` and a ~15 min ``expires_at``.
      2. Frontend opens ``t.me/<bot>?start=<token>`` — Telegram delivers
         ``/start <token>`` to the bot as a normal message.
      3. ``channel_gateway.dispatch_inbound`` sees the prefix, looks up
         the row by token, sets the inbound contact's ``user_id`` +
         ``role`` to the claiming user's, marks ``claimed_at``, and
         skips the agent run.
      4. UI polls / refetches and sees the contact under "Connected
         Channels".

    Single-use: ``claimed_at`` is set on success and the gateway refuses
    to claim a second contact with the same token. Anyone who intercepts
    the token still needs to send it through *the bot the user already
    intended to bind* — and even then it pins to the user_id that
    generated it, not the sender's identity.
    """
    __tablename__ = "channel_link_tokens"
    __table_args__ = (
        Index("ux_channel_link_token", "token", unique=True),
        Index("ix_channel_link_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    token: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(30), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    claimed_contact_id: Mapped[Optional[str]] = mapped_column(String(26))


# ---------------------------------------------------------------------------
# Message log (replaces SysSourceMailMessage + TwilioSmsRecord + TwilioVoiceRecord)
# ---------------------------------------------------------------------------

class MessageLog(Base, TimestampMixin):
    """Unified log of all channel messages — inbound and outbound.

    Maps to Java:
      - SysSourceMailMessage (email)
      - TwilioSmsRecord (SMS)
      - TwilioVoiceRecord (voice)
      - WhatsAppMessageLog (WhatsApp)
    """
    __tablename__ = "message_logs"
    __table_args__ = (
        Index("ix_message_logs_entity", "entity_id"),
        Index("ix_message_logs_conversation", "conversation_id"),
        Index("ix_message_logs_channel_config", "channel_config_id"),
        Index("ix_message_logs_external", "external_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    channel_config_id: Mapped[Optional[str]] = mapped_column(String(26))
    conversation_id: Mapped[Optional[str]] = mapped_column(String(26))

    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    # inbound | outbound
    channel_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # email | sms | voice | whatsapp

    from_address: Mapped[Optional[str]] = mapped_column(String(500))
    to_address: Mapped[Optional[str]] = mapped_column(String(500))
    subject: Mapped[Optional[str]] = mapped_column(String(1000))
    content: Mapped[Optional[str]] = mapped_column(String)       # plain text body
    html_content: Mapped[Optional[str]] = mapped_column(String)  # HTML body (email)

    attachments: Mapped[Optional[dict]] = mapped_column(JSONB)
    # e.g. [{"filename": "...", "url": "...", "size": 1234}]

    external_id: Mapped[Optional[str]] = mapped_column(String(255))
    # Provider's message ID (Gmail message ID, Twilio SID, WhatsApp wamid, ...)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="sent")
    # sent | delivered | failed | received | queued
    error_message: Mapped[Optional[str]] = mapped_column(String)

    # Cost tracking (Twilio charges, etc.)
    cost_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    cost_currency: Mapped[Optional[str]] = mapped_column(String(3))  # USD, etc.

    # Voice-specific
    duration_seconds: Mapped[Optional[int]] = mapped_column()
    recording_url: Mapped[Optional[str]] = mapped_column(String(1000))


# ---------------------------------------------------------------------------
# Phone numbers (replaces TwilioBoughtPhoneNumbers)
# ---------------------------------------------------------------------------

class PhoneNumber(Base, TimestampMixin):
    """Phone numbers provisioned via Twilio (or other provider) for an entity."""
    __tablename__ = "phone_numbers"
    __table_args__ = (
        Index("ix_phone_numbers_entity", "entity_id"),
        Index("ix_phone_numbers_number", "phone_number", unique=True),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(30), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="twilio")
    provider_id: Mapped[Optional[str]] = mapped_column(String(255))  # Twilio phone number SID

    capabilities: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # e.g. {"sms": true, "voice": true, "mms": true, "whatsapp": false}

    monthly_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # active | released | pending


# ---------------------------------------------------------------------------
# Announcements (replaces WhatsAppAnnouncement)
# ---------------------------------------------------------------------------

class Announcement(Base, TimestampMixin):
    """Broadcast announcement via WhatsApp, SMS, or email."""
    __tablename__ = "announcements"
    __table_args__ = (
        Index("ix_announcements_entity", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    channel_config_id: Mapped[Optional[str]] = mapped_column(String(26))

    channel_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # whatsapp | sms | email
    title: Mapped[Optional[str]] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(String, nullable=False)

    # Template fields (WhatsApp message templates)
    template_id: Mapped[Optional[str]] = mapped_column(String(255))
    template_name: Mapped[Optional[str]] = mapped_column(String(255))
    template_language: Mapped[Optional[str]] = mapped_column(String(10))

    schedule_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    recipient_count: Mapped[int] = mapped_column(default=0)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    # draft | scheduled | sending | sent | failed
    error_message: Mapped[Optional[str]] = mapped_column(String)


class AnnouncementRecipient(Base, TimestampMixin):
    """Individual recipient of an announcement broadcast."""
    __tablename__ = "announcement_recipients"
    __table_args__ = (
        Index("ix_announcement_recipients_ann", "announcement_id"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    announcement_id: Mapped[str] = mapped_column(String(26), nullable=False)
    recipient_address: Mapped[str] = mapped_column(String(500), nullable=False)
    # phone number or email address

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending | sent | delivered | failed
    error_message: Mapped[Optional[str]] = mapped_column(String)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
