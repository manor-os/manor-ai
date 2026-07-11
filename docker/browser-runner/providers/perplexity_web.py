"""Perplexity (perplexity.ai) — drive a logged-in Pro account from
the web app instead of paying API per-request.

Why use the web instead of Perplexity's API: Pro subscribers get
unlimited Sonar Pro / Sonar Reasoning + file uploads without per-call
charges, but the API meters everything. For agents doing lots of
research, the web path is dramatically cheaper for Pro users.

Auth: ``__Secure-next-auth.session-token`` + Cloudflare cookies from
a Pro-logged-in browser.

Actions:
  * search(query, focus?, model?) — run a search-grounded query
  * follow_up(thread_id, query)   — append to an existing thread
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict


_BASE = "https://www.perplexity.ai"


async def perform(page, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if action == "search":
        return await _search(page, params)
    if action == "follow_up":
        return await _follow_up(page, params)
    return {"error": f"unknown perplexity_web action: {action!r}"}


async def _search(page, params: Dict[str, Any]) -> Dict[str, Any]:
    query = (params.get("query") or "").strip()
    if not query:
        return {"error": "query required"}

    await page.goto(_BASE, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    # Optional focus filter — All / Academic / Writing / Wolfram / Reddit / YouTube.
    focus = params.get("focus")
    if focus:
        try:
            await page.click("button[aria-label*='Focus']", timeout=3000)
            await page.click(f"text={focus}", timeout=3000)
        except Exception:
            pass

    # Model picker (Pro / Sonar Reasoning / o1 / GPT-4 / Claude / etc.)
    model = params.get("model")
    if model:
        try:
            await page.click("button[aria-label*='Model']", timeout=3000)
            await page.click(f"text={model}", timeout=3000)
        except Exception:
            pass

    typed = False
    for sel in (
        "textarea[placeholder*='Ask']",
        "textarea[placeholder*='anything']",
        "div[contenteditable='true']",
        "textarea",
    ):
        try:
            el = await page.wait_for_selector(sel, timeout=4000)
            if not el:
                continue
            await el.click()
            await page.keyboard.type(query, delay=10)
            typed = True
            break
        except Exception:
            continue
    if not typed:
        return {"error": "could not find Perplexity search box"}

    await page.keyboard.press("Enter")

    # Wait for streaming to finish — Perplexity shows a stop button
    # then surfaces the citations row when done.
    await _wait_for_response(page, timeout_s=90.0)

    # Extract answer + sources.
    answer = ""
    try:
        msgs = await page.query_selector_all(
            "[data-testid*='answer'], .prose, [class*='answer']"
        )
        if msgs:
            answer = (await msgs[-1].text_content() or "").strip()
    except Exception:
        pass

    sources: list[Dict[str, str]] = []
    try:
        cite_cards = await page.query_selector_all(
            "[data-testid*='source'], a[class*='citation']"
        )
        for c in cite_cards[:15]:
            href = await c.get_attribute("href")
            title = (await c.text_content()) or ""
            if href and href.startswith("http"):
                sources.append({"url": href, "title": title.strip()[:120]})
    except Exception:
        pass

    thread_id = page.url.rsplit("/", 1)[-1] if "/search/" in page.url else None
    return {
        "thread_id": thread_id,
        "url": page.url,
        "query": query,
        "answer": answer[:8000],
        "sources": sources,
    }


async def _follow_up(page, params: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = (params.get("thread_id") or "").strip()
    query = (params.get("query") or "").strip()
    if not thread_id or not query:
        return {"error": "thread_id and query required"}

    await page.goto(f"{_BASE}/search/{thread_id}", wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    typed = False
    for sel in (
        "textarea[placeholder*='Ask']",
        "div[contenteditable='true']",
        "textarea",
    ):
        try:
            el = await page.wait_for_selector(sel, timeout=4000)
            if not el:
                continue
            await el.click()
            await page.keyboard.type(query, delay=10)
            typed = True
            break
        except Exception:
            continue
    if not typed:
        return {"error": "could not find follow-up box"}

    await page.keyboard.press("Enter")
    await _wait_for_response(page, timeout_s=90.0)

    answer = ""
    try:
        msgs = await page.query_selector_all(
            "[data-testid*='answer'], .prose"
        )
        if msgs:
            answer = (await msgs[-1].text_content() or "").strip()
    except Exception:
        pass

    return {"thread_id": thread_id, "query": query, "answer": answer[:8000]}


async def _wait_for_response(page, *, timeout_s: float) -> None:
    try:
        await page.wait_for_selector(
            "button[aria-label*='Stop']", state="visible", timeout=8_000,
        )
        await page.wait_for_selector(
            "button[aria-label*='Stop']", state="hidden",
            timeout=int(timeout_s * 1000),
        )
    except Exception:
        await asyncio.sleep(2.0)


async def _ensure_logged_in(page) -> None:
    if "/login" in page.url or "auth0.com" in page.url:
        raise RuntimeError(
            "Perplexity session is not authenticated — re-export your "
            "cookies from a Pro-logged-in browser."
        )
