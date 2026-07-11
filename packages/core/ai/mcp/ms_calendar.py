"""Microsoft Calendar MCP server — in-process MCP for Microsoft Graph
``/me/calendar`` and ``/me/events``.

Scopes used:
  - Calendars.Read       — list / read events
  - Calendars.ReadWrite  — create / update / cancel
  - MailboxSettings.Read — for working hours + timezone (used by
                            findMeetingTimes)

Auth: Microsoft Graph access_token (resolved via ``_ms_auth``).

Mirrors google_calendar.py shape so an agent that already understands
"list_events / freebusy_query / respond_to_invite" works the same on
both ecosystems.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_API = "https://graph.microsoft.com/v1.0"
_MAX_CHARS = 12_000


def list_tools() -> List[Dict[str, Any]]:
    return [_tool_def(name, spec) for name, spec in _TOOLS.items()]


async def call_tool(
    name: str, arguments: Dict[str, Any], bearer_token: str,
) -> Dict[str, Any]:
    handler = _HANDLERS.get(name)
    if not handler:
        return _error(f"Unknown tool: {name}")
    spec = _TOOLS.get(name, {})
    missing = [p for p in spec.get("required", []) if arguments.get(p) in (None, "")]
    if missing:
        return _error(f"Missing required params: {', '.join(missing)}")
    try:
        text = await handler(bearer_token, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as exc:  # noqa: BLE001
        logger.exception("MS Calendar MCP tool %s failed", name)
        return _error(str(exc))


from packages.core.ai.mcp._http import mcp_err as _error  # noqa: E402, F401


# ── HTTP client ─────────────────────────────────────────────────────────────

async def _api(
    token: str, method: str, path: str,
    body: Optional[Dict] = None, params: Optional[Dict] = None,
) -> str:
    url = f"{_API}/{path.lstrip('/')}" if not path.startswith("http") else path
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.request(method, url, headers=headers, json=body, params=params or {})
    if resp.status_code == 401:
        raise RuntimeError("MS Calendar auth failed. Reconnect Microsoft on the Integration page.")
    if resp.status_code == 403:
        raise RuntimeError(f"MS Calendar forbidden: {resp.text[:300]}")
    if resp.status_code == 404:
        raise RuntimeError("Not found.")
    if resp.status_code in (202, 204):
        return json.dumps({"success": True})
    if not resp.is_success:
        raise RuntimeError(f"MS Calendar API error ({resp.status_code}): {resp.text[:300]}")
    if not resp.text:
        return json.dumps({"success": True})
    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]
    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        return out[:_MAX_CHARS] + "\n… (truncated)"
    return out


def _to_attendees(value: Any, attendee_type: str = "required") -> List[Dict[str, Any]]:
    if not value:
        return []
    raw = value if isinstance(value, list) else [e.strip() for e in str(value).split(",") if e.strip()]
    return [
        {"emailAddress": {"address": e}, "type": attendee_type}
        for e in raw
    ]


def _build_event_body(args: Dict[str, Any]) -> Dict[str, Any]:
    timezone = args.get("timezone") or "UTC"
    body: Dict[str, Any] = {
        "subject": args["subject"],
        "start": {"dateTime": args["start_time"], "timeZone": timezone},
        "end": {"dateTime": args.get("end_time") or args["start_time"], "timeZone": timezone},
    }
    if args.get("body"):
        body["body"] = {"contentType": args.get("body_type") or "html", "content": args["body"]}
    if args.get("location"):
        body["location"] = {"displayName": args["location"]}
    attendees = _to_attendees(args.get("attendees"), "required")
    optional = _to_attendees(args.get("optional_attendees"), "optional")
    if attendees or optional:
        body["attendees"] = attendees + optional
    if args.get("is_online_meeting"):
        body["isOnlineMeeting"] = True
        body["onlineMeetingProvider"] = args.get("online_meeting_provider") or "teamsForBusiness"
    if args.get("reminder_minutes_before") is not None:
        body["reminderMinutesBeforeStart"] = int(args["reminder_minutes_before"])
        body["isReminderOn"] = True
    return body


# ── Tool handlers ───────────────────────────────────────────────────────────

# Calendars

async def _list_calendars(token: str, args: Dict) -> str:
    return await _api(token, "GET", "me/calendars")


# Events

async def _list_events(token: str, args: Dict) -> str:
    """Pull events from a window. ``timeMin/timeMax`` map to Graph's
    calendarView endpoint when both are present (recommended — expands
    recurring events into their concrete instances)."""
    calendar = args.get("calendar_id")
    base = f"me/calendars/{calendar}" if calendar else "me"
    if args.get("time_min") and args.get("time_max"):
        endpoint = f"{base}/calendarView"
        params: Dict[str, Any] = {
            "startDateTime": args["time_min"],
            "endDateTime": args["time_max"],
            "$top": min(int(args.get("top") or 50), 500),
            "$orderby": "start/dateTime ASC",
        }
    else:
        endpoint = f"{base}/events"
        params = {
            "$top": min(int(args.get("top") or 25), 500),
            "$orderby": "start/dateTime ASC",
        }
    if args.get("filter"):
        params["$filter"] = args["filter"]
    if args.get("select"):
        params["$select"] = args["select"]
    return await _api(token, "GET", endpoint, params=params)


async def _get_event(token: str, args: Dict) -> str:
    return await _api(token, "GET", f"me/events/{args['event_id']}")


async def _create_event(token: str, args: Dict) -> str:
    body = _build_event_body(args)
    cal = args.get("calendar_id")
    path = f"me/calendars/{cal}/events" if cal else "me/events"
    return await _api(token, "POST", path, body=body)


async def _update_event(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {}
    timezone = args.get("timezone") or "UTC"
    if args.get("subject"):
        body["subject"] = args["subject"]
    if args.get("start_time"):
        body["start"] = {"dateTime": args["start_time"], "timeZone": timezone}
    if args.get("end_time"):
        body["end"] = {"dateTime": args["end_time"], "timeZone": timezone}
    if args.get("body"):
        body["body"] = {"contentType": args.get("body_type") or "html", "content": args["body"]}
    if args.get("location"):
        body["location"] = {"displayName": args["location"]}
    if args.get("attendees") is not None:
        body["attendees"] = _to_attendees(args["attendees"], "required")
    if not body:
        return "No fields to update."
    return await _api(token, "PATCH", f"me/events/{args['event_id']}", body=body)


async def _delete_event(token: str, args: Dict) -> str:
    return await _api(token, "DELETE", f"me/events/{args['event_id']}")


async def _cancel_event(token: str, args: Dict) -> str:
    """Cancel (with attendee notification). For events you don't own,
    use delete_event instead."""
    body: Dict[str, Any] = {}
    if args.get("comment"):
        body["comment"] = args["comment"]
    return await _api(
        token, "POST", f"me/events/{args['event_id']}/cancel", body=body,
    )


# RSVP

async def _accept_event(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {"sendResponse": bool(args.get("send_response", True))}
    if args.get("comment"):
        body["comment"] = args["comment"]
    return await _api(
        token, "POST", f"me/events/{args['event_id']}/accept", body=body,
    )


async def _decline_event(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {"sendResponse": bool(args.get("send_response", True))}
    if args.get("comment"):
        body["comment"] = args["comment"]
    return await _api(
        token, "POST", f"me/events/{args['event_id']}/decline", body=body,
    )


async def _tentatively_accept_event(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {"sendResponse": bool(args.get("send_response", True))}
    if args.get("comment"):
        body["comment"] = args["comment"]
    return await _api(
        token, "POST", f"me/events/{args['event_id']}/tentativelyAccept", body=body,
    )


# Recurring events

async def _list_event_instances(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", f"me/events/{args['event_id']}/instances",
        params={
            "startDateTime": args["time_min"],
            "endDateTime": args["time_max"],
            "$top": min(int(args.get("top") or 50), 500),
        },
    )


# Scheduling helpers

async def _get_schedule(token: str, args: Dict) -> str:
    """Free/busy data for one or more calendars (the MS analog of
    Google's freeBusy.query)."""
    raw = args["calendars"]
    cals: List[str] = raw if isinstance(raw, list) else [c.strip() for c in str(raw).split(",") if c.strip()]
    timezone = args.get("timezone") or "UTC"
    body = {
        "schedules": cals,
        "startTime": {"dateTime": args["time_min"], "timeZone": timezone},
        "endTime": {"dateTime": args["time_max"], "timeZone": timezone},
        "availabilityViewInterval": int(args.get("interval_minutes") or 60),
    }
    return await _api(token, "POST", "me/calendar/getSchedule", body=body)


async def _find_meeting_times(token: str, args: Dict) -> str:
    """High-level "find a slot when these N people are free for X min"
    helper. Microsoft Graph does the optimisation server-side and
    returns ranked candidate slots."""
    raw = args["attendees"]
    emails = raw if isinstance(raw, list) else [e.strip() for e in str(raw).split(",") if e.strip()]
    body: Dict[str, Any] = {
        "attendees": [
            {"emailAddress": {"address": e}, "type": "required"} for e in emails
        ],
        "meetingDuration": args.get("duration") or "PT30M",
        "maxCandidates": int(args.get("max_candidates") or 5),
        "isOrganizerOptional": bool(args.get("organizer_optional", False)),
    }
    if args.get("time_min") and args.get("time_max"):
        body["timeConstraint"] = {
            "timeslots": [{
                "start": {"dateTime": args["time_min"], "timeZone": args.get("timezone") or "UTC"},
                "end": {"dateTime": args["time_max"], "timeZone": args.get("timezone") or "UTC"},
            }]
        }
    return await _api(token, "POST", "me/findMeetingTimes", body=body)


# ── Tool definitions ────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string") -> Dict[str, str]:
    return {"type": type_, "description": desc}


_TOOLS: Dict[str, Dict[str, Any]] = {
    "list_calendars": {
        "description": "List the user's calendars.",
        "properties": {},
        "required": [],
    },
    "list_events": {
        "description": (
            "List events. Pass time_min + time_max for the recommended "
            "calendarView endpoint (expands recurring events). Otherwise "
            "returns the next ``top`` events in chronological order."
        ),
        "properties": {
            "calendar_id": _prop("Calendar ID (default: primary)"),
            "time_min": _prop("Window start (ISO 8601, e.g. 2026-04-17T00:00:00Z)"),
            "time_max": _prop("Window end (ISO 8601)"),
            "top": _prop("Max results (default 25-50, max 500)", "integer"),
            "filter": _prop("OData $filter expression"),
            "select": _prop("Comma-separated $select fields"),
        },
        "required": [],
    },
    "get_event": {
        "description": "Get a single event by ID.",
        "properties": {"event_id": _prop("Event ID")},
        "required": ["event_id"],
    },
    "create_event": {
        "description": "Create a new event. Set is_online_meeting=true to attach a Teams meeting link.",
        "properties": {
            "calendar_id": _prop("Calendar ID (default: primary)"),
            "subject": _prop("Event title"),
            "start_time": _prop("ISO 8601 start"),
            "end_time": _prop("ISO 8601 end (default: same as start_time)"),
            "timezone": _prop("IANA tz (default: UTC)"),
            "body": _prop("Event description (HTML or text)"),
            "body_type": _prop("html | text (default html)"),
            "location": _prop("Location display name"),
            "attendees": _prop("Required attendees — emails (list or comma-string)"),
            "optional_attendees": _prop("Optional attendees"),
            "is_online_meeting": _prop("Attach a Teams link", "boolean"),
            "online_meeting_provider": _prop("teamsForBusiness | skypeForBusiness | skypeForConsumer (default teamsForBusiness)"),
            "reminder_minutes_before": _prop("Pop-up reminder N minutes before start", "integer"),
        },
        "required": ["subject", "start_time"],
    },
    "update_event": {
        "description": "Update fields on an existing event.",
        "properties": {
            "event_id": _prop("Event ID"),
            "subject": _prop("New title"),
            "start_time": _prop("New ISO 8601 start"),
            "end_time": _prop("New ISO 8601 end"),
            "timezone": _prop("IANA tz"),
            "body": _prop("New description"),
            "body_type": _prop("html | text"),
            "location": _prop("New location"),
            "attendees": _prop("New attendees list (replaces existing)"),
        },
        "required": ["event_id"],
    },
    "delete_event": {
        "description": "Delete an event without notifying attendees. Use cancel_event when you organize the meeting.",
        "properties": {"event_id": _prop("Event ID")},
        "required": ["event_id"],
    },
    "cancel_event": {
        "description": "Cancel a meeting you organize and notify attendees.",
        "properties": {
            "event_id": _prop("Event ID"),
            "comment": _prop("Optional cancellation message"),
        },
        "required": ["event_id"],
    },
    "accept_event": {
        "description": "Accept an event invitation.",
        "properties": {
            "event_id": _prop("Event ID"),
            "comment": _prop("Optional response comment"),
            "send_response": _prop("Notify the organizer (default: true)", "boolean"),
        },
        "required": ["event_id"],
    },
    "decline_event": {
        "description": "Decline an event invitation.",
        "properties": {
            "event_id": _prop("Event ID"),
            "comment": _prop("Optional response comment"),
            "send_response": _prop("Notify the organizer (default: true)", "boolean"),
        },
        "required": ["event_id"],
    },
    "tentatively_accept_event": {
        "description": "Tentatively accept an event.",
        "properties": {
            "event_id": _prop("Event ID"),
            "comment": _prop("Optional response comment"),
            "send_response": _prop("Notify the organizer (default: true)", "boolean"),
        },
        "required": ["event_id"],
    },
    "list_event_instances": {
        "description": "Expand a recurring event into concrete occurrences within a window.",
        "properties": {
            "event_id": _prop("Recurring parent event ID"),
            "time_min": _prop("Window start (ISO 8601)"),
            "time_max": _prop("Window end (ISO 8601)"),
            "top": _prop("Max instances (default 50, max 500)", "integer"),
        },
        "required": ["event_id", "time_min", "time_max"],
    },
    "get_schedule": {
        "description": (
            "Free/busy view across one or more calendars (MS analog of "
            "Google's freeBusy.query). Returns 'free' / 'busy' / 'tentative' "
            "/ 'oof' per interval."
        ),
        "properties": {
            "calendars": _prop("Calendar emails (list or comma-string)"),
            "time_min": _prop("Window start (ISO 8601)"),
            "time_max": _prop("Window end (ISO 8601)"),
            "interval_minutes": _prop("Resolution in minutes (default 60)", "integer"),
            "timezone": _prop("IANA tz (default UTC)"),
        },
        "required": ["calendars", "time_min", "time_max"],
    },
    "find_meeting_times": {
        "description": (
            "AI-style 'find me a slot when these people are free for X minutes'. "
            "Microsoft Graph runs the optimisation server-side and returns "
            "ranked candidate slots."
        ),
        "properties": {
            "attendees": _prop("Required attendees — emails (list or comma-string)"),
            "duration": _prop("ISO 8601 duration like 'PT30M' (default 30 min)"),
            "max_candidates": _prop("Max candidate slots (default 5)", "integer"),
            "time_min": _prop("Earliest acceptable start (ISO 8601)"),
            "time_max": _prop("Latest acceptable end (ISO 8601)"),
            "timezone": _prop("IANA tz (default UTC)"),
            "organizer_optional": _prop("Allow slots without organizer (default false)", "boolean"),
        },
        "required": ["attendees"],
    },
}


_HANDLERS = {
    "list_calendars": _list_calendars,
    "list_events": _list_events,
    "get_event": _get_event,
    "create_event": _create_event,
    "update_event": _update_event,
    "delete_event": _delete_event,
    "cancel_event": _cancel_event,
    "accept_event": _accept_event,
    "decline_event": _decline_event,
    "tentatively_accept_event": _tentatively_accept_event,
    "list_event_instances": _list_event_instances,
    "get_schedule": _get_schedule,
    "find_meeting_times": _find_meeting_times,
}


def _tool_def(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": name,
        "description": spec["description"],
        "inputSchema": {
            "type": "object",
            "properties": spec.get("properties", {}),
            "required": spec.get("required", []),
        },
    }
