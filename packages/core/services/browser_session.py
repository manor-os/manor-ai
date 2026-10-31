"""30-day HttpOnly login recovery with rotation, revocation and live auth gates."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from sqlalchemy import select

from packages.core.config import get_settings
from packages.core.models.browser_session import BrowserSession
from packages.core.models.user import User

COOKIE = "manor_refresh"
COOKIE_PATH = "/api/v1/auth"
LIFETIME = timedelta(days=30)
ROTATION_GRACE = timedelta(seconds=10)


def require_browser_origin(request: Request):
    # Cross-origin browser requests need a CORS preflight for this custom
    # header. Do not repeat the CORS allowlist here: same-origin dev proxies
    # can rewrite Host to an internal service without changing Origin.
    if request.headers.get("x-manor-session") != "1":
        raise HTTPException(403, "Browser session header required")
    origin = request.headers.get("origin", "")
    if not origin or origin == "null":
        raise HTTPException(403, "Browser origin required")
    try:
        parsed = urlsplit(origin)
        parsed.port  # Reject malformed ports rather than returning a server error.
    except ValueError:
        raise HTTPException(403, "Invalid browser origin")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise HTTPException(403, "Invalid browser origin")


def clear_cookie(response: Response):
    response.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"


def set_cookie(request: Request, response: Response, session: BrowserSession, secret: str):
    origin = urlsplit(request.headers.get("origin") or str(request.base_url))
    # Cloud authentication cookies must retain TLS-only transport even when a
    # client supplies an HTTP Origin or the reverse proxy uses an HTTP backend.
    cloud_tls_required = (
        get_settings().DEPLOYMENT_MODE.strip().lower() == "cloud"
    )
    secure = cloud_tls_required or origin.scheme == "https"
    response.set_cookie(COOKIE, f"{session.id}.{secret}",
                        max_age=max(0, int((session.expires_at - datetime.now(timezone.utc)).total_seconds())),
                        expires=session.expires_at, path=COOKIE_PATH,
                        httponly=True, secure=secure, samesite="strict")
    response.headers["Cache-Control"] = "no-store"


def digest(secret: str):
    return hashlib.sha256(secret.encode()).hexdigest()


def next_secret(session: BrowserSession, secret: str):
    # Deterministic only for a duplicate of the immediately preceding request.
    # The database never stores a refresh secret, including during the grace window.
    value = f"browser-refresh:{session.id}:{session.rotation}:{secret}"
    return hmac.new(get_settings().JWT_SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()


async def load_session(db, request: Request):
    value = request.cookies.get(COOKIE, "")
    sid, separator, secret = value.partition(".")
    if not separator or len(sid) != 26 or not (32 <= len(secret) <= 128):
        raise HTTPException(401, "No persistent login session")
    uid = await db.scalar(select(BrowserSession.user_id).where(BrowserSession.id == sid))
    if not uid:
        raise HTTPException(401, "Invalid persistent login session")
    # Logout/password changes and refresh serialize in User -> Session order.
    user = await db.scalar(select(User).where(User.id == uid).with_for_update())
    session = await db.scalar(select(BrowserSession).where(BrowserSession.id == sid).with_for_update())
    now = datetime.now(timezone.utc)
    if not user or not session or user.deleted_at or user.status != "active" or session.revoked_at or session.expires_at <= now or session.token_version != int(user.token_version or 0):
        raise HTTPException(401, "Persistent login session expired or revoked")
    if user.totp_enabled and not session.mfa_authenticated:
        raise HTTPException(401, "Sign in with two-factor authentication")
    supplied = digest(secret)
    if hmac.compare_digest(supplied, session.token_hash):
        return session, user, secret, False
    if session.previous_hash and hmac.compare_digest(supplied, session.previous_hash):
        if session.rotated_at and now - session.rotated_at <= ROTATION_GRACE:
            return session, user, secret, True
        # A known used secret outside the duplicate window revokes the family
        # and existing bearer tokens; an arbitrary wrong secret cannot log a user out.
        session.revoked_at = now
        user.token_version = int(user.token_version or 0) + 1
        await db.commit()
    raise HTTPException(401, "Invalid persistent login session")


async def issue_session(db, request: Request, response: Response, user: User, *, remember: bool, mfa_authenticated: bool = False):
    clear_cookie(response)
    if not request.headers.get("origin"):
        # Native/CLI clients retain the existing short bearer-token contract.
        return
    require_browser_origin(request)
    old_id, _, old_secret = request.cookies.get(COOKIE, "").partition(".")
    if old_id and old_secret:
        old = await db.scalar(select(BrowserSession).where(BrowserSession.id == old_id).with_for_update())
        if old and (hmac.compare_digest(digest(old_secret), old.token_hash) or (old.previous_hash and hmac.compare_digest(digest(old_secret), old.previous_hash))):
            old.revoked_at = datetime.now(timezone.utc)
    if not remember:
        await db.commit()
        return
    secret = secrets.token_urlsafe(32)
    session = BrowserSession(user_id=user.id, entity_id=user.entity_id,
                             token_version=int(user.token_version or 0), mfa_authenticated=mfa_authenticated,
                             token_hash=digest(secret), rotation=0,
                             expires_at=datetime.now(timezone.utc) + LIFETIME)
    db.add(session)
    await db.flush()
    await db.commit()
    set_cookie(request, response, session, secret)


async def rotate_session(db, request: Request):
    require_browser_origin(request)
    session, user, secret, duplicate = await load_session(db, request)
    if not duplicate:
        session.previous_hash = session.token_hash
        session.rotation += 1
        secret = next_secret(session, secret)
        session.token_hash = digest(secret)
        session.rotated_at = datetime.now(timezone.utc)
    else:
        secret = next_secret(session, secret)
        if not hmac.compare_digest(digest(secret), session.token_hash):
            raise HTTPException(401, "Invalid persistent login session")
    return session, user, secret
