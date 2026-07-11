"""WeChat Official Account / Work WeChat channel adapter.

Handles:
- Receiving messages from WeChat webhook callbacks
- Sending replies back via WeChat API
- Token management (access_token refresh)
- Message type handling (text, image, voice, video, location, link)

Configuration:
  Credentials are stored in ChannelConfig.credentials:
    app_id       — WeChat Official Account AppID
    app_secret   — WeChat Official Account AppSecret
    token        — Callback verification token
    encoding_aes_key — (optional) AES key for encrypted message mode
"""
from __future__ import annotations

import hashlib
import logging
import time
import xml.etree.ElementTree as ET
from typing import Any

logger = logging.getLogger(__name__)

# Optional dependency — fail gracefully
try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WECHAT_API_BASE = "https://api.weixin.qq.com/cgi-bin"
TOKEN_EXPIRY_BUFFER = 300  # refresh 5 min before actual expiry


class WeChatAdapter:
    """Adapter for WeChat Official Account API.

    Supports plain-text message mode. For encrypted mode (encoding_aes_key),
    messages must be decrypted before parsing — not yet implemented.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        token: str,
        encoding_aes_key: str | None = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.token = token
        self.encoding_aes_key = encoding_aes_key

        # Token cache
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    # Callback verification (GET request from WeChat servers)
    # ------------------------------------------------------------------

    async def verify_callback(
        self,
        signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
    ) -> str:
        """Verify WeChat server callback (GET request).

        WeChat sends signature = SHA1(sort([token, timestamp, nonce])).
        If it matches, we return echostr to confirm the endpoint is valid.

        Raises ValueError if signature does not match.
        """
        expected = self._sign(self.token, timestamp, nonce)
        if expected != signature:
            raise ValueError("WeChat callback signature mismatch")
        return echostr

    # ------------------------------------------------------------------
    # Inbound message handling (POST request from WeChat servers)
    # ------------------------------------------------------------------

    async def handle_message(self, xml_body: str) -> dict[str, Any]:
        """Parse incoming WeChat XML message into a normalised dict.

        Returns:
            {
                "sender_id": str,       # FromUserName (OpenID)
                "recipient_id": str,     # ToUserName (OA account)
                "message_type": str,     # text | image | voice | video | location | link | event
                "content": str,          # Text content or media description
                "msg_id": str | None,    # WeChat MsgId
                "media_id": str | None,  # For image/voice/video
                "raw": dict,             # All parsed XML fields
            }
        """
        root = ET.fromstring(xml_body)

        raw: dict[str, str] = {}
        for child in root:
            raw[child.tag] = (child.text or "").strip()

        msg_type = raw.get("MsgType", "text")
        content = ""

        if msg_type == "text":
            content = raw.get("Content", "")
        elif msg_type == "image":
            content = raw.get("PicUrl", "")
        elif msg_type == "voice":
            content = raw.get("Recognition", "")  # speech-to-text if enabled
        elif msg_type == "video" or msg_type == "shortvideo":
            content = f"[Video: {raw.get('MediaId', '')}]"
        elif msg_type == "location":
            content = f"Location: ({raw.get('Location_X', '')},{raw.get('Location_Y', '')}) {raw.get('Label', '')}"
        elif msg_type == "link":
            content = f"{raw.get('Title', '')} — {raw.get('Url', '')}"
        elif msg_type == "event":
            content = f"Event: {raw.get('Event', '')} {raw.get('EventKey', '')}".strip()

        return {
            "sender_id": raw.get("FromUserName", ""),
            "recipient_id": raw.get("ToUserName", ""),
            "message_type": msg_type,
            "content": content,
            "msg_id": raw.get("MsgId"),
            "media_id": raw.get("MediaId"),
            "raw": raw,
        }

    # ------------------------------------------------------------------
    # Outbound messaging (Customer Service API)
    # ------------------------------------------------------------------

    async def send_text(self, openid: str, content: str) -> bool:
        """Send text message to user via Customer Service Message API.

        Returns True on success, raises on failure.
        """
        if httpx is None:
            raise RuntimeError("httpx is not installed. Run: pip install httpx")

        token = await self.get_access_token()
        url = f"{WECHAT_API_BASE}/message/custom/send?access_token={token}"
        payload = {
            "touser": openid,
            "msgtype": "text",
            "text": {"content": content},
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()

        errcode = data.get("errcode", 0)
        if errcode != 0:
            errmsg = data.get("errmsg", "unknown error")
            logger.error("WeChat send_text failed: errcode=%s errmsg=%s", errcode, errmsg)
            # Token may have expired mid-request — invalidate cache
            if errcode in (40001, 40014, 42001):
                self._invalidate_token_cache()
            raise RuntimeError(f"WeChat API error {errcode}: {errmsg}")

        return True

    async def send_image(self, openid: str, media_id: str) -> bool:
        """Send image message to user via Customer Service Message API.

        The media_id must be obtained by uploading media via WeChat Media API first.
        """
        if httpx is None:
            raise RuntimeError("httpx is not installed. Run: pip install httpx")

        token = await self.get_access_token()
        url = f"{WECHAT_API_BASE}/message/custom/send?access_token={token}"
        payload = {
            "touser": openid,
            "msgtype": "image",
            "image": {"media_id": media_id},
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()

        errcode = data.get("errcode", 0)
        if errcode != 0:
            errmsg = data.get("errmsg", "unknown error")
            logger.error("WeChat send_image failed: errcode=%s errmsg=%s", errcode, errmsg)
            if errcode in (40001, 40014, 42001):
                self._invalidate_token_cache()
            raise RuntimeError(f"WeChat API error {errcode}: {errmsg}")

        return True

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def get_access_token(self) -> str:
        """Get or refresh WeChat access token (cached for ~7200s).

        WeChat access_token expires after 7200s. We refresh TOKEN_EXPIRY_BUFFER
        seconds before actual expiry to avoid race conditions.
        """
        await self.refresh_token_if_needed()
        if not self._access_token:
            raise RuntimeError("Failed to obtain WeChat access token")
        return self._access_token

    async def refresh_token_if_needed(self) -> None:
        """Check token expiry and refresh if necessary.

        Token is cached in Redis keyed by app_id so every worker sees the
        same value. Falls back to per-instance memory if Redis is down.
        WeChat's server-side TTL is 7200s; we refresh TOKEN_EXPIRY_BUFFER
        seconds earlier to avoid races.
        """
        now = time.time()
        if self._access_token and now < self._token_expires_at:
            return

        # Redis first — amortises the cgi-bin/token call across workers
        from packages.core.cache import cache
        cache_key = f"wechat:oa:token:{self.app_id}"
        cached = await cache.get(cache_key)
        if cached and cached.get("expires_at", 0) > now:
            self._access_token = cached["access_token"]
            self._token_expires_at = cached["expires_at"]
            return

        if httpx is None:
            raise RuntimeError("httpx is not installed. Run: pip install httpx")

        url = (
            f"{WECHAT_API_BASE}/token"
            f"?grant_type=client_credential"
            f"&appid={self.app_id}"
            f"&secret={self.app_secret}"
        )

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            data = resp.json()

        if "access_token" not in data:
            errcode = data.get("errcode", "?")
            errmsg = data.get("errmsg", "unknown")
            logger.error("WeChat token refresh failed: %s %s", errcode, errmsg)
            raise RuntimeError(f"WeChat token refresh failed: {errcode} {errmsg}")

        self._access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 7200))
        self._token_expires_at = now + expires_in - TOKEN_EXPIRY_BUFFER

        # Share with siblings — TTL mirrors the expiry buffer
        await cache.set(
            cache_key,
            {"access_token": self._access_token,
             "expires_at": self._token_expires_at},
            ttl=max(60, expires_in - TOKEN_EXPIRY_BUFFER),
        )

        logger.info("WeChat access token refreshed, expires_in=%ss", expires_in)

    def _invalidate_token_cache(self) -> None:
        """Drop both memory and Redis token caches after a 40001/40014/42001."""
        import asyncio
        self._access_token = None
        self._token_expires_at = 0.0
        try:
            from packages.core.cache import cache
            asyncio.create_task(cache.delete(f"wechat:oa:token:{self.app_id}"))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sign(token: str, timestamp: str, nonce: str) -> str:
        """Generate SHA1 signature for WeChat callback verification."""
        parts = sorted([token, timestamp, nonce])
        raw = "".join(parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ── Polymorphic ChannelAdapter wrapper ──────────────────────────────────────

from typing import Optional as _Optional

from packages.core.models.channel import ChannelConfig as _CC
from packages.core.services.channels.base import (
    ChannelAdapter, NormalizedInbound, register_adapter,
)


class WeChatChannelAdapter(ChannelAdapter):
    """Polymorphic bridge for WeChat Official Account. Credentials are
    read from ChannelConfig at call time so a single adapter instance
    serves every OA configured on the deployment.
    """

    channel_type = "wechat"

    def _build(self, cc: _CC) -> WeChatAdapter:
        creds = cc.credentials or {}
        app_id = creds.get("app_id")
        app_secret = creds.get("app_secret")
        token = creds.get("token", "")
        if not (app_id and app_secret):
            raise RuntimeError("WeChat ChannelConfig missing app_id / app_secret")
        return WeChatAdapter(
            app_id=app_id, app_secret=app_secret, token=token,
            encoding_aes_key=creds.get("encoding_aes_key"),
        )

    def webhook_path(self, cc: _CC) -> str:
        # WeChat OA requires a fixed callback URL set manually in mp.weixin.qq.com
        return f"/api/v1/channels/wechat/callback?config_id={cc.id}"

    async def verify_inbound(self, cc: _CC, *, headers, query, body) -> bool:
        sig = query.get("signature", "")
        ts = query.get("timestamp", "")
        nonce = query.get("nonce", "")
        if not (sig and ts and nonce):
            return False
        token = (cc.credentials or {}).get("token", "")
        return WeChatAdapter._sign(token, ts, nonce) == sig

    async def parse_inbound(self, cc: _CC, *, headers, query, body) -> _Optional[NormalizedInbound]:
        if not body:
            return None
        try:
            parsed = await self._build(cc).handle_message(body.decode("utf-8"))
        except Exception:
            return None
        return NormalizedInbound(
            channel_type="wechat",
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=parsed.get("sender_id", ""),
            reply_to=parsed.get("sender_id", ""),
            content=parsed.get("content", "") or "",
            message_type=parsed.get("message_type", "text"),
            external_message_id=parsed.get("msg_id"),
            raw=parsed.get("raw") or {},
        )

    async def send_text(self, cc: _CC, to: str, text: str, **kwargs: Any) -> dict[str, Any]:
        adapter = self._build(cc)
        await adapter.send_text(to, text)
        return {"status": "sent"}


register_adapter(WeChatChannelAdapter())
