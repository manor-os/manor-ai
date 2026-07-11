"""In-app channel adapter.

"In-app" is the Manor web/mobile UI itself — the same AI assistant that
answers via Telegram or email can also answer inside the app without any
external provider. This adapter exists so the gateway treats in-app
traffic as just another channel: same ChannelContact upsert, same
Conversation dedup, same agent binding.

Credentials are minimal — everything is internal:
    {}  # no external API keys needed

Outbound:
  - WebSocket push to any currently-connected client for the user (via
    ``realtime.push_notification``).
  - Always writes a Notification row (future tool) so offline users see
    the reply when they reopen the app.

Inbound:
  - The in-app chat UI POSTs user messages to
    ``/api/v1/channels/inapp/callback`` with a JSON body. No signature
    verification — auth happens at the HTTP layer (JWT middleware).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from packages.core.models.channel import ChannelConfig
from packages.core.services.channels.base import (
    ChannelAdapter, NormalizedInbound, register_adapter,
)
from packages.core.services.realtime import push_notification

logger = logging.getLogger(__name__)


class InAppChannelAdapter(ChannelAdapter):
    channel_type = "inapp"

    async def send_text(
        self, cc: ChannelConfig, to: str, text: str, **kwargs: Any,
    ) -> Dict[str, Any]:
        # ``to`` is the Manor user_id for in-app.
        await push_notification(
            user_id=to,
            notification={
                "type": "agent_message",
                "channel_config_id": cc.id,
                "text": text,
                "conversation_id": kwargs.get("conversation_id"),
            },
        )
        return {"delivered_via": "websocket", "user_id": to}

    async def verify_inbound(self, cc: ChannelConfig, *, headers, query, body) -> bool:
        # Auth happens at the HTTP middleware layer — this path is only
        # reachable via authenticated JWT.
        return True

    async def parse_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> Optional[NormalizedInbound]:
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            return None
        user_id = str(payload.get("user_id") or "")
        if not user_id:
            return None
        return NormalizedInbound(
            channel_type="inapp",
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=user_id,
            sender_name=payload.get("display_name"),
            reply_to=user_id,
            content=payload.get("text", "") or "",
            message_type="text",
            external_message_id=payload.get("client_msg_id"),
            raw=payload,
        )


register_adapter(InAppChannelAdapter())
