---
name: mcp_twitter_x
description: Operate the user's X (Twitter) account through the Twitter/X MCP. Use when the user asks to post a tweet or thread, reply to a tweet, search recent tweets, read their timeline or tweet metrics, or like / retweet / follow.
version: 1.0.0
---

# Twitter / X Runtime Skill

Use this skill to operate the user's **connected X (Twitter)** account through the X API v2 MCP (`mcp__twitter_x__*`).

## When To Use

Use X when the user asks to publish a tweet/thread, reply, search recent tweets, read timelines or engagement metrics, or manage likes/retweets/follows on their account.

## Connection

Authenticates via X OAuth2. On an auth/scope error, stop and ask the user to reconnect X. `get_me` returns the authenticated user (and `user_id`) — needed for timeline/follower calls that take `user_id`.

## Core Tools

Read / search:
- `search_recent` (req `query`), `get_tweet` (req `tweet_id`), `get_tweet_metrics` (req `tweet_id`).
- `get_my_timeline`, `get_user_timeline` (req `user_id`), `get_mentions` (req `user_id`).
- `get_user` (req `username`), `search_users` (req `query`), `get_followers` / `get_following` (req `user_id`).

Publish (high-impact — see Guardrails):
- `create_tweet` (req `text`), `comment_tweet` (req `tweet_id`,`text`), `create_thread` (req `texts` — an ordered list).
- `delete_tweet` (req `tweet_id`).

Engage:
- `like_tweet` / `unlike_tweet`, `retweet` / `unretweet`, `follow_user` / `unfollow_user`.

## Common Recipes

**Post a tweet**
1. Draft `text` (respect the character limit). 2. **Show it to the user and get approval** (Guardrails). 3. `create_tweet`.

**Post a thread**
1. Split content into an ordered `texts` list, each within the limit. 2. Confirm the full thread with the user. 3. `create_thread`.

**Report on a tweet's performance**
1. `get_tweet` to confirm content. 2. `get_tweet_metrics` to summarize impressions/engagement.

## Guardrails

- **Publishing is public and immediate. Never `create_tweet` / `comment_tweet` / `create_thread` without showing the exact text and getting explicit approval.**
- Keep within the per-tweet character limit; for long content use `create_thread`, not a truncated tweet.
- **Mind rate limits** — batch reads, avoid rapid repeated posts/likes/follows; a burst can get the account throttled.
- `delete_tweet` may run after the tweet already has impressions; deletion is not a true undo. `follow_user` / `like_tweet` are public actions — confirm if they're not clearly intended.

## Edge Cases & Errors

- Timeline/follower tools need a `user_id`, not a handle — resolve via `get_me` (self) or `get_user` (others) first.
- `search_recent` only covers the recent window (not full history) — say so if the user expects older tweets.
- 429 = rate limited; back off rather than retrying immediately.
- Auth/scope errors → stop and ask the user to reconnect.
