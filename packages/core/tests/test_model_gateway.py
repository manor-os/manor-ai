from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from packages.core.services.model_gateway import (
    ModelGatewayRoute,
    model_provider_catalog,
    resolve_official_model_route,
    route_pricing_source,
)
from packages.core.services.platform_model_provider_keys import OfficialProviderCredential


def test_model_provider_catalog_exposes_gateway_providers():
    catalog = model_provider_catalog()
    providers = {item["provider"] for item in catalog}
    keyed = {item["provider"]: item for item in catalog}

    assert "anthropic" in providers
    assert "openrouter" in providers
    assert "vercel" in providers
    assert "zyphra" in providers
    assert keyed["vercel"]["base_url"] == "https://ai-gateway.vercel.sh/v1"
    assert keyed["moonshotai"]["api_shape"] == "kimi_chat_completions"
    assert "https://api.moonshot.cn/v1" in keyed["moonshotai"]["base_url_aliases"]


def test_route_pricing_source_distinguishes_gateway_official_and_byok():
    official = ModelGatewayRoute(
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com/v1",
        provider="anthropic",
        source="official",
    )
    vercel = ModelGatewayRoute(
        api_key="vck_test",
        base_url="https://ai-gateway.vercel.sh/v1",
        provider="vercel",
        source="official",
    )
    openrouter = ModelGatewayRoute(
        api_key="sk-or-test",
        base_url="https://openrouter.ai/api/v1",
        provider="openrouter",
        source="official",
    )
    byok = ModelGatewayRoute(
        api_key="sk-ant-user",
        base_url="https://api.anthropic.com/v1",
        provider="anthropic",
        source="byok",
    )

    assert route_pricing_source(official) == ("anthropic", "official")
    assert route_pricing_source(vercel) == ("vercel", "vercel")
    assert route_pricing_source(openrouter) == ("openrouter", "openrouter")
    assert route_pricing_source(byok) == ("anthropic", "byok")


@pytest.mark.asyncio
async def test_resolve_official_model_route_prefers_vercel_gateway(monkeypatch):
    import packages.core.services.platform_model_provider_keys as provider_keys

    async def fake_resolve(provider: str, *, reason: str = "", sources=("db", "env")):
        assert provider == "vercel"
        return OfficialProviderCredential(
            provider="vercel",
            api_key="vck_official_test",
            base_url="https://ai-gateway.vercel.sh/v1",
            source="official",
            source_detail="test",
        )

    monkeypatch.setattr(provider_keys, "resolve_official_provider_credential", fake_resolve)

    route = await resolve_official_model_route("anthropic/claude-sonnet-4.6")

    assert route is not None
    assert route.provider == "vercel"
    assert route.pricing_source == "vercel"


@pytest.mark.asyncio
async def test_resolve_official_model_route_falls_back_to_openrouter(monkeypatch):
    import packages.core.services.platform_model_provider_keys as provider_keys

    async def fake_resolve(provider: str, *, reason: str = "", sources=("db", "env")):
        if provider == "vercel":
            return None
        assert provider == "openrouter"
        return OfficialProviderCredential(
            provider="openrouter",
            api_key="sk-or-official-test",
            base_url="https://openrouter.ai/api/v1",
            source="official",
            source_detail="test",
        )

    monkeypatch.setattr(provider_keys, "resolve_official_provider_credential", fake_resolve)

    route = await resolve_official_model_route("anthropic/claude-sonnet-4.6")

    assert route is not None
    assert route.provider == "openrouter"
    assert route.pricing_source == "openrouter"


@pytest.mark.asyncio
async def test_resolve_official_model_route_survives_vercel_lookup_error(monkeypatch):
    import packages.core.services.platform_model_provider_keys as provider_keys

    async def fake_resolve(provider: str, *, reason: str = "", sources=("db", "env")):
        if provider == "vercel":
            raise RuntimeError("Vercel credential store unavailable")
        return OfficialProviderCredential(
            provider="openrouter",
            api_key="sk-or-official-test",
            base_url="https://openrouter.ai/api/v1",
            source="official",
            source_detail="test",
        )

    monkeypatch.setattr(provider_keys, "resolve_official_provider_credential", fake_resolve)

    route = await resolve_official_model_route("anthropic/claude-sonnet-4.6")

    assert route is not None
    assert route.provider == "openrouter"


@pytest.mark.asyncio
async def test_resolve_official_model_route_prefers_admin_db_source_over_env_source(
    monkeypatch,
):
    """Admin DB credentials beat environment credentials across the route chain."""
    import packages.core.services.platform_model_provider_keys as provider_keys

    async def fake_resolve(provider: str, *, reason: str = "", sources=("db", "env")):
        if provider == "vercel" and "env" in sources:
            return OfficialProviderCredential(
                provider="vercel",
                api_key="vck_env_candidate_1234567890",
                base_url="https://ai-gateway.vercel.sh/v1",
                source="official",
                source_detail="AI_GATEWAY_API_KEY",
            )
        if provider == "openrouter" and "db" in sources:
            return OfficialProviderCredential(
                provider="openrouter",
                api_key="sk-or-db_candidate_1234567890",
                base_url="https://db-openrouter.example/v1",
                source="official",
                source_detail="db",
            )
        return None

    monkeypatch.setattr(provider_keys, "resolve_official_provider_credential", fake_resolve)

    route = await resolve_official_model_route(
        "anthropic/claude-sonnet-4.6",
    )

    assert route is not None
    assert route.provider == "openrouter"
    assert route.source_detail == "db"


@pytest.mark.asyncio
async def test_openrouter_env_base_url_is_used_when_db_key_is_missing(monkeypatch):
    import packages.core.services.platform_model_provider_keys as provider_keys

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env_key_1234567890")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://router.example/v1")

    credential = await provider_keys.resolve_official_provider_credential("openrouter")

    assert credential is not None
    assert credential.source_detail == "OPENROUTER_API_KEY"
    assert credential.base_url == "https://router.example/v1"


@pytest.mark.asyncio
async def test_legacy_encrypted_admin_key_is_leased_before_env(monkeypatch):
    import packages.core.credentials as credential_pkg
    import packages.core.database as database
    import packages.core.services.platform_model_provider_keys as provider_keys

    row = SimpleNamespace(
        provider="vercel",
        status="active",
        credential_ref="vault:v1:ciphertext",
        credential_scheme="vault_transit",
        config={"base_url": "https://ai-gateway.vercel.sh/v1"},
    )

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def fake_get_row(_db, provider):
        assert provider == "vercel"
        return row

    class CredentialService:
        def lease_model_provider_key(self, leased_row, *, requester, reason):
            assert leased_row is row
            assert requester.kind == "system"
            assert requester.id == "model_provider:vercel"
            assert reason == "test.legacy.admin.key"
            return {"api_key": "vck-legacy-admin-key-1234567890"}

    monkeypatch.setenv("AI_GATEWAY_API_KEY", "vck-env-key-1234567890")
    monkeypatch.setattr(database, "async_session", fake_session)
    monkeypatch.setattr(provider_keys, "_get_row", fake_get_row)
    monkeypatch.setattr(
        credential_pkg,
        "get_credential_service",
        lambda: CredentialService(),
    )

    credential = await provider_keys.resolve_official_provider_credential(
        "vercel",
        reason="test.legacy.admin.key",
    )

    assert credential is not None
    assert credential.api_key == "vck-legacy-admin-key-1234567890"
    assert credential.source_detail == "db"
