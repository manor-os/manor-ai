"""Unit tests for multi-provider BYOK routing.

Tests: detect_provider_from_key, detect_provider_from_model,
resolve_provider_base_url, normalize_model_for_provider.
"""

import pytest

from packages.core.ai.llm_client import (
    _route_metadata_for_resolved_model,
    detect_provider_from_key,
    detect_provider_from_model,
    normalize_model_for_provider,
    resolve_llm_routing_for_model,
    resolve_provider_base_url,
)
from packages.core.services.platform_model_provider_keys import OfficialProviderCredential


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

    def test_generic_sk_uses_moonshot_model_provider(self):
        url = resolve_provider_base_url("moonshotai/kimi-k2.6", "sk-abc123", None)
        assert url == "https://api.moonshot.ai/v1"

    def test_unknown_zyphra_key_uses_zyphra_model_provider(self):
        url = resolve_provider_base_url("zyphra/zonos-v0.1-hybrid", "zyphra-test-key", None)
        assert url == "https://api.zyphra.com/v1"


# ── normalize_model_for_provider ──────────────────────────────────────


class TestNormalizeModelForProvider:
    def test_openrouter_keeps_full_id(self):
        result = normalize_model_for_provider("anthropic/claude-sonnet-4.6", "https://openrouter.ai/api/v1")
        assert result == "anthropic/claude-sonnet-4.6"

    def test_direct_openai_strips_prefix(self):
        result = normalize_model_for_provider("openai/gpt-4.1", "https://api.openai.com/v1")
        assert result == "gpt-4.1"

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
    async def test_official_provider_token_routes_catalog_model_native(self, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
        import packages.core.services.platform_model_provider_keys as provider_keys
        from packages.core.ai import llm_client

        async def fake_resolve(provider: str, *, reason: str = ""):
            assert provider == "anthropic"
            return OfficialProviderCredential(
                provider="anthropic",
                api_key="sk-ant-official-provider-key-1234567890",
                base_url="https://api.anthropic.com/v1",
                source="official",
                source_detail="test",
            )

        token = llm_client._is_byok_call.set(True)
        try:
            monkeypatch.setattr(provider_keys, "resolve_official_provider_credential", fake_resolve)
            routing = await resolve_llm_routing_for_model("anthropic/claude-sonnet-4.6")
        finally:
            llm_client._is_byok_call.reset(token)

        assert routing.api_key == "sk-ant-official-provider-key-1234567890"
        assert routing.base_url == "https://api.anthropic.com/v1"
        assert routing.provider == "anthropic"
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
    async def test_openrouter_fallback_when_native_official_missing(self, monkeypatch):
        monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
        import packages.core.services.platform_model_provider_keys as provider_keys
        from packages.core.ai import llm_client

        async def fake_resolve(provider: str, *, reason: str = ""):
            if provider == "anthropic":
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
