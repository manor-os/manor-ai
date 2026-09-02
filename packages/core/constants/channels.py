"""Shared channel capabilities used across ingress and worker runtimes."""
from __future__ import annotations

from enum import StrEnum


class DurableInboundChannel(StrEnum):
    """Channels whose provider event ID is backed by a durable MessageLog."""

    SLACK = "slack"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    TWILIO_SMS = "twilio_sms"
    OUTLOOK = "outlook"
    MS_TEAMS = "ms_teams"
    WECHAT_PERSONAL = "wechat_personal"


class ExternalMessageActionKey(StrEnum):
    """Canonical governance actions for external channel messages."""

    SEND = "external_message.send"


DURABLE_INBOUND_CHANNEL_TYPES = frozenset(
    channel.value for channel in DurableInboundChannel
)


def uses_durable_inbound_receipt(channel_type: object) -> bool:
    """Return whether a channel must use the receipt-aware worker path."""

    return str(channel_type or "").strip() in DURABLE_INBOUND_CHANNEL_TYPES


__all__ = [
    "DURABLE_INBOUND_CHANNEL_TYPES",
    "DurableInboundChannel",
    "ExternalMessageActionKey",
    "uses_durable_inbound_receipt",
]
