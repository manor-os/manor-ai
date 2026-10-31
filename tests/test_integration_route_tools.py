"""A platform action uses its connected integration first and Chrome only without one."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.ai.tools import integration_route_tools as tools


def test_platform_names_resolve_to_the_integration_that_acts_on_them():
    assert tools.integration_provider_for_platform("instagram") == "facebook"
    assert tools.integration_provider_for_platform("x") == "twitter_x"
    assert tools.integration_provider_for_platform("YouTube") == "youtube"
    # A browser-only platform has no integration to prefer.
    assert tools.integration_provider_for_platform("reddit") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connected", "chrome", "route"),
    [
        ({"youtube"}, True, "integration"),
        ({"youtube"}, False, "integration"),
        (set(), True, "chrome"),
        (set(), False, "none"),
    ],
)
async def test_the_integration_wins_whenever_it_is_connected(monkeypatch, connected, chrome, route):
    async def readiness(_db, *, entity_id, user_id, provider_keys):
        return {
            key: SimpleNamespace(ready=(key == "chrome" and chrome) or key in connected, reason=f"{key} state")
            for key in provider_keys
        }

    monkeypatch.setattr(
        "packages.core.services.integration_resolution.integration_provider_readiness", readiness,
    )
    result = await tools.integration_routes(None, entity_id="e", user_id="u", platforms=["youtube", "reddit"])

    assert result["routes"]["youtube"]["route"] == route
    assert result["routes"]["youtube"]["integration"] == "youtube"
    assert result["routes"]["reddit"]["route"] == ("chrome" if chrome else "none")
