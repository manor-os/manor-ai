"""E2E tests: OpenAPI schema export and API versioning."""

import pytest
from httpx import AsyncClient
from fastapi.routing import APIRoute

pytestmark = pytest.mark.oss_regression


@pytest.mark.asyncio
async def test_openapi_schema_accessible(client: AsyncClient):
    response = await client.get("/api/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "Manor AI"
    assert schema["info"]["version"] == "0.1.0"
    assert "paths" in schema


@pytest.mark.asyncio
async def test_swagger_docs_accessible(client: AsyncClient):
    response = await client.get("/api/docs")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_schema_has_all_paths(client: AsyncClient):
    response = await client.get("/api/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]

    expected = [
        "/api/v1/auth/register",
        "/api/v1/tasks",
        "/api/v1/chat/stream",
        "/api/v1/agents",
        "/api/v1/documents",
        "/api/v1/workspaces",
        "/api/v1/entities/me",
        "/health",
    ]
    for path in expected:
        assert path in paths, f"Missing expected path: {path}"


def test_app_does_not_register_duplicate_http_routes():
    from apps.api.main import app

    seen: dict[tuple[str, str], str] = {}
    duplicates: list[tuple[str, str, str, str]] = []

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or []:
            if method in {"HEAD", "OPTIONS"}:
                continue
            key = (route.path, method)
            endpoint = f"{route.endpoint.__module__}.{route.endpoint.__name__}"
            previous = seen.get(key)
            if previous is not None:
                duplicates.append((method, route.path, previous, endpoint))
            else:
                seen[key] = endpoint

    assert duplicates == []


def _route_paths(app) -> set[str]:
    return set(app.openapi()["paths"])


def _route_path_strings(app) -> set[str]:
    paths: set[str] = set()

    def visit(route) -> None:
        if path := getattr(route, "path", ""):
            paths.add(path)
        if original_router := getattr(route, "original_router", None):
            for child in getattr(original_router, "routes", []):
                visit(child)
        for child in getattr(route, "routes", []) or []:
            visit(child)

    for route in app.routes:
        visit(route)
    return paths


