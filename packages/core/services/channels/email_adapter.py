"""Email channel adapter — SMTP send, IMAP inbound hook (optional).

Outbound uses stdlib ``smtplib`` (matches the email MCP module). Inbound
is a thin stub: a deploy can either (a) set up an IMAP IDLE poller that
posts parsed messages to ``/api/v1/channels/email/callback`` or (b) wire
an external mail-forward-to-webhook service (Mailgun, Postmark, SendGrid
inbound parse). Both funnel through ``parse_inbound`` below.

Credentials in ChannelConfig.credentials (mirrors the email MCP bundle):
    {
      smtp_host, smtp_port, use_tls_smtp, use_ssl_smtp,
      imap_host, imap_port, use_ssl_imap,
      username, password, from_address
    }
"""
from __future__ import annotations

import asyncio
import json
import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Dict, Optional

from packages.core.models.channel import ChannelConfig
from packages.core.services.channels.base import (
    ChannelAdapter, NormalizedInbound, register_adapter,
)

logger = logging.getLogger(__name__)


class EmailChannelAdapter(ChannelAdapter):
    channel_type = "email"

    async def send_text(
        self, cc: ChannelConfig, to: str, text: str, **kwargs: Any,
    ) -> Dict[str, Any]:
        cfg = cc.credentials or {}
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
            raise RuntimeError("Email ChannelConfig missing host/username/password/from_address")

        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")

        def _send_sync() -> None:
            if use_ssl:
                with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                    s.login(username, password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=20) as s:
                    s.ehlo()
                    if use_tls:
                        s.starttls(); s.ehlo()
                    s.login(username, password)
                    s.send_message(msg)

        await asyncio.to_thread(_send_sync)
        return {"to": to, "subject": subject, "status": "sent"}

    async def send_attachment(
        self, cc: ChannelConfig, to: str, *, url=None, data=None,
        mime_type=None, caption=None, kind="document",
    ) -> Dict[str, Any]:
        """Send an email with a file attachment. Fetches the URL if
        bytes weren't supplied."""
        import httpx
        cfg = cc.credentials or {}
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

        def _send_sync() -> None:
            if use_ssl:
                with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                    s.login(username, password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=30) as s:
                    s.ehlo()
                    if use_tls:
                        s.starttls(); s.ehlo()
                    s.login(username, password)
                    s.send_message(msg)

        await asyncio.to_thread(_send_sync)
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
              "message_id": "<abc@example.com>"
            }
        """
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            return None
        sender = (payload.get("from") or "").strip().lower()
        if not sender:
            return None
        return NormalizedInbound(
            channel_type="email",
            channel_config_id=cc.id,
            entity_id=cc.entity_id,
            source_id=sender,
            sender_name=payload.get("from_name") or sender,
            reply_to=sender,
            content=payload.get("text") or payload.get("body") or "",
            message_type="text",
            external_message_id=payload.get("message_id"),
            raw=payload,
        )


register_adapter(EmailChannelAdapter())
