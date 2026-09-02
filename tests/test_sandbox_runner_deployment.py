from __future__ import annotations

from pathlib import Path
import stat


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "cloud" / "sandbox-runner" / "install.sh"
SMOKE = ROOT / "scripts" / "cloud" / "sandbox-runner" / "smoke.sh"
COMPOSE = ROOT / "docker-compose.yml"


def test_local_sandbox_allows_networkless_ephemeral_chat_execution() -> None:
    src = COMPOSE.read_text()

    assert "SANDBOX_ALLOWED_NETWORKS: ${SANDBOX_ALLOWED_NETWORKS:-bridge,none}" in src


def test_external_sandbox_runner_install_script_creates_systemd_runner() -> None:
    src = INSTALL.read_text()

    assert 'SERVICE_NAME="${SERVICE_NAME:-manor-sandbox-runner}"' in src
    assert 'SANDBOX_RUNNER_IMAGE_SOURCE="${SANDBOX_RUNNER_IMAGE_SOURCE:-pull}"' in src
    assert 'SANDBOX_RUNNER_BIND_ADDRESS="${SANDBOX_RUNNER_BIND_ADDRESS:-0.0.0.0}"' in src
    assert 'SANDBOX_RUNNER_PORT="${SANDBOX_RUNNER_PORT:-8000}"' in src
    assert 'SANDBOX_API_TOKEN="${SANDBOX_API_TOKEN:-}"' in src
    assert 'SANDBOX_NETWORK_NAME="${SANDBOX_NETWORK_NAME:-manor-sandbox}"' in src
    assert 'SANDBOX_NETWORK="${SANDBOX_NETWORK:-$SANDBOX_NETWORK_NAME}"' in src
    assert 'SANDBOX_ALLOWED_NETWORKS="${SANDBOX_ALLOWED_NETWORKS:-$SANDBOX_NETWORK_NAME}"' in src
    assert "ensure_sandbox_network()" in src
    assert 'docker network inspect "$SANDBOX_NETWORK_NAME"' in src
    assert 'docker network create --driver bridge "$SANDBOX_NETWORK_NAME"' in src
    assert "pull_runner_images()" in src
    assert 'docker pull "$SANDBOX_RUNNER_IMAGE"' in src
    assert 'docker pull "$SANDBOX_SKILL_IMAGE"' in src
    assert "build_runner_images()" in src
    assert "docker build -t \"$SANDBOX_RUNNER_IMAGE\" -f docker/Dockerfile.sandbox-service ." in src
    assert "docker build -t \"$SANDBOX_SKILL_IMAGE\" -f docker/Dockerfile.sandbox ." in src
    assert "docker_login_if_configured()" in src
    assert "REGISTRY_PASSWORD" in src
    assert "EnvironmentFile=/etc/manor/sandbox-runner.env" in src
    assert "-v /var/run/docker.sock:/var/run/docker.sock" in src
    assert "-p ${SANDBOX_RUNNER_BIND_ADDRESS}:${SANDBOX_RUNNER_PORT}:8000" in src
    assert "systemctl enable \"$SERVICE_NAME\"" in src
    assert "systemctl restart \"$SERVICE_NAME\"" in src
    assert "SANDBOX_MAX_SANDBOXES" in src
    assert "SANDBOX_INSTANCE_MAX_EXECUTING" in src
    assert "SANDBOX_GATE_WAIT_TIMEOUT_SECONDS" in src
    assert "SANDBOX_API_TOKEN" in src
    assert "SANDBOX_NETWORK=${SANDBOX_NETWORK}" in src
    assert "SANDBOX_ALLOWED_NETWORKS=${SANDBOX_ALLOWED_NETWORKS}" in src


def test_external_sandbox_runner_smoke_covers_create_exec_destroy() -> None:
    src = SMOKE.read_text()

    assert 'SANDBOX_RUNNER_URL="${SANDBOX_RUNNER_URL:-http://127.0.0.1:8000}"' in src
    assert 'SANDBOX_API_TOKEN="${SANDBOX_API_TOKEN:-}"' in src
    assert 'EXPECTED_BUILD_VERSION="${EXPECTED_BUILD_VERSION:-}"' in src
    assert 'EXPECTED_SANDBOX_IMAGE="${EXPECTED_SANDBOX_IMAGE:-}"' in src
    assert 'X-Manor-Sandbox-Token: ${SANDBOX_API_TOKEN}' in src
    assert "/health" in src
    assert "unexpected sandbox runner build_version" in src
    assert "unexpected sandbox runner sandbox_image" in src
    assert "/api/v1/sandbox/create-from-files" in src
    assert "/api/v1/sandbox/${sandbox_id}/exec" in src
    assert "DELETE" in src
    assert "/api/v1/sandbox/${sandbox_id}" in src
    assert "manor-external-sandbox-runner-ok" in src


def test_external_sandbox_runner_scripts_are_executable() -> None:
    for script in (INSTALL, SMOKE):
        mode = script.stat().st_mode
        assert mode & stat.S_IXUSR, f"{script.name} must be executable because docs invoke it directly"
