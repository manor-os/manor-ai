---
name: mcp_google_drive
description: Operate the user's Google Drive through the Google Drive MCP. Use when the user asks to find, read, create, move, rename, copy, share, or delete files/folders in their Drive, or to manage sharing permissions, revisions, or file comments.
version: 1.0.0
---

# Google Drive Runtime Skill

Use this skill to operate the user's **connected Google Drive** through the Google Drive MCP (`mcp__google_drive__*`).

## When To Use

Use Drive when the user asks to locate, read, organize, share, or clean up files/folders in their Google Drive, or to manage who has access to a file.

## Connection

Authenticates via Google OAuth. On an auth/scope error, stop and ask the user to reconnect Google Drive. `get_about` reports the connected account and storage.

## Core Tools

Find / read:
- `search_files` (req `query`), `list_files`, `get_file` (metadata, req `file_id`), `read_file` (content, req `file_id`).
- `list_revisions` / `get_revision`, `list_comments`.

Create / organize:
- `create_file` (req `name`), `create_folder` (req `name`).
- `move_file` (req `file_id`,`folder_id`), `rename_file` (req `file_id`,`new_name`), `copy_file` (req `file_id`).

Share (high-impact — see Guardrails):
- `share_file` (req `file_id`), `list_permissions` (req `file_id`), `update_permission` (req `file_id`,`permission_id`,`role`), `delete_permission`.

Delete / restore:
- `delete_file` (req `file_id`, → trash), `restore_file` (req `file_id`), `delete_file_permanent`, `empty_trash`.

Comments: `create_comment` (req `file_id`,`content`), `resolve_comment`, `create_reply`.

## Common Recipes

**Find and read a document**
1. `search_files` with a `query` (name/content). 2. `get_file` to confirm it's the right one. 3. `read_file` for content.

**Organize files**
1. `search_files` / `list_files` to locate. 2. `create_folder` if needed. 3. `move_file` / `rename_file` to tidy up.

**Share with a collaborator**
1. `list_permissions` to see current access. 2. **Confirm recipient + role with the user.** 3. `share_file` (or `update_permission`) with the agreed role.

## Guardrails

- **Sharing changes who can see private files — confirm the recipient and role (viewer/commenter/editor) before `share_file` / `update_permission`.** Never widen access (e.g. "anyone with the link") without explicit instruction.
- **Deletion**: `delete_file` moves to trash (recoverable via `restore_file`); `delete_file_permanent` and `empty_trash` are irreversible — require explicit confirmation and never run them speculatively.
- Prefer `copy_file` before destructive edits to important files.
- Privacy: read only the files the task needs; don't enumerate or export the whole Drive.

## Edge Cases & Errors

- `search_files` query syntax matters; if results are noisy, narrow by name/type rather than reading many files.
- `move_file` needs the destination `folder_id` — resolve it (search/list) first.
- Permission changes can cascade (a shared folder shares its children) — note this when changing folder access.
- Auth/scope errors → stop and ask the user to reconnect.
