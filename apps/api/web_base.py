"""
Browser-facing origin resolution for backend-minted URLs.

When the backend constructs a URL that's meant to be opened by a browser
— a share link, an OAuth callback, a public chat URL, etc. — using
``request.base_url`` is almost always wrong: that's the host:port the
backend was hit on, not the host the user is browsing.

In dev, frontend at ``localhost:3010`` proxies ``/api/*`` to backend at
``localhost:8000`` → ``request.base_url == http://localhost:8000/`` →
the minted link would point at the backend port (which serves API JSON,
not the SPA). The recipient gets a 404 or JSON.

In production behind nginx/caddy, the proxy strips the original host
unless ``X-Forwarded-*`` headers are passed through and trusted.

This helper handles both cases:

  1. ``settings.APP_URL`` — explicit per-env override (best)
  2. ``X-Forwarded-Host`` + ``X-Forwarded-Proto`` — vite dev proxy sets
     these (see ``forwardOriginalHost`` in apps/web/vite.config.ts), and
     nginx/caddy usually do too
  3. ``Host`` header — last resort that picks up the original origin
     when no forwarder is in front
  4. ``settings.PUBLIC_BASE_URL`` or ``request.base_url`` — final fallback

Extracted from ``apps/api/routers/public_chat.py`` so share-link minting
in ``document_permissions.py`` / ``folder_permissions.py`` (and future
callers) doesn't have to re-implement the same logic — and so a single
bug fix updates every URL-minting site at once.
"""
from __future__ import annotations

from fastapi import Request

from packages.core.config import get_settings


def _first_header(value: str | None) -> str:
    """Trim and pick the first comma-separated value from a header.

    Proxies sometimes chain ``X-Forwarded-Host`` (e.g. ``client.example,
    edge.example``); only the leftmost entry — the original client — is
    meaningful for link minting.
    """
    if not value:
        return ""
    return value.split(",", 1)[0].strip()


def public_web_base(request: Request) -> str:
    """Return the browser-facing app origin (scheme + host[:port]) for the
    current request — never includes a trailing slash.

    Always prefer this over ``str(request.base_url)`` when minting URLs
    the user will paste into a browser or send to a recipient.
    """
    settings = get_settings()

    configured = (settings.APP_URL or "").strip().rstrip("/")
    if configured:
        return configured

    forwarded_host = _first_header(
        request.headers.get("x-forwarded-host") or request.headers.get("host"),
    )
    forwarded_proto = _first_header(
        request.headers.get("x-forwarded-proto") or request.url.scheme or "http",
    )
    if forwarded_host:
        return f"{forwarded_proto}://{forwarded_host}".rstrip("/")

    fallback = (settings.PUBLIC_BASE_URL or str(request.base_url) or "").strip().rstrip("/")
    return fallback
