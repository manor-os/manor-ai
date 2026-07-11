from __future__ import annotations

import json

import pytest

from packages.core.services.integration_health import test_browser_session as _test_browser_session


def _creds(cookie_names: list[str]) -> dict:
    return {
        "api_key": json.dumps(
            {
                "cookies": [{"name": name, "value": "v", "expires": -1} for name in cookie_names],
            }
        ),
    }


@pytest.mark.asyncio
async def test_other_browser_health_still_accepts_any_expected_cookie() -> None:
    result = await _test_browser_session(
        _creds(["li_at"]),
        provider="linkedin_browser",
    )

    assert result["ok"] is True
