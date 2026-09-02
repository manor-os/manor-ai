"""Discord channel adapter — Interactions webhook in, Bot API out.

Two inbound modes exist for Discord; we implement the HTTPS Interactions
endpoint (simpler, no gateway socket needed):

  1. User sends a slash command or message component.
  2. Discord POSTs a signed Interaction payload to our callback URL.
  3. Adapter verifies the Ed25519 signature using the app's public key.
  4. Gateway runs the agent, and reply is POSTed via the Bot API.

The Discord App identity is deployment-owned. User-owned ChannelConfigs carry
only the installed Guild identity and an OAuthAccount reference.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from packages.core.database import async_session
from packages.core.models.channel import ChannelConfig
from packages.core.services.discord_app_config import (
    DiscordAppConfig,
    resolve_discord_app_config,
)
from packages.core.services.channels.base import (
    ChannelAdapter,
    ChannelTextSendError,
    ChannelTextSendRetryMode,
    NormalizedInbound,
    register_adapter,
)

logger = logging.getLogger(__name__)

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

_DISCORD_API = "https://discord.com/api/v10"
_DISCORD_NONCE_MAX_LENGTH = 25


def _discord_nonce(idempotency_key: str) -> str:
    """Keep Discord's retry nonce within its 25-character limit."""
    if len(idempotency_key) <= _DISCORD_NONCE_MAX_LENGTH:
        return idempotency_key
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:_DISCORD_NONCE_MAX_LENGTH]


async def _deployment_bot_token() -> str:
    async with async_session() as db:
        app = await resolve_discord_app_config(db)
    if app is None:
        raise RuntimeError("Discord App runtime configuration is incomplete")
    return app.bot_token


async def register_discord_guild_command(
    app: DiscordAppConfig,
    guild_id: str,
) -> None:
    """Bulk-overwrite the one V1 command for a connected Guild."""
    command = {
        "name": "manor",
        "description": "Send a message to your bound Manor Agent",
        "type": 1,
        "options": [{
            "name": "message",
            "description": "Message for Manor",
            "type": 3,
            "required": True,
        }],
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.put(
            f"{_DISCORD_API}/applications/{app.application_id}/guilds/"
            f"{guild_id}/commands",
            headers={
                "Authorization": f"Bot {app.bot_token}",
                "Content-Type": "application/json",
            },
            json=[command],
        )
    if not response.is_success:
        raise RuntimeError(
            f"Discord command registration failed with HTTP {response.status_code}"
        )


async def leave_discord_guild(
    app: DiscordAppConfig,
    guild_id: str,
) -> None:
    """Remove the deployment Bot from a disconnected Guild."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.delete(
            f"{_DISCORD_API}/users/@me/guilds/{guild_id}",
            headers={"Authorization": f"Bot {app.bot_token}"},
        )
    if not response.is_success:
        raise RuntimeError(
            f"Discord Guild leave failed with HTTP {response.status_code}"
        )


def verify_discord_signature(
    *,
    public_key: str,
    signature: str,
    timestamp: str,
    body: bytes,
) -> bool:
    """Verify a Discord Interaction signature against the raw body."""
    if not all((public_key, signature, timestamp)):
        return False
    try:
        VerifyKey(bytes.fromhex(public_key)).verify(
            timestamp.encode() + body,
            bytes.fromhex(signature),
        )
    except (ValueError, BadSignatureError):
        return False
    return True


class DiscordChannelAdapter(ChannelAdapter):
    channel_type = "discord"
    text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

    async def send_text(
        self, cc: ChannelConfig, to: str, text: str, **kwargs: Any,
    ) -> Dict[str, Any]:
        if httpx is None:
            raise ChannelTextSendError.determinate(
                "httpx is required — pip install httpx"
            )
        reply_context = kwargs.get("reply_context") or {}
        application_id = str(reply_context.get("application_id") or "").strip()
        interaction_token = str(
            reply_context.get("interaction_token") or ""
        ).strip()
        idempotency_key = str(kwargs.get("idempotency_key") or "").strip()
        async with httpx.AsyncClient(timeout=15) as client:
            if application_id and interaction_token:
                resp = await client.patch(
                    f"{_DISCORD_API}/webhooks/{application_id}/"
                    f"{interaction_token}/messages/@original",
                    json={"content": text},
                )
            else:
                try:
                    token = await _deployment_bot_token()
                except RuntimeError as exc:
                    raise ChannelTextSendError.determinate(str(exc)) from exc
                payload: dict[str, Any] = {"content": text}
                if idempotency_key:
                    payload.update({
                        "nonce": _discord_nonce(idempotency_key),
                        "enforce_nonce": True,
                    })
                resp = await client.post(
                    f"{_DISCORD_API}/channels/{to}/messages",
                    headers={
                        "Authorization": f"Bot {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        if not resp.is_success:
            raise ChannelTextSendError.from_http_status(
                f"Discord API error {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        return {"channel_id": to, "message_id": resp.json().get("id"), "status": "sent"}

    async def send_attachment(
        self, cc: ChannelConfig, to: str, *, url=None, data=None,
        mime_type=None, caption=None, kind="document",
    ) -> Dict[str, Any]:
        """Post a message with a file attachment. Discord's API takes
        ``multipart/form-data`` with the file bytes + a JSON payload."""
        if httpx is None:
            raise RuntimeError("httpx is required — pip install httpx")
        token = await _deployment_bot_token()
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
        credentials = await self.credentials(
            cc, reason="channel.discord.verify_inbound",
        )
        public_key = credentials.get("public_key", "")
        signature = headers.get("X-Signature-Ed25519", "")
        timestamp = headers.get("X-Signature-Timestamp", "")
        if not (public_key and signature and timestamp):
            return False
        return verify_discord_signature(
            public_key=public_key,
            signature=signature,
            timestamp=timestamp,
            body=body,
        )

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
