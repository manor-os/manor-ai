"""Lightweight client-side error capture endpoint."""
from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.deps import security
from packages.core.database import get_db
from packages.core.models.client_error import ClientErrorEvent
from packages.core.models.user import User
from packages.core.services.auth_service import decode_token, get_user_by_id


router = APIRouter(prefix="/api/v1/client-errors", tags=["client-errors"])

_MAX_EVENTS_PER_MINUTE_PER_IP = 60
_ip_buckets: dict[str, list[float]] = defaultdict(list)

_SECRET_RE = re.compile(
    r"(?i)\b("
    r"authorization|bearer|token|access_token|refresh_token|api[_-]?key|"
    r"password|secret|client_secret"
    r")\b\s*[:=]\s*([^\s&\"']+)"
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


class ClientErrorRequest(BaseModel):
    source: str = Field(default="web", max_length=40)
    level: str = Field(default="error", max_length=20)
    handled: bool = False
    name: Optional[str] = Field(default=None, max_length=120)
    message: str = Field(default="Unknown client error")
    stack: Optional[str] = None
    component_stack: Optional[str] = None
    fingerprint: Optional[str] = Field(default=None, max_length=96)
    route: Optional[str] = Field(default=None, max_length=500)
    url: Optional[str] = None
    release: Optional[str] = Field(default=None, max_length=120)
    environment: Optional[str] = Field(default=None, max_length=80)
    request_id: Optional[str] = Field(default=None, max_length=80)
    tags: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)


class ClientErrorAccepted(BaseModel):
    accepted: bool = True


@router.post("", response_model=ClientErrorAccepted, status_code=status.HTTP_202_ACCEPTED)
async def capture_client_error(
    body: ClientErrorRequest,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
):
    """Accept best-effort browser/admin UI errors.

    Auth is optional because public pages can fail before login. If a valid
    bearer token is present we attach the resolved user/entity; otherwise the
    event remains anonymous. Payloads are scrubbed and clipped server-side so
    accidental secrets or huge stacks do not become permanent logs.
    """
    ip = _client_ip(request)
    if _rate_limited(ip):
        return ClientErrorAccepted()

    user = await _optional_user(credentials, db)
    rid = getattr(request.state, "request_id", "") or body.request_id

    message = _clip(_scrub_text(body.message or "Unknown client error"), 4000)
    stack = _clip(_scrub_text(body.stack), 12000)
    component_stack = _clip(_scrub_text(body.component_stack), 8000)
    source = _bounded_choice(body.source, "web", {"web", "admin", "api", "router", "react", "window", "websocket", "manual"})
    level = _bounded_choice(body.level, "error", {"error", "warning", "info"})
    fingerprint = body.fingerprint or _fingerprint(
        source=source,
        name=body.name,
        message=message,
        stack=stack,
        route=body.route,
    )

    row = ClientErrorEvent(
        entity_id=user.entity_id if user else None,
        user_id=user.id if user else None,
        source=source,
        level=level,
        handled=body.handled,
        name=_clip(_scrub_text(body.name), 120),
        message=message,
        stack=stack,
        component_stack=component_stack,
        fingerprint=_clip(fingerprint, 96) or "unknown",
        route=_clip(_scrub_text(body.route), 500),
        url=_clip(_scrub_text(body.url), 2000),
        release=_clip(_scrub_text(body.release), 120),
        environment=_clip(_scrub_text(body.environment), 80),
        request_id=_clip(_scrub_text(rid), 80),
        tags=_scrub_json(body.tags),
        extra=_scrub_json(body.extra),
        context=_scrub_json(body.context),
        ip_address=_clip(ip, 128),
        user_agent=_clip(_scrub_text(request.headers.get("user-agent")), 1000),
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.flush()
    return ClientErrorAccepted()


async def _optional_user(
    credentials: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
) -> User | None:
    if not credentials:
        return None
    try:
        claims = decode_token(credentials.credentials)
        user_id = claims.get("sub") if claims else None
        if not user_id:
            return None
        return await get_user_by_id(db, user_id)
    except Exception:
        return None


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.time()
    bucket = _ip_buckets[ip]
    _ip_buckets[ip] = [t for t in bucket if now - t < 60]
    if len(_ip_buckets[ip]) >= _MAX_EVENTS_PER_MINUTE_PER_IP:
        return True
    _ip_buckets[ip].append(now)
    return False


def _bounded_choice(value: str | None, fallback: str, allowed: set[str]) -> str:
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in allowed else fallback


def _scrub_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = _EMAIL_RE.sub("<email>", text)
    return text.replace("\x00", "")


def _clip(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 20] + "...[truncated]"


def _scrub_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[max-depth]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clip(_scrub_text(value), 1000)
    if isinstance(value, list):
        return [_scrub_json(v, depth=depth + 1) for v in value[:50]]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, raw in list(value.items())[:80]:
            k = _clip(_scrub_text(key), 120) or "key"
            if re.search(r"(?i)(token|secret|password|api[_-]?key|authorization)", k):
                out[k] = "<redacted>"
            else:
                out[k] = _scrub_json(raw, depth=depth + 1)
        return out
    return _clip(_scrub_text(value), 1000)


def _fingerprint(
    *,
    source: str,
    name: str | None,
    message: str | None,
    stack: str | None,
    route: str | None,
) -> str:
    first_frames = "\n".join((stack or "").splitlines()[:5])
    raw = "|".join([
        source or "",
        name or "",
        (message or "")[:500],
        first_frames,
        route or "",
    ])
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:32]
