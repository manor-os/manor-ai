"""Channel adapters — provider-specific implementations for messaging channels.

Every adapter module self-registers into ``base.ADAPTERS`` at import
time. Importing this package eagerly pulls every adapter so the registry
is complete by the time ``channel_gateway`` looks up a handler.
"""
from __future__ import annotations

from packages.core.services.channels.base import (
    ADAPTERS,
    ChannelAdapter,
    ChannelTextSendError,
    ChannelTextSendFailureDisposition,
    ChannelTextSendResultStatus,
    ChannelTextSendRetryMode,
    NormalizedInbound,
    get_adapter,
    channel_text_send_result,
    register_adapter,
    registered_channel_types,
)

# Side-effect imports — each module calls ``register_adapter(…)``. Keep
# this list in sync as new channels ship.
from packages.core.services.channels import telegram_adapter  # noqa: F401
from packages.core.services.channels import wechat_adapter    # noqa: F401
from packages.core.services.channels import wechat_personal_adapter  # noqa: F401
from packages.core.services.channels import email_adapter     # noqa: F401
from packages.core.services.channels import slack_adapter     # noqa: F401
from packages.core.services.channels import discord_adapter   # noqa: F401
from packages.core.services.channels import whatsapp_adapter  # noqa: F401
from packages.core.services.channels import ms_teams_adapter  # noqa: F401
from packages.core.services.channels import outlook_adapter    # noqa: F401
from packages.core.services.channels import twilio_adapter    # noqa: F401
from packages.core.services.channels import facebook_adapter  # noqa: F401
from packages.core.services.channels import inapp_adapter     # noqa: F401
from packages.core.services.channels import webchat_adapter   # noqa: F401
from packages.core.services.channels import twitter_x_adapter  # noqa: F401

__all__ = [
    "ADAPTERS",
    "ChannelAdapter",
    "ChannelTextSendError",
    "ChannelTextSendFailureDisposition",
    "ChannelTextSendResultStatus",
    "ChannelTextSendRetryMode",
    "NormalizedInbound",
    "get_adapter",
    "channel_text_send_result",
    "register_adapter",
    "registered_channel_types",
]
