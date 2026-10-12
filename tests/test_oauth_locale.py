"""OAuth initializes language once and preserves it when accounts are reused."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.requests import Request
from starlette.responses import Response

from packages.core.i18n import get_locale, set_locale
from packages.core.models.user import OAuthAccount, User
from packages.core.services import auth_service, team_invite_service


def _result(value):
    return SimpleNamespace(scalar_one_or_none=lambda: value, scalar_one=lambda: value)


@pytest.fixture
def oauth_dependencies(monkeypatch):
    monkeypatch.setattr(auth_service, "ensure_user_membership", AsyncMock())
    monkeypatch.setattr(auth_service, "_grant_initial_plan_credits", AsyncMock(), raising=False)
    monkeypatch.setattr("packages.core.services.entity_fs.is_fs_enabled", lambda: False)
    monkeypatch.setattr(
        "packages.core.services.oauth_account_credentials.store_oauth_account_tokens", Mock(),
    )
    previous = get_locale()
    set_locale("vi")
    yield
    set_locale(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "apple", "saml"])
@pytest.mark.parametrize("account", ["new", "email", "oauth"])
@pytest.mark.parametrize("locale", [None, "fr"])
async def test_oauth_language_changes_only_on_creation(oauth_dependencies, provider, account, locale):
    existing = User(id="existing", entity_id="entity", email="user@example.test", locale="zh",
                    role="owner", status="active")
    oauth = OAuthAccount(user_id=existing.id) if account == "oauth" else None
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[_result(oauth), _result(None if account == "new" else existing)]),
        add=Mock(), flush=AsyncMock(),
    )

    user, is_new = await auth_service.get_or_create_oauth_user(
        db, provider=provider, provider_user_id="provider-user", email=existing.email, locale=locale,
        email_verified=provider in {"google", "apple"},
    )

    assert is_new is (account == "new")
    assert user.locale == ((locale or "vi") if is_new else "zh")
    if not is_new:
        assert user is existing


@pytest.mark.asyncio
@pytest.mark.parametrize("locale, expected", [
    (None, "vi"), ("zh-CN", "zh"), (" DE_de ", "de"), ("ja-JP", "ja"),
    ("unsupported", "en"),
])
async def test_oauth_initial_locale_normalizes_or_uses_request(oauth_dependencies, locale, expected):
    db = SimpleNamespace(execute=AsyncMock(return_value=_result(None)), add=Mock(), flush=AsyncMock())

    user, is_new = await auth_service.get_or_create_oauth_user(
        db, provider="saml", provider_user_id="new-user", email="new@example.test", locale=locale,
    )

    assert is_new is True
    assert user.locale == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_account", [False, True])
@pytest.mark.parametrize("locale, expected", [(None, "vi"), ("fr-FR", "fr"), ("unknown", "en")])
async def test_team_oauth_initializes_new_user_only(
    monkeypatch, oauth_dependencies, existing_account, locale, expected,
):
    staff = SimpleNamespace(entity_id="team", email="staff@example.test", name="Staff")
    existing = User(id="existing", entity_id="personal", email=staff.email, locale="zh", status="active")
    monkeypatch.setattr(team_invite_service, "_load_invited_staff", AsyncMock(return_value=staff))
    monkeypatch.setattr(team_invite_service, "_user_for_staff_email", AsyncMock(
        return_value=existing if existing_account else None,
    ))
    monkeypatch.setattr(team_invite_service, "_legacy_role_for_staff", AsyncMock(return_value="member"))
    monkeypatch.setattr(team_invite_service, "_upsert_oauth_account", AsyncMock())
    monkeypatch.setattr(team_invite_service, "_activate_staff_membership", AsyncMock())
    db = SimpleNamespace(add=Mock(), flush=AsyncMock())

    accepted = await team_invite_service.accept_team_invite_with_oauth(
        db, token="invite", provider="google", provider_user_id="provider-user",
        email=staff.email, locale=locale,
    )

    assert accepted.user.locale == ("zh" if existing_account else expected)
    assert accepted.user.entity_id == ("personal" if existing_account else "team")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [
    "google_code",
    pytest.param("google_id_token", marks=pytest.mark.cloud),
    "google_team_invite",
    pytest.param("apple", marks=pytest.mark.cloud),
])
async def test_oauth_routes_pass_request_locale(monkeypatch, oauth_dependencies, entry):
    from apps.api.routers import auth

    user = User(id="new", entity_id="entity", email="new@example.test", role="owner",
                status="active", locale="en", token_version=0)
    provision = AsyncMock(return_value=(user, True))
    accept_invite = AsyncMock(return_value=SimpleNamespace(user=user))
    monkeypatch.setattr(auth, "get_or_create_oauth_user", provision)
    monkeypatch.setattr(team_invite_service, "accept_team_invite_with_oauth", accept_invite)
    monkeypatch.setattr("packages.core.services.invitation_codes.is_required", AsyncMock(return_value=False))
    monkeypatch.setattr("packages.core.services.email_service.send_welcome_email", AsyncMock())
    monkeypatch.setattr(auth, "_initialize_registration_context", AsyncMock(), raising=False)
    monkeypatch.setattr(auth, "_attach_affiliate_referral", AsyncMock(return_value=False), raising=False)
    monkeypatch.setattr(auth, "_record_signup_attribution", AsyncMock(), raising=False)
    monkeypatch.setattr(auth, "mark_user_login", Mock())
    monkeypatch.setattr(auth, "create_access_token", Mock(return_value="token"))
    db = SimpleNamespace(execute=AsyncMock(return_value=_result(None)), scalar=AsyncMock(return_value=None), commit=AsyncMock())

    async def welcome_after_commit(*args):
        db.commit.assert_awaited_once()

    monkeypatch.setattr("packages.core.services.email_service.send_welcome_email", welcome_after_commit)
    request = Request({"type": "http", "headers": []})
    claims = {"sub": "provider-user", "email": user.email, "email_verified": True,
              "aud": "client", "exp": 9999999999}

    if entry == "apple":
        monkeypatch.setattr(auth.jwt, "PyJWKClient", Mock(return_value=SimpleNamespace(
            get_signing_key_from_jwt=lambda _: SimpleNamespace(key="key"),
        )))
        monkeypatch.setattr(auth.jwt, "decode", Mock(return_value=claims))
        response = await auth.oauth_apple(auth.OAuthAppleRequest(identity_token="identity"), request, db)
    else:
        monkeypatch.setattr("packages.core.services.oauth_provider_config.resolve_oauth_config", AsyncMock(
            return_value=SimpleNamespace(client_id="client", client_secret="secret"),
        ))
        http = AsyncMock()
        http.__aenter__.return_value = http
        http.get.return_value = SimpleNamespace(status_code=200, json=lambda: claims)
        http.post.return_value = SimpleNamespace(status_code=200, json=lambda: {"access_token": "google-token"})
        monkeypatch.setattr(auth.httpx, "AsyncClient", Mock(return_value=http))
        req = auth.OAuthGoogleRequest(
            redirect_uri="manor://oauth",
            code="code" if entry != "google_id_token" else None,
            id_token="identity" if entry == "google_id_token" else None,
            team_invite_token="invite" if entry == "google_team_invite" else None,
        )
        response = await auth.oauth_google(req, request, Response(), db)

    called = accept_invite if entry == "google_team_invite" else provision
    assert called.await_args.kwargs["locale"] == "vi"
    assert response.is_new is True
