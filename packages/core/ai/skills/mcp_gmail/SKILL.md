---
name: mcp_gmail
description: Operate the user's connected Gmail account through the Gmail MCP. Use when the user asks to read, search, summarize, send, reply to, draft, label, archive, or triage email on their Gmail / Google Mail account.
version: 1.0.0
---

# Gmail Runtime Skill

Use this skill to operate the user's **connected Gmail account** through the Gmail MCP (`mcp__gmail__*`). Do not use `web_search`, `web_fetch`, or a browser to read or send mail when this account is connected — these tools are the source of truth for the user's inbox.

## When To Use

Use Gmail when the user names Gmail / Google Mail, or asks to read, search, summarize, triage, send, reply, draft, label, or archive email and a Gmail integration is connected. For non-Gmail IMAP/SMTP accounts use the `email` MCP; for Outlook use the `outlook` MCP — the tool surfaces differ, do not mix them.

## Connection

If a tool returns an auth error (token expired, insufficient scope, account not connected), stop and tell the user to reconnect Gmail in Integrations. Do not fall back to scraping mail through a browser. `mcp__gmail__get_profile` is a cheap way to confirm the connected address before acting on "my email".

## Core Tools

Read / search:
- `list_messages` — search with Gmail query syntax (required: `query`, e.g. `is:unread newer_than:7d`, `from:x@y.com`). Returns IDs only.
- `get_message` — full headers + body by `message_id`.
- `list_threads` / `get_thread` — work at thread granularity for conversations.
- `download_attachment` — fetch an attachment by message + attachment id.
- `list_labels` / `get_profile` — labels and the connected address.

Write / send (high-impact — see Guardrails):
- `send_message` — required: `to`, `subject`, `body`; optional `cc`, `bcc`, `reply_to_message_id`.
- `reply_to_message` — required: `message_id`, `body` (replies in-thread).
- `create_draft` → `send_draft` — stage then send; preferred when the user should review first.

Triage:
- `mark_read` / `mark_unread`, `archive_message`, `trash_message` (reversible ~30d via `untrash_message`), `mark_spam`.
- `batch_modify` — add/remove labels across up to 1000 `message_ids` in one call; use for bulk triage instead of looping single calls.

## Common Recipes

**Summarize unread inbox**
1. `list_messages` with `query: "is:unread in:inbox newer_than:7d"`.
2. `get_message` (format `metadata` or `full`) for each returned ID.
3. Summarize; do not mark read unless the user asked.

**Reply to a specific email**
1. `list_messages` to locate it (e.g. `from:... subject:...`), then `get_message` to read full context.
2. Draft the reply text, **show it to the user, and get confirmation** (see Guardrails).
3. `reply_to_message` with that `message_id` and `body` — keeps the thread.

**Bulk triage / clean up**
1. `list_messages` with a precise query (e.g. `from:newsletter@x.com older_than:30d`).
2. Confirm the count and the action with the user.
3. `batch_modify` (e.g. add `TRASH` / remove `INBOX`) over the collected IDs — one call.

## Guardrails

- **Never send or reply without explicit user confirmation of the recipient(s) and the body.** Default to `create_draft` and show the draft when intent is ambiguous; only `send_message` / `send_draft` after the user approves.
- **Verify recipients before sending** — re-read `to`/`cc`/`bcc`; do not invent addresses. Use `bcc` for multi-recipient external sends to avoid leaking address lists.
- **Bulk operations** (`batch_modify`, mass trash) require an explicit count + confirmation. Prefer `archive_message`/`trash_message` (reversible) over `mark_spam` or hard deletes.
- **Privacy**: only read messages relevant to the task. Do not dump full inbox contents into other tools or external channels.

## Edge Cases & Errors

- `list_messages` returns **IDs only** — you must `get_message` to read content; don't assume the query result has bodies.
- Empty result ≠ error: report "no matching messages" rather than retrying with broader queries unprompted.
- Pagination: use the returned `page_token` to continue; don't raise `max_results` past 100.
- `trash_message` is reversible (~30 days) via `untrash_message`; there is no separate permanent-delete tool here — say so if the user expects hard deletion.
- Auth / scope errors → stop and ask the user to reconnect; do not work around with a browser.
