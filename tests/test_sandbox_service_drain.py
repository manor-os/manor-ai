"""Sandbox Service liveness and graceful restart.

2026-10-14 incident: under host load the liveness probe (1s timeout) hit a
/health that ran ``docker image inspect`` on the event loop; kubelet killed the
service and lifespan shutdown destroyed a Video Edit render sandbox mid-flight.
These tests pin the two contracts that prevent it: /health answers from memory
while Docker work is in flight, and a SIGTERM drains in-flight work before the
service stops listening and destroys its sandboxes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

SANDBOX_SERVICE_ROOT = Path(__file__).resolve().parents[1] / "sandbox-service"
sys.path.insert(0, str(SANDBOX_SERVICE_ROOT))

from sandbox.drain import SandboxServiceDraining  # noqa: E402
from sandbox.docker_backend import DockerSandbox  # noqa: E402
from sandbox.models import ContainerConfig, SandboxStatus, SkillManifest  # noqa: E402

TOKEN = "drain-test-token"


def _load_sandbox_main_module():
    spec = importlib.util.spec_from_file_location(
        "_sandbox_service_main_drain_test",
        SANDBOX_SERVICE_ROOT / "main.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sandbox_main(monkeypatch):
    import api.routes as routes

    module = _load_sandbox_main_module()
    monkeypatch.setattr(module.config, "API_TOKEN", TOKEN, raising=False)
    monkeypatch.setattr(routes, "_runner", module.runner)
    yield module
    sys.modules.pop("_sandbox_service_main_drain_test", None)


def _ready_sandbox(sandbox_id: str) -> DockerSandbox:
    sandbox = DockerSandbox(sandbox_id, ContainerConfig())
    sandbox.container_name = f"skill-sbx-{sandbox_id}"
    sandbox.skill = SkillManifest(name="video-edit-runtime", skill_dir="/tmp/skill")
    sandbox.status = SandboxStatus.READY
    sandbox.created_at = sandbox.last_used_at = 1.0
    return sandbox


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://sandbox",
        headers={"X-Manor-Sandbox-Token": TOKEN},
    )


@pytest.mark.asyncio
async def test_health_answers_while_a_docker_exec_blocks_and_never_calls_docker(sandbox_main, monkeypatch):
    import sandbox.docker_backend as docker_backend

    docker_calls: list[list[str]] = []
    exec_started = threading.Event()
    release_exec = threading.Event()

    def slow_docker(args, **_kwargs):
        docker_calls.append(list(args))
        if args[:2] == ["docker", "exec"]:
            exec_started.set()
            # A loaded host: the docker CLI takes as long as the test says.
            assert release_exec.wait(timeout=10)
        return subprocess.CompletedProcess(args, 0, b"rendered\n", b"")

    monkeypatch.setattr(docker_backend, "_run_docker", slow_docker)
    runner = sandbox_main.runner
    runner._sandboxes["sbx-1"] = _ready_sandbox("sbx-1")
    # A started service: startup already filled the image cache.
    runner._image_status = {"sandbox_image_available": True}
    runner._image_status_at = time.monotonic()

    async with _client(sandbox_main.app) as client:
        exec_task = asyncio.create_task(
            client.post("/api/v1/sandbox/sbx-1/exec", json={"command": "render", "timeout": 30})
        )
        try:
            assert await asyncio.to_thread(exec_started.wait, 5)
            calls_before_health = len(docker_calls)

            health = await asyncio.wait_for(client.get("/health"), timeout=5)
            ready = await asyncio.wait_for(client.get("/ready"), timeout=5)

            assert health.status_code == 200
            assert health.json()["status"] == "ok"
            # Liveness must not spawn the Docker CLI (it queues behind host load).
            assert len(docker_calls) == calls_before_health
            assert not exec_task.done()
            # Not started (no lifespan): readiness says so without touching Docker.
            assert ready.status_code == 503
            assert ready.json()["detail"] == "starting"
        finally:
            release_exec.set()
        response = await asyncio.wait_for(exec_task, timeout=5)
    assert response.status_code == 200
    assert response.json()["stdout"] == "rendered\n"


@pytest.mark.asyncio
async def test_drain_refuses_new_sandboxes_and_waits_for_inflight_exec(sandbox_main, monkeypatch):
    runner = sandbox_main.runner
    sandbox = _ready_sandbox("sbx-1")
    exec_started = asyncio.Event()
    release_exec = asyncio.Event()
    destroyed: list[str] = []

    async def blocked_exec(command, **kwargs):
        exec_started.set()
        await release_exec.wait()
        from sandbox.models import ExecResponse

        return ExecResponse(stdout="done", stderr="", exit_code=0, execution_id=kwargs.get("execution_id"))

    async def fake_destroy():
        destroyed.append(sandbox.sandbox_id)
        sandbox.status = SandboxStatus.DESTROYED

    async def alive():
        return None

    monkeypatch.setattr(sandbox, "_run_in_container", blocked_exec)
    monkeypatch.setattr(sandbox, "verify_alive", alive)
    monkeypatch.setattr(sandbox, "destroy", fake_destroy)
    runner._sandboxes["sbx-1"] = sandbox

    async with _client(sandbox_main.app) as client:
        exec_task = asyncio.create_task(
            client.post("/api/v1/sandbox/sbx-1/exec", json={"command": "render", "timeout": 30})
        )
        await asyncio.wait_for(exec_started.wait(), timeout=5)

        drain_task = asyncio.create_task(runner.drain(timeout=10, quiet_seconds=0.05))
        await asyncio.sleep(0.2)
        assert runner.draining
        # The render is still running: the drain must not finish around it.
        assert not drain_task.done()

        refused = await client.post(
            "/api/v1/sandbox/create-from-files",
            json={"skill_name": "other", "files": {"run.py": "print(1)"}},
        )
        assert refused.status_code == 503
        assert refused.json()["code"] == "sandbox_service_draining"
        assert refused.headers["Retry-After"]
        with pytest.raises(SandboxServiceDraining):
            await runner.create_sandbox_from_files("other", {"run.py": "print(1)"}, env={})

        release_exec.set()
        response = await asyncio.wait_for(exec_task, timeout=5)
        assert response.status_code == 200
        # The caller can still read its result from the draining service.
        status = await client.get("/api/v1/sandbox/sbx-1")
        assert status.status_code == 200
        assert (await client.get("/health")).json()["draining"] is True

        assert await asyncio.wait_for(drain_task, timeout=5) is True

    assert destroyed == []
    await runner.shutdown()
    assert destroyed == ["sbx-1"]


@pytest.mark.asyncio
async def test_drain_gives_up_at_its_timeout_with_work_still_running(sandbox_main):
    runner = sandbox_main.runner
    never_finishes = asyncio.Event()
    background = asyncio.create_task(never_finishes.wait())
    runner._execution_tasks[("sbx-1", "exec-1")] = background
    try:
        assert await runner.drain(timeout=0.2, quiet_seconds=0) is False
    finally:
        background.cancel()
        runner._execution_tasks.clear()


@pytest.mark.asyncio
async def test_sigterm_drains_before_uvicorn_shuts_down_and_second_signal_forces_exit():
    drain_started = asyncio.Event()
    finish_drain = asyncio.Event()

    class FakeRunner:
        async def drain(self, *, timeout, quiet_seconds):
            drain_started.set()
            await finish_drain.wait()
            return True

    sandbox_main = _load_sandbox_main_module()
    try:
        server = sandbox_main.DrainingServer(
            uvicorn.Config(sandbox_main.app),
            skill_runner=FakeRunner(),
            drain_timeout=30,
            quiet_seconds=0,
        )
        server._loop = asyncio.get_running_loop()

        server.handle_exit(signal.SIGTERM, None)
        await asyncio.wait_for(drain_started.wait(), timeout=5)
        # Still listening while the drain runs.
        assert server.should_exit is False

        finish_drain.set()
        await asyncio.wait_for(server._drain_task, timeout=5)
        assert server.should_exit is True

        second = sandbox_main.DrainingServer(
            uvicorn.Config(sandbox_main.app),
            skill_runner=FakeRunner(),
            drain_timeout=30,
            quiet_seconds=0,
        )
        second._loop = asyncio.get_running_loop()
        finish_drain.clear()
        second.handle_exit(signal.SIGTERM, None)
        second.handle_exit(signal.SIGTERM, None)
        assert second.should_exit is True
        await asyncio.sleep(0)
        finish_drain.set()
        if second._drain_task is not None:
            await asyncio.wait_for(second._drain_task, timeout=5)
    finally:
        sys.modules.pop("_sandbox_service_main_drain_test", None)
