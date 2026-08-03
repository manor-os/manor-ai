"""Unit tests for multi-provider BYOK routing.

Tests: detect_provider_from_key, detect_provider_from_model,
resolve_provider_base_url, normalize_model_for_provider.
"""

import pytest

from packages.core.ai.llm_client import (
    _validate_llm_key_model_compatibility,
    _resolve_vision_model_if_needed,
    _route_metadata_for_resolved_model,
    detect_provider_from_key,
    detect_provider_from_model,
    normalize_model_for_provider,
    resolve_llm_routing_for_model,
    resolve_provider_base_url,
)
from packages.core.services.model_provider_handlers import provider_from_base_url
from packages.core.services.platform_model_provider_keys import OfficialProviderCredential
from packages.core.services.voice.whisper import WhisperError, transcribe_blob


# ── detect_provider_from_key ──────────────────────────────────────────


class TestDetectProviderFromKey:
    def test_openrouter(self):
        assert detect_provider_from_key("sk-or-v1-abc123") == "openrouter"

    def test_openai(self):
        assert detect_provider_from_key("sk-abc123") == "openai"

    def test_openai_project_key(self):
        assert detect_provider_from_key("sk-proj-abc123") == "openai"

    def test_groq(self):
        assert detect_provider_from_key("gsk_abc123") == "groq"

    def test_google(self):
        assert detect_provider_from_key("AIzaSyABC123") == "google"

    def test_anthropic(self):
        assert detect_provider_from_key("sk-ant-abc123") == "anthropic"

    def test_empty(self):
        assert detect_provider_from_key("") is None

    def test_unknown(self):
        assert detect_provider_from_key("random-key-format") is None


# ── detect_provider_from_model ────────────────────────────────────────


class TestDetectProviderFromModel:
    def test_openai_model(self):
        assert detect_provider_from_model("openai/gpt-4.1") == "openai"

    def test_google_model(self):
        assert detect_provider_from_model("google/gemini-2.5-pro") == "google"

    def test_groq_model(self):
        assert detect_provider_from_model("groq/whisper-large-v3") == "groq"

    def test_mistral_model(self):
        assert detect_provider_from_model("mistral/mistral-large") == "mistral"

    def test_zyphra_model(self):
        assert detect_provider_from_model("zyphra/zonos-v0.1-hybrid") == "zyphra"

    def test_anthropic_model(self):
        assert detect_provider_from_model("anthropic/claude-sonnet-4.6") == "anthropic"

    def test_bytedance_not_in_endpoints(self):
        """ByteDance Seed models must go through OpenRouter."""
        assert detect_provider_from_model("bytedance-seed/seed-2.0-lite") is None

    def test_no_prefix(self):
        assert detect_provider_from_model("claude-sonnet-4.6") is None

    def test_empty(self):
        assert detect_provider_from_model("") is None


# ── resolve_provider_base_url ─────────────────────────────────────────


class TestResolveProviderBaseUrl:
    def test_user_override_wins(self):
        url = resolve_provider_base_url("openai/gpt-4.1", "sk-abc", "https://custom.com/v1")
        assert url == "https://custom.com/v1"

    def test_openai_key(self):
        url = resolve_provider_base_url("openai/gpt-4.1", "sk-abc123", None)
        assert url == "https://api.openai.com/v1"

    def test_openrouter_key(self):
        url = resolve_provider_base_url("anthropic/claude-sonnet-4.6", "sk-or-abc", None)
        assert url == "https://openrouter.ai/api/v1"

    def test_google_key(self):
        url = resolve_provider_base_url("google/gemini-2.5-pro", "AIzaSyABC", None)
        assert url == "https://generativelanguage.googleapis.com/v1beta/openai"

    def test_groq_key(self):
        url = resolve_provider_base_url("groq/whisper-large-v3", "gsk_abc", None)
        assert url == "https://api.groq.com/openai/v1"

    def test_anthropic_key(self):
        url = resolve_provider_base_url("anthropic/claude-sonnet-4.6", "sk-ant-abc", None)
        assert url == "https://api.anthropic.com/v1"

    def test_unknown_key_and_model(self):
        url = resolve_provider_base_url("some-model", "random-key", None)
        assert url == "https://openrouter.ai/api/v1"

    def test_key_detection_beats_non_generic_model_detection(self):
        """If key says OpenAI but model says Google, key wins."""
        url = resolve_provider_base_url("google/gemini-2.5-pro", "sk-abc123", None)
        assert url == "https://api.openai.com/v1"

    def test_generic_sk_uses_deepseek_model_provider(self):
        url = resolve_provider_base_url("deepseek/deepseek-v4-pro", "sk-abc123", None)
        assert url == "https://api.deepseek.com/v1"

    def test_generic_sk_uses_qwen_model_provider(self):
        url = resolve_provider_base_url("qwen/qwen3.6-plus", "sk-abc123", None)
        assert url == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    @pytest.mark.parametrize("model", ["moonshotai/kimi-k2.6", "moonshotai/kimi-k3"])
    def test_generic_sk_uses_moonshot_model_provider(self, model):
        url = resolve_provider_base_url(model, "sk-abc123", None)
        assert url == "https://api.moonshot.ai/v1"

    @pytest.mark.parametrize(
        "base_url",
        ["https://api.moonshot.ai/v1", "https://api.moonshot.cn/v1"],
    )
    def test_moonshot_official_regions_are_recognized(self, base_url):
        assert provider_from_base_url(base_url) == "moonshotai"
        _validate_llm_key_model_compatibility(
            "sk-" + "m" * 32,
            base_url,
            "moonshotai/kimi-k3",
        )

    def test_unknown_zyphra_key_uses_zyphra_model_provider(self):
        url = resolve_provider_base_url("zyphra/zonos-v0.1-hybrid", "zyphra-test-key", None)
        assert url == "https://api.zyphra.com/v1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolved_model", "api_key", "expected_url", "expected_wire_model"),
    [
        (
            "openai/whisper-1",
            "sk-openai-user",
            "https://api.openai.com/v1/audio/transcriptions",
            "whisper-1",
        ),
        (
            "groq/whisper-large-v3",
            "gsk_groq_user",
            "https://api.groq.com/openai/v1/audio/transcriptions",
            "whisper-large-v3",
        ),
    ],
)
async def test_native_stt_byok_routes_request_verbose_segment_timestamps(
    monkeypatch,
    resolved_model,
    api_key,
    expected_url,
    expected_wire_model,
):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "text": "Measured narration.",
                "duration": 2.5,
                "segments": [
                    {"start": 0.2, "end": 2.3, "text": "Measured narration."}
                ],
                "words": [
                    {"start": 0.2, "end": 1.1, "word": "Measured"},
                    {"start": 1.2, "end": 2.3, "word": "narration."},
                ],
            }

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr(
        "packages.core.services.voice.whisper.httpx.AsyncClient",
        FakeClient,
    )

    result = await transcribe_blob(
        b"RIFF-audio",
        mime="audio/wav",
        filename="narration.wav",
        user_api_key=api_key,
        resolved_model=resolved_model,
        require_timestamps=True,
    )

    assert captured["url"] == expected_url
    assert captured["data"] == {
        "model": expected_wire_model,
        "response_format": "verbose_json",
        "timestamp_granularities[]": ["word", "segment"],
    }
    assert result.segments == [
        {"start": 0.2, "end": 2.3, "text": "Measured narration."}
    ]
    assert result.words == [
        {"start": 0.2, "end": 1.1, "text": "Measured"},
        {"start": 1.2, "end": 2.3, "text": "narration."},
    ]


@pytest.mark.asyncio
async def test_native_stt_ordinary_transcription_keeps_legacy_http_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"text": "Ordinary transcript.", "duration": 1.5}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr(
        "packages.core.services.voice.whisper.httpx.AsyncClient",
        FakeClient,
    )

    result = await transcribe_blob(
        b"RIFF-audio",
        mime="audio/wav",
        filename="attachment.wav",
        user_api_key="sk-openai-user",
        resolved_model="openai/whisper-1",
        require_timestamps=False,
    )

    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert captured["data"] == {
        "model": "whisper-1",
        "response_format": "verbose_json",
    }
    assert result.text == "Ordinary transcript."
    assert result.segments is None
    assert result.words is None


@pytest.mark.asyncio
async def test_native_stt_byok_uses_only_custom_openai_compatible_base_url(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "text": "Measured narration.",
                "duration": 1.0,
                "segments": [
                    {"start": 0.1, "end": 0.9, "text": "Measured narration."}
                ],
            }

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr(
        "packages.core.services.voice.whisper.httpx.AsyncClient",
        FakeClient,
    )

    await transcribe_blob(
        b"RIFF-audio",
        mime="audio/wav",
        user_api_key="sk-custom-endpoint-key",
        user_base_url="https://stt.example.test/v1/",
        resolved_model="openai/whisper-1",
        require_timestamps=True,
    )

    assert captured["url"] == "https://stt.example.test/v1/audio/transcriptions"
    assert captured["headers"]["Authorization"] == "Bearer sk-custom-endpoint-key"
    assert captured["data"]["model"] == "whisper-1"


@pytest.mark.asyncio
async def test_native_stt_byok_rejects_known_base_url_provider_mismatch(monkeypatch):
    class UnexpectedClient:
        def __init__(self, **_kwargs):
            raise AssertionError("provider mismatch must fail before network")

    monkeypatch.setattr(
        "packages.core.services.voice.whisper.httpx.AsyncClient",
        UnexpectedClient,
    )

    with pytest.raises(WhisperError, match="base URL routes to openai"):
        await transcribe_blob(
            b"RIFF-audio",
            mime="audio/wav",
            user_api_key="gsk_groq_user",
            user_base_url="https://api.openai.com/v1",
            resolved_model="groq/whisper-large-v3",
            require_timestamps=True,
        )


@pytest.mark.asyncio
async def test_chat_audio_stt_rejects_timestamp_required_alignment_before_network(monkeypatch):
    class UnexpectedClient:
        def __init__(self, **_kwargs):
            raise AssertionError("timestamp-less chat audio must be blocked before network")

    monkeypatch.setattr(
        "packages.core.services.voice.whisper.httpx.AsyncClient",
        UnexpectedClient,
    )

    with pytest.raises(WhisperError, match="timestamp-capable STT"):
        await transcribe_blob(
            b"RIFF-audio",
            mime="audio/wav",
            user_api_key="sk-or-user",
            resolved_model="openai/gpt-4o-audio-preview",
            require_timestamps=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolved_model",
    ["openai/whisper-1", "openai/gpt-4o-audio-preview"],
)
async def test_platform_openrouter_fallback_returns_reference_aligned_timestamps(
    monkeypatch,
    resolved_model,
):
    captured = {}

    for key in (
        "WHISPER_API_KEY",
        "WHISPER_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "GROQ_API_KEY",
        "OPENROUTER_AUDIO_TRANSCRIPTION_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-platform")

    async def no_official_credential(provider, **_kwargs):
        if resolved_model == "openai/gpt-4o-audio-preview" and provider == "openrouter":
            return OfficialProviderCredential(
                provider="openrouter",
                api_key="sk-or-official",
                base_url="https://openrouter.ai/api/v1",
                source="env",
            )
        return None

    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_gateway_credential",
        no_official_credential,
    )

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"segments":['
                                '{"start":0.2,"end":1.8,"text":"First sentence."},'
                                '{"start":2.0,"end":3.9,"text":"Second sentence."}'
                                "]}"
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr(
        "packages.core.services.voice.whisper.httpx.AsyncClient",
        FakeClient,
    )

    result = await transcribe_blob(
        b"RIFF-audio",
        mime="audio/wav",
        filename="narration.wav",
        resolved_model=resolved_model,
        require_timestamps=True,
        reference_transcript="First sentence. Second sentence.",
    )

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["json"]["model"] == "google/gemini-3.1-flash-lite"
    assert "First sentence. Second sentence." in captured["json"]["messages"][0]["content"][1]["text"]
    assert result.model == "google/gemini-3.1-flash-lite"
    assert result.text == "First sentence. Second sentence."
    assert result.duration_seconds == 3.9
    assert result.segments == [
        {
            "start": 0.2,
            "end": 1.8,
            "text": "First sentence.",
            "timing_source": "measured_openrouter_audio_segments",
        },
        {
            "start": 2.0,
            "end": 3.9,
            "text": "Second sentence.",
            "timing_source": "measured_openrouter_audio_segments",
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolved_model", "env", "expected_url", "expected_wire_model"),
    [
        (
            "openai/whisper-1",
            {
                "WHISPER_API_KEY": "sk-platform-whisper",
                "WHISPER_BASE_URL": "https://api.openai.com/v1",
            },
            "https://api.openai.com/v1/audio/transcriptions",
            "whisper-1",
        ),
        (
            "groq/whisper-large-v3",
            {"GROQ_API_KEY": "gsk_platform_groq"},
            "https://api.groq.com/openai/v1/audio/transcriptions",
            "whisper-large-v3",
        ),
    ],
)
async def test_native_stt_platform_env_routes_without_byok(
    monkeypatch,
    resolved_model,
    env,
    expected_url,
    expected_wire_model,
):
    captured = {}

    for key in (
        "WHISPER_API_KEY",
        "WHISPER_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    async def no_official_credential(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "packages.core.services.model_gateway.resolve_gateway_credential",
        no_official_credential,
    )

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "text": "Measured narration.",
                "duration": 1.0,
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": "Measured narration."}
                ],
            }

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr(
        "packages.core.services.voice.whisper.httpx.AsyncClient",
        FakeClient,
    )

    await transcribe_blob(
        b"RIFF-audio",
        mime="audio/wav",
        resolved_model=resolved_model,
        require_timestamps=True,
    )

    assert captured["url"] == expected_url
    assert captured["data"]["model"] == expected_wire_model


# ── normalize_model_for_provider ──────────────────────────────────────


class TestNormalizeModelForProvider:
    def test_openrouter_keeps_full_id(self):
        result = normalize_model_for_provider("anthropic/claude-sonnet-4.6", "https://openrouter.ai/api/v1")
        assert result == "anthropic/claude-sonnet-4.6"

    def test_vercel_gateway_keeps_full_id(self):
        result = normalize_model_for_provider(
            "anthropic/claude-sonnet-4.6",
            "https://ai-gateway.vercel.sh/v1",
        )
        assert result == "anthropic/claude-sonnet-4.6"
        assert provider_from_base_url("https://ai-gateway.vercel.sh/v1") == "vercel"

    def test_direct_openai_strips_prefix(self):
        result = normalize_model_for_provider("openai/gpt-4.1", "https://api.openai.com/v1")
        assert result == "gpt-4.1"

    @pytest.mark.parametrize(
        "base_url",
        ["https://api.moonshot.ai/v1", "https://api.moonshot.cn/v1"],
    )
    def test_direct_kimi_strips_catalog_prefix(self, base_url):
        result = normalize_model_for_provider("moonshotai/kimi-k3", base_url)
        assert result == "kimi-k3"

    def test_direct_google_strips_prefix(self):
        result = normalize_model_for_provider(
            "google/gemini-2.5-pro", "https://generativelanguage.googleapis.com/v1beta/openai"
        )
        assert result == "gemini-2.5-pro"

    def test_direct_anthropic_strips_prefix_and_hyphenates_version(self):
        result = normalize_model_for_provider("anthropic/claude-sonnet-4.6", "https://api.anthropic.com/v1")
        assert result == "claude-sonnet-4-6"

    def test_no_prefix_unchanged(self):
        result = normalize_model_for_provider("gpt-4o", "https://api.openai.com/v1")
        assert result == "gpt-4o"

    def test_empty_model(self):
        result = normalize_model_for_provider("", "https://api.openai.com/v1")
        assert result == ""


# ── vision fallback routing metadata ──────────────────────────────────


class TestVisionFallbackRoutingMetadata:
    @staticmethod
    def _image_messages():
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                    }
                ],
            }
        ]

    @pytest.mark.parametrize(
        ("requested_model", "expected_model"),
        [
            ("gpt-5.6", "openai/gpt-5.6-sol"),
            ("openai/gpt-5.6-sol", "openai/gpt-5.6-sol"),
            ("gpt-5.6-terra", "openai/gpt-5.6-terra"),
            ("openai/gpt-5.6-luna", "openai/gpt-5.6-luna"),
            ("gpt-5.5", "openai/gpt-5.5"),
            ("openai/gpt-5.5", "openai/gpt-5.5"),
            ("gpt-5.5-pro", "openai/gpt-5.5-pro"),
            ("openai/gpt-5.5-pro", "openai/gpt-5.5-pro"),
        ],
    )
    def test_gpt55_image_input_keeps_canonical_openai_model(self, requested_model, expected_model):
        assert _resolve_vision_model_if_needed(requested_model, self._image_messages()) == expected_model

    def test_unknown_image_model_does_not_silently_switch_provider(self):
        assert (
            _resolve_vision_model_if_needed(
                "custom/acme-multimodal-preview",
                self._image_messages(),
            )
            == "custom/acme-multimodal-preview"
        )

    def test_model_input_modalities_are_centralized(self):
        from packages.core.constants import models

        assert models.model_input_modalities("openai/gpt-4") == frozenset({"text"})
        assert models.model_input_modalities("openai/gpt-5.6-sol") == frozenset({"text", "image"})
        assert models.model_input_modalities("openai/gpt-5.6-terra") == frozenset({"text", "image"})
        assert models.model_input_modalities("openai/gpt-5.6-luna") == frozenset({"text", "image"})
        assert models.model_input_modalities("openai/gpt-5.5") == frozenset({"text", "image"})
        assert models.model_input_modalities("openai/gpt-5.5-pro") == frozenset({"text", "image"})
        assert models.model_input_modalities("moonshotai/kimi-k3") == frozenset({"text", "image"})
        assert models.model_input_modalities("custom/unknown") is None

    def test_explicit_text_only_model_uses_configured_vision_fallback(self, monkeypatch):
        monkeypatch.setenv("LLM_VISION_MODEL", "openai/gpt-4o")

        assert (
            _resolve_vision_model_if_needed(
                "deepseek/deepseek-v4-pro",
                self._image_messages(),
            )
            == "openai/gpt-4o"
        )

    def test_gpt55_image_routing_keeps_openai_byok_metadata(self):
        metadata = {
            "llm_api_key": "sk-" + "o" * 32,
            "llm_base_url": "https://api.openai.com/v1",
            "trace_id": "keep-me",
        }

        resolved_model = _resolve_vision_model_if_needed("gpt-5.5", self._image_messages())
        routed = _route_metadata_for_resolved_model(metadata, resolved_model, resolved_model)

        assert routed == {**metadata, "_resolved_model": "openai/gpt-5.5"}

    def test_drops_incompatible_deepseek_byok_after_vision_model_switch(self):
        metadata = {
            "llm_api_key": "sk-" + "d" * 32,
            "llm_base_url": "https://api.deepseek.com/v1",
            "trace_id": "keep-me",
        }

        routed = _route_metadata_for_resolved_model(
            metadata,
            "deepseek/deepseek-v4-pro",
            "anthropic/claude-sonnet-4.6",
        )

        assert routed is not None
        assert "llm_api_key" not in routed
        assert "llm_base_url" not in routed
        assert routed["_resolved_model"] == "anthropic/claude-sonnet-4.6"
        assert routed["trace_id"] == "keep-me"

    def test_keeps_compatible_anthropic_byok_after_vision_model_switch(self):
        metadata = {"llm_api_key": "sk-ant-" + "a" * 32}

        routed = _route_metadata_for_resolved_model(
            metadata,
            "deepseek/deepseek-v4-pro",
            "anthropic/claude-sonnet-4.6",
        )

        assert routed is not None
        assert routed["llm_api_key"] == metadata["llm_api_key"]
        assert routed["_resolved_model"] == "anthropic/claude-sonnet-4.6"


# ── official provider routing ─────────────────────────────────────────


class TestOfficialProviderRouting:
    @pytest.mark.asyncio
    async def test_official_route_uses_vercel_gateway_first(self, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
        import packages.core.services.platform_model_provider_keys as provider_keys
        from packages.core.ai import llm_client

        async def fake_resolve(provider: str, *, reason: str = ""):
            assert provider == "vercel"
            return OfficialProviderCredential(
                provider="vercel",
                api_key="vck_official-provider-key-1234567890",
                base_url="https://ai-gateway.vercel.sh/v1",
                source="official",
                source_detail="test",
            )

        token = llm_client._is_byok_call.set(True)
        try:
            monkeypatch.setattr(provider_keys, "resolve_official_provider_credential", fake_resolve)
            routing = await resolve_llm_routing_for_model("anthropic/claude-sonnet-4.6")
        finally:
            llm_client._is_byok_call.reset(token)

        assert routing.api_key == "vck_official-provider-key-1234567890"
        assert routing.base_url == "https://ai-gateway.vercel.sh/v1"
        assert routing.provider == "vercel"
        assert routing.source == "official"
        assert llm_client._is_byok_call.get(False) is False

    @pytest.mark.asyncio
    async def test_native_byok_wins_over_official_provider_token(self, monkeypatch):
        import packages.core.services.platform_model_provider_keys as provider_keys
        from packages.core.ai import llm_client

        async def fail_resolve(*_args, **_kwargs):
            raise AssertionError("BYOK routing should not query official provider tokens")

        monkeypatch.setattr(provider_keys, "resolve_official_provider_credential", fail_resolve)
        token = llm_client._is_byok_call.set(False)
        try:
            routing = await resolve_llm_routing_for_model(
                "anthropic/claude-sonnet-4.6",
                {"llm_api_key": "sk-ant-user-byok-key-1234567890"},
            )
            assert llm_client._is_byok_call.get(False) is True
        finally:
            llm_client._is_byok_call.reset(token)

        assert routing.api_key == "sk-ant-user-byok-key-1234567890"
        assert routing.base_url == "https://api.anthropic.com/v1"
        assert routing.source == "byok"

    @pytest.mark.asyncio
    async def test_openrouter_fallback_when_vercel_gateway_missing(self, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
        import packages.core.services.platform_model_provider_keys as provider_keys
        from packages.core.ai import llm_client

        async def fake_resolve(provider: str, *, reason: str = ""):
            if provider == "vercel":
                return None
            assert provider == "openrouter"
            return OfficialProviderCredential(
                provider="openrouter",
                api_key="sk-or-official-fallback-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                source="official",
                source_detail="test",
            )

        token = llm_client._is_byok_call.set(True)
        try:
            monkeypatch.setattr(provider_keys, "resolve_official_provider_credential", fake_resolve)
            routing = await resolve_llm_routing_for_model("anthropic/claude-sonnet-4.6")
        finally:
            llm_client._is_byok_call.reset(token)

        assert routing.api_key == "sk-or-official-fallback-key-1234567890"
        assert routing.base_url == "https://openrouter.ai/api/v1"
        assert routing.provider == "openrouter"
        assert llm_client._is_byok_call.get(False) is False

    @pytest.mark.asyncio
    async def test_oss_without_byok_does_not_use_official_or_env_fallback(self, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-should-not-be-used")
        import packages.core.services.platform_model_provider_keys as provider_keys
        from packages.core.ai import llm_client

        async def fail_resolve(*_args, **_kwargs):
            raise AssertionError("OSS routing must not query official provider tokens")

        monkeypatch.setattr(provider_keys, "resolve_official_provider_credential", fail_resolve)
        token = llm_client._is_byok_call.set(True)
        try:
            routing = await resolve_llm_routing_for_model("anthropic/claude-sonnet-4.6")
        finally:
            llm_client._is_byok_call.reset(token)

        assert routing.api_key == ""
        assert routing.provider == "anthropic"
        assert routing.source == "missing"
        assert llm_client._is_byok_call.get(False) is False


# ── Anthropic native sampling-parameter gate ──────────────────────────


class TestAnthropicSamplingParamsGate:
    """Anthropic removed temperature/top_p/top_k from Opus 4.7 onward and
    the Fable/Mythos tier — the native /v1/messages payload must omit them
    or the request 400s. Older Claude models still accept temperature."""

    def test_fable_normalizes_and_rejects_sampling(self):
        from packages.core.ai.llm_client import _anthropic_accepts_sampling_params

        wire = normalize_model_for_provider(
            "anthropic/claude-fable-5",
            "https://api.anthropic.com/v1",
        )
        assert wire == "claude-fable-5"
        assert _anthropic_accepts_sampling_params(wire) is False

    def test_opus_47_and_newer_reject_sampling(self):
        from packages.core.ai.llm_client import _anthropic_accepts_sampling_params

        assert _anthropic_accepts_sampling_params("claude-opus-4-7") is False
        assert _anthropic_accepts_sampling_params("claude-opus-4-8") is False
        assert _anthropic_accepts_sampling_params("claude-mythos-5") is False
        # Future point releases stay covered by the prefix match.
        assert _anthropic_accepts_sampling_params("claude-fable-5.1") is False

    def test_older_claude_models_keep_sampling(self):
        from packages.core.ai.llm_client import _anthropic_accepts_sampling_params

        assert _anthropic_accepts_sampling_params("claude-sonnet-4-6") is True
        assert _anthropic_accepts_sampling_params("claude-opus-4-6") is True
        assert _anthropic_accepts_sampling_params("claude-haiku-4-5") is True
