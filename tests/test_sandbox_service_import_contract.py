from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_backend_uses_the_runtime_config_module():
    source = (ROOT / "sandbox-service" / "sandbox" / "docker_backend.py").read_text(
        encoding="utf-8"
    )

    assert "from sandbox_config import config as app_config" in source
