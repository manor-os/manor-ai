"""Tests for health check and system status endpoints."""

from __future__ import annotations

import pytest

from apps.api.routers import health as health_router
from packages.core.credentials import HealthResult

pytestmark = pytest.mark.oss_smoke


@pytest.mark.asyncio
async def test_health(client):
    """Basic health endpoint returns 200 with status ok."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "timestamp" in data


@pytest.mark.asyncio
async def test_client_config_defaults_flows_to_coming_soon_in_prod(client, monkeypatch):
    monkeypatch.delenv("FLOWS_AVAILABLE", raising=False)
    monkeypatch.setenv("MANOR_ENV", "prod")

    response = await client.get("/config")

    assert response.status_code == 200
    assert response.json()["flows_available"] is False


@pytest.mark.asyncio
async def test_client_config_enables_flows_locally_or_by_override(client, monkeypatch):
    monkeypatch.delenv("FLOWS_AVAILABLE", raising=False)
    monkeypatch.setenv("MANOR_ENV", "local")
    local_response = await client.get("/config")
    assert local_response.json()["flows_available"] is True

    monkeypatch.setenv("MANOR_ENV", "production")
    monkeypatch.setenv("FLOWS_AVAILABLE", "true")
    override_response = await client.get("/config")
    assert override_response.json()["flows_available"] is True


@pytest.mark.asyncio
async def test_deep_health(client):
    """Deep health check returns postgres status (at minimum)."""
    resp = await client.get("/health/deep")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded")
    assert "checks" in data
    assert "postgres" in data["checks"]
    assert data["checks"]["postgres"]["status"] == "ok"
    assert "credentials" in data["checks"]


@pytest.mark.asyncio
async def test_deep_health_reports_credential_backend_failure(client, monkeypatch):
    """A bad Vault token must degrade /health/deep instead of looking healthy."""

    import packages.core.credentials as credentials

    class BrokenCredentialService:
        def health(self):
            return HealthResult(ok=False, detail="invalid token")

    monkeypatch.setattr(
        credentials,
        "get_credential_service",
        lambda: BrokenCredentialService(),
    )

    resp = await client.get("/health/deep")

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["checks"]["credentials"]["status"] == "error"
    assert "invalid token" in data["checks"]["credentials"]["detail"]


@pytest.mark.asyncio
async def test_readiness(client):
    """Readiness probe returns ready=true when DB is available."""
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ready"] is True


@pytest.mark.asyncio
async def test_system_info(client):
    """Deep health includes version and uptime in system info."""
    resp = await client.get("/health/deep")
    assert resp.status_code == 200
    data = resp.json()
    assert "system" in data
    system = data["system"]
    assert system["version"] == "0.1.0"
    assert "uptime_seconds" in system
    assert isinstance(system["uptime_seconds"], int)
    assert system["uptime_seconds"] >= 0
    assert "python" in system
    assert "platform" in system


def test_filesystem_marker_is_repaired_when_mount_exists(tmp_path, monkeypatch):
    marker = tmp_path / ".accesschk"
    monkeypatch.setattr(health_router, "_is_mountpoint", lambda path: True)

    ok, detail = health_router._ensure_fs_marker(str(tmp_path), str(marker))

    assert ok is True
    assert detail is None
    assert marker.exists()


def test_filesystem_marker_missing_fails_when_not_mounted(tmp_path, monkeypatch):
    marker = tmp_path / ".accesschk"
    monkeypatch.setattr(health_router, "_is_mountpoint", lambda path: False)

    ok, detail = health_router._ensure_fs_marker(str(tmp_path), str(marker))

    assert ok is False
    assert detail == f"{tmp_path} not mounted"
    assert not marker.exists()


def test_filesystem_marker_existing_still_requires_mount(tmp_path, monkeypatch):
    marker = tmp_path / ".accesschk"
    marker.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(health_router, "_is_mountpoint", lambda path: False)

    ok, detail = health_router._ensure_fs_marker(str(tmp_path), str(marker))

    assert ok is False
    assert detail == f"{tmp_path} not mounted"


