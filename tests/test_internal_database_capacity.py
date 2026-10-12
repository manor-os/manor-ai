from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.routers import internal_capacity
from packages.core import database


@pytest.mark.asyncio
@pytest.mark.parametrize("role,enabled,status", [
    ("api", True, 200), ("chat", True, 200),
    ("api", False, 404), ("worker", True, 404), ("all", True, 404),
])
async def test_private_database_capacity_never_connects(monkeypatch, role, enabled, status):
    monkeypatch.setattr(internal_capacity, "get_settings", lambda: SimpleNamespace(
        MANOR_SERVICE_ROLE=role, MANOR_AUTOSCALING_METRICS_ENABLED=enabled,
    ))
    snapshot = {"admission_limit": 4, "active": 4, "waiting": 14, "rejected": 7}
    # Only snapshot access exists: acquiring a DB connection would fail.
    monkeypatch.setattr(database, "engine", SimpleNamespace(sync_engine=SimpleNamespace(
        _manor_database_runtime=SimpleNamespace(snapshot=lambda: snapshot),
    )))
    app = FastAPI()
    app.include_router(internal_capacity.router)
    path = "/internal/autoscaling/database"
    assert path not in app.openapi()["paths"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == status
    if status == 200:
        assert response.json() == snapshot
