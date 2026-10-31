---
name: mcp_tiktok
description: Operate the user's TikTok creator account through the TikTok MCP. Use when the user asks to read their TikTok profile or videos, or publish reviewed video or photo posts to TikTok. For TikTok Shop (orders/products) use mcp_tiktok_shop instead.
version: 1.2.0
---

# TikTok Runtime Skill

Use this skill to operate the user's **connected TikTok creator account** through the TikTok MCP (`mcp__tiktok__*`). For the seller/commerce surface (orders, products) use `mcp_tiktok_shop`.

## When To Use

Use TikTok when the user asks to read their TikTok profile/stats or own videos, or to publish a reviewed video or photo post.

## Connection

Authenticates via TikTok OAuth. Direct Post and its status checks use `video.publish`; Manor does not request `video.upload` or send drafts to the TikTok inbox. Public Direct Post also requires TikTok's independent audit. Only an actual authentication or unauthorized-scope error calls for reconnecting. `get_creator_info` returns current privacy levels and posting constraints; the publication review refreshes these itself.

## Core Tools

Read:
- `get_user_info` (profile + stats), `list_videos` (own videos), `query_videos` (req `video_ids`).
- `get_creator_info` — pre-flight: allowed privacy levels, interaction/duet/stitch options.

Publish (high-impact — see Guardrails):
- `post_video` (req `video_url` — the exact authorized current-conversation video attachment, or `{"source_kind":"workspace_document","document_id":"..."}` for readable active-Workspace Knowledge video, transferred via PULL_FROM_URL after its dedicated review).
- `post_photo` (req `photo_urls` — one to 35 ordered exact JPEG/WebP current-conversation attachments, or readable active-Workspace Knowledge image references). It has its own photo preview and cover selection; a video review never authorizes it.
- `get_publish_status` (req `publish_id`) — poll publishing progress.

## Common Recipes

**Publish a video**
1. Use the original `uploads/chat/...` attachment reference in `video_url`. A relative `/api/v1/fs/{entity}/uploads/chat/...` reference also works. For a Knowledge video, use exactly `{"source_kind":"workspace_document","document_id":"..."}` while the active Workspace, conversation, and document read access remain valid. Remote video URLs and arbitrary paths are not supported.
2. Call `post_video` with the proposed caption. Manor creates its dedicated TikTok review before transferring media. The user previews the video, chooses privacy and interactions, declares any commercial content, and confirms the applicable TikTok policies and this exact post. There is no default privacy or standing consent.
3. The user's form submission resumes the exact edited call once. Do not fill consent fields for the user, replace the form with a chat acknowledgement, reconstruct the approved arguments, or retry a submitted post.
4. An initialization `publish_id` means processing, not published. Use `get_publish_status` for that same ID in a permitted subsequent read to report the actual outcome. Never initiate another upload to check status.

**Publish photos**
1. Pass each original `uploads/chat/...` JPEG/WebP attachment in display order through `photo_urls`; for a Knowledge image use exactly `{"source_kind":"workspace_document","document_id":"..."}`. Do not use remote URLs, downloaded copies, arbitrary paths, or substitute media.
2. The dedicated review shows every image in order. The reviewer selects the cover and explicitly chooses privacy, interaction, commercial disclosure, and applicable policy confirmations. Do not supply consent or a cover index through chat arguments.
3. The reviewed one-time call transfers each exact original byte sequence with separate short-lived signed `PULL_FROM_URL` capabilities. A changed, missing, reordered, foreign, or reused source requires a new review.

**Report on recent videos**
1. `list_videos` → recent IDs. 2. `query_videos` for per-video metrics.

## Guardrails

- **Only the dedicated TikTok review can authorize transfer and publication.** Chat approval, scheduled-run consent, and a standing rule cannot replace this per-post review. Video and photo reviews are media-type-specific.
- The runtime refreshes creator information, verifies duration and original source bytes, and validates the user's privacy, interaction and disclosure choices before provider initialization. A changed source or account requires fresh review.
- TikTok fetches media via **PULL_FROM_URL** on a domain verified for the TikTok app. Manor can prepare an exact attachment or active-Workspace Knowledge document as a short-lived signed URL on its configured HTTPS domain, bound to the actor, conversation, Workspace, source identity, and original bytes; every download rechecks access. This does not grant directory access. If domain verification or download access fails, stop and report that error.
- Publishing is async — confirm success via `get_publish_status`, don't assume it posted.

## Edge Cases & Errors

- `unaudited_client_can_only_post_to_private_accounts` means TikTok has not audited the application's Direct Post access. Report the separate audit requirement; do not change account privacy, reconnect repeatedly, retry unchanged, or silently publish privately.
- Respect the privacy options returned for the current creator. Never switch visibility automatically to get past an error.
- `get_publish_status` may report processing/failed states — surface the real status, don't claim success early.
- Auth/scope errors → stop and ask the user to reconnect.
