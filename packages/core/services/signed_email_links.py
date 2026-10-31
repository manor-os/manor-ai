"""Signed, address-bound links for emails (unsubscribe and similar one-step actions).

A token names the account and the address it was sent to. Once the account's
email changes the link stops verifying, so a forwarded or stale message cannot
act on the new address.
"""
from __future__ import annotations

import hashlib
import html
import os
import time
from collections.abc import Iterable
from typing import Any

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.config import get_settings
from packages.core.models.user import User


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def sign_email_link(user: User, typ: str, **claims: Any) -> str:
    settings = get_settings()
    return jwt.encode(
        {**claims, "typ": typ, "sub": user.id, "email_hash": email_hash(user.email), "iat": int(time.time())},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


async def verify_email_link(
    db: AsyncSession,
    token: str,
    typ: str,
    *,
    required: Iterable[str] = (),
) -> tuple[User, dict] | None:
    """Return ``(user, claims)`` for a valid link of type ``typ``, else ``None``.

    ``iat`` is deliberately not required: links sent before it was added still work.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["typ", "sub", "email_hash", *required]},
        )
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != typ:
        return None
    user = (await db.execute(
        select(User).where(User.id == str(payload.get("sub") or ""), User.deleted_at.is_(None))
    )).scalar_one_or_none()
    if user is None or not user.email or payload.get("email_hash") != email_hash(user.email):
        return None
    return user, payload


def app_link(path: str) -> str:
    app_url = os.getenv("APP_URL", "http://localhost:3010").rstrip("/")
    return f"{app_url}/{path.lstrip('/')}"


def email_action_page_html(
    title: str,
    message: str,
    action: str | None = None,
    *,
    button: str = "Unsubscribe",
) -> str:
    """HTML for a one-step email action page; the caller wraps it in a response."""
    form = (
        f'<form method="post" action="{html.escape(action, quote=True)}">'
        f'<button type="submit">{html.escape(button)}</button></form>'
        if action else ""
    )
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · Manor AI</title>
<style>
body{{margin:0;background:#fbfbfa;color:#292524;font-family:Inter,-apple-system,BlinkMacSystemFont,system-ui,sans-serif}}
main{{width:min(520px,calc(100% - 40px));margin:12vh auto;padding:32px;border-radius:22px;background:rgba(255,255,255,.78);box-shadow:0 18px 52px rgba(28,25,23,.10)}}
h1{{margin:0 0 12px;font-size:26px;color:#1c1917}}p{{margin:0 0 24px;line-height:1.6;color:#57534e}}
button{{border:0;border-radius:10px;background:#1c1917;color:white;padding:11px 18px;font:inherit;font-weight:700;cursor:pointer}}
button:focus-visible{{outline:3px solid rgba(67,107,101,.28);outline-offset:2px}}
</style></head><body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>{form}</main></body></html>"""
    return body
