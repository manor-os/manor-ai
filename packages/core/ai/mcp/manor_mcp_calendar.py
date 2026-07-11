"""First-party Manor Calendar MCP.

This module exposes Manor's own calendar/booking state to agents:
booking links, working hours, stored booking records, and the day agenda
that combines Manor tasks with bookings. It does not authenticate to an
external calendar provider; Google/Microsoft Calendar remain separate MCPs.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
import os
import re
from typing import Any, Callable, Dict, List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from packages.core.database import async_session
from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.models.user import User
from packages.core.services.settings_service import update_user_preferences


_PREF_KEY = "calendar_settings"
_DEFAULT_COLOR = "#4f7d75"
_LOCATION_TYPES = {"none", "phone", "video", "in_person", "custom"}
_SUPPORTED_PROVIDERS = {"", "google_calendar", "ms_calendar"}

_call_ctx: Dict[str, str] = {}


def set_call_context(ctx: Dict[str, str]) -> None:
    _call_ctx.clear()
    _call_ctx.update({k: str(v) for k, v in (ctx or {}).items() if v is not None})


def clear_call_context() -> None:
    _call_ctx.clear()


def list_tools() -> List[Dict[str, Any]]:
    return [{"name": name, **spec} for name, spec in _TOOLS.items()]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    del bearer_token
    handler = _HANDLERS.get(name)
    if not handler:
        return _error(f"Unknown tool: {name}")

    spec = _TOOLS.get(name, {}).get("inputSchema", {})
    missing = [p for p in spec.get("required", []) if arguments.get(p) in (None, "")]
    if missing:
        return _error(f"Missing required params: {', '.join(missing)}")

    try:
        data = await handler(arguments or {})
        return _ok(data)
    except Exception as exc:
        return _error(str(exc))


def _ok(data: Any) -> Dict[str, Any]:
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(data, ensure_ascii=False, indent=2, default=str),
        }],
        "structuredContent": data,
        "isError": False,
    }


def _error(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _ctx() -> tuple[str, str]:
    user_id = _call_ctx.get("user_id")
    entity_id = _call_ctx.get("entity_id")
    if not user_id or not entity_id:
        raise RuntimeError("Manor Calendar MCP requires user_id and entity_id context.")
    return user_id, entity_id


async def _load_user(db, user_id: str) -> User:
    user = (await db.execute(
        select(User).where(User.id == user_id, User.deleted_at.is_(None))
    )).scalar_one_or_none()
    if not user:
        raise RuntimeError("User not found.")
    return user


def _default_working_hours() -> list[dict[str, Any]]:
    return [
        {
            "day_of_week": day,
            "enabled": day < 5,
            "start": "09:00",
            "end": "17:00",
        }
        for day in range(7)
    ]


def _raw_settings(user: User) -> dict[str, Any]:
    raw = (user.preferences or {}).get(_PREF_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _settings(user: User) -> dict[str, Any]:
    raw = _raw_settings(user)
    raw.setdefault("provider", "")
    raw.setdefault("connection_id", None)
    raw.setdefault("default_calendar_id", "primary")
    raw.setdefault("conflict_calendar_ids", ["primary"])
    raw.setdefault("timezone", user.timezone or "UTC")
    raw.setdefault("working_hours", _default_working_hours())
    raw.setdefault("booking_defaults", {})
    raw.setdefault("booking_links", [])
    raw.setdefault("bookings", [])
    raw.setdefault("auto_create_events_from_tasks", False)
    raw.setdefault("track_task_deadlines", True)
    raw.setdefault("track_scheduled_tasks", True)

    if raw.get("provider") not in _SUPPORTED_PROVIDERS:
        raw["provider"] = ""
    if not isinstance(raw.get("conflict_calendar_ids"), list):
        raw["conflict_calendar_ids"] = ["primary"]

    defaults = _booking_defaults(raw.get("booking_defaults"))
    raw["booking_defaults"] = defaults
    raw["working_hours"] = _working_hours(raw.get("working_hours"))
    raw["booking_links"] = [
        _booking_link(item, defaults, idx)
        for idx, item in enumerate(raw.get("booking_links") or [])
        if isinstance(item, dict)
    ]
    raw["bookings"] = [
        _booking_record(item)
        for item in raw.get("bookings") or []
        if isinstance(item, dict)
    ]
    return raw


def _booking_defaults(raw: Any) -> dict[str, int]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "duration_minutes": _bounded_int(raw.get("duration_minutes"), 30, 5, 480),
        "buffer_before_minutes": _bounded_int(raw.get("buffer_before_minutes"), 0, 0, 240),
        "buffer_after_minutes": _bounded_int(raw.get("buffer_after_minutes"), 10, 0, 240),
        "min_notice_minutes": _bounded_int(raw.get("min_notice_minutes"), 120, 0, 43200),
        "rolling_window_days": _bounded_int(raw.get("rolling_window_days"), 30, 1, 365),
    }


def _working_hours(raw: Any) -> list[dict[str, Any]]:
    defaults = {item["day_of_week"]: item for item in _default_working_hours()}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                day = int(item.get("day_of_week"))
            except Exception:
                continue
            if day < 0 or day > 6:
                continue
            defaults[day] = {
                "day_of_week": day,
                "enabled": bool(item.get("enabled", defaults[day]["enabled"])),
                "start": _hhmm(item.get("start"), defaults[day]["start"]),
                "end": _hhmm(item.get("end"), defaults[day]["end"]),
            }
    return [defaults[day] for day in range(7)]


def _merge_working_hours(current: Any, updates: Any) -> list[dict[str, Any]]:
    by_day = {item["day_of_week"]: item for item in _working_hours(current)}
    if not isinstance(updates, list):
        raise ValueError("working_hours must be an array.")
    for item in updates:
        if not isinstance(item, dict):
            continue
        try:
            day = int(item.get("day_of_week"))
        except Exception:
            continue
        if day < 0 or day > 6:
            continue
        previous = by_day[day]
        by_day[day] = {
            "day_of_week": day,
            "enabled": bool(item.get("enabled", previous["enabled"])),
            "start": _hhmm(item.get("start"), previous["start"]),
            "end": _hhmm(item.get("end"), previous["end"]),
        }
    return [by_day[day] for day in range(7)]


def _booking_link(item: dict[str, Any], defaults: dict[str, int], idx: int) -> dict[str, Any]:
    name = str(item.get("name") or f"Booking link {idx + 1}").strip() or "Booking link"
    location_type = str(item.get("location_type") or "video").strip()
    if location_type not in _LOCATION_TYPES:
        location_type = "video"
    return {
        "id": str(item.get("id") or generate_ulid()),
        "slug": _slugify(item.get("slug") or name) or f"booking-{idx + 1}",
        "name": name,
        "description": _optional_str(item.get("description")),
        "duration_minutes": _bounded_int(
            item.get("duration_minutes"), defaults["duration_minutes"], 5, 480,
        ),
        "location_type": location_type,
        "location_detail": _optional_str(item.get("location_detail")),
        "calendar_id": _optional_str(item.get("calendar_id")),
        "enabled": bool(item.get("enabled", True)),
        "color": str(item.get("color") or _DEFAULT_COLOR),
        "buffer_before_minutes": _bounded_int(
            item.get("buffer_before_minutes"), defaults["buffer_before_minutes"], 0, 240,
        ),
        "buffer_after_minutes": _bounded_int(
            item.get("buffer_after_minutes"), defaults["buffer_after_minutes"], 0, 240,
        ),
        "min_notice_minutes": _bounded_int(
            item.get("min_notice_minutes"), defaults["min_notice_minutes"], 0, 43200,
        ),
        "rolling_window_days": _bounded_int(
            item.get("rolling_window_days"), defaults["rolling_window_days"], 1, 365,
        ),
        "created_at": _optional_str(item.get("created_at")),
        "updated_at": _optional_str(item.get("updated_at")),
    }


def _booking_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or generate_ulid()),
        "booking_link_id": str(item.get("booking_link_id") or ""),
        "booking_link_slug": str(item.get("booking_link_slug") or ""),
        "guest_name": str(item.get("guest_name") or ""),
        "guest_email": str(item.get("guest_email") or ""),
        "note": _optional_str(item.get("note")),
        "starts_at": str(item.get("starts_at") or ""),
        "ends_at": str(item.get("ends_at") or ""),
        "timezone": str(item.get("timezone") or "UTC"),
        "status": "cancelled" if item.get("status") == "cancelled" else "confirmed",
        "calendar_provider": _optional_str(item.get("calendar_provider")),
        "calendar_account_id": _optional_str(item.get("calendar_account_id")),
        "calendar_event_id": _optional_str(item.get("calendar_event_id")),
        "calendar_event_url": _optional_str(item.get("calendar_event_url")),
        "meeting_url": _optional_str(item.get("meeting_url")),
        "calendar_event_created": bool(item.get("calendar_event_created", False)),
        "email_sent": bool(item.get("email_sent", False)),
        "created_at": _optional_str(item.get("created_at")),
    }


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _hhmm(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if not re.match(r"^\d{2}:\d{2}$", text):
        return fallback
    try:
        hour, minute = [int(part) for part in text.split(":", 1)]
        time(hour=hour, minute=minute)
        return text
    except Exception:
        return fallback


def _slugify(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:80] or "booking"


def _unique_slug(settings: dict[str, Any], preferred: str, skip_link_id: str | None = None) -> str:
    base = _slugify(preferred)
    used = {
        str(link.get("slug"))
        for link in settings.get("booking_links") or []
        if str(link.get("id") or "") != str(skip_link_id or "")
    }
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _app_url() -> str:
    return (
        os.getenv("APP_URL")
        or os.getenv("PUBLIC_BASE_URL")
        or "http://localhost:3001"
    ).rstrip("/")


def _booking_url(user: User, slug: str) -> str:
    return f"{_app_url()}/book/u/{user.id}/{slug}"


def _settings_for_output(settings: dict[str, Any], user: User) -> dict[str, Any]:
    out = json.loads(json.dumps(settings, ensure_ascii=False, default=str))
    out["booking_links"] = [
        {**link, "url": _booking_url(user, str(link.get("slug") or ""))}
        for link in out.get("booking_links") or []
    ]
    return out


def _save_payload(settings: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(settings, ensure_ascii=False, default=str))
    payload["booking_links"] = [
        {key: value for key, value in link.items() if key != "url"}
        for link in payload.get("booking_links") or []
    ]
    return payload


async def _save_settings(db, user: User, settings: dict[str, Any]) -> dict[str, Any]:
    payload = _save_payload(settings)
    await update_user_preferences(db, user.id, {_PREF_KEY: payload})
    user.preferences = {**(user.preferences or {}), _PREF_KEY: payload}
    await db.commit()
    return payload


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _parse_date(value: Any) -> date:
    if not value:
        raise ValueError("day is required in YYYY-MM-DD format.")
    return date.fromisoformat(str(value))


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _task_start(task: Task, settings: dict[str, Any]) -> datetime | None:
    details = task.details or {}
    if settings.get("track_scheduled_tasks", True):
        scheduled = _parse_datetime(details.get("scheduled_at"))
        if scheduled:
            return scheduled
    if settings.get("track_task_deadlines", True) and task.deadline:
        return task.deadline if task.deadline.tzinfo else task.deadline.replace(tzinfo=timezone.utc)
    return None


def _booking_times(booking: dict[str, Any], default_tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    starts_at = _parse_datetime(booking.get("starts_at"))
    ends_at = _parse_datetime(booking.get("ends_at"))
    if not starts_at or not ends_at:
        return None
    return starts_at.astimezone(default_tz), ends_at.astimezone(default_tz)


async def _get_calendar_settings(args: dict[str, Any]) -> dict[str, Any]:
    user_id, _entity_id = _ctx()
    async with async_session() as db:
        user = await _load_user(db, user_id)
        settings = _settings(user)
        return {
            "settings": _settings_for_output(settings, user),
            "summary": {
                "provider": settings.get("provider") or "manor",
                "timezone": settings.get("timezone"),
                "booking_link_count": len(settings.get("booking_links") or []),
                "booking_count": len(settings.get("bookings") or []),
            },
        }


async def _list_booking_links(args: dict[str, Any]) -> dict[str, Any]:
    user_id, _entity_id = _ctx()
    async with async_session() as db:
        user = await _load_user(db, user_id)
        settings = _settings(user)
        links = _settings_for_output(settings, user)["booking_links"]
        if args.get("enabled_only"):
            links = [link for link in links if link.get("enabled")]
        return {"booking_links": links}


async def _create_booking_link(args: dict[str, Any]) -> dict[str, Any]:
    user_id, _entity_id = _ctx()
    async with async_session() as db:
        user = await _load_user(db, user_id)
        settings = _settings(user)
        defaults = settings["booking_defaults"]
        name = str(args.get("name") or "").strip()
        if not name:
            raise ValueError("name is required.")
        location_type = str(args.get("location_type") or "video").strip()
        if location_type not in _LOCATION_TYPES:
            raise ValueError("location_type must be one of: none, phone, video, in_person, custom.")
        now = _now_iso()
        link = {
            "id": generate_ulid(),
            "slug": _unique_slug(settings, args.get("slug") or name),
            "name": name,
            "description": _optional_str(args.get("description")),
            "duration_minutes": _bounded_int(
                args.get("duration_minutes"), defaults["duration_minutes"], 5, 480,
            ),
            "location_type": location_type,
            "location_detail": _optional_str(args.get("location_detail")),
            "calendar_id": _optional_str(args.get("calendar_id")),
            "enabled": bool(args.get("enabled", True)),
            "color": str(args.get("color") or _DEFAULT_COLOR),
            "buffer_before_minutes": _bounded_int(
                args.get("buffer_before_minutes"), defaults["buffer_before_minutes"], 0, 240,
            ),
            "buffer_after_minutes": _bounded_int(
                args.get("buffer_after_minutes"), defaults["buffer_after_minutes"], 0, 240,
            ),
            "min_notice_minutes": _bounded_int(
                args.get("min_notice_minutes"), defaults["min_notice_minutes"], 0, 43200,
            ),
            "rolling_window_days": _bounded_int(
                args.get("rolling_window_days"), defaults["rolling_window_days"], 1, 365,
            ),
            "created_at": now,
            "updated_at": now,
        }
        settings["booking_links"].append(link)
        await _save_settings(db, user, settings)
        return {
            "booking_link": {**link, "url": _booking_url(user, link["slug"])},
            "settings": _settings_for_output(settings, user),
        }


async def _update_working_hours(args: dict[str, Any]) -> dict[str, Any]:
    user_id, _entity_id = _ctx()
    raw = args.get("working_hours")
    if not isinstance(raw, list):
        raise ValueError("working_hours must be an array.")
    async with async_session() as db:
        user = await _load_user(db, user_id)
        settings = _settings(user)
        next_hours = _merge_working_hours(settings.get("working_hours"), raw)
        for item in next_hours:
            if item["enabled"] and item["start"] >= item["end"]:
                raise ValueError("Each enabled working-hours window must start before it ends.")
        settings["working_hours"] = next_hours
        await _save_settings(db, user, settings)
        return {"working_hours": next_hours, "settings": _settings_for_output(settings, user)}


async def _get_daily_agenda(args: dict[str, Any]) -> dict[str, Any]:
    user_id, entity_id = _ctx()
    async with async_session() as db:
        user = await _load_user(db, user_id)
        settings = _settings(user)
        tz = _tz(str(settings.get("timezone") or "UTC"))
        target = _parse_date(args.get("day")) if args.get("day") else datetime.now(tz).date()
        local_start = datetime.combine(target, time.min, tzinfo=tz)
        local_end = local_start + timedelta(days=1)
        start_utc = local_start.astimezone(timezone.utc)
        end_utc = local_end.astimezone(timezone.utc)

        tasks = (await db.execute(
            select(Task).where(Task.entity_id == entity_id)
        )).scalars().all()

        items: list[dict[str, Any]] = []
        for task in tasks:
            if isinstance(task.details, dict) and task.details.get("scheduled_job_id"):
                continue
            starts_at = _task_start(task, settings)
            if not starts_at:
                continue
            starts_utc = starts_at.astimezone(timezone.utc)
            if not (start_utc <= starts_utc < end_utc):
                continue
            duration = _duration_from_task(task)
            ends_at = starts_at + timedelta(minutes=duration) if duration else None
            items.append({
                "id": f"task:{task.id}",
                "source": "task",
                "title": task.title,
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat() if ends_at else None,
                "status": task.status,
                "priority": task.priority,
                "task_id": task.id,
                "workspace_id": task.workspace_id,
            })

        links_by_id = {str(link.get("id")): link for link in settings.get("booking_links") or []}
        for booking in settings.get("bookings") or []:
            if booking.get("status") != "confirmed":
                continue
            times = _booking_times(booking, tz)
            if not times:
                continue
            starts_local, ends_local = times
            if not (start_utc <= starts_local.astimezone(timezone.utc) < end_utc):
                continue
            link = links_by_id.get(str(booking.get("booking_link_id") or ""))
            meeting_name = (
                str(link.get("name"))
                if link
                else str(booking.get("booking_link_slug") or "Meeting").replace("-", " ").title()
            )
            items.append({
                "id": f"booking:{booking['id']}",
                "source": "booking",
                "title": f"{booking.get('guest_name') or 'Guest'} - {meeting_name}",
                "starts_at": starts_local.isoformat(),
                "ends_at": ends_local.isoformat(),
                "status": booking.get("status"),
                "booking_id": booking.get("id"),
                "booking_link_id": booking.get("booking_link_id"),
                "booking_link_slug": booking.get("booking_link_slug"),
                "guest_name": booking.get("guest_name"),
                "guest_email": booking.get("guest_email"),
                "meeting_url": booking.get("meeting_url"),
                "calendar_event_url": booking.get("calendar_event_url"),
            })

        items.sort(key=lambda item: item.get("starts_at") or "")
        return {"date": target.isoformat(), "timezone": settings.get("timezone"), "items": items}


def _duration_from_task(task: Task) -> int | None:
    if isinstance(task.details, dict) and task.details.get("duration_minutes") is not None:
        try:
            return max(1, int(task.details["duration_minutes"]))
        except Exception:
            return None
    return None


async def _list_bookings(args: dict[str, Any]) -> dict[str, Any]:
    user_id, _entity_id = _ctx()
    status = str(args.get("status") or "").strip()
    from_dt = _parse_datetime(args.get("from_datetime"))
    to_dt = _parse_datetime(args.get("to_datetime"))
    slug = str(args.get("booking_link_slug") or "").strip()
    async with async_session() as db:
        user = await _load_user(db, user_id)
        settings = _settings(user)
        bookings = []
        for booking in settings.get("bookings") or []:
            if status and booking.get("status") != status:
                continue
            if slug and booking.get("booking_link_slug") != slug:
                continue
            starts_at = _parse_datetime(booking.get("starts_at"))
            if from_dt and starts_at and starts_at.astimezone(timezone.utc) < from_dt.astimezone(timezone.utc):
                continue
            if to_dt and starts_at and starts_at.astimezone(timezone.utc) >= to_dt.astimezone(timezone.utc):
                continue
            bookings.append(booking)
        bookings.sort(key=lambda item: item.get("starts_at") or "")
        return {"bookings": bookings}


_TOOLS: dict[str, dict[str, Any]] = {
    "get_calendar_settings": {
        "description": "Read the current user's first-party Manor calendar and booking settings, including working hours, booking defaults, booking links, and connected provider metadata.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "list_booking_links": {
        "description": "List the current user's Manor booking links, with public URLs scoped to that user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "enabled_only": {
                    "type": "boolean",
                    "description": "When true, only return enabled booking links.",
                },
            },
        },
    },
    "create_booking_link": {
        "description": "Create a first-party Manor booking link for the current user. Confirm with the user before creating public links.",
        "inputSchema": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "slug": {"type": "string"},
                "description": {"type": "string"},
                "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 480},
                "location_type": {
                    "type": "string",
                    "enum": ["none", "phone", "video", "in_person", "custom"],
                },
                "location_detail": {"type": "string"},
                "calendar_id": {"type": "string"},
                "enabled": {"type": "boolean"},
                "color": {"type": "string"},
                "buffer_before_minutes": {"type": "integer", "minimum": 0, "maximum": 240},
                "buffer_after_minutes": {"type": "integer", "minimum": 0, "maximum": 240},
                "min_notice_minutes": {"type": "integer", "minimum": 0, "maximum": 43200},
                "rolling_window_days": {"type": "integer", "minimum": 1, "maximum": 365},
            },
        },
    },
    "update_working_hours": {
        "description": "Update the current user's Manor booking availability windows. Omitted days keep their existing settings. day_of_week uses 0=Monday through 6=Sunday.",
        "inputSchema": {
            "type": "object",
            "required": ["working_hours"],
            "properties": {
                "working_hours": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["day_of_week", "enabled", "start", "end"],
                        "properties": {
                            "day_of_week": {"type": "integer", "minimum": 0, "maximum": 6},
                            "enabled": {"type": "boolean"},
                            "start": {"type": "string", "description": "HH:MM in the user's timezone."},
                            "end": {"type": "string", "description": "HH:MM in the user's timezone."},
                        },
                    },
                },
            },
        },
    },
    "get_daily_agenda": {
        "description": "Return the current user's Manor day agenda by combining scheduled tasks and confirmed booking records.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "day": {"type": "string", "description": "Local date in YYYY-MM-DD format. Defaults to today."},
            },
        },
    },
    "list_bookings": {
        "description": "List stored Manor booking records for the current user. Use this to fetch booking details and meeting URLs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["confirmed", "cancelled"]},
                "from_datetime": {"type": "string", "description": "Inclusive ISO datetime lower bound."},
                "to_datetime": {"type": "string", "description": "Exclusive ISO datetime upper bound."},
                "booking_link_slug": {"type": "string"},
            },
        },
    },
}

_HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "get_calendar_settings": _get_calendar_settings,
    "list_booking_links": _list_booking_links,
    "create_booking_link": _create_booking_link,
    "update_working_hours": _update_working_hours,
    "get_daily_agenda": _get_daily_agenda,
    "list_bookings": _list_bookings,
}
