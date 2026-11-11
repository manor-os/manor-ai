"""A provider refusing the description goes back to the person, not to stock media.

Production, 2026-10-30: Google Lyria answered "Input blocked: The prompt could
not be processed" five times for one customer's song. Each attempt retried the
same description, and the failure read like any provider error; nothing told
the person to describe the song differently.
"""
from __future__ import annotations

import json

import pytest

from packages.core.services import music_library
from packages.core.services.media_prompt_declined import (
    PROMPT_DECLINED,
    mark_declined_result,
    provider_declined_prompt,
)

LYRIA = "Google Lyria music generation failed (400): Input blocked: The prompt could not be processed."


@pytest.mark.parametrize("text", [
    LYRIA,
    "OpenAI image generation failed (400): Your request was rejected as a result of our safety system. content_policy_violation",
    "Image generation failed (400): This request violates our usage policies.",
    "Google image generation failed (400): PROHIBITED_CONTENT",
])
def test_providers_refusing_the_content_are_recognised(text):
    assert provider_declined_prompt(text)


@pytest.mark.parametrize("text", [
    "OpenRouter audio generation failed (429): rate limit exceeded",
    "Google Lyria music generation failed (403): API key not valid",
    "Request blocked by the network firewall",
    "You've reached the Personal plan limit of 1024 MB of knowledge base storage.",
    "",
])
def test_quota_auth_network_and_storage_failures_are_not_the_description(text):
    assert not provider_declined_prompt(text)


@pytest.mark.asyncio
async def test_a_refused_song_description_asks_for_a_new_one_and_never_uses_stock_music(tmp_path, monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def resolve_model(_user_id, _entity_id, *, purpose):
        return "google/lyria-3-clip-preview", "audio"

    async def credentials(_user_id, _entity_id, *, role):
        return "AIza-user-google-key", "", True

    async def refused(**_kwargs):
        raise RuntimeError(LYRIA)

    async def no_library(*_args, **_kwargs):
        raise AssertionError("a refused description must not be replaced by library music")

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", resolve_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", credentials)
    monkeypatch.setattr(extended_tools, "_google_music_bytes", refused)
    monkeypatch.setattr(extended_tools, "_music_from_library", no_library)

    result = json.loads(await extended_tools._generate_audio_handler(
        entity_id="entity", user_id="user", prompt="A song exactly like a famous artist's hit", purpose="music",
    ))
    assert result["status"] == "error" and result["code"] == PROMPT_DECLINED
    assert "Input blocked" in result["error"] and "Describe it differently" in result["error"]


@pytest.mark.asyncio
async def test_other_music_failures_still_fall_back_to_the_library(tmp_path, monkeypatch):
    from packages.core.ai.tools import extended_tools

    monkeypatch.setattr(music_library, "LIBRARY_ROOT", tmp_path / "empty")

    async def live_fails(**_kwargs):
        return json.dumps({"status": "error", "code": "audio_generation_failed", "error": "provider timed out"})

    monkeypatch.setattr(extended_tools, "_generate_live_audio", live_fails)
    result = json.loads(await extended_tools._generate_audio_handler(
        entity_id="e", user_id="u", prompt="calm", purpose="music"))
    assert result["status"] == "skipped" and result["skip_code"] == "audio_generation_failed"


def test_pictures_and_videos_say_the_same_and_other_errors_pass_through():
    refused = json.dumps({"error": "OpenAI image generation failed (400): content_policy_violation"})
    typed = json.loads(mark_declined_result(refused, "image"))
    assert typed["code"] == PROMPT_DECLINED and typed["retryable"] is False
    assert typed["error"].startswith("The picture service declined this description")
    other = json.dumps({"error": "Image generation failed (500): upstream error"})
    assert mark_declined_result(other, "video") == other
    assert mark_declined_result("not json", "image") == "not json"


def test_the_registered_picture_and_video_tools_carry_the_refusal_type():
    from packages.core.ai.tools import extended_tools

    tools = {schema["function"]["name"]: handler for schema, handler in extended_tools.get_tools()}
    assert tools["generate_image"].__wrapped__ is extended_tools._generate_image_handler
    assert tools["generate_video"].__wrapped__ is extended_tools._generate_video_handler
