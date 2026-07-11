"""Browser-session capture + lookup.

The capture flow is HITL by design — Manor never auto-types passwords or
solves CAPTCHAs. The operator manually signs into the target site in a
Manor-spawned Playwright window; we then dump
``page.context.storage_state()`` and stash the encrypted blob here.

  start_capture(...)         → returns SessionCapture {session_id, paired_with}
  finalize_capture(state, …) → encrypts + flips status='active'
  load_storage_state(…)      → decrypts, used by the browser adapter
  mark_validated(…)          → bump validated_steps after a successful run
  expire_session(…)          → mark expired (health-check failed) + post HITL card

The service is async-DB only and never spawns the browser itself; the
launcher lives in ``packages.worker_sdk.adapters.browser`` so the runtime
can decide whether to use real Patchright or the dry-run stub.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.credentials import Requester
from packages.core.models.integration_session import IntegrationSession

logger = logging.getLogger(__name__)


# ── Errors ───────────────────────────────────────────────────────────

class SessionNotFound(Exception):
    """No matching IntegrationSession row."""


class SessionExpired(Exception):
    """The matching session is marked expired/revoked — caller should
    trigger a re-pairing flow rather than retry."""


# ── DTOs ─────────────────────────────────────────────────────────────

@dataclass
class SessionCapture:
    """Returned by ``start_capture`` — handed to the chat card so the
    operator knows which session_id to bind their browser dump to."""

    session_id: str
    provider: str
    label: Optional[str]
    expected_login_url: Optional[str]


@dataclass
class SessionPaired:
    session_id: str
    provider: str
    captured_at: datetime


# ── Capture lifecycle ────────────────────────────────────────────────

async def start_capture(
    db: AsyncSession,
    *,
    entity_id: str,
    provider: str,
    label: Optional[str] = None,
    expected_login_url: Optional[str] = None,
    health_check: Optional[dict] = None,
    metadata: Optional[dict] = None,
) -> SessionCapture:
    """Create a new session row in 'pending' state. The operator is
    expected to complete capture (manual login + storage_state dump) and
    call ``finalize_capture`` with the resulting JSON.

    Idempotent on (entity_id, provider, label): if a row already exists
    we re-use it (status flipped to 'pending') so a re-pairing flow
    overwrites cleanly instead of accumulating dead rows.
    """
    existing = await _find_by_label(db, entity_id, provider, label)
    if existing is not None:
        existing.status = "pending"
        existing.expired_at = None
        existing.expired_reason = None
        if health_check is not None:
            existing.health_check = health_check
        if metadata is not None:
            existing.metadata_json = {**(existing.metadata_json or {}), **metadata}
        if expected_login_url is not None:
            existing.metadata_json = {
                **(existing.metadata_json or {}),
                "expected_login_url": expected_login_url,
            }
        await db.flush()
        return SessionCapture(
            session_id=existing.id,
            provider=provider,
            label=label,
            expected_login_url=expected_login_url
                or (existing.metadata_json or {}).get("expected_login_url"),
        )

    row = IntegrationSession(
        entity_id=entity_id,
        provider=provider,
        label=label,
        status="pending",
        health_check=health_check or {},
        metadata_json={
            **(metadata or {}),
            **({"expected_login_url": expected_login_url} if expected_login_url else {}),
        },
    )
    db.add(row)
    await db.flush()
    return SessionCapture(
        session_id=row.id,
        provider=provider,
        label=label,
        expected_login_url=expected_login_url,
    )


async def finalize_capture(
    db: AsyncSession,
    *,
    session_id: str,
    storage_state: dict,
    user_agent: Optional[str] = None,
    viewport: Optional[dict] = None,
) -> SessionPaired:
    """Encrypt the storage_state, persist on the row, mark active.

    Caller commits. Raises SessionNotFound if the row is missing.
    """
    from packages.core.credentials import get_credential_service

    row = await _get(db, session_id)
    creds = get_credential_service()
    creds.store_browser_session(row, storage_state)

    row.status = "active"
    row.last_validated_at = datetime.now(timezone.utc)
    row.expired_at = None
    row.expired_reason = None

    md = dict(row.metadata_json or {})
    if user_agent:
        md["user_agent"] = user_agent
    if viewport:
        md["viewport"] = viewport
    md["captured_at"] = row.last_validated_at.isoformat()
    row.metadata_json = md

    await db.flush()
    return SessionPaired(
        session_id=row.id,
        provider=row.provider,
        captured_at=row.last_validated_at,
    )


# ── Read path ────────────────────────────────────────────────────────

async def get_active_session(
    db: AsyncSession,
    *,
    entity_id: str,
    provider: str,
    label: Optional[str] = None,
) -> IntegrationSession:
    """Return the active session matching (entity, provider, label).

    Raises SessionNotFound if missing. Raises SessionExpired if found
    but status is not 'active' — caller should trigger re-pairing.
    """
    row = await _find_by_label(db, entity_id, provider, label)
    if row is None:
        raise SessionNotFound(
            f"no session for entity={entity_id} provider={provider} label={label}"
        )
    if row.status != "active":
        raise SessionExpired(
            f"session {row.id} status={row.status!r} reason={row.expired_reason!r}"
        )
    return row


async def load_storage_state(
    db: AsyncSession,
    *,
    session: IntegrationSession,
    requester: Requester,
    reason: str,
) -> dict:
    """Decrypt the storage_state JSON for handoff to the browser adapter."""
    from packages.core.credentials import get_credential_service

    creds = get_credential_service()
    return creds.lease_browser_session(session, requester=requester, reason=reason)


async def list_sessions(
    db: AsyncSession,
    *,
    entity_id: str,
    provider: Optional[str] = None,
) -> list[IntegrationSession]:
    stmt = select(IntegrationSession).where(IntegrationSession.entity_id == entity_id)
    if provider is not None:
        stmt = stmt.where(IntegrationSession.provider == provider)
    stmt = stmt.order_by(IntegrationSession.created_at.desc())
    return list((await db.execute(stmt)).scalars().all())


# ── Health / lifecycle ──────────────────────────────────────────────

async def mark_validated(
    db: AsyncSession,
    *,
    session_id: str,
) -> None:
    """Bump validated_steps + refresh last_validated_at after a successful
    handler run. Cheap UPDATE — caller commits."""
    await db.execute(
        update(IntegrationSession)
        .where(IntegrationSession.id == session_id)
        .values(
            validated_steps=IntegrationSession.validated_steps + 1,
            last_validated_at=datetime.now(timezone.utc),
        )
    )


async def expire_session(
    db: AsyncSession,
    *,
    session_id: str,
    reason: str,
    notify_chat: bool = True,
) -> None:
    """Flip the row to expired + best-effort post a HITL re-pairing card.

    Reasons we expect: 'health_check_failed', 'cookies_expired',
    'origin_changed', 'manual_revoke'.
    """
    row = await _get(db, session_id)
    row.status = "expired"
    row.expired_at = datetime.now(timezone.utc)
    row.expired_reason = reason[:200]
    await db.flush()

    if notify_chat:
        await _post_repair_card(row, reason)


async def revoke_session(
    db: AsyncSession,
    *,
    session_id: str,
    reason: str = "manual_revoke",
) -> None:
    """Permanently revoke + scrub the encrypted ref. The row stays for
    audit, but the storage_state is unrecoverable from this point."""
    row = await _get(db, session_id)
    row.status = "revoked"
    row.expired_at = datetime.now(timezone.utc)
    row.expired_reason = reason[:200]
    row.session_state_ref = None
    await db.flush()


# ── Internals ───────────────────────────────────────────────────────

async def _get(db: AsyncSession, session_id: str) -> IntegrationSession:
    row = (await db.execute(
        select(IntegrationSession).where(IntegrationSession.id == session_id)
    )).scalar_one_or_none()
    if row is None:
        raise SessionNotFound(f"session_id={session_id} not found")
    return row


async def _find_by_label(
    db: AsyncSession,
    entity_id: str,
    provider: str,
    label: Optional[str],
) -> Optional[IntegrationSession]:
    stmt = select(IntegrationSession).where(
        IntegrationSession.entity_id == entity_id,
        IntegrationSession.provider == provider,
    )
    # NULL ≠ NULL in SQL, so split the predicate on label.
    if label is None:
        stmt = stmt.where(IntegrationSession.label.is_(None))
    else:
        stmt = stmt.where(IntegrationSession.label == label)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _post_repair_card(row: IntegrationSession, reason: str) -> None:
    """Best-effort chat card prompting the operator to re-pair."""
    try:
        from packages.core.database import async_session
        from packages.core.workspace_chat import service as chat_service

        body = (
            f"🔌 **Session expired** — {row.provider}"
            f"{f' ({row.label})' if row.label else ''} needs re-pairing. "
            f"Reason: `{reason}`. "
            f"Reply `/pair {row.provider}{f' {row.label}' if row.label else ''}` "
            f"or open Settings → Integrations to re-link."
        )
        async with async_session() as db:
            await chat_service.post_message(
                db,
                entity_id=row.entity_id,
                workspace_id=None,
                body=body,
                message_kind="goal_alert",
                author_kind="system",
            )
            await db.commit()
    except Exception:
        logger.warning("repair card post failed", exc_info=True)
