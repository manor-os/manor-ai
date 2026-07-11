---
name: mcp_facebook
description: Operate Facebook Pages (and linked Instagram Business accounts) through the Facebook MCP. Use when the user asks to publish a Page post / photo / video, manage Page comments, send Messenger DMs, run a live video, read Page or post insights, or publish / moderate on a linked Instagram account.
version: 1.0.0
---

# Facebook (Pages + Messenger + Instagram) Runtime Skill

Use this skill to operate **Facebook Pages the user manages** — and the Instagram Business accounts linked to them — through the Facebook MCP (`mcp__facebook__*`).

## When To Use

Use this skill to: publish/edit/delete Page posts (text, multi-photo, video), moderate Page comments, send Messenger DMs, run/inspect live video, read Page/post insights, and publish or moderate on a linked **Instagram Business** account.

## Connection

Authenticates via Facebook OAuth (Page + linked-IG permissions). Start with `list_pages` to choose the Page you act as; for Instagram use `list_instagram_accounts` to get the linked IG account. On an auth/permission error (often a missing Page role or scope), stop and ask the user to reconnect or confirm they admin the Page.

## Core Tools

Pages & posts:
- `list_pages`, `get_page`, `update_page`, `list_page_albums`.
- `create_post`, `create_multi_photo_post`, `create_video_post`, `list_posts`, `get_post`, `update_post`, `delete_post`.

Comments:
- `list_comments`, `reply_comment`, `like_comment`, `hide_comment`, `delete_comment`.

Messenger:
- `list_conversations`, `list_conversation_messages`, `send_messenger`, `send_messenger_image`, `send_typing_indicator`, `mark_seen`.

Live & insights:
- `create_live_video`, `list_live_videos`, `get_live_video`, `end_live_video`.
- `get_page_insights`, `get_post_insights`.

Instagram (two-step publish):
- `list_instagram_accounts`, `get_instagram_account`, `list_instagram_media`, `get_instagram_media`.
- `create_instagram_media` (step 1: build a media container) → `publish_instagram_media` (step 2: publish it).
- `list_instagram_comments`, `reply_instagram_comment`, `delete_instagram_comment`, `get_instagram_insights`.

## Common Recipes

**Publish a Page post**
1. `list_pages` → choose the Page. 2. **Show the post content and get approval** (Guardrails). 3. `create_post` (or `create_multi_photo_post` / `create_video_post`).

**Publish to Instagram**
1. `list_instagram_accounts` → the IG account. 2. `create_instagram_media` (container with media + caption). 3. Confirm with the user. 4. `publish_instagram_media`.

**Respond to a Messenger DM**
1. `list_conversations` → the thread; `list_conversation_messages` for context. 2. Draft a reply, confirm if sensitive. 3. `send_messenger`.

## Guardrails

- **Publishing is public/immediate. Never `create_post` / `create_*_post` / `publish_instagram_media` without showing the exact content and target Page/IG account and getting approval.**
- **Confirm which Page** you're posting as (`list_pages`) — posting to the wrong Page is a public mistake.
- Moderation: prefer `hide_comment` (reversible) over `delete_comment` (permanent). `delete_post` is permanent.
- **Messenger has a messaging window / policy** — only reply to users who messaged the Page; don't initiate marketing DMs unprompted.
- Live video (`create_live_video` / `end_live_video`) is high-visibility — confirm before starting/ending a broadcast.

## Edge Cases & Errors

- Instagram publishing is **two steps** — `create_instagram_media` only stages a container; nothing is public until `publish_instagram_media`.
- Video/IG media must be a hosted/public URL, not a local path.
- A 403 usually means the user lacks the Page role or scope, not a bug — surface that.
- Auth/permission errors → stop and ask the user to reconnect.
