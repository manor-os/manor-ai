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


