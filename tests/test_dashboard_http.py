from __future__ import annotations

import pytest

from packages.core.services import dashboard_http
from packages.core.services.dashboard_http import (
    DashboardHttpPolicyError,
    validate_dashboard_http_url,
)


def test_dashboard_http_url_accepts_public_https_and_removes_fragment() -> None:
    assert validate_dashboard_http_url(
        "https://api.example.com/v1/data?region=west#preview"
    ) == "https://api.example.com/v1/data?region=west"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/data",
        "https://localhost/data",
        "https://127.0.0.1/data",
        "https://169.254.169.254/latest/meta-data",
        "https://user:secret@api.example.com/data",
        "https://api.example.com:8443/data",
    ],
)
def test_dashboard_http_url_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(DashboardHttpPolicyError):
        validate_dashboard_http_url(url)


@pytest.mark.asyncio
async def test_dashboard_http_dns_rejects_private_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_to_thread(*_args, **_kwargs):
        return [(None, None, None, None, ("10.0.0.5", 443))]

    monkeypatch.setattr(dashboard_http.asyncio, "to_thread", fake_to_thread)

    with pytest.raises(DashboardHttpPolicyError):
        await dashboard_http._resolve_public_host("api.example.com")
