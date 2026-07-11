---
name: mcp_tiktok
description: Operate the user's TikTok creator account through the TikTok MCP. Use when the user asks to read their TikTok profile or videos, or publish a video/photo post to TikTok. For TikTok Shop (orders/products) use mcp_tiktok_shop instead.
version: 1.0.0
---

# TikTok Runtime Skill

Use this skill to operate the user's **connected TikTok creator account** through the TikTok MCP (`mcp__tiktok__*`). For the seller/commerce surface (orders, products) use `mcp_tiktok_shop`.

## When To Use

Use TikTok when the user asks to read their TikTok profile/stats or own videos, or to publish a video or photo post.

## Connection

Authenticates via TikTok OAuth (Content Posting API). On an auth/scope error, stop and ask the user to reconnect. **Before any post, call `get_creator_info`** — it returns the allowed privacy levels and posting constraints for this creator.

## Core Tools

Read:
- `get_user_info` (profile + stats), `list_videos` (own videos), `query_videos` (req `video_ids`).
- `get_creator_info` — pre-flight: allowed privacy levels, interaction/duet/stitch options.

Publish (high-impact — see Guardrails):
- `post_video` (req `video_url` — published from a public URL via PULL_FROM_URL).
- `post_photo` (req `photo_urls` — public image URLs).
- `get_publish_status` (req `publish_id`) — poll publishing progress.

## Common Recipes

**Publish a video**
1. `get_creator_info` to read allowed privacy levels + constraints.
2. **Show the caption + privacy level to the user and get approval** (Guardrails).
3. `post_video` with the public `video_url` and a valid privacy level.
4. `get_publish_status` with the returned `publish_id` until it completes.

**Report on recent videos**
1. `list_videos` → recent IDs. 2. `query_videos` for per-video metrics.

## Guardrails

- **Publishing is public and immediate. Never `post_video` / `post_photo` without showing the caption + privacy level and getting approval.**
- **Always `get_creator_info` first** — the privacy level you set must be one of the allowed values it returns; sending an invalid level fails or posts more publicly than intended.
- Media must be a **public URL** (PULL_FROM_URL) — a private/local path won't work.
- Publishing is async — confirm success via `get_publish_status`, don't assume it posted.

## Edge Cases & Errors

- A creator account may restrict privacy to e.g. self-only until verified — respect what `get_creator_info` reports rather than forcing `public`.
- `get_publish_status` may report processing/failed states — surface the real status, don't claim success early.
- Auth/scope errors → stop and ask the user to reconnect.
