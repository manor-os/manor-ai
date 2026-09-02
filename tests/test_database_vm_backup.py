from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.k8s

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts" / "cloud" / "database-vm"
BACKUP = SCRIPT_DIR / "backup.sh"
INSTALLER = SCRIPT_DIR / "install-backup.sh"


def test_database_backup_supports_only_the_declared_local_database_sets() -> None:
    backup = BACKUP.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")

    assert 'DATABASE_NAMES="${DATABASE_NAMES:-}"' in backup
    assert "IFS=',' read -r -a database_names" in backup
    assert '[[ "$database_name" =~ ^(manor_prod|manor_staging|nango_prod)$ ]]' in backup
    assert "nango_staging" not in backup
    assert "DATABASE_NAMES=manor_prod,nango_prod" in installer
    assert "DATABASE_NAMES=manor_staging" in installer
    assert "nango_staging" not in installer


def test_database_backup_validates_and_atomically_publishes_each_dump() -> None:
    backup = BACKUP.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")

    assert 'for database_name in "${database_names[@]}"; do' in backup
    assert 'backup_dir="$BACKUP_ROOT/$database_name"' in backup
    assert "runuser -u postgres -- pg_dump" in backup
    assert "--format=custom" in backup
    assert 'runuser -u postgres -- pg_restore --list "$dump_tmp"' in backup
    assert 'mv "$dump_tmp" "$dump_file"' in backup
    assert '-mtime +"$RETENTION_DAYS" -delete' in backup
    assert "BACKUP_ENDPOINT" not in backup + installer
    assert "S3" not in backup + installer
    assert "MINIO" not in backup + installer
