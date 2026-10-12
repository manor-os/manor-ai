"""website_content — search and read the live website a public chat belongs to.

Armed only for public webchat turns on a channel that a live site is connected
to (see packages.core.services.site_content.guard_stream). It reads published
snapshots, which are already public, so every page of every connected site is
reachable however large the site is.
"""
from __future__ import annotations

import json
from typing import Any

from packages.core.services import site_content

WEBSITE_CONTENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": site_content.TOOL_NAME,
        "description": (
            "Search and read the published website this chat is on. action='search' "
            "with query ranks every passage of every page (title, section, url, snippet); "
            "with no match it returns an outline of every page and section to choose "
            "from by meaning. Use the site's own words and try synonyms. "
            "action='read' with url returns a page's full text, continuing with "
            "offset=next_offset for long pages. Use it before answering questions about "
            "the business, its products, prices or policies."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "read"]},
                "query": {"type": "string", "description": "search: words to look for."},
                "url": {"type": "string", "description": "read: page URL or path such as /pricing/."},
                "offset": {"type": "integer", "description": "read: continue a long page from next_offset."},
            },
            "required": ["action"],
        },
    },
}


async def _website_content_handler(entity_id: str = "", user_id: str | None = None, **kwargs: Any) -> str:
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
    from packages.core.database import async_session

    scope = site_content.current_site_chat_scope()
    context = runtime_tool_call_context_from_kwargs(kwargs, entity_id=entity_id, user_id=user_id)
    envelope = context.runtime_envelope
    if (
        scope is None
        or envelope is None
        or envelope.surface != ChatSurface.PUBLIC_CUSTOMER_CHAT
        or scope.entity_id != entity_id
        or (scope.conversation_id and scope.conversation_id != context.conversation_id)
    ):
        return json.dumps({"error": "website_content_unavailable"})
    async with async_session() as db:
        sites = await site_content.published_sites_for_webchat(
            db,
            entity_id=scope.entity_id,
            workspace_id=scope.workspace_id,
            channel_config_id=scope.channel_config_id,
        )
    action = str(kwargs.get("action") or "").strip()
    if action == "search":
        query = str(kwargs.get("query") or "")
        return json.dumps(site_content.search_pages(sites, query), ensure_ascii=False)
    if action == "read":
        try:
            offset = int(kwargs.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        return json.dumps(
            site_content.read_page(sites, str(kwargs.get("url") or ""), offset),
            ensure_ascii=False,
        )
    return json.dumps({"error": "unknown action; use search or read"})


def get_tools():
    return [(WEBSITE_CONTENT_SCHEMA, _website_content_handler)]
