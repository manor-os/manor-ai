from packages.core.constants.models import CATALOG, DEFAULTS
from packages.core.services.model_provider_handlers import (
    catalog_openrouter_contract_issues,
    catalog_vercel_contract_issues,
    normalize_model_for_provider,
    openrouter_model_id,
    vercel_catalog_model_type,
    vercel_model_id,
)
from packages.core.services.vercel_ai_gateway import (
    vercel_gateway_endpoint,
    vercel_gateway_headers,
)


def _catalog_fixture() -> dict[str, str]:
    return {
        vercel_model_id(item["id"]): model_type
        for role, entries in CATALOG.items()
        for item in entries
        if item.get("deployment") != "local"
        if (model_type := vercel_catalog_model_type(role, item["id"]))
    }


def test_every_vercel_catalog_entry_has_an_exact_protocol_model():
    assert (
        catalog_vercel_contract_issues(
            CATALOG,
            available_models=_catalog_fixture(),
        )
        == []
    )


def test_every_chat_catalog_entry_has_an_openrouter_fallback_model():
    available = {
        openrouter_model_id(item["id"])
        for role in ("primary", "worker")
        for item in CATALOG[role]
    }
    assert (
        catalog_openrouter_contract_issues(
            CATALOG,
            available_models=available,
        )
        == []
    )


def test_native_media_models_are_not_misrepresented_as_vercel_models():
    assert CATALOG["audio"]
    assert CATALOG["sfx"]
    assert DEFAULTS["audio"] == "google/lyria-3-clip-preview"
    assert DEFAULTS["sfx"] == "openai/gpt-audio-mini"
    assert vercel_catalog_model_type("audio", "google/lyria-3-clip-preview") is None
    assert vercel_catalog_model_type("sfx", "openai/gpt-audio-mini") is None
    assert vercel_catalog_model_type("image", "google/gemini-3.1-flash-image-preview") is None


def test_fixed_qwen_catalog_ids_normalize_to_vercel_catalog_namespace():
    assert (
        normalize_model_for_provider(
            "qwen/qwen3.8-max",
            "https://ai-gateway.vercel.sh/v1",
        )
        == "alibaba/qwen3.8-max"
    )


def test_legacy_catalog_ids_normalize_to_supported_vercel_wire_ids():
    """The picker keeps its stable IDs even when Vercel uses a newer key."""
    assert vercel_model_id("openai/gpt-4") == "openai/gpt-4-turbo"
    assert (
        normalize_model_for_provider(
            "openai/gpt-4",
            "https://ai-gateway.vercel.sh/v1",
        )
        == "openai/gpt-4-turbo"
    )
    assert vercel_model_id("openai/gpt-5-image-mini") == "openai/gpt-image-1-mini"
    assert vercel_catalog_model_type("image", "openai/gpt-5-image-mini") == "image"
    assert vercel_model_id("kwaivgi/kling-v3.0-std") == "klingai/kling-v3.0-t2v"
    assert vercel_catalog_model_type("video", "kwaivgi/kling-v3.0-std") == "video"


def test_historical_alibaba_id_normalizes_back_to_openrouter_namespace():
    assert (
        normalize_model_for_provider(
            "alibaba/qwen3.8-max",
            "https://openrouter.ai/api/v1",
        )
        == "qwen/qwen3.8-max"
    )


def test_vercel_v4_protocol_endpoint_and_headers():
    assert (
        vercel_gateway_endpoint(
            "https://ai-gateway.vercel.sh/v1",
            "image",
        )
        == "https://ai-gateway.vercel.sh/v4/ai/image-model"
    )
    headers = vercel_gateway_headers(
        api_key="vck_test",
        model="openai/gpt-image-2",
        protocol="image",
        auth_method="oidc",
    )
    assert headers["ai-model-id"] == "openai/gpt-image-2"
    assert headers["ai-image-model-specification-version"] == "4"
    assert headers["ai-gateway-auth-method"] == "oidc"
