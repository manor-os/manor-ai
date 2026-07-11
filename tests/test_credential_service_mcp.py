from __future__ import annotations

import json
import logging
from http.client import RemoteDisconnected
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from packages.core.credentials import Requester
from packages.core.credentials.audit import NullAuditSink
from packages.core.credentials.dev_provider import DevKeyProvider
from packages.core.credentials.service import CredentialService


def _service() -> tuple[CredentialService, DevKeyProvider]:
    key_provider = DevKeyProvider(
        key=Fernet.generate_key().decode("ascii"),
        audit_sink=NullAuditSink(),
    )
    return CredentialService(key_provider, audit_sink=NullAuditSink()), key_provider


def test_mcp_server_secret_uses_stable_server_key_context() -> None:
    service, _ = _service()
    server = SimpleNamespace(
        id="mcp_github________________",
        server_key="github",
        default_config={},
        credential_ref=None,
        credential_scheme=None,
    )

    service.store_mcp_server(server, {"oauth_client_secret": "gh_secret"})
    server.id = "different_row_id"

    leased = service.lease_mcp_server(
        server,
        requester=Requester(kind="test", id="mcp_stable_context"),
        reason="assert_mcp_secret_roundtrip",
    )

    assert leased == {"oauth_client_secret": "gh_secret"}
    assert server.default_config["_credential_context"] == "mcp_server_key_v1"
    assert "oauth_client_secret" not in server.default_config


def test_mcp_server_secret_can_read_legacy_id_bound_context() -> None:
    service, key_provider = _service()
    server = SimpleNamespace(
        id="mcp_slack_________________",
        server_key="slack",
        default_config={},
        credential_ref=None,
        credential_scheme=service.backend,
    )
    legacy_context = {
        "kind": "mcp_server",
        "mcp_server_id": server.id,
        "server_key": server.server_key,
    }
    server.credential_ref = key_provider.encrypt(
        json.dumps({"oauth_client_secret": "slack_secret"}).encode("utf-8"),
        legacy_context,
    )

    leased = service.lease_mcp_server(
        server,
        requester=Requester(kind="test", id="mcp_legacy_context"),
        reason="assert_mcp_legacy_roundtrip",
    )

    assert leased == {"oauth_client_secret": "slack_secret"}


def test_oauth_client_configured_check_does_not_lease_secret(monkeypatch) -> None:
    import packages.core.credentials as credentials
    from packages.core.services.oauth_provider_config import oauth_client_configured

    class ExplodingCredentialService:
        def lease_mcp_server(self, *args, **kwargs):
            raise AssertionError("catalog readiness must not decrypt OAuth secrets")

    monkeypatch.setattr(
        credentials,
        "get_credential_service",
        lambda: ExplodingCredentialService(),
    )
    monkeypatch.delenv("DISCORD_CLIENT_ID", raising=False)
    monkeypatch.delenv("DISCORD_CLIENT_SECRET", raising=False)

    assert oauth_client_configured(
        "discord",
        SimpleNamespace(
            default_config={"oauth_client_id": "db_cid"},
            credential_ref="vault:v1:discord",
        ),
    )


def test_oauth_client_configured_accepts_env_pair(monkeypatch) -> None:
    from packages.core.services.oauth_provider_config import oauth_client_configured

    monkeypatch.setenv("DISCORD_CLIENT_ID", "env_cid")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "env_csec")

    assert oauth_client_configured("discord", None)


@pytest.mark.asyncio
async def test_oauth_resolve_uses_env_pair_when_env_seeded_secret_is_stale(
    client,
    monkeypatch,
    caplog,
) -> None:
    import packages.core.credentials as credentials
    import packages.core.database as dbmod
    from packages.core.credentials import CredentialDecryptError
    from packages.core.models.mcp import MCPServer
    import packages.core.services.oauth_provider_config as oauth_provider_config
    from packages.core.services.oauth_provider_config import resolve_oauth_config
    from sqlalchemy import select

    caplog.set_level(logging.WARNING, logger="packages.core.services.oauth_provider_config")
    oauth_provider_config._OAUTH_SECRET_DECRYPT_WARNED.clear()  # noqa: SLF001

    class BrokenCredentialService:
        def lease_mcp_server(self, *args, **kwargs):
            raise CredentialDecryptError("cipher: message authentication failed")

    monkeypatch.setattr(
        credentials,
        "get_credential_service",
        lambda: BrokenCredentialService(),
    )
    monkeypatch.setenv("SLACK_CLIENT_ID", "env_cid")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "env_csec")

    async with dbmod.async_session() as db:
        server = (await db.execute(select(MCPServer).where(MCPServer.server_key == "slack"))).scalar_one()
        server.default_config = {
            "oauth_client_id": "stale_cid",
            "_oauth_source": "env",
        }
        server.credential_ref = "vault:v1:stale"
        server.credential_scheme = "vault_transit"
        await db.commit()

    async with dbmod.async_session() as db:
        cfg = await resolve_oauth_config(db, "slack")

    assert cfg is not None
    assert cfg.client_id == "env_cid"
    assert cfg.client_secret == "env_csec"
    assert cfg.source == "env"
    assert "OAuth client secret decrypt failed for slack" in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
async def test_oauth_resolve_uses_env_pair_when_secret_backend_disconnects(
    monkeypatch,
    caplog,
) -> None:
    import packages.core.credentials as credentials
    import packages.core.services.oauth_provider_config as oauth_provider_config
    from packages.core.services.oauth_provider_config import resolve_oauth_config

    caplog.set_level(logging.WARNING, logger="packages.core.services.oauth_provider_config")
    oauth_provider_config._OAUTH_SECRET_DECRYPT_WARNED.clear()  # noqa: SLF001

    class BrokenCredentialService:
        def lease_mcp_server(self, *args, **kwargs):
            raise RemoteDisconnected("Remote end closed connection without response")

    class Result:
        def __init__(self, server):
            self._server = server

        def scalar_one_or_none(self):
            return self._server

    class FakeDB:
        async def execute(self, _query):
            return Result(
                SimpleNamespace(
                    server_key="discord",
                    default_config={
                        "oauth_client_id": "env_cid",
                        "_oauth_source": "env",
                    },
                    credential_ref="vault:v1:discord",
                    credential_scheme="vault_transit",
                )
            )

    monkeypatch.setattr(
        credentials,
        "get_credential_service",
        lambda: BrokenCredentialService(),
    )
    monkeypatch.setenv("DISCORD_CLIENT_ID", "env_cid")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "env_csec")

    cfg = await resolve_oauth_config(FakeDB(), "discord")

    assert cfg is not None
    assert cfg.client_id == "env_cid"
    assert cfg.client_secret == "env_csec"
    assert cfg.source == "env"
    assert "OAuth client secret lease failed for discord" in caplog.text
    assert "lease oauth secret failed for discord" not in caplog.text
    assert "Traceback" not in caplog.text
