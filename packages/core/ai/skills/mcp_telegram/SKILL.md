---
name: mcp_telegram
description: Send messages and media to Telegram through the Telegram Bot MCP. Use when the user asks to send a text, photo, or document to a Telegram chat/channel/group via their bot, or to verify the bot.
version: 1.0.0
---

# Telegram Runtime Skill

Use this skill to send to **Telegram** via the user's bot through the Telegram MCP (`mcp__telegram__*`), backed by the Bot API.

## When To Use

Use Telegram when the user asks to send a message, photo, or document to a Telegram chat, group, or channel through their bot.

## Connection

Authenticates with a Telegram bot token. `get_me` verifies the token and returns the bot profile. On an auth error, stop and ask the user to fix the bot token.

## Core Tools

- `send_message` (req `content`) — text to a chat.
- `send_photo` (req `photo` — URL or file_id), `send_document` (req `document` — URL or file_id).
- `get_me` — verify the bot.
- `answer_callback_query` (req `callback_query_id`) — acknowledge an inline-keyboard tap.

## Common Recipes

**Send a notification**
1. Confirm the chat target + message with the user. 2. `send_message` with `content`.

**Send a file**
1. `send_document` (or `send_photo`) with a public URL or a known `file_id`.

## Guardrails

- **A bot can broadcast to groups/channels — confirm the target chat and the content before sending.** Don't send to a channel/group speculatively.
- Telegram Bot API has **rate limits** (notably bulk/broadcast sends) — avoid rapid repeated sends; space them out.
- Media must be a public URL or a valid `file_id`; a local path won't work.
- `answer_callback_query` is only relevant when handling an inline-keyboard interaction — don't call it otherwise.

## Edge Cases & Errors

- The bot can only message chats it's a member of / users who started it — a "chat not found" / "bot was blocked" error is expected, not a bug.
- Sending to a channel requires the bot to be an admin there.
- Auth errors (invalid token) → stop and ask the user to fix the bot token.
