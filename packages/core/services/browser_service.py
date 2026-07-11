"""Browser automation service using Playwright.

Provides programmatic browser control for:
- Web scraping and data extraction
- Form filling and submission
- Screenshot capture
- PDF generation from web pages
- Session management

Optional dependency: playwright
  Install: pip install playwright && playwright install chromium
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from packages.core.models.base import generate_ulid

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Browser session
# ---------------------------------------------------------------------------

class BrowserSession:
    """Represents an active browser automation session."""

    def __init__(self, session_id: str, entity_id: str):
        self.session_id = session_id
        self.entity_id = entity_id
        self.status: str = "created"
        self.current_url: str | None = None
        self.created_at: datetime = datetime.now(timezone.utc)

        # Playwright internals — set during start()
        self._pw: Any = None
        self.browser: Any = None
        self.page: Any = None

    async def start(self, headless: bool = True) -> None:
        """Launch browser instance.

        Raises RuntimeError if playwright is not installed.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "playwright is not installed. "
                "Run: pip install playwright && playwright install chromium"
            )

        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=headless)
        self.page = await self.browser.new_page()
        self.status = "running"
        logger.info("Browser session %s started (headless=%s)", self.session_id, headless)

    async def navigate(self, url: str) -> dict[str, Any]:
        """Navigate to URL, return page info."""
        self._ensure_running()
        response = await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        self.current_url = self.page.url
        return {
            "url": self.page.url,
            "title": await self.page.title(),
            "status": response.status if response else None,
        }

    async def screenshot(self, full_page: bool = False) -> bytes:
        """Take screenshot of current page. Returns PNG bytes."""
        self._ensure_running()
        return await self.page.screenshot(full_page=full_page, type="png")

    async def get_content(self) -> str:
        """Get page text content (innerText of body)."""
        self._ensure_running()
        return await self.page.inner_text("body")

    async def click(self, selector: str) -> None:
        """Click element by CSS selector."""
        self._ensure_running()
        await self.page.click(selector, timeout=10_000)

    async def fill(self, selector: str, value: str) -> None:
        """Fill input field by CSS selector."""
        self._ensure_running()
        await self.page.fill(selector, value, timeout=10_000)

    async def evaluate(self, script: str) -> Any:
        """Execute JavaScript in page context and return the result."""
        self._ensure_running()
        return await self.page.evaluate(script)

    async def pdf(self) -> bytes:
        """Generate PDF of current page.

        Note: PDF generation only works in headless Chromium.
        """
        self._ensure_running()
        return await self.page.pdf(format="A4", print_background=True)

    async def close(self) -> None:
        """Close browser session and release resources."""
        self.status = "closed"
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            logger.debug("Error closing browser for session %s", self.session_id, exc_info=True)
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            logger.debug("Error stopping playwright for session %s", self.session_id, exc_info=True)
        self.browser = None
        self.page = None
        self._pw = None
        logger.info("Browser session %s closed", self.session_id)

    def to_dict(self) -> dict[str, Any]:
        """Serialise session metadata (no browser internals)."""
        return {
            "session_id": self.session_id,
            "entity_id": self.entity_id,
            "status": self.status,
            "current_url": self.current_url,
            "created_at": self.created_at.isoformat(),
        }

    def _ensure_running(self) -> None:
        if self.status != "running" or not self.page:
            raise RuntimeError(f"Browser session {self.session_id} is not running")


# ---------------------------------------------------------------------------
# Session registry (in-process; for multi-node deploy use Redis or DB)
# ---------------------------------------------------------------------------

_sessions: dict[str, BrowserSession] = {}


async def create_session(entity_id: str, *, headless: bool = True) -> BrowserSession:
    """Create and start a new browser session."""
    session_id = generate_ulid()
    session = BrowserSession(session_id=session_id, entity_id=entity_id)
    await session.start(headless=headless)
    _sessions[session_id] = session
    return session


async def get_session(session_id: str) -> BrowserSession | None:
    """Get an existing session by ID."""
    return _sessions.get(session_id)


async def close_session(session_id: str) -> bool:
    """Close and remove a session. Returns False if not found."""
    session = _sessions.pop(session_id, None)
    if not session:
        return False
    await session.close()
    return True


async def list_sessions(entity_id: str) -> list[dict[str, Any]]:
    """List active sessions for an entity."""
    return [
        s.to_dict()
        for s in _sessions.values()
        if s.entity_id == entity_id
    ]
