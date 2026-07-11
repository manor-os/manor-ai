---
name: mcp_wechat_official
description: Operate a WeChat Official Account through the WeChat Official MCP. Use when the user asks to send a customer-service or template message to a follower, upload media, or read follower info on their WeChat Official Account.
version: 1.0.0
---

# WeChat Official Account Runtime Skill

Use this skill to operate a **WeChat Official Account** through the WeChat Official MCP (`mcp__wechat_official__*`). For a personal WeChat account use `mcp_wechat_personal`.

## When To Use

Use this skill to message followers (customer-service or template messages), upload media, and look up follower profiles on the user's Official Account.

## Connection

Authenticates with the Official Account's app credentials. On an auth error, stop and ask the user to reconnect. Followers are addressed by **OpenID** (`to_user` / `open_id`).

## Core Tools

Messaging:
- `send_text_message` (req `to_user`,`content`) — customer-service text (only within the allowed reply window).
- `send_image_message` (req `to_user`,`media_id`) — needs a prior `upload_media`.
- `send_template_message` (req `to_user`,`template_id`,`data`) — a **pre-approved** template.
- `upload_media` (req `media_type`,`file_url`) — upload temporary media, returns a `media_id`.

Followers:
- `get_follower_info` (req `open_id`), `list_followers` (paginated OpenIDs).

## Common Recipes

**Reply to a follower**
1. Confirm the message + recipient (`to_user` OpenID). 2. `send_text_message`.

**Send an image**
1. `upload_media` (`media_type: image`, `file_url`) → `media_id`. 2. `send_image_message` with that `media_id`.

**Send a transactional notification**
1. Use an existing approved `template_id`. 2. Fill `data` fields. 3. Confirm with the user. 4. `send_template_message`.

## Guardrails

- **WeChat enforces a customer-service messaging window** — `send_text_message` / `send_image_message` are generally only allowed within ~48h of the follower's last interaction. Outside it, only **pre-approved template messages** are permitted. Don't try to free-text a cold follower.
- **Template messages must use an approved `template_id` with correct `data`** — never invent template IDs or repurpose a template for unrelated content (violates WeChat policy).
- Confirm recipient + content before sending; these reach real users under the brand's account.
- Don't mass-message `list_followers` results — that's spam and against policy.

## Edge Cases & Errors

- Image sends need a `media_id` from `upload_media` first; media is temporary and expires — upload fresh when needed.
- "Out of messaging window" errors are policy, not bugs — switch to a template message or tell the user it can't be sent.
- Auth errors → stop and ask the user to reconnect.
