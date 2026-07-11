"""Smoke test for the M7 browser adapter.

Fakes ``patchright`` so the test runs without a real Chromium install.
Covers:
  1. dry_run mode → no browser launch, returns {dry_run: true, ...}
  2. live mode w/ resolver returning storage_state → action sees the page
  3. resolver returning None → NeedHumanInput
  4. action returns sign-out body text → NeedHumanInput (missing_session)
  5. unknown action_key → graceful failure result

Run with: uv run python -m packages.worker_sdk.adapters._smoke_browser
"""
from __future__ import annotations

import asyncio
import sys
import types
from typing import Any, Optional

from packages.worker_sdk.types import Lease, NeedHumanInput
from packages.worker_sdk.worker import LeaseContext, ManorWorker


# ── Fake Patchright (installed into sys.modules before import-under-test) ──

class _FakePage:
    def __init__(self, post_action_body: str = "Welcome back!"):
        self._body = post_action_body
        self.goto_calls: list[str] = []

    async def goto(self, url: str) -> None:
        self.goto_calls.append(url)

    async def inner_text(self, selector: str, timeout: int = 0) -> str:
        return self._body


class _FakeContext:
    def __init__(self, page: _FakePage):
        self._page = page
        self.closed = False

    async def new_page(self) -> _FakePage:
        return self._page

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, page: _FakePage):
        self._page = page
        self.contexts: list[_FakeContext] = []
        self.closed = False

    async def new_context(self, **kw: Any) -> _FakeContext:
        ctx = _FakeContext(self._page)
        self.contexts.append(ctx)
        # Stash kwargs on instance so the test can assert them.
        ctx.kw = kw  # type: ignore[attr-defined]
        return ctx

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser):
        self._browser = browser

    async def launch(self, headless: bool = True) -> _FakeBrowser:
        return self._browser


class _FakePlaywrightCM:
    def __init__(self, browser: _FakeBrowser):
        self._browser = browser

    async def __aenter__(self) -> "_FakePlaywrightCM":
        self.chromium = _FakeChromium(self._browser)
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


def install_fake_patchright(page: _FakePage) -> _FakeBrowser:
    browser = _FakeBrowser(page)
    fake_async_api = types.SimpleNamespace(
        async_playwright=lambda: _FakePlaywrightCM(browser),
    )
    fake_pkg = types.ModuleType("patchright")
    fake_pkg.async_api = fake_async_api  # type: ignore[attr-defined]
    sys.modules["patchright"] = fake_pkg
    sys.modules["patchright.async_api"] = fake_async_api  # type: ignore[assignment]
    return browser


# ── Test harness ──────────────────────────────────────────────────────

class _FakeCtx:
    """Minimal LeaseContext substitute — enough for the adapter."""

    def __init__(self, lease: Lease):
        self.lease = lease

    async def progress(self, fraction: float) -> None:
        return None


def make_lease(action_key: str, *, mode: str = "live") -> Lease:
    return Lease(
        lease_id="lease_t",
        step_id="step_t",
        plan_id="plan_t",
        workspace_id="ws_t",
        kind="action",
        provider="x",
        action_key=action_key,
        integration_id=None,
        params={"text": "hello"},
        risk_level="low",
        lease_until="2099-01-01T00:00:00Z",
        execution_mode=mode,  # type: ignore[arg-type]
        credentials=[],
    )


def _check(cond: bool, msg: str) -> None:
    print(f"  {'✓' if cond else '✗'} {msg}")
    if not cond:
        sys.exit(1)


# ── Cases ─────────────────────────────────────────────────────────────

async def case_dry_run() -> None:
    print("[case] dry_run → no browser launch, dry_run flag")
    install_fake_patchright(_FakePage())
    from packages.worker_sdk.adapters.browser import register_browser_adapter

    worker = ManorWorker(endpoint="http://t", workerId="w", secret="s")  # type: ignore[call-arg]
    raise NotImplementedError  # placeholder to be replaced by real Python ctor


async def case_dry_run_python() -> None:
    print("[case] dry_run → no browser launch, dry_run flag")
    install_fake_patchright(_FakePage())
    from packages.worker_sdk.adapters.browser import register_browser_adapter

    handlers: dict = {}
    class _W:
        def handle(self, *, kind, provider):
            def deco(fn):
                handlers[(kind, provider)] = fn
                return fn
            return deco
    w = _W()

    async def post_tweet(page, lease):
        return {"posted": True, "params": dict(lease.params)}

    register_browser_adapter(
        w, provider="x",
        resolve_storage_state=lambda lease: _none(),
        actions={"x.post_tweet": post_tweet},
    )
    handler = handlers[("action", "x")]
    lease = make_lease("x.post_tweet", mode="dry_run")
    out = await handler(lease, _FakeCtx(lease))
    _check(out["result"]["dry_run"] is True, "dry_run flag set")
    _check(out["result"]["action"] == "x.post_tweet", "action surfaced")


async def case_live_action_runs() -> None:
    print("\n[case] live + resolver returns storage_state → action runs on page")
    page = _FakePage(post_action_body="Welcome back, alice!")
    browser = install_fake_patchright(page)
    from packages.worker_sdk.adapters.browser import register_browser_adapter

    handlers: dict = {}
    class _W:
        def handle(self, *, kind, provider):
            def deco(fn):
                handlers[(kind, provider)] = fn
                return fn
            return deco
    w = _W()

    storage = {"cookies": [{"name": "auth", "value": "ok"}], "origins": []}

    async def resolver(lease):
        return storage

    seen: dict = {}

    async def post_tweet(page_arg, lease):
        seen["page"] = page_arg
        seen["text"] = lease.params.get("text")
        return {"posted": True}

    register_browser_adapter(
        w, provider="x",
        resolve_storage_state=resolver,
        actions={"x.post_tweet": post_tweet},
        user_agent="manor-test/1.0",
    )
    handler = handlers[("action", "x")]
    lease = make_lease("x.post_tweet")
    out = await handler(lease, _FakeCtx(lease))

    _check(out["result"]["posted"] is True, "action result returned")
    _check(seen["text"] == "hello", "params reached the action")
    _check(browser.contexts[0].kw["storage_state"] == storage, "storage_state passed to context")  # type: ignore[attr-defined]
    _check(browser.contexts[0].kw["user_agent"] == "manor-test/1.0", "user_agent passed to context")  # type: ignore[attr-defined]
    _check(browser.closed is True, "browser closed after run")


async def case_resolver_returns_none() -> None:
    print("\n[case] resolver returns None → NeedHumanInput(missing_session)")
    install_fake_patchright(_FakePage())
    from packages.worker_sdk.adapters.browser import register_browser_adapter

    handlers: dict = {}
    class _W:
        def handle(self, *, kind, provider):
            def deco(fn):
                handlers[(kind, provider)] = fn
                return fn
            return deco
    w = _W()

    async def post(page, lease):
        return {}

    register_browser_adapter(
        w, provider="x",
        resolve_storage_state=lambda lease: _none(),
        actions={"x.post": post},
    )
    handler = handlers[("action", "x")]
    lease = make_lease("x.post")
    try:
        await handler(lease, _FakeCtx(lease))
    except NeedHumanInput as exc:
        _check(exc.kind == "missing_session", "kind=missing_session")
        _check("pair" in exc.prompt.lower(), "prompt mentions re-pairing")
    else:
        _check(False, "expected NeedHumanInput")


async def case_signed_out_body() -> None:
    print("\n[case] action body says 'sign in' → NeedHumanInput")
    page = _FakePage(post_action_body="Please log in to continue")
    install_fake_patchright(page)
    from packages.worker_sdk.adapters.browser import register_browser_adapter

    handlers: dict = {}
    class _W:
        def handle(self, *, kind, provider):
            def deco(fn):
                handlers[(kind, provider)] = fn
                return fn
            return deco
    w = _W()

    async def post(page, lease):
        return {"posted": True}

    async def resolver(lease):
        return {"cookies": [], "origins": []}

    register_browser_adapter(
        w, provider="x",
        resolve_storage_state=resolver,
        actions={"x.post": post},
    )
    handler = handlers[("action", "x")]
    lease = make_lease("x.post")
    try:
        await handler(lease, _FakeCtx(lease))
    except NeedHumanInput as exc:
        _check(exc.kind == "missing_session", "kind=missing_session detected")
    else:
        _check(False, "expected NeedHumanInput from sign-out detection")


async def case_unknown_action() -> None:
    print("\n[case] unknown action_key → graceful failure result")
    install_fake_patchright(_FakePage())
    from packages.worker_sdk.adapters.browser import register_browser_adapter

    handlers: dict = {}
    class _W:
        def handle(self, *, kind, provider):
            def deco(fn):
                handlers[(kind, provider)] = fn
                return fn
            return deco
    w = _W()

    async def known(page, lease):
        return {}

    register_browser_adapter(
        w, provider="x",
        resolve_storage_state=lambda lease: _none(),
        actions={"x.known": known},
    )
    handler = handlers[("action", "x")]
    lease = make_lease("x.unknown")
    out = await handler(lease, _FakeCtx(lease))
    _check(out["result"]["ok"] is False, "ok=False on unknown action")
    _check("x.known" in out["result"]["available"], "available actions listed")


async def _none() -> Optional[dict]:
    return None


async def main() -> None:
    await case_dry_run_python()
    await case_live_action_runs()
    await case_resolver_returns_none()
    await case_signed_out_body()
    await case_unknown_action()
    print("\nSMOKE OK")


if __name__ == "__main__":
    asyncio.run(main())
