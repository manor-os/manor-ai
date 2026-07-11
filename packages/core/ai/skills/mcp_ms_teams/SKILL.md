---
name: mcp_ms_teams
description: Operate the user's Microsoft Teams through the Microsoft Teams MCP. Use when the user asks to read or post messages in Teams channels, read or send Teams 1:1 / group chats, start a chat, create a Teams meeting link, or check/set their Teams presence.
version: 1.0.0
---

# Microsoft Teams Runtime Skill

Use this skill to operate the user's **connected Microsoft Teams** through the Teams MCP (`mcp__ms_teams__*`).

## When To Use

Use Teams when the user asks to read or post in a Teams channel, read or send a Teams chat (1:1 or group), start a new chat, create a Teams meeting link, or check/set presence.

## Connection

Authenticates via Microsoft OAuth. On an auth/scope error, stop and ask the user to reconnect. IDs are required: get `team_id` from `list_my_teams`, `channel_id` from `list_channels`, and `chat_id` from `list_chats`.

## Core Tools

Channels:
- `list_my_teams`, `list_channels` (req `team_id`), `get_channel`, `list_channel_messages` (req `team_id`,`channel_id`).
- `send_channel_message` (req `team_id`,`channel_id`,`body`), `reply_to_channel_message` (req `…`,`message_id`,`body`), `list_channel_message_replies`.

Chats (1:1 / group):
- `list_chats`, `get_chat`, `list_chat_messages` (req `chat_id`).
- `send_chat_message` (req `chat_id`,`body`), `create_chat` (req `recipients` — 1:1 if one, group if more).

Meetings & presence:
- `create_online_meeting` (req `subject`; returns a join URL), `get_online_meeting`.
- `get_my_presence`, `set_my_presence` (req `availability`).

## Common Recipes

**Post to a channel**
1. `list_my_teams` → `team_id`; `list_channels` → `channel_id`.
2. **Show the message and target channel to the user; confirm** (Guardrails).
3. `send_channel_message` — or `reply_to_channel_message` to continue a thread.

**Message a person/group**
1. `list_chats` to find an existing `chat_id`, or `create_chat` with `recipients`.
2. Confirm content + recipients, then `send_chat_message`.

**Set up a meeting**
1. Confirm subject/time/attendees. 2. `create_online_meeting` and share the returned join URL.

## Guardrails

- **Confirm content and exact target before any send** (`send_channel_message`, `reply_to_channel_message`, `send_chat_message`, `create_chat`). A channel post is visible to the whole team — verify you have the right `team_id`/`channel_id`.
- Avoid broad announcements / @-mentions to large channels unless explicitly asked.
- `set_my_presence` changes a status others see — only set it when the user asked; don't toggle presence as a side effect.
- One message per request unless the user asked for several; don't repeat-post.

## Edge Cases & Errors

- Everything is ID-addressed — resolve `team_id` / `channel_id` / `chat_id` from the list tools first; names won't work directly.
- `create_chat` with one recipient is 1:1, multiple is a group — confirm which the user intends.
- A reply needs the parent `message_id`; posting a new top-level message vs replying are different calls.
- Auth/consent errors → stop and ask the user to reconnect.
