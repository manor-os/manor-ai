---
name: mcp_wechat_personal
description: Reply to WeChat groups and contacts through the personal WeChat bot runner MCP. Use when the user asks to reply to a WeChat group or 1:1 chat that messaged their QR-logged-in personal WeChat bot, or to check the runner / re-scan the login QR.
version: 1.0.0
---

# WeChat (Personal) Runtime Skill

Use this skill to reply on **personal WeChat** through a QR-login bot runner via the WeChat Personal MCP (`mcp__wechat_personal__*`). For an Official Account use `mcp_wechat_official`.

## When To Use

Use this skill to **reply** to WeChat groups or 1:1 contacts that have messaged the user's bot, and to manage the runner session (status / QR login).

## Connection

This is a **QR-login runner session** of a personal account, not an API key. Check `get_bot_status` first (online / QR-pending / offline). If it's not online, call `get_qr_code` and tell the user to scan it to (re)log in — do not attempt to send until the session is online.

## Core Tools

- `get_bot_status` — session state (online / QR pending / offline).
- `get_qr_code` — fresh QR URL for the user to scan to (re)log in.
- `list_groups` — recently-seen group peers the bot can reply to.
- `list_contacts` — recently-seen 1:1 peers the bot can reply to.
- `send_group_message` (req `group_id`,`content`), `send_direct_message` (req `contact_id`,`content`).

## Common Recipes

**Reply to a group**
1. `get_bot_status` — ensure online (else `get_qr_code` → ask user to scan).
2. `list_groups` → the `group_id` of the peer that messaged. 3. Confirm the reply text. 4. `send_group_message`.

**Reply to a contact**
1. Ensure online. 2. `list_contacts` → `contact_id`. 3. Confirm text. 4. `send_direct_message`.

## Guardrails

- **This sends as the user's own personal WeChat identity — confirm content before sending.** It is the user speaking, not a labeled bot.
- **Reply-only**: only message peers that have recently messaged the bot (they appear in `list_groups` / `list_contacts`). Do not cold-message or broadcast — personal WeChat automation is easily flagged/banned.
- Keep volume human — no rapid bulk sends; space replies out to avoid the account being restricted.
- If the session is offline/QR-pending, stop and get the user to scan; never silently fail.

## Edge Cases & Errors

- A peer not in `list_groups` / `list_contacts` likely can't be replied to — don't fabricate IDs.
- QR sessions expire — re-check `get_bot_status` if a send fails and re-issue `get_qr_code` if needed.
- This is inherently less stable than an official API; surface session problems to the user rather than retrying blindly.
