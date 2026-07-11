"""Webchat channel adapter — embeddable chat widget / QR code link.

"Webchat" is a public-facing chat interface accessed via a shareable
URL or QR code. Unlike the in-app channel (requires JWT auth), webchat
is anonymous or lightly identified (visitor provides name/email).

The public chat page is served at:
    /chat/{public_token}

No external provider credentials needed — Manor hosts everything.

Outbound:
  - WebSocket push to the active browser session for the visitor.
  - Falls back to polling if WebSocket isn't connected.

Inbound:
  - The public chat page POSTs messages to
    ``/api/v1/public/chat/{channel_id}/message`` with a JSON body.
  - No JWT auth — identified by channel_id + session_token.
  - Rate-limited per session to prevent abuse.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from packages.core.models.channel import ChannelConfig
from packages.core.services.channels.base import (
    ChannelAdapter, NormalizedInbound, register_adapter,
)

logger = logging.getLogger(__name__)


class WebchatAdapter(ChannelAdapter):
    channel_type = "webchat"

    async def send_text(
        self, cc: ChannelConfig, to: str, text: str, **kwargs: Any,
    ) -> Dict[str, Any]:
        # ``to`` is the visitor session_id.
        # The gateway already persists the reply into MessageLog + Message.
        # The public chat endpoint polls for new messages, so no extra
        # push is needed. When WebSocket support is added later, push here.
        return {"delivered_via": "webchat", "session_id": to}

    async def verify_inbound(self, cc: ChannelConfig, *, headers, query, body) -> bool:
        # Public endpoint — no signature verification.
        # Rate limiting and session validation happen at the router layer.
        return True

    async def parse_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> Optional[NormalizedInbound]:
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            return None

        session_id = str(payload.get("session_id") or "")
        if not session_id:
            return None

        return NormalizedInbound(
            channel_type="webchat",
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=session_id,
            sender_name=payload.get("visitor_name"),
            sender_username=payload.get("visitor_email"),
            reply_to=session_id,
            content=payload.get("text", "") or "",
            message_type=payload.get("message_type", "text"),
            attachments=payload.get("attachments") or [],
            external_message_id=payload.get("client_msg_id"),
            raw=payload,
        )

    def webhook_path(self, cc: ChannelConfig) -> str:
        return f"/api/v1/public/chat/{cc.id}/message"


register_adapter(WebchatAdapter())
