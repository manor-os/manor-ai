"""Browser automation AI tools — browse_web, take_screenshot, interact_with_page.

These tools allow AI agents to control a headless browser for web scraping,
form interaction, and content extraction.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

BROWSE_WEB_SCHEMA = {
    "type": "function",
    "function": {
        "name": "browse_web",
        "description": (
            "Navigate to a URL in a real headless browser and return rendered visible content. "
            "Use this for JavaScript-rendered websites, SPAs, scraping data, or checking website status."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to.",
                },
                "extract_content": {
                    "type": "boolean",
                    "description": "Whether to extract page text content (default true).",
                },
                "selector": {
                    "type": "string",
                    "description": "Optional CSS selector to extract instead of the whole body.",
                },
                "wait_ms": {
                    "type": "integer",
                    "description": "Extra milliseconds to wait after load for SPA rendering (default 1200, cap 10000).",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum visible text characters to return (default 30000, cap 60000).",
                },
            },
            "required": ["url"],
        },
    },
}

TAKE_SCREENSHOT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "take_screenshot",
        "description": (
            "Take a screenshot of the current browser page. "
            "Returns a base64-encoded PNG image. "
            "Must call browse_web first to navigate to a page."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "full_page": {
                    "type": "boolean",
                    "description": "Capture the full scrollable page (default false, viewport only).",
                },
            },
            "required": [],
        },
    },
}

INTERACT_WITH_PAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "interact_with_page",
        "description": (
            "Interact with elements on the current browser page. "
            "Supports click, fill (type text into input), and evaluate (run JavaScript). "
            "Must call browse_web first to navigate to a page."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "fill", "evaluate"],
                    "description": "The interaction type.",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector for the target element (required for click/fill).",
                },
                "value": {
                    "type": "string",
                    "description": "Value to type (required for fill action).",
                },
                "script": {
                    "type": "string",
                    "description": "JavaScript to execute (required for evaluate action).",
                },
            },
            "required": ["action"],
        },
    },
}


# ---------------------------------------------------------------------------
# Session management — one session per entity for tool calls
# ---------------------------------------------------------------------------

async def _get_or_create_session(entity_id: str):
    """Get the existing browser session for this entity or create one."""
    from packages.core.services.browser_service import (
        list_sessions,
        create_session,
        get_session,
    )

    sessions = await list_sessions(entity_id)
    running = [s for s in sessions if s["status"] == "running"]

    if running:
        session = await get_session(running[0]["session_id"])
        if session:
            return session

    # Create a new headless session
    return await create_session(entity_id, headless=True)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _browse_web(entity_id: str = "", **kwargs: Any) -> str:
    url = (kwargs.get("url") or "").strip()
    if not url:
        return json.dumps({"error": "url is required"})

    extract_content = kwargs.get("extract_content", True)
    selector = (kwargs.get("selector") or "").strip() or None
    try:
        wait_ms = max(0, min(int(kwargs.get("wait_ms") or 1200), 10_000))
    except (TypeError, ValueError):
        wait_ms = 1200
    try:
        max_chars = max(1_000, min(int(kwargs.get("max_chars") or 30_000), 60_000))
    except (TypeError, ValueError):
        max_chars = 30_000

    runner_error: str | None = None
    try:
        from packages.core.ai.mcp import _browser_runner

        resp = await _browser_runner.perform(
            provider="public_web",
            action="extract",
            params={
                "url": url,
                "extract_content": extract_content,
                "selector": selector,
                "wait_ms": wait_ms,
                "max_chars": max_chars,
            },
            timeout_ms=max(45_000, wait_ms + 45_000),
        )
        if resp.get("ok"):
            return json.dumps(resp.get("result") or {}, ensure_ascii=False)
        runner_error = str(resp.get("error") or "browser-runner returned non-ok")
    except Exception as e:
        runner_error = str(e)

    try:
        session = await _get_or_create_session(entity_id)
        nav_result = await session.navigate(url)

        result: dict[str, Any] = {
            "url": nav_result["url"],
            "title": nav_result.get("title", ""),
            "status": nav_result.get("status"),
        }

        if extract_content:
            content = await session.get_content()
            if len(content) > max_chars:
                content = content[:max_chars] + "\n\n... [content truncated]"
            result["content"] = content

        if runner_error:
            result["fallback_warning"] = (
                "browser-runner public_web was unavailable; used in-process browser fallback."
            )
        return json.dumps(result, ensure_ascii=False)

    except RuntimeError as e:
        return json.dumps({"error": str(e), "browser_runner_error": runner_error}, ensure_ascii=False)
    except Exception as e:
        logger.error("browse_web failed: %s", e)
        return json.dumps({
            "error": f"Failed to browse {url}: {e}",
            "browser_runner_error": runner_error,
        }, ensure_ascii=False)


async def _take_screenshot(entity_id: str = "", **kwargs: Any) -> str:
    full_page = bool(kwargs.get("full_page", False))

    try:
        from packages.core.services.browser_service import list_sessions, get_session

        sessions = await list_sessions(entity_id)
        running = [s for s in sessions if s["status"] == "running"]
        if not running:
            return json.dumps({"error": "No active browser session. Call browse_web first to navigate to a page."})

        session = await get_session(running[0]["session_id"])
        if not session:
            return json.dumps({"error": "Browser session not found"})

        png_bytes = await session.screenshot(full_page=full_page)
        b64 = base64.b64encode(png_bytes).decode("ascii")

        return json.dumps({
            "image_base64": b64,
            "format": "png",
            "url": session.current_url,
        })

    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("take_screenshot failed: %s", e)
        return json.dumps({"error": f"Screenshot failed: {e}"})


async def _interact_with_page(entity_id: str = "", **kwargs: Any) -> str:
    action = (kwargs.get("action") or "").strip()
    if not action:
        return json.dumps({"error": "action is required (click, fill, or evaluate)"})

    try:
        from packages.core.services.browser_service import list_sessions, get_session

        sessions = await list_sessions(entity_id)
        running = [s for s in sessions if s["status"] == "running"]
        if not running:
            return json.dumps({"error": "No active browser session. Call browse_web first."})

        session = await get_session(running[0]["session_id"])
        if not session:
            return json.dumps({"error": "Browser session not found"})

        if action == "click":
            selector = (kwargs.get("selector") or "").strip()
            if not selector:
                return json.dumps({"error": "selector is required for click"})
            await session.click(selector)
            return json.dumps({"action": "click", "selector": selector, "result": "ok"})

        elif action == "fill":
            selector = (kwargs.get("selector") or "").strip()
            value = kwargs.get("value", "")
            if not selector:
                return json.dumps({"error": "selector is required for fill"})
            await session.fill(selector, value)
            return json.dumps({"action": "fill", "selector": selector, "result": "ok"})

        elif action == "evaluate":
            script = (kwargs.get("script") or "").strip()
            if not script:
                return json.dumps({"error": "script is required for evaluate"})
            result = await session.evaluate(script)
            return json.dumps({"action": "evaluate", "result": str(result) if result is not None else None})

        else:
            return json.dumps({"error": f"Unknown action: {action}. Use click, fill, or evaluate."})

    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.error("interact_with_page failed: %s", e)
        return json.dumps({"error": f"Action '{action}' failed: {e}"})


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, callable]]:
    return [
        (BROWSE_WEB_SCHEMA, _browse_web),
        (TAKE_SCREENSHOT_SCHEMA, _take_screenshot),
        (INTERACT_WITH_PAGE_SCHEMA, _interact_with_page),
    ]
