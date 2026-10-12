"""A device following its OS must not overwrite a newer account language."""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value

from apps.api.routers.auth import UpdateProfileRequest, update_profile
from packages.core.models.user import User


def test_expected_locale_uses_the_same_language_normalization():
    req = UpdateProfileRequest(locale="ja-JP", expected_locale=" EN_us ")
    assert (req.locale, req.expected_locale) == ("ja", "en")
    with pytest.raises(ValidationError, match="Unsupported language"):
        UpdateProfileRequest(locale="ja", expected_locale="system")


async def _register(client):
    response = await client.post(
        "/api/v1/auth/register",
        headers={"X-Language": "en"},
        json={
            "email": "device-locale@example.com",
            "password": "securepass123",
            "entity_name": "Device Locale",
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_device_sync_updates_only_the_language_it_observed(client):
    headers = await _register(client)
    updated = await client.put(
        "/api/v1/auth/me", headers=headers,
        json={"locale": "ja-JP", "expected_locale": "EN_us"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["locale"] == "ja"

    stale = await client.put(
        "/api/v1/auth/me", headers=headers,
        json={"locale": "de", "expected_locale": "en", "display_name": "Stale device"},
    )
    assert stale.status_code == 409, stale.text
    current = (await client.get("/api/v1/auth/me", headers=headers)).json()
    assert current["locale"] == "ja"
    assert current["display_name"] == updated.json()["display_name"]

    explicit = await client.put(
        "/api/v1/auth/me", headers=headers, json={"locale": "zh"},
    )
    assert explicit.status_code == 200, explicit.text
    assert explicit.json()["locale"] == "zh"

    invalid = await client.put(
        "/api/v1/auth/me", headers=headers,
        json={"expected_locale": "zh", "display_name": "Invalid request"},
    )
    assert invalid.status_code == 400, invalid.text


@pytest.mark.asyncio
async def test_device_sync_reloads_a_profile_loaded_before_another_device_saved(client, db_session):
    headers = await _register(client)
    profile = (await client.get("/api/v1/auth/me", headers=headers)).json()
    stale_user = await db_session.get(User, profile["id"])
    assert stale_user.locale == "en"

    updated = await client.put(
        "/api/v1/auth/me", headers=headers, json={"locale": "zh"},
    )
    assert updated.status_code == 200, updated.text
    assert stale_user.locale == "en"

    with pytest.raises(HTTPException) as error:
        await update_profile(
            UpdateProfileRequest(locale="ja", expected_locale="en"),
            user=stale_user, db=db_session,
        )
    assert error.value.status_code == 409
    assert (await client.get("/api/v1/auth/me", headers=headers)).json()["locale"] == "zh"


@pytest.mark.asyncio
async def test_device_sync_preserves_active_membership_and_updates_a_stale_same_value(client, db_session):
    headers = await _register(client)
    profile = (await client.get("/api/v1/auth/me", headers=headers)).json()
    user = await db_session.get(User, profile["id"])
    # get_current_user binds the selected membership without changing the home entity.
    set_committed_value(user, "entity_id", "selected-team")
    set_committed_value(user, "role", "member")
    updated = await client.put(
        "/api/v1/auth/me", headers=headers, json={"locale": "ja"},
    )
    assert updated.status_code == 200, updated.text
    assert user.locale == "en"

    response = await update_profile(
        UpdateProfileRequest(locale="en", expected_locale="ja"), user=user, db=db_session,
    )
    assert response.locale == "en"
    assert response.entity_id == "selected-team"
    assert response.role == "member"
    stored = (await db_session.execute(
        select(User.locale, User.entity_id, User.role).where(User.id == user.id),
    )).one()
    assert stored == ("en", profile["entity_id"], "owner")
