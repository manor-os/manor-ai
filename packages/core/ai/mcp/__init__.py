"""In-process MCP servers — Python modules that implement the MCP
``tools/list`` + ``tools/call`` contract without spawning subprocesses
or running HTTP servers.

Each module exposes:
  * ``list_tools() -> List[Dict]``                  (MCP tools/list format)
  * ``call_tool(name, arguments, bearer_token) -> Dict``  (MCP tools/call format)

The registry below maps ``server_key`` (as stored in ``mcp_servers.server_key``)
to its Python module. ``mcp_builtin.py`` uses this registry to dispatch
tool calls after credentials are resolved by the agent runtime.
"""
from __future__ import annotations

from typing import Any, Dict, Protocol

from . import (
    gmail,
    google_calendar,
    google_drive,
    manor_mcp_calendar,
    github,
    linkedin,
    linkedin_browser,
    quickbooks,
    stripe,
    twitter_x,
    wechat_official,
    wechat_personal,
    telegram,
    replicate,
    elevenlabs,
    tavily,
    jimeng,
    notebooklm,
    claude_ai_web,
    chatgpt_web,
    gemini_web,
    perplexity_web,
    producthunt,
    facebook,
    youtube,
    tiktok,
    shopify,
    woocommerce,
    square,
    tiktok_shop,
    amazon,
)
from . import email as email_module   # aliased to avoid shadowing stdlib `email`


class MCPModule(Protocol):
    def list_tools(self) -> list[Dict[str, Any]]: ...

    async def call_tool(
        self, name: str, arguments: Dict[str, Any], bearer_token: str,
    ) -> Dict[str, Any]: ...


BUILTIN_MCP_MODULES: dict[str, MCPModule] = {
    "gmail": gmail,
    "google_calendar": google_calendar,
    "manor_mcp_calendar": manor_mcp_calendar,
    "google_drive": google_drive,
    "github": github,
    "linkedin": linkedin,
    "quickbooks": quickbooks,
    "stripe": stripe,
    "twitter_x": twitter_x,
    "email": email_module,
    "wechat_official": wechat_official,
    "wechat_personal": wechat_personal,
    "telegram": telegram,
    # AI generation / research APIs (api_key auth)
    "replicate": replicate,
    "elevenlabs": elevenlabs,
    "tavily": tavily,
    "jimeng": jimeng,
    # Browser-session AI tools (cookie auth, dispatch via browser-runner sidecar)
    "notebooklm": notebooklm,
    "claude_ai_web": claude_ai_web,
    "chatgpt_web": chatgpt_web,
    "gemini_web": gemini_web,
    "perplexity_web": perplexity_web,
    # Launch / community platforms
    "producthunt": producthunt,
    # Social platforms (OAuth via Nango)
    "facebook": facebook,
    # Video platforms (official API + OAuth). Instagram Reels publishing
    # lives in the `facebook` module (Meta Graph API).
    "youtube": youtube,
    "tiktok": tiktok,
    # E-commerce platforms (credentials/JSON-blob auth — store domain +
    # API token / consumer key+secret). Read + write: products, orders,
    # customers, inventory.
    "shopify": shopify,
    "woocommerce": woocommerce,
    "square": square,
    # Marketplace seller APIs (signed / token-exchange auth)
    "tiktok_shop": tiktok_shop,
    "amazon": amazon,
    # Social platforms (browser session — covers what each platform's
    # public API does NOT expose: profile/company/jobs/messages/feed)
    "linkedin_browser": linkedin_browser,
}


def get_module(server_key: str) -> MCPModule | None:
    return BUILTIN_MCP_MODULES.get(server_key)
