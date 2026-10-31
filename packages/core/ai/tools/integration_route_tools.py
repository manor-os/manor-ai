"""Choose how a platform action is carried out: its integration first, Chrome second."""
from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs


RESOLVE_PLATFORM_ROUTES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "resolve_platform_routes",
        "description": (
            "Read-only: for each platform, report whether its connected integration "
            "can carry out the action, else whether signed-in Chrome can. The chosen "
            "route is always the integration when it is connected, Chrome only when it "
            "is not, and none when neither is available. Call before acting on a platform."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "platforms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Platform names, e.g. youtube, instagram, tiktok, x, linkedin, reddit.",
                },
            },
            "required": ["platforms"],
        },
    },
}


def integration_provider_for_platform(platform: str) -> str | None:
    """The registered integration that acts on a platform, or None when only a browser can."""
    from packages.core.ai.runtime.integration_skill_registry import INTEGRATION_SKILL_ROUTES
    from packages.core.services.integration_resolution import canonical_provider_key

    name = str(platform or "").strip().lower()
    if not name:
        return None
    canonical = canonical_provider_key(name)
    if canonical in INTEGRATION_SKILL_ROUTES:
        return canonical
    spaced = name.replace("_", " ")
    for route in INTEGRATION_SKILL_ROUTES.values():
        if name in route.aliases or spaced in route.aliases:
            return route.provider_key
    # Reddit, Medium, Hacker News…: no integration, the browser is the route.
    return None


async def integration_routes(
    db: Any,
    *,
    entity_id: str,
    user_id: str | None,
    platforms: list[str],
) -> dict[str, Any]:
    """Pick one route per platform: integration when connected, else Chrome, else none."""
    from packages.core.services.integration_resolution import integration_provider_readiness

    names = list(dict.fromkeys(str(item or "").strip().lower() for item in platforms if str(item or "").strip()))
    providers = {name: integration_provider_for_platform(name) for name in names}
    readiness = await integration_provider_readiness(
        db,
        entity_id=entity_id,
        user_id=user_id,
        provider_keys=[*{provider for provider in providers.values() if provider}, "chrome"],
    )
    chrome = readiness.get("chrome")
    chrome_ready = bool(chrome and chrome.ready)
    routes: dict[str, Any] = {}
    for name, provider in providers.items():
        state = readiness.get(provider) if provider else None
        integration_ready = bool(state and state.ready)
        routes[name] = {
            "route": "integration" if integration_ready else "chrome" if chrome_ready else "none",
            "integration": provider,
            "integration_ready": integration_ready,
            "integration_reason": state.reason if state else "This platform has no integration; only Chrome can act on it.",
            "chrome_ready": chrome_ready,
        }
    return {
        "routes": routes,
        "chrome_ready": chrome_ready,
        "chrome_reason": chrome.reason if chrome else "",
    }


async def _resolve_platform_routes(platforms: list[str] | None = None, entity_id: str = "", **kwargs: Any) -> str:
    from packages.core.database import async_session

    context = runtime_tool_call_context_from_kwargs(kwargs)
    if not entity_id:
        return json.dumps({"error": "An account is required to check integration routes."})
    async with async_session() as db:
        result = await integration_routes(
            db, entity_id=entity_id, user_id=context.user_id, platforms=list(platforms or []),
        )
    return json.dumps(result, ensure_ascii=False)


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [(RESOLVE_PLATFORM_ROUTES_SCHEMA, _resolve_platform_routes)]
