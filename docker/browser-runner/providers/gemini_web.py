"""Gemini (gemini.google.com) — Google's chat AI on the user's
account / Gemini Advanced subscription.

Auth: Google's session cookies (NID, SAPISID, __Secure-1PSID, …) —
same as NotebookLM. Export from a logged-in browser.

Actions:
  * new_chat(prompt, model?)    — start a fresh conversation
  * continue_chat(chat_id, prompt) — append to existing
  * list_chats(limit?)          — recent conversations
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict


_BASE = "https://gemini.google.com"


async def perform(page, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if action == "new_chat":
        return await _new_chat(page, params)
    if action == "continue_chat":
        return await _continue_chat(page, params)
    if action == "list_chats":
        return await _list_chats(page, params)
    return {"error": f"unknown gemini_web action: {action!r}"}


async def _new_chat(page, params: Dict[str, Any]) -> Dict[str, Any]:
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        return {"error": "prompt required"}

    await page.goto(f"{_BASE}/app", wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    # Optional model picker — Advanced users can pick "2.5 Pro" / "Flash" etc.
    model = params.get("model")
    if model:
        try:
            await page.click("[data-test-id='bard-mode-menu-button']", timeout=3000)
            await page.click(f"text={model}", timeout=3000)
        except Exception:
            pass

    # Composer is a rich-text contenteditable.
    typed = False
    for sel in (
        "rich-textarea div[contenteditable='true']",
        "div[contenteditable='true'][role='textbox']",
        "textarea",
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
        return {"error": "could not find Gemini composer"}

    # Submit via send button (Enter inserts newline in Gemini).
    try:
        await page.click(
            "button[aria-label*='Send'], button[data-test-id='send-button']",
            timeout=4000,
        )
    except Exception:
        return {"error": "could not click send"}

    # Gemini shows a "Stop generating" affordance during streaming.
    await _wait_for_response(page, timeout_s=120.0)

    answer = ""
    try:
        msgs = await page.query_selector_all(
            "[data-test-id='response-content'], message-content, .response-container .markdown"
        )
        if msgs:
            answer = (await msgs[-1].text_content() or "").strip()
    except Exception:
        pass

    chat_id = page.url.rsplit("/", 1)[-1] if "/app/" in page.url else None
    return {"chat_id": chat_id, "url": page.url, "answer": answer[:8000]}


async def _continue_chat(page, params: Dict[str, Any]) -> Dict[str, Any]:
    chat_id = (params.get("chat_id") or "").strip()
    prompt = (params.get("prompt") or "").strip()
    if not chat_id or not prompt:
        return {"error": "chat_id and prompt required"}

    await page.goto(f"{_BASE}/app/{chat_id}", wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    typed = False
    for sel in (
        "rich-textarea div[contenteditable='true']",
        "div[contenteditable='true'][role='textbox']",
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

    try:
        await page.click("button[aria-label*='Send']", timeout=4000)
    except Exception:
        return {"error": "could not click send"}

    await _wait_for_response(page, timeout_s=120.0)

    answer = ""
    try:
        msgs = await page.query_selector_all("[data-test-id='response-content']")
        if msgs:
            answer = (await msgs[-1].text_content() or "").strip()
    except Exception:
        pass

    return {"chat_id": chat_id, "answer": answer[:8000]}


async def _list_chats(page, params: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(params.get("limit") or 20)
    await page.goto(f"{_BASE}/app", wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    out: list[Dict[str, str]] = []
    try:
        # Toggle the recent-chats sidebar if it's collapsed.
        try:
            await page.click("button[aria-label*='Show more']", timeout=2000)
        except Exception:
            pass
        cards = await page.query_selector_all(
            "[data-test-id='conversation'], .conversation-list-item"
        )
        for c in cards[:limit]:
            link_el = await c.query_selector("a") or c
            href = await link_el.get_attribute("href") or ""
            cid = href.rsplit("/", 1)[-1] if "/app/" in href else None
            title_el = await c.query_selector(
                "[data-test-id='conversation-title'], .conversation-title"
            )
            title = (await title_el.text_content()) if title_el else None
            if cid:
                out.append({"id": cid, "title": (title or "").strip(), "href": href})
    except Exception:
        pass

    return {"count": len(out), "chats": out}


async def _wait_for_response(page, *, timeout_s: float) -> None:
    try:
        await page.wait_for_selector(
            "button[aria-label*='Stop'], [data-test-id='stop-button']",
            state="visible", timeout=8_000,
        )
        await page.wait_for_selector(
            "button[aria-label*='Stop'], [data-test-id='stop-button']",
            state="hidden", timeout=int(timeout_s * 1000),
        )
    except Exception:
        await asyncio.sleep(2.0)


async def _ensure_logged_in(page) -> None:
    if "accounts.google.com" in page.url or "/signin" in page.url:
        raise RuntimeError(
            "Gemini session is not authenticated — re-export your "
            "Google session cookies from a logged-in browser."
        )
