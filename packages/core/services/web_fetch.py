"""Shared async URL fetcher — single place for HTTP fetch logic.

Used by:
  - Document URL upload (needs raw bytes + content-type)
  - web_fetch tool (needs parsed text/markdown)
  - Google Drive sync, etc.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Manor AI Bot)",
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
}


@dataclass
class FetchResult:
    """Result of an async URL fetch."""
    content: bytes
    content_type: str
    url: str            # final URL after redirects
    status_code: int


async def fetch_url(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = 0,
    headers: dict[str, str] | None = None,
) -> FetchResult:
    """Fetch a URL asynchronously. Returns raw bytes + metadata.

    Parameters
    ----------
    url : str
        URL to fetch.
    timeout : float
        Request timeout in seconds (default 30).
    max_bytes : int
        If > 0, reject responses larger than this (raises ValueError).
    headers : dict, optional
        Extra headers to merge with defaults.

    Raises
    ------
    httpx.HTTPStatusError
        On non-2xx responses.
    ValueError
        If response exceeds max_bytes.
    httpx.TimeoutException
        On timeout.
    """
    merged_headers = {**_DEFAULT_HEADERS, **(headers or {})}

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers=merged_headers)
        resp.raise_for_status()

        content = resp.content
        ct = resp.headers.get("content-type", "")

        if max_bytes > 0 and len(content) > max_bytes:
            raise ValueError(
                f"Response too large: {len(content)} bytes (max {max_bytes})"
            )

        return FetchResult(
            content=content,
            content_type=ct,
            url=str(resp.url),
            status_code=resp.status_code,
        )
