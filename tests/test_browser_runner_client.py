from __future__ import annotations

import json

import httpx
import pytest

from packages.core.ai.mcp import _browser_runner


def test_http_timeout_tracks_runner_timeout() -> None:
    timeout = _browser_runner._http_timeout_for(300_000)

    assert timeout.read == 330.0
    assert timeout.connect == 10.0


@pytest.mark.asyncio
async def test_perform_reports_read_timeout_not_unreachable(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("slow browser task")

    monkeypatch.setattr(_browser_runner.httpx, "AsyncClient", FakeClient)

    result = await _browser_runner.perform(
        provider="linkedin_browser",
        action="search_people",
        timeout_ms=300_000,
    )

    assert result["ok"] is False
    assert "timed out after 330s" in result["error"]
    assert "unreachable" not in result["error"].lower()


@pytest.mark.asyncio
async def test_perform_reports_connect_error_as_networking(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            raise httpx.ConnectError("name not resolved")

    monkeypatch.setattr(_browser_runner.httpx, "AsyncClient", FakeClient)

    result = await _browser_runner.perform(
        provider="linkedin_browser",
        action="search_people",
    )

    assert result["ok"] is False
    assert "Could not connect" in result["error"]
    assert "BROWSER_RUNNER_URL" in result["error"]


@pytest.mark.asyncio
async def test_call_provider_does_not_save_artifacts_by_default(monkeypatch) -> None:
    async def fake_perform(**_kwargs):
        return {
            "ok": True,
            "result": {
                "title": "Chrome page evidence",
                "artifacts": [
                    {
                        "token": "tok-page-image",
                        "filename": "page-image.png",
                        "mime": "image/png",
                    }
                ],
            },
        }

    called = False

    async def fake_process_result_artifacts(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("artifact persistence should be explicit")

    monkeypatch.setattr(_browser_runner, "perform", fake_perform)
    monkeypatch.setattr(
        "packages.core.ai.mcp._knowledge_artifact.process_result_artifacts",
        fake_process_result_artifacts,
    )

    result = await _browser_runner.call_provider(
        provider="generic_browser",
        name="extract",
        arguments={},
        bearer_token=json.dumps({"cookies": []}),
        entity_id="ent-A",
    )

    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["artifacts"][0]["token"] == "tok-page-image"
    assert "saved_to" not in payload["artifacts"][0]
    assert called is False


@pytest.mark.asyncio
async def test_call_provider_saves_artifacts_when_explicitly_enabled(monkeypatch) -> None:
    async def fake_perform(**_kwargs):
        return {
            "ok": True,
            "result": {
                "artifacts": [
                    {
                        "token": "tok-download",
                        "filename": "download.png",
                        "mime": "image/png",
                    }
                ],
            },
        }

    captured = {}

    async def fake_process_result_artifacts(result, **kwargs):
        captured.update(kwargs)
        result["artifacts"][0].pop("token", None)
        result["artifacts"][0]["saved_to"] = "Research/download.png"
        return result

    monkeypatch.setattr(_browser_runner, "perform", fake_perform)
    monkeypatch.setattr(
        "packages.core.ai.mcp._knowledge_artifact.process_result_artifacts",
        fake_process_result_artifacts,
    )

    result = await _browser_runner.call_provider(
        provider="linkedin_browser",
        name="view_profile",
        arguments={"save_to": "Research", "save_images": True},
        bearer_token=json.dumps({"cookies": []}),
        entity_id="ent-A",
        save_artifacts_to_knowledge=True,
    )

    assert result["isError"] is False
    assert captured == {
        "entity_id": "ent-A",
        "provider": "linkedin_browser",
        "target_folder": "Research",
    }
    payload = json.loads(result["content"][0]["text"])
    assert payload["artifacts"][0] == {
        "filename": "download.png",
        "mime": "image/png",
        "saved_to": "Research/download.png",
    }
