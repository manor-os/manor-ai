"""FastAPI adapters for active SSE concurrency controls."""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from packages.core.service_role import SERVICE_ROLE_CHAT, normalize_service_role
from packages.core.services.chat_concurrency import (
    ChatConcurrencyExceeded,
    ChatConcurrencyLease,
    acquire_chat_stream_slot,
)

logger = logging.getLogger(__name__)

# Strong references keep a body's close alive after its response is cancelled.
_pending_body_closes: set[asyncio.Future[None]] = set()


class LeasedStreamingResponse(StreamingResponse):
    """A StreamingResponse that always closes its leased body when it ends.

    On a client disconnect Starlette cancels the response and abandons the
    body iterator. An async generator left suspended at ``yield`` runs its
    ``finally`` only once it is garbage-collected, so the lease would keep its
    slot, and the work behind the stream would keep running, until then.
    """

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                # The caller may be cancelled again at every await; the close
                # runs in a task of its own so it finishes regardless.
                closing = asyncio.ensure_future(_close_body(close))
                _pending_body_closes.add(closing)
                closing.add_done_callback(_pending_body_closes.discard)
                await asyncio.shield(closing)


async def _close_body(close) -> None:
    try:
        await close()
    except Exception:  # noqa: BLE001 - the response is already over
        logger.warning("Leased stream body close failed", exc_info=True)


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


async def acquire_chat_stream_lease(*, scope: str) -> ChatConcurrencyLease:
    if _env_bool("CHAT_STREAM_REQUIRE_CHAT_ROLE"):
        role = normalize_service_role(os.getenv("MANOR_SERVICE_ROLE"))
        if role != SERVICE_ROLE_CHAT:
            raise HTTPException(
                status_code=503,
                detail="Chat stream endpoint must be served by manor-chat",
                headers={"Retry-After": "1"},
            )

    try:
        return await acquire_chat_stream_slot(scope=scope)
    except ChatConcurrencyExceeded as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
