"""Channel service — unified send_message() that routes to any provider.

Ported from Java backend:
  - TwilioServiceImpl (SMS / voice)
  - WhatsAppIntegrationServiceImpl (WhatsApp Cloud API)
  - SysSourceMailMessageServiceImpl (email via SMTP/Gmail)
  - WhatsAppAnnouncementServiceImpl (broadcast announcements)

Configuration:
  Per-entity credentials are stored in ChannelConfig.credentials (encrypted JSONB).
  Global fallback env vars:
    TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN   — default Twilio credentials
    WHATSAPP_API_TOKEN / WHATSAPP_PHONE_ID   — WhatsApp Cloud API defaults
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.channel import (
    Announcement,
    AnnouncementRecipient,
    ChannelConfig,
    MessageLog,
    PhoneNumber,
)

logger = logging.getLogger(__name__)

_SMS_CHANNEL_TYPES = {"sms", "twilio_sms"}
_VOICE_CHANNEL_TYPES = {"voice", "twilio_voice"}

# Optional heavy imports — fail gracefully if not installed
try:
    import aiosmtplib
except ImportError:
    aiosmtplib = None  # type: ignore[assignment]

try:
    from twilio.rest import Client as TwilioClient
except ImportError:
    TwilioClient = None  # type: ignore[assignment]

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]


# ============================================================================
# Channel config management
# ============================================================================

async def create_channel_config(
    db: AsyncSession,
    entity_id: str,
    channel_type: str,
    provider: str,
    config: dict,
    credentials: dict | None = None,
    *,
    name: str | None = None,
    workspace_id: str | None = None,
) -> ChannelConfig:
    """Create a new channel configuration."""
    cc = ChannelConfig(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_type=channel_type,
        provider=provider,
        name=name,
        config=config,
        credentials=credentials or {},
        workspace_id=workspace_id,
    )
    db.add(cc)
    await db.flush()
    return cc


async def update_channel_config(
    db: AsyncSession,
    config_id: str,
    entity_id: str,
    **updates,
) -> Optional[ChannelConfig]:
    """Update an existing channel config. Returns None if not found."""
    cc = await _get_channel_config(db, config_id, entity_id)
    if not cc:
        return None
    for key, value in updates.items():
        if value is not None and hasattr(cc, key):
            setattr(cc, key, value)
    await db.flush()
    await db.refresh(cc)
    return cc


async def list_channel_configs(
    db: AsyncSession,
    entity_id: str,
    channel_type: str | None = None,
) -> list[ChannelConfig]:
    """List channel configs for an entity, optionally filtered by type."""
    q = select(ChannelConfig).where(ChannelConfig.entity_id == entity_id)
    if channel_type:
        q = q.where(ChannelConfig.channel_type == channel_type)
    q = q.order_by(ChannelConfig.created_at.desc())
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_channel_config(
    db: AsyncSession,
    config_id: str,
    entity_id: str,
) -> Optional[ChannelConfig]:
    """Public accessor."""
    return await _get_channel_config(db, config_id, entity_id)


async def delete_channel_config(
    db: AsyncSession,
    config_id: str,
    entity_id: str,
) -> bool:
    cc = await _get_channel_config(db, config_id, entity_id)
    if not cc:
        return False
    await db.delete(cc)
    await db.flush()
    return True


async def test_channel_config(
    db: AsyncSession,
    config_id: str,
    entity_id: str,
) -> dict:
    """Send a test message through the channel to verify config works.

    Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    cc = await _get_channel_config(db, config_id, entity_id)
    if not cc:
        return {"ok": False, "error": "Channel config not found"}

    try:
        if cc.channel_type == "email":
            test_to = cc.config.get("from_email", cc.config.get("email", ""))
            result = await _send_email(cc, to=test_to, subject="Manor AI — Channel Test", content="This is a test email from Manor AI.", html_content=None, attachments=None)
        elif cc.channel_type in _SMS_CHANNEL_TYPES:
            # Send test to the first owned number or a configured test number
            test_to = cc.config.get("test_number", "")
            if not test_to:
                return {"ok": False, "error": "Set config.test_number to run a test"}
            result = await _send_sms(cc, to=test_to, content="Manor AI channel test")
        elif cc.channel_type in _VOICE_CHANNEL_TYPES:
            test_to = cc.config.get("test_number", "")
            if not test_to:
                return {"ok": False, "error": "Set config.test_number to run a test"}
            twiml_url = cc.config.get("test_twiml_url", "")
            if not twiml_url:
                return {"ok": False, "error": "Set config.test_twiml_url to run a voice test"}
            result = await _send_voice_call(cc, to=test_to, twiml_url=twiml_url)
        elif cc.channel_type == "whatsapp":
            test_to = cc.config.get("test_number", "")
            if not test_to:
                return {"ok": False, "error": "Set config.test_number to run a test"}
            result = await _send_whatsapp(cc, to=test_to, content="Manor AI channel test")
        else:
            return {"ok": False, "error": f"Test not implemented for channel type: {cc.channel_type}"}

        if result.get("error"):
            return {"ok": False, "error": result["error"]}
        return {"ok": True, "external_id": result.get("external_id")}
    except Exception as exc:
        logger.exception("Channel test failed for config %s", config_id)
        return {"ok": False, "error": str(exc)}


# ============================================================================
# Sending messages — unified entry point
# ============================================================================

async def send_message(
    db: AsyncSession,
    entity_id: str,
    *,
    channel_config_id: str,
    to_address: str,
    content: str,
    subject: str | None = None,
    html_content: str | None = None,
    attachments: list[dict] | None = None,
    conversation_id: str | None = None,
    twiml_url: str | None = None,
) -> MessageLog:
    """Send a message through any configured channel. Routes to the provider-specific sender.

    This is the core abstraction — callers don't need to know about SMTP vs Twilio vs WhatsApp.
    """
    cc = await _get_channel_config(db, channel_config_id, entity_id)
    if not cc:
        raise ValueError(f"Channel config {channel_config_id} not found for entity {entity_id}")

    # Dispatch to provider
    result: dict = {}
    try:
        if cc.channel_type == "email":
            result = await _send_email(cc, to=to_address, subject=subject or "", content=content, html_content=html_content, attachments=attachments)
        elif cc.channel_type in _SMS_CHANNEL_TYPES:
            result = await _send_sms(cc, to=to_address, content=content)
        elif cc.channel_type == "whatsapp":
            result = await _send_whatsapp(cc, to=to_address, content=content)
        elif cc.channel_type in _VOICE_CHANNEL_TYPES:
            result = await _send_voice_call(cc, to=to_address, twiml_url=twiml_url or "")
        else:
            result = {"error": f"Unsupported channel type: {cc.channel_type}"}
    except Exception as exc:
        logger.exception("Failed to send %s message via config %s", cc.channel_type, cc.id)
        result = {"error": str(exc)}

    # Log the message
    log_entry = MessageLog(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_config_id=channel_config_id,
        conversation_id=conversation_id,
        direction="outbound",
        channel_type=cc.channel_type,
        from_address=result.get("from_address", cc.config.get("from_email", cc.config.get("phone_number", ""))),
        to_address=to_address,
        subject=subject,
        content=content,
        html_content=html_content,
        attachments=attachments,
        external_id=result.get("external_id"),
        status="failed" if result.get("error") else "sent",
        error_message=result.get("error"),
        cost_amount=result.get("cost_amount"),
        cost_currency=result.get("cost_currency"),
        duration_seconds=result.get("duration_seconds"),
    )
    db.add(log_entry)
    await db.flush()
    return log_entry


# ============================================================================
# Provider-specific senders
# ============================================================================

async def _send_email(
    config: ChannelConfig,
    *,
    to: str,
    subject: str,
    content: str,
    html_content: str | None,
    attachments: list[dict] | None,
) -> dict:
    """Send email via SMTP. Config maps from Java ClientEmailConfig / SysSourceConfig."""
    if aiosmtplib is None:
        return {"error": "aiosmtplib is not installed"}

    smtp_host = config.config.get("smtp_host", os.getenv("SMTP_HOST", "localhost"))
    smtp_port = int(config.config.get("smtp_port", os.getenv("SMTP_PORT", "587")))
    username = config.credentials.get("username", config.credentials.get("email", os.getenv("SMTP_USER", "")))
    password = config.credentials.get("password", os.getenv("SMTP_PASSWORD", ""))
    from_email = config.config.get("from_email", config.config.get("email", os.getenv("SMTP_FROM_EMAIL", "noreply@example.com")))
    from_name = config.config.get("from_name", os.getenv("SMTP_FROM_NAME", "Manor AI"))
    use_tls = config.config.get("use_tls", True)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to

    if content:
        msg.attach(MIMEText(content, "plain"))
    if html_content:
        msg.attach(MIMEText(html_content, "html"))

    try:
        await aiosmtplib.send(
            msg,
            hostname=smtp_host,
            port=smtp_port,
            username=username or None,
            password=password or None,
            use_tls=use_tls,
        )
        return {"from_address": from_email}
    except Exception as exc:
        return {"error": f"SMTP send failed: {exc}", "from_address": from_email}


async def _send_sms(config: ChannelConfig, *, to: str, content: str) -> dict:
    """Send SMS via Twilio. Config maps from Java TwilioServiceImpl."""
    if TwilioClient is None:
        return {"error": "twilio library is not installed"}

    account_sid = config.credentials.get("account_sid", os.getenv("TWILIO_ACCOUNT_SID", ""))
    auth_token = config.credentials.get("auth_token", os.getenv("TWILIO_AUTH_TOKEN", ""))
    from_number = (
        config.config.get("phone_number", "")
        or config.credentials.get("phone_number", "")
        or config.credentials.get("from_number", "")
    )

    if not account_sid or not auth_token:
        return {"error": "Twilio credentials not configured"}
    if not from_number:
        return {"error": "No from phone_number in channel config"}

    try:
        client = TwilioClient(account_sid, auth_token)
        message = client.messages.create(
            body=content,
            from_=from_number,
            to=to,
        )
        return {
            "external_id": message.sid,
            "from_address": from_number,
            "cost_amount": float(message.price) if message.price else None,
            "cost_currency": message.price_unit or "USD",
        }
    except Exception as exc:
        return {"error": f"Twilio SMS failed: {exc}", "from_address": from_number}


async def _send_whatsapp(config: ChannelConfig, *, to: str, content: str) -> dict:
    """Send WhatsApp message via WhatsApp Cloud API (Meta Business).

    Config maps from Java WhatsAppIntegrationServiceImpl.
    """
    if httpx is None:
        return {"error": "httpx library is not installed"}

    api_token = config.credentials.get("api_token", os.getenv("WHATSAPP_API_TOKEN", ""))
    phone_number_id = config.config.get("phone_number_id", os.getenv("WHATSAPP_PHONE_ID", ""))
    api_version = config.config.get("api_version", "v21.0")

    if not api_token:
        return {"error": "WhatsApp API token not configured"}
    if not phone_number_id:
        return {"error": "WhatsApp phone_number_id not configured"}

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": content},
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            data = resp.json()

            if resp.status_code >= 400:
                error_msg = data.get("error", {}).get("message", resp.text)
                return {"error": f"WhatsApp API error: {error_msg}"}

            wamid = data.get("messages", [{}])[0].get("id", "")
            return {"external_id": wamid, "from_address": phone_number_id}
    except Exception as exc:
        return {"error": f"WhatsApp send failed: {exc}"}


async def _send_voice_call(config: ChannelConfig, *, to: str, twiml_url: str) -> dict:
    """Initiate an outbound voice call via Twilio. Config maps from TwilioServiceImpl."""
    if TwilioClient is None:
        return {"error": "twilio library is not installed"}

    account_sid = config.credentials.get("account_sid", os.getenv("TWILIO_ACCOUNT_SID", ""))
    auth_token = config.credentials.get("auth_token", os.getenv("TWILIO_AUTH_TOKEN", ""))
    from_number = (
        config.config.get("phone_number", "")
        or config.credentials.get("phone_number", "")
        or config.credentials.get("from_number", "")
    )

    if not account_sid or not auth_token:
        return {"error": "Twilio credentials not configured"}
    if not from_number:
        return {"error": "No from phone_number in channel config"}
    if not twiml_url:
        return {"error": "twiml_url is required for voice calls"}

    try:
        client = TwilioClient(account_sid, auth_token)
        call = client.calls.create(
            url=twiml_url,
            from_=from_number,
            to=to,
        )
        return {"external_id": call.sid, "from_address": from_number}
    except Exception as exc:
        return {"error": f"Twilio voice call failed: {exc}", "from_address": from_number}


# ============================================================================
# Inbound message handling (webhook handlers)
# ============================================================================

async def handle_inbound_message(
    db: AsyncSession,
    entity_id: str,
    channel_config_id: str,
    payload: dict,
) -> MessageLog:
    """Process an inbound message received via webhook.

    The payload is provider-specific; this function normalises it into a MessageLog.
    Callers (webhook endpoints) should pass the raw provider payload.
    """
    cc = await _get_channel_config(db, channel_config_id, entity_id)
    channel_type = cc.channel_type if cc else payload.get("channel_type", "unknown")

    # Normalise fields from provider payload
    from_addr = payload.get("from") or payload.get("From") or payload.get("from_address", "")
    to_addr = payload.get("to") or payload.get("To") or payload.get("to_address", "")
    content = payload.get("body") or payload.get("Body") or payload.get("content", "")
    subject = payload.get("subject") or payload.get("Subject")
    external_id = (
        payload.get("MessageSid")
        or payload.get("message_id")
        or payload.get("wamid")
        or payload.get("external_id")
    )

    log_entry = MessageLog(
        id=generate_ulid(),
        entity_id=entity_id,
        channel_config_id=channel_config_id,
        conversation_id=payload.get("conversation_id"),
        direction="inbound",
        channel_type=channel_type,
        from_address=from_addr,
        to_address=to_addr,
        subject=subject,
        content=content,
        html_content=payload.get("html_content"),
        external_id=external_id,
        status="received",
    )
    db.add(log_entry)
    await db.flush()
    return log_entry


# ============================================================================
# Message log queries
# ============================================================================

async def list_messages(
    db: AsyncSession,
    entity_id: str,
    *,
    conversation_id: str | None = None,
    channel_type: str | None = None,
    direction: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[MessageLog]:
    """List message logs with optional filters."""
    q = select(MessageLog).where(MessageLog.entity_id == entity_id)
    if conversation_id:
        q = q.where(MessageLog.conversation_id == conversation_id)
    if channel_type:
        q = q.where(MessageLog.channel_type == channel_type)
    if direction:
        q = q.where(MessageLog.direction == direction)
    q = q.order_by(MessageLog.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_message_stats(
    db: AsyncSession,
    entity_id: str,
    *,
    days: int = 30,
) -> dict:
    """Aggregate message statistics for the last N days."""
    cutoff = func.now() - func.cast(f"{days} days", func.literal_column("INTERVAL"))
    # Simpler approach: just count by status and channel_type
    q = (
        select(
            MessageLog.channel_type,
            MessageLog.direction,
            MessageLog.status,
            func.count().label("count"),
        )
        .where(
            MessageLog.entity_id == entity_id,
            MessageLog.created_at >= func.now() - func.make_interval(0, 0, 0, days),
        )
        .group_by(MessageLog.channel_type, MessageLog.direction, MessageLog.status)
    )
    result = await db.execute(q)
    rows = result.all()

    stats: dict = {"total": 0, "by_channel": {}, "by_direction": {}, "by_status": {}}
    for row in rows:
        ch, direction, status, count = row
        stats["total"] += count
        stats["by_channel"][ch] = stats["by_channel"].get(ch, 0) + count
        stats["by_direction"][direction] = stats["by_direction"].get(direction, 0) + count
        stats["by_status"][status] = stats["by_status"].get(status, 0) + count
    return stats


# ============================================================================
# Phone numbers
# ============================================================================

async def list_phone_numbers(
    db: AsyncSession,
    entity_id: str,
) -> list[PhoneNumber]:
    """List phone numbers owned by an entity."""
    q = (
        select(PhoneNumber)
        .where(PhoneNumber.entity_id == entity_id)
        .order_by(PhoneNumber.created_at.desc())
    )
    result = await db.execute(q)
    return list(result.scalars().all())


async def provision_phone_number(
    db: AsyncSession,
    entity_id: str,
    *,
    phone_number: str,
    provider: str = "twilio",
    provider_id: str | None = None,
    capabilities: dict | None = None,
    monthly_cost: float | None = None,
) -> PhoneNumber:
    """Record a provisioned phone number.

    Note: Actual Twilio number purchase should be done via Twilio API before
    calling this. This function only stores the record. For full Twilio
    provisioning (like Java TwilioServiceImpl.incomingPhoneNumber), the caller
    should use the Twilio SDK first, then call this to persist.
    """
    pn = PhoneNumber(
        id=generate_ulid(),
        entity_id=entity_id,
        phone_number=phone_number,
        provider=provider,
        provider_id=provider_id,
        capabilities=capabilities or {},
        monthly_cost=monthly_cost,
    )
    db.add(pn)
    await db.flush()
    return pn


async def release_phone_number(
    db: AsyncSession,
    phone_number_id: str,
    entity_id: str,
) -> bool:
    """Mark a phone number as released."""
    result = await db.execute(
        select(PhoneNumber).where(
            PhoneNumber.id == phone_number_id,
            PhoneNumber.entity_id == entity_id,
        )
    )
    pn = result.scalar_one_or_none()
    if not pn:
        return False
    pn.status = "released"
    await db.flush()
    return True


# ============================================================================
# Announcements (broadcast messaging)
# ============================================================================

async def create_announcement(
    db: AsyncSession,
    entity_id: str,
    channel_type: str,
    title: str,
    content: str,
    recipients: list[str],
    *,
    channel_config_id: str | None = None,
    workspace_id: str | None = None,
    template_id: str | None = None,
    template_name: str | None = None,
    template_language: str | None = None,
    schedule_at: datetime | None = None,
) -> Announcement:
    """Create a broadcast announcement with recipients."""
    ann = Announcement(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        channel_config_id=channel_config_id,
        channel_type=channel_type,
        title=title,
        content=content,
        template_id=template_id,
        template_name=template_name,
        template_language=template_language,
        schedule_at=schedule_at,
        recipient_count=len(recipients),
        status="scheduled" if schedule_at else "draft",
    )
    db.add(ann)
    await db.flush()

    # Create recipient records
    for addr in recipients:
        rec = AnnouncementRecipient(
            id=generate_ulid(),
            announcement_id=ann.id,
            recipient_address=addr,
            status="pending",
        )
        db.add(rec)
    await db.flush()

    return ann


async def send_announcement(
    db: AsyncSession,
    announcement_id: str,
    entity_id: str,
) -> dict:
    """Send an announcement to all its recipients.

    Maps to Java WhatsAppAnnouncementServiceImpl.sendSingleAnnouncement.
    Returns summary: {"sent": N, "failed": N, "errors": [...]}.
    """
    # Load announcement
    result = await db.execute(
        select(Announcement).where(
            Announcement.id == announcement_id,
            Announcement.entity_id == entity_id,
        )
    )
    ann = result.scalar_one_or_none()
    if not ann:
        return {"sent": 0, "failed": 0, "errors": ["Announcement not found"]}

    # Load recipients
    result = await db.execute(
        select(AnnouncementRecipient).where(
            AnnouncementRecipient.announcement_id == announcement_id,
            AnnouncementRecipient.status == "pending",
        )
    )
    recipients = list(result.scalars().all())
    if not recipients:
        return {"sent": 0, "failed": 0, "errors": ["No pending recipients"]}

    ann.status = "sending"
    await db.flush()

    sent = 0
    failed = 0
    errors: list[str] = []

    for rec in recipients:
        try:
            if ann.channel_config_id:
                msg_log = await send_message(
                    db,
                    entity_id,
                    channel_config_id=ann.channel_config_id,
                    to_address=rec.recipient_address,
                    content=ann.content,
                    subject=ann.title,
                )
                if msg_log.status == "failed":
                    rec.status = "failed"
                    rec.error_message = msg_log.error_message
                    failed += 1
                    errors.append(f"{rec.recipient_address}: {msg_log.error_message}")
                else:
                    rec.status = "sent"
                    rec.sent_at = datetime.now(timezone.utc)
                    sent += 1
            else:
                rec.status = "failed"
                rec.error_message = "No channel_config_id on announcement"
                failed += 1
        except Exception as exc:
            rec.status = "failed"
            rec.error_message = str(exc)
            failed += 1
            errors.append(f"{rec.recipient_address}: {exc}")

    # Update announcement
    ann.status = "sent" if failed == 0 else ("failed" if sent == 0 else "sent")
    ann.sent_at = datetime.now(timezone.utc)
    ann.recipient_count = sent + failed
    if errors:
        ann.error_message = "; ".join(errors[:10])  # cap stored errors

    await db.flush()
    return {"sent": sent, "failed": failed, "errors": errors}


async def list_announcements(
    db: AsyncSession,
    entity_id: str,
    *,
    workspace_id: str | None = None,
    limit: int = 50,
) -> list[Announcement]:
    """List announcements for an entity."""
    q = select(Announcement).where(Announcement.entity_id == entity_id)
    if workspace_id:
        q = q.where(Announcement.workspace_id == workspace_id)
    q = q.order_by(Announcement.created_at.desc()).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())


# ============================================================================
# Internal helpers
# ============================================================================

async def _get_channel_config(
    db: AsyncSession,
    config_id: str,
    entity_id: str,
) -> Optional[ChannelConfig]:
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.id == config_id,
            ChannelConfig.entity_id == entity_id,
        )
    )
    return result.scalar_one_or_none()
