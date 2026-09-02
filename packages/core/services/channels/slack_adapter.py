"""Slack channel adapter — Events API inbound + chat.postMessage outbound.

OAuth-backed ChannelConfigs lease the owner's Slack access token from the
linked OAuthAccount. The Slack signing secret remains deployment App
configuration, because it authenticates webhook delivery for that App.

Signing: Slack signs every inbound request with the signing_secret as
``v0=<sha256 hmac of 'v0:' + ts + ':' + body>``. The signature is in the
``X-Slack-Signature`` header and must match within ~5 minutes of
``X-Slack-Request-Timestamp`` to prevent replays.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, Optional

from packages.core.models.channel import ChannelConfig
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

_SLACK_API = "https://slack.com/api"
_MAX_TS_SKEW = 60 * 5


def verify_slack_request(*, headers: dict, body: bytes) -> bool:
    """Verify a Slack request before its payload is trusted or routed."""
    secret = os.getenv("SLACK_SIGNING_SECRET", "").strip()
    if not secret:
        return False
    normalized_headers = {str(key).lower(): value for key, value in headers.items()}
    ts = normalized_headers.get("x-slack-request-timestamp", "")
    sig = normalized_headers.get("x-slack-signature", "")
    if not (ts and sig):
        return False
    try:
        if abs(time.time() - int(ts)) > _MAX_TS_SKEW:
            return False
    except ValueError:
        return False
    base = f"v0:{ts}:".encode() + body
    mac = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={mac}", sig)


class SlackChannelAdapter(ChannelAdapter):
    channel_type = "slack"
    text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

    def webhook_path(self, cc: ChannelConfig) -> str:
        return "/api/v1/channels/slack/events"

    async def send_text(
        self, cc: ChannelConfig, to: str, text: str, **kwargs: Any,
    ) -> Dict[str, Any]:
        if httpx is None:
            raise ChannelTextSendError.determinate(
                "httpx is required — pip install httpx"
            )
        credentials = await self.credentials(cc, reason="channel.slack.send_text")
        token = credentials.get("bot_token") or credentials.get("access_token")
        if not token:
            raise ChannelTextSendError.determinate(
                "Slack credential source has no bot token"
            )
        thread_ts = kwargs.get("thread_ts")
        payload: Dict[str, Any] = {"channel": to, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        idempotency_key = str(kwargs.get("idempotency_key") or "").strip()
        if idempotency_key:
            payload["client_msg_id"] = idempotency_key
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_SLACK_API}/chat.postMessage",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json; charset=utf-8"},
                json=payload,
            )
        data = resp.json()
        if not resp.is_success:
            raise ChannelTextSendError.from_http_status(
                f"Slack API error {resp.status_code}: {data.get('error')}",
                status_code=resp.status_code,
            )
        if not data.get("ok"):
            raise ChannelTextSendError.determinate(
                f"Slack API error: {data.get('error')}"
            )
        return {
            "channel": to,
            "ts": data.get("ts"),
            "external_id": data.get("ts"),
            "status": "sent",
        }

    async def send_attachment(
        self, cc: ChannelConfig, to: str, *, url=None, data=None,
        mime_type=None, caption=None, kind="document",
    ) -> Dict[str, Any]:
        """Upload via files.upload_v2 — Slack's modern file upload.
        Fetches the URL server-side, posts the bytes to Slack, shares
        the resulting file in the target channel."""
        if httpx is None:
            raise RuntimeError("httpx is required — pip install httpx")
        if not url and not data:
            raise RuntimeError("send_attachment needs url or data")
        credentials = await self.credentials(cc, reason="channel.slack.send_attachment")
        token = credentials.get("bot_token") or credentials.get("access_token")
        if not token:
            raise RuntimeError("Slack credential source has no bot token")

        # Pull the file bytes if only url given
        async with httpx.AsyncClient(timeout=30) as client:
            if data is None:
                fr = await client.get(url)  # type: ignore[arg-type]
                fr.raise_for_status()
                data = fr.content
                mime_type = mime_type or fr.headers.get(
                    "Content-Type", "application/octet-stream",
                )

            # 1. getUploadURLExternal → pre-signed URL + file_id
            step1 = await client.get(
                f"{_SLACK_API}/files.getUploadURLExternal",
                headers={"Authorization": f"Bearer {token}"},
                params={"filename": "attachment", "length": str(len(data))},
            )
            j1 = step1.json()
            if not j1.get("ok"):
                raise RuntimeError(f"Slack getUploadURLExternal: {j1.get('error')}")
            upload_url = j1["upload_url"]
            file_id = j1["file_id"]

            # 2. PUT the bytes
            up = await client.post(upload_url, content=data)
            if not up.is_success:
                raise RuntimeError(f"Slack file upload HTTP {up.status_code}")

            # 3. completeUploadExternal — shares into the channel
            step3 = await client.post(
                f"{_SLACK_API}/files.completeUploadExternal",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json; charset=utf-8"},
                json={
                    "files": [{"id": file_id, "title": caption or "attachment"}],
                    "channel_id": to,
                    "initial_comment": caption or "",
                },
            )
            j3 = step3.json()
        if not j3.get("ok"):
            raise RuntimeError(f"Slack completeUpload: {j3.get('error')}")
        return {"file_id": file_id, "channel": to, "status": "sent"}

    async def verify_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> bool:
        # Incoming Events API requests are signed by the deployment's Slack
        # App, rather than by a user-owned OAuth token.
        return verify_slack_request(headers=headers, body=body)

    async def parse_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> Optional[NormalizedInbound]:
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            return None

        # Slack challenges are handled at the router level, but tolerate
        # a challenge body here by returning None.
        if payload.get("type") == "url_verification":
            return None

        event = payload.get("event") or {}
        event_type = event.get("type")
        if event_type not in ("message", "app_mention"):
            return None
        # The product contract is explicit mentions in channels plus direct
        # messages. Ordinary channel traffic must never trigger an Agent.
        if event_type == "message" and event.get("channel_type") != "im":
            return None
        # Skip messages the bot itself sent
        if event.get("subtype"):
            return None
        if event.get("bot_id"):
            return None

        user = event.get("user", "")
        channel = event.get("channel", "")
        if not (user and channel):
            return None

        thread_ts = event.get("thread_ts")
        if event_type == "app_mention" and not thread_ts:
            thread_ts = event.get("ts")

        return NormalizedInbound(
            channel_type="slack",
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=user,
            reply_to=channel,
            content=event.get("text", "") or "",
            message_type="text",
            external_message_id=event.get("ts"),
            thread_ts=thread_ts,
            raw=payload,
        )


register_adapter(SlackChannelAdapter())
