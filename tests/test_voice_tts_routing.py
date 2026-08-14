import pytest


@pytest.mark.asyncio
async def test_resolve_channel_tts_credentials_prefers_byok_then_managed_route(monkeypatch):
    from packages.core.services.voice import tts

    async def metadata(_entity_id):
        return {"llm_api_key": "sk-byok-voice", "llm_base_url": "https://byok.example/v1"}

    async def route(*_args, **_kwargs):
        raise AssertionError("managed route must not be queried for BYOK")

    monkeypatch.setattr(tts, "_resolve_voice_metadata", metadata, raising=False)
    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_official_model_route",
        route,
    )

    resolved = await tts.resolve_channel_tts_credentials("entity", "openai/tts-1")

    assert resolved == ("sk-byok-voice", "https://byok.example/v1", True)
