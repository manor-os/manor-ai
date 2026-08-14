"""Guards for the chat model catalog and its factory defaults.

These check the *class* of mistake rather than one model: every chat-tier
catalog entry must be priceable and routable, and the primary default has
extra duties beyond being cheap (see ``test_primary_default_*``).
"""
import time

from packages.core.ai.llm_client import DEFAULT_LLM_MODEL
from packages.core.constants.models import (
    CATALOG,
    DEFAULTS,
    env_configured_primary_model,
    model_input_modalities,
    resolve_model_for_role,
)
from packages.core.services.model_pricing import OFFICIAL_TOKEN_PRICES
from packages.core.services.model_provider_handlers import provider_for_model_id

CHAT_ROLES = ("primary", "worker")


def test_chat_defaults_are_the_first_pick_in_their_catalog():
    for role in CHAT_ROLES:
        assert CATALOG[role][0]["id"] == DEFAULTS[role], (
            f"the {role} picker must open on its factory default"
        )


def test_primary_default_matches_llm_client_fallback():
    # llm_client keeps its own literal for bare/legacy call sites; a drift
    # here means env-less callers silently run a different model than the
    # one the picker shows as default.
    assert DEFAULT_LLM_MODEL == DEFAULTS["primary"]


def test_primary_default_accepts_image_input():
    # ``_resolve_vision_model_if_needed`` falls back to the primary default
    # when the selected model is text-only, so a text-only default would
    # divert every image chat to the hard-coded gpt-4o fallback instead.
    modalities = model_input_modalities(DEFAULTS["primary"])
    assert modalities is not None, "primary default must be classified in _MODEL_INPUT_MODALITIES"
    assert "image" in modalities


def test_chat_catalog_models_are_priced():
    unpriced = [
        item["id"]
        for role in CHAT_ROLES
        for item in CATALOG[role]
        if item["id"] not in OFFICIAL_TOKEN_PRICES
    ]
    assert unpriced == [], f"catalog models without official pricing: {unpriced}"


def test_chat_catalog_models_have_a_registered_provider():
    # resolve_official_model_route() returns None for an unregistered
    # provider prefix, which drops the model off Manor's official routing.
    unroutable = [
        item["id"]
        for role in CHAT_ROLES
        for item in CATALOG[role]
        if provider_for_model_id(item["id"]) is None
    ]
    assert unroutable == [], f"catalog models with no provider handler: {unroutable}"


def test_admin_default_override_beats_the_env_pin(monkeypatch):
    """An operator picking a default in the admin portal is an explicit,
    audited act; ``LLM_MODEL`` is a static deployment setting. With the
    old order the portal reported a default it could not deliver.
    """
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-sonnet-4")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    settings = {"default_overrides": {"primary": "openai/gpt-5.6-luna"}}

    assert resolve_model_for_role("primary", None, None, settings) == "openai/gpt-5.6-luna"


def test_env_pin_still_applies_when_no_override_is_set(monkeypatch):
    # Deployments that never touch the admin portal keep the old behavior.
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-sonnet-4")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    assert resolve_model_for_role("primary", None, None, {}) == "anthropic/claude-sonnet-4"
    assert env_configured_primary_model("primary") == "anthropic/claude-sonnet-4"


def test_env_pin_never_applies_outside_the_primary_role(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-sonnet-4")

    assert env_configured_primary_model("worker") == ""
    assert resolve_model_for_role("worker", None, None, {}) == DEFAULTS["worker"]


def test_user_and_entity_preferences_still_outrank_the_admin_override(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    settings = {"default_overrides": {"primary": "openai/gpt-5.6-luna"}}

    assert resolve_model_for_role(
        "primary", {"models": {"primary": "anthropic/claude-opus-5"}}, None, settings
    ) == "anthropic/claude-opus-5"
    assert resolve_model_for_role(
        "primary", None, {"models": {"primary": "anthropic/claude-opus-5"}}, settings
    ) == "anthropic/claude-opus-5"


def test_unnamed_model_calls_also_honour_the_admin_override(monkeypatch):
    """``get_llm_model()`` backs every chat call that omits a model. If it
    kept reading env first, the override would work in the picker and be
    bypassed on that path.
    """
    from packages.core.ai import llm_client
    from packages.core.services import model_settings

    monkeypatch.setenv("LLM_MODEL", "anthropic/claude-sonnet-4")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setattr(model_settings, "_cache_at", time.time())
    monkeypatch.setattr(
        model_settings,
        "_cache",
        {"disabled_models": {}, "default_overrides": {"primary": "openai/gpt-5.6-luna"}},
    )
    assert llm_client.get_llm_model() == "openai/gpt-5.6-luna"

    # No override cached (OSS boot, cold worker) → unchanged env behavior.
    monkeypatch.setattr(model_settings, "_cache", None)
    assert llm_client.get_llm_model() == "anthropic/claude-sonnet-4"


def test_chat_catalog_has_no_duplicate_ids():
    for role in CHAT_ROLES:
        ids = [item["id"] for item in CATALOG[role]]
        assert len(ids) == len(set(ids)), f"duplicate ids in the {role} catalog: {ids}"
