"""Helpers for the public waiting-list submit flow.

The public endpoint in ``apps/api/routers/business.py`` is anonymous —
no token, no rate-limited user context — so it needs in-app defences
against bot floods and admin notifications for new genuine submissions.

What lives here:

* ``is_disposable_email`` — domain blocklist check for known throwaway
  inboxes. Built-in list plus ``WAITLIST_BLOCKED_EMAIL_DOMAINS`` env
  for ops to extend without a deploy.
* ``find_recent_entry_by_email`` — short-window dedupe lookup so the
  same email retrying (manually or via bot) becomes a silent no-op
  rather than a duplicate row.
* ``send_admin_notification`` — fire-and-forget email to the ops
  recipients configured in ``ops_config`` / ``OPS_EMAIL_RECIPIENTS``
  whenever a new genuine entry lands. Best-effort: any failure is
  swallowed and logged.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.waiting_list import WaitingListEntry

logger = logging.getLogger(__name__)


# ── Disposable-email blocklist ───────────────────────────────────────

# Common throwaway-mail providers. Not exhaustive — bots can spin up
# new domains faster than we can chase them — but cuts off the
# overwhelming majority of low-effort spam at zero cost. Extend via
# the env var; never relax the built-in list.
_BUILTIN_DISPOSABLE_DOMAINS = frozenset({
    "mailinator.com",
    "10minutemail.com",
    "guerrillamail.com",
    "guerrillamail.net",
    "guerrillamail.org",
    "tempmail.com",
    "temp-mail.org",
    "throwawaymail.com",
    "yopmail.com",
    "trashmail.com",
    "trashmail.net",
    "dispostable.com",
    "fakeinbox.com",
    "getnada.com",
    "maildrop.cc",
    "sharklasers.com",
    "spam4.me",
    "mintemail.com",
    "mohmal.com",
    "moakt.com",
    "emailondeck.com",
})


def _extra_blocked_domains() -> frozenset[str]:
    raw = os.environ.get("WAITLIST_BLOCKED_EMAIL_DOMAINS", "")
    return frozenset(
        d.strip().lower() for d in raw.split(",") if d.strip()
    )


def is_disposable_email(email: str) -> bool:
    """True when the email's domain is on the disposable blocklist.

    Case-insensitive. Returns False for malformed addresses — callers
    should validate the ``@`` separator separately and return their
    own error message there.
    """
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if not domain:
        return False
    return domain in _BUILTIN_DISPOSABLE_DOMAINS or domain in _extra_blocked_domains()


# ── Dedupe ───────────────────────────────────────────────────────────

DEDUPE_WINDOW_HOURS = int(os.environ.get("WAITLIST_DEDUPE_WINDOW_HOURS", "24"))


async def find_recent_entry_by_email(
    db: AsyncSession, email: str, *, window_hours: int = DEDUPE_WINDOW_HOURS,
) -> Optional[WaitingListEntry]:
    """Return the most recent submission from this email within the
    dedupe window, or None.

    Used so a retry (user double-clicked Submit, bot replays the form)
    can be silently absorbed instead of creating piles of duplicate
    rows that admins then have to wade through.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    row = (await db.execute(
        select(WaitingListEntry)
        .where(
            WaitingListEntry.email == email,
            WaitingListEntry.created_at >= cutoff,
        )
        .order_by(desc(WaitingListEntry.created_at))
        .limit(1)
    )).scalar_one_or_none()
    return row


# ── Admin notifications ──────────────────────────────────────────────

async def send_admin_notification(entry_data: dict) -> None:
    """Best-effort: notify ops recipients that a new entry arrived.

    Called from a FastAPI ``BackgroundTasks`` so the public POST
    returns immediately. Failures are logged and swallowed — the user
    must never see a 500 because the admin email pipe is misconfigured.

    Recipient list comes from ``ops_config.load_config()`` (Redis
    overrides) and falls back to ``OPS_EMAIL_RECIPIENTS`` env.
    """
    try:
        from packages.core.services.ops_config import load_config
        from packages.core.services.email_service import send_common_email
    except Exception as exc:  # pragma: no cover — defensive import
        logger.warning("waitlist notification import failed: %s", exc)
        return

    try:
        cfg = await load_config()
        recipients = [r for r in (cfg.get("recipients") or []) if r]
    except Exception as exc:
        logger.warning("waitlist notification: ops_config load failed: %s", exc)
        recipients = []

    if not recipients:
        # No ops recipients configured — nothing to do. Don't error;
        # ops can wire this up later by setting the env var.
        return

    name = (entry_data.get("name") or "?").strip()
    email = (entry_data.get("email") or "?").strip()
    company = (entry_data.get("company") or "").strip()
    interested = (entry_data.get("interested") or "").strip()
    message = (entry_data.get("message") or "").strip()
    source = (entry_data.get("source") or "landing").strip()

    admin_url = os.environ.get("APP_URL", "http://localhost:3010").rstrip("/")
    subject = f"New waitlist signup: {name} <{email}>"

    rows = [
        ("Name", name),
        ("Email", email),
        ("Company", company or "—"),
        ("Interested in", interested),
        ("Source", source),
    ]
    table = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#64748b;'>{label}</td>"
        f"<td style='padding:4px 0;'><strong>{value}</strong></td></tr>"
        for label, value in rows
    )
    safe_message = (message or "(empty)").replace("\n", "<br/>")
    body = (
        f"<p>A new waiting-list entry just came in.</p>"
        f"<table style='border-collapse:collapse;font-size:14px;'>{table}</table>"
        f"<p style='margin-top:16px;color:#475569;'>Message:</p>"
        f"<blockquote style='border-left:3px solid #cbd5e1;margin:8px 0;padding:4px 12px;color:#1e293b;'>{safe_message}</blockquote>"
        f"<p style='margin-top:16px;'>"
        f"<a href='{admin_url}/admin/waiting-list' style='color:#0d9488;'>Open the waitlist in admin →</a>"
        f"</p>"
    )

    for recipient in recipients:
        try:
            await send_common_email(recipient, subject, body)
        except Exception as exc:
            # send_common_email already swallows its own errors and
            # returns False, but belt-and-suspenders so one bad
            # recipient never breaks the loop.
            logger.warning(
                "waitlist notification to %s failed: %s", recipient, exc,
            )
