---
name: mcp_github
description: Operate the user's GitHub through the GitHub MCP. Use when the user asks to view, create, or comment on issues and pull requests, read or change repository files, manage branches/commits, search code, or inspect Actions/CI runs.
version: 1.0.0
---

# GitHub Runtime Skill

Use this skill to operate the user's **connected GitHub account** through the GitHub MCP (`mcp__github__*`). Every tool takes a `repo` as `owner/name`. Prefer these tools over a browser or raw git for GitHub data.

## When To Use

Use GitHub when the user asks about issues, pull requests, repository files/code, branches, commits, releases, or CI/Actions on a GitHub repo they can access.

## Connection

Authenticates via GitHub OAuth (`repo`, `read:org`). On a 401/403, stop and ask the user to reconnect GitHub or confirm they have access to that repo/org. Use `repo_info` / `list_repos` to confirm access before deeper work.

## Core Tools

Issues: `list_issues`, `get_issue`, `create_issue` (req `repo`,`title`), `comment_issue`, `update_issue`, `close_issue`, `add_labels`, `add_assignees`.

Pull requests: `list_prs`, `get_pr`, `get_pr_diff`, `list_pr_files`, `create_pr` (req `repo`,`title`,`head`), `create_pr_review` (req `repo`,`number`,`event` = `APPROVE`/`REQUEST_CHANGES`/`COMMENT`), `merge_pr`, `request_pr_review`.

Code / files: `read_file`, `list_files`, `search_code`, `create_or_update_file` (req `repo`,`path`,`message`,`content`), `delete_file` (req `…`,`sha`), `push_files` (req `repo`,`branch`,`message`,`files`).

Branches / commits: `list_branches`, `get_branch`, `create_branch`, `delete_branch`, `list_commits`, `get_commit`, `compare_commits`.

CI / Actions: `list_runs`, `list_workflow_jobs`, `get_job_logs`, `run_workflow`, `rerun_failed_jobs`, `cancel_workflow_run`.

## Common Recipes

**Triage an issue**
1. `get_issue` (read body + thread). 2. Optionally `comment_issue` / `add_labels` / `add_assignees` after confirming.

**Open a PR from changes**
1. `create_branch` off the base. 2. `create_or_update_file` or `push_files` onto that branch with a clear commit `message`. 3. `create_pr` with `head` = the branch, a descriptive `title`/body.

**Diagnose a failing CI run**
1. `list_runs` → find the failed `run_id`. 2. `list_workflow_jobs` → the failed `job_id`. 3. `get_job_logs` to read the failure. Summarize the root cause before proposing a fix.

## Guardrails

- **State-changing calls require explicit confirmation**: `merge_pr`, `push_files`, `create_or_update_file`, `delete_file`, `delete_branch`, `close_issue`, `run_workflow`, `cancel_workflow_run`. Name the repo + target before acting.
- **Never `merge_pr` without the user's go-ahead**, even if checks are green. Surface review state + CI status first.
- `delete_file` requires the current `sha` — read it first; a wrong/stale sha can clobber. `create_or_update_file` overwrites — confirm the path and that you have the latest content.
- Respect repo scope: only act on repos the user named/authorized. Don't fan out across an org unprompted.

## Edge Cases & Errors

- `repo` must be `owner/name`; a bare name will fail — resolve it first if ambiguous.
- `create_pr` needs `head` (and base) branches to exist and differ; create/verify the branch first.
- Large diffs: prefer `list_pr_files` + targeted `get_pr_diff` over dumping everything.
- 404 can mean "no access" as well as "not found" — check access via `repo_info` before concluding it doesn't exist.
- Auth errors → stop and ask the user to reconnect.
