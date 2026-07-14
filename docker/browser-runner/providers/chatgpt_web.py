"""ChatGPT (chatgpt.com) — drive the web app on a user's Plus / Team
subscription.

Auth: OpenAI's session cookie (``__Secure-next-auth.session-token``)
plus Cloudflare cookies (``cf_clearance``, ``__cf_bm``). Export from
a logged-in browser via Cookie-Editor → JSON.

Actions:
  * new_chat(prompt, model?)    — start a fresh conversation
  * continue_chat(chat_id, prompt) — append to an existing thread
  * list_chats(limit?)          — recent conversations

ChatGPT's UI changes often. Selectors here target the April 2026
layout; if they break, re-record with Playwright codegen.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict


_BASE = "https://chatgpt.com"


async def perform(page, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if action == "new_chat":
        return await _new_chat(page, params)
    if action == "continue_chat":
        return await _continue_chat(page, params)
    if action == "list_chats":
        return await _list_chats(page, params)
    return {"error": f"unknown chatgpt_web action: {action!r}"}


# ── new_chat ────────────────────────────────────────────────────────────────

async def _new_chat(page, params: Dict[str, Any]) -> Dict[str, Any]:
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        return {"error": "prompt required"}

    await page.goto(f"{_BASE}/?model=auto", wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    # Optional model picker — Plus users can switch between auto / 4o /
    # o1 / o3-mini etc. via the dropdown next to "ChatGPT".
    model = params.get("model")
    if model:
        try:
            await page.click("[data-testid='model-switcher-dropdown-button']", timeout=3000)
            await page.click(f"text={model}", timeout=3000)
        except Exception:
            pass

    # Type into composer (contenteditable div in 2026 layout).
    typed = False
    for sel in (
        "div[contenteditable='true'][data-id='root']",
        "#prompt-textarea",
        "textarea[data-id='root']",
        "div[contenteditable='true']",
    ):
        try:
            el = await page.wait_for_selector(sel, timeout=4000)
            if not el:
                continue
            await el.click()
            await page.keyboard.type(prompt, delay=10)
            typed = True
            break
        except Exception:
            continue
    if not typed:
        return {"error": "could not find ChatGPT composer"}

    # Submit. ChatGPT historically uses the Enter key + a separate
    # send button — try keyboard first, fall back to button.
    try:
        await page.keyboard.press("Enter")
    except Exception:
        try:
            await page.click("button[data-testid='send-button']", timeout=2000)
        except Exception:
            return {"error": "could not submit prompt"}

    # Wait for streaming to finish — the "stop generating" button
    # disappears, replaced by the "regenerate" button.
    await _wait_for_response(page, timeout_s=180.0)

    # Read final assistant message.
    answer = ""
    try:
        msgs = await page.query_selector_all(
            "[data-message-author-role='assistant'], "
            "[data-testid*='conversation-turn'][data-testid*='assistant']"
        )
        if msgs:
            answer = (await msgs[-1].text_content() or "").strip()
    except Exception:
        pass

    chat_id = page.url.rsplit("/", 1)[-1] if "/c/" in page.url else None
    return {"chat_id": chat_id, "url": page.url, "answer": answer[:8000]}


# ── continue_chat ──────────────────────────────────────────────────────────

async def _continue_chat(page, params: Dict[str, Any]) -> Dict[str, Any]:
    chat_id = (params.get("chat_id") or "").strip()
    prompt = (params.get("prompt") or "").strip()
    if not chat_id or not prompt:
        return {"error": "chat_id and prompt required"}

    await page.goto(f"{_BASE}/c/{chat_id}", wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    typed = False
    for sel in (
        "div[contenteditable='true'][data-id='root']",
        "#prompt-textarea",
        "div[contenteditable='true']",
    ):
        try:
            el = await page.wait_for_selector(sel, timeout=4000)
            if not el:
                continue
            await el.click()
            await page.keyboard.type(prompt, delay=10)
            typed = True
            break
        except Exception:
            continue
    if not typed:
        return {"error": "could not find composer"}

    await page.keyboard.press("Enter")
    await _wait_for_response(page, timeout_s=180.0)

    answer = ""
    try:
        msgs = await page.query_selector_all(
            "[data-message-author-role='assistant']"
        )
        if msgs:
            answer = (await msgs[-1].text_content() or "").strip()
    except Exception:
        pass

    return {"chat_id": chat_id, "answer": answer[:8000]}


# ── list_chats ─────────────────────────────────────────────────────────────

async def _list_chats(page, params: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(params.get("limit") or 20)
    await page.goto(_BASE, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    out: list[Dict[str, str]] = []
    try:
        # Sidebar lists recent conversations.
        cards = await page.query_selector_all(
            "nav a[href*='/c/'], aside a[href*='/c/']"
        )
        for c in cards[:limit]:
            href = await c.get_attribute("href") or ""
            cid = href.rsplit("/", 1)[-1] if "/c/" in href else None
            title = (await c.text_content()) or ""
            if cid:
                out.append({"id": cid, "title": title.strip(), "href": href})
    except Exception:
        pass

    return {"count": len(out), "chats": out}


# ── helpers ────────────────────────────────────────────────────────────────

async def _wait_for_response(page, *, timeout_s: float) -> None:
    """Block until ChatGPT finishes streaming. Strategy: wait for the
    Stop button to appear, then disappear. If neither happens, just
    wait a couple of seconds and let the caller read whatever's there."""
    try:
        await page.wait_for_selector(
            "[data-testid='stop-button'], button[aria-label*='Stop']",
            state="visible", timeout=10_000,
        )
        await page.wait_for_selector(
            "[data-testid='stop-button'], button[aria-label*='Stop']",
            state="hidden", timeout=int(timeout_s * 1000),
        )
    except Exception:
        await asyncio.sleep(2.0)


async def _ensure_logged_in(page) -> None:
    if "/auth/login" in page.url or "auth0.com" in page.url:
        raise RuntimeError(
            "ChatGPT session is not authenticated — re-export your "
            "cookies from a browser where you're signed into ChatGPT."
        )
