---
name: mcp_youtube
description: Operate YouTube through the YouTube MCP. Use when the user asks to search YouTube, read a video's details or comments, list or update their own videos, post/reply/delete comments, rate a video, or manage playlists.
version: 1.0.0
---

# YouTube Runtime Skill

Use this skill to operate **YouTube** through the YouTube MCP (`mcp__youtube__*`) — public search/read plus authenticated actions on the user's own channel.

## When To Use

Use YouTube when the user asks to search videos/channels, read video stats or comments, manage their own videos/comments, like/dislike a video, or build playlists.

## Connection

Authenticates via Google OAuth (YouTube scope). On an auth/scope error, stop and ask the user to reconnect. `get_channel` (no args) returns the authenticated user's channel.

## Core Tools

Read / search:
- `search` (req `query`), `get_video` (req `video_id`), `get_channel`, `list_comments` (req `video_id`), `list_captions` (req `video_id`), `list_my_videos`.

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

- **`post_comment` / `reply_comment` are public — confirm the text before posting** and don't post repeatedly (spam).
- **`update_video` changes your live video's public metadata** (title/description/tags affect SEO and viewers) — confirm exact values; don't overwrite a description wholesale unless asked.
- `delete_comment` is permanent and only valid on your own comments / comments on your videos.
- `rate_video` is a public like/dislike on the account — only when the user asked.

## Edge Cases & Errors

- Quota: the YouTube API is quota-limited; batch reads and avoid redundant `search` calls.
- `update_video` / `delete_comment` only work on content you own — a permission error there is expected for others' content.
- `get_channel` resolves by id, @handle, or the authed user — be explicit about whose channel you mean.
- Auth/scope errors → stop and ask the user to reconnect.
