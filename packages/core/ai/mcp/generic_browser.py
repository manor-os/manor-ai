"""Generic browser — L2 LLM-driven web agent (any site, any task).

Wraps `browser-use` <https://github.com/browser-use/browser-use> via the
browser-runner sidecar. Use this when the user wants to run a task on a
site that does NOT have a dedicated MCP wrapper (linkedin_browser,
gmail_browser, etc) — the agent will navigate, click, type, extract on
its own based on a natural-language `task`.

Layering
────────
  L1  Playwright primitives           (page.click, page.fill)
  L2  browser-use generic agent       ← this module
  L4  site-specific business actions  (linkedin_browser.easy_apply,
                                       send_invitation, search_jobs, …)

Pick L4 when one exists: it's an order of magnitude cheaper (1 LLM
call vs 10–30) and has hand-tuned selectors with fallbacks. Pick L2
for the long tail.

Cost / safety
─────────────
* `max_steps` caps LLM iterations (default 20, hard ceiling 50)
* `allowed_domains` whitelists hosts the agent may navigate to
* `confirm_destructive=False` (default) blocks Submit/Send/Buy/Pay
  clicks — the provider intercepts and returns ``status='needs_input'``
  so the wrapper can ask the user.
* Each call spins up a fresh browser-use Agent. Stateless — no session
  resumption between calls.

Auth
────
`bearer_token` = optional Playwright storage_state JSON OR Cookie-Editor
export, same shape as every other browser-runner-backed wrapper. Empty
is fine — the agent just runs anonymously.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from . import _browser_runner
from ._http import mcp_err
from ..pending_action import PendingAction

logger = logging.getLogger(__name__)


_MAX_STEPS_HARD_CAP = 50


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "run_task",
            "description": (
                "Run a natural-language browser task via an LLM-driven "
                "agent (browser-use). USE THIS ONLY when no dedicated "
                "MCP wrapper exists for the site (linkedin_browser, "
                "gmail_browser, etc). Prefer the "
                "site-specific tool — this one is 10-30x more "
                "expensive (one LLM call per agent step) and slower "
                "(~30-60s per task). Returns the agent's history, "
                "extracted data, and final URL/state."
            ),
            "parameters": {
                "type": "object",
                "required": ["task"],
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "Natural-language task description. Be "
                            "specific about what to extract or what "
                            "constitutes 'done'. Example: 'go to "
                            "stripe.com/jobs, find all open Software "
                            "Engineer roles in San Francisco, return "
                            "title + url for each'."
                        ),
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "Optional starting URL. If omitted, the "
                            "agent picks one from the task."
                        ),
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": (
                            "Maximum LLM iterations (default 20, hard "
                            "ceiling 50). Each step is roughly one "
                            "click/type/scroll plus the LLM call to "
                            "decide it."
                        ),
                    },
                    "allowed_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Hostnames the agent may navigate to "
                            "(e.g. ['stripe.com', 'jobs.lever.co']). "
                            "Default: only the starting URL's domain. "
                            "Anything outside this list is blocked."
                        ),
                    },
                    "confirm_destructive": {
                        "type": "boolean",
                        "description": (
                            "Allow the agent to click destructive "
                            "buttons (Submit, Send, Buy, Pay, "
                            "Delete). Default false: those clicks "
                            "are intercepted and returned as "
                            "status='needs_input' so the caller can "
                            "confirm. Set true ONLY when the user "
                            "has explicitly authorized this run."
                        ),
                    },
                    "allowed_sso_hosts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "SSO providers (accounts.google.com, "
                            "login.microsoftonline.com, etc.) the "
                            "agent may pass through silently when "
                            "the user is already signed in. Default "
                            "empty: ANY SSO redirect returns "
                            "status='login_required' so the caller "
                            "can spawn a headed-login session. "
                            "Whitelist a host ONLY when the user has "
                            "explicitly OK'd that SSO path."
                        ),
                    },
                },
            },
        }
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    if name != "run_task":
        return mcp_err(f"Unknown tool: {name}")

    task = (arguments.get("task") or "").strip()
    if not task:
        return mcp_err("'task' is required")

    raw_steps = arguments.get("max_steps")
    if raw_steps is None:
        max_steps = 20
    else:
        try:
            max_steps = int(raw_steps)
        except (TypeError, ValueError):
            return mcp_err("max_steps must be an integer")
    # Explicit check (not `or 20` shortcut) so that 0 isn't silently
    # promoted to the default — caller meant "no steps", which we
    # reject as out of range.
    if max_steps < 1 or max_steps > _MAX_STEPS_HARD_CAP:
        return mcp_err(
            f"max_steps must be between 1 and {_MAX_STEPS_HARD_CAP} "
            f"(got {max_steps})"
        )

    # Optional storage_state — empty is fine for anonymous browsing.
    storage_state: Optional[Dict[str, Any]] = None
    if bearer_token:
        storage_state = _browser_runner.parse_storage_state(bearer_token)
        if storage_state is None:
            return mcp_err(
                "Could not parse the cookies. Expected either "
                "Playwright storage_state JSON or a Cookie-Editor "
                "export. Re-export and paste again, or leave the "
                "cookies field empty for anonymous browsing."
            )

    # Provider opens its own Chromium (MANAGES_OWN_BROWSER=True), so we
    # pass storage_state via params and tell the runner not to launch.
    params = {
        "task": task,
        "max_steps": max_steps,
    }
    if arguments.get("url"):
        params["url"] = arguments["url"]
    if arguments.get("allowed_domains") is not None:
        params["allowed_domains"] = arguments["allowed_domains"]
    if arguments.get("confirm_destructive"):
        params["confirm_destructive"] = True
    if arguments.get("allowed_sso_hosts") is not None:
        # Pass through verbatim; provider lower-cases + dedupes.
        params["allowed_sso_hosts"] = arguments["allowed_sso_hosts"]

    # browser-use Agent is multi-step; budget liberally. Hard cap is
    # roughly max_steps * 30s per step.
    timeout_ms = min(900_000, max(60_000, max_steps * 45_000))

    try:
        resp = await _browser_runner.perform(
            provider="generic_browser",
            action="run_task",
            params=params,
            storage_state=storage_state,
            timeout_ms=timeout_ms,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("generic_browser call failed")
        return mcp_err(f"generic_browser call failed: {exc}")

    if not resp.get("ok"):
        return mcp_err(resp.get("error") or "browser-runner returned non-ok")

    result = resp.get("result") or {}

    envelope: Dict[str, Any] = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, indent=2),
            }
        ],
        "isError": False,
    }

    # Translate provider statuses into a generic PendingAction the
    # chat / Plan executor can route on without knowing about
    # generic_browser specifics. See packages/core/ai/pending_action.py
    # for the contract.
    pending = _to_pending_action(result)
    if pending is not None:
        pending.attach_to_envelope(envelope)

    return envelope


def _to_pending_action(result: Dict[str, Any]) -> Optional[PendingAction]:
    """Map generic_browser provider statuses to PendingAction kinds.

    Returns None for terminal statuses (completed / max_steps_reached /
    error) — those don't need user action, just summary."""
    status = result.get("status")

    if status == "login_required":
        login_url = result.get("login_url")
        if not login_url:
            return None
        return PendingAction.needs_login(
            login_url=login_url,
            partial_data=result.get("extracted_data"),
        )

    if status == "needs_input":
        # Destructive guard hit — agent tried to click Submit/Send/etc.
        # Re-running with confirm_destructive=True is the right
        # resolution, so this becomes a needs_confirmation action
        # (NOT a needs_input form).
        intercepted = result.get("intercepted_destructive_actions") or []
        labels = [
            (item.get("label") or "").strip()
            for item in intercepted
            if isinstance(item, dict) and (item.get("label") or "").strip()
        ]
        summary = (
            f"Allow the agent to click: {', '.join(labels)}"
            if labels
            else "Allow the agent to perform a destructive action"
        )
        return PendingAction.needs_confirmation(
            action_summary=summary,
            impact=(
                "The agent will be re-run with confirm_destructive=True. "
                "Only approve if the listed click(s) are what you want."
            ),
        )

    return None
