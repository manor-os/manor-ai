---
name: mcp_email
description: Operate a generic IMAP+SMTP mailbox through the Email MCP. Use when the user asks to read, search, send, move, or delete email on a connected IMAP/SMTP account that is not Gmail or Outlook.
version: 1.0.0
---

# Email (IMAP + SMTP) Runtime Skill

Use this skill for the user's **generic IMAP/SMTP mailbox** through the Email MCP (`mcp__email__*`). For Gmail use `mcp_gmail`; for Outlook/Microsoft 365 use `mcp_outlook` — those expose richer, provider-native tools. Do not read or send mail through a browser when this account is connected.

## When To Use

Use Email when the user names a non-Gmail/non-Outlook account (work mailbox, hosting/cPanel mail, custom domain over IMAP/SMTP) and asks to triage, read, send, file, or delete messages.

## Connection

This MCP authenticates with stored IMAP/SMTP **credentials** (no OAuth). If a tool returns an auth/connection error (login failed, host unreachable, TLS error), stop and tell the user to re-enter their mailbox credentials in Integrations. Do not fall back to other mail tools or a browser.

## Core Tools

- `list_folders` — enumerate mailbox folders (IMAP has folders, not Gmail-style labels). Run this first when the user names a folder.
- `list_messages` — list messages (optionally scoped to a folder). Returns message **UIDs**, not bodies.
- `get_message` — fetch one message by `uid` (headers + body).
- `send_email` — required: `to`, `subject`, `body`. Sent via SMTP from the connected address.
- `mark_read` / `mark_unread` — by `uid`.
- `move_message` — required: `uid`, `to_folder` (e.g. move to "Archive").
- `delete_message` — by `uid`.

## Common Recipes

**Triage a folder**
1. `list_folders` if the target folder name is uncertain.
2. `list_messages` scoped to the folder → collect UIDs.
3. `get_message` per UID to read; summarize. Only `mark_read` if the user asked.

**Send a message**
1. Compose `to` / `subject` / `body`; confirm recipient + content with the user (see Guardrails).
2. `send_email`. This account has **no draft tool** — there is no staged-draft step, so confirmation must happen before the call.

**File or clean up**
1. `list_messages` with a precise scope, `get_message` to verify.
2. `move_message` to Archive/another folder (reversible), preferring it over `delete_message`.

## Guardrails

- **Confirm recipient and body before `send_email`.** There is no draft/undo step on this MCP — once sent it is gone.
- **`delete_message` behavior is server-dependent**: some IMAP servers move to a Trash folder, others expunge permanently. Treat it as possibly irreversible — prefer `move_message` to Archive, and confirm before deleting.
- Operate UID-by-UID against the folder you listed; UIDs are folder-scoped, so re-list after a `move_message`.
- Privacy: read only what the task needs; do not export mailbox contents to other tools/channels.

## Edge Cases & Errors

- `list_messages` returns **UIDs only** — you must `get_message` to read content.
- UIDs are scoped to a folder and can change after moves/expunge; don't reuse a UID across folders.
- No thread/label model here (unlike Gmail). Group conversations yourself by subject/sender from `get_message`.
- Auth/host errors → stop and ask the user to fix credentials; do not retry blindly or switch tools.
