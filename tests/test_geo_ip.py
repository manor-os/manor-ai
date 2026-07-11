"""Geo-IP service edge cases.

Private and unparsable IPs short-circuit to None before either Redis
or the external API is touched. External API failures are swallowed
and return None. We don't need a fixture for the real ip-api.com
endpoint — those would be flaky.
"""

import pytest

from packages.core.services.geo_ip import _is_private, lookup_geo


def test_private_ip_detector_covers_common_ranges():
    assert _is_private("127.0.0.1")
    assert _is_private("10.0.0.1")
    assert _is_private("192.168.1.1")
    assert _is_private("172.16.0.1")
    assert _is_private("::1")
    assert _is_private("fe80::1")
    assert _is_private("not-an-ip")  # unparsable → treated as private
    assert not _is_private("8.8.8.8")
    assert not _is_private("1.1.1.1")


@pytest.mark.asyncio
async def test_lookup_geo_returns_none_for_private_input(monkeypatch):
    monkeypatch.setattr(
        "packages.core.services.geo_ip._redis",
        lambda: None,
    )
    assert await lookup_geo("10.0.0.1") is None
    assert await lookup_geo("127.0.0.1") is None
    assert await lookup_geo("") is None
    assert await lookup_geo(None) is None


@pytest.mark.asyncio
async def test_lookup_geo_returns_none_on_http_failure(monkeypatch):
    """The ip-api call raising / 5xx / malformed response should never
    bubble — geo is best-effort and the caller relies on None as a
    sentinel for "no enrichment available"."""
    monkeypatch.setattr(
        "packages.core.services.geo_ip._redis",
        lambda: None,
    )

    class _BoomClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *_, **__):
            raise RuntimeError("boom")

    monkeypatch.setattr("packages.core.services.geo_ip.httpx.AsyncClient", _BoomClient)
    assert await lookup_geo("8.8.8.8") is None
