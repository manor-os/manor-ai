"""Claude.ai (web) — drive an existing Anthropic chat subscription.

Why: lets users with a paid Claude Pro / Max subscription (no API
budget) put Claude in the loop for agent tasks without burning API
credits. The subscription's daily message cap still applies.

Auth: Anthropic's session cookie (``sessionKey``) plus the standard
``__cf_bm`` / ``cf_clearance`` Cloudflare cookies. Export from a
logged-in browser via Cookie-Editor → JSON.

Actions:
  * new_chat(prompt, model?)   — start a fresh conversation
  * continue_chat(chat_id, prompt) — append to an existing thread
  * list_chats(limit?)         — recent conversations
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict


_BASE = "https://claude.ai"


async def perform(page, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if action == "new_chat":
        return await _new_chat(page, params)
    if action == "continue_chat":
        return await _continue_chat(page, params)
    if action == "list_chats":
        return await _list_chats(page, params)
    return {"error": f"unknown claude_ai_web action: {action!r}"}


async def _new_chat(page, params: Dict[str, Any]) -> Dict[str, Any]:
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        return {"error": "prompt required"}

    await page.goto(f"{_BASE}/new", wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    # Optional model picker — Claude.ai exposes a Sonnet/Opus dropdown
    # in the composer header. Click only if user requested a non-default.
    model = params.get("model")
    if model:
        try:
            await page.click("[aria-label*='model'], button:has-text('Sonnet'), button:has-text('Opus')", timeout=3000)
            await page.click(f"text={model}", timeout=3000)
        except Exception:
            pass

    # Type into the composer.
    typed = False
    for sel in (
        "div[contenteditable='true'][role='textbox']",
        "textarea",
        "[data-testid='chat-input']",
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
        return {"error": "could not find Claude.ai composer"}

    await page.keyboard.press("Enter")

    # Wait for response: Claude.ai's "stop generating" button vanishes
    # when the assistant finishes streaming.
    try:
        await page.wait_for_selector(
            "button:has-text('Stop')", state="visible", timeout=10_000,
        )
        await page.wait_for_selector(
            "button:has-text('Stop')", state="hidden", timeout=180_000,
        )
    except Exception:
        # Streaming finished too fast or selector changed — fall through
        # and read whatever we have.
        await asyncio.sleep(2.0)

    # Final assistant message.
    answer = ""
    try:
        msgs = await page.query_selector_all(
            "[data-testid*='message'], .font-claude-message, .prose"
        )
        if msgs:
            answer = (await msgs[-1].text_content() or "").strip()
    except Exception:
        pass

    chat_id = page.url.rsplit("/", 1)[-1] if "/chat/" in page.url else None
    return {"chat_id": chat_id, "url": page.url, "answer": answer[:8000]}


async def _continue_chat(page, params: Dict[str, Any]) -> Dict[str, Any]:
    chat_id = (params.get("chat_id") or "").strip()
    prompt = (params.get("prompt") or "").strip()
    if not chat_id or not prompt:
        return {"error": "chat_id and prompt required"}

    await page.goto(f"{_BASE}/chat/{chat_id}", wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    typed = False
    for sel in (
        "div[contenteditable='true'][role='textbox']",
        "textarea",
        "[data-testid='chat-input']",
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
    try:
        await page.wait_for_selector("button:has-text('Stop')", state="visible", timeout=10_000)
        await page.wait_for_selector("button:has-text('Stop')", state="hidden", timeout=180_000)
    except Exception:
        await asyncio.sleep(2.0)

    answer = ""
    try:
        msgs = await page.query_selector_all(
            "[data-testid*='message'], .font-claude-message, .prose"
        )
        if msgs:
            answer = (await msgs[-1].text_content() or "").strip()
    except Exception:
        pass

    return {"chat_id": chat_id, "answer": answer[:8000]}


async def _list_chats(page, params: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(params.get("limit") or 20)
    await page.goto(f"{_BASE}/chats", wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    out: list[Dict[str, str]] = []
    try:
        cards = await page.query_selector_all("a[href*='/chat/'], [data-testid*='chat-row']")
        for c in cards[:limit]:
            href = await c.get_attribute("href")
            title_el = await c.query_selector("h3, .title, [data-testid*='title']")
            title = (await title_el.text_content()) if title_el else None
            cid = href.rsplit("/", 1)[-1] if href else None
            if cid:
                out.append({"id": cid, "title": (title or "").strip(), "href": href})
    except Exception:
        pass

    return {"count": len(out), "chats": out}


async def _ensure_logged_in(page) -> None:
    if "/login" in page.url or "claude.ai" not in page.url:
        raise RuntimeError(
            "Claude.ai session is not authenticated — re-export your "
            "cookies from a browser where you're signed into Claude."
        )
