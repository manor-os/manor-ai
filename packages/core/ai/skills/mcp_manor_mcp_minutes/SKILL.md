---
name: mcp_manor_mcp_minutes
description: Access the entity's Minutes meeting notes through the Manor Minutes MCP — search meetings, read transcripts, summaries, key points, and action items, and answer questions about a meeting.
version: 1.0.0
---

# Manor Minutes Runtime Skill

Use this skill for Manor's first-party meeting-notes layer (the Minutes app): recorded meetings, their transcripts, AI summaries, key points, action items, and meeting Q&A. All tools are scoped to the current entity; they require the entity to be subscribed to the Meeting Minutes app in Apps.

## When To Use

Use Manor Minutes when the user asks what happened in a meeting, wants a meeting summarized or quoted, needs action items or decisions from a meeting, wants meetings searched by topic, or asks a question that a past meeting likely answers ("what did we decide about pricing?").

Use Google/Microsoft Calendar MCPs for scheduling and calendar events — Minutes covers what was said in meetings, not when they happen.

## Core Tools

Discovery:
- `search_meetings` — find meetings by title or transcript content (query, limit).
- `list_recent_meetings` — the entity's most recent meetings.
- `get_meeting_stats` — aggregate counts and duration totals.

Reading one meeting (ids come from the discovery tools):
- `get_meeting_details` — title, time, duration, platform, status, participants.
- `get_summary` — AI summary, key points, and action items.
- `get_transcript` — the full transcript (can be long; prefer get_summary first).
- `get_action_items` — just the action items.
- `chat_with_meeting` — ask a focused question answered from the meeting's transcript.

## Workflow

1. Resolve the meeting first: search_meetings or list_recent_meetings, then confirm with the user if several match.
2. Prefer get_summary over get_transcript — transcripts can be very long; pull the full transcript only when the user needs verbatim content or the summary lacks the detail asked for.
3. For a specific question about one meeting, chat_with_meeting is cheaper and more precise than reading the whole transcript yourself.
4. When turning action items into Manor tasks, quote each item's task/assignee/due_date as recorded and confirm before creating tasks.

## Guardrails

- Meeting content is tenant-scoped; never imply access to another entity's meetings.
- If tools report that the Meeting Minutes app is not subscribed, tell the user to subscribe in Apps rather than retrying.
- Transcripts may contain sensitive discussion — share externally only when the user explicitly asks.
