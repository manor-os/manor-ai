---
name: mcp_ms_calendar
description: Read and manage the user's Microsoft / Outlook Calendar through the Microsoft Calendar MCP. Use when the user asks to check their schedule, find a free slot or meeting time, create / update / cancel events, or accept/decline a meeting invite on their Microsoft 365 calendar.
version: 1.0.0
---

# Microsoft Calendar Runtime Skill

Use this skill to read and manage the user's **connected Microsoft (Outlook) Calendar** through the Microsoft Calendar MCP (`mcp__ms_calendar__*`). For Google Calendar use `mcp_google_calendar`. It is the source of truth for their schedule — do not guess availability.

## When To Use

Use this skill when the user asks about their Microsoft/Outlook schedule, wants to schedule/reschedule/cancel a meeting, find a free slot or a time that works for several people, or respond to an invite.

## Connection

Authenticates via Microsoft OAuth. On an auth/scope error, stop and ask the user to reconnect. Use `list_calendars` to discover calendar IDs when working across multiple calendars; default to the primary calendar otherwise.

## Core Tools

Read / availability:
- `list_events`, `get_event` (req `event_id`), `list_event_instances` (req `event_id`,`time_min`,`time_max` — expand a recurring series).
- `list_calendars`.
- `get_schedule` (req `calendars`,`time_min`,`time_max`) — free/busy across calendars (MS analog of Google free/busy).
- `find_meeting_times` (req `attendees`) — suggest slots when attendees are free.

Write (high-impact — see Guardrails):
- `create_event` (req `subject`,`start_time`), `update_event` (req `event_id`).
- `cancel_event` (req `event_id`) — **cancels and notifies attendees** (use for meetings you organize).
- `delete_event` (req `event_id`) — **deletes without notifying** attendees.
- `accept_event` / `decline_event` / `tentatively_accept_event` (req `event_id`).

## Common Recipes

**Find a time for a group and book it**
1. `find_meeting_times` with `attendees` (and duration) — or `get_schedule` to inspect free/busy.
2. Pick a slot; **confirm time + attendees with the user**.
3. `create_event` with explicit `start_time`/end and timezone.

**Reschedule**
1. `get_event` to read current time/attendees. 2. Confirm new time. 3. `update_event`.

**Respond to an invite**
1. `get_event` to read details. 2. `accept_event` / `decline_event` / `tentatively_accept_event`.

## Guardrails

- **Confirm before `create_event`, `update_event`, `cancel_event`, or `delete_event`** — they change the calendar and may notify attendees. State the exact date/time/attendees.
- **`cancel_event` vs `delete_event` differ**: `cancel_event` notifies attendees (correct for a meeting you organize); `delete_event` removes silently. Pick deliberately and tell the user which you'll use.
- **Always resolve timezone explicitly** — don't assume; say which timezone you used.
- Check `get_schedule` / `find_meeting_times` before booking to avoid conflicts.

## Edge Cases & Errors

- Recurring events: edit the series vs one occurrence differs — use `list_event_instances` and be explicit about which you change.
- Multiple calendars: confirm which calendar you're writing to; default to primary unless told.
- `find_meeting_times` returns suggestions, not a booking — you still must `create_event` after confirmation.
- Auth/consent errors → stop and ask the user to reconnect.
