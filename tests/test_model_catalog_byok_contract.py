from packages.core.constants.models import CATALOG, DEFAULTS
from packages.core.services.model_provider_handlers import (
    catalog_byok_contract_issues,
    catalog_provider_capability,
    handler_for_model,
    native_catalog_model_id,
)


def test_every_remote_catalog_model_has_test_save_runtime_contract():
    assert catalog_byok_contract_issues() == []

    for role, entries in CATALOG.items():
        for item in entries:
            model = item["id"]
            if item.get("deployment") == "local":
                assert item.get("byok") is False
                continue
            handler = handler_for_model(model)
            capability = catalog_provider_capability(role, model)
            assert handler is not None, f"{role}:{model} has no official provider"
            assert role in handler.roles, f"{role}:{model} is not runnable by its provider"
            assert capability is not None, f"{role}:{model} cannot be live-tested"
            assert capability.test_strategy
            assert capability.runtime_strategy


def test_byok_support_does_not_replace_product_catalog_model_ids():
    primary_ids = {item["id"] for item in CATALOG["primary"]}
    worker_ids = {item["id"] for item in CATALOG["worker"]}

    assert {
        "moonshotai/kimi-k3",
        "moonshotai/kimi-k2.6",
        "qwen/qwen3.8-max",
        "qwen/qwen3.7-flash",
    }.issubset(primary_ids)
    assert {"qwen/qwen3.7-flash", "openai/gpt-4.1-mini"}.issubset(worker_ids)
    assert not any(model.startswith("alibaba/") for model in primary_ids | worker_ids)


def test_every_default_is_an_explicit_catalog_entry():
    for role, model in DEFAULTS.items():
        if not model:
            assert CATALOG[role] == []
            continue
        assert model in {item["id"] for item in CATALOG[role]}, f"{role}:{model}"


def test_media_catalog_ids_match_provider_wire_ids():
    image_ids = {item["id"] for item in CATALOG["image"]}
    assert {
        "openai/gpt-5-image-mini",
        "google/gemini-3.1-flash-image-preview",
        "openai/gpt-image-2",
    }.issubset(image_ids)
    for catalog_id in image_ids:
        assert native_catalog_model_id(catalog_id)
    assert {"openai/gpt-audio-mini", "openai/gpt-audio"}.issubset(
        {item["id"] for item in CATALOG["sfx"]}
    )
    assert {
        "openai/whisper-1",
        "groq/whisper-large-v3",
        "openai/gpt-4o-audio-preview",
        "openai/gpt-audio-mini",
        "openai/gpt-audio",
        "openai/gpt-4o-mini-transcribe",
        "openai/gpt-4o-transcribe",
    }.issubset({item["id"] for item in CATALOG["stt"]})

    assert native_catalog_model_id("openai/gpt-image-2") == "gpt-image-2"
