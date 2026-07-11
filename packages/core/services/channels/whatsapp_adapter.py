"""WhatsApp Cloud API adapter (Meta Business Platform).

Handles:
- Webhook verification (GET challenge-response)
- Receiving inbound messages (text, image, document, location, contacts, interactive)
- Receiving delivery status updates (sent, delivered, read, failed)
- Sending text, template, image, and document messages
- Marking messages as read

Configuration:
  Credentials are stored in ChannelConfig.credentials:
    phone_number_id  — WhatsApp Business phone number ID
    access_token     — Meta Graph API access token (permanent or system user)
    verify_token     — Webhook verification token (arbitrary string you choose)
"""
from __future__ import annotations

import logging
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

from packages.core.external_api_versions import META_GRAPH as _META_PIN

GRAPH_API_BASE = "https://graph.facebook.com"
# Centralized in packages/core/external_api_versions.py — keep in sync
# with facebook.py + integration_health.py via the same pin.
DEFAULT_API_VERSION = _META_PIN.value


class WhatsAppAdapter:
    """Adapter for WhatsApp Cloud API (Meta Business Platform).

    Uses httpx for async HTTP calls.
    All methods return normalised dicts compatible with channel_service.
    """

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        verify_token: str,
        api_version: str = DEFAULT_API_VERSION,
    ):
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.verify_token = verify_token
        self.api_version = api_version
        self.messages_url = f"{GRAPH_API_BASE}/{api_version}/{phone_number_id}/messages"

    # ------------------------------------------------------------------
    # Webhook verification (GET)
    # ------------------------------------------------------------------

    async def verify_webhook(
        self,
        mode: str,
        token: str,
        challenge: str,
    ) -> str | None:
        """Verify webhook subscription (GET request from Meta).

        Meta sends hub.mode, hub.verify_token, and hub.challenge.
        If mode is "subscribe" and token matches, return the challenge string.
        Otherwise return None (caller should return 403).
        """
        if mode == "subscribe" and token == self.verify_token:
            logger.info("WhatsApp webhook verified successfully")
            return challenge
        logger.warning("WhatsApp webhook verification failed: mode=%s token_match=%s", mode, token == self.verify_token)
        return None

    # ------------------------------------------------------------------
    # Inbound webhook handling (POST)
    # ------------------------------------------------------------------

    async def handle_webhook(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse incoming webhook events into normalised messages.

        Meta sends a batch of changes in each webhook POST. Each change may
        contain messages and/or status updates.

        Returns a list of normalised dicts:
            {
                "sender_id": str,
                "message_type": str,   # text | image | document | location | contacts | interactive | status
                "content": str,
                "msg_id": str | None,
                "raw": dict,
                "channel": "whatsapp",
            }
        """
        results: list[dict[str, Any]] = []

        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # Process messages
                for message in value.get("messages", []):
                    parsed = self._parse_message(message, value)
                    if parsed:
                        results.append(parsed)

                # Process status updates
                for status in value.get("statuses", []):
                    results.append({
                        "sender_id": status.get("recipient_id", ""),
                        "message_type": "status",
                        "content": status.get("status", ""),
                        "msg_id": status.get("id"),
                        "status": status.get("status", ""),
                        "timestamp": status.get("timestamp", ""),
                        "raw": status,
                        "channel": "whatsapp",
                    })

        return results

    def _parse_message(
        self,
        message: dict[str, Any],
        value: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Parse a single WhatsApp message into a normalised dict."""
        msg_type = message.get("type", "text")
        sender = message.get("from", "")
        msg_id = message.get("id", "")
        content = ""

        if msg_type == "text":
            content = message.get("text", {}).get("body", "")

        elif msg_type == "image":
            image = message.get("image", {})
            caption = image.get("caption", "")
            content = caption if caption else f"[Image: {image.get('id', '')}]"

        elif msg_type == "document":
            doc = message.get("document", {})
            filename = doc.get("filename", "")
            caption = doc.get("caption", "")
            content = caption if caption else f"[Document: {filename or doc.get('id', '')}]"

        elif msg_type == "location":
            loc = message.get("location", {})
            content = f"Location: ({loc.get('latitude', '')},{loc.get('longitude', '')}) {loc.get('name', '')}".strip()

        elif msg_type == "contacts":
            contacts = message.get("contacts", [])
            names = [c.get("name", {}).get("formatted_name", "") for c in contacts]
            content = f"Contacts: {', '.join(names)}"

        elif msg_type == "interactive":
            interactive = message.get("interactive", {})
            resp_type = interactive.get("type", "")
            if resp_type == "button_reply":
                content = interactive.get("button_reply", {}).get("title", "")
            elif resp_type == "list_reply":
                content = interactive.get("list_reply", {}).get("title", "")
            else:
                content = f"[Interactive: {resp_type}]"

        elif msg_type == "reaction":
            reaction = message.get("reaction", {})
            content = f"[Reaction: {reaction.get('emoji', '')}]"

        elif msg_type == "sticker":
            content = "[Sticker]"

        elif msg_type == "audio":
            content = "[Audio message]"

        elif msg_type == "video":
            video = message.get("video", {})
            caption = video.get("caption", "")
            content = caption if caption else "[Video]"

        else:
            content = f"[{msg_type}]"

        # Extract contact profile name if available
        contacts = value.get("contacts", [])
        profile_name = ""
        if contacts:
            profile_name = contacts[0].get("profile", {}).get("name", "")

        return {
            "sender_id": sender,
            "message_type": msg_type,
            "content": content,
            "msg_id": msg_id,
            "profile_name": profile_name,
            "raw": message,
            "channel": "whatsapp",
        }

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    async def send_text(self, to: str, text: str) -> dict[str, Any]:
        """Send text message.

        POST https://graph.facebook.com/{version}/{phone_number_id}/messages

        Returns:
            {
                "external_id": str,   # WhatsApp message ID (wamid)
                "status": str,
                "raw": dict,
            }
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }
        return await self._send(payload)

    async def send_template(
        self,
        to: str,
        template_name: str,
        language: str,
        components: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Send template message (required for initiating conversations).

        Template messages must be pre-approved by Meta.

        Args:
            to: Recipient phone number in E.164 format.
            template_name: Approved template name.
            language: Language code (e.g. "en_US").
            components: List of template components (header, body, button params).

        Returns normalised result dict.
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": components,
            },
        }
        return await self._send(payload)

    async def send_image(
        self,
        to: str,
        image_url: str,
        caption: str = "",
    ) -> dict[str, Any]:
        """Send image message.

        Args:
            to: Recipient phone number.
            image_url: Public URL of the image.
            caption: Optional image caption.
        """
        image_obj: dict[str, Any] = {"link": image_url}
        if caption:
            image_obj["caption"] = caption

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "image",
            "image": image_obj,
        }
        return await self._send(payload)

    async def send_document(
        self,
        to: str,
        document_url: str,
        filename: str,
    ) -> dict[str, Any]:
        """Send document message.

        Args:
            to: Recipient phone number.
            document_url: Public URL of the document.
            filename: Display filename for the recipient.
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "document",
            "document": {
                "link": document_url,
                "filename": filename,
            },
        }
        return await self._send(payload)

    # ------------------------------------------------------------------
    # Read receipts
    # ------------------------------------------------------------------

    async def mark_as_read(self, message_id: str) -> bool:
        """Mark a message as read.

        Sends a read receipt to the sender so they see blue check marks.
        """
        if httpx is None:
            raise RuntimeError("httpx is not installed. Run: pip install httpx")

        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                self.messages_url,
                json=payload,
                headers=self._headers(),
            )

        if resp.status_code >= 400:
            logger.error("WhatsApp mark_as_read failed: status=%s body=%s", resp.status_code, resp.text)
            return False
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    async def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a message payload to the WhatsApp Cloud API.

        Returns normalised result dict with external_id and raw response.
        """
        if httpx is None:
            raise RuntimeError("httpx is not installed. Run: pip install httpx")

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.messages_url,
                json=payload,
                headers=self._headers(),
            )
            data = resp.json()

        if resp.status_code >= 400:
            error_msg = data.get("error", {}).get("message", resp.text)
            error_code = data.get("error", {}).get("code", resp.status_code)
            logger.error("WhatsApp API error: code=%s message=%s", error_code, error_msg)
            raise RuntimeError(f"WhatsApp API error {error_code}: {error_msg}")

        # Extract wamid from response
        messages = data.get("messages", [])
        wamid = messages[0].get("id", "") if messages else ""

        return {
            "external_id": wamid,
            "status": "sent",
            "raw": data,
        }


# ── Polymorphic ChannelAdapter wrapper ──────────────────────────────────────

import json as _json
from typing import Optional as _Optional

from packages.core.models.channel import ChannelConfig as _CC
from packages.core.services.channels.base import (
    ChannelAdapter, NormalizedInbound, register_adapter,
)


class WhatsAppChannelAdapter(ChannelAdapter):
    channel_type = "whatsapp"

    def _build(self, cc: _CC) -> WhatsAppAdapter:
        creds = cc.credentials or {}
        phone_id = creds.get("phone_number_id") or creds.get("phone_id")
        token = creds.get("access_token") or creds.get("api_key")
        verify = creds.get("verify_token", "")
        if not (phone_id and token):
            raise RuntimeError("WhatsApp ChannelConfig missing phone_number_id / access_token")
        return WhatsAppAdapter(
            phone_number_id=phone_id, access_token=token, verify_token=verify,
        )

    async def send_text(self, cc: _CC, to: str, text: str, **kwargs: Any) -> dict[str, Any]:
        return await self._build(cc).send_text(to, text)

    async def send_attachment(
        self, cc: _CC, to: str, *, url=None, data=None,
        mime_type=None, caption=None, kind="document",
    ) -> dict[str, Any]:
        """Send media via WhatsApp Cloud API. The easiest path for
        public HTTPS URLs is the ``link`` field — Meta fetches the
        URL server-side. For local bytes we'd upload via /media first
        but that's not implemented yet."""
        if httpx is None:
            raise RuntimeError("httpx is required — pip install httpx")
        if not url:
            raise NotImplementedError(
                "WhatsApp send_attachment needs an HTTPS URL (bytes upload via "
                "/media not implemented yet)."
            )
        adapter = self._build(cc)
        # Map manor-os kinds to WhatsApp's msgtype
        kind_map = {
            "image": "image", "document": "document",
            "audio": "audio", "video": "video",
        }
        msg_type = kind_map.get(kind, "document")
        body: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": msg_type,
            msg_type: {"link": url},
        }
        if caption and msg_type in ("image", "document", "video"):
            body[msg_type]["caption"] = caption
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{GRAPH_API_BASE}/{DEFAULT_API_VERSION}/{adapter.phone_number_id}/messages",
                headers={
                    "Authorization": f"Bearer {adapter.access_token}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        if not resp.is_success:
            raise RuntimeError(f"WhatsApp API error {resp.status_code}: {resp.text[:200]}")
        return {"to": to, "status": "sent", "raw": resp.json()}

    async def verify_inbound(self, cc: _CC, *, headers, query, body) -> bool:
        # GET handshake handled at router level (hub.mode=subscribe). POST
        # signature from Meta uses X-Hub-Signature-256 HMAC-SHA256.
        import hashlib, hmac
        secret = (cc.credentials or {}).get("app_secret", "")
        if not secret:
            return True  # not enforced when secret absent
        sig = headers.get("X-Hub-Signature-256", "")
        if not sig.startswith("sha256="):
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"sha256={expected}", sig)

    async def parse_inbound(self, cc: _CC, *, headers, query, body) -> _Optional[NormalizedInbound]:
        try:
            payload = _json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            return None
        events = await self._build(cc).handle_webhook(payload)
        msg_events = [e for e in events if e.get("message_type") not in (None, "status")]
        if not msg_events:
            return None
        first = msg_events[0]
        sender = str(first.get("sender_id", ""))
        return NormalizedInbound(
            channel_type="whatsapp",
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=sender,
            reply_to=sender,
            content=first.get("content", "") or "",
            message_type=first.get("message_type", "text"),
            external_message_id=first.get("msg_id"),
            raw=first.get("raw") or payload,
        )


register_adapter(WhatsAppChannelAdapter())
