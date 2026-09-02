from __future__ import annotations

import pytest

from packages.core.services.model_resolver import (
    _resolve_entity_scoped_model,
    resolve_llm_metadata_from_settings,
)


@pytest.mark.parametrize("role", ["worker", "docgen", "knowledge_gen", "agentic_loop"])
def test_local_live_llm_roles_inherit_account_primary_gpt55_byok(role: str) -> None:
    """Local live/E2E routes must use the Account-selected Primary BYOK model.

    This protects the Office/File Engine loop from silently falling back to
    env/default models when the Account page already has GPT-5.5 BYOK set.
    """

    settings = {
        "models": {"primary": "openai/gpt-5.5"},
        "llm_api_keys": {"primary": "sk-test-account-primary-1234567890"},
        "llm_api_key_models": {"primary": "openai/gpt-5.5"},
    }

    selected_model = _resolve_entity_scoped_model(
        role,
        entity_settings=settings,
        owner_prefs=None,
        platform_settings={},
    )
    metadata = resolve_llm_metadata_from_settings(
        settings,
        role=role,
        source="entity",
        selected_model=selected_model,
    )

    assert selected_model == "openai/gpt-5.5"
    assert metadata == {"llm_api_key": "sk-test-account-primary-1234567890"}


def test_account_primary_byok_does_not_bind_to_mismatched_selected_model() -> None:
    settings = {
        "models": {"primary": "openai/gpt-5.5"},
        "llm_api_keys": {"primary": "sk-test-account-primary-1234567890"},
        "llm_api_key_models": {"primary": "openai/gpt-5.5"},
    }

    assert (
        resolve_llm_metadata_from_settings(
            settings,
            role="worker",
            source="entity",
            selected_model="openai/gpt-5.6-terra",
        )
        is None
    )
