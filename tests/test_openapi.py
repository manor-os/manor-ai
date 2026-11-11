"""E2E tests: OpenAPI schema export and API versioning."""

import json
import re
from pathlib import Path

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
    assert schema["info"]["version"] == "0.2.1"
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


WEB_API_CLIENT = Path(__file__).resolve().parents[1] / "apps" / "web" / "src" / "lib" / "api.ts"
# Built from parts so the OSS export does not read this line as a strip marker.
_CLOUD_START, _CLOUD_END = "@manor-cloud-" + "start", "@manor-cloud-" + "end"
_CLOUD_BLOCK = re.compile(
    rf"^[ \t]*//[ \t]*{_CLOUD_START}\b.*?^[ \t]*//[ \t]*{_CLOUD_END}[^\n]*\n", re.S | re.M,
)
_REQUEST_CALL = re.compile(r"\brequest(?:Uncoalesced)?(?=[<(])")
_SINGLE_METHOD = re.compile(r"""method:\s*["'](\w+)["']\s*[,}]""")


def _skip_quoted(src: str, i: int) -> int:
    """Index just past the string/template literal that opens at ``src[i]``."""
    quote = src[i]
    i += 1
    while src[i] != quote:
        if src[i] == "\\":
            i += 2
        elif quote == "`" and src.startswith("${", i):
            i = _skip_nested(src, i + 2, "{", "}")
        else:
            i += 1
    return i + 1


def _skip_nested(src: str, i: int, open_ch: str, close_ch: str) -> int:
    """Index just past the ``close_ch`` balancing an ``open_ch`` already consumed."""
    depth = 1
    while depth:
        if src[i] in "\"'`":
            i = _skip_quoted(src, i)
            continue
        if src.startswith("//", i):
            i = src.index("\n", i)
        elif src.startswith("/*", i):
            i = src.index("*/", i) + 2
            continue
        depth += {open_ch: 1, close_ch: -1}.get(src[i], 0)
        i += 1
    return i


def _client_route(src: str, i: int) -> str | None:
    """Route template of the path literal at ``src[i]``; None for a dynamic prefix.

    An interpolation filling a whole path segment is a path parameter; one glued
    to other text (``/skills${suffix}``) or a literal ``?`` starts the query.
    """
    quote = src[i]
    i += 1
    path = ""
    while src[i] != quote and src[i] != "?":
        if quote == "`" and src.startswith("${", i):
            end = _skip_nested(src, i + 2, "{", "}")
            if not path:
                return None
            if not path.endswith("/") or src[end] not in f"/?${quote}":
                break
            path += "{param}"
            i = end
            continue
        path += src[i]
        i += 1
    return path


def _web_client_requests(mode: str) -> list[tuple[int, str | None, str]]:
    """(line, method or None for computed, route) for every request() call in api.ts."""
    src = WEB_API_CLIENT.read_text()
    if mode != "cloud":
        # Blank (not delete) Cloud blocks so reported line numbers stay real.
        src = _CLOUD_BLOCK.sub(lambda block: "\n" * block.group(0).count("\n"), src)
    calls = []
    for match in _REQUEST_CALL.finditer(src):
        i = match.end()
        if src[i] == "<":
            i = _skip_nested(src.replace("=>", "~~"), i + 1, "<", ">")
        if src[i] != "(":
            continue
        start = i + 1
        while src[start].isspace():
            start += 1
        if src[start] not in "\"'`":
            continue
        args = src[start:_skip_nested(src, i + 1, "(", ")")]
        route = _client_route(src, start)
        if route is None:
            continue
        method = _SINGLE_METHOD.search(args)
        method_name = method.group(1).upper() if method else ("GET" if "method:" not in args else None)
        calls.append((src.count("\n", 0, start) + 1, method_name, route))
    return calls


def _unmounted_web_client_requests(mode: str, monkeypatch) -> list[str]:
    monkeypatch.setenv("DEPLOYMENT_MODE", mode)
    from apps.api.main import create_app

    routes = [
        (route.methods, route.path_regex)
        for route in create_app().routes if isinstance(route, APIRoute)
    ]
    calls = _web_client_requests(mode)
    assert len(calls) > 500, "api.ts request() extraction stopped matching the client"
    return [
        f"api.ts:{line} {method or '*'} {path}"
        for line, method, path in calls
        if not any(
            (method is None or method in methods)
            and regex.match("/api/v1" + path.replace("{param}", "p"))
            for methods, regex in routes
        )
    ]


def test_web_api_client_requests_hit_mounted_oss_routes(monkeypatch):
    # The Tasks drawer once called PUT /tasks/{id}/status, which no router
    # mounted, so every status change from the drawer failed with 404/405.
    assert _unmounted_web_client_requests("oss", monkeypatch) == []








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


