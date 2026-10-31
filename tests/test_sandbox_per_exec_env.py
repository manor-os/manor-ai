"""An exec may carry its own env, layered over the skill env.

The in-sandbox ``manor`` CLI needs a short-lived token for exactly one bash
command. It travels like the skill env (a 0600 ``--env-file`` removed after
the exec), never reaches argv or logs, and is never kept for later commands.
A started exec (bash's command, which may wait for a slot while its run
parks) carries it the same way: only in that execution's own task, never on
its record. Bash's own working-copy scripts run on the side channel, next to
the command slots, with no env at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sandbox-service"))
sys.modules.pop("config", None)

docker_backend = pytest.importorskip("sandbox.docker_backend")
models = pytest.importorskip("sandbox.models")


def _ready_sandbox():
    sandbox = docker_backend.DockerSandbox("sandbox-env", models.ContainerConfig())
    sandbox.container_name = "sandbox-env-container"
    sandbox.status = models.SandboxStatus.READY
    sandbox.last_used_at = time.time() - 60
    sandbox.skill = models.SkillManifest(name="generic-sandbox", skill_dir="/old-skill")
    return sandbox


class _DockerRecorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.env_files: list[dict[str, str]] = []

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        if "--env-file" in args:
            path = args[args.index("--env-file") + 1]
            assert oct(os.stat(path).st_mode & 0o777) == "0o600"
            content = Path(path).read_text(encoding="utf-8")
            self.env_files.append(dict(line.split("=", 1) for line in content.splitlines() if line))
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")


@pytest.mark.asyncio
async def test_per_exec_env_is_layered_over_the_skill_env_for_one_command(monkeypatch):
    recorder = _DockerRecorder()
    monkeypatch.setattr(docker_backend, "_run_docker", recorder)
    sandbox = _ready_sandbox()
    sandbox._skill_env = {"SKILL_KEY": "skill"}

    await sandbox.exec("manor ls", timeout=5, env={"MANOR_CLI_TOKEN": "tok-secret"})
    await sandbox.exec("echo later", timeout=5)

    assert recorder.env_files == [
        {"SKILL_KEY": "skill", "MANOR_CLI_TOKEN": "tok-secret"},
        {"SKILL_KEY": "skill"},
    ], "the per-exec env reaches one command and is never stored"
    assert sandbox._skill_env == {"SKILL_KEY": "skill"}
    for call in recorder.calls:
        assert "tok-secret" not in " ".join(call), "no value ever reaches argv"
        if "--env-file" in call:
            assert not os.path.exists(call[call.index("--env-file") + 1])


@pytest.mark.asyncio
async def test_per_exec_env_without_skill_env_still_uses_an_env_file(monkeypatch):
    recorder = _DockerRecorder()
    monkeypatch.setattr(docker_backend, "_run_docker", recorder)
    sandbox = _ready_sandbox()

    await sandbox.exec("manor ls", timeout=5, env={"MANOR_CLI_TOKEN": "tok"})

    assert recorder.env_files == [{"MANOR_CLI_TOKEN": "tok"}]


@pytest.mark.asyncio
async def test_sdk_sends_env_only_when_given() -> None:
    from packages.core.services.sandbox_sdk import SandboxClient

    bodies: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"stdout": "", "stderr": "", "exit_code": 0})

    client = SandboxClient(base_url="http://sandbox", transport=httpx.MockTransport(handler))
    try:
        await client.exec("sbx-1", "ls")
        await client.exec("sbx-1", "manor ls", env={"MANOR_CLI_TOKEN": "tok"})
    finally:
        await client.close()

    assert "env" not in bodies[0]
    assert bodies[1]["env"] == {"MANOR_CLI_TOKEN": "tok"}


@pytest.mark.asyncio
async def test_sdk_start_carries_env_and_exec_marks_the_side_channel() -> None:
    from packages.core.services.sandbox_sdk import SandboxClient

    bodies: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/exec/start"):
            return httpx.Response(200, json={
                "sandbox_id": "sbx-1", "execution_id": "e1", "status": "queued", "created_at": 1.0,
            })
        return httpx.Response(200, json={"stdout": "", "stderr": "", "exit_code": 0})

    client = SandboxClient(base_url="http://sandbox", transport=httpx.MockTransport(handler))
    try:
        await client.start_exec("sbx-1", "manor ls", env={"MANOR_CLI_TOKEN": "tok"}, queue_timeout=600)
        await client.start_exec("sbx-1", "ls")
        await client.exec("sbx-1", "python3 -c job", side_channel=True)
        await client.exec("sbx-1", "ls")
    finally:
        await client.close()

    assert bodies[0][1]["env"] == {"MANOR_CLI_TOKEN": "tok"}
    assert "env" not in bodies[1][1]
    assert bodies[2][1]["side_channel"] is True
    assert "side_channel" not in bodies[3][1]


def test_exec_routes_forward_env_and_route_the_side_channel(monkeypatch):
    from fastapi.testclient import TestClient

    from tests.sandbox_runner_fakes import API_TOKEN, load_service_main

    service = load_service_main(monkeypatch, "_sandbox_service_main_per_exec_env_test")
    from api import routes

    seen: dict = {}

    class _Runner:
        async def exec_command(self, **kwargs):
            seen["exec"] = kwargs
            return models.ExecResponse(stdout="ok", stderr="", exit_code=0)

        async def start_execution(self, **kwargs):
            seen["start"] = kwargs
            return models.ExecutionStatusResponse(
                sandbox_id="sbx-1", execution_id="e1", status=models.ExecutionStatus.QUEUED, created_at=1.0,
            )

        async def side_channel_exec(self, **kwargs):
            seen["side"] = kwargs
            return models.ExecResponse(stdout="side", stderr="", exit_code=0)

    monkeypatch.setattr(routes, "_runner", _Runner())
    client = TestClient(
        service.app,
        raise_server_exceptions=False,
        headers={"X-Manor-Sandbox-Token": API_TOKEN},
    )
    ok = client.post(
        "/api/v1/sandbox/sbx-1/exec",
        json={"command": "manor ls", "env": {"MANOR_CLI_TOKEN": "tok"}},
    )
    assert ok.status_code == 200, ok.text
    assert seen["exec"]["env"] == {"MANOR_CLI_TOKEN": "tok"}

    started = client.post(
        "/api/v1/sandbox/sbx-1/exec/start",
        json={"command": "manor ls", "env": {"MANOR_CLI_TOKEN": "tok"}, "queue_timeout": 600},
    )
    assert started.status_code == 200, started.text
    assert seen["start"]["env"] == {"MANOR_CLI_TOKEN": "tok"}
    assert "tok" not in started.text, "the env is never echoed back on the record"

    side = client.post(
        "/api/v1/sandbox/sbx-1/exec",
        json={"command": "python3 -c job", "workdir": "/tmp", "timeout": 120, "side_channel": True},
    )
    assert side.status_code == 200, side.text
    assert seen["side"] == {"sandbox_id": "sbx-1", "command": "python3 -c job", "timeout": 120, "workdir": "/tmp"}

    # The side channel takes no credentials and never queues; it is synchronous only.
    for body, path in (
        ({"command": "x", "side_channel": True, "env": {"MANOR_CLI_TOKEN": "tok"}}, "exec"),
        ({"command": "x", "side_channel": True, "queue_timeout": 600}, "exec"),
        ({"command": "x", "side_channel": True}, "exec/start"),
    ):
        refused = client.post(f"/api/v1/sandbox/sbx-1/{path}", json=body)
        assert refused.status_code == 400, refused.text
        assert refused.json()["code"] == "invalid_request"
        assert "tok" not in refused.text


@pytest.mark.asyncio
async def test_a_started_exec_hands_its_env_to_that_one_command_and_keeps_it_off_the_record(monkeypatch):
    """The runner's background task takes the env; the record and its fingerprint never do."""

    skill_runner = pytest.importorskip("sandbox.skill_runner")
    runner = skill_runner.SkillRunner.__new__(skill_runner.SkillRunner)
    captured: dict = {}

    class _Sandbox:
        max_concurrent_executions = 1

        def ensure_available(self):
            return None

        def enqueue_command(self, *args, **kwargs):
            import asyncio

            future = asyncio.get_running_loop().create_future()
            future.set_result(None)
            return future

        def queue_position(self, _execution_id):
            return None

        def release_command(self, _execution_id):
            return None

    runner._execution_records = {}
    runner._execution_request_fingerprints = {}
    runner._execution_locks = {}
    runner._execution_tasks = {}
    runner._execution_cancel_requested = set()
    runner._execution_cancel_confirmed = set()
    runner._execution_admission_lock = __import__("asyncio").Lock()
    sandbox = _Sandbox()
    runner._sandboxes = {"sbx-1": sandbox}

    async def _no_cleanup(_ids):
        return None

    async def _no_prune():
        return {}

    runner._cleanup_execution_mailboxes = _no_cleanup
    runner._prune_execution_history = _no_prune

    async def background(key, **kwargs):
        captured.update(kwargs)

    runner._run_background_execution = background

    view = await runner._start_execution(
        sandbox, "sbx-1", "manor ls", timeout=30, workdir="/tmp", execution_id="e1",
        queue_timeout=60, env={"MANOR_CLI_TOKEN": "tok"},
    )
    await runner._execution_tasks[("sbx-1", "e1")]
    assert captured["env"] == {"MANOR_CLI_TOKEN": "tok"}, "the command's own task gets the env"
    assert "tok" not in view.model_dump_json()
    record = runner._execution_records[("sbx-1", "e1")]
    assert "tok" not in record.model_dump_json()
    assert "tok" not in runner._execution_request_fingerprints[("sbx-1", "e1")]
    # The same command re-started with a fresh token is the same request (idempotent).
    again = await runner._start_execution(
        sandbox, "sbx-1", "manor ls", timeout=30, workdir="/tmp", execution_id="e1",
        queue_timeout=60, env={"MANOR_CLI_TOKEN": "other"},
    )
    assert again.execution_id == "e1"


def test_health_reports_per_exec_env_support():
    source = (ROOT / "sandbox-service" / "main.py").read_text(encoding="utf-8")
    assert '"exec_env": True' in source
    assert '"exec_start_env": True' in source
    assert '"exec_side_channel": True' in source
