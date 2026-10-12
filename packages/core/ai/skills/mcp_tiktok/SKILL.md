---
name: mcp_tiktok
description: Operate the user's TikTok creator account through the TikTok MCP. Use when the user asks to read their TikTok profile or videos, or publish a reviewed video to TikTok. For TikTok Shop (orders/products) use mcp_tiktok_shop instead.
version: 1.2.0
---

# TikTok Runtime Skill

Use this skill to operate the user's **connected TikTok creator account** through the TikTok MCP (`mcp__tiktok__*`). For the seller/commerce surface (orders, products) use `mcp_tiktok_shop`.

## When To Use

Use TikTok when the user asks to read their TikTok profile/stats or own videos, or to publish a video.

## Connection

Authenticates via TikTok OAuth. Content Posting API contains `video.upload` and `video.publish`; public Direct Post also requires TikTok's independent audit. Only an actual authentication or unauthorized-scope error calls for reconnecting. `get_creator_info` returns current privacy levels and posting constraints; the publication review refreshes these itself.

## Core Tools

Read:
- `get_user_info` (profile + stats), `list_videos` (own videos), `query_videos` (req `video_ids`).
- `get_creator_info` — pre-flight: allowed privacy levels, interaction/duet/stitch options.

Publish (high-impact — see Guardrails):
- `post_video` (req `video_url` — the exact authorized current-conversation video attachment, transferred via PULL_FROM_URL after its dedicated review).
- Photo publishing is unavailable until a matching photo review is implemented; do not substitute it for the video flow.
- `get_publish_status` (req `publish_id`) — poll publishing progress.

## Common Recipes

**Publish a video**
1. Use the original `uploads/chat/...` attachment reference in `video_url`. A relative `/api/v1/fs/{entity}/uploads/chat/...` reference also works. Remote video URLs must first be uploaded to the current Manor conversation; do not invent a host or copy another conversation's file.
2. Call `post_video` with the proposed caption. Manor creates its dedicated TikTok review before transferring media. The user previews the video, chooses privacy and interactions, declares any commercial content, and confirms the applicable TikTok policies and this exact post. There is no default privacy or standing consent.
3. The user's form submission resumes the exact edited call once. Do not fill consent fields for the user, replace the form with a chat acknowledgement, reconstruct the approved arguments, or retry a submitted post.
4. An initialization `publish_id` means processing, not published. Use `get_publish_status` for that same ID in a permitted subsequent read to report the actual outcome. Never initiate another upload to check status.

**Report on recent videos**
1. `list_videos` → recent IDs. 2. `query_videos` for per-video metrics.

## Guardrails

- **Only the dedicated TikTok review can authorize transfer and publication.** Chat approval, scheduled-run consent, and a standing rule cannot replace this per-post review.
- The runtime refreshes creator information, verifies duration and original source bytes, and validates the user's privacy, interaction and disclosure choices before provider initialization. A changed source or account requires fresh review.
- TikTok fetches media via **PULL_FROM_URL** on a domain verified for the TikTok app. Manor can prepare an exact current-conversation attachment as a short-lived signed URL on its configured HTTPS domain, bound to the actor, conversation and original bytes; every download rechecks access. This does not grant directory access. Arbitrary local paths and another conversation's files are not accepted. If domain verification or download access fails, stop and report that error.
- Publishing is async — confirm success via `get_publish_status`, don't assume it posted.

## Edge Cases & Errors

- `unaudited_client_can_only_post_to_private_accounts` means TikTok has not audited the application's Direct Post access. Report the separate audit requirement; do not change account privacy, reconnect repeatedly, retry unchanged, or silently publish privately.
- Respect the privacy options returned for the current creator. Never switch visibility automatically to get past an error.
- `get_publish_status` may report processing/failed states — surface the real status, don't claim success early.
- Auth/scope errors → stop and ask the user to reconnect.
