"""Public business endpoints used by the marketing site."""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.middleware.rate_limit import RateLimiter
from packages.core.database import get_db
from packages.core.models.waiting_list import WaitingListEntry
from packages.core.services.captcha_service import verify_captcha
from packages.core.services.cloudflare_ips import is_cloudflare_ip
from packages.core.services.email_service import send_waitlist_confirmation_email
from packages.core.services.waiting_list import (
    find_recent_entry_by_email,
    is_disposable_email,
    send_admin_notification,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/business", tags=["business-public"])


# ── Anti-bot configuration (env-driven) ──────────────────────────────

# A separate flag from the global ``CAPTCHA_ENABLED`` so we can turn
# captcha on for the public waitlist without forcing it on every auth
# endpoint (or vice versa). Default off — landing page can opt in once
# the site key is wired up.
WAITLIST_CAPTCHA_REQUIRED = os.getenv("WAITLIST_CAPTCHA_REQUIRED", "false").lower() == "true"

# Per-IP submissions allowed within ``WAITLIST_RATE_WINDOW_SECONDS``.
# Defaults: 5 submissions / hour. Honest users almost never hit this;
# scripted floods hit it on the first burst.
WAITLIST_RATE_MAX = int(os.getenv("WAITLIST_RATE_MAX", "5"))
WAITLIST_RATE_WINDOW_SECONDS = int(os.getenv("WAITLIST_RATE_WINDOW_SECONDS", "3600"))

# Dedicated in-memory limiter — kept separate from the chat/API
# limiter in middleware/rate_limit.py so its window state doesn't get
# entangled with general traffic. Process-local; multi-replica
# deploys will still see N×limit in aggregate, which is acceptable
# for a low-traffic public form.
_waitlist_limiter = RateLimiter()


class WaitingListRequest(BaseModel):
    name: str
    email: str
    company: Optional[str] = ""
    interested: str
    message: str
    # Optional captcha token — required only when WAITLIST_CAPTCHA_REQUIRED
    # is on. Field name matches what auth.py uses so a shared form
    # component on the landing page can fill it the same way.
    captcha_token: Optional[str] = None
    # Honeypot: a hidden form field. Real users never fill it; bots
    # that auto-fill every input do. When non-empty we silently accept
    # and discard — telling the bot "no" would help it train.
    # ``Field(alias=...)`` lets the landing page choose a benign name
    # like ``website`` while the server logic uses ``_hp``.
    hp: Optional[str] = Field(default="", alias="website")

    model_config = {"populate_by_name": True}


class WaitingListEntryResponse(BaseModel):
    id: str
    name: str
    email: str
    company: Optional[str]
    interested: str
    message: str
    status: str
    created_at: str


class WaitingListSubmitResponse(BaseModel):
    code: int
    message: str
    data: WaitingListEntryResponse


def _client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip() or None
    if request.client:
        return request.client.host
    return None


def _entry_response(row: WaitingListEntry) -> WaitingListEntryResponse:
    return WaitingListEntryResponse(
        id=row.id,
        name=row.name,
        email=row.email,
        company=row.company,
        interested=row.interested,
        message=row.message,
        status=row.status,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


def _fake_accepted_response(req: WaitingListRequest) -> WaitingListSubmitResponse:
    """Pretend success — used when a bot trips the honeypot. We never
    surface "you got blocked" so the bot can't iterate against the
    signal."""
    return WaitingListSubmitResponse(
        code=200,
        message="Submitted",
        data=WaitingListEntryResponse(
            id="",
            name=req.name,
            email=req.email,
            company=req.company or None,
            interested=req.interested,
            message=req.message,
            status="new",
            created_at="",
        ),
    )


@router.post("/waitingList", response_model=WaitingListSubmitResponse)
@router.post("/waiting-list", response_model=WaitingListSubmitResponse, include_in_schema=False)
async def submit_waiting_list(
    req: WaitingListRequest,
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Create a waiting-list entry from the landing page.

    Public — no token required because the caller doesn't have one
    yet. Defended against bots with a layered pipeline (honeypot →
    field validation → disposable-domain block → per-IP rate limit →
    optional captcha → 24h email dedupe). Each layer fails closed
    except the honeypot (silent accept) and dedupe (silent return of
    the existing row).
    """
    # 1) Honeypot — silent accept. Must be FIRST so we burn the
    #    fewest cycles on obvious bots.
    if (req.hp or "").strip():
        logger.info(
            "waitlist honeypot tripped from ip=%s ua=%s",
            _client_ip(request), request.headers.get("user-agent"),
        )
        return _fake_accepted_response(req)

    # 2) Field validation.
    name = req.name.strip()
    email = req.email.strip().lower()
    company = (req.company or "").strip() or None
    interested = req.interested.strip()
    message = req.message.strip()

    if not name or not email or "@" not in email or not interested or not message:
        raise HTTPException(400, "Please fill in all fields.")

    # 3) Disposable-domain block — cheap, no I/O.
    if is_disposable_email(email):
        raise HTTPException(
            400, "Please use a permanent email address."
        )

    # 4) Per-IP rate limit. We check BEFORE the captcha verify so a
    #    flood can't burn captcha-provider quota.
    ip = _client_ip(request) or "unknown"
    if not _waitlist_limiter.check_sync(
        f"waitlist-ip:{ip}",
        WAITLIST_RATE_MAX,
        WAITLIST_RATE_WINDOW_SECONDS,
    ).allowed:
        raise HTTPException(
            429, "Too many submissions from this network. Please try again later.",
        )

    # 5) Captcha (only when explicitly required for waitlist).
    if WAITLIST_CAPTCHA_REQUIRED:
        if not req.captcha_token:
            raise HTTPException(400, "Captcha token is required.")
        if not await verify_captcha(req.captcha_token, ip if ip != "unknown" else None):
            raise HTTPException(400, "Captcha verification failed.")

    # 6) Silent dedupe — same email retrying within 24h returns the
    #    existing entry as if it were fresh. Stops both honest
    #    double-submits and lazy bots from filling the table.
    existing = await find_recent_entry_by_email(db, email)
    if existing is not None:
        return WaitingListSubmitResponse(
            code=200,
            message="Submitted",
            data=_entry_response(existing),
        )

    # 7) Insert. We tag CF-egress submissions with a distinct source so
    #    admins can filter the suspicious ones without blocking real
    #    users on WARP / Zero Trust (who'd otherwise be false positives).
    source = "landing-cf" if is_cloudflare_ip(ip) else "landing"
    row = WaitingListEntry(
        name=name,
        email=email,
        company=company,
        interested=interested,
        message=message,
        source=source,
        ip_address=ip if ip != "unknown" else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.add(row)
    await db.flush()

    # 8) Notify ops out-of-band — never blocks the response.
    background.add_task(
        send_admin_notification,
        {
            "name": row.name,
            "email": row.email,
            "company": row.company,
            "interested": row.interested,
            "message": row.message,
            "source": row.source,
        },
    )

    # 9) Confirm to the submitter that they're on the list. Errors are
    #    swallowed by send_email — a bad SMTP must not 500 the public form.
    background.add_task(send_waitlist_confirmation_email, row.email, row.name)

    return WaitingListSubmitResponse(
        code=200,
        message="Submitted",
        data=_entry_response(row),
    )
