"""Readiness batches account reads while retaining the runtime access policy."""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.document import Integration
from packages.core.models.mcp import MCPServer
from packages.core.models.user import OAuthAccount
from packages.core.services.agent_permission_service import can_use_mcp_server
from packages.core.services.integration_resolution import integration_provider_readiness


async def test_provider_count_does_not_multiply_account_queries(db_session, monkeypatch):
    db = db_session
    user_id, entity_id = generate_ulid(), generate_ulid()
    providers = [f"readiness_{generate_ulid().lower()}" for _ in range(16)]
    db.add_all([
        OAuthAccount(
            user_id=user_id, provider=provider, provider_user_id=provider,
            access_token="fixture",
        )
        for provider in providers[::2]
    ])
    await db.flush()
    expected = {
        provider: await can_use_mcp_server(
            db, user_id=user_id, entity_id=entity_id, server_key=provider,
            allow_env_fallback=False,
        )
        for provider in providers
    }
    execute = AsyncMock(wraps=db.execute)
    monkeypatch.setattr(db, "execute", execute)

    await integration_provider_readiness(
        db, user_id=user_id, entity_id=entity_id, provider_keys=providers[:1],
    )
    single_query_count = execute.await_count
    execute.reset_mock()
    states = await integration_provider_readiness(
        db, user_id=user_id, entity_id=entity_id, provider_keys=providers,
    )

    assert execute.await_count == single_query_count
    for provider, decision in expected.items():
        assert (states[provider].ready, states[provider].reason, states[provider].scope) == (
            decision.allowed, decision.reason, decision.scope,
        )


async def test_each_check_reloads_actor_entity_and_account_liveness(db_session):
    db = db_session
    user_id, other_user_id = generate_ulid(), generate_ulid()
    entity_id, other_entity_id = generate_ulid(), generate_ulid()
    personal_provider = f"readiness_{generate_ulid().lower()}"
    entity_provider = f"readiness_{generate_ulid().lower()}"
    account = OAuthAccount(
        user_id=user_id, provider=personal_provider, provider_user_id="owner",
        access_token="fixture",
    )
    db.add_all([
        account,
        Integration(
            entity_id=entity_id, owner_user_id=user_id, provider=entity_provider,
            credentials={"token": "fixture"}, status="active",
        ),
    ])
    await db.flush()

    async def check(actor=user_id, entity=entity_id):
        return await integration_provider_readiness(
            db, user_id=actor, entity_id=entity,
            provider_keys=[personal_provider, entity_provider],
        )

    assert all(state.ready for state in (await check()).values())
    assert not any(state.ready for state in (await check(actor=other_user_id)).values())
    assert (await check(entity=other_entity_id))[entity_provider].ready is False
    account.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db.flush()
    states = await check()
    assert states[personal_provider].ready is False
    assert states[entity_provider].ready is True


async def test_account_free_and_first_party_checks_do_not_load_accounts(db_session, monkeypatch):
    from packages.core.services import integration_account_service

    server_key = f"readiness-no-auth-{generate_ulid().lower()}"
    db_session.add(MCPServer(
        server_key=server_key, name="No account", transport="builtin",
        auth_type="none", status="active",
    ))
    await db_session.flush()
    load = AsyncMock(side_effect=AssertionError("account registry is unnecessary"))
    monkeypatch.setattr(integration_account_service, "load_runtime_integration_registry", load)

    result = await integration_provider_readiness(
        db_session, user_id=generate_ulid(), entity_id=generate_ulid(),
        provider_keys=[server_key, "manor_mcp_calendar"],
    )

    assert all(state.ready and state.scope == "internal" for state in result.values())
    load.assert_not_awaited()


async def test_account_registry_failure_does_not_become_ready(db_session, monkeypatch):
    from packages.core.services import integration_account_service

    monkeypatch.setattr(
        integration_account_service, "load_runtime_integration_registry",
        AsyncMock(side_effect=RuntimeError("registry unavailable")),
    )
    with pytest.raises(RuntimeError, match="registry unavailable"):
        await integration_provider_readiness(
            db_session, user_id=generate_ulid(), entity_id=generate_ulid(),
            provider_keys=["slack", "github"],
        )
