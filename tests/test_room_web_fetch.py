from __future__ import annotations

import httpx
import pytest

from packages.core.services import dashboard_http, room_web_fetch
from packages.core.services.dashboard_http import PublicHttpsEndpoint
from packages.core.services.room_web_fetch import (
    MAX_ROOM_WEB_BYTES,
    ROOM_WEB_FETCH_SCHEMA,
    RoomWebFetchError,
    fetch_room_public_page,
)


def _mock_public_request(monkeypatch: pytest.MonkeyPatch, handler):
    target = PublicHttpsEndpoint(
        url="https://example.com/page",
        hostname="example.com",
        port=443,
        addresses=("93.184.216.34",),
    )
    requests: list[httpx.Request] = []
    client_options: list[dict] = []
    original_client = httpx.AsyncClient

    async def resolve(_url: str) -> PublicHttpsEndpoint:
        return target

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    def client_factory(*args, **kwargs):
        client_options.append(kwargs)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(room_web_fetch, "resolve_public_https_target", resolve)
    monkeypatch.setattr(
        room_web_fetch,
        "create_public_https_transport",
        lambda _target: httpx.MockTransport(respond),
    )
    monkeypatch.setattr(room_web_fetch.httpx, "AsyncClient", client_factory)
    return requests, client_options


def test_room_web_fetch_schema_is_read_only_public_page() -> None:
    function = ROOM_WEB_FETCH_SCHEMA["function"]
    assert function["name"] == "web_fetch"
    assert function["parameters"]["required"] == ["url"]
    assert function["parameters"]["additionalProperties"] is False
    assert "public HTTPS" in function["description"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/page",
        "https://localhost/private",
        "https://127.0.0.1/private",
        "https://169.254.169.254/latest/meta-data",
        "https://user:password@example.com/page",
        "https://example.com:8443/page",
    ],
)
async def test_room_web_fetch_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(RoomWebFetchError, match="Only public HTTPS"):
        await fetch_room_public_page(url)


@pytest.mark.asyncio
async def test_room_web_fetch_rejects_private_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_to_thread(*_args, **_kwargs):
        return [(None, None, None, None, ("10.0.0.5", 443))]

    monkeypatch.setattr(dashboard_http.asyncio, "to_thread", fake_to_thread)
    with pytest.raises(RoomWebFetchError, match="Only public HTTPS"):
        await fetch_room_public_page("https://example.com/page")


@pytest.mark.asyncio
async def test_room_web_fetch_returns_bounded_untrusted_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"<html><body><h1>Example</h1><p>" + (b"a" * 1_500) + b"</p></body></html>"
    requests, client_options = _mock_public_request(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=body,
        ),
    )

    first = await fetch_room_public_page("https://example.com/page", max_length=1_000)
    second = await fetch_room_public_page(
        "https://example.com/page",
        offset=first["next_offset"],
        expected_sha256=first["source_sha256"],
    )

    assert first["content"].startswith("# Example")
    assert first["source_trust"] == "untrusted_public_web_page"
    assert first["chars_returned"] == 1_000
    assert first["next_offset"] == 1_000
    assert second["next_offset"] is None
    assert len(requests) == 2
    assert all(request.method == "GET" for request in requests)
    assert all("authorization" not in request.headers for request in requests)
    assert all("cookie" not in request.headers for request in requests)
    assert all(options["follow_redirects"] is False for options in client_options)
    assert all(options["trust_env"] is False for options in client_options)


@pytest.mark.asyncio
async def test_room_web_fetch_rejects_redirect_without_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests, _options = _mock_public_request(
        monkeypatch,
        lambda _request: httpx.Response(
            302,
            headers={"location": "https://127.0.0.1/private"},
        ),
    )

    with pytest.raises(RoomWebFetchError, match="redirects are not followed"):
        await fetch_room_public_page("https://example.com/page")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_room_web_fetch_rejects_pdf_before_reading_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnreadableBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("PDF body should not be read")
            yield b""

    _mock_public_request(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            stream=UnreadableBody(),
        ),
    )

    with pytest.raises(RoomWebFetchError, match="Only public HTML"):
        await fetch_room_public_page("https://example.com/page")


@pytest.mark.asyncio
async def test_room_web_fetch_streaming_cap_without_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OversizeBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a" * MAX_ROOM_WEB_BYTES
            yield b"z"

    _mock_public_request(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            stream=OversizeBody(),
        ),
    )

    with pytest.raises(RoomWebFetchError, match="too large"):
        await fetch_room_public_page("https://example.com/page")


@pytest.mark.asyncio
async def test_room_web_fetch_stale_digest_does_not_return_page_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_public_request(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"new page",
        ),
    )

    result = await fetch_room_public_page(
        "https://example.com/page", expected_sha256="old"
    )
    assert result["error"] == "source_changed"
    assert "content" not in result
