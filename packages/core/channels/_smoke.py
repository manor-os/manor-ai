"""Smoke test for the M10c DM pairing flow.

Covers:
  1. create_pairing_code mints a fresh code in the right shape
  2. redeem_pairing_code creates a Channel row + marks consumed
  3. double-redeem raises PairingExpired
  4. expired code raises PairingExpired
  5. wrong channel_type raises PairingMismatch
  6. unknown code raises PairingExpired

Run with: uv run python -m packages.core.channels._smoke
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from packages.core.channels import (
    PairingExpired,
    PairingMismatch,
    create_pairing_code,
    redeem_pairing_code,
)
from packages.core.channels.pairing import _CODE_ALPHABET, CODE_LENGTH
from packages.core.models.channel_pairing import ChannelPairingCode
from packages.core.models.document import Channel


def _check(cond: bool, msg: str) -> None:
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        sys.exit(1)


async def main() -> None:
    # JSONB → JSON for sqlite.
    for tbl in (Channel.__table__, ChannelPairingCode.__table__):
        for col in tbl.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Channel.__table__.create)
        await conn.run_sync(ChannelPairingCode.__table__.create)
    SessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    print("[case] create_pairing_code mints a 6-char code from the safe alphabet")
    async with SessionLocal() as db:
        row = await create_pairing_code(
            db, entity_id="ent_x", channel_type="telegram",
            user_id="usr_a", workspace_id="ws_x", hint="alice's phone",
        )
        await db.commit()
        _check(len(row.code) == CODE_LENGTH, f"code length {CODE_LENGTH}")
        _check(all(c in _CODE_ALPHABET for c in row.code), "code uses safe alphabet")
        _check(row.expires_at > datetime.now(timezone.utc), "expires_at in the future")
        first_code = row.code

    print("\n[case] redeem creates a Channel row + marks consumed")
    async with SessionLocal() as db:
        ch = await redeem_pairing_code(
            db, code=first_code, channel_type="telegram",
            address="123456789", address_kind="chat_id",
            display_name="Alice (TG)",
        )
        await db.commit()
        _check(ch.entity_id == "ent_x", "Channel inherits entity")
        _check(ch.workspace_id == "ws_x", "Channel inherits workspace")
        _check(ch.type == "telegram", "Channel.type set")
        _check(ch.config["chat_id"] == "123456789", "address persisted")
        _check(ch.config["paired_via_code"] == first_code, "back-reference to code")

        rcode = (await db.execute(
            select(ChannelPairingCode).where(ChannelPairingCode.code == first_code)
        )).scalar_one()
        _check(rcode.consumed_at is not None, "consumed_at set")
        _check(rcode.created_channel_id == ch.id, "created_channel_id back-link")

    print("\n[case] double-redeem raises PairingExpired")
    async with SessionLocal() as db:
        try:
            await redeem_pairing_code(
                db, code=first_code, channel_type="telegram",
                address="987",
            )
            _check(False, "should have raised")
        except PairingExpired as exc:
            _check("already redeemed" in str(exc), "error reason: already redeemed")

    print("\n[case] expired code raises PairingExpired")
    async with SessionLocal() as db:
        # Mint then artificially age it.
        row = await create_pairing_code(
            db, entity_id="ent_x", channel_type="telegram",
            ttl_seconds=1,
        )
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        try:
            await redeem_pairing_code(
                db, code=row.code, channel_type="telegram", address="0",
            )
            _check(False, "should have raised on expired")
        except PairingExpired as exc:
            _check("expired" in str(exc), "error reason: expired")

    print("\n[case] wrong channel_type raises PairingMismatch")
    async with SessionLocal() as db:
        row = await create_pairing_code(
            db, entity_id="ent_x", channel_type="telegram",
        )
        await db.commit()
        try:
            await redeem_pairing_code(
                db, code=row.code, channel_type="whatsapp", address="0",
            )
            _check(False, "should have raised PairingMismatch")
        except PairingMismatch as exc:
            _check("telegram" in str(exc) and "whatsapp" in str(exc), "error names both types")

    print("\n[case] unknown code raises PairingExpired")
    async with SessionLocal() as db:
        try:
            await redeem_pairing_code(
                db, code="ZZZZZZ", channel_type="telegram", address="0",
            )
            _check(False, "should have raised")
        except PairingExpired as exc:
            _check("no such code" in str(exc), "error reason: no such code")

    print("\nSMOKE OK")


if __name__ == "__main__":
    asyncio.run(main())
