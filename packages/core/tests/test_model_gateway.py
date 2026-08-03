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

    async def fake_resolve(provider: str, *, reason: str = ""):
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

    async def fake_resolve(provider: str, *, reason: str = ""):
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

    async def fake_resolve(provider: str, *, reason: str = ""):
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
