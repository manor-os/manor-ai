---
name: mcp_manor_mcp_calendar
description: Manage Manor's first-party calendar settings, booking links, working hours, stored bookings, and daily agenda through the Manor Calendar MCP.
version: 1.0.0
---

# Manor Calendar Runtime Skill

Use this skill for Manor's first-party booking and schedule layer: booking links, working hours, booking records, and the Manor day agenda that combines scheduled tasks with confirmed bookings.

## When To Use

Use Manor Calendar when the user asks to create or inspect booking links, change booking availability, view Manor's agenda, or retrieve booking details such as guest email and meeting URL.

Use Google Calendar or Microsoft Calendar MCPs instead when the user asks to create, update, cancel, or inspect events inside their external calendar account.

## Core Tools

Read:
- `get_calendar_settings` - current booking settings, working hours, defaults, links, and provider metadata.
- `list_booking_links` - public booking links scoped to the current user.
- `get_daily_agenda` - scheduled Manor tasks plus confirmed bookings for one day.
- `list_bookings` - booking records, including guest details and meeting URLs.

Write:
- `create_booking_link` - creates a public Manor booking link.
- `update_working_hours` - updates booking availability windows; omitted days keep their current settings.

## Guardrails

- Confirm before creating a public booking link or changing working hours.
- Treat Manor Calendar as the Manor scheduling layer, not the source of truth for external Google/Microsoft events.
- If a booking must also create an external calendar invite, make sure the user has connected the external provider and use the provider-specific MCP or booking flow.
- Always make timezone explicit when discussing available times.
