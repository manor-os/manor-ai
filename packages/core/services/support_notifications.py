"""Email notifications for the support-ticket pipeline.

Two flows:
  * ``notify_ops_of_new_user_message`` — sent to ops_config recipients
    whenever a user opens a ticket or replies to one. Lets the team
    triage in real time without polling the admin inbox.
  * ``notify_user_of_admin_reply`` — sent to the ticket opener
    whenever an admin replies. Both use the common branded wrapper
    so they match the rest of the transactional emails.

All sends are fire-and-forget (errors swallowed) — a misconfigured
SMTP must never block the request that triggered them.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _admin_inbox_url(ticket_id: str) -> str:
    base = os.environ.get("APP_URL", "http://localhost:3010").rstrip("/")
    return f"{base}/admin/support-tickets/{ticket_id}"


def _user_app_url() -> str:
    return os.environ.get("APP_URL", "http://localhost:3010").rstrip("/")


def _html_escape(s: str) -> str:
    import html
    return html.escape(s or "")


def _body_block(body: str) -> str:
    """Render the message body as a quoted paragraph block. Preserves
    line breaks without trusting markup — admins/users type plain text."""
    safe = _html_escape(body).replace("\n", "<br/>")
    return (
        "<blockquote style='border-left:3px solid #cbd5e1;"
        "margin:8px 0;padding:8px 14px;color:#1e293b;"
        "background:#f8fafc;border-radius:6px;'>"
        f"{safe}"
        "</blockquote>"
    )


async def notify_ops_of_new_user_message(
    *,
    ticket_id: str,
    subject: str,
    user_email: str,
    user_display_name: Optional[str],
    body: str,
    is_new_ticket: bool,
) -> None:
    """Email ops recipients about a user message that needs a reply."""
    try:
        from packages.core.services.email_service import send_common_email
        from packages.core.services.ops_config import load_config
    except Exception as exc:  # pragma: no cover
        logger.warning("support ops-notify import failed: %s", exc)
        return

    try:
        cfg = await load_config()
        recipients = [r for r in (cfg.get("recipients") or []) if r]
    except Exception as exc:
        logger.warning("support ops-notify: ops_config load failed: %s", exc)
        recipients = []

    if not recipients:
        return

    display_name = user_display_name or user_email
    verb = "opened a support ticket" if is_new_ticket else "replied to a support ticket"
    email_subject = (
        f"[Support] {subject}" if is_new_ticket
        else f"[Support · reply] {subject}"
    )
    body_html = (
        f"<p><strong>{_html_escape(display_name)}</strong> "
        f"&lt;{_html_escape(user_email)}&gt; {verb}.</p>"
        f"<p style='color:#64748b;font-size:13px;'>"
        f"<strong>Subject:</strong> {_html_escape(subject)}</p>"
        f"{_body_block(body)}"
        f"<p style='margin-top:16px;'>"
        f"<a href='{_admin_inbox_url(ticket_id)}' "
        f"style='background:#0f766e;color:#fff;text-decoration:none;"
        f"padding:10px 18px;border-radius:8px;display:inline-block;font-weight:600;'>"
        f"Open ticket in admin →</a></p>"
    )

    for to in recipients:
        try:
            await send_common_email(to, email_subject, body_html)
        except Exception as exc:
            logger.warning(
                "support ops-notify to %s failed: %s", to, exc,
            )


async def notify_user_of_admin_reply(
    *,
    ticket_id: str,
    subject: str,
    user_email: str,
    user_display_name: Optional[str],
    admin_display_name: Optional[str],
    body: str,
) -> None:
    """Email the ticket opener about an admin reply."""
    try:
        from packages.core.services.email_service import send_common_email
    except Exception as exc:  # pragma: no cover
        logger.warning("support user-notify import failed: %s", exc)
        return

    if not user_email:
        return

    greeting_name = user_display_name or "there"
    admin_label = admin_display_name or "the Manor AI team"
    email_subject = f"Re: {subject}"

    body_html = (
        f"<p>Hi {_html_escape(greeting_name)},</p>"
        f"<p>{_html_escape(admin_label)} replied to your support ticket:</p>"
        f"{_body_block(body)}"
        f"<p style='margin-top:16px;'>"
        f"<a href='{_user_app_url()}' "
        f"style='background:#0f766e;color:#fff;text-decoration:none;"
        f"padding:10px 18px;border-radius:8px;display:inline-block;font-weight:600;'>"
        f"Reply in Manor AI →</a></p>"
        f"<p style='color:#94a3b8;font-size:12px;margin-top:16px;'>"
        f"You can continue the conversation by signing in and opening "
        f"the Support panel in the sidebar.</p>"
    )

    try:
        await send_common_email(user_email, email_subject, body_html)
    except Exception as exc:
        logger.warning(
            "support user-notify to %s failed: %s", user_email, exc,
        )
