"""Account language remains a durable preference across request languages."""

import pytest
from pydantic import ValidationError

from apps.api.routers.auth import UpdateProfileRequest
from packages.core.i18n import SUPPORTED_LOCALES, t


@pytest.mark.parametrize("locale", sorted(SUPPORTED_LOCALES))
def test_profile_accepts_every_client_language(locale):
    assert UpdateProfileRequest(locale=locale).locale == locale


@pytest.mark.parametrize("locale, expected", [("zh-CN", "zh"), (" EN_us ", "en"), ("de-DE", "de")])
def test_profile_normalizes_language_tags(locale, expected):
    assert UpdateProfileRequest(locale=locale).locale == expected


@pytest.mark.parametrize("locale", ["", "  ", "unsupported", "system", "English"])
def test_profile_rejects_invalid_language(locale):
    with pytest.raises(ValidationError, match="Unsupported language"):
        UpdateProfileRequest(locale=locale)


def test_languages_without_ui_translations_keep_the_english_fallback():
    assert {"de", "vi", "ja"} <= SUPPORTED_LOCALES
    for locale in ("de", "vi"):
        assert t("error.not_found", locale=locale) == "Not found"


@pytest.mark.asyncio
async def test_profile_language_persists_across_requests(client):
    registration = await client.post(
        "/api/v1/auth/register",
        headers={"X-Language": "es"},
        json={"email": "locale-owner@example.com", "password": "securepass123", "entity_name": "Locale Test"},
    )
    assert registration.status_code == 200, registration.text
    headers = {"Authorization": f"Bearer {registration.json()['access_token']}", "X-Language": "en"}
    initial = await client.get("/api/v1/auth/me", headers=headers)
    assert initial.json()["locale"] == "es"

    updated = await client.put("/api/v1/auth/me", headers=headers, json={"locale": "zh-CN"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["locale"] == "zh"
    restored = await client.get("/api/v1/auth/me", headers=headers)
    assert restored.json()["locale"] == "zh"

    invalid = await client.put("/api/v1/auth/me", headers=headers, json={"locale": "invalid"})
    assert invalid.status_code == 422
    unchanged = await client.get("/api/v1/auth/me", headers=headers)
    assert unchanged.json()["locale"] == "zh"
