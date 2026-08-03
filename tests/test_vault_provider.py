from __future__ import annotations

from types import SimpleNamespace

from packages.core.credentials.vault_provider import VaultKeyProvider


class _FakeTransit:
    def __init__(self) -> None:
        self.read_calls = 0

    def read_key(self, *, name: str, mount_point: str):
        self.read_calls += 1
        return {"data": {"name": name, "mount_point": mount_point}}


class _FakeSys:
    def __init__(self) -> None:
        self.enable_calls = 0

    def enable_secrets_engine(self, **_kwargs):
        self.enable_calls += 1
        raise AssertionError("health/ensure must not require sys/mounts for an existing transit key")

    def is_sealed(self) -> bool:
        return False


class _FakeClient:
    def __init__(self, *, authenticated: bool) -> None:
        self._authenticated = authenticated
        self.sys = _FakeSys()
        self.secrets = SimpleNamespace(transit=_FakeTransit())

    def is_authenticated(self) -> bool:
        return self._authenticated


def _provider_with_client(client: _FakeClient) -> VaultKeyProvider:
    provider = VaultKeyProvider("http://vault:8200", "token", transit_key="manor-keys")
    provider._client = client  # noqa: SLF001
    return provider


def test_ensure_transit_key_reads_existing_key_without_sys_mount_permission() -> None:
    client = _FakeClient(authenticated=True)
    provider = _provider_with_client(client)

    provider._ensure_transit_key()  # noqa: SLF001

    assert client.secrets.transit.read_calls == 1
    assert client.sys.enable_calls == 0


def test_health_reports_invalid_vault_token() -> None:
    provider = _provider_with_client(_FakeClient(authenticated=False))

    health = provider.health()

    assert health.ok is False
    assert "token" in health.detail.lower()
