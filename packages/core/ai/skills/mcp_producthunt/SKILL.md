---
name: mcp_producthunt
description: Read Product Hunt and comment through the Product Hunt MCP. Use when the user asks to search Product Hunt posts, see top daily launches, read a post and its comments, or leave a comment on a post.
version: 1.0.0
---

# Product Hunt Runtime Skill

Use this skill to read **Product Hunt** and comment through the Product Hunt MCP (`mcp__producthunt__*`).

## When To Use

Use Product Hunt when the user asks to discover/search launches, see what's trending on a given day, read a post and its discussion, or post a comment.

## Connection

Authenticates via Product Hunt OAuth. `me` is a quick sanity check that the token works and returns the authenticated PH user. On an auth error, stop and ask the user to reconnect.

## Core Tools

Read:
- `search_posts` (by topic / free-text / launch date), `get_post` (detail by slug), `daily_posts` (top launches on a specific day), `list_comments` (most recent first).

Write (high-impact — see Guardrails):
- `post_comment` — comment on a post as the authenticated user.

## Common Recipes

**Find and summarize today's top launches**
1. `daily_posts` for the day. 2. `get_post` on the interesting ones for detail. Summarize.

**Research a product/space**
1. `search_posts` by topic/keyword. 2. `get_post` + `list_comments` to read positioning and community reaction.

**Comment on a launch**
1. `get_post` to read context. 2. **Show the comment text to the user and get approval.** 3. `post_comment`.

## Guardrails

- **`post_comment` is public and posts as the user — confirm the exact text before posting.** Keep it genuine; don't post promotional/spammy or repeated comments.
- Reading (`search_posts` / `get_post` / `daily_posts` / `list_comments`) is safe and needs no confirmation.

## Edge Cases & Errors

- Posts are addressed by slug — get it from `search_posts` / `daily_posts` before `get_post`.
- Date-scoped queries (`daily_posts`, date search) reflect that day only — state the date you used.
- Auth errors → stop and ask the user to reconnect.
