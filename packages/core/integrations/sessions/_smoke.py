"""Smoke test for the M7 sessions service.

Runs end-to-end against an ephemeral in-memory sqlite DB:
  1. start_capture creates a 'pending' row
  2. finalize_capture encrypts + flips to 'active'
  3. get_active_session round-trips
  4. load_storage_state decrypts back to the original JSON
  5. mark_validated bumps validated_steps
  6. expire_session flips to 'expired' + raises on subsequent get
  7. start_capture (same label) revives the row to 'pending'

Run with: uv run python -m packages.core.integrations.sessions._smoke
"""
from __future__ import annotations

import asyncio
import os
import sys

# Force the dev (Fernet) credential backend for the in-memory smoke run.
# This must precede importing get_credential_service so the lru_cache
# never sees the production backend.
os.environ.setdefault("CREDENTIAL_BACKEND", "dev")
os.environ.setdefault(
    "DEV_CREDENTIAL_KEY", "0123456789abcdef0123456789abcdef01234567",
)

from sqlalchemy import Column, String, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import JSONB

from packages.core.credentials import Requester
from packages.core.credentials.audit import NullAuditSink
from packages.core.credentials.dev_provider import DevKeyProvider
from packages.core.credentials.service import CredentialService
from packages.core.credentials import factory as creds_factory
from packages.core.models.base import Base
from packages.core.models.integration_session import IntegrationSession
from packages.core.integrations.sessions import (
    SessionExpired,
    expire_session,
    finalize_capture,
    get_active_session,
    list_sessions,
    load_storage_state,
    mark_validated,
    start_capture,
)


def _check(cond: bool, msg: str) -> None:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {msg}")
    if not cond:
        sys.exit(1)


async def main() -> None:
    # Override factory + the package re-export so service.py picks up
    # our DevKeyProvider regardless of which module path it imports from.
    sink = NullAuditSink()
    fake_service = CredentialService(DevKeyProvider(audit_sink=sink), audit_sink=sink)
    creds_factory.get_credential_service.cache_clear()
    creds_factory.get_credential_service = lambda: fake_service  # type: ignore[assignment]
    import packages.core.credentials as _creds_pkg
    _creds_pkg.get_credential_service = lambda: fake_service  # type: ignore[assignment]

    # SQLite-friendly: replace JSONB columns with JSON for the in-memory test.
    # IntegrationSession is the only SUT model so target it directly.
    for col in IntegrationSession.__table__.columns:
        if isinstance(col.type, JSONB):
            from sqlalchemy import JSON
            col.type = JSON()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(IntegrationSession.__table__.create)

    SessionLocal = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )

    requester = Requester(kind="step", id="step_test", step_id="step_test")

    print("[case] start_capture creates pending row")
    async with SessionLocal() as db:
        cap = await start_capture(
            db,
            entity_id="ent_demo",
            provider="x",
            label="alice",
            expected_login_url="https://x.com/login",
            health_check={"url": "https://x.com/home", "expected_text": "Home"},
        )
        await db.commit()
        _check(cap.session_id is not None, "session_id assigned")
        _check(cap.expected_login_url == "https://x.com/login", "expected_login_url surfaced")

    print("\n[case] finalize_capture encrypts + flips to active")
    storage_state = {
        "cookies": [{"name": "auth", "value": "xyz", "domain": ".x.com"}],
        "origins": [{"origin": "https://x.com", "localStorage": [{"name": "k", "value": "v"}]}],
    }
    async with SessionLocal() as db:
        paired = await finalize_capture(
            db,
            session_id=cap.session_id,
            storage_state=storage_state,
            user_agent="Mozilla/5.0 manor-test",
            viewport={"width": 1280, "height": 800},
        )
        await db.commit()
        _check(paired.session_id == cap.session_id, "session_id round-trips through finalize")

    print("\n[case] get_active_session returns active row")
    async with SessionLocal() as db:
        row = await get_active_session(db, entity_id="ent_demo", provider="x", label="alice")
        _check(row.status == "active", "status flipped to active")
        _check(row.session_state_ref is not None, "ciphertext ref persisted")
        _check(row.metadata_json.get("user_agent") == "Mozilla/5.0 manor-test", "metadata persisted")

    print("\n[case] load_storage_state decrypts back to original JSON")
    async with SessionLocal() as db:
        row = await get_active_session(db, entity_id="ent_demo", provider="x", label="alice")
        decrypted = await load_storage_state(
            db, session=row, requester=requester, reason="test_decrypt",
        )
        _check(decrypted["cookies"][0]["value"] == "xyz", "cookie value round-trips")
        _check(
            decrypted["origins"][0]["localStorage"][0]["value"] == "v",
            "localStorage round-trips",
        )

    print("\n[case] mark_validated bumps validated_steps")
    async with SessionLocal() as db:
        before = (await get_active_session(db, entity_id="ent_demo", provider="x", label="alice")).validated_steps
        await mark_validated(db, session_id=cap.session_id)
        await db.commit()
        after = (await get_active_session(db, entity_id="ent_demo", provider="x", label="alice")).validated_steps
        _check(after == before + 1, f"validated_steps {before} → {after}")

    print("\n[case] expire_session flips to expired + raises on get_active")
    async with SessionLocal() as db:
        await expire_session(
            db, session_id=cap.session_id, reason="health_check_failed",
            notify_chat=False,
        )
        await db.commit()
        try:
            await get_active_session(db, entity_id="ent_demo", provider="x", label="alice")
        except SessionExpired as exc:
            _check("health_check_failed" in str(exc), "expired error carries reason")
        else:
            _check(False, "expected SessionExpired to be raised")

    print("\n[case] start_capture revives the row to pending")
    async with SessionLocal() as db:
        cap2 = await start_capture(
            db, entity_id="ent_demo", provider="x", label="alice",
        )
        await db.commit()
        _check(cap2.session_id == cap.session_id, "same row reused (no duplicate)")
        all_rows = await list_sessions(db, entity_id="ent_demo", provider="x")
        _check(len(all_rows) == 1, "only one row exists after revive")
        _check(all_rows[0].status == "pending", "revived row is pending again")
        _check(all_rows[0].expired_reason is None, "expired_reason cleared")

    print("\nSMOKE OK")


if __name__ == "__main__":
    asyncio.run(main())
