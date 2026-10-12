from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi.testclient import TestClient


SANDBOX_SERVICE_ROOT = Path(__file__).resolve().parents[1] / "sandbox-service"


def _load_sandbox_main_module():
    sys.path.insert(0, str(SANDBOX_SERVICE_ROOT))
    spec = importlib.util.spec_from_file_location(
        "_sandbox_service_main_auth_test",
        SANDBOX_SERVICE_ROOT / "main.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sandbox_runner_api_token_protects_api_routes_but_not_health(monkeypatch) -> None:
    sandbox_main = _load_sandbox_main_module()
    monkeypatch.setattr(sandbox_main.config, "API_TOKEN", "runner-secret", raising=False)
    monkeypatch.setattr(sandbox_main, "BUILD_VERSION", "k8s-test-fixture")
    monkeypatch.setattr(
        sandbox_main.runner,
        "runtime_status",
        lambda: {"sandbox_image": "ghcr.io/manor-os/sandbox-skill:k8s-test-fixture"},
    )

    client = TestClient(sandbox_main.app, raise_server_exceptions=False)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["build_version"] == "k8s-test-fixture"
    assert health.json()["sandbox_image"].endswith(":k8s-test-fixture")
    assert client.get("/api/v1/sandbox").status_code == 401
    assert client.get(
        "/api/v1/sandbox",
        headers={"X-Manor-Sandbox-Token": "wrong"},
    ).status_code == 401
    assert client.get(
        "/api/v1/sandbox",
        headers={"X-Manor-Sandbox-Token": "runner-secret"},
    ).status_code != 401


def test_sandbox_runner_api_fails_closed_when_token_is_unconfigured(monkeypatch) -> None:
    sandbox_main = _load_sandbox_main_module()
    monkeypatch.setattr(sandbox_main.config, "API_TOKEN", "", raising=False)

    client = TestClient(sandbox_main.app, raise_server_exceptions=False)

    assert client.get("/health").status_code == 200
    response = client.get("/api/v1/sandbox")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Sandbox API token is not configured",
    }
