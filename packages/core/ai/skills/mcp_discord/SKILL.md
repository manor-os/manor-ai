---
name: mcp_discord
description: Operate a Discord bot through the Discord MCP. Use when the user asks to post a message to a Discord channel, read recent channel messages, list a server's channels, or react to a message via their bot.
version: 1.0.0
---

# Discord Runtime Skill

Use this skill to operate the user's **Discord bot** through the Discord MCP (`mcp__discord__*`).

## When To Use

Use Discord when the user asks to post to a Discord channel, read recent messages, list a guild's (server's) channels, or add a reaction — via their connected bot.

## Connection

Authenticates with a Discord bot token. On an auth error, stop and ask the user to fix the bot token. The bot can only see/act in guilds it has been added to, and channels it has permission for.

## Core Tools

- `list_channels` — channels in a guild (server).
- `get_channel_messages` — recent messages from a channel.
- `send_message` — post a message to a channel.
- `add_reaction` — add a reaction emoji to a message.

## Common Recipes

**Post to a channel**
1. `list_channels` to resolve the target channel in the guild. 2. **Confirm the channel + message with the user.** 3. `send_message`.

**Catch up on a channel**
1. `get_channel_messages` for the channel. 2. Summarize.

**React to a message**
1. `get_channel_messages` to find the target message. 2. `add_reaction` with the emoji.

## Guardrails

- **A channel post is visible to everyone in the server — confirm the target channel and content before `send_message`.** Don't post speculatively.
- Avoid @everyone / @here or mass mentions unless explicitly asked.
- Keep volume human; don't flood a channel with rapid messages.
- Post only to channels the user intends; the bot may be in several guilds — verify which.

## Edge Cases & Errors

- The bot only works in guilds it's been invited to and channels where it has permission — a "missing access/permissions" error is expected, not a bug.
- Resolve the channel via `list_channels` rather than guessing a channel id.
- Reactions need a valid message reference from `get_channel_messages`.
- Auth errors (invalid bot token) → stop and ask the user to fix it.
