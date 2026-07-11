"""
Google Calendar MCP server — in-process MCP for Google Calendar API.

Scopes used:
  - https://www.googleapis.com/auth/calendar (full calendar access)
  - or https://www.googleapis.com/auth/calendar.events (events only)

Auth: Google OAuth access_token (from entity integration config, auto-refreshed).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_API = "https://www.googleapis.com/calendar/v3"
_MAX_CHARS = 12_000


# ── MCP Protocol ─────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    return [_tool_def(name, spec) for name, spec in _TOOLS.items()]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
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
    except Exception as e:
        logger.exception("Google Calendar MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ── Google Calendar API client ───────────────────────────────────────────────

async def _api(
    token: str,
    method: str,
    path: str,
    body: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> str:
    url = f"{_API}/{path.lstrip('/')}" if not path.startswith("http") else path
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.request(
            method, url, headers=headers,
            json=body, params=params or {},
        )

    if resp.status_code == 401:
        raise RuntimeError("Google Calendar auth failed. Reconnect Google on the Integration page.")
    if resp.status_code == 403:
        raise RuntimeError(f"Google Calendar forbidden (scope or permissions): {resp.text[:300]}")
    if resp.status_code == 404:
        raise RuntimeError("Not found.")
    if resp.status_code == 204:
        return json.dumps({"success": True})
    if not resp.is_success:
        raise RuntimeError(f"Google Calendar API error ({resp.status_code}): {resp.text[:300]}")

    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]

    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        return out[:_MAX_CHARS] + "\n… (truncated)"
    return out


# ── Tool handlers ─────────────────────────────────────────────────────────────

async def _list_events(token: str, args: Dict) -> str:
    calendar_id = args.get("calendar_id") or "primary"
    params: Dict[str, Any] = {
        "maxResults": min(int(args.get("max_results") or 10), 250),
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if args.get("time_min"):
        params["timeMin"] = args["time_min"]
    if args.get("time_max"):
        params["timeMax"] = args["time_max"]
    if args.get("query"):
        params["q"] = args["query"]
    return await _api(token, "GET", f"calendars/{calendar_id}/events", params=params)


async def _get_event(token: str, args: Dict) -> str:
    calendar_id = args.get("calendar_id") or "primary"
    return await _api(token, "GET", f"calendars/{calendar_id}/events/{args['event_id']}")


async def _create_event(token: str, args: Dict) -> str:
    calendar_id = args.get("calendar_id") or "primary"
    start_time = args["start_time"]
    end_time = args.get("end_time") or start_time

    body: Dict[str, Any] = {"summary": args["summary"]}

    # dateTime for times with T, date for all-day events
    if "T" in start_time:
        body["start"] = {"dateTime": start_time}
        body["end"] = {"dateTime": end_time}
    else:
        body["start"] = {"date": start_time}
        body["end"] = {"date": end_time}

    if args.get("description"):
        body["description"] = args["description"]
    if args.get("location"):
        body["location"] = args["location"]
    if args.get("attendees"):
        raw = args["attendees"]
        emails = raw if isinstance(raw, list) else [e.strip() for e in str(raw).split(",") if e.strip()]
        body["attendees"] = [{"email": e} for e in emails]
    if args.get("reminder_minutes") is not None:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": int(args["reminder_minutes"])}],
        }
    params = {"sendUpdates": "all"}
    if args.get("create_meet_link"):
        body["conferenceData"] = {
            "createRequest": {
                "requestId": str(args.get("conference_request_id") or f"manor-{calendar_id}-{start_time}"),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            },
        }
        params["conferenceDataVersion"] = "1"

    # sendUpdates=all so attendees actually receive the invitation email.
    return await _api(token, "POST", f"calendars/{calendar_id}/events", body, params=params)


async def _update_event(token: str, args: Dict) -> str:
    calendar_id = args.get("calendar_id") or "primary"
    body: Dict[str, Any] = {}
    if args.get("summary"):
        body["summary"] = args["summary"]
    if args.get("description"):
        body["description"] = args["description"]
    if args.get("location"):
        body["location"] = args["location"]
    if args.get("start_time"):
        st = args["start_time"]
        body["start"] = {"dateTime": st} if "T" in st else {"date": st}
    if args.get("end_time"):
        et = args["end_time"]
        body["end"] = {"dateTime": et} if "T" in et else {"date": et}
    if args.get("attendees"):
        raw = args["attendees"]
        emails = raw if isinstance(raw, list) else [e.strip() for e in str(raw).split(",") if e.strip()]
        body["attendees"] = [{"email": e} for e in emails]
    if not body:
        return "No fields to update. Provide at least one of: summary, description, location, start_time, end_time, attendees."
    return await _api(token, "PATCH", f"calendars/{calendar_id}/events/{args['event_id']}", body,
                      params={"sendUpdates": "all"})


async def _delete_event(token: str, args: Dict) -> str:
    calendar_id = args.get("calendar_id") or "primary"
    return await _api(token, "DELETE", f"calendars/{calendar_id}/events/{args['event_id']}")


async def _list_calendars(token: str, args: Dict) -> str:
    return await _api(token, "GET", "users/me/calendarList")


async def _freebusy_query(token: str, args: Dict) -> str:
    """POST /freeBusy — given a list of calendars and a time window,
    returns the busy-block ranges per calendar so the caller can find
    a slot when everyone is free.
    """
    raw = args["calendars"]
    cal_ids: List[str]
    if isinstance(raw, list):
        cal_ids = [str(x) for x in raw if x]
    else:
        cal_ids = [c.strip() for c in str(raw).split(",") if c.strip()]
    if not cal_ids:
        return "freebusy_query needs at least one calendar in `calendars`."

    body: Dict[str, Any] = {
        "timeMin": args["time_min"],
        "timeMax": args["time_max"],
        "items": [{"id": c} for c in cal_ids],
    }
    if args.get("timezone"):
        body["timeZone"] = args["timezone"]
    return await _api(token, "POST", "freeBusy", body=body)


async def _list_event_instances(token: str, args: Dict) -> str:
    """List concrete instances of a recurring event."""
    calendar_id = args.get("calendar_id") or "primary"
    params: Dict[str, Any] = {
        "maxResults": min(int(args.get("max_results") or 25), 250),
    }
    if args.get("time_min"):
        params["timeMin"] = args["time_min"]
    if args.get("time_max"):
        params["timeMax"] = args["time_max"]
    return await _api(
        token, "GET",
        f"calendars/{calendar_id}/events/{args['event_id']}/instances",
        params=params,
    )


async def _respond_to_invite(token: str, args: Dict) -> str:
    """Set the user's RSVP status on an event invite. Google's API
    requires us to PATCH the event with the full attendees array; this
    helper re-reads the event, edits the matching attendee row, and
    writes it back.
    """
    response = args["response"]
    if response not in ("accepted", "declined", "tentative", "needsAction"):
        return (
            f"response must be one of: accepted, declined, tentative, "
            f"needsAction (got {response!r})"
        )
    calendar_id = args.get("calendar_id") or "primary"
    event_id = args["event_id"]
    user_email = args.get("attendee_email")

    current = await _api(token, "GET", f"calendars/{calendar_id}/events/{event_id}")
    try:
        evt = json.loads(current)
    except Exception:
        return current

    attendees = list(evt.get("attendees") or [])
    if not user_email:
        # Default to the self attendee row (Google flags it with self=true).
        for a in attendees:
            if a.get("self"):
                user_email = a.get("email")
                break
    if not user_email:
        return (
            "Couldn't infer which attendee to update — pass attendee_email "
            "explicitly. The event has no `self` attendee row."
        )
    found = False
    for a in attendees:
        if (a.get("email") or "").lower() == user_email.lower():
            a["responseStatus"] = response
            found = True
            break
    if not found:
        attendees.append({"email": user_email, "responseStatus": response})
    return await _api(
        token, "PATCH",
        f"calendars/{calendar_id}/events/{event_id}",
        body={"attendees": attendees},
        params={"sendUpdates": "all"},
    )


async def _quick_add_event(token: str, args: Dict) -> str:
    """Natural-language event creation: "Lunch with Sarah Tomorrow 1pm"
    becomes a real calendar event."""
    calendar_id = args.get("calendar_id") or "primary"
    return await _api(
        token, "POST", f"calendars/{calendar_id}/events/quickAdd",
        params={"text": args["text"]},
    )


async def _move_event(token: str, args: Dict) -> str:
    """Move an event between calendars. Can't be used for recurring
    instances (Google API limitation)."""
    src = args.get("calendar_id") or "primary"
    return await _api(
        token, "POST",
        f"calendars/{src}/events/{args['event_id']}/move",
        params={"destination": args["destination_calendar_id"]},
    )


async def _list_event_attendees(token: str, args: Dict) -> str:
    """Convenience: pull just the attendee list + RSVP status for an
    event. (The full event payload includes them, but agents asking
    'who hasn't responded yet?' shouldn't have to parse the rest.)"""
    calendar_id = args.get("calendar_id") or "primary"
    raw = await _api(token, "GET", f"calendars/{calendar_id}/events/{args['event_id']}")
    try:
        evt = json.loads(raw)
    except Exception:
        return raw
    return json.dumps({
        "event_id": evt.get("id"),
        "summary": evt.get("summary"),
        "start": evt.get("start"),
        "attendees": [
            {
                "email": a.get("email"),
                "display_name": a.get("displayName"),
                "response_status": a.get("responseStatus"),
                "optional": a.get("optional", False),
                "organizer": a.get("organizer", False),
            }
            for a in (evt.get("attendees") or [])
        ],
    }, ensure_ascii=False, indent=2)


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string") -> Dict[str, str]:
    return {"type": type_, "description": desc}


_TOOLS: Dict[str, Dict[str, Any]] = {
    "list_events": {
        "description": "List upcoming Google Calendar events",
        "properties": {
            "calendar_id": _prop("Calendar ID (default: 'primary')"),
            "max_results": _prop("Max events to return (default: 10, max: 250)", "integer"),
            "time_min": _prop("Lower bound for event start (ISO 8601, e.g. 2026-04-17T00:00:00Z)"),
            "time_max": _prop("Upper bound for event start (ISO 8601)"),
            "query": _prop("Free-text search query to filter events"),
        },
        "required": [],
    },
    "get_event": {
        "description": "Get details of a specific calendar event",
        "properties": {
            "calendar_id": _prop("Calendar ID (default: 'primary')"),
            "event_id": _prop("Event ID"),
        },
        "required": ["event_id"],
    },
    "create_event": {
        "description": "Create a new Google Calendar event",
        "properties": {
            "calendar_id": _prop("Calendar ID (default: 'primary')"),
            "summary": _prop("Event title"),
            "start_time": _prop("Start time (ISO 8601: '2026-04-17T10:00:00-04:00' or '2026-04-17' for all-day)"),
            "end_time": _prop("End time (ISO 8601, defaults to start_time)"),
            "description": _prop("Event description/notes"),
            "location": _prop("Event location"),
            "attendees": _prop("Comma-separated email addresses of attendees"),
            "reminder_minutes": _prop("Popup reminder N minutes before event", "integer"),
        },
        "required": ["summary", "start_time"],
    },
    "update_event": {
        "description": "Update an existing Google Calendar event",
        "properties": {
            "calendar_id": _prop("Calendar ID (default: 'primary')"),
            "event_id": _prop("Event ID to update"),
            "summary": _prop("New event title"),
            "description": _prop("New event description"),
            "location": _prop("New event location"),
            "start_time": _prop("New start time (ISO 8601)"),
            "end_time": _prop("New end time (ISO 8601)"),
            "attendees": _prop("Comma-separated email addresses"),
        },
        "required": ["event_id"],
    },
    "delete_event": {
        "description": "Delete a Google Calendar event",
        "properties": {
            "calendar_id": _prop("Calendar ID (default: 'primary')"),
            "event_id": _prop("Event ID to delete"),
        },
        "required": ["event_id"],
    },
    "list_calendars": {
        "description": "List all calendars the user has access to",
        "properties": {},
        "required": [],
    },
    "freebusy_query": {
        "description": (
            "Find busy-block ranges across one or more calendars in a "
            "time window. Use this to compute free slots for scheduling. "
            "Pass calendar IDs (e.g. ['primary', 'colleague@example.com'])."
        ),
        "properties": {
            "calendars": _prop("Calendar IDs (list, or comma-separated string)"),
            "time_min": _prop("Lower time bound (ISO 8601)"),
            "time_max": _prop("Upper time bound (ISO 8601)"),
            "timezone": _prop("Optional timezone (e.g. 'America/New_York')"),
        },
        "required": ["calendars", "time_min", "time_max"],
    },
    "list_event_instances": {
        "description": (
            "Expand a recurring event into its concrete instances "
            "(occurrences). Useful when you need to update / delete a "
            "single occurrence rather than the whole series."
        ),
        "properties": {
            "calendar_id": _prop("Default: 'primary'"),
            "event_id": _prop("Recurring parent event ID"),
            "time_min": _prop("Lower bound (ISO 8601)"),
            "time_max": _prop("Upper bound (ISO 8601)"),
            "max_results": _prop("Default 25, max 250", "integer"),
        },
        "required": ["event_id"],
    },
    "respond_to_invite": {
        "description": (
            "Update RSVP status on an event the user is invited to "
            "(accepted / declined / tentative / needsAction). "
            "Defaults to the 'self' attendee row if attendee_email omitted."
        ),
        "properties": {
            "calendar_id": _prop("Default: 'primary'"),
            "event_id": _prop("Event ID"),
            "response": _prop("accepted | declined | tentative | needsAction"),
            "attendee_email": _prop("Attendee to update (default: the authenticated user)"),
        },
        "required": ["event_id", "response"],
    },
    "quick_add_event": {
        "description": (
            "Create an event from a natural-language string. Google "
            "parses things like 'Lunch with Sarah tomorrow at 1pm' into "
            "a structured event. Use create_event for explicit control."
        ),
        "properties": {
            "calendar_id": _prop("Default: 'primary'"),
            "text": _prop("Natural-language event description"),
        },
        "required": ["text"],
    },
    "move_event": {
        "description": (
            "Move an event from one calendar to another. Recurring event "
            "instances cannot be moved — move the parent instead."
        ),
        "properties": {
            "calendar_id": _prop("Source calendar (default: 'primary')"),
            "event_id": _prop("Event ID"),
            "destination_calendar_id": _prop("Target calendar ID"),
        },
        "required": ["event_id", "destination_calendar_id"],
    },
    "list_event_attendees": {
        "description": (
            "Return just the attendee list + RSVP status for an event. "
            "Useful for 'who hasn't responded yet?' queries."
        ),
        "properties": {
            "calendar_id": _prop("Default: 'primary'"),
            "event_id": _prop("Event ID"),
        },
        "required": ["event_id"],
    },
}

_HANDLERS = {
    "list_events": _list_events,
    "get_event": _get_event,
    "create_event": _create_event,
    "update_event": _update_event,
    "delete_event": _delete_event,
    "list_calendars": _list_calendars,
    "freebusy_query": _freebusy_query,
    "list_event_instances": _list_event_instances,
    "respond_to_invite": _respond_to_invite,
    "quick_add_event": _quick_add_event,
    "move_event": _move_event,
    "list_event_attendees": _list_event_attendees,
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
