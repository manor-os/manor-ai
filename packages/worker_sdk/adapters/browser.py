"""Browser adapter — drives Patchright with a pre-captured ``storage_state``.

Use as:

    from packages.worker_sdk import ManorWorker
    from packages.worker_sdk.adapters.browser import register_browser_adapter

    worker = ManorWorker(...)
    register_browser_adapter(
        worker,
        provider="x",
        # Resolve a captured session for this lease. Most workers route
        # by lease.workspace_id → entity_id; the SDK stays agnostic and
        # leaves resolution to the caller.
        resolve_storage_state=my_resolver,
        actions={
            "x.post_tweet": post_tweet,
            "x.like":       like_tweet,
        },
    )
    await worker.run_forever()

The actions are async functions ``async fn(page, lease) -> dict`` —
``page`` is a Patchright Page bound to a fresh BrowserContext seeded
with the captured storage_state. The adapter wraps each call in:

  * lease.execution_mode handling (live | dry_run | sandbox)
      - live    → run for real
      - dry_run → log the call but skip the navigation; return
                  ``{"dry_run": true, "action": ...}``
      - sandbox → run, but inside a synthetic data-only page (file://
                  about:blank with mocked DOM) — useful for plan
                  rehearsal without touching the real site
  * NeedHumanInput on detected sign-out / CAPTCHA / 2FA prompts
  * automatic ``progress(0.5)`` mid-action so the dispatcher knows the
    handler isn't stalled

Patchright is a soft dependency: importing this module without it
installed raises a clear error only when the handler actually runs, so
Manor can boot on environments that don't need the browser.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Optional

from packages.worker_sdk.types import Lease, LeaseResult, NeedHumanInput
from packages.worker_sdk.worker import LeaseContext, ManorWorker

logger = logging.getLogger(__name__)


# ── Public types ─────────────────────────────────────────────────────

# Resolver: given a lease, return the decrypted Playwright storage_state
# (cookies + origins) for this lease's session — or None if no session
# is bound (handler will then fail with NeedHumanInput).
StorageStateResolver = Callable[[Lease], Awaitable[Optional[dict]]]

# Action: handler-style function bound to a Patchright Page.
PageAction = Callable[[Any, Lease], Awaitable[dict]]

# Hooks fired around each action — wire to mark_validated / expire_session.
OnSuccess = Callable[[Lease, dict], Awaitable[None]]
OnFailure = Callable[[Lease, BaseException], Awaitable[None]]


# Phrases we treat as a "session lost" signal. Conservative — we'd
# rather false-negative (let the handler error normally) than
# false-positive (mark a session expired when it's actually fine).
_SIGNED_OUT_TEXTS = (
    "sign in",
    "log in",
    "please log in",
    "your session has expired",
    "for your security, please sign in again",
)
_CAPTCHA_TEXTS = ("verify you are human", "i'm not a robot", "captcha")


# ── Registration ─────────────────────────────────────────────────────

def register_browser_adapter(
    worker: ManorWorker,
    *,
    provider: str,
    resolve_storage_state: StorageStateResolver,
    actions: dict[str, PageAction],
    headless: bool = True,
    user_agent: Optional[str] = None,
    on_success: Optional[OnSuccess] = None,
    on_failure: Optional[OnFailure] = None,
) -> None:
    """Wire up a `kind=action, provider=<provider>` handler that routes
    by ``lease.action_key`` into one of ``actions``."""

    async def handle(lease: Lease, ctx: LeaseContext) -> dict:
        action_key = lease.action_key
        if not action_key or action_key not in actions:
            return {
                "result": {
                    "ok": False,
                    "error": f"unknown action_key: {action_key!r}",
                    "available": sorted(actions.keys()),
                },
            }
        fn = actions[action_key]

        if lease.execution_mode == "dry_run":
            logger.info("browser adapter: dry_run %s — skipping navigation", action_key)
            return {
                "result": {"dry_run": True, "action": action_key, "params": lease.params},
                "cost": {"api_calls": 0, "usd": 0},
            }

        storage_state = await resolve_storage_state(lease)
        if storage_state is None:
            raise NeedHumanInput(
                f"No active browser session for {provider}"
                f" — pair one in Settings → Integrations and retry.",
                kind="missing_session",
            )

        result, exc = await _run_in_browser(
            ctx=ctx, lease=lease, fn=fn,
            storage_state=storage_state,
            headless=headless,
            user_agent=user_agent,
            sandbox=lease.execution_mode == "sandbox",
        )
        if exc is not None:
            if on_failure is not None:
                try:
                    await on_failure(lease, exc)
                except Exception:
                    logger.warning("browser adapter on_failure hook raised", exc_info=True)
            raise exc
        if on_success is not None:
            try:
                await on_success(lease, result)
            except Exception:
                logger.warning("browser adapter on_success hook raised", exc_info=True)
        return {"result": result, "cost": {"api_calls": 1, "usd": 0}}

    worker.handle(kind="action", provider=provider)(handle)


# ── Browser lifecycle ─────────────────────────────────────────────────

async def _run_in_browser(
    *,
    ctx: LeaseContext,
    lease: Lease,
    fn: PageAction,
    storage_state: dict,
    headless: bool,
    user_agent: Optional[str],
    sandbox: bool,
) -> tuple[dict, Optional[BaseException]]:
    """Spin up a Patchright browser, run ``fn``, return its result.

    Returns (result, None) on success, ({}, exc) on failure — the caller
    decides whether to swallow or re-raise. Always closes the browser.
    """
    try:
        from patchright.async_api import async_playwright
    except ImportError as exc:
        err = RuntimeError(
            "browser adapter requires patchright "
            "(pip install patchright && patchright install chromium)"
        )
        err.__cause__ = exc
        return {}, err

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context_kwargs: dict[str, Any] = {"storage_state": storage_state}
            if user_agent:
                context_kwargs["user_agent"] = user_agent
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            try:
                # Mid-flight progress so the dispatcher's lease watchdog
                # doesn't think we're stalled.
                progress_task = asyncio.create_task(_periodic_progress(ctx))
                try:
                    if sandbox:
                        await page.goto("about:blank")
                    result = await fn(page, lease)
                    await _check_for_signout_or_captcha(page, lease)
                    return result, None
                finally:
                    progress_task.cancel()
                    with _suppress(asyncio.CancelledError):
                        await progress_task
            finally:
                await context.close()
        except NeedHumanInput as exc:
            return {}, exc
        except Exception as exc:  # noqa: BLE001 — surface anything to caller
            return {}, exc
        finally:
            await browser.close()


async def _periodic_progress(ctx: LeaseContext, interval_s: float = 30.0) -> None:
    """Heartbeat while a browser action runs (some flows take 60–90s)."""
    fraction = 0.1
    while True:
        await asyncio.sleep(interval_s)
        try:
            await ctx.progress(min(fraction, 0.9))
            fraction += 0.1
        except Exception:
            return


async def _check_for_signout_or_captcha(page: Any, lease: Lease) -> None:
    """If the post-action page looks like a sign-in or CAPTCHA, raise
    NeedHumanInput so the lease pauses for operator attention rather
    than reporting a misleading 'success'."""
    try:
        body_text = (await page.inner_text("body", timeout=2_000)).lower()
    except Exception:
        return
    if any(s in body_text for s in _SIGNED_OUT_TEXTS):
        raise NeedHumanInput(
            f"Looks like the {lease.provider} session is signed out — "
            f"please re-pair in Settings → Integrations.",
            kind="missing_session",
        )
    if any(s in body_text for s in _CAPTCHA_TEXTS):
        raise NeedHumanInput(
            f"{lease.provider} returned a CAPTCHA / human-verification page. "
            f"Solve it in your browser, refresh the session, then retry.",
            kind="captcha_blocked",
        )


# ── tiny stdlib-style helpers (avoid importing contextlib only for this) ──

class _suppress:
    def __init__(self, *exc_types: type[BaseException]):
        self._exc_types = exc_types

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc is not None and issubclass(exc_type, self._exc_types)
