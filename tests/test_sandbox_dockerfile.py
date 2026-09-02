from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile.sandbox"


def test_sandbox_runtime_pins_node_major_without_unbounded_npm_upgrade() -> None:
    src = DOCKERFILE.read_text()

    assert "FROM node:22-bookworm-slim AS node-runtime" in src
    assert "COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node" in src
    assert "npm install -g npm@latest" not in src, (
        "npm@latest can move past the pinned Node major's supported engine range "
        "and break sandbox-skill image builds; keep the base-image npm or pin a "
        "compatible npm major."
    )
