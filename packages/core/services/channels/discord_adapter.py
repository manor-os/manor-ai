"""Discord channel adapter — Interactions webhook in, Bot API out.

Two inbound modes exist for Discord; we implement the HTTPS Interactions
endpoint (simpler, no gateway socket needed):

  1. User sends a slash command or message component.
  2. Discord POSTs a signed Interaction payload to our callback URL.
  3. Adapter verifies the Ed25519 signature using the app's public key.
  4. Gateway runs the agent, and reply is POSTed via the Bot API.

Credentials in ChannelConfig.credentials:
    {
      "bot_token":         "…"          (required)
      "public_key":        "hex-ed25519" (required for inbound verify)
      "application_id":    "…"          (required for some outbound ops)
      "default_guild_id":  "…"          (optional)
    }

Verify uses PyNaCl if available; without it, inbound verification fails
closed (deploys that don't need inbound can leave it unconfigured).
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

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

_DISCORD_API = "https://discord.com/api/v10"


class DiscordChannelAdapter(ChannelAdapter):
    channel_type = "discord"

    async def send_text(
        self, cc: ChannelConfig, to: str, text: str, **kwargs: Any,
    ) -> Dict[str, Any]:
        if httpx is None:
            raise RuntimeError("httpx is required — pip install httpx")
        token = (cc.credentials or {}).get("bot_token", "")
        if not token:
            raise RuntimeError("Discord ChannelConfig missing bot_token")
        # ``to`` is a channel_id
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_DISCORD_API}/channels/{to}/messages",
                headers={"Authorization": f"Bot {token}",
                         "Content-Type": "application/json"},
                json={"content": text},
            )
        if not resp.is_success:
            raise RuntimeError(f"Discord API error {resp.status_code}: {resp.text[:200]}")
        return {"channel_id": to, "message_id": resp.json().get("id"), "status": "sent"}

    async def send_attachment(
        self, cc: ChannelConfig, to: str, *, url=None, data=None,
        mime_type=None, caption=None, kind="document",
    ) -> Dict[str, Any]:
        """Post a message with a file attachment. Discord's API takes
        ``multipart/form-data`` with the file bytes + a JSON payload."""
        if httpx is None:
            raise RuntimeError("httpx is required — pip install httpx")
        token = (cc.credentials or {}).get("bot_token", "")
        if not token:
            raise RuntimeError("Discord ChannelConfig missing bot_token")
        if not url and not data:
            raise RuntimeError("send_attachment needs url or data")

        async with httpx.AsyncClient(timeout=30) as client:
            if data is None:
                fr = await client.get(url)   # type: ignore[arg-type]
                fr.raise_for_status()
                data = fr.content
                mime_type = mime_type or fr.headers.get(
                    "Content-Type", "application/octet-stream",
                )

            # Extract a reasonable filename from the URL or caption
            import os as _os
            from urllib.parse import urlparse as _urlparse
            if url:
                parsed = _urlparse(url)
                fname = _os.path.basename(parsed.path) or "attachment"
            else:
                fname = caption or "attachment"

            files = {"files[0]": (fname, data, mime_type or "application/octet-stream")}
            payload: Dict[str, Any] = {}
            if caption:
                payload["content"] = caption
            resp = await client.post(
                f"{_DISCORD_API}/channels/{to}/messages",
                headers={"Authorization": f"Bot {token}"},
                data={"payload_json": json.dumps(payload)} if payload else None,
                files=files,
            )
        if not resp.is_success:
            raise RuntimeError(f"Discord attachment error {resp.status_code}: {resp.text[:200]}")
        return {"channel_id": to, "message_id": resp.json().get("id"), "status": "sent"}

    async def verify_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> bool:
        public_key = (cc.credentials or {}).get("public_key", "")
        signature = headers.get("X-Signature-Ed25519", "")
        timestamp = headers.get("X-Signature-Timestamp", "")
        if not (public_key and signature and timestamp):
            return False
        try:
            from nacl.signing import VerifyKey
            from nacl.exceptions import BadSignatureError
        except ImportError:
            logger.warning("PyNaCl not installed — Discord inbound verify disabled. "
                           "Run: pip install pynacl")
            return False
        try:
            VerifyKey(bytes.fromhex(public_key)).verify(
                (timestamp.encode() + body),
                bytes.fromhex(signature),
            )
            return True
        except BadSignatureError:
            return False
        except Exception:
            logger.exception("Discord signature verify failed")
            return False

    async def parse_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> Optional[NormalizedInbound]:
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            return None

        # type 1 = PING (handled at router level), 2 = APPLICATION_COMMAND,
        # 3 = MESSAGE_COMPONENT, 5 = MODAL_SUBMIT
        itype = payload.get("type")
        if itype == 1 or itype is None:
            return None

        # Pull the textual payload from whichever interaction shape this is
        data = payload.get("data") or {}
        user = (payload.get("member") or {}).get("user") or payload.get("user") or {}
        user_id = str(user.get("id", ""))
        channel_id = str(payload.get("channel_id", ""))
        if not user_id:
            return None

        # Slash-command: join option values into a text line
        if itype == 2:
            name = data.get("name", "")
            options = data.get("options") or []
            arg_text = " ".join(
                str(o.get("value", "")) for o in options if o.get("value") is not None
            )
            content = f"/{name} {arg_text}".strip()
        elif itype == 3:
            content = str(data.get("custom_id", ""))
        elif itype == 5:
            components = data.get("components") or []
            content = " ".join(
                str((c.get("components") or [{}])[0].get("value", ""))
                for c in components
            )
        else:
            content = ""

        return NormalizedInbound(
            channel_type="discord",
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=user_id,
            sender_name=user.get("global_name") or user.get("username"),
            sender_username=user.get("username"),
            reply_to=channel_id or user_id,
            content=content,
            message_type="text",
            external_message_id=payload.get("id"),
            raw=payload,
        )


register_adapter(DiscordChannelAdapter())
