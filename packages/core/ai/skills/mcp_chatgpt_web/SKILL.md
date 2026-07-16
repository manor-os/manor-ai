---
name: mcp_chatgpt_web
description: Drive the user's logged-in ChatGPT web session through the ChatGPT (web) MCP. Use only when the user specifically wants to run a prompt inside their own ChatGPT account/UI (e.g. to keep it in their ChatGPT history), not for normal model calls.
version: 1.0.0
---

# ChatGPT (web) Runtime Skill

Use this skill to drive the user's **logged-in ChatGPT web session** via the ChatGPT Web MCP (`mcp__chatgpt_web__*`).

## When To Use

Use this **only** when the user specifically wants a prompt to run inside their own ChatGPT account/UI — to keep it in their ChatGPT history, use their plan, or continue an existing ChatGPT thread. For ordinary reasoning/generation, answer directly; don't route normal work through this web session.

## Connection

Runs against the user's logged-in ChatGPT session (browser automation). If the session is missing/expired, stop and tell the user to reconnect.

## Core Tools

- `list_chats` — recent ChatGPT conversations.
- `new_chat` — start a fresh conversation with a prompt.
- `continue_chat` — append a turn to an existing conversation.

## Common Recipes

**Continue a thread**
1. `list_chats` → the target. 2. `continue_chat` with the prompt. 3. Return the reply.

**New thread**
1. `new_chat` with the prompt. 2. Return the reply + chat reference.

## Guardrails

- **Don't route normal tasks here** — slower, uses the user's web quota, and writes to their ChatGPT history. Use only when explicitly wanted in their ChatGPT account.
- Confirm before posting sensitive content into the user's web account history.
- Surface returned content faithfully as the web UI's output.

## Edge Cases & Errors

- Session expired → stop and ask the user to re-log in.
- The web UI can be slow/rate-limited — report actual status rather than retrying blindly.
