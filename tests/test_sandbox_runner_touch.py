"""Idle heartbeats neither execute code nor resurrect unavailable sandboxes."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

SANDBOX_SERVICE_ROOT = Path(__file__).resolve().parents[1] / "sandbox-service"
sys.path.insert(0, str(SANDBOX_SERVICE_ROOT))

from sandbox import skill_runner as runner_module  # noqa: E402
from sandbox.docker_backend import DockerSandbox  # noqa: E402
from sandbox.models import ContainerConfig, SandboxInfo, SandboxStatus  # noqa: E402
from sandbox.skill_runner import SkillRunner  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_sandbox_owner_registry():
    # Tests use an in-memory registry, never a live Docker/Redis sandbox.
    yield


@pytest.fixture
def ready_runner(monkeypatch):
    monkeypatch.setattr(runner_module.app_config, "MAX_SANDBOXES", 2)
    monkeypatch.setattr(runner_module.app_config, "IDLE_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(runner_module.time, "time", lambda: 1000.0)
    runner = SkillRunner()
    sandbox = DockerSandbox("sandbox-1", ContainerConfig())
    sandbox.container_name = "test-existing-sandbox"
    sandbox.status = SandboxStatus.READY
    sandbox.created_at = 800.0
    sandbox.last_used_at = 900.0
    runner._sandboxes[sandbox.sandbox_id] = sandbox
    sandbox.exec = AsyncMock(side_effect=AssertionError("touch must not execute commands"))
    return runner, sandbox


def test_touch_refreshes_idle_expiry_and_returns_existing_info_without_execution(ready_runner):
    runner, sandbox = ready_runner
    capacity_before = runner._local_active_capacity.reserved
    lease = object()
    runner._active_leases[sandbox.sandbox_id] = lease

    result = runner.touch_sandbox(sandbox.sandbox_id)

    assert isinstance(result, SandboxInfo)
    assert result == runner.get_sandbox_status(sandbox.sandbox_id)
    assert result.sandbox_id == sandbox.sandbox_id
    assert result.container_name == "test-existing-sandbox"
    assert result.status == SandboxStatus.READY
    assert result.created_at == 800.0
    assert result.last_used_at == 1000.0
    assert result.expires_at == 1060.0
    assert result.config == sandbox.config
    assert runner._active_leases == {sandbox.sandbox_id: lease}
    assert runner._local_active_capacity.reserved == capacity_before
    assert runner._gate.executing == 0
    assert runner._execution_records == {}
    sandbox.exec.assert_not_awaited()


@pytest.mark.parametrize("status,destroy_requested", [
    (SandboxStatus.ERROR, False),
    (SandboxStatus.DESTROYING, False),
    (SandboxStatus.DESTROYED, False),
    (SandboxStatus.READY, True),
])
def test_touch_rejects_quarantined_or_destroying_sandbox_without_renewing(
    ready_runner, status, destroy_requested,
):
    runner, sandbox = ready_runner
    sandbox.status = status
    sandbox._destroy_requested = destroy_requested
    with pytest.raises(RuntimeError, match="unavailable"):
        runner.touch_sandbox(sandbox.sandbox_id)
    assert sandbox.last_used_at == 900.0
    assert sandbox.status == status


def test_touch_unknown_sandbox_does_not_create_one(ready_runner):
    runner, _ = ready_runner
    with pytest.raises(KeyError):
        runner.touch_sandbox("missing")
    assert set(runner._sandboxes) == {"sandbox-1"}


def test_touch_active_execution_only_renews_idle_timestamp(ready_runner):
    runner, sandbox = ready_runner
    sandbox.status = SandboxStatus.EXECUTING
    sandbox._active_command = "render original project"
    sandbox._active_execution_id = "execution-1"
    result = runner.touch_sandbox(sandbox.sandbox_id)
    assert result.status == SandboxStatus.EXECUTING
    assert result.active_command == "render original project"
    assert result.active_execution_id == "execution-1"
    assert result.last_used_at == 1000.0
    sandbox.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_heartbeats_reuse_existing_capacity_and_execution_state(ready_runner):
    runner, sandbox = ready_runner
    assert await runner._local_active_capacity.try_acquire()

    async def heartbeat():
        await asyncio.sleep(0)
        return runner.touch_sandbox(sandbox.sandbox_id)

    results = await asyncio.gather(*(heartbeat() for _ in range(128)))

    assert all(result.last_used_at == 1000.0 and result.expires_at == 1060.0 for result in results)
    assert runner._sandboxes == {sandbox.sandbox_id: sandbox}
    assert runner._local_active_capacity.reserved == 1
    assert runner._gate.executing == 0
    assert runner._execution_records == {}
    sandbox.exec.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["touch", "busy", "replace"])
async def test_prune_rechecks_later_candidate_after_an_earlier_destroy_yields(ready_runner, change):
    runner, first = ready_runner
    second = DockerSandbox("sandbox-2", ContainerConfig())
    second.status = SandboxStatus.READY
    second.last_used_at = 900.0
    runner._sandboxes[second.sandbox_id] = second
    second.destroy = AsyncMock()
    assert await runner._local_active_capacity.try_acquire()
    assert await runner._local_active_capacity.try_acquire()
    first_destroy_started = asyncio.Event()
    finish_first_destroy = asyncio.Event()

    async def slow_destroy():
        first._destroy_requested = True
        first.status = SandboxStatus.DESTROYING
        first_destroy_started.set()
        await finish_first_destroy.wait()
        first.status = SandboxStatus.DESTROYED

    first.destroy = slow_destroy
    prune = asyncio.create_task(runner._prune_idle())
    await asyncio.wait_for(first_destroy_started.wait(), timeout=1)
    if change == "touch":
        assert runner.touch_sandbox(second.sandbox_id).expires_at == 1060.0
    elif change == "busy":
        second.status = SandboxStatus.EXECUTING
    else:
        replacement = DockerSandbox(second.sandbox_id, ContainerConfig())
        replacement.status = SandboxStatus.READY
        replacement.last_used_at = 900.0
        runner._sandboxes[second.sandbox_id] = replacement
    finish_first_destroy.set()
    await asyncio.wait_for(prune, timeout=1)

    assert "sandbox-1" not in runner._sandboxes
    assert "sandbox-2" in runner._sandboxes
    assert runner._local_active_capacity.reserved == 1
    second.destroy.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_untouched_sandbox_is_still_pruned(ready_runner):
    runner, sandbox = ready_runner
    assert await runner._local_active_capacity.try_acquire()
    sandbox.destroy = AsyncMock()
    await runner._prune_idle()
    sandbox.destroy.assert_awaited_once()
    assert runner._sandboxes == {}
    assert runner._local_active_capacity.reserved == 0


@pytest.fixture
def touch_client(monkeypatch, ready_runner):
    spec = importlib.util.spec_from_file_location(
        "_sandbox_service_main_touch_test", SANDBOX_SERVICE_ROOT / "main.py",
    )
    assert spec and spec.loader
    main = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, main)
    spec.loader.exec_module(main)
    from api import routes

    runner, sandbox = ready_runner
    monkeypatch.setattr(main.config, "API_TOKEN", "runner-test-secret")
    monkeypatch.setattr(routes, "_runner", runner)
    # Not entering the client context deliberately avoids the Docker lifespan.
    client = TestClient(main.app, raise_server_exceptions=False)
    yield client, runner, sandbox
    client.close()


def test_touch_api_returns_refreshed_sandbox_info(touch_client):
    client, runner, sandbox = touch_client
    result = client.post(
        f"/api/v1/sandbox/{sandbox.sandbox_id}/touch",
        headers={"X-Manor-Sandbox-Token": "runner-test-secret"},
    )
    assert result.status_code == 200, result.text
    assert result.json() == runner.get_sandbox_status(sandbox.sandbox_id).model_dump(mode="json")
    assert result.json()["last_used_at"] == 1000.0
    assert result.json()["expires_at"] == 1060.0
    sandbox.exec.assert_not_awaited()


@pytest.mark.parametrize("token", [None, "wrong"])
def test_touch_api_requires_token_and_does_not_refresh_on_denial(touch_client, token):
    client, _, sandbox = touch_client
    headers = {"X-Manor-Sandbox-Token": token} if token else {}
    result = client.post(f"/api/v1/sandbox/{sandbox.sandbox_id}/touch", headers=headers)
    assert result.status_code == 401
    assert sandbox.last_used_at == 900.0


def test_touch_api_unknown_sandbox_returns_404(touch_client):
    client, runner, _ = touch_client
    result = client.post(
        "/api/v1/sandbox/missing/touch",
        headers={"X-Manor-Sandbox-Token": "runner-test-secret"},
    )
    assert result.status_code == 404
    assert set(runner._sandboxes) == {"sandbox-1"}


@pytest.mark.parametrize("status,destroy_requested", [
    (SandboxStatus.ERROR, False),
    (SandboxStatus.DESTROYING, False),
    (SandboxStatus.DESTROYED, False),
    (SandboxStatus.READY, True),
])
def test_touch_api_unavailable_sandbox_returns_409(touch_client, status, destroy_requested):
    client, _, sandbox = touch_client
    sandbox.status = status
    sandbox._destroy_requested = destroy_requested
    result = client.post(
        f"/api/v1/sandbox/{sandbox.sandbox_id}/touch",
        headers={"X-Manor-Sandbox-Token": "runner-test-secret"},
    )
    assert result.status_code == 409, result.text
    assert sandbox.last_used_at == 900.0
