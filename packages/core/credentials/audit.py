"""DB-backed audit sink.

Each decrypt/rotate/revoke writes one row to ``vault_audit_log``. We avoid
touching the main async session here — audit must not fail inside a
caller's transaction and should not couple decrypts to caller commits.

Implementation: fire-and-forget inserts on a short-lived sync engine
built from ``DATABASE_URL_SYNC``. Losing the sink is annoying but must
never block an operation; errors are logged, swallowed, and the decrypt
proceeds.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

from sqlalchemy import Engine, create_engine, text

from packages.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditEvent:
    credential_ref: Optional[str]
    action: str             # 'encrypt' | 'decrypt' | 'rotate' | 'revoke' | 'health'
    requester_kind: Optional[str]
    requester_id: Optional[str]
    step_id: Optional[str]
    reason: Optional[str]
    ttl_seconds: Optional[int] = None


class AuditSink(Protocol):
    def log(self, event: AuditEvent) -> None: ...


class NullAuditSink:
    """Drop events on the floor. Used in tests."""

    def log(self, event: AuditEvent) -> None:  # noqa: D401 — stub
        return None


class LoggingAuditSink:
    """Emit events to stdlib logging — a fallback when the DB is offline."""

    def log(self, event: AuditEvent) -> None:
        logger.info(
            "vault_audit action=%s requester=%s/%s ref=%s reason=%s",
            event.action,
            event.requester_kind,
            event.requester_id,
            (event.credential_ref or "")[:32],
            event.reason,
        )


class DBAuditSink:
    """Insert into ``vault_audit_log`` via a sync engine.

    We keep our own engine separate from the app's async engine because
    (a) async DB access from sync helpers is ugly and (b) we want audit
    to survive even if the async pool is saturated or mid-rollback.
    """

    _engine: Optional[Engine] = None

    def __init__(self, database_url_sync: Optional[str] = None):
        self._url = database_url_sync or get_settings().DATABASE_URL_SYNC

    def _get_engine(self) -> Engine:
        # Lazy init so importing this module never tries to connect.
        if DBAuditSink._engine is None:
            DBAuditSink._engine = create_engine(
                self._url,
                pool_size=2,
                max_overflow=2,
                pool_pre_ping=True,
                future=True,
            )
        return DBAuditSink._engine

    def log(self, event: AuditEvent) -> None:
        # Truncate refs — never store the full ciphertext.
        ref = (event.credential_ref or "")[:64]
        sql = text(
            """
            INSERT INTO vault_audit_log (
                credential_ref, action, requester_kind, requester_id,
                step_id, reason, ttl_seconds, occurred_at
            ) VALUES (
                :ref, :action, :req_kind, :req_id,
                :step_id, :reason, :ttl, :occurred_at
            )
            """
        )
        try:
            with self._get_engine().begin() as conn:
                conn.execute(sql, {
                    "ref": ref or None,
                    "action": event.action,
                    "req_kind": event.requester_kind,
                    "req_id": event.requester_id,
                    "step_id": event.step_id,
                    "reason": event.reason,
                    "ttl": event.ttl_seconds,
                    "occurred_at": datetime.now(timezone.utc),
                })
        except Exception as exc:  # noqa: BLE001 — audit must never raise
            logger.warning("vault audit write failed: %s", exc)
