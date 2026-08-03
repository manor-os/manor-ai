---
name: mcp_youtube
description: Operate a connected YouTube account through the YouTube MCP. Use for video or channel search, video details, comments, caption-track metadata, owned-video metadata updates, ratings, and playlists. Do not use this MCP guidance for uploading a new video file or Studio-only visibility and scheduling controls; hand those operations to a verified Chrome route.
version: 1.2.0
---

# YouTube Runtime Skill

Use this skill to operate **YouTube** through the YouTube MCP (`mcp__youtube__*`) — public search/read plus authenticated actions on the user's own channel.

## Connection

Authenticate through Google OAuth with the YouTube scopes supplied by the Integration. On an auth or scope error, stop and ask the user to reconnect. Call `get_channel(mine=true)` for the authenticated user's channel; never imply that an empty `get_channel()` call selects it.

## Parallel Route Boundary

- This built-in Skill is the default guidance for the YouTube MCP. It is not a Marketplace install and appears only when the YouTube MCP surface is connectable.
- The YouTube MCP and Manor local Chrome are parallel capability routes. Use this MCP only when its current tools support the requested operation; otherwise hand control back to the parent workflow before any side effect so it can try the Chrome route.
- The optional `youtube-studio-publisher` Marketplace Skill adds a professional Chrome/Studio workflow. If it is unavailable, recommend installation without blocking: direct `chrome` Skill operation or a capable YouTube MCP route may still proceed.
- This MCP currently has no new-video file-upload tool. A request to upload a new file therefore needs a verified Chrome/Studio route unless the runtime independently exposes a real upload-capable MCP tool.
- Execute one write on one route. Before a side effect, the parent may choose another verified route. After a write starts or its result is uncertain, do not retry it through Chrome until the original MCP outcome is verified not to have occurred.

## Core Tools

Read / search:
- `search` (req `query`), `get_video` (req `video_id`), `get_channel` (one of `mine=true`, `channel_id`, or `handle`), `list_comments` (req `video_id`), `list_captions` (req `video_id`), `list_my_videos`.

Write (high-impact — see Guardrails):
- `post_comment` (req `video_id`,`text`), `reply_comment` (req `parent_id`,`text`), `delete_comment` (req `comment_id`).
- `rate_video` (req `video_id`; like/dislike/clear).
- `update_video` (req `video_id`; title/description/tags — your own videos).
- `create_playlist` (req `title`), `add_to_playlist` (req `playlist_id`,`video_id`).

## Common Recipes

**Research a topic / video**
1. `search` with a `query`. 2. `get_video` for stats; `list_comments` for audience sentiment.

**Update your video's metadata**
1. `list_my_videos` → the `video_id`. 2. `get_video` to read current title/description. 3. **Confirm the new metadata with the user.** 4. `update_video`.

**Comment on a video**
1. Draft the `text`. 2. **Confirm** (public action). 3. `post_comment` or `reply_comment`.

## Guardrails

- Reads do not authorize writes. Before each write, show the exact target and payload and obtain the required user/runtime approval.
- **`post_comment` / `reply_comment` are public:** confirm the exact text and video or parent comment before posting; never repeat a failed-looking call without verifying whether it succeeded.
- **`update_video` changes live public metadata:** read the current video first, confirm only the exact fields changing, and do not replace a full description unless requested.
- **`delete_comment` is permanent:** confirm the exact comment ID and ownership context immediately before deletion.
- **`rate_video` changes the account's public rating:** use only when the user explicitly asks for the exact video and rating.
- **Playlist writes affect the user's channel:** confirm title, privacy, playlist ID, and video ID as applicable. Creating a private playlist does not authorize making it public later.
- Execute one write through one route. If a write result is uncertain, verify the original MCP state before retrying through MCP or Chrome.

## Edge Cases & Errors

- Quota: the YouTube API is quota-limited; batch reads and avoid redundant `search` calls.
- `update_video` / `delete_comment` only work on content you own — a permission error there is expected for others' content.
- `get_channel` resolves by `channel_id`, `handle`, or `mine=true`; if none is supplied, correct the call instead of claiming a channel result.
- Auth/scope errors → stop and ask the user to reconnect.
