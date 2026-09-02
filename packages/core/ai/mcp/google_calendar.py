"""
Google Calendar MCP server — in-process MCP for Google Calendar API.

Scopes used:
  - https://www.googleapis.com/auth/calendar.events for event operations
  - https://www.googleapis.com/auth/calendar.calendarlist.readonly to list
    subscribed calendars
  - https://www.googleapis.com/auth/calendar.events.freebusy for availability

The broader ``calendar`` scope is intentionally not requested.  This server
does not create/delete calendars, change calendar properties or ACLs, or
modify the user's calendar-list subscriptions.

Auth: Google OAuth access_token (from entity integration config, auto-refreshed).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_API = "https://www.googleapis.com/calendar/v3"
_MAX_CHARS = 12_000


def _path_segment(value: Any) -> str:
    """Encode a Google resource id without treating its contents as a URL."""
    return quote(str(value), safe="@")


# ── MCP Protocol ─────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    return [_tool_def(name, spec) for name, spec in _TOOLS.items()]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    token = bearer_token.strip() if isinstance(bearer_token, str) else ""
    if not token:
        return _error(
            "Google Calendar access token is missing. Reconnect Google on the Integration page."
        )

    handler = _HANDLERS.get(name)
    if not handler:
        return _error(f"Unknown tool: {name}")
    if not isinstance(arguments, dict):
        return _error("arguments must be an object")
    arguments = dict(arguments)

    spec = _TOOLS.get(name, {})
    missing = [
        p
        for p in spec.get("required", [])
        if arguments.get(p) is None
        or (isinstance(arguments.get(p), str) and not arguments[p].strip())
    ]
    if missing:
        return _error(f"Missing required params: {', '.join(missing)}")

    try:
        text = await handler(token, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        logger.exception("Google Calendar MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ── Google Calendar API client ───────────────────────────────────────────────

async def _api_json(
    token: str,
    method: str,
    path: str,
    body: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> Any:
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
        return {"success": True}
    if not resp.is_success:
        raise RuntimeError(f"Google Calendar API error ({resp.status_code}): {resp.text[:300]}")

    if not resp.text:
        return {"success": True}
    try:
        return resp.json()
    except Exception:
        return resp.text


async def _api(
    token: str,
    method: str,
    path: str,
    body: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> str:
    data = await _api_json(token, method, path, body=body, params=params)
    if isinstance(data, str):
        return data[:_MAX_CHARS]

    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        return out[:_MAX_CHARS] + "\n… (truncated)"
    return out


async def list_calendars_data(token: str) -> List[Dict[str, Any]]:
    """Return every calendar without applying the MCP text-output limit."""
    calendars: List[Dict[str, Any]] = []
    page_token = ""
    seen_tokens: set[str] = set()
    while True:
        params: Dict[str, Any] = {"maxResults": 250}
        if page_token:
            params["pageToken"] = page_token
        data = await _api_json(token, "GET", "users/me/calendarList", params=params)
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            raise RuntimeError("Google Calendar returned an invalid calendar list")
        page_items = data.get("items", [])
        if any(not isinstance(item, dict) for item in page_items):
            raise RuntimeError("Google Calendar returned an invalid calendar item")
        calendars.extend(page_items)
        next_token = str(data.get("nextPageToken") or "")
        if not next_token:
            return calendars
        if next_token in seen_tokens:
            raise RuntimeError("Google Calendar returned a repeated page token")
        seen_tokens.add(next_token)
        page_token = next_token


async def query_freebusy_data(token: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Return free/busy data without applying the MCP text-output limit."""
    raw = args["calendars"]
    if isinstance(raw, list):
        calendar_ids = [str(item) for item in raw if item]
    else:
        calendar_ids = [item.strip() for item in str(raw).split(",") if item.strip()]
    calendar_ids = list(dict.fromkeys(calendar_ids))
    if not calendar_ids:
        raise ValueError("freebusy_query needs at least one calendar in `calendars`.")

    merged: Dict[str, Any] | None = None
    merged_calendars: Dict[str, Any] = {}
    for offset in range(0, len(calendar_ids), 50):
        batch = calendar_ids[offset:offset + 50]
        body: Dict[str, Any] = {
            "timeMin": args["time_min"],
            "timeMax": args["time_max"],
            "items": [{"id": calendar_id} for calendar_id in batch],
        }
        if args.get("timezone"):
            body["timeZone"] = args["timezone"]
        data = await _api_json(token, "POST", "freeBusy", body=body)
        if not isinstance(data, dict) or not isinstance(data.get("calendars"), dict):
            raise RuntimeError("Google Calendar returned an invalid free/busy response")
        if merged is None:
            merged = {key: value for key, value in data.items() if key != "calendars"}
        merged_calendars.update(data["calendars"])

    result = merged or {}
    result["calendars"] = merged_calendars
    return result


# ── Tool handlers ─────────────────────────────────────────────────────────────

def _max_results(value: Any, *, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError("max_results must be an integer between 1 and 250")
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("max_results must be an integer between 1 and 250") from exc
    if isinstance(value, float) and value != count:
        raise ValueError("max_results must be an integer between 1 and 250")
    if count < 1:
        raise ValueError("max_results must be at least 1")
    return min(count, 250)


def _list_events_params(args: Dict) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "maxResults": _max_results(args.get("max_results"), default=10),
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if args.get("time_min"):
        params["timeMin"] = args["time_min"]
    if args.get("time_max"):
        params["timeMax"] = args["time_max"]
    if args.get("query"):
        params["q"] = args["query"]
    return params


async def list_events_data(token: str, args: Dict) -> Dict[str, Any]:
    """Return every event page without applying the MCP text-output limit."""
    calendar_id = args.get("calendar_id") or "primary"
    base_params = _list_events_params(args)
    items: List[Dict[str, Any]] = []
    metadata: Dict[str, Any] = {}
    page_token = ""
    seen_tokens: set[str] = set()
    while True:
        params = dict(base_params)
        if page_token:
            params["pageToken"] = page_token
        data = await _api_json(
            token,
            "GET",
            f"calendars/{_path_segment(calendar_id)}/events",
            params=params,
        )
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            raise RuntimeError("Google Calendar returned an invalid event list")
        page_items = data.get("items", [])
        if any(not isinstance(item, dict) for item in page_items):
            raise RuntimeError("Google Calendar returned an invalid event item")
        if not metadata:
            metadata = {
                key: value
                for key, value in data.items()
                if key not in {"items", "nextPageToken"}
            }
        items.extend(page_items)
        next_token = str(data.get("nextPageToken") or "")
        if not next_token:
            return {**metadata, "items": items}
        if next_token in seen_tokens:
            raise RuntimeError("Google Calendar returned a repeated page token")
        seen_tokens.add(next_token)
        page_token = next_token


async def _list_events(token: str, args: Dict) -> str:
    calendar_id = args.get("calendar_id") or "primary"
    return await _api(
        token,
        "GET",
        f"calendars/{_path_segment(calendar_id)}/events",
        params=_list_events_params(args),
    )


async def _get_event(token: str, args: Dict) -> str:
    calendar_id = args.get("calendar_id") or "primary"
    return await _api(
        token,
        "GET",
        f"calendars/{_path_segment(calendar_id)}/events/{_path_segment(args['event_id'])}",
    )


async def get_event_data(token: str, args: Dict) -> Dict[str, Any]:
    """Return one event without applying the MCP text-output limit."""
    calendar_id = args.get("calendar_id") or "primary"
    data = await _api_json(
        token,
        "GET",
        f"calendars/{_path_segment(calendar_id)}/events/{_path_segment(args['event_id'])}",
    )
    if not isinstance(data, dict):
        raise RuntimeError("Google Calendar returned an invalid event")
    return data


def _create_event_request(
    args: Dict,
) -> tuple[str, Dict[str, Any], Dict[str, Any]]:
    calendar_id = args.get("calendar_id") or "primary"
    start_time = args["start_time"]
    end_time = args.get("end_time") or start_time

    body: Dict[str, Any] = {"summary": args["summary"]}
    if args.get("event_id"):
        body["id"] = str(args["event_id"])

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

    return f"calendars/{_path_segment(calendar_id)}/events", body, params


async def create_event_data(token: str, args: Dict) -> Dict[str, Any]:
    """Create an event and return its full resource without MCP truncation."""
    path, body, params = _create_event_request(args)
    data = await _api_json(token, "POST", path, body=body, params=params)
    if not isinstance(data, dict):
        raise RuntimeError("Google Calendar returned an invalid created event")
    return data


async def _create_event(token: str, args: Dict) -> str:
    path, body, params = _create_event_request(args)
    # sendUpdates=all so attendees actually receive the invitation email.
    return await _api(token, "POST", path, body, params=params)


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
    return await _api(token, "PATCH", f"calendars/{_path_segment(calendar_id)}/events/{_path_segment(args['event_id'])}", body,
                      params={"sendUpdates": "all"})


async def _delete_event(token: str, args: Dict) -> str:
    calendar_id = args.get("calendar_id") or "primary"
    return await _api(token, "DELETE", f"calendars/{_path_segment(calendar_id)}/events/{_path_segment(args['event_id'])}")


async def _list_calendars(token: str, args: Dict) -> str:
    return await _api(token, "GET", "users/me/calendarList")


async def _freebusy_query(token: str, args: Dict) -> str:
    """POST /freeBusy — given a list of calendars and a time window,
    returns the busy-block ranges per calendar so the caller can find
    a slot when everyone is free.
    """
    data = await query_freebusy_data(token, args)
    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        return out[:_MAX_CHARS] + "\n… (truncated)"
    return out


async def _list_event_instances(token: str, args: Dict) -> str:
    """List concrete instances of a recurring event."""
    calendar_id = args.get("calendar_id") or "primary"
    params: Dict[str, Any] = {
        "maxResults": _max_results(args.get("max_results"), default=25),
    }
    if args.get("time_min"):
        params["timeMin"] = args["time_min"]
    if args.get("time_max"):
        params["timeMax"] = args["time_max"]
    return await _api(
        token, "GET",
        f"calendars/{_path_segment(calendar_id)}/events/{_path_segment(args['event_id'])}/instances",
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

    current = await _api(
        token,
        "GET",
        f"calendars/{_path_segment(calendar_id)}/events/{_path_segment(event_id)}",
    )
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
        f"calendars/{_path_segment(calendar_id)}/events/{_path_segment(event_id)}",
        body={"attendees": attendees},
        params={"sendUpdates": "all"},
    )


async def _quick_add_event(token: str, args: Dict) -> str:
    """Natural-language event creation: "Lunch with Sarah Tomorrow 1pm"
    becomes a real calendar event."""
    calendar_id = args.get("calendar_id") or "primary"
    return await _api(
        token, "POST", f"calendars/{_path_segment(calendar_id)}/events/quickAdd",
        params={"text": args["text"]},
    )


async def _move_event(token: str, args: Dict) -> str:
    """Move an event between calendars. Can't be used for recurring
    instances (Google API limitation)."""
    src = args.get("calendar_id") or "primary"
    return await _api(
        token, "POST",
        f"calendars/{_path_segment(src)}/events/{_path_segment(args['event_id'])}/move",
        params={"destination": args["destination_calendar_id"]},
    )


async def _list_event_attendees(token: str, args: Dict) -> str:
    """Convenience: pull just the attendee list + RSVP status for an
    event. (The full event payload includes them, but agents asking
    'who hasn't responded yet?' shouldn't have to parse the rest.)"""
    calendar_id = args.get("calendar_id") or "primary"
    raw = await _api(
        token,
        "GET",
        f"calendars/{_path_segment(calendar_id)}/events/{_path_segment(args['event_id'])}",
    )
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
            "event_id": _prop("Optional client-generated idempotency ID"),
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
