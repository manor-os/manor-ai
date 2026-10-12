"""Single-owner OAuth bootstrap, using disposable SQLite and shell doubles."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from packages.core.services import oauth_provider_service as svc
from tests.test_oauth_provider_entity_scope import (  # noqa: F401
    CLIENT_SECRET,
    REDIRECT_URI,
    client_secret_hash,
    oauth_db,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def clean_client_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("MANOR_OAUTH_CLIENT_"):
            monkeypatch.delenv(name)


async def test_bootstrap_updates_existing_pms_policy_without_rotating_secret(clean_client_env, monkeypatch, oauth_db):  # noqa: F811
    from scripts.bootstrap_oauth_provider_clients import bootstrap

    monkeypatch.setenv("MANOR_OAUTH_CLIENT_PMS_MANAGEMENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_PMS_MANAGEMENT_REDIRECT_URIS", REDIRECT_URI)
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_PMS_MANAGEMENT_REQUIRE_PKCE", "true")
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_PMS_MANAGEMENT_REQUIRE_VERIFIED_EMAIL", "true")
    sessions, _ = oauth_db
    async with sessions() as db:
        assert await bootstrap(db) == {"pms-management": "seeded"}
        client = await svc.get_client_by_id(db, "pms-management")
        original_hash = client.client_secret_hash
        client.require_verified_email = client.require_pkce = False
        await db.commit()
        assert await bootstrap(db) == {"pms-management": "updated(require_pkce,require_verified_email)"}
        assert client.require_verified_email is client.require_pkce is True
        assert client.client_secret_hash == original_hash
        assert await bootstrap(db) == {"pms-management": "unchanged"}


@pytest.mark.parametrize("settings,code", [
    ({"REDIRECT_URIS": REDIRECT_URI}, "missing_client_secret"),
    ({"SECRET": CLIENT_SECRET}, "missing_redirect_uris"),
    ({"SECRET": CLIENT_SECRET, "REDIRECT_URIS": REDIRECT_URI, "REQUIRE_PKCE": "invalid"}, "invalid_client_configuration"),
])
async def test_partial_client_configuration_is_fatal_before_seed(clean_client_env, monkeypatch, settings, code):
    from scripts.bootstrap_oauth_provider_clients import BootstrapError, bootstrap

    for name, value in settings.items():
        monkeypatch.setenv(f"MANOR_OAUTH_CLIENT_PMS_MANAGEMENT_{name}", value)

    async def forbidden(_db):
        pytest.fail("Malformed configuration must fail before any database seed")

    monkeypatch.setattr(svc, "seed_clients_from_env", forbidden)
    with pytest.raises(BootstrapError, match=code):
        await bootstrap(object())


async def test_absent_clients_are_not_created(clean_client_env, monkeypatch):
    from scripts.bootstrap_oauth_provider_clients import bootstrap

    monkeypatch.setenv("MANOR_OAUTH_CLIENT_UNCONFIGURED_SECRET", "")

    async def forbidden(_db):
        pytest.fail("Unconfigured deployments must not touch OAuth client rows")

    monkeypatch.setattr(svc, "seed_clients_from_env", forbidden)
    assert await bootstrap(object()) == {}


async def test_duplicate_resolved_client_cannot_overwrite_another_slug(clean_client_env, monkeypatch):
    from scripts.bootstrap_oauth_provider_clients import BootstrapError, bootstrap

    for slug in ("PMS_MANAGEMENT", "OTHER"):
        monkeypatch.setenv(f"MANOR_OAUTH_CLIENT_{slug}_SECRET", CLIENT_SECRET)
        monkeypatch.setenv(f"MANOR_OAUTH_CLIENT_{slug}_REDIRECT_URIS", REDIRECT_URI)
        monkeypatch.setenv(f"MANOR_OAUTH_CLIENT_{slug}_CLIENT_ID", "pms-management")

    async def forbidden(_db):
        pytest.fail("Duplicate client IDs must fail before any database seed")

    monkeypatch.setattr(svc, "seed_clients_from_env", forbidden)
    with pytest.raises(BootstrapError, match="duplicate_client_id"):
        await bootstrap(object())


@pytest.mark.parametrize("action", [None, "error:DatabaseError", "skipped:no_redirect_uris"])
async def test_seed_failure_is_not_reported_as_success(clean_client_env, monkeypatch, action):
    from scripts.bootstrap_oauth_provider_clients import BootstrapError, bootstrap

    monkeypatch.setenv("MANOR_OAUTH_CLIENT_PMS_MANAGEMENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("MANOR_OAUTH_CLIENT_PMS_MANAGEMENT_REDIRECT_URIS", REDIRECT_URI)

    async def incomplete(_db):
        return {} if action is None else {"pms-management": action}

    monkeypatch.setattr(svc, "seed_clients_from_env", incomplete)
    with pytest.raises(BootstrapError, match="oauth_client_seed_incomplete"):
        await bootstrap(object())


@pytest.mark.parametrize("role,seed_status", [("migration", 0), ("migration", 19), ("api", 0), ("chat", 0), ("worker", 0)])
def test_real_entrypoint_seeds_only_single_owner_and_stops_on_failure(tmp_path, role, seed_status):
    """Execute the real shell entrypoint; every DB/Python command is a double."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "events"
    python = binaries / "python3"
    python.write_text("""#!/bin/sh
case "$1" in
  scripts/bootstrap_oauth_provider_clients.py)
    echo oauth-bootstrap >> "$EVENTS"
    exit "$SEED_STATUS";;
  -c)
    case "$2" in *has_table*) echo false;; esac;;
  -) cat >/dev/null;;
esac
exit 0
""")
    alembic = binaries / "alembic"
    alembic.write_text('#!/bin/sh\necho migration >> "$EVENTS"\n')
    python.chmod(0o755)
    alembic.chmod(0o755)
    (tmp_path / "alembic.ini").touch()
    env = {
        "PATH": str(binaries) + os.pathsep + os.defpath,
        "EVENTS": str(log), "SEED_STATUS": str(seed_status),
        "MANOR_SERVICE_ROLE": role, "MANOR_RUN_DB_BOOTSTRAP": "true" if role == "migration" else "false",
        "MANOR_FS_ENABLED": "false", "DEPLOYMENT_MODE": "cloud",
        "DATABASE_URL_SYNC": "postgresql://unused:unused@127.0.0.1:1/not_used",
    }
    result = subprocess.run(["bash", str(ROOT / "docker/entrypoint.sh"), "sh", "-c", 'echo main >> "$EVENTS"'],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    events = log.read_text().splitlines() if log.exists() else []
    if role == "migration":
        assert events[:2] == ["migration", "oauth-bootstrap"]
        assert result.returncode == seed_status
        assert ("main" in events) is (seed_status == 0)
    else:
        assert result.returncode == 0
        assert events == ["main"]
