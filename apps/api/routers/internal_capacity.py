"""Private workload-capacity endpoints for cluster autoscaling."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from packages.core.config import get_settings
from packages.core.services.chat_concurrency import chat_stream_local_active_count

router = APIRouter(prefix="/internal/autoscaling", include_in_schema=False)


@router.get("/chat")
async def chat_capacity() -> dict[str, int]:
    settings = get_settings()
    if (
        settings.MANOR_SERVICE_ROLE != "chat"
        or not settings.MANOR_AUTOSCALING_METRICS_ENABLED
    ):
        raise HTTPException(status_code=404)
    return {"active_streams": chat_stream_local_active_count()}
