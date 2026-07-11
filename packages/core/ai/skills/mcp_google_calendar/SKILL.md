---
name: mcp_google_calendar
description: Read and manage the user's Google Calendar through the Google Calendar MCP. Use when the user asks to check their schedule, find a free slot, create / move / update / cancel events, or respond to a meeting invite.
version: 1.0.0
---

# Google Calendar Runtime Skill

Use this skill to read and manage the user's **connected Google Calendar** through the Google Calendar MCP (`mcp__google_calendar__*`). It is the source of truth for their schedule — do not guess availability or scrape a browser.

## When To Use

Use Calendar when the user asks about their schedule/availability, wants to schedule/reschedule/cancel something, find a free slot, or accept/decline an invite.

## Connection

Authenticates via Google OAuth. On an auth/scope error, stop and ask the user to reconnect Google Calendar. Use `list_calendars` to discover calendar IDs when the user works across multiple calendars; default to `primary` otherwise.

## Core Tools

Read:
- `list_events` — events in a window (default `primary`; pass a time range).
- `get_event` — one event by `event_id`.
- `list_event_instances` — concrete occurrences of a recurring event.
- `list_event_attendees` — attendees for an event.
- `list_calendars` — calendar IDs the user can access.
- `freebusy_query` — required: `calendars`, `time_min`, `time_max`. Use to find conflicts/free slots.

Write (high-impact — see Guardrails):
- `create_event` — required: `summary`, `start_time` (set end/attendees/timezone as needed).
- `quick_add_event` — required: `text` (natural-language event, e.g. "Lunch with Sam Friday 1pm").
- `update_event` — by `event_id` (time, title, attendees).
- `move_event` — required: `event_id`, `destination_calendar_id`.
- `delete_event` — cancel by `event_id`.
- `respond_to_invite` — required: `event_id`, `response` (`accepted` / `declined` / `tentative`).

## Common Recipes

**Find a free slot and book it**
1. `freebusy_query` over the candidate window (and all relevant `calendars`).
2. Pick a slot with no conflict; **confirm time + attendees with the user**.
3. `create_event` with explicit `start_time`/end and timezone.

**Reschedule a meeting**
1. `list_events` / `get_event` to locate it and read current attendees.
2. Confirm the new time. 3. `update_event` with the new time (attendees are re-notified).

**Respond to an invite**
1. `get_event` to read details. 2. `respond_to_invite` with `accepted` / `declined` / `tentative`.

## Guardrails

- **Confirm before `create_event`, `update_event`, `move_event`, or `delete_event`** — these notify attendees and change other people's calendars. State the exact date/time/attendees you will write.
- **Always resolve timezone explicitly.** Don't assume; if the user's timezone is unknown, ask or use the calendar's default and say which you used.
- **Check `freebusy_query` before booking** to avoid double-booking.
- `delete_event` cancels and notifies attendees — treat as high-impact; confirm first.

## Edge Cases & Errors

- Recurring events: editing the series vs one instance differs — use `list_event_instances` and be explicit about which you change.
- Multiple calendars: confirm which calendar ID you are writing to; default `primary` unless told otherwise.
- `quick_add_event` parses natural language and may misread ambiguous dates — read back what it created via `get_event`.
- Auth/scope errors → stop and ask the user to reconnect.
