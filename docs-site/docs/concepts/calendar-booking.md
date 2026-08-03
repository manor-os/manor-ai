---
sidebar_position: 10
title: Calendar & Booking Links
---

# Calendar & Booking Links

Manor includes personal calendar settings and public, Calendly-style booking
links, so scheduling can flow through the same system that runs your tasks.

## Calendar Settings

Under your account settings you configure:

- **Working hours** — per-weekday time windows.
- **Booking defaults** — meeting duration (5–480 minutes), buffers before
  and after, minimum notice, and how far ahead people may book (1–365 days).
- **External calendars** — connect Google Calendar or Microsoft Calendar via
  OAuth to pull external events into availability and the daily agenda
  (`GET /api/v1/calendar-settings/events`, `GET /api/v1/calendar-settings/day`).

## Booking Links

Each booking link has a slug, a name, a duration, and a location type
(`phone`, `video`, `in_person`, `custom`, or none). The public page at
`/book/{slug}` computes available slots from your working hours, buffers,
minimum notice, and booking window — no authentication required for the
person booking.

A confirmed booking creates a **task** in Manor and notifies you, so bookings
show up in the same board, agenda, and agent context as the rest of your
work.

## API Summary

| Endpoint | Purpose |
| --- | --- |
| `GET` / `PUT /api/v1/calendar-settings` | Read / update working hours and defaults |
| `POST /api/v1/calendar-settings/booking-links`, `PUT/DELETE .../booking-links/{id}` | Manage links |
| `GET /api/v1/calendar-settings/public/booking-links/{slug}` | Public: link info + available slots |
| `POST .../public/booking-links/{slug}/book` | Public: book a slot |
| `GET /api/v1/calendar-settings/events` | External calendar events (Google / Microsoft) |
| `GET /api/v1/calendar-settings/day` | Daily agenda |
