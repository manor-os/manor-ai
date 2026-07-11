---
name: mcp_claude_ai_web
description: Drive the user's logged-in Claude.ai web session through the Claude.ai (web) MCP. Use only when the user specifically wants to run a prompt inside their own Claude.ai account/UI (e.g. to keep it in their Claude history), not for normal model calls.
version: 1.0.0
---

# Claude.ai (web) Runtime Skill

Use this skill to drive the user's **logged-in Claude.ai web session** via the Claude.ai Web MCP (`mcp__claude_ai_web__*`).

## When To Use

Use this **only** when the user specifically wants a prompt to run inside their own Claude.ai account/UI — e.g. to keep the conversation in their Claude history, use their Claude subscription/projects, or continue an existing Claude.ai thread. For ordinary reasoning/generation, just answer directly; do not route normal work through this web session.

## Connection

Runs against the user's logged-in Claude.ai session (browser automation). If the session is missing/expired, stop and tell the user to reconnect.

## Core Tools

- `list_chats` — recent Claude.ai conversations.
- `new_chat` — start a fresh Claude.ai conversation with a prompt.
- `continue_chat` — append a prompt to an existing conversation.

## Common Recipes

**Continue an existing thread**
1. `list_chats` → the target conversation. 2. `continue_chat` with the new prompt. 3. Return the reply.

**Start a new thread**
1. `new_chat` with the prompt. 2. Return the reply (and the new chat reference).

## Guardrails

- **Don't route normal tasks here** — it's slower, uses the user's web quota, and writes to their Claude history. Use it only when the user explicitly wants it in their Claude.ai account.
- Confirm before posting anything sensitive into the user's web account history.
- Treat returned content as the web UI's output; surface it faithfully.

## Edge Cases & Errors

- Session expired → stop and ask the user to re-log in.
- The web UI can be slow/rate-limited — report actual status rather than retrying blindly.
