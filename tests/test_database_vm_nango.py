from __future__ import annotations

from pathlib import Path
import stat
import subprocess

import pytest


pytestmark = pytest.mark.k8s

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "cloud" / "database-vm"
INSTALLER = SCRIPT_DIR / "install-nango-database.sh"
VERIFY = SCRIPT_DIR / "verify-nango-database.sh"


def test_nango_database_tools_are_executable_and_syntax_valid() -> None:
    for script in (INSTALLER, VERIFY):
        assert script.is_file()
        assert script.stat().st_mode & stat.S_IXUSR
        subprocess.run(["bash", "-n", str(script)], check=True, cwd=ROOT)


def test_nango_database_installer_maps_only_fresh_environment_identities() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")

    for token in (
        'NANGO_DATABASE_ENV_FILE="${NANGO_DATABASE_ENV_FILE:-/etc/manor/nango-database.env}"',
        'database_name="nango_staging"',
        'database_user="nango_staging_user"',
        'database_name="nango_prod"',
        'database_user="nango_prod_user"',
        'NANGO_DB_PASSWORD="${NANGO_DB_PASSWORD:-}"',
        "NANGO_DB_PASSWORD must contain at least 32 characters",
        "CONNECTION LIMIT 20",
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
        "REVOKE CONNECT ON DATABASE",
        "ALTER SCHEMA public OWNER TO",
        "scram-sha-256",
    ):
        assert token in installer

    assert "manor_prod_user" not in installer
    assert "manor_staging_user" not in installer
    assert "pgbouncer.ini" not in installer
    assert "listen_port = 6432" not in installer
    assert "set -x" not in installer


def test_nango_database_hba_allows_exact_database_then_rejects_role_fallback() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")

    assert "# BEGIN MANOR NANGO DATABASE" in installer
    assert "# END MANOR NANGO DATABASE" in installer
    assert "hostssl ${database_name} ${database_user} ${cidr} scram-sha-256" in installer
    assert "hostssl all ${database_user} ${cidr} reject" in installer
    assert installer.index(
        "hostssl ${database_name} ${database_user} ${cidr} scram-sha-256"
    ) < installer.index("hostssl all ${database_user} ${cidr} reject")
    assert "SELECT error FROM pg_hba_file_rules WHERE error IS NOT NULL" in installer
    assert "SELECT pg_reload_conf()" in installer
    assert "ufw --force reset" not in installer


def test_nango_database_verifier_checks_tls_privileges_and_isolation() -> None:
    verify = VERIFY.read_text(encoding="utf-8")

    for token in (
        "sslmode=require",
        "pg_stat_ssl",
        "CASE WHEN ssl THEN 't' ELSE 'f' END",
        "rolconnlimit",
        "rolsuper",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
        "cross-database access was unexpectedly allowed",
        "pg_hba.conf",
        "pgbouncer.ini",
        "Nango role must not be present in PgBouncer",
    ):
        assert token in verify

    assert '"f,f,f,f,f,t,20"' in verify
    assert "port=6432" not in verify
    assert "set -x" not in verify
