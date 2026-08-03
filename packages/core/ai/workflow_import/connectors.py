"""Shared connector resolution — the one place platform integration names are
mapped onto manor's connector model.

Every adapter (Dify ``tool`` nodes, n8n integration nodes, …) funnels its
platform-specific connector/tool references through :func:`resolve_connector`,
so after adaptation the canonical ``connector`` node looks identical regardless
of which platform it came from. manor's MCP tools are named
``mcp__<server_key>__<tool>``; when a provider maps to a known manor server the
node becomes directly executable, otherwise it's flagged ``resolved=False`` for
manual wiring (honest degradation rather than a silently broken call).
"""
from __future__ import annotations

import re

# Platform provider alias (normalised: lowercase, separators stripped) -> manor
# MCP server_key. Only providers manor actually ships a server for resolve to a
# server_key; the rest stay unresolved.
_PROVIDER_ALIASES: dict[str, str] = {
    "gmail": "gmail",
    "googlemail": "gmail",
    "github": "github",
    "telegram": "telegram",
    "discord": "discord",
    "linkedin": "linkedin",
    "twitter": "twitter_x",
    "twitterx": "twitter_x",
    "x": "twitter_x",
    "stripe": "stripe",
    "shopify": "shopify",
    "woocommerce": "woocommerce",
    "square": "square",
    "quickbooks": "quickbooks",
    "youtube": "youtube",
    "tiktok": "tiktok",
    "tiktokshop": "tiktok_shop",
    "amazon": "amazon",
    "googlecalendar": "google_calendar",
    "googledrive": "google_drive",
    "googlesheets": "google_drive",
    "microsoftteams": "ms_teams",
    "msteams": "ms_teams",
    "teams": "ms_teams",
    "microsoftoutlook": "outlook",
    "outlook": "outlook",
    "microsoftexcel": "ms_excel",
    "onedrive": "onedrive",
    "wechat": "wechat_official",
    "weixin": "wechat_official",
    "facebook": "facebook",
    "xiaohongshu": "xiaohongshu_local",
}

_manor_keys_cache: set[str] | None = None


def _manor_server_keys() -> set[str]:
    """manor's known MCP server keys, derived lazily so the map stays accurate."""
    global _manor_keys_cache
    if _manor_keys_cache is None:
        try:
            from packages.core.ai.tools import mcp_builtin
            _manor_keys_cache = set(mcp_builtin._SERVER_TOOL_SCHEMAS.keys())
        except Exception:  # pragma: no cover - defensive
            _manor_keys_cache = set()
    return _manor_keys_cache


def _normalize(provider: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(provider or "").lower())


def resolve_connector(
    provider: str,
    *,
    operation: str | None = None,
    args: dict | None = None,
) -> dict:
    """Map a platform connector reference to canonical manor connector config.

    Returns a uniform dict (same shape for every adapter):
      - provider: original platform provider string
      - server_key: manor MCP server_key, if resolved
      - tool: ``mcp__<server_key>__<operation>`` when both are known (the key
        the runner's connector node uses to actually invoke)
      - operation, args: passed through
      - resolved: True if it maps to a manor server, else False (needs wiring)
    """
    norm = _normalize(provider)
    server_key = _PROVIDER_ALIASES.get(norm)
    if server_key is None and norm in _manor_server_keys():
        server_key = norm

    cfg: dict = {"provider": provider, "args": args or {}, "resolved": server_key is not None}
    if operation:
        cfg["operation"] = operation
    if server_key:
        cfg["server_key"] = server_key
        if operation:
            cfg["tool"] = f"mcp__{server_key}__{operation}"
    return cfg
