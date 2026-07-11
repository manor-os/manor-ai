"""Runtime-owned facade for common system utility actions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def _runtime_system_time_payload(now: datetime, *, timezone_name: str | None = None) -> dict[str, Any]:
    import zoneinfo

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    payload: dict[str, Any] = {
        "utc": now_utc.isoformat(),
        "date": now_utc.strftime("%Y-%m-%d"),
        "time": now_utc.strftime("%H:%M:%S"),
        "timezone": "UTC",
        "unix_timestamp": int(now_utc.timestamp()),
    }
    if timezone_name and timezone_name != "UTC":
        try:
            user_tz = zoneinfo.ZoneInfo(timezone_name)
            now_local = now_utc.astimezone(user_tz)
        except Exception:
            return payload
        payload["local_time"] = now_local.strftime("%Y-%m-%d %H:%M:%S")
        payload["local_timezone"] = timezone_name
    return payload


async def runtime_get_current_time_action(*, user_id: str | None = None) -> str:
    """Return UTC time plus best-effort user-local time through Runtime."""

    tz_name: str | None = None
    if user_id:
        try:
            from sqlalchemy import text

            from packages.core.database import async_session

            async with async_session() as db:
                row = await db.execute(
                    text("SELECT timezone FROM users WHERE id = :uid"),
                    {"uid": user_id},
                )
                value = row.scalar()
                tz_name = value if isinstance(value, str) else None
        except Exception:
            tz_name = None
    return json.dumps(_runtime_system_time_payload(datetime.now(timezone.utc), timezone_name=tz_name))


async def runtime_get_entity_info_action(*, entity_id: str) -> str:
    """Return basic entity metadata through the Runtime action boundary."""

    from packages.core.database import async_session
    from packages.core.services import entity_service

    async with async_session() as db:
        entity = await entity_service.get_entity(db, entity_id)
        if not entity:
            return json.dumps({"error": f"Entity {entity_id} not found"})

    return json.dumps({
        "id": entity.id,
        "name": entity.name,
        "slug": entity.slug,
        "email": entity.email,
        "phone": entity.phone,
        "address": entity.address,
        "llm_model": entity.llm_model,
        "settings": entity.settings or {},
    })
