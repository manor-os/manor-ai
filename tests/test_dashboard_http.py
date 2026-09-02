from __future__ import annotations

import httpcore
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


def test_dashboard_http_url_uses_httpx_idna_profile() -> None:
    assert validate_dashboard_http_url(
        "https://faß.de/data"
    ) == "https://xn--fa-hia.de/data"


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


@pytest.mark.asyncio
async def test_public_https_target_can_pin_a_nonstandard_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved_ports: list[int] = []

    async def fake_to_thread(_resolver, _hostname, port, **_kwargs):
        resolved_ports.append(port)
        return [(None, None, None, None, ("93.184.216.34", port))]

    monkeypatch.setattr(dashboard_http.asyncio, "to_thread", fake_to_thread)

    target = await dashboard_http.resolve_public_https_target(
        "https://hooks.example.com:8443/events",
        allow_nonstandard_port=True,
    )

    assert target.url == "https://hooks.example.com:8443/events"
    assert target.port == 8443
    assert target.addresses == ("93.184.216.34",)
    assert resolved_ports == [8443]


@pytest.mark.asyncio
async def test_public_https_target_canonicalizes_idn_for_dns_and_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved_hosts: list[str] = []

    async def fake_to_thread(_resolver, hostname, port, **_kwargs):
        resolved_hosts.append(hostname)
        return [(None, None, None, None, ("93.184.216.34", port))]

    class FakeNetworkBackend:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def connect_tcp(self, host, port, **_kwargs):
            self.calls.append((host, port))
            return object()

    monkeypatch.setattr(dashboard_http.asyncio, "to_thread", fake_to_thread)

    target = await dashboard_http.resolve_public_https_target(
        "https://b\u00fccher.de/events"
    )
    backend = FakeNetworkBackend()
    pinned = dashboard_http._PinnedPublicNetworkBackend(target, backend=backend)
    stream = await pinned.connect_tcp("b\u00fccher.de", 443)

    assert stream is not None
    assert target.url == "https://xn--bcher-kva.de/events"
    assert target.hostname == "xn--bcher-kva.de"
    assert resolved_hosts == ["xn--bcher-kva.de"]
    assert backend.calls == [("93.184.216.34", 443)]


@pytest.mark.asyncio
async def test_pinned_backend_shares_one_timeout_budget_across_addresses(
) -> None:
    calls: list[tuple[str, float | None]] = []

    class FailingBackend:
        async def connect_tcp(self, host, _port, *, timeout, **_kwargs):
            calls.append((host, timeout))
            raise httpcore.ConnectTimeout("unreachable")

    monotonic_values = iter((100.0, 101.0, 104.0, 106.0))
    target = dashboard_http.PublicHttpsEndpoint(
        url="https://api.example.com/data",
        hostname="api.example.com",
        port=443,
        addresses=("93.184.216.34", "93.184.216.35", "93.184.216.36"),
    )
    backend = dashboard_http._PinnedPublicNetworkBackend(
        target,
        backend=FailingBackend(),
        monotonic=lambda: next(monotonic_values),
    )

    with pytest.raises(httpcore.ConnectTimeout, match="deadline exceeded"):
        await backend.connect_tcp("api.example.com", 443, timeout=6.0)

    assert [call[0] for call in calls] == ["93.184.216.34", "93.184.216.35"]
    assert calls[0][1] == pytest.approx(5.0)
    assert calls[1][1] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_public_host_caps_validated_connection_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_addresses = [f"93.184.216.{index}" for index in range(1, 11)]

    async def fake_to_thread(*_args, **_kwargs):
        return [
            (None, None, None, None, (address, 443))
            for address in public_addresses
        ]

    monkeypatch.setattr(dashboard_http.asyncio, "to_thread", fake_to_thread)

    assert await dashboard_http._resolve_public_host("api.example.com") == tuple(
        public_addresses[:dashboard_http.MAX_PUBLIC_HTTPS_ADDRESSES]
    )
