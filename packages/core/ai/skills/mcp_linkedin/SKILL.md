---
name: mcp_linkedin
description: Publish and manage LinkedIn content through the official LinkedIn API MCP. Use when the user asks to post to LinkedIn (text, image, video, document), comment or react on their posts, manage company-page posts they admin, or read engagement stats on their own posts.
version: 1.0.0
---

# LinkedIn (Posting & Analytics) Runtime Skill

Use this skill to publish and manage content on the user's **LinkedIn** via the official API MCP (`mcp__linkedin__*`). This is the compliant path for posting and reading stats on the user's own content and the company pages they admin.

## When To Use

Use this skill to: post to LinkedIn, attach images/video/documents to a post, comment/react on posts, publish on an admin'd company page, manage (list/delete) the user's own posts, or read engagement stats on their posts.

**Do NOT use this for**: people/company search, reading third-party profiles, jobs, DMs/messaging, or browsing the feed — those require the separate `linkedin_browser` (Search & Messaging) MCP.

## Connection

Authenticates via LinkedIn OAuth. **URNs are required identifiers**: get the member URN from `get_profile` (use as `author_urn`/`actor_urn`), and organization URNs from `list_organizations` (use as `owner_urn`/`org_urn`). On an auth/scope error, stop and ask the user to reconnect LinkedIn.

## Core Tools

Publish:
- `create_post` — required: `author_urn`, `text`. The core posting call.
- `upload_image` / `upload_video` / `upload_document` — required: `owner_urn`, `src`. Upload media first, then reference it when creating the post.

Company pages (admin only):
- `list_organizations`, `get_organization`, `list_org_posts` (req `org_urn`).

Manage & measure:
- `get_my_posts` (req `author_urn`), `delete_post` (req `post_urn`).
- `get_post_stats` (req `author_urn`) — engagement on the user's posts.
- `create_comment` (req `post_urn`,`actor_urn`,`text`), `react_to_post` (req `post_urn`,`actor_urn`), `get_post_comments`, `get_post_reactions`.

## Common Recipes

**Post text to the user's profile**
1. `get_profile` → `author_urn`.
2. **Show the final post text to the user and get approval** (Guardrails).
3. `create_post` with `author_urn` + `text`.

**Post with an image**
1. `get_profile` → `author_urn` (or `list_organizations` → `org_urn` for a company page).
2. `upload_image` with `owner_urn` + `src`; keep the returned media reference.
3. Confirm caption with the user, then `create_post` including the uploaded image.

**Report engagement**
1. `get_my_posts` (`author_urn`) to list recent posts. 2. `get_post_stats` to summarize reach/engagement.

## Guardrails

- **Publishing is public and immediate. Never `create_post` / `create_comment` / `react_to_post` without showing the exact content (and target profile vs company page) and getting explicit approval.**
- **Confirm the target**: posting as the member (`author_urn` from `get_profile`) vs a company page (`org_urn`) are very different audiences — verify which the user means.
- `delete_post` is the only cleanup; a published post may already have impressions before deletion. Don't rely on delete to "undo" a mistaken post.
- One post per request unless the user explicitly asked for several; avoid spammy repeated posting.

## Edge Cases & Errors

- Wrong/empty URN is the most common failure — always derive URNs from `get_profile` / `list_organizations`, never invent them.
- Company-page actions require the user to be an admin with org scopes; a 403 means they lack page permission, not a bug.
- Media must be uploaded before it can be attached — don't pass a raw local path to `create_post`.
- Auth/scope errors → stop and ask the user to reconnect.
