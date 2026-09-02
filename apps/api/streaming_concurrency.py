"""FastAPI adapters for active SSE concurrency controls."""
from __future__ import annotations

import os

from fastapi import HTTPException

from packages.core.service_role import SERVICE_ROLE_CHAT, normalize_service_role
from packages.core.services.chat_concurrency import (
    ChatConcurrencyExceeded,
    ChatConcurrencyLease,
    acquire_chat_stream_slot,
)


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
