---
name: mcp_onedrive
description: Operate the user's OneDrive through the OneDrive MCP. Use when the user asks to find, read, upload, organize, share, or delete files/folders in their OneDrive / Microsoft 365 storage, or to manage sharing permissions or version history.
version: 1.0.0
---

# OneDrive Runtime Skill

Use this skill to operate the user's **connected OneDrive** (Microsoft 365 storage) through the OneDrive MCP (`mcp__onedrive__*`). For Google Drive use `mcp_google_drive`.

## When To Use

Use OneDrive when the user asks to locate, read, upload, organize, share, or clean up files/folders in their OneDrive, or to manage who has access to an item.

## Connection

Authenticates via Microsoft OAuth. On an auth/scope error, stop and ask the user to reconnect OneDrive. `get_drive_info` reports the connected drive (owner, quota, type).

## Core Tools

Find / read:
- `search_files` (req `query`), `list_files` (folder children), `get_file` (metadata, req `file_id`), `get_file_by_path` (resolve by `path`), `read_file` (text content, req `file_id`).
- `get_recent_files`, `get_shared_with_me`, `list_versions` (req `file_id`).

Create / organize:
- `upload_text_file` (req `name`,`content`; small ≤4MB UTF-8 text), `create_folder` (req `name`).
- `move_file` (req `file_id`,`destination_folder_id`), `rename_file` (req `file_id`,`new_name`), `copy_file` (req `file_id`).

Share (high-impact — see Guardrails):
- `create_share_link` (req `file_id`; mint view/edit/embed link), `invite` (req `file_id`,`recipients`; per-user access by email — more granular than a link), `list_permissions` (req `file_id`), `delete_permission` (req `file_id`,`permission_id`).

Delete / restore:
- `delete_file` (req `file_id`; → recycle bin, recoverable from web UI), `restore_version` (req `file_id`,`version_id`).

## Common Recipes

**Find and read a document**
1. `search_files` with a `query` (or `get_file_by_path` if you know the path). 2. `get_file` to confirm. 3. `read_file` for content.

**Organize files**
1. `search_files` / `list_files` to locate. 2. `create_folder` if needed. 3. `move_file` / `rename_file` to tidy up.

**Share with a person**
1. `list_permissions` to see current access. 2. **Confirm recipient + access level with the user.** 3. Prefer `invite` (per-user by email) over a broad `create_share_link` unless a link is explicitly wanted.

## Guardrails

- **Sharing exposes private files — confirm recipients and access level before `create_share_link` / `invite`.** Prefer per-user `invite` over an open share link; never mint an "anyone with the link" / edit link without explicit instruction.
- **Deletion**: `delete_file` goes to the recycle bin (recoverable from the web UI), but treat it as user-visible data loss — confirm before deleting, and never delete speculatively.
- Prefer `copy_file` before destructive edits to important files.
- `upload_text_file` is for small text only (≤4MB); don't use it for binary/large files.
- Privacy: read only the files the task needs; don't enumerate or export the whole drive.

## Edge Cases & Errors

- Two lookup styles: by `file_id` (most tools) or `get_file_by_path` — resolve an ID first when a tool needs `file_id`.
- `move_file` needs `destination_folder_id` — get it via `search_files`/`list_files` first.
- Sharing a folder cascades to its children — note this when changing folder access.
- Auth/consent errors → stop and ask the user to reconnect.
