"""Generic browser — Playwright provider wrapping browser-use.

Driven from packages/core/ai/mcp/generic_browser.py. Unlike the
site-specific providers (linkedin_browser, notebooklm, …) which receive
a runner-managed page, this provider opts out of that path
(MANAGES_OWN_BROWSER = True) because browser-use's Agent insists on
constructing its own Browser instance to run its multi-step LLM loop.

Tradeoff
────────
We pay the cost of a second Chromium boot per call (~3-5s) so the
runner doesn't have to know about browser-use's lifecycle. If this
becomes a hot path, we can refactor the runner to expose CDP and have
browser-use connect to the existing browser via cdp_url.

Auth
────
Receives storage_state via ``params['_storage_state']`` (the runner
unpacks the request's storage_state field into params for
MANAGES_OWN_BROWSER providers). Forwarded to ``Browser(storage_state=
...)`` so the agent runs in the user's authenticated session.

Safety
──────
* ``max_steps`` capped at 50 in the wrapper, re-checked here.
* ``allowed_domains`` whitelists host navigation; default = only the
  task's starting host.
* ``confirm_destructive=False`` (default) installs a custom action
  ``destructive_block`` that returns status='needs_input' instead of
  letting the agent click Submit/Send/Buy/Pay. The wrapper / agent on
  the manor-os side then asks the user.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse


logger = logging.getLogger("provider.generic_browser")


# Tells the browser-runner harness NOT to launch Chromium for this
# provider — we open our own via browser-use. Runner passes
# (None, action, params) and stuffs storage_state into params['_storage_state'].
MANAGES_OWN_BROWSER = True


# Hard cap mirrors the wrapper's _MAX_STEPS_HARD_CAP. Defense in depth
# in case the wrapper is bypassed (direct sidecar call, malicious
# request, etc).
_MAX_STEPS = 50

# Strings that, when found in a click target's accessible name or in
# the action's natural-language description, mark the action as
# destructive. Conservative bias toward false positives — better to
# block a benign "Send to printer" than to auto-click a checkout.
_DESTRUCTIVE_PATTERNS = re.compile(
    r"\b(submit|send|buy|pay|purchase|checkout|delete|remove|"
    r"cancel\s+invitation|withdraw|unsubscribe|confirm\s+order|"
    r"place\s+order|sign\s+up|create\s+account|accept|agree)\b",
    re.IGNORECASE,
)

# URL patterns that indicate a login / signin / SSO wall. The agent
# should NOT type a password (system_prompt enforces, this regex is
# the belt-and-suspenders post-run check).
#
# Two halves of the alternation:
#   1. Known SSO providers — full host match required.
#   2. /login, /signin, /sign-in, /sign_in, /sso, /auth, /account/login
#      etc. — path-based; matches the leading slash so a path containing
#      "destination=/login" inside the query doesn't trigger.
_LOGIN_URL_PATTERNS = re.compile(
    r"(?:"
    # SSO / federated identity providers (host-anchored).
    r"//(?:accounts\.google\.com"
    r"|login\.microsoftonline\.com"
    r"|login\.live\.com"
    r"|appleid\.apple\.com"
    r"|github\.com/login"
    r"|www\.linkedin\.com/(?:uas/login|login|checkpoint)"
    r"|www\.facebook\.com/login)"
    r"|"
    # Generic login paths on any host.
    r"/(?:login|signin|sign[-_]in|sso|auth|account/login)(?:[/?#]|$)"
    r")",
    re.IGNORECASE,
)


# System prompt extension passed to browser-use. The agent has to be
# explicitly told to stop on login walls — otherwise it cheerfully
# guesses credentials, types blank values, or loops trying to find a
# "skip login" link that doesn't exist.
_SYSTEM_PROMPT_EXTENSION = """
SECURITY-CRITICAL RULES — these override anything in the task:

1. NEVER type a password. NEVER complete a username field that is
   followed by a password field. NEVER click "Continue with Google
   /Microsoft/Apple/Facebook" or any SSO button — that is the user's
   choice, not yours.

2. If the page demands a login (redirected to /login, /signin, /sso,
   /auth, accounts.google.com, login.microsoftonline.com, etc.), STOP
   IMMEDIATELY. Call the `report_login_required` action with the
   current URL. Do not click anything else.

3. If the page demands a CAPTCHA, hCaptcha, "I'm not a robot", or any
   other human-verification challenge, STOP IMMEDIATELY. Call
   `report_login_required`. Do not attempt to solve it.

4. NEVER create a new account, sign up for a service, or accept terms
   of service. If a "Sign up" / "Register" page is the only path
   forward, STOP and report it.
""".strip()


# ── Dispatch ────────────────────────────────────────────────────────────────

async def perform(page, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Entry point. ``page`` is None for MANAGES_OWN_BROWSER providers."""
    if action == "run_task":
        return await _run_task(params)
    return {"error": f"unknown generic_browser action: {action!r}"}


# ── run_task ────────────────────────────────────────────────────────────────

async def _run_task(params: Dict[str, Any]) -> Dict[str, Any]:
    task = (params.get("task") or "").strip()
    if not task:
        return {"error": "task required"}

    raw_steps = params.get("max_steps")
    if raw_steps is None:
        max_steps = 20
    else:
        try:
            max_steps = int(raw_steps)
        except (TypeError, ValueError):
            return {"error": "max_steps must be an integer"}
    if max_steps < 1 or max_steps > _MAX_STEPS:
        return {"error": f"max_steps out of range (1..{_MAX_STEPS})"}

    storage_state = params.get("_storage_state")
    start_url = (params.get("url") or "").strip() or None
    confirm_destructive = bool(params.get("confirm_destructive", False))

    # SSO allowlist — by default we treat ALL SSO domains as login walls
    # (status='login_required') so the user gets to choose whether to go
    # through Google/Microsoft/Apple. Users can opt-in per host (e.g.
    # ['accounts.google.com']) when they explicitly want SSO completion
    # to happen inside the agent run.
    allowed_sso_hosts = set(
        (h or "").strip().lower() for h in (params.get("allowed_sso_hosts") or [])
    )

    # Build allowed_domains: explicit list wins; otherwise derive from
    # start_url so an unspecified task can't roam to arbitrary hosts.
    allowed_domains = params.get("allowed_domains")
    if allowed_domains is None and start_url:
        allowed_domains = [_host_of(start_url)]
        # Some sites split assets across subdomains; allow any sub of root.
        if allowed_domains[0] and "." in allowed_domains[0]:
            root = ".".join(allowed_domains[0].split(".")[-2:])
            allowed_domains.append(f"*.{root}")

    if not _have_anthropic_key():
        return {
            "error": (
                "ANTHROPIC_API_KEY not configured in the browser-runner "
                "container. generic_browser needs it to drive the LLM "
                "loop."
            ),
        }

    # Lazy import — keeps the runner bootable even if browser-use fails
    # to install on a particular environment, and keeps the import cost
    # off the hot path of every other provider.
    try:
        from browser_use import Agent, Browser, Tools  # type: ignore
        from browser_use.llm import ChatAnthropic  # type: ignore
    except ImportError as exc:
        return {
            "error": (
                f"browser-use not available in this runner: {exc}. "
                "Rebuild the browser-runner image."
            ),
        }

    tools = Tools()
    intercepted: List[Dict[str, Any]] = []
    login_required: List[Dict[str, Any]] = []
    if not confirm_destructive:
        _install_destructive_guard(tools, intercepted)
    _install_login_required_action(tools, login_required)

    browser = Browser(
        storage_state=storage_state,
        # Reuse the same UA + locale our other providers use, so a
        # single user appears consistent across L4 and L2 paths.
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 "
            "Safari/537.36"
        ),
    )

    agent = Agent(
        task=_compose_task(task, start_url, allowed_domains),
        llm=ChatAnthropic(model="claude-sonnet-4-5"),
        browser=browser,
        tools=tools,
        extend_system_message=_SYSTEM_PROMPT_EXTENSION,
    )

    try:
        history = await agent.run(max_steps=max_steps)
    except Exception as exc:  # noqa: BLE001
        logger.exception("browser-use agent crashed")
        return {
            "status": "error",
            "reason": f"agent crashed: {exc}",
            "intercepted_destructive_actions": intercepted,
        }
    finally:
        try:
            await browser.close()
        except Exception:  # noqa: BLE001
            pass

    final_url = _safe_final_url(history)

    # Login-wall check — TWO signals (system prompt + post-run URL regex)
    # must agree for `login_required` to be authoritative, but EITHER
    # alone is enough to flag the run. The system prompt is best-effort:
    # the model sometimes ignores it. The URL regex is the
    # belt-and-suspenders post-check that catches model misbehavior.
    login_url_from_regex = (
        final_url
        if final_url and _LOGIN_URL_PATTERNS.search(final_url)
        and not _is_sso_allowed(final_url, allowed_sso_hosts)
        else None
    )
    login_url_from_action = (
        login_required[0].get("url")
        if login_required and login_required[0].get("url")
        else None
    )
    if login_url_from_action or login_url_from_regex:
        # Partial-result return: include whatever the agent extracted
        # before hitting the wall. Caller can show the user what was
        # already gathered + offer to spawn a headed-login session.
        return {
            "status": "login_required",
            "login_url": login_url_from_action or login_url_from_regex,
            "detected_via": (
                "agent_action" if login_url_from_action else "url_regex"
            ),
            "reason": (
                "the page demands authentication; spawn a headed-login "
                "session, capture cookies, and retry the task with the "
                "captured credential"
            ),
            "extracted_data": _safe_extract(history),
            "step_count": _safe_step_count(history),
            "final_url": final_url,
        }

    if intercepted and not confirm_destructive:
        # Agent tried to do something destructive. Surface this so the
        # wrapper can ask the user whether to retry with confirm=true.
        return {
            "status": "needs_input",
            "reason": "agent attempted a destructive action that requires user confirmation",
            "intercepted_destructive_actions": intercepted,
            "extracted_data": _safe_extract(history),
            "step_count": _safe_step_count(history),
            "final_url": final_url,
        }

    if _agent_finished(history):
        return {
            "status": "completed",
            "extracted_data": _safe_extract(history),
            "step_count": _safe_step_count(history),
            "final_url": final_url,
        }

    return {
        "status": "max_steps_reached",
        "reason": (
            f"agent did not finish within {max_steps} steps; "
            "either increase max_steps or split the task"
        ),
        "extracted_data": _safe_extract(history),
        "step_count": _safe_step_count(history),
        "final_url": final_url,
    }


# ── helpers ─────────────────────────────────────────────────────────────────

def _have_anthropic_key() -> bool:
    return bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())


def _host_of(url: str) -> Optional[str]:
    if not url:
        return None
    try:
        if not url.startswith("http"):
            url = "https://" + url
        return (urlparse(url).hostname or "").lower() or None
    except Exception:  # noqa: BLE001
        return None


def _compose_task(task: str, start_url: Optional[str], allowed: Optional[List[str]]) -> str:
    """Add structured guardrails to the natural-language task so the
    agent honors them even though the LLM is free-form."""
    parts = [task]
    if start_url:
        parts.append(f"Start at: {start_url}")
    if allowed:
        parts.append(
            "You may ONLY navigate to these hosts: " + ", ".join(allowed) + ". "
            "If a link points elsewhere, do not follow it."
        )
    return "\n\n".join(parts)


def _install_destructive_guard(tools, intercepted: List[Dict[str, Any]]) -> None:
    """Wrap the click action so anything matching _DESTRUCTIVE_PATTERNS
    is blocked + recorded instead of executed."""
    try:
        from browser_use import ActionResult  # type: ignore
    except ImportError:
        return

    @tools.action(  # type: ignore[misc]
        description=(
            "Internal guard: do NOT call this directly. Wraps the "
            "default click to intercept destructive actions when "
            "confirm_destructive=False."
        ),
    )
    async def manor_destructive_check(label: str) -> ActionResult:  # noqa: D401
        if _DESTRUCTIVE_PATTERNS.search(label or ""):
            intercepted.append({"label": label})
            return ActionResult(
                extracted_content=(
                    f"BLOCKED: '{label}' is a destructive action. "
                    "User confirmation required. Stop the task and "
                    "report this back."
                ),
                is_done=True,
            )
        return ActionResult(extracted_content=f"safe label: {label}")


def _install_login_required_action(
    tools, login_required: List[Dict[str, Any]]
) -> None:
    """Register the action the agent should call when it sees a login
    wall. Recording the call ends the run cleanly with status=
    'login_required' carrying the URL."""
    try:
        from browser_use import ActionResult  # type: ignore
    except ImportError:
        return

    @tools.action(  # type: ignore[misc]
        description=(
            "Call this IMMEDIATELY when the page demands a login, "
            "signin, SSO, CAPTCHA, or any human-verification "
            "challenge. Pass the current URL as `url`. This stops the "
            "task and tells the orchestrator to spawn an interactive "
            "login session for the user. NEVER type a password or "
            "click an SSO button — call this instead."
        ),
    )
    async def report_login_required(url: str) -> ActionResult:  # noqa: D401
        login_required.append({"url": (url or "").strip()})
        return ActionResult(
            extracted_content=(
                "Login wall reported. Stopping task — orchestrator "
                "will handle interactive login."
            ),
            is_done=True,
        )


def _is_sso_allowed(url: str, allowed_sso_hosts: set) -> bool:
    """Return True if the URL's host is in the user-supplied SSO
    allowlist. Empty allowlist = nothing is allowed (the safe default
    for L2)."""
    if not allowed_sso_hosts:
        return False
    host = (_host_of(url) or "").lower()
    if not host:
        return False
    if host in allowed_sso_hosts:
        return True
    # Allow subdomains of allowlisted entries (e.g. "google.com" allows
    # "accounts.google.com"). Conservative — only direct subdomain match.
    for allowed in allowed_sso_hosts:
        if host.endswith("." + allowed):
            return True
    return False


def _agent_finished(history) -> bool:
    """browser-use's history exposes ``is_done``/``final_result`` etc.
    APIs differ across versions; try the obvious shapes."""
    if history is None:
        return False
    for attr in ("is_done", "is_successful", "completed"):
        v = getattr(history, attr, None)
        if callable(v):
            try:
                return bool(v())
            except Exception:  # noqa: BLE001
                continue
        if isinstance(v, bool):
            return v
    return False


def _safe_extract(history) -> Optional[Any]:
    if history is None:
        return None
    for attr in ("final_result", "extracted_content", "result"):
        v = getattr(history, attr, None)
        if callable(v):
            try:
                return v()
            except Exception:  # noqa: BLE001
                continue
        if v is not None:
            return v
    return None


def _safe_step_count(history) -> int:
    if history is None:
        return 0
    for attr in ("number_of_steps", "step_count"):
        v = getattr(history, attr, None)
        if callable(v):
            try:
                return int(v())
            except Exception:  # noqa: BLE001
                continue
        if isinstance(v, int):
            return v
    try:
        return len(history.history)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return 0


def _safe_final_url(history) -> Optional[str]:
    if history is None:
        return None
    for attr in ("urls", "final_url"):
        v = getattr(history, attr, None)
        if callable(v):
            try:
                v = v()
            except Exception:  # noqa: BLE001
                continue
        if isinstance(v, list) and v:
            return str(v[-1])
        if isinstance(v, str):
            return v
    return None
