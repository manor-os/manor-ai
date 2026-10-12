from __future__ import annotations

import json

import httpx
import pytest


class _Response:
    status_code = 200
    text = ""
    payload: object = {"id": "job_123", "status": "submitted"}

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class _Client:
    calls: list[dict] = []
    response: _Response = _Response()
    error: Exception | None = None

    def __init__(self, *args, **kwargs) -> None:
        self.options = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def suno_http(monkeypatch):
    from packages.core.ai.mcp import suno

    _Client.calls = []
    _Client.response = _Response()
    _Client.error = None
    monkeypatch.setattr(suno.httpx, "AsyncClient", _Client)
    return suno


def test_suno_tool_surface_is_complete() -> None:
    from packages.core.ai.mcp import suno

    assert {tool["name"] for tool in suno.list_tools()} == {
        "generate_music",
        "get_generation",
        "create_cover",
        "create_mashup",
        "get_usage",
    }


def test_suno_is_an_executable_audio_catalog_provider() -> None:
    from packages.core.constants.models import CATALOG
    from packages.core.services.model_provider_handlers import (
        catalog_provider_capability,
        handler_for_model,
    )

    assert "suno/music" in {item["id"] for item in CATALOG["audio"]}
    handler = handler_for_model("suno/music")
    assert handler is not None
    assert handler.base_url == "https://api.suno.com/v0"
    assert handler.env_vars == ("SUNO_API_KEY",)
    assert catalog_provider_capability("audio", "suno/music").runtime_strategy == "suno_audio_jobs"


def test_suno_normalizes_nested_generation_results() -> None:
    from packages.core.ai.mcp import suno

    submitted = {"data": {"taskId": "job_nested", "status": "queued"}}
    completed = {
        "data": {
            "status": "complete",
            "clips": [{"audioUrl": "https://cdn.example.test/song.mp3"}],
        }
    }

    assert suno.generation_id(submitted) == "job_nested"
    assert suno.generation_status(submitted) == "pending"
    assert suno.generation_status(completed) == "completed"
    assert suno.generation_audio_url(completed) == "https://cdn.example.test/song.mp3"


def test_suno_catalog_accepts_only_a_native_suno_key() -> None:
    from fastapi import HTTPException

    from apps.api.routers.auth import _validate_role_api_key_for_model
    from packages.core.services.platform_model_provider_keys import _validate_provider_key

    _validate_role_api_key_for_model(
        "audio",
        "sk_live_test-key-1234567890",
        "suno/music",
    )
    _validate_provider_key("suno", "sk_live_test-key-1234567890")
    with pytest.raises(HTTPException, match="Google, OpenAI, or Suno"):
        _validate_role_api_key_for_model(
            "audio",
            "sk-openai-test-key-1234567890",
            "suno/music",
        )
    with pytest.raises(ValueError, match="prefix looks like openai"):
        _validate_provider_key("suno", "sk-openai-test-key-1234567890")


@pytest.mark.asyncio
async def test_generate_music_submits_one_bearer_authenticated_job(suno_http):
    result = await suno_http.call_tool(
        "generate_music",
        {
            "description": "Warm indie pop for a product launch",
            "title": "First Light",
        },
        "sk_live_test",
    )

    assert result["isError"] is False
    assert result["structuredContent"] == {"id": "job_123", "status": "submitted"}
    assert json.loads(result["content"][0]["text"])["id"] == "job_123"
    assert _Client.calls == [
        {
            "method": "POST",
            "url": "https://api.suno.com/v0/audio",
            "headers": {
                "Authorization": "Bearer sk_live_test",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            "json": {
                "instrumental": False,
                "description": "Warm indie pop for a product launch",
                "title": "First Light",
            },
        }
    ]


@pytest.mark.asyncio
async def test_generate_music_accepts_custom_instrumental_without_lyrics(suno_http):
    result = await suno_http.call_tool(
        "generate_music",
        {"style": "cinematic electronic", "instrumental": True},
        "sk_live_test",
    )

    assert result["isError"] is False
    assert _Client.calls[0]["json"] == {
        "instrumental": True,
        "style": "cinematic electronic",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"description": "pop", "style": "jazz"}, "cannot be combined"),
        ({"lyrics": "hello"}, "style is required"),
        ({"style": "pop"}, "lyrics are required"),
        ({"description": "pop", "instrumental": "yes"}, "must be a boolean"),
    ],
)
async def test_generate_music_rejects_invalid_modes_before_http(
    suno_http,
    arguments,
    message,
):
    result = await suno_http.call_tool("generate_music", arguments, "sk_live_test")

    assert result["isError"] is True
    assert message in result["content"][0]["text"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_cover_and_mashup_use_source_ids_in_fixed_paths(suno_http):
    cover = await suno_http.call_tool(
        "create_cover",
        {"source_audio_id": "audio-one", "style": "acoustic"},
        "sk_live_test",
    )
    mashup = await suno_http.call_tool(
        "create_mashup",
        {
            "source_audio_id": "audio-one",
            "additional_audio_id": "audio-two",
            "title": "Combined",
        },
        "sk_live_test",
    )

    assert cover["isError"] is False
    assert mashup["isError"] is False
    assert _Client.calls[0]["url"] == "https://api.suno.com/v0/audio/audio-one/covers"
    assert _Client.calls[0]["json"] == {"style": "acoustic"}
    assert _Client.calls[1]["url"] == "https://api.suno.com/v0/audio/audio-one/mashups"
    assert _Client.calls[1]["json"] == {
        "title": "Combined",
        "additional_audio_id": "audio-two",
    }


@pytest.mark.asyncio
async def test_status_and_usage_are_read_only_requests(suno_http):
    await suno_http.call_tool(
        "get_generation",
        {"generation_id": "job_123"},
        "sk_live_test",
    )
    await suno_http.call_tool("get_usage", {}, "sk_live_test")

    assert [(call["method"], call["url"]) for call in _Client.calls] == [
        ("GET", "https://api.suno.com/v0/audio/job_123"),
        ("GET", "https://api.suno.com/v0/account/usage"),
    ]
    assert all("json" not in call for call in _Client.calls)


@pytest.mark.asyncio
async def test_paid_timeout_is_not_retried_and_warns_about_duplicate_charge(suno_http):
    _Client.error = httpx.ReadTimeout(
        "timed out",
        request=httpx.Request("POST", "https://api.suno.com/v0/audio"),
    )

    result = await suno_http.call_tool(
        "generate_music",
        {"description": "short synth theme"},
        "sk_live_test",
    )

    assert result["isError"] is True
    assert len(_Client.calls) == 1
    message = result["content"][0]["text"].lower()
    assert "may have been accepted" in message
    assert "do not submit it again automatically" in message


@pytest.mark.asyncio
async def test_suno_rejects_bad_ids_and_missing_credentials_without_http(suno_http):
    bad_id = await suno_http.call_tool(
        "get_generation",
        {"generation_id": "../other"},
        "sk_live_test",
    )
    missing_key = await suno_http.call_tool("get_usage", {}, "  ")

    assert bad_id["isError"] is True
    assert missing_key["isError"] is True
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_suno_health_uses_only_read_only_usage_endpoint(monkeypatch):
    from packages.core.services import integration_health

    seen: dict = {}

    class Response:
        status_code = 200
        text = ""

    async def fake_get(url, **kwargs):
        seen.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(integration_health, "_http_get", fake_get)

    result = await integration_health.test_suno({"api_key": "sk_live_test"})

    assert result["ok"] is True
    assert seen == {
        "url": "https://api.suno.com/v0/account/usage",
        "headers": {"Authorization": "Bearer sk_live_test"},
        "timeout": 10,
    }


@pytest.mark.asyncio
async def test_suno_catalog_key_probe_is_read_only(monkeypatch):
    import apps.api.routers.auth as auth_router

    seen: dict = {}

    class Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"credits": 10}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, headers=None):
            seen.update({"url": url, "headers": headers})
            return Response()

    monkeypatch.setattr(auth_router.httpx, "AsyncClient", Client)

    await auth_router._probe_media_role_key(
        "audio",
        "sk_live_test",
        "suno/music",
        "",
    )

    assert seen == {
        "url": "https://api.suno.com/v0/account/usage",
        "headers": {"Authorization": "Bearer sk_live_test"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("byok", [True, False])
async def test_generate_audio_routes_suno_catalog_with_explicit_billing_mode(
    monkeypatch,
    byok,
):
    from packages.core.ai.tools import extended_tools

    captured: dict = {}

    async def resolve_model(_user_id, _entity_id, *, purpose):
        assert purpose == "music"
        return "suno/music", "audio"

    async def resolve_credentials(_user_id, _entity_id, *, role):
        assert role == "audio"
        return ("sk_live_byok", "https://api.suno.com/v0", True) if byok else ("", "", False)

    async def platform_credentials(provider):
        assert provider == "suno"
        return "sk_live_platform", "https://api.suno.com/v0"

    async def credit_gate(entity_id, *, source):
        captured["credit_gate"] = (entity_id, source)

    async def start_job(**kwargs):
        captured.update(kwargs)
        return json.dumps({
            "kind": "audio",
            "status": "pending",
            "provider": "suno",
            "job_id": "job_local",
        })

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", resolve_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", resolve_credentials)
    monkeypatch.setattr(
        extended_tools,
        "_platform_native_media_credential_async",
        platform_credentials,
    )
    monkeypatch.setattr(extended_tools, "runtime_assert_credit_available", credit_gate)
    monkeypatch.setattr(extended_tools, "_start_suno_audio_job", start_job)

    result = json.loads(await extended_tools._generate_audio_handler(
        entity_id="entity-test",
        user_id="user-test",
        prompt="warm instrumental product-launch theme",
        purpose="music",
        instrumental=True,
    ))

    assert result["status"] == "pending"
    assert captured["model"] == "suno/music"
    assert captured["is_byok"] is byok
    assert captured["instrumental"] is True
    if byok:
        assert "credit_gate" not in captured
    else:
        assert captured["credit_gate"][0] == "entity-test"


@pytest.mark.asyncio
async def test_ambiguous_suno_submit_never_releases_the_reservation(monkeypatch):
    from packages.core.tasks import media_tasks

    finalized: dict = {}

    async def marked(_job_id):
        return True

    async def approved(_job_id):
        return True

    async def ambiguous(_job_id):
        raise media_tasks.SunoSubmissionReconciliationRequired("manual reconciliation")

    async def finalize(job_id, error, *, release_reservation):
        finalized.update({
            "job_id": job_id,
            "error": error,
            "release_reservation": release_reservation,
        })

    monkeypatch.setattr(media_tasks, "_mark_suno_audio_job_processing", marked)
    monkeypatch.setattr(
        "packages.core.services.room_generated_files.room_media_job_approved_for_execution",
        approved,
    )
    monkeypatch.setattr(media_tasks, "_call_suno_audio_api", ambiguous)
    monkeypatch.setattr(media_tasks, "_finalize_suno_audio_failure", finalize)

    await media_tasks.process_suno_audio_job("job-ambiguous")

    assert finalized == {
        "job_id": "job-ambiguous",
        "error": "manual reconciliation",
        "release_reservation": False,
    }
