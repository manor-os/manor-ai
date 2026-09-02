from __future__ import annotations

import asyncio

import httpx
import pytest


@pytest.mark.asyncio
async def test_runner_credentials_are_bearer_protected_and_status_is_redacted():
    from apps.wechat_personal_runner import runner

    old_token = runner.RUNNER_BEARER_TOKEN
    runner.RUNNER_BEARER_TOKEN = "runner-secret"
    session = runner.Session(
        "persisted-session",
        bot_token="ilink-secret",
        base_url="https://ilink.example",
    )
    runner._sessions[session.sid] = session
    try:
        transport = httpx.ASGITransport(app=runner.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            status = await client.get("/sessions/persisted-session/status")
            assert status.status_code == 401

            status = await client.get(
                "/sessions/persisted-session/status",
                headers={"Authorization": "Bearer runner-secret"},
            )
            assert status.status_code == 200
            assert "bot_token" not in status.json()

            credentials = await client.get(
                "/sessions/persisted-session/credentials",
                headers={"Authorization": "Bearer runner-secret"},
            )
            assert credentials.status_code == 200
            assert credentials.json() == {
                "session_id": "persisted-session",
                "bot_token": "ilink-secret",
                "base_url": "https://ilink.example",
            }
    finally:
        runner._sessions.pop(session.sid, None)
        runner.RUNNER_BEARER_TOKEN = old_token


@pytest.mark.asyncio
async def test_runner_restores_token_backed_sessions(monkeypatch: pytest.MonkeyPatch):
    from apps.wechat_personal_runner import runner

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.response = httpx.Response(
                200,
                json=[
                    {
                        "session_id": "restored-session",
                        "bot_token": "ilink-secret",
                        "base_url": "https://ilink.example",
                        "account": {"nick_name": "Restored"},
                        "callback_url": "https://manor.example/callback",
                        "callback_bearer": "runner-secret",
                    },
                ],
                request=httpx.Request("GET", "http://api/internal"),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return self.response

    async def fake_long_poll(sess):
        await asyncio.sleep(0)

    monkeypatch.setattr(runner.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(runner, "MANOR_API_URL", "http://manor-api:8000")
    monkeypatch.setattr(runner, "RUNNER_BEARER_TOKEN", "runner-secret")
    monkeypatch.setattr(runner, "_long_poll_loop", fake_long_poll)

    restored = await runner._restore_sessions()
    try:
        assert restored == 1
        session = runner._sessions["restored-session"]
        assert session.online is True
        assert session.qr_pending is False
        assert session.client.bot_token == "ilink-secret"
        assert session.client.base_url == "https://ilink.example"
        assert session.callback_url == "https://manor.example/callback"
        assert session.callback_bearer == "runner-secret"
    finally:
        session = runner._sessions.pop("restored-session", None)
        if session:
            await session.shutdown()


@pytest.mark.asyncio
async def test_runner_retries_restore_until_api_is_available(monkeypatch: pytest.MonkeyPatch):
    from apps.wechat_personal_runner import runner

    attempts = 0
    delays: list[float] = []

    async def fake_restore() -> int | None:
        nonlocal attempts
        attempts += 1
        return None if attempts < 6 else 1

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(runner, "_restore_sessions", fake_restore)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)

    await runner._restore_sessions_with_retry()

    assert attempts == 6
    assert delays == [1, 2, 4, 8, 16]


@pytest.mark.asyncio
async def test_runner_ready_waits_for_restore_completion(monkeypatch: pytest.MonkeyPatch):
    from apps.wechat_personal_runner import runner

    old_ready = runner._restore_ready
    runner._restore_ready = False
    try:
        transport = httpx.ASGITransport(app=runner.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            pending = await client.get("/ready")
            assert pending.status_code == 503

            runner._restore_ready = True
            ready = await client.get("/ready")
            assert ready.status_code == 200
            assert ready.json()["ok"] is True
    finally:
        runner._restore_ready = old_ready


@pytest.mark.asyncio
async def test_runner_retries_auth_failure_instead_of_treating_it_as_empty_restore(
    monkeypatch: pytest.MonkeyPatch,
):
    from apps.wechat_personal_runner import runner

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return httpx.Response(
                401,
                json={"detail": "Bad WeChat runner bearer token"},
                request=httpx.Request("GET", "http://api/internal"),
            )

    monkeypatch.setattr(runner.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(runner, "MANOR_API_URL", "http://manor-api:8000")

    assert await runner._restore_sessions() is None
