"""Channel adapter — abstract base + registry.

Every channel (Telegram, WeChat, WhatsApp, email, Slack, Discord, Twilio
SMS, Twilio voice, in-app notifications, …) conforms to the same small
interface so:

  - the channel_gateway can dispatch outbound replies with one call
    (``ADAPTERS[channel_type].send_text(cc, to, text)``),
  - inbound webhooks share one verify/parse contract,
  - adding a new channel is a new file + one `register_adapter()` call,
    never a touch on the gateway.

Adapters are free to keep their existing convenience methods (e.g.
``TelegramAdapter.send_photo``) — the ABC just guarantees the minimum
surface the gateway needs.
"""
from __future__ import annotations

import abc
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from packages.core.models.channel import ChannelConfig


# ── Normalised shapes ───────────────────────────────────────────────────────

@dataclass
class NormalizedInbound:
    """The canonical shape a gateway consumes.

    Every adapter's ``parse_inbound`` must return (roughly) this — the
    gateway won't know or care about XML vs JSON vs multipart.
    """
    channel_type: str
    channel_config_id: str
    entity_id: str

    # Channel-native identity of the sender — becomes ChannelContact.source_id
    source_id: str
    sender_name: Optional[str] = None
    sender_username: Optional[str] = None

    # Address to reply to (may equal source_id for 1:1 channels; differs
    # for group chats where reply goes to chat_id not user_id)
    reply_to: Optional[str] = None

    content: str = ""
    message_type: str = "text"   # text | image | voice | video | file | event
    attachments: List[dict] = field(default_factory=list)
    external_message_id: Optional[str] = None

    # Arbitrary channel-specific context that may be useful later
    raw: Dict[str, Any] = field(default_factory=dict)


# ── Adapter ABC ─────────────────────────────────────────────────────────────

class ChannelAdapter(abc.ABC):
    """Polymorphic channel adapter.

    Subclasses are pure logic — no DB access. The gateway passes the
    ``ChannelConfig`` so credentials + URL-building live in one place.
    """

    #: Stable key — must match ``ChannelConfig.channel_type`` and the
    #: ``Channel.type`` binding value.
    channel_type: str = ""

    # ── Outbound ────────────────────────────────────────────────────────

    @abc.abstractmethod
    async def send_text(
        self, cc: ChannelConfig, to: str, text: str, **kwargs: Any,
    ) -> Dict[str, Any]:
        """Send a plain text reply. ``to`` is whatever the adapter's
        ``parse_inbound`` put in ``NormalizedInbound.reply_to``."""

    async def send_attachment(
        self,
        cc: ChannelConfig,
        to: str,
        *,
        url: Optional[str] = None,
        data: Optional[bytes] = None,
        mime_type: Optional[str] = None,
        caption: Optional[str] = None,
        kind: str = "document",  # document | image | audio | video
    ) -> Dict[str, Any]:
        """Send a media attachment. Default: unsupported."""
        raise NotImplementedError(
            f"{self.channel_type} adapter does not support attachments yet."
        )

    async def send_actionable_message(
        self,
        cc: ChannelConfig,
        to: str,
        text: str,
        *,
        actions: List[dict],
    ) -> Dict[str, Any]:
        """Send a message with selectable actions (HITL approve / reject).

        Channels with native interactive primitives (Telegram inline
        keyboard, WhatsApp quick replies, Slack interactive blocks)
        override this with their rich UI. The default implementation
        appends a numbered "Reply with…" footer and falls back to
        ``send_text`` — works on every channel, just less pretty.

        Whatever the rendering, the ``key`` of each action is what the
        adapter must echo back as the inbound content (``callback_data``
        for Telegram, button id for WhatsApp, etc.) so the notification
        callbacks matcher resolves it the same way it would resolve a
        typed reply.
        """
        footer_lines: list[str] = []
        for idx, action in enumerate(actions, start=1):
            if not isinstance(action, dict):
                continue
            label = action.get("label") or action.get("key")
            if isinstance(label, str) and label:
                footer_lines.append(f"{idx}. {label}")
        rendered = text
        if footer_lines:
            rendered = rendered.rstrip() + "\n\nReply with:\n" + "\n".join(footer_lines)
        return await self.send_text(cc, to, rendered)

    # ── Inbound ─────────────────────────────────────────────────────────

    async def verify_inbound(
        self,
        cc: ChannelConfig,
        *,
        headers: Dict[str, str],
        query: Dict[str, str],
        body: bytes,
    ) -> bool:
        """Validate the request really came from the provider (signature
        / bearer / IP). Default: trust caller; per-channel subclasses
        should override (Slack signs, Discord signs, WeChat signs, …).
        """
        return True

    @abc.abstractmethod
    async def parse_inbound(
        self,
        cc: ChannelConfig,
        *,
        headers: Dict[str, str],
        query: Dict[str, str],
        body: bytes,
    ) -> Optional[NormalizedInbound]:
        """Parse a raw webhook body into a NormalizedInbound, or None
        when the update isn't a user message (delivery receipt, typing
        event, …)."""

    # ── Provisioning ────────────────────────────────────────────────────

    def webhook_path(self, cc: ChannelConfig) -> str:
        """Return the URL path part Manor exposes for this channel's
        inbound webhook. Combined with ``settings.PUBLIC_BASE_URL`` to
        produce the URL handed to the provider.
        """
        return f"/api/v1/channels/{self.channel_type}/callback?config_id={cc.id}"

    async def register_webhook(self, cc: ChannelConfig) -> Dict[str, Any]:
        """Called once when credentials are saved. Telegram / WhatsApp
        / Slack register themselves automatically with the provider.
        Channels that don't need registration (email, WeChat where the
        admin pastes the URL into mp.weixin.qq.com by hand) can leave
        the default no-op.
        """
        return {"registered": False, "reason": "no-op"}

    async def unregister_webhook(self, cc: ChannelConfig) -> Dict[str, Any]:
        return {"unregistered": False, "reason": "no-op"}

    # ── Live reply-in-progress feedback ─────────────────────────────────

    @asynccontextmanager
    async def typing_indicator(
        self, cc: ChannelConfig, to: str,
    ) -> AsyncIterator[None]:
        """Show a "typing…" / "recording…" state in the upstream chat
        while the agent is thinking. Called as::

            async with adapter.typing_indicator(cc, chat_id):
                reply = await run_agent(...)

        Default: no-op. Telegram overrides to loop ``sendChatAction``
        every 4 s (Telegram's typing state expires ~5 s after the last
        call, so 4 s keeps it steady).
        """
        yield


# ── Registry ────────────────────────────────────────────────────────────────

_ADAPTERS: Dict[str, ChannelAdapter] = {}


def register_adapter(adapter: ChannelAdapter) -> None:
    """Register an adapter instance under ``adapter.channel_type``.

    Called at module import time by each adapter file so an entry
    appears in ``ADAPTERS`` as soon as ``packages.core.services.channels``
    is imported.
    """
    if not adapter.channel_type:
        raise ValueError("ChannelAdapter must declare channel_type")
    _ADAPTERS[adapter.channel_type] = adapter


def get_adapter(channel_type: str) -> Optional[ChannelAdapter]:
    return _ADAPTERS.get(channel_type)


def registered_channel_types() -> List[str]:
    return sorted(_ADAPTERS.keys())


# Public alias for callers that want to iterate
ADAPTERS = _ADAPTERS
