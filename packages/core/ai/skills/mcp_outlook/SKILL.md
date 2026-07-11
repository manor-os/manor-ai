---
name: mcp_outlook
description: Operate the user's connected Outlook / Microsoft 365 mailbox through the Outlook MCP. Use when the user asks to read, search, summarize, send, reply, forward, draft, categorize, file, or triage email on their Outlook / Microsoft / Office 365 account.
version: 1.0.0
---

# Outlook Mail Runtime Skill

Use this skill to operate the user's **connected Outlook / Microsoft 365 mailbox** through the Outlook MCP (`mcp__outlook__*`), backed by Microsoft Graph. For Gmail use `mcp_gmail`; for a generic IMAP account use `mcp_email`. Do not read or send Outlook mail through a browser when this account is connected.

## When To Use

Use Outlook when the user names Outlook / Microsoft / Office 365 / Hotmail / Exchange mail and asks to read, search, triage, send, reply, forward, draft, categorize, or file messages.

## Connection

Authenticates via Microsoft OAuth. On an auth/scope error (token expired, consent required), stop and ask the user to reconnect Outlook in Integrations. `get_profile` confirms the connected address before acting on "my email".

## Core Tools

Read / search:
- `list_messages` — list mail (optionally by folder/query); returns IDs.
- `get_message` — full message by `message_id`.
- `list_folders` — folder IDs (needed for `move_message`).
- `list_attachments` / `download_attachment` — attachments by `message_id` (+ `attachment_id`).

Write / send (high-impact — see Guardrails):
- `send_message` — required: `to`, `subject`, `body`.
- `reply_to_message` — required: `message_id`, `body` (keeps the conversation).
- `forward_message` — required: `message_id`, `to`.
- `create_draft` → `send_draft` — stage then send; preferred for review.

Triage / organize:
- `mark_read` / `mark_unread`, `flag_message`, `categorize` (required: `message_id`, `categories`).
- `move_message` — required: `message_id`, `destination_folder_id` (from `list_folders`).
- `create_folder`, `delete_message`, `delete_draft`, `update_draft`.

## Common Recipes

**Summarize recent inbox**
1. `list_messages` (inbox, recent) → IDs.
2. `get_message` per ID. Summarize; don't mark read unless asked.

**Reply in-thread**
1. `get_message` for full context.
2. Draft reply text, **confirm with the user** (Guardrails).
3. `reply_to_message` with `message_id` + `body`.

**File by category**
1. `list_messages` for the target set.
2. `categorize` (add `categories`) and/or `move_message` into a folder from `list_folders`.

## Guardrails

- **Never `send_message` / `reply_to_message` / `forward_message` / `send_draft` without explicit confirmation of recipients and body.** When intent is ambiguous, `create_draft` and show it first.
- **Forwarding leaks the whole thread + attachments** — confirm the recipient is intended before `forward_message`.
- `delete_message` removes from the mailbox — prefer `move_message` to a folder (reversible). Confirm bulk deletes with a count.
- Privacy: read only task-relevant mail; don't dump contents into other tools/channels.

## Edge Cases & Errors

- `list_messages` returns IDs only — `get_message` to read content.
- `move_message` needs a **folder ID**, not a name — resolve via `list_folders` first.
- Empty result ≠ error: report "no matching messages" rather than broadening the query unprompted.
- Auth/consent errors → stop and ask the user to reconnect; do not work around with a browser.
