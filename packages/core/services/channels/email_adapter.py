"""Email channel adapter — SMTP send, IMAP inbound hook (optional).

Outbound uses stdlib ``smtplib`` (matches the email MCP module). Inbound
is a thin stub: a deploy can either (a) set up an IMAP IDLE poller that
posts parsed messages to ``/api/v1/channels/email/callback`` or (b) wire
an external mail-forward-to-webhook service (Mailgun, Postmark, SendGrid
inbound parse). Both funnel through ``parse_inbound`` below.

Credentials are leased from the ChannelConfig's source Integration (mirrors
the email MCP bundle):
    {
      smtp_host, smtp_port, use_tls_smtp, use_ssl_smtp,
      imap_host, imap_port, use_ssl_imap,
      username, password, from_address
    }
"""
from __future__ import annotations

import base64
import json
import logging
from email.message import EmailMessage
from typing import Any, Dict, Optional

from packages.core.models.channel import ChannelConfig
from packages.core.services.channels.base import (
    ChannelAdapter, ChannelTextSendError, NormalizedInbound, register_adapter,
)
from packages.core.services import smtp_transport

logger = logging.getLogger(__name__)


def _normalize_inbound_attachments(payload: Dict[str, Any]) -> list[dict[str, Any]]:
    """Accept bounded inline bytes from an IMAP poller/inbound-mail webhook."""

    from packages.core.services.email_attachments import (
        EmailAttachmentError,
        MAX_EMAIL_ATTACHMENTS,
        MAX_EMAIL_ATTACHMENTS_TOTAL_BYTES,
        decode_inbound_email_attachment,
    )

    raw_items = payload.get("attachments")
    if not isinstance(raw_items, list):
        return []
    normalized: list[dict[str, Any]] = []
    total_bytes = 0
    for index, raw in enumerate(raw_items[:MAX_EMAIL_ATTACHMENTS]):
        if not isinstance(raw, dict):
            continue
        item = {
            "filename": raw.get("filename") or f"attachment-{index + 1}",
            "content_type": raw.get("content_type") or raw.get("mime_type"),
            "data_base64": raw.get("data_base64") or raw.get("content_base64"),
            "attachment_id": raw.get("attachment_id") or str(index),
            "folder": raw.get("folder"),
            "uid": raw.get("uid"),
            "message_id": payload.get("message_id"),
            "from": payload.get("from"),
            "subject": payload.get("subject"),
        }
        try:
            data, filename, content_type = decode_inbound_email_attachment(item)
            total_bytes += len(data)
            if total_bytes > MAX_EMAIL_ATTACHMENTS_TOTAL_BYTES:
                raise EmailAttachmentError(
                    "Email attachments exceed the 20 MiB total limit."
                )
        except EmailAttachmentError as exc:
            normalized.append({
                "filename": str(item["filename"]),
                "attachment_id": str(item["attachment_id"]),
                "status": "error",
                "error": str(exc),
            })
            continue
        normalized.append({
            **{
                key: value
                for key, value in item.items()
                if key not in {"filename", "content_type", "data_base64"}
                and value not in (None, "")
            },
            "filename": filename,
            "content_type": content_type,
            "size": len(data),
            "data_base64": base64.b64encode(data).decode("ascii"),
        })
    return normalized


class EmailChannelAdapter(ChannelAdapter):
    channel_type = "email"

    async def send_text(
        self, cc: ChannelConfig, to: str, text: str, **kwargs: Any,
    ) -> Dict[str, Any]:
        cfg = await self.credentials(cc, reason="channel.email.send_text")
        host = cfg.get("smtp_host") or cfg.get("host")
        port = int(cfg.get("smtp_port") or cfg.get("port") or 587)
        username = cfg.get("username")
        password = cfg.get("password")
        from_addr = kwargs.get("from_address") or cfg.get("from_address") or username
        subject = kwargs.get("subject") or "Reply from your assistant"
        html = kwargs.get("html")
        use_tls = bool(cfg.get("use_tls_smtp", port == 587))
        use_ssl = bool(cfg.get("use_ssl_smtp", port == 465))

        if not (host and username and password and from_addr):
            raise ChannelTextSendError.determinate(
                "Email ChannelConfig missing host/username/password/from_address"
            )

        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")

        await smtp_transport.send_message_async(
            message=msg,
            host=host,
            port=port,
            username=username,
            password=password,
            use_starttls=use_tls,
            use_ssl=use_ssl,
            timeout=20,
        )
        return {"to": to, "subject": subject, "status": "sent"}

    async def send_attachment(
        self, cc: ChannelConfig, to: str, *, url=None, data=None,
        mime_type=None, caption=None, kind="document",
    ) -> Dict[str, Any]:
        """Send an email with a file attachment. Fetches the URL if
        bytes weren't supplied."""
        import httpx
        cfg = await self.credentials(cc, reason="channel.email.send_attachment")
        if not url and not data:
            raise RuntimeError("send_attachment needs url or data")
        fname = "attachment"
        if url:
            import os as _os
            from urllib.parse import urlparse as _urlparse
            fname = _os.path.basename(_urlparse(url).path) or fname
            if data is None:
                async with httpx.AsyncClient(timeout=30) as c:
                    r = await c.get(url)
                    r.raise_for_status()
                    data = r.content
                    mime_type = mime_type or r.headers.get(
                        "Content-Type", "application/octet-stream",
                    )

        host = cfg.get("smtp_host") or cfg.get("host")
        port = int(cfg.get("smtp_port") or cfg.get("port") or 587)
        username = cfg.get("username")
        password = cfg.get("password")
        from_addr = cfg.get("from_address") or username
        use_tls = bool(cfg.get("use_tls_smtp", port == 587))
        use_ssl = bool(cfg.get("use_ssl_smtp", port == 465))
        if not (host and username and password and from_addr):
            raise RuntimeError("Email ChannelConfig missing SMTP fields")

        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = caption or f"Attachment: {fname}"
        msg.set_content(caption or f"Attachment: {fname}")
        maintype, _, subtype = (mime_type or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype or "application",
                           subtype=subtype or "octet-stream", filename=fname)

        await smtp_transport.send_message_async(
            message=msg,
            host=host,
            port=port,
            username=username,
            password=password,
            use_starttls=use_tls,
            use_ssl=use_ssl,
            timeout=30,
        )
        return {"to": to, "filename": fname, "status": "sent"}

    async def parse_inbound(
        self, cc: ChannelConfig, *, headers, query, body,
    ) -> Optional[NormalizedInbound]:
        """Parses a normalised JSON envelope that the IMAP poller / third-
        party mail-forward service POSTs. Shape::

            {
              "from": "alice@example.com",
              "from_name": "Alice",
              "subject": "Hi",
              "text": "...",
              "message_id": "<abc@example.com>",
              "attachments": [{
                "filename": "brief.pdf",
                "content_type": "application/pdf",
                "data_base64": "..."
              }]
            }
        """
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            return None
        sender = (payload.get("from") or "").strip().lower()
        if not sender:
            return None
        attachments = _normalize_inbound_attachments(payload)
        content = payload.get("text") or payload.get("body") or ""
        return NormalizedInbound(
            channel_type="email",
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=sender,
            sender_name=payload.get("from_name") or sender,
            reply_to=sender,
            content=content,
            message_type="file" if attachments and not content else "text",
            attachments=attachments,
            external_message_id=payload.get("message_id"),
            raw=payload,
        )


register_adapter(EmailChannelAdapter())
