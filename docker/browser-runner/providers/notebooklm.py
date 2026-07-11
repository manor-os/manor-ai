"""NotebookLM (notebooklm.google.com) — browser automation provider.

NotebookLM has no public API. Manor drives a logged-in Chromium
session against the web app instead. The user supplies a Playwright
``storage_state`` JSON (typically exported via a Chrome extension
like "Cookie-Editor → Export → JSON") that includes Google's
auth cookies (NID, SAPISID, __Secure-1PSID, …).

Actions:
  * list_notebooks     — returns the user's notebooks (id + title)
  * create_notebook    — start a new notebook with optional source URLs
  * ask                — submit a question against a notebook, return
                         the cited answer

Caveats — Google's UI churns; selectors here are best-effort against
the April 2026 layout. If a step fails with a wait_for_selector
timeout, re-record with Playwright codegen and update the strings.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict


_BASE_URL = "https://notebooklm.google.com"


async def perform(page, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if action == "list_notebooks":
        return await _list_notebooks(page)
    if action == "create_notebook":
        return await _create_notebook(page, params)
    if action == "ask":
        return await _ask(page, params)
    return {"error": f"unknown notebooklm action: {action!r}"}


# ── list_notebooks ──────────────────────────────────────────────────────────

async def _list_notebooks(page) -> Dict[str, Any]:
    await page.goto(_BASE_URL, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    items: list[Dict[str, str]] = []
    cards = await page.query_selector_all(
        "[data-testid*='notebook'], .notebook-card, mat-card"
    )
    for c in cards[:50]:
        try:
            title_el = (
                await c.query_selector("[data-testid*='title']")
                or await c.query_selector("h2, h3, .title")
            )
            title = (await title_el.text_content()) if title_el else None
            link_el = await c.query_selector("a[href*='/notebook/']")
            href = await link_el.get_attribute("href") if link_el else None
            nb_id = href.rsplit("/", 1)[-1] if href else None
            if title and nb_id:
                items.append({"id": nb_id, "title": title.strip(), "href": href})
        except Exception:
            continue
    return {"count": len(items), "notebooks": items}


# ── create_notebook ────────────────────────────────────────────────────────

async def _create_notebook(page, params: Dict[str, Any]) -> Dict[str, Any]:
    name = (params.get("name") or "Untitled").strip()
    sources = params.get("sources") or []   # list of URLs

    await page.goto(_BASE_URL, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    # Hit the "+ Create" / "+ New notebook" button.
    create_clicked = False
    for sel in (
        "button:has-text('Create new')",
        "button:has-text('+ New')",
        "button:has-text('New notebook')",
        "[aria-label*='Create']",
    ):
        try:
            await page.click(sel, timeout=3000)
            create_clicked = True
            break
        except Exception:
            continue
    if not create_clicked:
        return {"error": "could not find 'Create' button — UI may have changed"}

    # The new-notebook flow opens a "Add sources" modal first.
    if sources:
        for url in sources[:10]:
            try:
                await page.click("text=Website", timeout=3000)
                await page.fill("input[placeholder*='URL'], input[type='url']", url)
                await page.click("button:has-text('Insert')", timeout=4000)
                await asyncio.sleep(1.0)
            except Exception:
                continue

    # Wait for the notebook to settle, then rename it.
    try:
        await page.wait_for_url("**/notebook/**", timeout=20_000)
    except Exception:
        pass
    nb_url = page.url
    nb_id = nb_url.rsplit("/", 1)[-1] if "/notebook/" in nb_url else None

    if name and name != "Untitled":
        try:
            await page.click("[data-testid='notebook-title'], h1[contenteditable]", timeout=3000)
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.type(name)
            await page.keyboard.press("Enter")
        except Exception:
            pass

    return {"id": nb_id, "url": nb_url, "name": name, "sources_added": len(sources)}


# ── ask ──────────────────────────────────────────────────────────────────────

async def _ask(page, params: Dict[str, Any]) -> Dict[str, Any]:
    notebook_id = (params.get("notebook_id") or "").strip()
    question = (params.get("question") or "").strip()
    if not notebook_id or not question:
        return {"error": "notebook_id and question required"}

    nb_url = f"{_BASE_URL}/notebook/{notebook_id}"
    await page.goto(nb_url, wait_until="domcontentloaded")
    await _ensure_logged_in(page)

    # Find the chat input. NotebookLM uses contenteditable / textarea
    # depending on the rollout cohort — try a few.
    typed = False
    for sel in (
        "[role='textbox']",
        "textarea[placeholder*='Ask']",
        "div[contenteditable='true']",
    ):
        try:
            el = await page.wait_for_selector(sel, timeout=4000)
            if not el:
                continue
            await el.click()
            await page.keyboard.type(question, delay=15)
            typed = True
            break
        except Exception:
            continue
    if not typed:
        return {"error": "could not find chat input; UI may have changed"}

    # Submit. Enter usually works; Send button is the fallback.
    try:
        await page.keyboard.press("Enter")
    except Exception:
        try:
            await page.click("button:has-text('Send')", timeout=2000)
        except Exception:
            return {"error": "could not submit question"}

    # Wait for the response to render. NotebookLM streams; the answer
    # is "settled" when the citation chips appear at the end. Poll
    # for citations or up to 90s.
    answer_text = ""
    citations: list[Dict[str, str]] = []
    deadline = asyncio.get_event_loop().time() + 90.0
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(2.0)
        try:
            # Last assistant message bubble.
            msgs = await page.query_selector_all(
                "[data-testid*='response'], .response-content, .chat-message"
            )
            if msgs:
                last = msgs[-1]
                txt = await last.text_content()
                answer_text = (txt or "").strip()
            # Citations as small numbered chips.
            chips = await page.query_selector_all("[data-testid*='citation'], .citation-chip")
            citations = []
            for chip in chips[-12:]:
                ttl = await chip.get_attribute("title")
                num = await chip.text_content()
                if ttl:
                    citations.append({"index": (num or "").strip(), "source": ttl.strip()})
            if answer_text and citations:
                break
        except Exception:
            continue

    return {
        "notebook_id": notebook_id,
        "question": question,
        "answer": answer_text[:8000],
        "citations": citations,
    }


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _ensure_logged_in(page) -> None:
    """Cheap sanity check — if we hit the login redirect, the cookies
    are stale or scoped to a different account."""
    if "accounts.google.com" in page.url:
        raise RuntimeError(
            "NotebookLM session is not authenticated — re-export your "
            "cookie jar from a browser where you're signed into Google."
        )
