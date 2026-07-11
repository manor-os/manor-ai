"""
GitHub MCP server — in-process MCP implementation for GitHub REST API.

Implements the MCP tools/list and tools/call protocol as Python functions.
Auth: Bearer token = GitHub OAuth access_token (from entity integration config).

Tools follow mcp__github__{tool_name} naming via the MCP tool pool.

Coverage: read + write across code, branches, commits, PRs, issues,
comments (incl. edit/delete), repos, search, commit status / check runs,
and Actions (list + logs + artifacts + dispatch/re-run/cancel). Excludes
Copilot-gated and admin-only
surfaces (code scanning alerts, dependabot, secret scanning, repo settings,
webhooks, branch protection, org admin) — those need scopes beyond
``repo,read:org`` and aren't worth the operational risk.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import httpx

logger = logging.getLogger(__name__)

from packages.core.external_api_versions import GITHUB as _GITHUB_PIN

_API = "https://api.github.com"
_MAX_CHARS = 12_000
_TIMEOUT = 30.0


# ── MCP Protocol ─────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    """Return MCP tool definitions (tools/list format)."""
    return [_tool_def(name, spec) for name, spec in _TOOLS.items()]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    """Execute a tool (tools/call format). Returns MCP content result."""
    handler = _HANDLERS.get(name)
    if not handler:
        return _error(f"Unknown tool: {name}")

    spec = _TOOLS.get(name, {})
    missing = [p for p in spec.get("required", []) if arguments.get(p) in (None, "")]
    if missing:
        return _error(f"Missing required params: {', '.join(missing)}")

    try:
        text = await handler(bearer_token, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        logger.exception("GitHub MCP tool %s failed", name)
        return _error(str(e))


from packages.core.ai.mcp._http import mcp_err as _error  # noqa: E402, F401


# ── GitHub API client ─────────────────────────────────────────────────────────

class _GitHubAPIError(RuntimeError):
    """Raised by ``_api_json`` on non-2xx; multi-step handlers catch it
    to produce a user-friendly string instead of bubbling the exception."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"GitHub API {status}: {body[:200]}")


async def _api(
    token: str,
    method: str,
    path: str,
    body: Optional[Dict] = None,
    accept: str = "application/vnd.github+json",
) -> str:
    url = f"{_API}/{path.lstrip('/')}" if not path.startswith("http") else path
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": _GITHUB_PIN.value,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, url, headers=headers, json=body)

    if resp.status_code == 401:
        return "GitHub authentication failed. Reconnect GitHub on the Integration page."
    if resp.status_code == 403:
        return f"GitHub forbidden (rate limit or permissions): {resp.text[:200]}"
    if resp.status_code == 404:
        return "Not found."
    if resp.status_code == 422:
        return f"GitHub validation error (422): {resp.text[:300]}"
    if not resp.is_success:
        return f"GitHub API error ({resp.status_code}): {resp.text[:300]}"

    if "raw" in accept or "diff" in accept or "patch" in accept:
        text = resp.text
        if len(text) > _MAX_CHARS:
            return text[:_MAX_CHARS] + f"\n… (truncated, {len(text)} total)"
        return text

    if not resp.text:
        return json.dumps({"ok": True})

    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]

    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        return out[:_MAX_CHARS] + "\n… (truncated)"
    return out


async def _api_json(
    token: str,
    method: str,
    path: str,
    body: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Like ``_api`` but raises on non-2xx and returns parsed JSON. Used by
    multi-step handlers (``push_files``, ``create_branch``) where typed
    intermediate values are required."""
    url = f"{_API}/{path.lstrip('/')}" if not path.startswith("http") else path
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_PIN.value,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.request(method, url, headers=headers, json=body)
    if not resp.is_success:
        raise _GitHubAPIError(resp.status_code, resp.text)
    if not resp.text:
        return {}
    return resp.json()


def _csv_or_list(v: Any) -> List[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    return [s.strip() for s in str(v).split(",") if s.strip()]


def _ok_or(api_result: str, message: str) -> str:
    """Write endpoints that return an empty 201/204 body surface through
    ``_api`` as ``{"ok": true}``. Swap that for a human-readable confirmation
    while passing real error strings straight through."""
    if api_result.strip() in ('{"ok": true}', '{"ok":true}', "{}", ""):
        return json.dumps({"ok": True, "message": message})
    return api_result


# ── Issues ────────────────────────────────────────────────────────────────────

async def _list_issues(token: str, args: Dict) -> str:
    repo = args["repo"]
    q = {"state": args.get("state", "open"), "per_page": int(args.get("per_page", 20))}
    if args.get("labels"):
        q["labels"] = args["labels"]
    if args.get("assignee"):
        q["assignee"] = args["assignee"]
    if args.get("creator"):
        q["creator"] = args["creator"]
    return await _api(token, "GET", f"repos/{repo}/issues?{urlencode(q)}")


async def _get_issue(token: str, args: Dict) -> str:
    return await _api(token, "GET", f"repos/{args['repo']}/issues/{args['number']}")


async def _create_issue(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {"title": args["title"]}
    if args.get("body"):
        body["body"] = args["body"]
    if args.get("labels"):
        body["labels"] = _csv_or_list(args["labels"])
    if args.get("assignees"):
        body["assignees"] = _csv_or_list(args["assignees"])
    return await _api(token, "POST", f"repos/{args['repo']}/issues", body)


async def _update_issue(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {}
    for k in ("title", "body", "state", "state_reason"):
        if k in args and args[k] is not None:
            body[k] = args[k]
    if args.get("labels") is not None:
        body["labels"] = _csv_or_list(args["labels"])
    if args.get("assignees") is not None:
        body["assignees"] = _csv_or_list(args["assignees"])
    return await _api(token, "PATCH", f"repos/{args['repo']}/issues/{args['number']}", body)


async def _comment_issue(token: str, args: Dict) -> str:
    return await _api(
        token, "POST",
        f"repos/{args['repo']}/issues/{args['number']}/comments",
        {"body": args["body"]},
    )


async def _list_issue_comments(token: str, args: Dict) -> str:
    per_page = int(args.get("per_page", 30))
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/issues/{args['number']}/comments?per_page={per_page}",
    )


async def _update_comment(token: str, args: Dict) -> str:
    """Edit an issue/PR conversation comment (the kind ``comment_issue`` makes).
    GitHub keys these by comment id, not issue number."""
    return await _api(
        token, "PATCH",
        f"repos/{args['repo']}/issues/comments/{args['comment_id']}",
        {"body": args["body"]},
    )


async def _delete_comment(token: str, args: Dict) -> str:
    """Delete an issue/PR conversation comment by id."""
    res = await _api(
        token, "DELETE",
        f"repos/{args['repo']}/issues/comments/{args['comment_id']}",
    )
    return _ok_or(res, f"Comment {args['comment_id']} deleted.")


async def _close_issue(token: str, args: Dict) -> str:
    return await _api(
        token, "PATCH",
        f"repos/{args['repo']}/issues/{args['number']}",
        {"state": "closed"},
    )


async def _add_labels(token: str, args: Dict) -> str:
    return await _api(
        token, "POST",
        f"repos/{args['repo']}/issues/{args['number']}/labels",
        {"labels": _csv_or_list(args["labels"])},
    )


async def _remove_label(token: str, args: Dict) -> str:
    return await _api(
        token, "DELETE",
        f"repos/{args['repo']}/issues/{args['number']}/labels/{quote(args['label'])}",
    )


async def _add_assignees(token: str, args: Dict) -> str:
    return await _api(
        token, "POST",
        f"repos/{args['repo']}/issues/{args['number']}/assignees",
        {"assignees": _csv_or_list(args["assignees"])},
    )


# ── Pull requests ─────────────────────────────────────────────────────────────

async def _list_prs(token: str, args: Dict) -> str:
    q: Dict[str, Any] = {
        "state": args.get("state", "open"),
        "per_page": int(args.get("per_page", 20)),
    }
    if args.get("base"):
        q["base"] = args["base"]
    if args.get("head"):
        q["head"] = args["head"]
    return await _api(token, "GET", f"repos/{args['repo']}/pulls?{urlencode(q)}")


async def _get_pr(token: str, args: Dict) -> str:
    return await _api(token, "GET", f"repos/{args['repo']}/pulls/{args['number']}")


async def _create_pr(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {
        "title": args["title"],
        "head": args["head"],
        "base": args.get("base", "main"),
    }
    if args.get("body"):
        body["body"] = args["body"]
    if args.get("draft") is not None:
        body["draft"] = bool(args["draft"])
    return await _api(token, "POST", f"repos/{args['repo']}/pulls", body)


async def _update_pr(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {}
    for k in ("title", "body", "state", "base"):
        if k in args and args[k] is not None:
            body[k] = args[k]
    return await _api(
        token, "PATCH",
        f"repos/{args['repo']}/pulls/{args['number']}", body,
    )


async def _merge_pr(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {}
    for k in ("commit_title", "commit_message", "merge_method", "sha"):
        if args.get(k):
            body[k] = args[k]
    return await _api(
        token, "PUT",
        f"repos/{args['repo']}/pulls/{args['number']}/merge", body,
    )


async def _list_pr_files(token: str, args: Dict) -> str:
    per_page = int(args.get("per_page", 30))
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/pulls/{args['number']}/files?per_page={per_page}",
    )


async def _get_pr_diff(token: str, args: Dict) -> str:
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/pulls/{args['number']}",
        accept="application/vnd.github.v3.diff",
    )


async def _create_pr_review(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {"event": args["event"]}
    if args.get("body"):
        body["body"] = args["body"]
    if args.get("commit_id"):
        body["commit_id"] = args["commit_id"]
    if args.get("comments"):
        body["comments"] = args["comments"]
    return await _api(
        token, "POST",
        f"repos/{args['repo']}/pulls/{args['number']}/reviews", body,
    )


async def _list_pr_reviews(token: str, args: Dict) -> str:
    per_page = int(args.get("per_page", 30))
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/pulls/{args['number']}/reviews?per_page={per_page}",
    )


async def _request_pr_review(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {}
    if args.get("reviewers"):
        body["reviewers"] = _csv_or_list(args["reviewers"])
    if args.get("team_reviewers"):
        body["team_reviewers"] = _csv_or_list(args["team_reviewers"])
    return await _api(
        token, "POST",
        f"repos/{args['repo']}/pulls/{args['number']}/requested_reviewers", body,
    )


async def _list_pr_review_comments(token: str, args: Dict) -> str:
    """List inline (diff-anchored) review comments on a PR. Distinct from
    conversation comments (``list_issue_comments``) and from reviews
    (``list_pr_reviews``)."""
    per_page = int(args.get("per_page", 30))
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/pulls/{args['number']}/comments?per_page={per_page}",
    )


async def _update_review_comment(token: str, args: Dict) -> str:
    """Edit an inline PR review comment by id (``/pulls/comments/{id}``)."""
    return await _api(
        token, "PATCH",
        f"repos/{args['repo']}/pulls/comments/{args['comment_id']}",
        {"body": args["body"]},
    )


async def _delete_review_comment(token: str, args: Dict) -> str:
    """Delete an inline PR review comment by id."""
    res = await _api(
        token, "DELETE",
        f"repos/{args['repo']}/pulls/comments/{args['comment_id']}",
    )
    return _ok_or(res, f"Review comment {args['comment_id']} deleted.")


# ── Code (read + write) ───────────────────────────────────────────────────────

async def _read_file(token: str, args: Dict) -> str:
    repo, path = args["repo"], args["path"]
    ref_q = f"?ref={quote(args['ref'])}" if args.get("ref") else ""
    return await _api(
        token, "GET",
        f"repos/{repo}/contents/{quote(path)}{ref_q}",
        accept="application/vnd.github.v3.raw",
    )


async def _list_files(token: str, args: Dict) -> str:
    repo = args["repo"]
    path = args.get("path", "")
    ref_q = f"?ref={quote(args['ref'])}" if args.get("ref") else ""
    return await _api(token, "GET", f"repos/{repo}/contents/{quote(path)}{ref_q}")


async def _create_or_update_file(token: str, args: Dict) -> str:
    repo, path = args["repo"], args["path"]
    content = args["content"]
    encoding = args.get("content_encoding", "utf-8")
    if encoding == "utf-8":
        content_b64 = base64.b64encode(content.encode("utf-8")).decode()
    elif encoding == "base64":
        content_b64 = content
    else:
        return f"Unsupported content_encoding '{encoding}' (use 'utf-8' or 'base64')"
    body: Dict[str, Any] = {"message": args["message"], "content": content_b64}
    if args.get("branch"):
        body["branch"] = args["branch"]
    if args.get("sha"):
        body["sha"] = args["sha"]
    return await _api(token, "PUT", f"repos/{repo}/contents/{quote(path)}", body)


async def _delete_file(token: str, args: Dict) -> str:
    repo, path = args["repo"], args["path"]
    body: Dict[str, Any] = {"message": args["message"], "sha": args["sha"]}
    if args.get("branch"):
        body["branch"] = args["branch"]
    return await _api(token, "DELETE", f"repos/{repo}/contents/{quote(path)}", body)


async def _push_files(token: str, args: Dict) -> str:
    """Atomic multi-file commit via Git Trees API.

    Steps: resolve ref → get parent tree → blob each file → build new tree →
    create commit → fast-forward ref. ``files`` is a list of
    ``{path, content, encoding?}`` (encoding default 'utf-8').
    """
    repo = args["repo"]
    branch = args["branch"]
    message = args["message"]
    files = args.get("files") or []
    if not files:
        return "files list is empty"
    try:
        ref = await _api_json(token, "GET", f"repos/{repo}/git/refs/heads/{quote(branch)}")
        parent_sha = ref["object"]["sha"]
        commit = await _api_json(token, "GET", f"repos/{repo}/git/commits/{parent_sha}")
        base_tree = commit["tree"]["sha"]

        tree_entries: List[Dict[str, Any]] = []
        for f in files:
            enc = f.get("encoding", "utf-8")
            if enc == "utf-8":
                blob_content = base64.b64encode(str(f["content"]).encode("utf-8")).decode()
            elif enc == "base64":
                blob_content = f["content"]
            else:
                return f"Unsupported encoding '{enc}' for {f.get('path')}"
            blob = await _api_json(
                token, "POST", f"repos/{repo}/git/blobs",
                {"content": blob_content, "encoding": "base64"},
            )
            tree_entries.append({
                "path": f["path"],
                "mode": f.get("mode", "100644"),
                "type": "blob",
                "sha": blob["sha"],
            })

        tree = await _api_json(
            token, "POST", f"repos/{repo}/git/trees",
            {"base_tree": base_tree, "tree": tree_entries},
        )
        new_commit = await _api_json(
            token, "POST", f"repos/{repo}/git/commits",
            {"message": message, "tree": tree["sha"], "parents": [parent_sha]},
        )
        updated_ref = await _api_json(
            token, "PATCH", f"repos/{repo}/git/refs/heads/{quote(branch)}",
            {"sha": new_commit["sha"]},
        )
        return json.dumps({
            "ok": True,
            "branch": branch,
            "commit_sha": new_commit["sha"],
            "files_pushed": [f["path"] for f in files],
            "ref": updated_ref,
        }, ensure_ascii=False, indent=2)
    except _GitHubAPIError as e:
        return f"push_files failed ({e.status}): {e.body[:300]}"


# ── Branches ──────────────────────────────────────────────────────────────────

async def _list_branches(token: str, args: Dict) -> str:
    per_page = int(args.get("per_page", 30))
    return await _api(token, "GET", f"repos/{args['repo']}/branches?per_page={per_page}")


async def _get_branch(token: str, args: Dict) -> str:
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/branches/{quote(args['branch'])}",
    )


async def _create_branch(token: str, args: Dict) -> str:
    repo = args["repo"]
    new_branch = args["branch"]
    if args.get("from_sha"):
        sha = args["from_sha"]
    elif args.get("from_branch"):
        try:
            ref = await _api_json(
                token, "GET",
                f"repos/{repo}/git/refs/heads/{quote(args['from_branch'])}",
            )
            sha = ref["object"]["sha"]
        except _GitHubAPIError as e:
            return f"create_branch: failed to resolve from_branch ({e.status}): {e.body[:200]}"
    else:
        return "create_branch requires either from_branch or from_sha"
    return await _api(
        token, "POST", f"repos/{repo}/git/refs",
        {"ref": f"refs/heads/{new_branch}", "sha": sha},
    )


async def _delete_branch(token: str, args: Dict) -> str:
    return await _api(
        token, "DELETE",
        f"repos/{args['repo']}/git/refs/heads/{quote(args['branch'])}",
    )


# ── Commits ───────────────────────────────────────────────────────────────────

async def _list_commits(token: str, args: Dict) -> str:
    repo = args["repo"]
    q: Dict[str, Any] = {"per_page": int(args.get("per_page", 20))}
    for k in ("sha", "path", "author", "since", "until"):
        if args.get(k):
            q[k] = args[k]
    return await _api(token, "GET", f"repos/{repo}/commits?{urlencode(q)}")


async def _get_commit(token: str, args: Dict) -> str:
    return await _api(token, "GET", f"repos/{args['repo']}/commits/{quote(args['ref'])}")


async def _compare_commits(token: str, args: Dict) -> str:
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/compare/{quote(args['base'])}...{quote(args['head'])}",
    )


# ── Repos ─────────────────────────────────────────────────────────────────────

async def _repo_info(token: str, args: Dict) -> str:
    return await _api(token, "GET", f"repos/{args['repo']}")


async def _list_repos(token: str, args: Dict) -> str:
    owner = args.get("owner")
    per_page = int(args.get("per_page", 20))
    affiliation = args.get("affiliation", "owner,collaborator,organization_member")
    if owner:
        return await _api(token, "GET", f"users/{owner}/repos?per_page={per_page}&sort=updated")
    return await _api(
        token, "GET",
        f"user/repos?per_page={per_page}&sort=updated&affiliation={affiliation}",
    )


async def _create_repo(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {"name": args["name"]}
    for k in ("description", "homepage", "private", "has_issues",
              "has_projects", "has_wiki", "auto_init",
              "license_template", "gitignore_template"):
        if k in args and args[k] is not None:
            body[k] = args[k]
    if args.get("org"):
        return await _api(token, "POST", f"orgs/{args['org']}/repos", body)
    return await _api(token, "POST", "user/repos", body)


async def _fork_repo(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {}
    if args.get("organization"):
        body["organization"] = args["organization"]
    if args.get("name"):
        body["name"] = args["name"]
    if args.get("default_branch_only") is not None:
        body["default_branch_only"] = bool(args["default_branch_only"])
    return await _api(token, "POST", f"repos/{args['repo']}/forks", body)


async def _list_forks(token: str, args: Dict) -> str:
    per_page = int(args.get("per_page", 30))
    return await _api(token, "GET", f"repos/{args['repo']}/forks?per_page={per_page}")


async def _list_collaborators(token: str, args: Dict) -> str:
    per_page = int(args.get("per_page", 30))
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/collaborators?per_page={per_page}",
    )


async def _list_tags(token: str, args: Dict) -> str:
    per_page = int(args.get("per_page", 30))
    return await _api(token, "GET", f"repos/{args['repo']}/tags?per_page={per_page}")


async def _list_topics(token: str, args: Dict) -> str:
    return await _api(token, "GET", f"repos/{args['repo']}/topics")


async def _list_releases(token: str, args: Dict) -> str:
    per_page = int(args.get("per_page", 10))
    return await _api(token, "GET", f"repos/{args['repo']}/releases?per_page={per_page}")


# ── Search ────────────────────────────────────────────────────────────────────

async def _search_code(token: str, args: Dict) -> str:
    q = quote(args["query"])
    per_page = int(args.get("per_page", 20))
    return await _api(token, "GET", f"search/code?q={q}&per_page={per_page}")


async def _search_repos(token: str, args: Dict) -> str:
    q = quote(args["query"])
    per_page = int(args.get("per_page", 20))
    sort = args.get("sort")
    extra = f"&sort={sort}" if sort else ""
    return await _api(
        token, "GET",
        f"search/repositories?q={q}&per_page={per_page}{extra}",
    )


async def _search_issues(token: str, args: Dict) -> str:
    q = quote(args["query"])
    per_page = int(args.get("per_page", 20))
    return await _api(token, "GET", f"search/issues?q={q}&per_page={per_page}")


async def _search_users(token: str, args: Dict) -> str:
    q = quote(args["query"])
    per_page = int(args.get("per_page", 20))
    return await _api(token, "GET", f"search/users?q={q}&per_page={per_page}")


# ── Actions / Workflows (read) ────────────────────────────────────────────────

async def _list_runs(token: str, args: Dict) -> str:
    q: Dict[str, Any] = {"per_page": int(args.get("per_page", 10))}
    if args.get("status"):
        q["status"] = args["status"]
    if args.get("branch"):
        q["branch"] = args["branch"]
    if args.get("event"):
        q["event"] = args["event"]
    return await _api(token, "GET", f"repos/{args['repo']}/actions/runs?{urlencode(q)}")


async def _list_workflows(token: str, args: Dict) -> str:
    per_page = int(args.get("per_page", 20))
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/actions/workflows?per_page={per_page}",
    )


async def _get_workflow_run(token: str, args: Dict) -> str:
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/actions/runs/{args['run_id']}",
    )


async def _list_workflow_jobs(token: str, args: Dict) -> str:
    per_page = int(args.get("per_page", 20))
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/actions/runs/{args['run_id']}/jobs?per_page={per_page}",
    )


async def _get_job_logs(token: str, args: Dict) -> str:
    """Download raw logs for a single Actions job. GitHub answers with a 302
    redirect to a short-lived log URL, so follow redirects and return the tail
    (failures are almost always at the end of the log)."""
    url = f"{_API}/repos/{args['repo']}/actions/jobs/{args['job_id']}/logs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_PIN.value,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code == 401:
        return "GitHub authentication failed. Reconnect GitHub on the Integration page."
    if resp.status_code == 404:
        return "Not found (logs expired, or job id invalid)."
    if not resp.is_success:
        return f"GitHub API error ({resp.status_code}): {resp.text[:300]}"
    text = resp.text
    if len(text) > _MAX_CHARS:
        return "… (truncated, showing tail)\n" + text[-_MAX_CHARS:]
    return text


async def _run_workflow(token: str, args: Dict) -> str:
    """Trigger a workflow_dispatch run. ``inputs`` may be a dict or JSON string."""
    body: Dict[str, Any] = {"ref": args["ref"]}
    inputs = args.get("inputs")
    if isinstance(inputs, str) and inputs.strip():
        try:
            inputs = json.loads(inputs)
        except Exception:
            return "inputs must be a JSON object (or omitted)."
    if isinstance(inputs, dict) and inputs:
        body["inputs"] = inputs
    wf = quote(str(args["workflow_id"]), safe="")
    res = await _api(
        token, "POST",
        f"repos/{args['repo']}/actions/workflows/{wf}/dispatches", body,
    )
    return _ok_or(res, f"Workflow '{args['workflow_id']}' dispatched on '{args['ref']}'.")


async def _rerun_workflow_run(token: str, args: Dict) -> str:
    res = await _api(
        token, "POST",
        f"repos/{args['repo']}/actions/runs/{args['run_id']}/rerun",
    )
    return _ok_or(res, f"Re-run requested for run {args['run_id']}.")


async def _rerun_failed_jobs(token: str, args: Dict) -> str:
    res = await _api(
        token, "POST",
        f"repos/{args['repo']}/actions/runs/{args['run_id']}/rerun-failed-jobs",
    )
    return _ok_or(res, f"Re-run of failed jobs requested for run {args['run_id']}.")


async def _cancel_workflow_run(token: str, args: Dict) -> str:
    res = await _api(
        token, "POST",
        f"repos/{args['repo']}/actions/runs/{args['run_id']}/cancel",
    )
    return _ok_or(res, f"Cancellation requested for run {args['run_id']}.")


async def _list_run_artifacts(token: str, args: Dict) -> str:
    """List artifacts produced by a workflow run."""
    per_page = int(args.get("per_page", 30))
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/actions/runs/{args['run_id']}/artifacts?per_page={per_page}",
    )


async def _get_commit_status(token: str, args: Dict) -> str:
    """Combined commit status (the legacy statuses API) for a ref / SHA /
    branch. For a PR, pass its head SHA or branch as ``ref``."""
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/commits/{quote(str(args['ref']))}/status",
    )


async def _list_check_runs(token: str, args: Dict) -> str:
    """Check runs (the modern CI checks surface) for a ref / SHA / branch."""
    per_page = int(args.get("per_page", 30))
    return await _api(
        token, "GET",
        f"repos/{args['repo']}/commits/{quote(str(args['ref']))}/check-runs?per_page={per_page}",
    )


# ── User ──────────────────────────────────────────────────────────────────────

async def _get_authenticated_user(token: str, _args: Dict) -> str:
    return await _api(token, "GET", "user")


async def _get_user(token: str, args: Dict) -> str:
    return await _api(token, "GET", f"users/{quote(args['username'])}")


# ── Tool definitions ──────────────────────────────────────────────────────────

def _prop(desc: str, type_: str = "string", **extra) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": type_, "description": desc}
    out.update(extra)
    return out


_TOOLS: Dict[str, Dict[str, Any]] = {
    # ── Issues ──
    "list_issues": {
        "description": "List issues in a GitHub repo",
        "properties": {
            "repo": _prop("Owner/repo (e.g. Manor-AI/manor)"),
            "state": _prop("open, closed, or all (default: open)"),
            "labels": _prop("Comma-separated labels filter"),
            "assignee": _prop("Filter by assignee username"),
            "creator": _prop("Filter by creator username"),
            "per_page": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["repo"],
    },
    "get_issue": {
        "description": "Get issue details by number",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("Issue number", "integer"),
        },
        "required": ["repo", "number"],
    },
    "create_issue": {
        "description": "Create a new GitHub issue",
        "properties": {
            "repo": _prop("Owner/repo"),
            "title": _prop("Issue title"),
            "body": _prop("Issue body (markdown)"),
            "labels": _prop("Comma-separated labels"),
            "assignees": _prop("Comma-separated GitHub usernames"),
        },
        "required": ["repo", "title"],
    },
    "update_issue": {
        "description": "Update an existing issue (title, body, state, labels, assignees)",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("Issue number", "integer"),
            "title": _prop("New title"),
            "body": _prop("New body"),
            "state": _prop("open or closed"),
            "state_reason": _prop("completed, not_planned, reopened"),
            "labels": _prop("Comma-separated labels (replaces existing set)"),
            "assignees": _prop("Comma-separated assignees (replaces existing set)"),
        },
        "required": ["repo", "number"],
    },
    "comment_issue": {
        "description": "Add a comment to an issue or PR",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("Issue/PR number", "integer"),
            "body": _prop("Comment body (markdown)"),
        },
        "required": ["repo", "number", "body"],
    },
    "list_issue_comments": {
        "description": "List comments on an issue or PR",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("Issue/PR number", "integer"),
            "per_page": _prop("Max results (default: 30)", "integer"),
        },
        "required": ["repo", "number"],
    },
    "update_comment": {
        "description": "Edit an existing issue/PR conversation comment (by comment id)",
        "properties": {
            "repo": _prop("Owner/repo"),
            "comment_id": _prop("Comment id (from list_issue_comments)", "integer"),
            "body": _prop("New comment body (markdown)"),
        },
        "required": ["repo", "comment_id", "body"],
    },
    "delete_comment": {
        "description": "Delete an issue/PR conversation comment (by comment id)",
        "properties": {
            "repo": _prop("Owner/repo"),
            "comment_id": _prop("Comment id (from list_issue_comments)", "integer"),
        },
        "required": ["repo", "comment_id"],
    },
    "close_issue": {
        "description": "Close a GitHub issue",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("Issue number", "integer"),
        },
        "required": ["repo", "number"],
    },
    "add_labels": {
        "description": "Add labels to an issue or PR (additive)",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("Issue/PR number", "integer"),
            "labels": _prop("Comma-separated labels to add"),
        },
        "required": ["repo", "number", "labels"],
    },
    "remove_label": {
        "description": "Remove a single label from an issue or PR",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("Issue/PR number", "integer"),
            "label": _prop("Label name to remove"),
        },
        "required": ["repo", "number", "label"],
    },
    "add_assignees": {
        "description": "Assign users to an issue or PR (additive)",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("Issue/PR number", "integer"),
            "assignees": _prop("Comma-separated GitHub usernames"),
        },
        "required": ["repo", "number", "assignees"],
    },

    # ── Pull requests ──
    "list_prs": {
        "description": "List pull requests in a repo",
        "properties": {
            "repo": _prop("Owner/repo"),
            "state": _prop("open, closed, or all (default: open)"),
            "base": _prop("Filter by base branch"),
            "head": _prop("Filter by head (e.g. user:branch)"),
            "per_page": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["repo"],
    },
    "get_pr": {
        "description": "Get pull request details",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("PR number", "integer"),
        },
        "required": ["repo", "number"],
    },
    "create_pr": {
        "description": "Create a pull request",
        "properties": {
            "repo": _prop("Owner/repo"),
            "title": _prop("PR title"),
            "body": _prop("PR description (markdown)"),
            "head": _prop("Source branch (or owner:branch for forks)"),
            "base": _prop("Target branch (default: main)"),
            "draft": _prop("Open as draft", "boolean"),
        },
        "required": ["repo", "title", "head"],
    },
    "update_pr": {
        "description": "Update PR title/body/state/base",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("PR number", "integer"),
            "title": _prop("New title"),
            "body": _prop("New body"),
            "state": _prop("open or closed"),
            "base": _prop("New base branch"),
        },
        "required": ["repo", "number"],
    },
    "merge_pr": {
        "description": "Merge a pull request",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("PR number", "integer"),
            "commit_title": _prop("Merge commit title"),
            "commit_message": _prop("Merge commit message"),
            "merge_method": _prop("merge, squash, or rebase (default: merge)"),
            "sha": _prop("SHA the PR head must match (defensive)"),
        },
        "required": ["repo", "number"],
    },
    "list_pr_files": {
        "description": "List files changed in a PR",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("PR number", "integer"),
            "per_page": _prop("Max results (default: 30)", "integer"),
        },
        "required": ["repo", "number"],
    },
    "get_pr_diff": {
        "description": "Fetch the unified diff text of a PR",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("PR number", "integer"),
        },
        "required": ["repo", "number"],
    },
    "create_pr_review": {
        "description": "Submit a PR review (APPROVE / REQUEST_CHANGES / COMMENT)",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("PR number", "integer"),
            "event": _prop("APPROVE, REQUEST_CHANGES, or COMMENT"),
            "body": _prop("Review summary"),
            "commit_id": _prop("Commit SHA the review applies to"),
            "comments": _prop(
                "Inline comments: list of {path, position, body}",
                "array",
                items={"type": "object"},
            ),
        },
        "required": ["repo", "number", "event"],
    },
    "list_pr_reviews": {
        "description": "List reviews on a PR",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("PR number", "integer"),
            "per_page": _prop("Max results (default: 30)", "integer"),
        },
        "required": ["repo", "number"],
    },
    "request_pr_review": {
        "description": "Request reviewers on a PR",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("PR number", "integer"),
            "reviewers": _prop("Comma-separated usernames"),
            "team_reviewers": _prop("Comma-separated team slugs"),
        },
        "required": ["repo", "number"],
    },
    "list_pr_review_comments": {
        "description": "List inline (diff-anchored) review comments on a PR",
        "properties": {
            "repo": _prop("Owner/repo"),
            "number": _prop("PR number", "integer"),
            "per_page": _prop("Max results (default: 30)", "integer"),
        },
        "required": ["repo", "number"],
    },
    "update_review_comment": {
        "description": "Edit an inline PR review comment (by comment id)",
        "properties": {
            "repo": _prop("Owner/repo"),
            "comment_id": _prop("Review comment id (from list_pr_review_comments)", "integer"),
            "body": _prop("New comment body (markdown)"),
        },
        "required": ["repo", "comment_id", "body"],
    },
    "delete_review_comment": {
        "description": "Delete an inline PR review comment (by comment id)",
        "properties": {
            "repo": _prop("Owner/repo"),
            "comment_id": _prop("Review comment id (from list_pr_review_comments)", "integer"),
        },
        "required": ["repo", "comment_id"],
    },

    # ── Code (read + write) ──
    "search_code": {
        "description": "Search code across GitHub repositories",
        "properties": {
            "query": _prop("Search query (GitHub code search syntax)"),
            "per_page": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["query"],
    },
    "read_file": {
        "description": "Read a file from a GitHub repo",
        "properties": {
            "repo": _prop("Owner/repo"),
            "path": _prop("File path in the repo"),
            "ref": _prop("Branch or commit SHA (optional)"),
        },
        "required": ["repo", "path"],
    },
    "list_files": {
        "description": "List files/directories in a repo path",
        "properties": {
            "repo": _prop("Owner/repo"),
            "path": _prop("Directory path (default: root)"),
            "ref": _prop("Branch or commit SHA (optional)"),
        },
        "required": ["repo"],
    },
    "create_or_update_file": {
        "description": "Create a new file or update an existing one in a repo",
        "properties": {
            "repo": _prop("Owner/repo"),
            "path": _prop("File path in the repo"),
            "message": _prop("Commit message"),
            "content": _prop("File content (utf-8 text by default)"),
            "content_encoding": _prop("'utf-8' (default) or 'base64' for binary"),
            "branch": _prop("Target branch (default: repo's default)"),
            "sha": _prop("Existing file SHA (required when updating)"),
        },
        "required": ["repo", "path", "message", "content"],
    },
    "delete_file": {
        "description": "Delete a file in a repo via the Contents API",
        "properties": {
            "repo": _prop("Owner/repo"),
            "path": _prop("File path"),
            "message": _prop("Commit message"),
            "sha": _prop("Current file SHA"),
            "branch": _prop("Target branch (default: repo's default)"),
        },
        "required": ["repo", "path", "message", "sha"],
    },
    "push_files": {
        "description": (
            "Atomic multi-file commit (Git Trees API). Pass a list of files "
            "and they are committed together on the given branch in one "
            "commit."
        ),
        "properties": {
            "repo": _prop("Owner/repo"),
            "branch": _prop("Branch to push to (must already exist)"),
            "message": _prop("Commit message"),
            "files": _prop(
                "Files to include: each {path, content, encoding?='utf-8'|'base64'}",
                "array",
                items={"type": "object"},
            ),
        },
        "required": ["repo", "branch", "message", "files"],
    },

    # ── Branches ──
    "list_branches": {
        "description": "List branches in a repo",
        "properties": {
            "repo": _prop("Owner/repo"),
            "per_page": _prop("Max results (default: 30)", "integer"),
        },
        "required": ["repo"],
    },
    "get_branch": {
        "description": "Get details on a single branch",
        "properties": {
            "repo": _prop("Owner/repo"),
            "branch": _prop("Branch name"),
        },
        "required": ["repo", "branch"],
    },
    "create_branch": {
        "description": "Create a new branch from another branch or commit SHA",
        "properties": {
            "repo": _prop("Owner/repo"),
            "branch": _prop("Name for the new branch"),
            "from_branch": _prop("Source branch name (resolved to HEAD SHA)"),
            "from_sha": _prop("Source commit SHA (alternative to from_branch)"),
        },
        "required": ["repo", "branch"],
    },
    "delete_branch": {
        "description": "Delete a branch",
        "properties": {
            "repo": _prop("Owner/repo"),
            "branch": _prop("Branch name"),
        },
        "required": ["repo", "branch"],
    },

    # ── Commits ──
    "list_commits": {
        "description": "List commits in a repo",
        "properties": {
            "repo": _prop("Owner/repo"),
            "sha": _prop("Branch / tag / SHA to start listing from"),
            "path": _prop("Filter by file path"),
            "author": _prop("Filter by author"),
            "since": _prop("ISO 8601 — only commits after this date"),
            "until": _prop("ISO 8601 — only commits before this date"),
            "per_page": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["repo"],
    },
    "get_commit": {
        "description": "Get a single commit (with files diff)",
        "properties": {
            "repo": _prop("Owner/repo"),
            "ref": _prop("Commit SHA, branch, or tag"),
        },
        "required": ["repo", "ref"],
    },
    "compare_commits": {
        "description": "Diff two refs (base...head)",
        "properties": {
            "repo": _prop("Owner/repo"),
            "base": _prop("Base ref (branch / SHA)"),
            "head": _prop("Head ref (branch / SHA)"),
        },
        "required": ["repo", "base", "head"],
    },

    # ── Repos ──
    "repo_info": {
        "description": "Get repository metadata (description, stars, language, etc.)",
        "properties": {"repo": _prop("Owner/repo")},
        "required": ["repo"],
    },
    "list_repos": {
        "description": (
            "List repositories accessible to the user. Omit owner to list all "
            "repos (owned + org + collaborator). Set owner to list a specific "
            "user/org's public repos."
        ),
        "properties": {
            "owner": _prop("GitHub user or org name (omit for authenticated user)"),
            "affiliation": _prop(
                "Filter: owner, collaborator, organization_member "
                "(comma-separated, default: all three)"
            ),
            "per_page": _prop("Max results (default: 20)", "integer"),
        },
        "required": [],
    },
    "create_repo": {
        "description": "Create a new repository (personal or under an org)",
        "properties": {
            "name": _prop("Repo name"),
            "org": _prop("Org login to create under (omit = personal)"),
            "description": _prop("Repo description"),
            "homepage": _prop("Homepage URL"),
            "private": _prop("Private repo", "boolean"),
            "has_issues": _prop("Enable Issues", "boolean"),
            "has_projects": _prop("Enable Projects", "boolean"),
            "has_wiki": _prop("Enable Wiki", "boolean"),
            "auto_init": _prop("Initialize with README", "boolean"),
            "license_template": _prop("License keyword (e.g. mit, apache-2.0)"),
            "gitignore_template": _prop("gitignore template name (e.g. Python)"),
        },
        "required": ["name"],
    },
    "fork_repo": {
        "description": "Fork a repository",
        "properties": {
            "repo": _prop("Owner/repo to fork"),
            "organization": _prop("Org to fork into (omit = your account)"),
            "name": _prop("Optional new name for the fork"),
            "default_branch_only": _prop("Only fork default branch", "boolean"),
        },
        "required": ["repo"],
    },
    "list_forks": {
        "description": "List forks of a repository",
        "properties": {
            "repo": _prop("Owner/repo"),
            "per_page": _prop("Max results (default: 30)", "integer"),
        },
        "required": ["repo"],
    },
    "list_collaborators": {
        "description": "List collaborators on a repository",
        "properties": {
            "repo": _prop("Owner/repo"),
            "per_page": _prop("Max results (default: 30)", "integer"),
        },
        "required": ["repo"],
    },
    "list_tags": {
        "description": "List tags in a repository",
        "properties": {
            "repo": _prop("Owner/repo"),
            "per_page": _prop("Max results (default: 30)", "integer"),
        },
        "required": ["repo"],
    },
    "list_topics": {
        "description": "List topics on a repository",
        "properties": {"repo": _prop("Owner/repo")},
        "required": ["repo"],
    },
    "list_releases": {
        "description": "List releases in a repo",
        "properties": {
            "repo": _prop("Owner/repo"),
            "per_page": _prop("Max results (default: 10)", "integer"),
        },
        "required": ["repo"],
    },

    # ── Search ──
    "search_repos": {
        "description": "Search repositories",
        "properties": {
            "query": _prop("Search query (GitHub repo search syntax)"),
            "sort": _prop("stars, forks, help-wanted-issues, updated"),
            "per_page": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["query"],
    },
    "search_issues": {
        "description": "Search issues and pull requests across GitHub",
        "properties": {
            "query": _prop("Search query (GitHub issue search syntax)"),
            "per_page": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["query"],
    },
    "search_users": {
        "description": "Search GitHub users and orgs",
        "properties": {
            "query": _prop("Search query"),
            "per_page": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["query"],
    },

    # ── Actions / Workflows (read) ──
    "list_runs": {
        "description": "List recent GitHub Actions workflow runs",
        "properties": {
            "repo": _prop("Owner/repo"),
            "per_page": _prop("Max results (default: 10)", "integer"),
            "status": _prop("Filter: completed, in_progress, queued, failure, success"),
            "branch": _prop("Filter by branch"),
            "event": _prop("Filter by triggering event (push, pull_request, ...)"),
        },
        "required": ["repo"],
    },
    "list_workflows": {
        "description": "List Actions workflows defined in a repo",
        "properties": {
            "repo": _prop("Owner/repo"),
            "per_page": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["repo"],
    },
    "get_workflow_run": {
        "description": "Get details on a single workflow run",
        "properties": {
            "repo": _prop("Owner/repo"),
            "run_id": _prop("Workflow run ID", "integer"),
        },
        "required": ["repo", "run_id"],
    },
    "list_workflow_jobs": {
        "description": "List jobs for a workflow run",
        "properties": {
            "repo": _prop("Owner/repo"),
            "run_id": _prop("Workflow run ID", "integer"),
            "per_page": _prop("Max results (default: 20)", "integer"),
        },
        "required": ["repo", "run_id"],
    },
    "get_job_logs": {
        "description": "Get the raw logs for a single Actions job (returns the tail)",
        "properties": {
            "repo": _prop("Owner/repo"),
            "job_id": _prop("Job ID (from list_workflow_jobs)", "integer"),
        },
        "required": ["repo", "job_id"],
    },
    "run_workflow": {
        "description": "Trigger a workflow run via workflow_dispatch",
        "properties": {
            "repo": _prop("Owner/repo"),
            "workflow_id": _prop("Workflow file name (e.g. ci.yml) or numeric id"),
            "ref": _prop("Git ref (branch or tag) to run on"),
            "inputs": _prop("Optional workflow inputs as a JSON object", "object"),
        },
        "required": ["repo", "workflow_id", "ref"],
    },
    "rerun_workflow_run": {
        "description": "Re-run all jobs of a workflow run",
        "properties": {
            "repo": _prop("Owner/repo"),
            "run_id": _prop("Workflow run ID", "integer"),
        },
        "required": ["repo", "run_id"],
    },
    "rerun_failed_jobs": {
        "description": "Re-run only the failed jobs of a workflow run",
        "properties": {
            "repo": _prop("Owner/repo"),
            "run_id": _prop("Workflow run ID", "integer"),
        },
        "required": ["repo", "run_id"],
    },
    "cancel_workflow_run": {
        "description": "Cancel an in-progress workflow run",
        "properties": {
            "repo": _prop("Owner/repo"),
            "run_id": _prop("Workflow run ID", "integer"),
        },
        "required": ["repo", "run_id"],
    },
    "list_run_artifacts": {
        "description": "List artifacts produced by a workflow run",
        "properties": {
            "repo": _prop("Owner/repo"),
            "run_id": _prop("Workflow run ID", "integer"),
            "per_page": _prop("Max results (default: 30)", "integer"),
        },
        "required": ["repo", "run_id"],
    },
    "get_commit_status": {
        "description": "Combined commit status for a ref/SHA/branch (use a PR's head ref for its CI status)",
        "properties": {
            "repo": _prop("Owner/repo"),
            "ref": _prop("Commit SHA, branch, or tag"),
        },
        "required": ["repo", "ref"],
    },
    "list_check_runs": {
        "description": "List check runs (CI checks) for a ref/SHA/branch",
        "properties": {
            "repo": _prop("Owner/repo"),
            "ref": _prop("Commit SHA, branch, or tag"),
            "per_page": _prop("Max results (default: 30)", "integer"),
        },
        "required": ["repo", "ref"],
    },

    # ── User ──
    "get_authenticated_user": {
        "description": "Get the authenticated user's profile",
        "properties": {},
        "required": [],
    },
    "get_user": {
        "description": "Get a GitHub user or org by login",
        "properties": {"username": _prop("GitHub login")},
        "required": ["username"],
    },
}

_HANDLERS = {
    # Issues
    "list_issues": _list_issues,
    "get_issue": _get_issue,
    "create_issue": _create_issue,
    "update_issue": _update_issue,
    "comment_issue": _comment_issue,
    "list_issue_comments": _list_issue_comments,
    "update_comment": _update_comment,
    "delete_comment": _delete_comment,
    "close_issue": _close_issue,
    "add_labels": _add_labels,
    "remove_label": _remove_label,
    "add_assignees": _add_assignees,
    # PRs
    "list_prs": _list_prs,
    "get_pr": _get_pr,
    "create_pr": _create_pr,
    "update_pr": _update_pr,
    "merge_pr": _merge_pr,
    "list_pr_files": _list_pr_files,
    "get_pr_diff": _get_pr_diff,
    "create_pr_review": _create_pr_review,
    "list_pr_reviews": _list_pr_reviews,
    "request_pr_review": _request_pr_review,
    "list_pr_review_comments": _list_pr_review_comments,
    "update_review_comment": _update_review_comment,
    "delete_review_comment": _delete_review_comment,
    # Code
    "search_code": _search_code,
    "read_file": _read_file,
    "list_files": _list_files,
    "create_or_update_file": _create_or_update_file,
    "delete_file": _delete_file,
    "push_files": _push_files,
    # Branches
    "list_branches": _list_branches,
    "get_branch": _get_branch,
    "create_branch": _create_branch,
    "delete_branch": _delete_branch,
    # Commits
    "list_commits": _list_commits,
    "get_commit": _get_commit,
    "compare_commits": _compare_commits,
    # Repos
    "repo_info": _repo_info,
    "list_repos": _list_repos,
    "create_repo": _create_repo,
    "fork_repo": _fork_repo,
    "list_forks": _list_forks,
    "list_collaborators": _list_collaborators,
    "list_tags": _list_tags,
    "list_topics": _list_topics,
    "list_releases": _list_releases,
    # Search
    "search_repos": _search_repos,
    "search_issues": _search_issues,
    "search_users": _search_users,
    # Actions
    "list_runs": _list_runs,
    "list_workflows": _list_workflows,
    "get_workflow_run": _get_workflow_run,
    "list_workflow_jobs": _list_workflow_jobs,
    "get_job_logs": _get_job_logs,
    "run_workflow": _run_workflow,
    "rerun_workflow_run": _rerun_workflow_run,
    "rerun_failed_jobs": _rerun_failed_jobs,
    "cancel_workflow_run": _cancel_workflow_run,
    "list_run_artifacts": _list_run_artifacts,
    "get_commit_status": _get_commit_status,
    "list_check_runs": _list_check_runs,
    # User
    "get_authenticated_user": _get_authenticated_user,
    "get_user": _get_user,
}


def _tool_def(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Build MCP tool definition."""
    return {
        "name": name,
        "description": spec["description"],
        "inputSchema": {
            "type": "object",
            "properties": spec.get("properties", {}),
            "required": spec.get("required", []),
        },
    }
