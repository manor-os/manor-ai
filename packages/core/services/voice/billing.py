"""Call-scoped credit reservation helpers for Twilio Voice media."""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

from packages.core.database import async_session
from packages.core.services.credit_reservations import (
    consume_reservation_by_source,
    release_reservation_by_source,
    reserve_credits,
)


VOICE_RESERVATION_SOURCE_KIND = "twilio_voice_call"
VOICE_CALL_RESERVATION_CREDITS = 1

_active_voice_reservation: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("active_twilio_voice_reservation", default=None)
)


def voice_call_has_active_reservation() -> bool:
    return bool(_active_voice_reservation.get())


@contextmanager
def voice_call_reservation_scope(source_id: str | None) -> Iterator[None]:
    token = _active_voice_reservation.set(source_id or None)
    try:
        yield
    finally:
        _active_voice_reservation.reset(token)


async def reserve_voice_call_credits(
    *,
    source_id: str,
    entity_id: str,
    workspace_id: str | None,
    user_id: str | None,
    agent_id: str | None,
    source_kind: str = VOICE_RESERVATION_SOURCE_KIND,
) -> None:
    async with async_session() as db:
        await reserve_credits(
            db,
            entity_id=entity_id,
            amount_credits=VOICE_CALL_RESERVATION_CREDITS,
            source_kind=source_kind,
            source_id=source_id,
            reason="Voice call admission",
            workspace_id=workspace_id,
            user_id=user_id,
            agent_id=agent_id,
            metadata={"channel": source_kind},
        )
        await db.commit()


async def settle_voice_call_credits(
    *,
    source_id: str,
    provider_started: bool,
    source_kind: str = VOICE_RESERVATION_SOURCE_KIND,
) -> None:
    async with async_session() as db:
        if provider_started:
            reservation = await consume_reservation_by_source(
                db,
                source_kind=source_kind,
                source_id=source_id,
            )
        else:
            reservation = await release_reservation_by_source(
                db,
                source_kind=source_kind,
                source_id=source_id,
                reason="Voice provider did not start",
            )
        if reservation is None:
            raise RuntimeError("Twilio Voice credit reservation is missing")
        await db.commit()
