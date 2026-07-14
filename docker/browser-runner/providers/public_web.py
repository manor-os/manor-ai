"""Public web extraction provider for browser-runner.

This provider renders normal public websites in Chromium and returns visible
text. It is intentionally read-only and does not require account credentials.
"""
from __future__ import annotations

import re
from typing import Any, Dict
from urllib.parse import urlparse


PROVIDER_VERSION = "20260520_01"
USE_STEALTH = True


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def _safe_inner_text(page, selector: str | None) -> tuple[str, str | None]:
    if selector:
        locator = page.locator(selector).first
        return await locator.inner_text(timeout=5_000), selector
    return await page.locator("body").inner_text(timeout=10_000), "body"


async def _collect_links(page) -> list[dict[str, str]]:
    links = await page.locator("a[href]").evaluate_all(
        """els => els.slice(0, 80).map(a => ({
            text: (a.innerText || a.textContent || '').trim().slice(0, 120),
            href: a.href
        })).filter(x => x.href)"""
    )
    if not isinstance(links, list):
        return []
    return [
        {"text": str(item.get("text") or ""), "href": str(item.get("href") or "")}
        for item in links
        if isinstance(item, dict)
    ]


async def perform(page, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if action != "extract":
        return {"error": "unsupported_action", "supported_actions": ["extract"]}

    url = str(params.get("url") or "").strip()
    if not _is_http_url(url):
        return {"error": "invalid_url", "message": "A public http(s) URL is required."}

    selector = str(params.get("selector") or "").strip() or None
    wait_ms = _bounded_int(params.get("wait_ms"), 1200, 0, 10_000)
    max_chars = _bounded_int(params.get("max_chars"), 30_000, 1_000, 60_000)
    extract_content = bool(params.get("extract_content", True))

    response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    network_idle = True
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        network_idle = False
    if wait_ms:
        await page.wait_for_timeout(wait_ms)

    title = await page.title()
    status = response.status if response else None
    content = ""
    selected = None
    if extract_content:
        try:
            content, selected = await _safe_inner_text(page, selector)
        except Exception:
            content = await page.evaluate("() => document.body ? document.body.innerText : ''")
            selected = "body"
        content = re.sub(r"\n{3,}", "\n\n", str(content or "").strip())

    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars] + "\n\n... [content truncated]"

    return {
        "url": page.url,
        "requested_url": url,
        "title": title,
        "status": status,
        "network_idle": network_idle,
        "provider": "public_web",
        "provider_version": PROVIDER_VERSION,
        "selector": selected,
        "chars_returned": len(content),
        "truncated": truncated,
        "links": await _collect_links(page),
        "content": content,
    }
