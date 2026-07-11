"""Telegram Bot API adapter.

Handles:
- Setting and removing webhooks
- Receiving and parsing incoming updates (messages, callback queries, edits)
- Sending text, photo, document messages
- Inline keyboard buttons and callback query answers
- Bot info verification

Configuration:
  Credentials are stored in ChannelConfig.credentials:
    bot_token  — Telegram Bot API token (from @BotFather)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager as _asynccontextmanager
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Optional dependency — fail gracefully
try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramAdapter:
    """Adapter for Telegram Bot API.

    Uses httpx for async HTTP calls.
    All methods return normalised dicts compatible with channel_service.
    """

    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.base_url = f"{TELEGRAM_API_BASE}/bot{bot_token}"

    # ------------------------------------------------------------------
    # Webhook management
    # ------------------------------------------------------------------

    async def set_webhook(
        self,
        url: str,
        secret_token: str | None = None,
    ) -> bool:
        """Set webhook URL for receiving updates.

        Args:
            url: HTTPS URL where Telegram will send updates.
            secret_token: Optional secret sent in X-Telegram-Bot-Api-Secret-Token header.

        Returns True on success.
        """
        params: dict[str, Any] = {"url": url}
        if secret_token:
            params["secret_token"] = secret_token

        data = await self._request("setWebhook", params)
        return data.get("result", False) is True

    async def delete_webhook(self) -> bool:
        """Remove the current webhook."""
        data = await self._request("deleteWebhook")
        return data.get("result", False) is True

    # ------------------------------------------------------------------
    # Inbound update handling
    # ------------------------------------------------------------------

    async def handle_update(self, update: dict[str, Any]) -> dict[str, Any] | None:
        """Parse incoming update into normalised message.

        Handles:
            - message (text, photo, document, voice, video, location, contact)
            - callback_query (inline button clicks)
            - edited_message

        Returns normalised dict or None if update type is not supported:
            {
                "sender_id": str,
                "message_type": str,
                "content": str,
                "msg_id": str | None,
                "chat_id": int,
                "raw": dict,
                "channel": "telegram",
            }
        """
        # Callback query (inline button click)
        callback_query = update.get("callback_query")
        if callback_query:
            sender = callback_query.get("from", {})
            message = callback_query.get("message", {})
            return {
                "sender_id": str(sender.get("id", "")),
                "sender_name": self._format_name(sender),
                "message_type": "callback_query",
                "content": callback_query.get("data", ""),
                "msg_id": callback_query.get("id"),
                "chat_id": message.get("chat", {}).get("id"),
                "raw": update,
                "channel": "telegram",
            }

        # Message or edited_message
        message = update.get("message") or update.get("edited_message")
        if not message:
            logger.debug("Unsupported Telegram update type: %s", list(update.keys()))
            return None

        is_edit = "edited_message" in update
        sender = message.get("from", {})
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        msg_id = str(message.get("message_id", ""))
        content = ""
        msg_type = "text"

        # Determine message type and extract content
        if message.get("text"):
            msg_type = "text"
            content = message["text"]

        elif message.get("photo"):
            msg_type = "photo"
            # Telegram sends multiple sizes; use the largest
            photos = message["photo"]
            largest = photos[-1] if photos else {}
            caption = message.get("caption", "")
            content = caption if caption else f"[Photo: {largest.get('file_id', '')}]"

        elif message.get("document"):
            msg_type = "document"
            doc = message["document"]
            caption = message.get("caption", "")
            content = caption if caption else f"[Document: {doc.get('file_name', doc.get('file_id', ''))}]"

        elif message.get("voice"):
            msg_type = "voice"
            voice = message["voice"]
            content = f"[Voice: {voice.get('duration', 0)}s]"

        elif message.get("video"):
            msg_type = "video"
            caption = message.get("caption", "")
            content = caption if caption else "[Video]"

        elif message.get("location"):
            msg_type = "location"
            loc = message["location"]
            content = f"Location: ({loc.get('latitude', '')},{loc.get('longitude', '')})"

        elif message.get("contact"):
            msg_type = "contact"
            contact = message["contact"]
            content = f"Contact: {contact.get('first_name', '')} {contact.get('last_name', '')} {contact.get('phone_number', '')}".strip()

        elif message.get("sticker"):
            msg_type = "sticker"
            sticker = message["sticker"]
            content = f"[Sticker: {sticker.get('emoji', '')}]"

        elif message.get("animation"):
            msg_type = "animation"
            content = message.get("caption", "[GIF]")

        else:
            msg_type = "unknown"
            content = "[Unsupported message type]"

        if is_edit:
            msg_type = f"edited_{msg_type}"

        return {
            "sender_id": str(sender.get("id", "")),
            "sender_name": self._format_name(sender),
            "message_type": msg_type,
            "content": content,
            "msg_id": msg_id,
            "chat_id": chat_id,
            "raw": update,
            "channel": "telegram",
        }

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
        reply_to_message_id: int | str | None = None,
    ) -> dict[str, Any]:
        """Send text message.

        Args:
            chat_id: Target chat ID or @channel_username.
            text: Message text (HTML or Markdown supported).
            parse_mode: "HTML" or "MarkdownV2".
            disable_notification: Send silently (no notification sound).
            reply_to_message_id: Reply to a specific message in the chat.

        Returns normalised result dict.
        """
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if disable_notification:
            params["disable_notification"] = True
        if reply_to_message_id is not None:
            params["reply_to_message_id"] = reply_to_message_id
        data = await self._request("sendMessage", params)
        result = data.get("result", {})
        return {
            "external_id": str(result.get("message_id", "")),
            "chat_id": result.get("chat", {}).get("id"),
            "status": "sent",
            "raw": data,
        }

    async def send_photo(
        self,
        chat_id: int | str,
        photo_url: str,
        caption: str = "",
    ) -> dict[str, Any]:
        """Send photo by URL.

        Args:
            chat_id: Target chat ID.
            photo_url: Public URL of the photo.
            caption: Optional caption text.
        """
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "photo": photo_url,
        }
        if caption:
            params["caption"] = caption
            params["parse_mode"] = "HTML"

        data = await self._request("sendPhoto", params)
        result = data.get("result", {})
        return {
            "external_id": str(result.get("message_id", "")),
            "chat_id": result.get("chat", {}).get("id"),
            "status": "sent",
            "raw": data,
        }

    async def send_document(
        self,
        chat_id: int | str,
        document_url: str,
        caption: str = "",
    ) -> dict[str, Any]:
        """Send document by URL.

        Args:
            chat_id: Target chat ID.
            document_url: Public URL of the document.
            caption: Optional caption text.
        """
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "document": document_url,
        }
        if caption:
            params["caption"] = caption
            params["parse_mode"] = "HTML"

        data = await self._request("sendDocument", params)
        result = data.get("result", {})
        return {
            "external_id": str(result.get("message_id", "")),
            "chat_id": result.get("chat", {}).get("id"),
            "status": "sent",
            "raw": data,
        }

    async def send_inline_keyboard(
        self,
        chat_id: int | str,
        text: str,
        buttons: list[list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Send message with inline keyboard buttons.

        Args:
            chat_id: Target chat ID.
            text: Message text.
            buttons: 2D array of button objects.
                Each button: {"text": "Label", "callback_data": "value"}
                or {"text": "Label", "url": "https://..."}

        Example:
            buttons = [
                [{"text": "Option A", "callback_data": "a"}, {"text": "Option B", "callback_data": "b"}],
                [{"text": "Visit site", "url": "https://manor.ai"}],
            ]
        """
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": buttons,
            },
        }
        data = await self._request("sendMessage", params)
        result = data.get("result", {})
        return {
            "external_id": str(result.get("message_id", "")),
            "chat_id": result.get("chat", {}).get("id"),
            "status": "sent",
            "raw": data,
        }

    # ------------------------------------------------------------------
    # Callback query
    # ------------------------------------------------------------------

    async def answer_callback(
        self,
        callback_query_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> bool:
        """Answer callback query from inline button.

        Must be called to stop the loading indicator on the button.

        Args:
            callback_query_id: ID from the callback_query update.
            text: Optional notification text shown to user.
            show_alert: Show the text as a modal alert instead of a toast.
        """
        params: dict[str, Any] = {
            "callback_query_id": callback_query_id,
        }
        if text:
            params["text"] = text
        if show_alert:
            params["show_alert"] = True

        data = await self._request("answerCallbackQuery", params)
        return data.get("result", False) is True

    # ------------------------------------------------------------------
    # Bot info
    # ------------------------------------------------------------------

    async def get_me(self) -> dict[str, Any]:
        """Get bot info for verification.

        Returns dict with id, is_bot, first_name, username, etc.
        """
        data = await self._request("getMe")
        return data.get("result", {})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a request to the Telegram Bot API.

        Args:
            method: API method name (e.g. "sendMessage").
            params: Request parameters (sent as JSON body).

        Returns the full API response dict.
        Raises RuntimeError on HTTP or API errors.
        """
        if httpx is None:
            raise RuntimeError("httpx is not installed. Run: pip install httpx")

        url = f"{self.base_url}/{method}"

        async with httpx.AsyncClient(timeout=30) as client:
            if params:
                resp = await client.post(url, json=params)
            else:
                resp = await client.get(url)
            data = resp.json()

        if resp.status_code >= 400 or not data.get("ok", False):
            error_desc = data.get("description", resp.text)
            error_code = data.get("error_code", resp.status_code)
            logger.error("Telegram API error: method=%s code=%s desc=%s", method, error_code, error_desc)
            raise RuntimeError(f"Telegram API error {error_code}: {error_desc}")

        return data

    @staticmethod
    def _format_name(user: dict[str, Any]) -> str:
        """Format a Telegram user's display name."""
        first = user.get("first_name", "")
        last = user.get("last_name", "")
        return f"{first} {last}".strip() or user.get("username", "")


# ── Polymorphic ChannelAdapter wrapper ──────────────────────────────────────

import hashlib as _hashlib
import json as _json

from packages.core.config import get_settings as _get_settings
from packages.core.services.channels.base import (
    ChannelAdapter,
    NormalizedInbound,
    register_adapter,
)


class TelegramChannelAdapter(ChannelAdapter):
    """Bridges ``TelegramAdapter`` into the polymorphic ``ChannelAdapter``
    contract used by channel_gateway. Credentials come from the
    ChannelConfig at call time so one adapter instance serves every
    bot configured on the deployment.
    """

    channel_type = "telegram"

    def _build(self, cc: ChannelConfig) -> TelegramAdapter:
        creds = cc.credentials or {}
        token = creds.get("bot_token")
        if not token:
            raise RuntimeError("Telegram ChannelConfig missing bot_token")
        return TelegramAdapter(bot_token=token)

    def _hash(self, bot_token: str) -> str:
        return _hashlib.sha256(bot_token.encode("utf-8")).hexdigest()

    def webhook_path(self, cc: ChannelConfig) -> str:
        token = (cc.credentials or {}).get("bot_token", "")
        return f"/api/v1/channels/telegram/webhook/{self._hash(token)}?config_id={cc.id}"

    async def register_webhook(self, cc: ChannelConfig) -> dict[str, Any]:
        base = _get_settings().PUBLIC_BASE_URL.rstrip("/")
        # Telegram's setWebhook REQUIRES https — skip gracefully for
        # local-dev HTTP setups rather than hammering the Bot API with a
        # guaranteed-to-fail request.
        if not base.startswith("https://"):
            logger.info(
                "Skipping Telegram setWebhook — PUBLIC_BASE_URL=%s is not HTTPS. "
                "Use an ngrok/cloudflare-tunnel URL, or set the webhook manually "
                "from Telegram's Bot API once the deployment is reachable.",
                base,
            )
            return {
                "registered": False,
                "reason": "public_base_url_not_https",
                "detail": f"PUBLIC_BASE_URL ({base}) must start with https:// for Telegram. "
                           "Run through an HTTPS tunnel or set the webhook manually.",
            }

        url = f"{base}{self.webhook_path(cc)}"
        secret = (cc.credentials or {}).get("secret_token") or None
        adapter = self._build(cc)
        try:
            ok = await adapter.set_webhook(url, secret_token=secret)
        except RuntimeError as e:
            # Already logged by the adapter; surface a calm result so
            # the integration save doesn't look like a server crash.
            return {"registered": False, "reason": "telegram_api_error", "detail": str(e)}
        return {"registered": bool(ok), "url": url}

    async def unregister_webhook(self, cc: ChannelConfig) -> dict[str, Any]:
        adapter = self._build(cc)
        ok = await adapter.delete_webhook()
        return {"unregistered": bool(ok)}

    async def verify_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> bool:
        # The URL hash is checked at the router level; additional header
        # secret is optional.
        secret = (cc.credentials or {}).get("secret_token") or ""
        if not secret:
            return True
        return headers.get("X-Telegram-Bot-Api-Secret-Token", "") == secret

    async def parse_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> Optional[NormalizedInbound]:
        try:
            update = _json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            return None
        parsed = await self._build(cc).handle_update(update)
        if not parsed:
            return None
        return NormalizedInbound(
            channel_type="telegram",
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=str(parsed.get("sender_id", "")),
            sender_name=parsed.get("sender_name"),
            reply_to=str(parsed.get("chat_id") or parsed.get("sender_id", "")),
            content=parsed.get("content", "") or "",
            message_type=parsed.get("message_type", "text"),
            external_message_id=parsed.get("msg_id"),
            raw=parsed.get("raw") or update,
        )

    async def send_text(
        self, cc: ChannelConfig, to: str, text: str, **kwargs: Any,
    ) -> dict[str, Any]:
        adapter = self._build(cc)
        # Chunk at 4096 chars to stay under Telegram's limit
        for chunk in _chunk_text(text, 4096):
            last = await adapter.send_message(to, chunk)
        return last  # type: ignore[return-value]

    async def send_actionable_message(
        self, cc: ChannelConfig, to: str, text: str, *, actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Inline keyboard with one row per (up to ~8) actions.

        ``callback_data`` carries the action ``key`` verbatim — exactly
        what ``notification_callbacks.match_action`` expects when the
        inbound webhook routes the click back into ``dispatch_inbound``
        as ``content = callback_data``. Telegram caps callback_data at
        64 bytes; we truncate generously since action keys are short.
        """
        rows: list[list[dict[str, Any]]] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            key = action.get("key")
            label = action.get("label") or key
            if not isinstance(key, str) or not key:
                continue
            rows.append([{
                "text": str(label),
                "callback_data": key[:64],
            }])
        if not rows:
            # Nothing actionable — degrade to plain text so we still
            # ship the body.
            return await self.send_text(cc, to, text)

        adapter = self._build(cc)
        # Telegram message + keyboard ships in one call; we use the
        # already-implemented helper rather than rebuilding the request.
        return await adapter.send_inline_keyboard(to, text, rows)

    # ── Typing indicator ─────────────────────────────────────────────────

    # Match the legacy 4 s cadence — Telegram's typing state auto-expires
    # ~5 s after the last sendChatAction so we refresh slightly ahead.
    _TYPING_INTERVAL = 4.0

    async def _send_typing(self, cc: ChannelConfig, chat_id: str) -> None:
        import httpx as _httpx
        token = (cc.credentials or {}).get("bot_token", "")
        if not token:
            return
        url = f"{TELEGRAM_API_BASE}/bot{token}/sendChatAction"
        try:
            async with _httpx.AsyncClient(timeout=5) as c:
                await c.post(url, json={"chat_id": chat_id, "action": "typing"})
        except Exception:
            # Best-effort — matches the legacy Java swallow-everything pattern.
            pass

    @_asynccontextmanager
    async def typing_indicator(self, cc: ChannelConfig, to: str):
        import asyncio
        stop = asyncio.Event()

        async def _loop() -> None:
            # Fire once immediately so the user sees "typing…" within
            # milliseconds of sending their message.
            await self._send_typing(cc, to)
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._TYPING_INTERVAL)
                    return  # stop was set
                except asyncio.TimeoutError:
                    await self._send_typing(cc, to)

        task = asyncio.create_task(_loop())
        try:
            yield
        finally:
            stop.set()
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except asyncio.TimeoutError:
                task.cancel()
            except Exception:
                pass

    async def send_attachment(
        self, cc: ChannelConfig, to: str, *, url=None, data=None,
        mime_type=None, caption=None, kind="document",
    ) -> dict[str, Any]:
        if not url:
            raise NotImplementedError(
                "TelegramChannelAdapter only supports URL-based attachments for now"
            )
        adapter = self._build(cc)
        if kind == "image":
            return await adapter.send_photo(to, url, caption=caption or "")
        return await adapter.send_document(to, url, caption=caption or "")


def _chunk_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for paragraph in text.split("\n\n"):
        candidate = paragraph if not buf else buf + "\n\n" + paragraph
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            out.append(buf); buf = ""
        while len(paragraph) > limit:
            out.append(paragraph[:limit]); paragraph = paragraph[limit:]
        buf = paragraph
    if buf:
        out.append(buf)
    return out


register_adapter(TelegramChannelAdapter())
