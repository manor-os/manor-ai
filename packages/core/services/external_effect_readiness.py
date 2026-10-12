"""Is there an executable route for an external side effect right now?

Prod task FQ2881 (2026-09-14): the plan carried a "Publish Post" step for a
workspace that had no X account connected and no paired Chrome; the planner
produced the plan anyway and the publish agent discovered the missing route
only after the drafting step had run. This answers the question up front,
with the same readiness source the runtime uses (``integration_provider_readiness``),
so a plan can park on "connect X first" before any step burns credits.

Deterministic: which providers count for an effect comes from the provider
markers shared with the approval classifier, filtered to the providers this
deployment actually offers; Chrome is always a candidate route.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.core.plans.external_effects import EXTERNAL_EFFECT_PROVIDER_MARKERS

BROWSER_ROUTE_PROVIDER = "chrome"

#: How to bring the browser route up. The public build says only what it
#: ships; the cloud build adds the paired local worker requirement.
_BROWSER_ROUTE_HINT = "or pair the Manor Chrome extension under Settings → Integrations → Chrome"


@dataclass(frozen=True)
class ExternalRouteReadiness:
    effect: str
    ready: bool
    ready_providers: list[str] = field(default_factory=list)
    checked_providers: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    setup_links: dict[str, str] = field(default_factory=dict)
    message: str = ""


#: Catalog keys that share a platform's name but are not a publishing
#: surface (commerce, ads, analytics). "tiktok" must not pull in "tiktok_shop".
_NON_PUBLISHING_KEY_PARTS: tuple[str, ...] = ("shop", "ads", "analytics", "insights")


def _providers_for_effect(effect: str, supported: set[str]) -> list[str]:
    markers = EXTERNAL_EFFECT_PROVIDER_MARKERS.get(effect) or ()
    return sorted(
        key for key in supported
        if any(marker in key for marker in markers)
        and not any(part in key.split("_") for part in _NON_PUBLISHING_KEY_PARTS)
    )


def describe_missing_route(effect: str, providers: list[str], setup_links: dict[str, str]) -> str:
    verb = "publish" if effect == "external.social" else "send"
    links = ", ".join(
        f"[{p}]({setup_links[p]})" if p in setup_links else p for p in providers
    ) or "an integration for this channel"
    return (
        f"No connected route to {verb}: connect {links} under Settings → Integrations, "
        f"{_BROWSER_ROUTE_HINT}. Then retry the task."
    )


async def external_effect_route_readiness(
    db,
    *,
    entity_id: str,
    user_id: str | None,
    effect: str,
    declared_provider: str | None = None,
) -> ExternalRouteReadiness | None:
    """None when the effect has no provider class to check (nothing to say)."""
    from packages.core.ai.runtime.integration_setup_links import runtime_integration_setup_link
    from packages.core.services.integration_resolution import (
        integration_provider_readiness,
        supported_integration_provider_keys,
    )

    declared = str(declared_provider or "").strip()
    if declared:
        candidates = [declared]
    else:
        if effect not in EXTERNAL_EFFECT_PROVIDER_MARKERS:
            return None
        supported = await supported_integration_provider_keys(db)
        candidates = _providers_for_effect(effect, supported)
    routes = [*candidates, BROWSER_ROUTE_PROVIDER] if BROWSER_ROUTE_PROVIDER not in candidates else list(candidates)

    readiness = await integration_provider_readiness(
        db, entity_id=entity_id, provider_keys=routes, user_id=user_id,
    )
    ready = sorted(p for p, r in readiness.items() if r.ready)
    checked = sorted(readiness.keys())
    reasons = {p: r.reason for p, r in readiness.items() if not r.ready}
    setup_links: dict[str, str] = {}
    for provider in checked:
        if provider == BROWSER_ROUTE_PROVIDER:
            continue
        link = runtime_integration_setup_link(provider).get("setup_url")
        if link:
            setup_links[provider] = link
    is_ready = bool(ready)
    return ExternalRouteReadiness(
        effect=effect,
        ready=is_ready,
        ready_providers=ready,
        checked_providers=checked,
        reasons=reasons,
        setup_links=setup_links,
        message="" if is_ready else describe_missing_route(
            effect, [p for p in checked if p != BROWSER_ROUTE_PROVIDER], setup_links,
        ),
    )
