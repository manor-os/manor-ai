"""Slack channel adapter — Events API inbound + chat.postMessage outbound.

Credentials in ChannelConfig.credentials:
    {
      "bot_token":      "xoxb-…"            (required — chat.write scope)
      "signing_secret": "…"                  (required for inbound verify)
      "app_id":         "A…"                 (optional)
    }

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
    ChannelAdapter, NormalizedInbound, register_adapter,
)

logger = logging.getLogger(__name__)

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

_SLACK_API = "https://slack.com/api"
_MAX_TS_SKEW = 60 * 5


class SlackChannelAdapter(ChannelAdapter):
    channel_type = "slack"

    async def send_text(
        self, cc: ChannelConfig, to: str, text: str, **kwargs: Any,
    ) -> Dict[str, Any]:
        if httpx is None:
            raise RuntimeError("httpx is required — pip install httpx")
        # Prefer the workspace-scoped token captured during OAuth; fall
        # back to the deployment-level env token for dev setups that
        # haven't completed the OAuth flow yet.
        token = (cc.credentials or {}).get("bot_token", "") \
            or os.getenv("SLACK_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("Slack bot_token not configured (env or ChannelConfig)")
        thread_ts = kwargs.get("thread_ts")
        payload: Dict[str, Any] = {"channel": to, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_SLACK_API}/chat.postMessage",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json; charset=utf-8"},
                json=payload,
            )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack API error: {data.get('error')}")
        return {"channel": to, "ts": data.get("ts"), "status": "sent"}

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
        token = (cc.credentials or {}).get("bot_token", "") \
            or os.getenv("SLACK_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("Slack bot_token not configured")

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
        # Deployment-level signing secret (one Slack app per deployment)
        # takes precedence; per-ChannelConfig override only for multi-app
        # OSS deployments.
        secret = (
            os.getenv("SLACK_SIGNING_SECRET", "").strip()
            or (cc.credentials or {}).get("signing_secret", "")
        )
        if not secret:
            return False
        ts = headers.get("X-Slack-Request-Timestamp", "")
        sig = headers.get("X-Slack-Signature", "")
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
        if event.get("type") not in ("message", "app_mention"):
            return None
        # Skip messages the bot itself sent
        if event.get("subtype") in ("bot_message", "channel_join"):
            return None
        if event.get("bot_id"):
            return None

        user = event.get("user", "")
        channel = event.get("channel", "")
        if not (user and channel):
            return None

        return NormalizedInbound(
            channel_type="slack",
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=user,
            reply_to=channel,
            content=event.get("text", "") or "",
            message_type="text",
            external_message_id=event.get("ts"),
            raw=payload,
        )


register_adapter(SlackChannelAdapter())
