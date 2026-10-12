from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "model", "expected_max"),
    [
        ("video", "bytedance/seedance-2.5", 30),
        ("video", "bytedance/seedance-2.0", 15),
        ("image", "google/gemini-3.1-flash-image-preview", 16),
    ],
)
async def test_search_exposes_selected_model_contract(monkeypatch, kind, model, expected_max):
    from packages.core.ai.tools import extended_tools
    from packages.core.ai.tools.generate_file.tool import _generate_file_handler

    async def actor(user_id, entity_id, task_id):
        assert (user_id, entity_id) == ("user-1", "entity-1")
        return user_id

    async def selected(*_args, **_kwargs):
        return model

    monkeypatch.setattr(extended_tools, "_resolve_media_task_user_id", actor)
    monkeypatch.setattr(
        extended_tools,
        "_resolve_user_image_model" if kind == "image" else "_resolve_user_video_model",
        selected,
    )
    result = json.loads(await _generate_file_handler(
        entity_id="entity-1", user_id="user-1", kind="search",
        params={"media_kind": kind},
    ))["selected_model"]

    assert result["model"] == model
    assert result["expected_model"] == model
    if kind == "image":
        assert result["generation_schema"]["reference_urls"]["maxItems"] == expected_max
        assert result["generation_schema"]["input_fidelity"]["provider_parameter"] is False
    else:
        assert result["generation_schema"]["duration"]["text_to_video"]["maximum"] == expected_max
        assert result["generation_schema"]["reference_urls"]["maxItems"] == (30 if expected_max == 30 else 9)


@pytest.mark.asyncio
async def test_image_generation_rejects_changed_model_before_credentials(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def actor(*_args):
        return "user-1"

    async def selected(*_args):
        return "openai/gpt-image-2"

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("credentials must not be read after model change")

    monkeypatch.setattr(extended_tools, "_resolve_media_task_user_id", actor)
    async def video_model(*_args):
        return "bytedance/seedance-2.0"

    monkeypatch.setattr(extended_tools, "_resolve_user_image_model", selected)
    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", video_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", unexpected)

    result = json.loads(await extended_tools._generate_image_handler(
        entity_id="entity-1", user_id="user-1", prompt="A forest",
        expected_model="google/gemini-3.1-flash-image-preview",
    ))
    assert result["error_code"] == "image_model_changed_reinspect_required"
    assert result["model"] == "openai/gpt-image-2"
    # The refusal says how to continue, for callers that cannot re-inspect.
    assert "Retry with expected_model=openai/gpt-image-2" in result["error"]


@pytest.mark.asyncio
async def test_image_generation_ignores_an_expectation_that_is_not_a_selectable_image_model(monkeypatch):
    from packages.core.ai.tools import extended_tools

    async def actor(*_args):
        return "user-1"

    async def image_model(*_args):
        return "openai/gpt-image-2"

    async def video_model(*_args):
        return "bytedance/seedance-2.0"

    class PassedModelCheck(Exception):
        pass

    async def credentials(*_args, **_kwargs):
        raise PassedModelCheck

    monkeypatch.setattr(extended_tools, "_resolve_media_task_user_id", actor)
    monkeypatch.setattr(extended_tools, "_resolve_user_image_model", image_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_video_model", video_model)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", credentials)

    # A caller that copied the video plan's model into an image call, or
    # guessed a model name, did not see a changed image selection.
    for guess in ("bytedance/seedance-2.0", "gpt-image-1", "openai/gpt-image-1"):
        with pytest.raises(PassedModelCheck):
            await extended_tools._generate_image_handler(
                entity_id="entity-1", user_id="user-1", prompt="An anchor frame",
                expected_model=guess,
            )


def test_generate_file_schema_prompts_inspection_and_passes_expected_model():
    from packages.core.ai.runtime.approval_classifier import _runtime_tool_is_explicitly_read_only
    from packages.core.ai.tools.generate_file.common import _merge_params
    from packages.core.ai.tools.generate_file.schema import GENERATE_FILE_SCHEMA

    schema = GENERATE_FILE_SCHEMA["function"]
    assert "kind=search" in schema["description"]
    assert schema["parameters"]["properties"]["params"]["properties"]["media_kind"]["enum"] == ["image", "video", "audio"]
    assert _merge_params({"expected_model": "bytedance/seedance-2.5"})["expected_model"] == "bytedance/seedance-2.5"
    assert _runtime_tool_is_explicitly_read_only(
        "generate_file", {"kind": "search", "params": {"media_kind": "video"}}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "video", "audio"])
async def test_generate_file_forwards_expected_model_to_media_boundary(monkeypatch, kind):
    from packages.core.ai.tools.generate_file import audio as audio_kind
    from packages.core.ai.tools.generate_file import image as image_kind
    from packages.core.ai.tools.generate_file import video as video_kind
    from packages.core.ai.tools.generate_file.tool import _generate_file_handler

    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return json.dumps({"status": "completed"})

    target = {"image": image_kind, "video": video_kind, "audio": audio_kind}[kind]
    monkeypatch.setattr(
        target,
        {"image": "runtime_generate_image_media", "video": "runtime_generate_video_media", "audio": "runtime_generate_audio_media"}[kind],
        capture,
    )
    await _generate_file_handler(
        entity_id="entity-1", user_id="user-1", kind=kind,
        prompt="A forest", expected_model="selected/model",
    )
    assert captured["params"]["expected_model"] == "selected/model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("purpose", "model", "role"),
    [
        ("speech", "google/gemini-3.1-flash-tts-preview", "voice"),
        ("music", "google/lyria-3-clip-preview", "audio"),
        ("sfx", "openai/gpt-audio-mini", "sfx"),
    ],
)
async def test_audio_inspection_uses_purpose_selected_model(monkeypatch, purpose, model, role):
    from packages.core.ai.tools import extended_tools
    from packages.core.ai.tools.generate_file.tool import _generate_file_handler
    from packages.core.contracts.audio_generation import AudioGenerationRole

    async def selected(_user_id, _entity_id, *, purpose):
        assert purpose.value in {"speech", "music", "sfx"}
        return model, AudioGenerationRole(role)

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", selected)
    result = json.loads(await _generate_file_handler(
        entity_id="entity-1", user_id="user-1", kind="search",
        params={"media_kind": "audio", "purpose": purpose},
    ))["selected_model"]
    assert (result["purpose"], result["model"], result["role"]) == (purpose, model, role)
    assert result["expected_model"] == model
    if purpose == "music":
        assert result["generation_schema"]["duration_seconds"]["fixed_output_seconds"] == 30
    if purpose == "speech":
        assert result["generation_schema"]["voice"]["default"] == "Zephyr"


@pytest.mark.asyncio
async def test_suno_inspection_exposes_custom_mode_without_false_format_control(monkeypatch):
    from packages.core.ai.tools import extended_tools
    from packages.core.ai.tools.generate_file.model_contract import selected_media_model_contract
    from packages.core.contracts.audio_generation import AudioGenerationRole

    async def selected(*_args, **_kwargs):
        return "suno/music", AudioGenerationRole.AUDIO

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", selected)
    result = await selected_media_model_contract(
        kind="audio", entity_id="entity-1", user_id="user-1", purpose="music",
    )
    schema = result["generation_schema"]
    assert "lyrics" in schema and "instrumental" in schema
    assert "response_format" not in schema
    assert schema["duration_seconds"]["supported_as_provider_parameter"] is False


@pytest.mark.asyncio
async def test_audio_generation_rejects_changed_model_before_credentials(monkeypatch):
    from packages.core.ai.tools import extended_tools
    from packages.core.contracts.audio_generation import AudioGenerationRole

    async def selected(*_args, **_kwargs):
        return "suno/music", AudioGenerationRole.AUDIO

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("credentials must not be read after model change")

    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", selected)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", unexpected)
    result = json.loads(await extended_tools._generate_audio_handler(
        entity_id="entity-1", user_id="user-1", prompt="Warm jazz",
        purpose="music", expected_model="google/lyria-3-clip-preview",
    ))
    assert result["code"] == "audio_model_changed_reinspect_required"
    assert result["model"] == "suno/music"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("purpose", "role"),
    [("narration", "voice"), ("music", "audio"), ("ambience", "sfx")],
)
async def test_audio_purpose_resolves_the_selected_catalog_role(monkeypatch, purpose, role):
    from packages.core.ai.tools import extended_tools
    from packages.core.contracts.audio_generation import AudioGenerationPurpose
    from packages.core.services import model_resolver

    async def resolve(selected_role, **_kwargs):
        assert selected_role == role
        return f"provider/{selected_role}-model"

    monkeypatch.setattr(model_resolver, "resolve_model_for_user", resolve)
    model, actual_role = await extended_tools._resolve_user_audio_model(
        "user-1", "entity-1", purpose=AudioGenerationPurpose(purpose),
    )
    assert (model, actual_role.value) == (f"provider/{role}-model", role)


@pytest.mark.asyncio
async def test_narration_inspection_uses_existing_pinned_model(monkeypatch):
    from packages.core.ai.tools import extended_tools
    from packages.core.ai.tools.generate_file import model_contract
    from packages.core.contracts.audio_generation import AudioGenerationRole

    async def selected(*_args, **_kwargs):
        return "google/gemini-3.1-flash-tts-preview", AudioGenerationRole.VOICE

    async def profile(**_kwargs):
        return {"version": 1, "provider": "openai", "model": "openai/tts-1-hd", "voice": "alloy", "voice_instructions": ""}

    async def actor(*_args):
        return "user-1"

    monkeypatch.setattr(extended_tools, "_resolve_media_task_user_id", actor)
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", selected)
    monkeypatch.setattr(model_contract, "_existing_narration_profile", profile)
    result = await model_contract.selected_media_model_contract(
        kind="audio", entity_id="entity-1", user_id="user-1",
        purpose="narration", task_id="task-1",
        narration_voice_mode="random_per_task",
    )
    assert result["model"] == result["expected_model"] == "openai/tts-1-hd"
    assert result["account_selected_model"] == "google/gemini-3.1-flash-tts-preview"
    assert result["model_source"] == "pinned_narrator_profile"


@pytest.mark.asyncio
async def test_audio_generation_checks_pinned_narration_model(monkeypatch):
    from packages.core.ai.tools import extended_tools
    from packages.core.contracts.audio_generation import AudioGenerationRole

    async def actor(*_args):
        return "user-1"

    async def selected(*_args, **_kwargs):
        return "google/gemini-3.1-flash-tts-preview", AudioGenerationRole.VOICE

    async def profile(**_kwargs):
        return {"version": 1, "provider": "openai", "model": "openai/tts-1-hd", "voice": "alloy", "voice_instructions": ""}

    async def credentials(*_args, **_kwargs):
        return "", "", False

    monkeypatch.setattr(extended_tools, "_resolve_media_task_user_id", actor)
    monkeypatch.setattr(extended_tools, "_resolve_user_audio_model", selected)
    monkeypatch.setattr(extended_tools, "_resolve_task_narrator_profile", profile)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", credentials)
    result = json.loads(await extended_tools._generate_audio_handler(
        entity_id="entity-1", user_id="user-1", task_id="task-1",
        prompt="Hello", purpose="narration", narration_voice_mode="random_per_task",
        expected_model="openai/tts-1-hd",
    ))
    assert result["model"] == "openai/tts-1-hd"
    assert result["code"] != "audio_model_changed_reinspect_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "purpose", "initial", "changed", "error_field"),
    [
        ("image", None, "google/gemini-3.1-flash-image-preview", "openai/gpt-image-2", "error_code"),
        ("video", None, "bytedance/seedance-2.0", "bytedance/seedance-2.5", "error_code"),
        ("audio", "music", "google/lyria-3-clip-preview", "suno/music", "code"),
    ],
)
async def test_search_then_generate_rechecks_model_before_credentials(
    monkeypatch, kind, purpose, initial, changed, error_field,
):
    """Exercise the composite tool and real media dispatch without provider I/O."""
    from packages.core.ai.tools import extended_tools
    from packages.core.ai.tools.generate_file.tool import _generate_file_handler
    from packages.core.contracts.audio_generation import AudioGenerationRole

    selected = {"model": initial}

    async def actor(*_args):
        return "user-1"

    async def model(*_args, **_kwargs):
        return selected["model"]

    async def audio_model(*_args, **_kwargs):
        return selected["model"], AudioGenerationRole.AUDIO

    class CredentialsReached(Exception):
        pass

    async def credentials(*_args, **_kwargs):
        raise CredentialsReached

    monkeypatch.setattr(extended_tools, "_resolve_media_task_user_id", actor)
    monkeypatch.setattr(extended_tools, "_resolve_user_media_credentials", credentials)
    monkeypatch.setattr(
        extended_tools,
        {"image": "_resolve_user_image_model", "video": "_resolve_user_video_model", "audio": "_resolve_user_audio_model"}[kind],
        audio_model if kind == "audio" else model,
    )
    search_params = {"media_kind": kind}
    if purpose:
        search_params["purpose"] = purpose
    inspected = json.loads(await _generate_file_handler(
        entity_id="entity-1", user_id="user-1", kind="search", params=search_params,
    ))["selected_model"]
    assert inspected["model"] == initial

    selected["model"] = changed
    generation_params = {"expected_model": inspected["expected_model"]}
    if purpose:
        generation_params["purpose"] = purpose
    stale = json.loads(await _generate_file_handler(
        entity_id="entity-1", user_id="user-1", kind=kind,
        prompt="A short sample", params=generation_params,
    ))
    assert "model_changed" in stale[error_field]

    inspected_again = json.loads(await _generate_file_handler(
        entity_id="entity-1", user_id="user-1", kind="search", params=search_params,
    ))["selected_model"]
    assert inspected_again["model"] == changed
    generation_params["expected_model"] = inspected_again["expected_model"]
    with pytest.raises(CredentialsReached):
        await _generate_file_handler(
            entity_id="entity-1", user_id="user-1", kind=kind,
            prompt="A short sample", params=generation_params,
        )
