"""Geo-IP service edge cases.

Private and unparsable IPs short-circuit to None before either Redis
or the external API is touched. External API failures are swallowed
and return None. We don't need a fixture for the real ip-api.com
endpoint — those would be flaky.
"""

import pytest

from packages.core.services.geo_ip import _is_private, lookup_geo


def test_geo_key_is_loaded_from_environment(monkeypatch):
    from packages.core.config import Settings
    monkeypatch.setenv("GEO_IP_API_KEY", " synthetic-test-key ")
    assert Settings().GEO_IP_API_KEY == "synthetic-test-key"


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


@pytest.mark.asyncio
async def test_registration_geo_requires_https_key_and_never_uses_legacy_cache(monkeypatch):
    from packages.core.config import get_settings
    from unittest.mock import Mock
    monkeypatch.setattr(get_settings(), "GEO_IP_API_KEY", "")
    redis = Mock()
    monkeypatch.setattr("packages.core.services.geo_ip._redis", redis)
    assert await lookup_geo("8.8.8.8", require_https=True) is None
    redis.assert_not_called()


@pytest.mark.asyncio
async def test_https_geo_validates_timezone_and_keeps_separate_cache(monkeypatch):
    from packages.core.config import get_settings
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    import httpx
    monkeypatch.setattr(get_settings(), "GEO_IP_API_KEY", "synthetic-test-key")
    redis = SimpleNamespace(get=AsyncMock(return_value=None), setex=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr("packages.core.services.geo_ip._redis", lambda: redis)
    captured = []
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, **kwargs):
            captured.append((url, kwargs))
            return httpx.Response(200, json={"status": "success", "countryCode": "US", "country": "United States", "timezone": "America/Los_Angeles"})
    monkeypatch.setattr("packages.core.services.geo_ip.httpx.AsyncClient", Client)
    result = await lookup_geo("8.8.8.8", require_https=True)
    assert result["timezone"] == "America/Los_Angeles"
    assert captured[0][0] == "https://pro.ip-api.com/json/8.8.8.8"
    assert captured[0][1]["params"]["key"] == "synthetic-test-key"
    redis.get.assert_awaited_once_with("geo:https:v2:8.8.8.8")
    redis.aclose.assert_awaited_once()
    from packages.core.observability.log_redaction import redact_sensitive_log_text
    assert "synthetic-test-key" not in redact_sensitive_log_text("https://pro.ip-api.com/json/8.8.8.8?key=synthetic-test-key&fields=timezone")


@pytest.mark.asyncio
async def test_geo_cancellation_closes_redis(monkeypatch):
    import asyncio
    from packages.core.config import get_settings
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    monkeypatch.setattr(get_settings(), "GEO_IP_API_KEY", "synthetic-test-key")
    redis = SimpleNamespace(get=AsyncMock(side_effect=asyncio.CancelledError), aclose=AsyncMock())
    monkeypatch.setattr("packages.core.services.geo_ip._redis", lambda: redis)
    with pytest.raises(asyncio.CancelledError):
        await lookup_geo("8.8.8.8", require_https=True)
    redis.aclose.assert_awaited_once()
