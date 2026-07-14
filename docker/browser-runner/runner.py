"""Browser-runner — RPC server for headless web automation.

One endpoint:
  POST /perform { provider, action, params, storage_state }
    → loads provider module from providers/<provider>.py
    → opens headless Chromium with the given storage_state (cookies +
      localStorage from the user's browser, captured client-side)
    → calls provider.perform(page, action, params)
    → returns the JSON the provider produced

The runner is stateless. Every call ships its own credentials.
Sessions / cookies are stored in Manor's main api container via
CredentialService → Vault, NOT here.

Auth between Manor api and this runner: shared bearer token
(``BROWSER_RUNNER_TOKEN`` env). Both sides must agree. If unset,
runs unauthenticated (dev only).
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import time
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel
from starlette.background import BackgroundTask

logger = logging.getLogger("browser-runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Manor Browser Runner")

_RUNNER_TOKEN = os.environ.get("BROWSER_RUNNER_TOKEN", "").strip()

# Headed login-session router — interactive Chromium streamed back to
# the user's browser via WebSocket so they can sign in once and we
# capture storage_state automatically (no cookie copy-paste).
import login_session as _login_session  # noqa: E402
_login_session.install(app, _RUNNER_TOKEN)

# Artifact store — capability-token-gated download channel for tools
# that need to send binaries back to the api (e.g. PDF download,
# scraped image). Providers call ``store.publish(...)`` to register a
# file and return a token; the api wrapper fetches via /artifacts/{token}.
from artifact_store import store as _artifact_store  # noqa: E402
_DEFAULT_TIMEOUT_MS = int(os.environ.get("BROWSER_RUNNER_TIMEOUT_MS", "30000"))
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


# ── Schemas ─────────────────────────────────────────────────────────────────

class PerformRequest(BaseModel):
    provider: str
    action: str
    params: Dict[str, Any] = {}
    # Playwright storage_state JSON — cookies + localStorage. Either
    # exported by the user (e.g. via a browser extension that grabs
    # the session) or captured by Manor on a previous headed login.
    storage_state: Optional[Dict[str, Any]] = None
    # Per-call override; defaults to BROWSER_RUNNER_TIMEOUT_MS.
    timeout_ms: Optional[int] = None
    headless: bool = True


class PerformResponse(BaseModel):
    ok: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    elapsed_ms: int


# ── Auth ────────────────────────────────────────────────────────────────────

def _check_auth(authorization: Optional[str]) -> None:
    if not _RUNNER_TOKEN:
        return  # Dev mode — no token configured
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != _RUNNER_TOKEN:
        raise HTTPException(403, "Bad token")


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"ok": True, "providers": _list_providers()}


@app.get("/artifacts/{token}")
async def fetch_artifact(
    token: str,
    authorization: Optional[str] = Header(default=None),
):
    """One-time artifact download. Consumes the token: a second GET
    returns 404. The token is the only credential — providers don't
    leak entity_ids to the runner, and the runner doesn't know who
    owns what. The api wrapper authenticates the inbound /perform
    call and is responsible for piping the bytes to the right tenant.

    Behavior:
      - 404 on unknown / expired / already-consumed tokens (no info leak
        about which tokens exist).
      - 200 with FileResponse on hit, then immediately schedules unlink.
    """
    _check_auth(authorization)
    entry = await _artifact_store.consume(token)
    if entry is None:
        raise HTTPException(404, "artifact not found or already consumed")

    # The store has already removed the registry entry. We delete the
    # on-disk file AFTER FastAPI finishes streaming via background task.
    def _cleanup():
        try:
            entry.path.unlink(missing_ok=True)
            entry.path.parent.rmdir()
        except OSError:
            pass

    return FileResponse(
        path=str(entry.path),
        media_type=entry.mime,
        filename=entry.filename,
        background=BackgroundTask(_cleanup),
    )


def _list_providers() -> list[str]:
    """Discover provider modules at runtime — drop in a new
    providers/<name>.py and it shows up here."""
    here = os.path.dirname(__file__)
    pdir = os.path.join(here, "providers")
    if not os.path.isdir(pdir):
        return []
    out: list[str] = []
    for fname in os.listdir(pdir):
        if fname.endswith(".py") and not fname.startswith("_"):
            out.append(fname[:-3])
    return sorted(out)


def _provider_version(module) -> str:
    version = getattr(module, "PROVIDER_VERSION", "")
    return str(version).strip() if version else ""


@app.post("/perform", response_model=PerformResponse)
async def perform(
    req: PerformRequest,
    authorization: Optional[str] = Header(default=None),
):
    _check_auth(authorization)

    if not req.provider.replace("_", "").isalnum():
        raise HTTPException(400, "invalid provider name")

    try:
        module = importlib.import_module(f"providers.{req.provider}")
    except ImportError as exc:
        raise HTTPException(404, f"unknown provider {req.provider!r}: {exc}")

    if not hasattr(module, "perform"):
        raise HTTPException(500, f"provider {req.provider} missing perform()")

    started = time.monotonic()
    try:
        result = await _run_with_browser(module, req)
        version = _provider_version(module)
        if version and isinstance(result, dict):
            result.setdefault("_provider_version", version)
        return PerformResponse(
            ok=True,
            result=result,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except asyncio.TimeoutError:
        return PerformResponse(
            ok=False,
            error=f"timeout after {req.timeout_ms or _DEFAULT_TIMEOUT_MS}ms",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001
        version = _provider_version(module)
        logger.exception(
            "perform failed for %s/%s provider_version=%s",
            req.provider,
            req.action,
            version or "unknown",
        )
        error = str(exc)
        if version:
            error = f"{error} [provider_version={version}]"
        return PerformResponse(
            ok=False,
            error=error,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


async def _run_with_browser(module, req: PerformRequest) -> Dict[str, Any]:
    """Open a Chromium context, apply stealth patches, run the
    provider's action, close. Stealth removes the most obvious
    headless fingerprints (navigator.webdriver, missing plugins,
    languages, WebGL spoof) — without it Google + Cloudflare often
    flag automation immediately."""
    timeout_ms = req.timeout_ms or _DEFAULT_TIMEOUT_MS

    # Some providers manage their own Chromium lifecycle — generic_browser
    # for example wraps browser-use, which only accepts a Browser instance
    # it constructed itself. For those, skip our launch path and pass the
    # storage_state through ``params`` so the provider can hand it to its
    # own Browser. Providers opt in with ``MANAGES_OWN_BROWSER = True`` at
    # module level.
    if getattr(module, "MANAGES_OWN_BROWSER", False):
        params = {**(req.params or {}), "_storage_state": req.storage_state}
        return await asyncio.wait_for(
            module.perform(None, req.action, params),
            timeout=timeout_ms / 1000,
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=req.headless,
            # These flags reduce detection further but require the
            # extra stealth patches below.
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1280, "height": 800},
                storage_state=req.storage_state if req.storage_state else None,
                locale="en-US",
                timezone_id="America/Los_Angeles",
            )
            context.set_default_timeout(timeout_ms)
            page = await context.new_page()
            # Per-provider stealth opt-out. Some sites detect
            # playwright-stealth's patches more reliably than they detect
            # raw automation. Provider modules can set ``USE_STEALTH =
            # False`` at module level to skip the stealth pass.
            if getattr(module, "USE_STEALTH", True):
                await _apply_stealth(page)
            return await asyncio.wait_for(
                module.perform(page, req.action, req.params),
                timeout=timeout_ms / 1000,
            )
        finally:
            await browser.close()


async def _apply_stealth(page) -> None:
    """Apply playwright-stealth's evasion patches. Imported lazily so
    the runner still boots even if the package becomes unavailable."""
    try:
        from playwright_stealth import stealth_async  # type: ignore
    except ImportError:
        logger.warning("playwright-stealth not installed — running unpatched")
        return
    try:
        await stealth_async(page)
    except Exception as exc:  # noqa: BLE001
        logger.warning("stealth_async failed: %s — continuing without", exc)
