from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import stat
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
COMPOSE_SMOKE = ROOT / "scripts" / "compose_config_smoke.sh"

pytestmark = pytest.mark.oss


def test_compose_keeps_worker_split_small_and_beat_filesystem_free() -> None:
    compose = yaml.safe_load(COMPOSE.read_text())
    services = compose["services"]

    assert {"manor-worker", "manor-worker-heavy", "manor-beat"}.issubset(services)
    assert "manor-worker-embedding" not in services

    worker_command = " ".join(str(part) for part in services["manor-worker"]["command"].split())
    worker_heavy_command = " ".join(str(part) for part in services["manor-worker-heavy"]["command"].split())
    beat = services["manor-beat"]

    assert "-Q celery,interactive,recovery-v2" in worker_command
    assert "-Q heavy,work" in worker_heavy_command
    assert "--hostname manor-worker@%h" in worker_command
    assert "--hostname manor-worker-heavy@%h" in worker_heavy_command
    assert beat["environment"]["MANOR_SERVICE_ROLE"] == "beat"
    assert beat["environment"]["MANOR_FS_ENABLED"] == "false"
    assert beat.get("devices") == []
    assert beat.get("volumes") == []
    assert beat.get("privileged") is False
    assert "juicefs-init" not in beat.get("depends_on", {})


def test_compose_keeps_local_state_single_machine_with_redis_db_split() -> None:
    compose = yaml.safe_load(COMPOSE.read_text())
    services = compose["services"]

    redis_services = [name for name in services if name.startswith("redis")]
    assert redis_services == ["redis"]

    api_env = services["manor-api"]["environment"]
    worker_env = services["manor-worker"]["environment"]
    sandbox_env = services["manor-sandbox"]["environment"]

    assert sandbox_env["MANOR_SERVICE_ROLE"] == "sandbox"
    assert api_env["REDIS_URL"] == "redis://redis:6379/0"
    assert api_env["JUICEFS_META_URL"] == "${JUICEFS_META_URL:-redis://redis:6379/1}"
    assert api_env["CELERY_BROKER_URL"] == "redis://redis:6379/2"
    assert api_env["CELERY_RESULT_BACKEND"] == "redis://redis:6379/3"
    assert worker_env["REDIS_URL"] == "redis://redis:6379/0"
    assert worker_env["CELERY_BROKER_URL"] == "redis://redis:6379/2"
    assert worker_env["CELERY_RESULT_BACKEND"] == "redis://redis:6379/3"
    assert worker_env["JUICEFS_META_URL"] == "${JUICEFS_META_URL:-redis://redis:6379/1}"
    assert worker_env["DEPLOYMENT_MODE"] == "${DEPLOYMENT_MODE:-oss}"
    assert worker_env["MANOR_ENV"] == "${MANOR_ENV:-dev}"
    assert worker_env["APP_URL"] == "${APP_URL:-http://localhost:18080}"
    assert worker_env["PUBLIC_BASE_URL"] == "${PUBLIC_BASE_URL:-http://localhost:8010}"
    assert services["manor-sandbox"]["ports"] == ["127.0.0.1:8110:8000"]
    local_sandbox_token = "${SANDBOX_API_TOKEN:-manor-local-sandbox-token-change-me}"
    assert api_env["SANDBOX_API_TOKEN"] == local_sandbox_token
    assert sandbox_env["SANDBOX_API_TOKEN"] == local_sandbox_token
    assert sandbox_env["SANDBOX_REDIS_URL"] == "redis://redis:6379/0"




def test_compose_pins_ollama_to_the_doks_runtime_version() -> None:
    compose = yaml.safe_load(COMPOSE.read_text())
    services = compose["services"]

    assert services["ollama"]["image"] == "ollama/ollama:0.32.15"
    assert services["ollama-init"]["image"] == "ollama/ollama:0.32.15"




def test_compose_config_smoke_script_covers_single_machine_modes() -> None:
    src = COMPOSE_SMOKE.read_text()
    mode = COMPOSE_SMOKE.stat().st_mode

    assert mode & stat.S_IXUSR, "scripts/compose_config_smoke.sh is invoked directly in docs"
    assert "docker compose -f docker-compose.yml config --services" in src
    assert "base_config" in src
    assert 'check_chat_concurrency_capacity "$base_config"' in src
    assert "manor-chat must render with exactly one Uvicorn worker" in src
    assert "single-machine compose must keep REDIS_URL on redis DB 0" in src
    assert "single-machine compose must keep CELERY_BROKER_URL on redis DB 2" in src
    assert "single-machine compose must keep JUICEFS_META_URL on redis DB 1" in src
    assert "single-machine compose must keep SANDBOX_REDIS_URL on redis DB 0" in src
    assert "single-machine compose sandbox gate defaults must stay bounded" in src
    for service in (
        "manor-api",
        "manor-chat",
        "manor-web",
        "manor-worker",
        "manor-worker-heavy",
        "manor-beat",
        "manor-migration",
        "manor-sandbox",
        "postgres",
        "redis",
        "minio",
    ):
        assert service in src


def test_cloud_entrypoint_rejects_unknown_explicit_service_role() -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "docker" / "entrypoint.sh"), "true"],
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "DEPLOYMENT_MODE": "cloud",
            "MANOR_SERVICE_ROLE": "typo-role",
            "MANOR_FS_ENABLED": "false",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Unsupported MANOR_SERVICE_ROLE=typo-role for DEPLOYMENT_MODE=cloud" in result.stderr


def test_database_wait_defaults_stay_bounded_and_configurable() -> None:
    src = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    assert 'DATABASE_WAIT_ATTEMPTS="${DATABASE_WAIT_ATTEMPTS:-15}"' in src
    assert 'DATABASE_WAIT_DELAY_SECONDS="${DATABASE_WAIT_DELAY_SECONDS:-2}"' in src
    assert 'seq 1 "$DATABASE_WAIT_ATTEMPTS"' in src
    assert 'sleep "$DATABASE_WAIT_DELAY_SECONDS"' in src
