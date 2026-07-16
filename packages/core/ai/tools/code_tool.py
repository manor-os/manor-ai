"""
Code composite tool — coding capabilities in a single agent-bindable tool.

Same pattern as manor_tool.py: action + params routing with keyword search.
Ported from manor-multi-agent's code_tool.py, adapted for manor-os conventions.

Actions:
  plan        — structured planning before coding
  git         — git operations: status, diff, log, branch, commit, stash, worktree, merge, PR
  lsp         — code intelligence: definitions, references, symbols, diagnostics, hover
  review      — code review: diff analysis, quality checks, security scan
  test        — discover and run tests, coverage
  refactor    — rename, extract, inline, move symbols
  monitor     — background command execution with output streaming
  agent       — spawn sub-agents for parallel coding tasks
  notebook    — Jupyter notebook read/edit/run

Flow:
  1. LLM unsure what action to call:
     code(action="search", query="branch") -> matching actions + param schemas
  2. LLM knows the action:
     code(action="git_status") -> executes directly
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from typing import Any

from packages.core.ai.runtime.tool_context import (
    RUNTIME_TOOL_CONTEXT_KEYS,
    runtime_manual_skill_slugs_from_context,
)

logger = logging.getLogger(__name__)

# ── Action catalog ──────────────────────────────────────────────────────────

_ACTIONS: dict[str, list[tuple[str, str]]] = {
    "Dashboard Module": [
        (
            "dashboard_module_validate",
            "Validate Dashboard HTML, CSS, JavaScript, data requests, and Manor UI styling",
        ),
    ],
    "Planning": [
        ("plan_create", "Create implementation plan from task description with steps and dependencies"),
        ("plan_show", "Show current plan — goal, steps, progress"),
        ("plan_update", "Update plan step status (completed/pending/blocked) and notes"),
        ("plan_clear", "Clear current plan"),
    ],
    "Git": [
        ("git_status", "Working tree status: staged, unstaged, untracked files"),
        ("git_diff", "Show diff of staged/unstaged changes, or between commits/branches"),
        ("git_log", "Commit history with filters: author, date range, path, grep"),
        ("git_branch", "List, create, switch, or delete branches"),
        ("git_commit", "Stage files and create a commit with message"),
        ("git_stash", "Stash or pop working changes"),
        ("git_worktree", "Create isolated worktree for parallel branch work"),
        ("git_merge", "Merge a branch into current with conflict detection"),
        ("git_pr_create", "Create pull request via gh CLI"),
        ("git_pr_review", "Review a pull request: view, diff, checks"),
    ],
    "LSP": [
        ("lsp_definitions", "Jump to definition of a symbol (function, class, variable)"),
        ("lsp_references", "Find all references to a symbol across the codebase"),
        ("lsp_symbols", "List symbols in a file or search symbols across workspace"),
        ("lsp_diagnostics", "Get errors, warnings, and linting issues for a file or project"),
        ("lsp_hover", "Get type info and documentation for a symbol at a position"),
        ("lsp_completions", "Get code completions at a position"),
    ],
    "Review": [
        ("review_diff", "Analyze a diff for bugs, style issues, and improvements"),
        ("review_security", "Security-focused review: injection, auth, secrets, OWASP top 10"),
        ("review_quality", "Code quality: complexity, duplication, naming, dead code"),
        ("review_pr", "Full PR review combining diff analysis, quality, and security"),
    ],
    "Test": [
        ("test_discover", "Find test files and test functions in a project"),
        ("test_run", "Run tests with optional coverage"),
        ("test_single", "Run a specific test by name or path"),
        ("test_coverage", "Run tests with coverage and report uncovered lines"),
    ],
    "Refactor": [
        ("refactor_rename", "Rename a symbol across all files (function, class, variable)"),
        ("refactor_extract", "Extract code block into a new function or method"),
        ("refactor_inline", "Inline a function/variable at all call sites"),
        ("refactor_move", "Move a symbol or file to a new location with import updates"),
    ],
    "Monitor": [
        ("monitor_start", "Start a background command and return its ID"),
        ("monitor_output", "Get stdout/stderr from a running or completed background command"),
        ("monitor_stop", "Stop a running background command"),
        ("monitor_list", "List all active and recent background commands"),
    ],
    "Agent": [
        ("agent_spawn", "Spawn sub-agent for a parallel coding task with its own context"),
        ("agent_status", "Check sub-agent status"),
        ("agent_collect", "Collect sub-agent results"),
    ],
    "Notebook": [
        ("notebook_read", "Read a Jupyter notebook — cells, outputs, metadata"),
        ("notebook_edit", "Edit a notebook cell (code or markdown)"),
        ("notebook_run", "Execute a notebook cell or range of cells"),
        ("notebook_add", "Add a new cell to a notebook"),
    ],
}

# Flatten for lookup
_ALL_ACTIONS: dict[str, str] = {}
_ACTION_DEPT: dict[str, str] = {}
for _dept, _actions in _ACTIONS.items():
    for _name, _desc in _actions:
        _ALL_ACTIONS[_name] = _desc
        _ACTION_DEPT[_name] = _dept


def get_code_action_names() -> set[str]:
    return set(_ALL_ACTIONS.keys())


def _search_actions(query: str, max_results: int = 8) -> list[dict]:
    query_lower = query.lower()
    scored: list[tuple[int, str, str, str]] = []
    for name, desc in _ALL_ACTIONS.items():
        dept = _ACTION_DEPT[name]
        score = 0
        for word in query_lower.split():
            if word in name.lower():
                score += 3
            elif word in desc.lower():
                score += 1
            elif word in dept.lower():
                score += 1
        if score > 0:
            scored.append((score, name, desc, dept))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"action": name, "description": desc, "category": dept}
        for _, name, desc, dept in scored[:max_results]
    ]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_cwd(params: dict) -> str:
    """Get working directory from params, or fall back to cwd/home."""
    cwd = params.get("cwd", "")
    if cwd and os.path.isdir(cwd):
        return cwd
    return os.getcwd()


def _run_cmd(cmd: list[str], cwd: str, timeout: int = 30) -> dict:
    """Run a command and return {ok, stdout, stderr, exit_code}."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "Command timed out", "exit_code": -1}
    except FileNotFoundError as e:
        return {"ok": False, "stdout": "", "stderr": str(e), "exit_code": -1}


def _git_cmd(args: list[str], cwd: str, timeout: int = 30) -> dict:
    """Run a git command."""
    return _run_cmd(["git"] + args, cwd, timeout)


# ── Background monitor state ────────────────────────────────────────────────

_MONITORS: dict[str, dict] = {}
_MONITOR_COUNTER = 0

# ── Plan state (in-memory, keyed by entity_id) ─────────────────────────────

_PLANS: dict[str, dict] = {}  # entity_id -> plan


def _get_plan(entity_id: str) -> dict:
    if entity_id not in _PLANS:
        _PLANS[entity_id] = {"goal": "", "steps": [], "created_at": None}
    return _PLANS[entity_id]


# ══════════════════════════════════════════════════════════════════════════════
# Action handlers
# ══════════════════════════════════════════════════════════════════════════════


async def _handle_dashboard_module_validate(params: dict, _entity_id: str) -> str:
    from packages.core.ai.runtime.dashboard_module_validation import (
        validate_dashboard_module_code,
    )
    from packages.core.ai.runtime.dashboard_submission import (
        runtime_record_dashboard_validation,
    )

    code = params.get("code")
    result = validate_dashboard_module_code(code)
    recorded = False
    if result.get("platform_ready") and isinstance(code, dict):
        recorded = runtime_record_dashboard_validation(code)
    return json.dumps(
        {
            **result,
            "recorded_for_dashboard_submission": recorded,
            "next_step": (
                "Submit this exact code bundle with dashboard_submit_module."
                if result.get("platform_ready")
                else "Revise every error and warning, then validate the complete bundle again."
            ),
        },
        ensure_ascii=False,
    )

# ── Planning ─────────────────────────────────────────────────────────────────

async def _handle_plan_create(params: dict, entity_id: str) -> str:
    goal = params.get("goal", "")
    steps = params.get("steps", [])
    if not goal:
        return json.dumps({"error": "goal is required — describe what you want to implement"})

    plan = _get_plan(entity_id)
    plan["goal"] = goal
    plan["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if steps:
        plan["steps"] = [
            {"id": s.get("id", f"step_{i+1}"), "description": s.get("description", ""),
             "status": "pending", "files": s.get("files", []), "notes": ""}
            for i, s in enumerate(steps)
        ]
    else:
        plan["steps"] = []

    return json.dumps({
        "plan_created": True,
        "goal": goal,
        "steps": len(plan["steps"]),
        "hint": "Use plan_show to check progress, plan_update to mark steps done.",
    })


async def _handle_plan_show(params: dict, entity_id: str) -> str:
    plan = _get_plan(entity_id)
    if not plan.get("goal"):
        return json.dumps({"error": "No active plan. Use code(action='plan_create') to create one."})

    completed = sum(1 for s in plan["steps"] if s.get("status") == "completed")
    total = len(plan["steps"])
    return json.dumps({
        "goal": plan["goal"],
        "progress": f"{completed}/{total} steps completed",
        "steps": plan["steps"],
        "created_at": plan.get("created_at"),
    })


async def _handle_plan_update(params: dict, entity_id: str) -> str:
    step_id = params.get("step_id", "")
    status = params.get("status", "completed")
    notes = params.get("notes", "")

    plan = _get_plan(entity_id)
    for step in plan.get("steps", []):
        if step["id"] == step_id:
            step["status"] = status
            if notes:
                step["notes"] = notes
            return json.dumps({"updated": True, "step_id": step_id, "status": status})

    return json.dumps({"error": f"Step '{step_id}' not found in plan"})


async def _handle_plan_clear(params: dict, entity_id: str) -> str:
    _PLANS.pop(entity_id, None)
    return json.dumps({"cleared": True})


# ── Git ──────────────────────────────────────────────────────────────────────

async def _handle_git_status(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    r = _git_cmd(["status", "--porcelain=v2", "--branch"], cwd)
    if not r["ok"]:
        return f"git status failed: {r['stderr']}"
    return r["stdout"] or "(clean working tree)"


async def _handle_git_diff(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    args = ["diff"]
    if params.get("staged"):
        args.append("--cached")
    if params.get("stat"):
        args.append("--stat")
    target = params.get("target", "")
    if target:
        args.append(target)
    path = params.get("path", "")
    if path:
        args.extend(["--", path])

    r = _git_cmd(args, cwd)
    if not r["ok"]:
        return f"git diff failed: {r['stderr']}"
    output = r["stdout"]
    if len(output) > 50000:
        output = output[:50000] + f"\n\n... (truncated, {len(r['stdout'])} chars total)"
    return output or "(no changes)"


async def _handle_git_log(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    n = min(int(params.get("n") or 20), 100)
    args = ["log", f"-{n}", "--format=%h %s (%an, %ar)"]
    if params.get("author"):
        args.append(f"--author={params['author']}")
    if params.get("since"):
        args.append(f"--since={params['since']}")
    if params.get("until"):
        args.append(f"--until={params['until']}")
    if params.get("grep"):
        args.extend(["--grep", params["grep"]])
    if params.get("path"):
        args.extend(["--", params["path"]])
    if params.get("oneline"):
        args = ["log", f"-{n}", "--oneline"]

    r = _git_cmd(args, cwd)
    if not r["ok"]:
        return f"git log failed: {r['stderr']}"
    return r["stdout"] or "(no commits)"


async def _handle_git_branch(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    op = params.get("op", "list")

    if op == "list":
        r = _git_cmd(["branch", "-a", "--format=%(refname:short) %(objectname:short) %(subject)"], cwd)
        return r["stdout"] if r["ok"] else f"git branch failed: {r['stderr']}"

    if op == "create":
        name = params.get("name", "")
        if not name:
            return "branch name required"
        base = params.get("base", "")
        args = ["checkout", "-b", name]
        if base:
            args.append(base)
        r = _git_cmd(args, cwd)
        return f"Created and switched to branch '{name}'" if r["ok"] else f"Failed: {r['stderr']}"

    if op == "switch":
        name = params.get("name", "")
        if not name:
            return "branch name required"
        r = _git_cmd(["checkout", name], cwd)
        return f"Switched to '{name}'" if r["ok"] else f"Failed: {r['stderr']}"

    if op == "delete":
        name = params.get("name", "")
        if not name:
            return "branch name required"
        r = _git_cmd(["branch", "-d", name], cwd)
        return f"Deleted branch '{name}'" if r["ok"] else f"Failed: {r['stderr']}"

    return f"Unknown branch op: {op}. Use list/create/switch/delete."


async def _handle_git_commit(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    message = params.get("message", "")
    if not message:
        return "commit message required"

    files = params.get("files", [])
    if files:
        r = _git_cmd(["add"] + files, cwd)
        if not r["ok"]:
            return f"git add failed: {r['stderr']}"
    elif params.get("all"):
        r = _git_cmd(["add", "-A"], cwd)
        if not r["ok"]:
            return f"git add failed: {r['stderr']}"

    r = _git_cmd(["commit", "-m", message], cwd)
    if not r["ok"]:
        return f"git commit failed: {r['stderr']}"
    return r["stdout"]


async def _handle_git_stash(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    op = params.get("op", "push")

    if op == "push":
        msg = params.get("message", "")
        args = ["stash", "push"]
        if msg:
            args.extend(["-m", msg])
        r = _git_cmd(args, cwd)
        return r["stdout"] if r["ok"] else f"Failed: {r['stderr']}"

    if op == "pop":
        r = _git_cmd(["stash", "pop"], cwd)
        return r["stdout"] if r["ok"] else f"Failed: {r['stderr']}"

    if op == "list":
        r = _git_cmd(["stash", "list"], cwd)
        return r["stdout"] or "(no stashes)" if r["ok"] else f"Failed: {r['stderr']}"

    if op == "drop":
        idx = params.get("index", "0")
        r = _git_cmd(["stash", "drop", f"stash@{{{idx}}}"], cwd)
        return r["stdout"] if r["ok"] else f"Failed: {r['stderr']}"

    return f"Unknown stash op: {op}. Use push/pop/list/drop."


async def _handle_git_worktree(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    op = params.get("op", "list")

    if op == "list":
        r = _git_cmd(["worktree", "list", "--porcelain"], cwd)
        return r["stdout"] if r["ok"] else f"Failed: {r['stderr']}"

    if op == "add":
        path = params.get("path", "")
        branch = params.get("branch", "")
        if not path:
            return "worktree path required"
        args = ["worktree", "add", path]
        if branch:
            args.extend(["-b", branch])
        r = _git_cmd(args, cwd)
        return r["stdout"] if r["ok"] else f"Failed: {r['stderr']}"

    if op == "remove":
        path = params.get("path", "")
        if not path:
            return "worktree path required"
        r = _git_cmd(["worktree", "remove", path], cwd)
        return r["stdout"] if r["ok"] else f"Failed: {r['stderr']}"

    return f"Unknown worktree op: {op}. Use list/add/remove."


async def _handle_git_merge(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    branch = params.get("branch", "")
    if not branch:
        return "branch to merge is required"

    args = ["merge", branch]
    if params.get("no_ff"):
        args.append("--no-ff")
    if params.get("message"):
        args.extend(["-m", params["message"]])

    r = _git_cmd(args, cwd)
    if not r["ok"]:
        conflict_check = _git_cmd(["diff", "--name-only", "--diff-filter=U"], cwd)
        if conflict_check["ok"] and conflict_check["stdout"]:
            return json.dumps({
                "status": "conflict",
                "conflicted_files": conflict_check["stdout"].split("\n"),
                "message": r["stderr"],
                "hint": "Resolve conflicts in the listed files, then git_commit.",
            })
        return f"Merge failed: {r['stderr']}"
    return r["stdout"] or f"Merged '{branch}' successfully."


async def _handle_git_pr_create(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    title = params.get("title", "")
    body = params.get("body", "")
    base = params.get("base", "")
    if not title:
        return "PR title required"
    args = ["pr", "create", "--title", title]
    if body:
        args.extend(["--body", body])
    if base:
        args.extend(["--base", base])
    r = _run_cmd(["gh"] + args, cwd, timeout=30)
    return r["stdout"] if r["ok"] else f"gh pr create failed: {r['stderr']}"


async def _handle_git_pr_review(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    op = params.get("op", "view")
    number = params.get("number", "")

    if op == "list":
        state_filter = params.get("state", "open")
        r = _run_cmd(["gh", "pr", "list", "--state", state_filter, "--limit", "20"], cwd)
        return r["stdout"] or "(no PRs)" if r["ok"] else f"gh pr list failed: {r['stderr']}"

    if not number:
        return "PR number required"

    if op == "view":
        r = _run_cmd(["gh", "pr", "view", str(number)], cwd)
        return r["stdout"] if r["ok"] else f"gh pr view failed: {r['stderr']}"

    if op == "diff":
        r = _run_cmd(["gh", "pr", "diff", str(number)], cwd)
        output = r["stdout"]
        if len(output) > 50000:
            output = output[:50000] + "\n\n... (truncated)"
        return output if r["ok"] else f"gh pr diff failed: {r['stderr']}"

    if op == "checks":
        r = _run_cmd(["gh", "pr", "checks", str(number)], cwd)
        return r["stdout"] if r["ok"] else f"gh pr checks failed: {r['stderr']}"

    return f"Unknown PR op: {op}. Use list/view/diff/checks."


# ── LSP (grep/ctags-based heuristics) ───────────────────────────────────────

async def _handle_lsp_definitions(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    symbol = params.get("symbol", "")
    if not symbol:
        return "symbol name required"

    file_glob = params.get("file_glob", "")

    # Build definition patterns based on common language constructs
    patterns = [
        rf"(def|function|func|fn)\s+{re.escape(symbol)}\s*[\(\[]",
        rf"(class|struct|interface|type|enum)\s+{re.escape(symbol)}\b",
        rf"(const|let|var|val)\s+{re.escape(symbol)}\s*[=:]",
        rf"{re.escape(symbol)}\s*=\s*(function|class|\()",
    ]
    combined = "|".join(f"({p})" for p in patterns)

    args = ["rg", "--line-number", "--no-heading", "-e", combined]
    if file_glob:
        args.extend(["--glob", file_glob])
    args.extend(["--max-count", "20", "--max-filesize", "1M", cwd])

    r = _run_cmd(args, cwd, timeout=10)
    if not r["ok"] or not r["stdout"]:
        # Fallback: simple grep
        r2 = _run_cmd(["rg", "--line-number", "--no-heading", "-w", symbol, "--max-count", "10", cwd], cwd, timeout=10)
        return r2["stdout"][:5000] if r2["ok"] and r2["stdout"] else f"No definition found for '{symbol}'"

    output = r["stdout"].replace(cwd + "/", "")
    return output[:10000] if len(output) > 10000 else output


async def _handle_lsp_references(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    symbol = params.get("symbol", "")
    if not symbol:
        return "symbol name required"

    args = ["rg", "--line-number", "--no-heading", "-w", re.escape(symbol)]
    file_glob = params.get("file_glob", "")
    if file_glob:
        args.extend(["--glob", file_glob])
    args.extend(["--max-count", "50", "--max-filesize", "1M", cwd])

    r = _run_cmd(args, cwd, timeout=15)
    if not r["ok"] or not r["stdout"]:
        return f"No references found for '{symbol}'"

    output = r["stdout"].replace(cwd + "/", "")
    lines = output.split("\n")
    result = f"Found {len(lines)} reference(s) for '{symbol}':\n\n" + "\n".join(lines[:100])
    if len(lines) > 100:
        result += f"\n\n... ({len(lines)} total, showing first 100)"
    return result


async def _handle_lsp_symbols(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    file_path = params.get("file", "")
    query = params.get("query", "")

    if file_path:
        full = os.path.join(cwd, file_path)
        if os.path.isfile(full):
            patterns = r"(def |class |function |const |let |var |type |interface |struct |enum )"
            r = _run_cmd(["rg", "--line-number", "--no-heading", patterns, full], cwd, timeout=10)
            if r["ok"] and r["stdout"]:
                return f"Symbols in {file_path}:\n{r['stdout'].replace(cwd + '/', '')}"
            return f"No symbols found in {file_path}"
        return f"File not found: {file_path}"

    if query:
        return await _handle_lsp_definitions({"symbol": query, "cwd": cwd, **params}, entity_id)

    return "Provide 'file' for file symbols or 'query' to search workspace."


async def _handle_lsp_diagnostics(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    file_path = params.get("file", "")
    tool = params.get("tool", "")

    if not tool:
        if os.path.isfile(os.path.join(cwd, "pyproject.toml")) or os.path.isfile(os.path.join(cwd, "setup.py")):
            tool = "ruff"
        elif os.path.isfile(os.path.join(cwd, "tsconfig.json")):
            tool = "tsc"
        elif os.path.isfile(os.path.join(cwd, "package.json")):
            tool = "eslint"
        else:
            return "Cannot auto-detect linter. Specify 'tool' param (ruff, mypy, eslint, tsc, etc.)"

    if tool == "ruff":
        args = ["ruff", "check", "--output-format", "text"]
        args.append(file_path if file_path else ".")
    elif tool == "mypy":
        args = ["mypy", "--no-error-summary"]
        args.append(file_path if file_path else ".")
    elif tool in ("tsc", "typescript"):
        args = ["npx", "tsc", "--noEmit"]
    elif tool == "eslint":
        args = ["npx", "eslint"]
        args.append(file_path if file_path else ".")
    else:
        args = [tool]
        if file_path:
            args.append(file_path)

    r = _run_cmd(args, cwd, timeout=60)
    output = (r["stdout"] + "\n" + r["stderr"]).strip()
    if not output:
        return f"No diagnostics ({tool}): all clean!"
    if len(output) > 20000:
        output = output[:20000] + "\n... (truncated)"
    return output


async def _handle_lsp_hover(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    symbol = params.get("symbol", "")
    file_path = params.get("file", "")
    if not symbol:
        return "symbol required"

    def_result = await _handle_lsp_definitions({"symbol": symbol, "file_glob": file_path or "", "cwd": cwd}, entity_id)

    if def_result and ":" in def_result:
        first_line = def_result.split("\n")[0]
        parts = first_line.split(":")
        if len(parts) >= 2:
            try:
                fpath = parts[0].strip()
                lineno = int(parts[1].strip())
                full = os.path.join(cwd, fpath)
                if os.path.isfile(full):
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        all_lines = f.readlines()
                    start = max(0, lineno - 6)
                    end = min(len(all_lines), lineno + 10)
                    context = "".join(all_lines[start:end])
                    return f"**{symbol}** defined at {fpath}:{lineno}\n\n```\n{context}```"
            except (ValueError, IndexError):
                pass

    return def_result or f"No info found for '{symbol}'"


async def _handle_lsp_completions(params: dict, entity_id: str) -> str:
    return "Completions are best provided by the LLM directly based on file context. Use lsp_symbols or lsp_hover instead."


# ── Review ───────────────────────────────────────────────────────────────────

async def _handle_review_diff(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    target = params.get("target", "HEAD")
    path = params.get("path", "")

    args = ["diff", target]
    if params.get("stat"):
        args.append("--stat")
    if path:
        args.extend(["--", path])

    r = _git_cmd(args, cwd)
    if not r["ok"]:
        return f"git diff failed: {r['stderr']}"

    diff = r["stdout"]
    if not diff:
        return "(no changes to review)"

    stat_r = _git_cmd(["diff", target, "--stat"], cwd)
    stat = stat_r["stdout"] if stat_r["ok"] else ""

    if len(diff) > 80000:
        diff = diff[:80000] + f"\n\n... (truncated, {len(r['stdout'])} chars total)"

    return f"## Changed files\n{stat}\n\n## Diff\n```diff\n{diff}\n```\n\nAnalyze the above diff for bugs, style issues, and improvements."


async def _handle_review_security(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    path = params.get("path", ".")

    security_patterns = {
        "hardcoded_secrets": r"(password|secret|api_key|token)\s*=\s*['\"][^'\"]+['\"]",
        "sql_injection": r"(execute|cursor\.execute|query)\s*\(\s*['\"].*%s",
        "command_injection": r"(os\.system|subprocess\.call|eval|exec)\s*\(",
        "insecure_http": r"http://(?!localhost|127\.0\.0\.1)",
        "debug_left_on": r"(DEBUG\s*=\s*True|console\.log\(|print\(.*password)",
        "todo_security": r"(TODO|FIXME|HACK|XXX).*(?i)(security|auth|token|secret|password)",
    }

    findings = []
    for label, pattern in security_patterns.items():
        r = _run_cmd(
            ["rg", "--line-number", "--no-heading", "-e", pattern, "--max-count", "10",
             "--max-filesize", "1M", os.path.join(cwd, path)],
            cwd, timeout=10,
        )
        if r["ok"] and r["stdout"]:
            matches = r["stdout"].replace(cwd + "/", "")
            findings.append(f"### {label.replace('_', ' ').title()}\n{matches}")

    if not findings:
        return "No common security issues detected. (Pattern-based scan — manual review recommended.)"

    return "## Security Scan Results\n\n" + "\n\n".join(findings) + \
           "\n\n---\n*Pattern-based scan. Review each finding for false positives.*"


async def _handle_review_quality(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    path = params.get("path", "")

    results = []

    diag = await _handle_lsp_diagnostics({"file": path, "cwd": cwd}, entity_id)
    if "No diagnostics" not in diag and "Cannot auto-detect" not in diag:
        results.append(f"## Lint/Type Issues\n{diag[:5000]}")

    target_path = os.path.join(cwd, path) if path else cwd
    r = _run_cmd(
        ["rg", "--line-number", "--no-heading", "-e",
         r"^(def |class |function |async function |const \w+ = )",
         "--max-filesize", "1M", target_path],
        cwd, timeout=10,
    )
    if r["ok"] and r["stdout"]:
        results.append(f"## Function/Class Definitions\n{r['stdout'].replace(cwd + '/', '')[:3000]}")

    r2 = _run_cmd(
        ["rg", "--line-number", "--no-heading", "-e", r"(TODO|FIXME|HACK|XXX)\b",
         "--max-count", "30", "--max-filesize", "1M", target_path],
        cwd, timeout=10,
    )
    if r2["ok"] and r2["stdout"]:
        results.append(f"## TODOs/FIXMEs\n{r2['stdout'].replace(cwd + '/', '')}")

    return "\n\n".join(results) if results else "No quality issues found."


async def _handle_review_pr(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    number = params.get("number", "")

    if number:
        r = _run_cmd(["gh", "pr", "diff", str(number)], cwd, timeout=30)
        if not r["ok"]:
            return f"Cannot get PR #{number}: {r['stderr']}"
        diff = r["stdout"]
    else:
        base = params.get("base", "main")
        r = _git_cmd(["diff", f"{base}...HEAD"], cwd)
        diff = r["stdout"] if r["ok"] else ""
        if not diff:
            return f"No changes between {base} and HEAD"

    if len(diff) > 80000:
        diff = diff[:80000] + "\n... (truncated)"

    security = await _handle_review_security({"cwd": cwd, **params}, entity_id)

    return (
        f"## PR Review Data\n\n"
        f"### Diff ({len(diff)} chars)\n```diff\n{diff}\n```\n\n"
        f"### Security Scan\n{security}\n\n"
        f"Analyze the above for: bugs, logic errors, edge cases, performance, naming, "
        f"and adherence to project conventions."
    )


# ── Test ─────────────────────────────────────────────────────────────────────

async def _handle_test_discover(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    framework = params.get("framework", "")

    if not framework:
        if os.path.isfile(os.path.join(cwd, "pytest.ini")) or os.path.isfile(os.path.join(cwd, "conftest.py")):
            framework = "pytest"
        elif os.path.isfile(os.path.join(cwd, "jest.config.js")) or os.path.isfile(os.path.join(cwd, "jest.config.ts")):
            framework = "jest"
        elif os.path.isfile(os.path.join(cwd, "vitest.config.ts")):
            framework = "vitest"
        elif os.path.isfile(os.path.join(cwd, "go.mod")):
            framework = "go"

    if framework == "pytest":
        r = _run_cmd(["python", "-m", "pytest", "--collect-only", "-q"], cwd, timeout=30)
    elif framework in ("jest", "vitest"):
        r = _run_cmd(
            ["rg", "--files", "--glob", "**/*.{test,spec}.{js,ts,jsx,tsx}", cwd],
            cwd, timeout=10,
        )
    elif framework == "go":
        r = _run_cmd(["go", "test", "-list", ".", "./..."], cwd, timeout=30)
    else:
        r = _run_cmd(
            ["rg", "--files", "--glob", "**/test_*", "--glob", "**/*_test.*",
             "--glob", "**/*.test.*", "--glob", "**/*.spec.*", cwd],
            cwd, timeout=10,
        )

    if not r["ok"] or not r["stdout"]:
        return f"No tests found (framework={framework or 'auto-detect'}). Check your project structure."

    output = r["stdout"].replace(cwd + "/", "")
    lines = output.split("\n")
    return f"Found {len(lines)} test(s):\n\n{output[:5000]}"


async def _handle_test_run(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    target = params.get("target", "")
    framework = params.get("framework", "")
    verbose = params.get("verbose", False)

    if not framework:
        if os.path.isfile(os.path.join(cwd, "pytest.ini")) or os.path.isfile(os.path.join(cwd, "conftest.py")) or os.path.isfile(os.path.join(cwd, "pyproject.toml")):
            framework = "pytest"
        elif os.path.isfile(os.path.join(cwd, "package.json")):
            framework = "npm"
        elif os.path.isfile(os.path.join(cwd, "go.mod")):
            framework = "go"

    if framework == "pytest":
        args = ["python", "-m", "pytest"]
        if verbose:
            args.append("-v")
        if target:
            args.append(target)
    elif framework in ("npm", "jest", "vitest"):
        args = ["npm", "test", "--"]
        if target:
            args.append(target)
    elif framework == "go":
        args = ["go", "test"]
        if verbose:
            args.append("-v")
        args.append(target if target else "./...")
    else:
        args = params.get("command", "").split() if params.get("command") else ["make", "test"]

    r = _run_cmd(args, cwd, timeout=120)
    output = (r["stdout"] + "\n" + r["stderr"]).strip()
    status = "PASSED" if r["ok"] else "FAILED"

    if len(output) > 30000:
        output = output[:30000] + "\n... (truncated)"

    return f"## Test Results: {status}\n\n```\n{output}\n```"


async def _handle_test_single(params: dict, entity_id: str) -> str:
    """Run a specific test by name or path."""
    cwd = _get_cwd(params)
    test = params.get("test", "")
    if not test:
        return "test name or path required"

    framework = params.get("framework", "")
    if not framework:
        if os.path.isfile(os.path.join(cwd, "pyproject.toml")) or os.path.isfile(os.path.join(cwd, "conftest.py")):
            framework = "pytest"
        elif os.path.isfile(os.path.join(cwd, "package.json")):
            framework = "npm"
        elif os.path.isfile(os.path.join(cwd, "go.mod")):
            framework = "go"

    if framework == "pytest":
        args = ["python", "-m", "pytest", "-v", test]
    elif framework in ("npm", "jest", "vitest"):
        args = ["npx", "jest", "--testPathPattern", test, "--verbose"]
    elif framework == "go":
        args = ["go", "test", "-v", "-run", test, "./..."]
    else:
        args = ["python", "-m", "pytest", "-v", test]

    r = _run_cmd(args, cwd, timeout=120)
    output = (r["stdout"] + "\n" + r["stderr"]).strip()
    status = "PASSED" if r["ok"] else "FAILED"

    if len(output) > 30000:
        output = output[:30000] + "\n... (truncated)"

    return f"## Test Result: {status}\n\n```\n{output}\n```"


async def _handle_test_coverage(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    framework = params.get("framework", "")
    target = params.get("target", "")

    if not framework:
        if os.path.isfile(os.path.join(cwd, "pyproject.toml")) or os.path.isfile(os.path.join(cwd, "setup.py")):
            framework = "pytest"
        elif os.path.isfile(os.path.join(cwd, "package.json")):
            framework = "npm"
        elif os.path.isfile(os.path.join(cwd, "go.mod")):
            framework = "go"

    if framework == "pytest":
        args = ["python", "-m", "pytest", "--cov", "--cov-report=term-missing"]
        if target:
            args.append(target)
    elif framework == "go":
        args = ["go", "test", "-coverprofile=coverage.out", "./..."]
    else:
        args = ["npm", "test", "--", "--coverage"]

    r = _run_cmd(args, cwd, timeout=120)
    output = (r["stdout"] + "\n" + r["stderr"]).strip()

    if framework == "go" and r["ok"]:
        r2 = _run_cmd(["go", "tool", "cover", "-func=coverage.out"], cwd, timeout=15)
        if r2["ok"]:
            output += "\n\n" + r2["stdout"]

    if len(output) > 30000:
        output = output[:30000] + "\n... (truncated)"
    return f"## Coverage Report\n\n```\n{output}\n```"


# ── Refactor ─────────────────────────────────────────────────────────────────

async def _handle_refactor_rename(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    old_name = params.get("old_name", "")
    new_name = params.get("new_name", "")
    if not old_name or not new_name:
        return "old_name and new_name required"

    file_glob = params.get("file_glob", "")

    args = ["rg", "--line-number", "--no-heading", "-w", re.escape(old_name), "--max-filesize", "1M"]
    if file_glob:
        args.extend(["--glob", file_glob])
    args.append(cwd)

    r = _run_cmd(args, cwd, timeout=15)
    if not r["ok"] or not r["stdout"]:
        return f"No occurrences of '{old_name}' found"

    lines = r["stdout"].replace(cwd + "/", "").split("\n")
    files = set()
    for line in lines:
        if ":" in line:
            files.add(line.split(":")[0])

    if params.get("dry_run", True):
        return json.dumps({
            "symbol": old_name,
            "new_name": new_name,
            "occurrences": len(lines),
            "files": sorted(files),
            "preview": "\n".join(lines[:20]),
            "hint": "Set dry_run=false to apply the rename, or use write_file for precise control.",
        }, ensure_ascii=False)

    for fpath in sorted(files):
        full_path = os.path.join(cwd, fpath)
        if os.path.isfile(full_path):
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    content = f.read()
                updated = re.sub(rf"\b{re.escape(old_name)}\b", new_name, content)
                if updated != content:
                    with open(full_path, "w", encoding="utf-8") as f:
                        f.write(updated)
            except Exception as e:
                logger.warning("[refactor_rename] error on %s: %s", fpath, e)

    return json.dumps({
        "renamed": True,
        "old_name": old_name,
        "new_name": new_name,
        "files_modified": len(files),
        "hint": "Review changes with git_diff before committing.",
    })


async def _handle_refactor_extract(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    file_path = params.get("file", "")
    start_line = params.get("start_line", 0)
    end_line = params.get("end_line", 0)
    new_name = params.get("new_name", "")

    if not file_path:
        return "file path required"

    full = os.path.join(cwd, file_path)
    if not os.path.isfile(full):
        return f"File not found: {file_path}"

    with open(full, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    if start_line and end_line:
        selected = all_lines[max(0, start_line-1):end_line]
        context_before = all_lines[max(0, start_line-10):start_line-1]
        context_after = all_lines[end_line:min(len(all_lines), end_line+5)]

        return (
            f"## Extract Function: {new_name or '(unnamed)'}\n"
            f"File: {file_path} lines {start_line}-{end_line}\n\n"
            f"### Context Before\n```\n{''.join(context_before)}```\n\n"
            f"### Code to Extract\n```\n{''.join(selected)}```\n\n"
            f"### Context After\n```\n{''.join(context_after)}```\n\n"
            f"Extract the selected code into a function named '{new_name}'. "
            f"Identify parameters and return values."
        )

    return f"Provide start_line and end_line to identify code to extract from {file_path}"


async def _handle_refactor_inline(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    symbol = params.get("symbol", "")
    if not symbol:
        return "symbol name required"

    def_result = await _handle_lsp_definitions({"symbol": symbol, "cwd": cwd}, entity_id)
    ref_result = await _handle_lsp_references({"symbol": symbol, "cwd": cwd}, entity_id)

    return (
        f"## Inline '{symbol}'\n\n"
        f"### Definition\n{def_result}\n\n"
        f"### Call Sites\n{ref_result}\n\n"
        f"Replace each call site with the function body, adapting parameter names."
    )


async def _handle_refactor_move(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    source = params.get("source", "")
    destination = params.get("destination", "")
    if not source or not destination:
        return "source and destination required"

    source_full = os.path.join(cwd, source)
    if os.path.isfile(source_full):
        basename = os.path.splitext(os.path.basename(source))[0]
        r = _run_cmd(
            ["rg", "--line-number", "--no-heading", "-e",
             f"(import|from|require).*{re.escape(basename)}", "--max-filesize", "1M", cwd],
            cwd, timeout=10,
        )
        imports = r["stdout"].replace(cwd + "/", "") if r["ok"] else ""
        return (
            f"## Move File: {source} -> {destination}\n\n"
            f"### Import References\n{imports or '(none found)'}\n\n"
            f"Move the file and update all import paths."
        )
    else:
        def_result = await _handle_lsp_definitions({"symbol": source, "cwd": cwd}, entity_id)
        ref_result = await _handle_lsp_references({"symbol": source, "cwd": cwd}, entity_id)
        return (
            f"## Move Symbol: '{source}' -> {destination}\n\n"
            f"### Definition\n{def_result}\n\n"
            f"### References\n{ref_result}\n\n"
            f"Move the symbol to {destination} and update all references."
        )


# ── Monitor ──────────────────────────────────────────────────────────────────

async def _handle_monitor_start(params: dict, entity_id: str) -> str:
    global _MONITOR_COUNTER
    cmd = params.get("command", "")
    if not cmd:
        return "command required"

    cwd = _get_cwd(params)
    _MONITOR_COUNTER += 1
    mid = f"mon_{_MONITOR_COUNTER}"

    try:
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, text=True, bufsize=1,
        )
        _MONITORS[mid] = {
            "process": proc,
            "cmd": cmd,
            "cwd": cwd,
            "started": time.time(),
            "pid": proc.pid,
        }
        return json.dumps({"id": mid, "pid": proc.pid, "command": cmd, "status": "running"})
    except Exception as e:
        return json.dumps({"error": f"Failed to start: {e}"})


async def _handle_monitor_output(params: dict, entity_id: str) -> str:
    mid = params.get("id", "")
    if not mid or mid not in _MONITORS:
        return f"Monitor '{mid}' not found. Use monitor_list to see active monitors."

    mon = _MONITORS[mid]
    proc = mon["process"]

    poll = proc.poll()
    if poll is not None:
        stdout = proc.stdout.read() if proc.stdout else ""
        stderr = proc.stderr.read() if proc.stderr else ""
        elapsed = time.time() - mon["started"]
        return json.dumps({
            "id": mid,
            "status": "completed",
            "exit_code": poll,
            "elapsed": f"{elapsed:.1f}s",
            "stdout": stdout[:20000],
            "stderr": stderr[:5000],
        })

    elapsed = time.time() - mon["started"]
    return json.dumps({
        "id": mid,
        "status": "running",
        "pid": mon["pid"],
        "elapsed": f"{elapsed:.1f}s",
        "hint": "Process still running. Check again later or use monitor_stop.",
    })


async def _handle_monitor_stop(params: dict, entity_id: str) -> str:
    mid = params.get("id", "")
    if not mid or mid not in _MONITORS:
        return f"Monitor '{mid}' not found."

    mon = _MONITORS[mid]
    proc = mon["process"]
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    del _MONITORS[mid]
    return json.dumps({"id": mid, "status": "stopped"})


async def _handle_monitor_list(params: dict, entity_id: str) -> str:
    if not _MONITORS:
        return "No active monitors."

    items = []
    for mid, mon in _MONITORS.items():
        poll = mon["process"].poll()
        items.append({
            "id": mid,
            "command": mon["cmd"][:80],
            "status": "completed" if poll is not None else "running",
            "exit_code": poll,
            "elapsed": f"{time.time() - mon['started']:.1f}s",
            "pid": mon["pid"],
        })
    return json.dumps(items, indent=2)


# ── Agent ────────────────────────────────────────────────────────────────────

async def _handle_agent_spawn(params: dict, entity_id: str) -> str:
    task = params.get("task", "")
    if not task:
        return "task description required"
    return json.dumps({
        "status": "stub",
        "task": task,
        "hint": "Sub-agent spawning requires orchestrator integration. Use bash or monitor_start for direct execution.",
    })


async def _handle_agent_status(params: dict, entity_id: str) -> str:
    return "Sub-agent status tracking requires orchestrator integration. Check conversation history for results."


async def _handle_agent_collect(params: dict, entity_id: str) -> str:
    return "Sub-agent result collection requires orchestrator integration. Check conversation history for results."


# ── Notebook ─────────────────────────────────────────────────────────────────

async def _handle_notebook_read(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    file_path = params.get("file", "")
    if not file_path:
        return "file path required"

    full = os.path.join(cwd, file_path)
    if not os.path.isfile(full):
        return f"Notebook not found: {file_path}"

    try:
        with open(full, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        return f"Error reading notebook: {e}"

    cells = nb.get("cells", [])
    output_parts = []
    for i, cell in enumerate(cells):
        cell_type = cell.get("cell_type", "unknown")
        source = "".join(cell.get("source", []))
        output_parts.append(f"### Cell {i} ({cell_type})\n```\n{source}\n```")

        for out in cell.get("outputs", []):
            if out.get("text"):
                text = "".join(out["text"])
                output_parts.append(f"Output:\n```\n{text[:2000]}\n```")
            elif out.get("data", {}).get("text/plain"):
                text = "".join(out["data"]["text/plain"])
                output_parts.append(f"Output:\n```\n{text[:2000]}\n```")

    result = "\n\n".join(output_parts)
    if len(result) > 50000:
        result = result[:50000] + "\n... (truncated)"
    return result


async def _handle_notebook_edit(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    file_path = params.get("file", "")
    cell_index = params.get("cell_index")
    new_source = params.get("source", "")

    if not file_path or cell_index is None:
        return "file and cell_index required"

    full = os.path.join(cwd, file_path)
    try:
        with open(full, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        return f"Error reading notebook: {e}"

    cells = nb.get("cells", [])
    idx = int(cell_index)
    if idx < 0 or idx >= len(cells):
        return f"Cell index {idx} out of range (0-{len(cells)-1})"

    cells[idx]["source"] = new_source.split("\n") if isinstance(new_source, str) else new_source
    cells[idx]["outputs"] = []

    with open(full, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1, ensure_ascii=False)

    return f"Updated cell {idx} in {file_path}"


async def _handle_notebook_run(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    file_path = params.get("file", "")
    if not file_path:
        return "file path required"

    full = os.path.join(cwd, file_path)
    if not os.path.isfile(full):
        return f"Notebook not found: {file_path}"

    r = _run_cmd(
        ["jupyter", "nbconvert", "--to", "notebook", "--execute",
         "--ExecutePreprocessor.timeout=120", full, "--output", full],
        cwd, timeout=150,
    )
    if r["ok"]:
        return f"Executed {file_path} successfully. Use notebook_read to see outputs."

    return f"Execution failed: {r['stderr']}\n\nTip: Ensure jupyter is installed (pip install jupyter)."


async def _handle_notebook_add(params: dict, entity_id: str) -> str:
    cwd = _get_cwd(params)
    file_path = params.get("file", "")
    cell_type = params.get("cell_type", "code")
    source = params.get("source", "")
    position = params.get("position", -1)

    if not file_path:
        return "file path required"

    full = os.path.join(cwd, file_path)
    try:
        with open(full, "r", encoding="utf-8") as f:
            nb = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        return f"Error reading notebook: {e}"

    new_cell: dict[str, Any] = {
        "cell_type": cell_type,
        "source": source.split("\n") if isinstance(source, str) else source,
        "metadata": {},
    }
    if cell_type == "code":
        new_cell["outputs"] = []
        new_cell["execution_count"] = None

    cells = nb.get("cells", [])
    pos = int(position)
    if pos < 0 or pos >= len(cells):
        cells.append(new_cell)
        pos = len(cells) - 1
    else:
        cells.insert(pos, new_cell)

    with open(full, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1, ensure_ascii=False)

    return f"Added {cell_type} cell at position {pos} in {file_path}"


# ══════════════════════════════════════════════════════════════════════════════
# Action dispatch table
# ══════════════════════════════════════════════════════════════════════════════

_DISPATCH: dict[str, Any] = {
    "dashboard_module_validate": _handle_dashboard_module_validate,
    # Planning
    "plan_create": _handle_plan_create,
    "plan_show": _handle_plan_show,
    "plan_update": _handle_plan_update,
    "plan_clear": _handle_plan_clear,
    # Git
    "git_status": _handle_git_status,
    "git_diff": _handle_git_diff,
    "git_log": _handle_git_log,
    "git_branch": _handle_git_branch,
    "git_commit": _handle_git_commit,
    "git_stash": _handle_git_stash,
    "git_worktree": _handle_git_worktree,
    "git_merge": _handle_git_merge,
    "git_pr_create": _handle_git_pr_create,
    "git_pr_review": _handle_git_pr_review,
    # LSP
    "lsp_definitions": _handle_lsp_definitions,
    "lsp_references": _handle_lsp_references,
    "lsp_symbols": _handle_lsp_symbols,
    "lsp_diagnostics": _handle_lsp_diagnostics,
    "lsp_hover": _handle_lsp_hover,
    "lsp_completions": _handle_lsp_completions,
    # Review
    "review_diff": _handle_review_diff,
    "review_security": _handle_review_security,
    "review_quality": _handle_review_quality,
    "review_pr": _handle_review_pr,
    # Test
    "test_discover": _handle_test_discover,
    "test_run": _handle_test_run,
    "test_single": _handle_test_single,
    "test_coverage": _handle_test_coverage,
    # Refactor
    "refactor_rename": _handle_refactor_rename,
    "refactor_extract": _handle_refactor_extract,
    "refactor_inline": _handle_refactor_inline,
    "refactor_move": _handle_refactor_move,
    # Monitor
    "monitor_start": _handle_monitor_start,
    "monitor_output": _handle_monitor_output,
    "monitor_stop": _handle_monitor_stop,
    "monitor_list": _handle_monitor_list,
    # Agent
    "agent_spawn": _handle_agent_spawn,
    "agent_status": _handle_agent_status,
    "agent_collect": _handle_agent_collect,
    # Notebook
    "notebook_read": _handle_notebook_read,
    "notebook_edit": _handle_notebook_edit,
    "notebook_run": _handle_notebook_run,
    "notebook_add": _handle_notebook_add,
}

# ── Tool schema ──────────────────────────────────────────────────────────────

_RESERVED_HANDLER_KEYS = {
    "action",
    "query",
    "params",
    "entity_id",
    "user_id",
} | RUNTIME_TOOL_CONTEXT_KEYS


def _extract_action_params(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Accept both params={...} and direct action args like cwd=... ."""
    params = kwargs.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    direct_params = {
        key: value
        for key, value in kwargs.items()
        if key not in _RESERVED_HANDLER_KEYS
    }
    return {**direct_params, **params}

CODE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "code",
        "description": (
            "Execute coding actions for git, tests, review, refactors, LSP, plans, "
            "monitors, agents, and notebooks. Use action='search' with query to "
            "discover actions, or action='<name>' with params to run one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action name, or 'search'.",
                },
                "query": {
                    "type": "string",
                    "description": "Search query.",
                },
                "params": {
                    "type": "object",
                    "description": "Action params.",
                    "additionalProperties": True,
                },
            },
            "required": ["action"],
        },
    },
}


async def _code_handler(entity_id: str = "", **kwargs: Any) -> str:
    """Code composite tool handler."""
    action = (kwargs.get("action") or "").strip()

    if not action:
        return json.dumps({"error": "action is required"})

    manual_skill_slugs = runtime_manual_skill_slugs_from_context(kwargs)
    if (
        "dashboard-module-builder" in manual_skill_slugs
        and action != "dashboard_module_validate"
    ):
        return json.dumps(
            {
                "error": (
                    "Dashboard module conversations may only use the code tool's "
                    "dashboard_module_validate action."
                )
            }
        )

    # Search mode
    if action == "search":
        query = (kwargs.get("query") or "").strip()
        if not query:
            summary = {dept: [a[0] for a in actions] for dept, actions in _ACTIONS.items()}
            return json.dumps({"categories": summary, "total_actions": len(_ALL_ACTIONS)})
        results = _search_actions(query)
        if not results:
            return json.dumps({"matches": [], "query": query, "hint": "Try broader keywords."})
        return json.dumps({"matches": results, "query": query}, ensure_ascii=False)

    # Validate action exists
    if action not in _ALL_ACTIONS:
        suggestions = _search_actions(action, max_results=5)
        return json.dumps({
            "error": f"Unknown action: '{action}'",
            "suggestions": [s["action"] for s in suggestions],
        })

    params = _extract_action_params(kwargs)
    handler = _DISPATCH.get(action)
    if not handler:
        return json.dumps({"error": f"Action '{action}' handler not yet implemented."})

    try:
        return await handler(params, entity_id)
    except Exception as e:
        logger.exception("code action=%s failed: %s", action, e)
        return json.dumps({"error": f"Action '{action}' failed: {e}"})


def get_tools():
    return [(CODE_SCHEMA, _code_handler)]
