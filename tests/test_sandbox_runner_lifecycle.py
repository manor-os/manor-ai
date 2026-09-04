from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SANDBOX_SERVICE_ROOT = Path(__file__).resolve().parents[1] / "sandbox-service"
sys.path.insert(0, str(SANDBOX_SERVICE_ROOT))

from sandbox.models import ContainerConfig, SandboxStatus  # noqa: E402
from sandbox.skill_runner import SkillRunner  # noqa: E402
from sandbox.docker_backend import DockerSandbox  # noqa: E402


@pytest.mark.asyncio
async def test_idempotent_create_deduplicates_and_reopens_after_destroy(monkeypatch, tmp_path):
    import sandbox.skill_runner as skill_runner_module

    skill_dir = tmp_path / "idempotent-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Idempotent Skill\n", encoding="utf-8")

    setup_started = asyncio.Event()
    allow_first_setup = asyncio.Event()
    setup_calls = 0

    class FakeDockerSandbox:
        def __init__(self, sandbox_id, config):
            self.sandbox_id = sandbox_id
            self.config = config
            self.container_name = f"test-{sandbox_id}"
            self.status = SandboxStatus.CREATING
            self.skill = None

        async def setup(self, skill, env, auto_install=True):
            nonlocal setup_calls
            setup_calls += 1
            if setup_calls == 1:
                setup_started.set()
                await allow_first_setup.wait()
            self.skill = skill
            self.status = SandboxStatus.READY

        async def destroy(self):
            self.status = SandboxStatus.DESTROYED

    monkeypatch.setattr(skill_runner_module.app_config, "MAX_SANDBOXES", 2)
    monkeypatch.setattr(skill_runner_module, "DockerSandbox", FakeDockerSandbox)

    runner = SkillRunner()
    first_task = asyncio.create_task(
        runner.create_sandbox(
            str(skill_dir),
            env={},
            auto_install=False,
            idempotency_key="reservation-1",
        )
    )
    await setup_started.wait()
    follower_task = asyncio.create_task(
        runner.create_sandbox(
            str(skill_dir),
            env={},
            auto_install=False,
            idempotency_key="reservation-1",
        )
    )
    await asyncio.sleep(0)
    allow_first_setup.set()

    first, follower = await asyncio.gather(first_task, follower_task)

    assert first.sandbox_id == follower.sandbox_id
    assert setup_calls == 1
    assert runner._local_active_capacity.reserved == 1

    await runner.destroy_sandbox(first.sandbox_id)
    recreated = await runner.create_sandbox(
        str(skill_dir),
        env={},
        auto_install=False,
        idempotency_key="reservation-1",
    )
    other = await runner.create_sandbox(
        str(skill_dir),
        env={},
        auto_install=False,
        idempotency_key="reservation-2",
    )

    assert recreated.sandbox_id == first.sandbox_id
    assert other.sandbox_id != first.sandbox_id
    assert setup_calls == 3
    assert runner._local_active_capacity.reserved == 2


@pytest.mark.asyncio
async def test_execution_cancel_terminates_active_process_group_only_when_needed(monkeypatch):
    import sandbox.docker_backend as docker_backend_module

    execution_started = threading.Event()
    allow_execution_finish = threading.Event()
    signals: list[str] = []
    stubborn = True

    def fake_run_docker(args, **kwargs):
        nonlocal stubborn
        command = args[-1]
        if "start_new_session=True" in " ".join(args):
            execution_started.set()
            assert allow_execution_finish.wait(timeout=1)
            return subprocess.CompletedProcess(args, 137, stdout=b"", stderr=b"killed")
        if command.startswith("cat "):
            return subprocess.CompletedProcess(args, 0, stdout=b"321\n", stderr=b"")
        if "kill -TERM" in command:
            signals.append("TERM")
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if "kill -0" in command:
            return subprocess.CompletedProcess(
                args,
                0 if stubborn else 1,
                stdout=b"",
                stderr=b"",
            )
        if "kill -KILL" in command:
            signals.append("KILL")
            allow_execution_finish.set()
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if command.startswith("rm -f "):
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"Unexpected Docker command: {args}")

    monkeypatch.setattr(docker_backend_module, "_run_docker", fake_run_docker)
    monkeypatch.setattr(docker_backend_module, "_CANCEL_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(docker_backend_module, "_CANCEL_POLL_INTERVAL_SECONDS", 0.001)

    sandbox = DockerSandbox("sandbox-1", ContainerConfig())
    sandbox.container_name = "test-sandbox-1"
    sandbox.status = SandboxStatus.READY
    execution_task = asyncio.create_task(
        sandbox.exec("sleep 60", execution_id="execution-1")
    )
    assert await asyncio.to_thread(execution_started.wait, 1) is True

    assert await sandbox.cancel_execution("execution-1") is True
    result = await execution_task

    assert result.execution_id == "execution-1"
    assert signals == ["TERM", "KILL"]
    assert await sandbox.cancel_execution("execution-1") is False
    assert sandbox.active_execution_id is None

    signals.clear()
    stubborn = False
    sandbox._active_execution_id = "execution-2"
    assert await sandbox.cancel_execution("execution-2") is True
    assert signals == ["TERM"]


@pytest.mark.asyncio
async def test_startup_reconciles_unaddressable_managed_containers(monkeypatch):
    import sandbox.skill_runner as skill_runner_module

    removed: list[str] = []
    monkeypatch.setattr(
        skill_runner_module,
        "list_managed_sandbox_containers",
        lambda: ["container-a", "container-b"],
        raising=False,
    )
    monkeypatch.setattr(
        skill_runner_module,
        "remove_managed_sandbox_container",
        removed.append,
        raising=False,
    )

    runner = SkillRunner()
    await runner.startup()
    try:
        assert removed == ["container-a", "container-b"]
        assert runner.runtime_status()["orphan_cleanup_failures"] == []
    finally:
        await runner.shutdown()


@pytest.mark.asyncio
async def test_capacity_status_reports_stable_local_counters(monkeypatch):
    import sandbox.skill_runner as skill_runner_module

    monkeypatch.setattr(skill_runner_module.app_config, "MAX_SANDBOXES", 5)
    monkeypatch.setattr(skill_runner_module.app_config, "INSTANCE_MAX_EXECUTING", 2)
    monkeypatch.setattr(skill_runner_module, "_image_exists_locally", lambda _image: True)

    runner = SkillRunner()
    execute_lease = await runner._gate.acquire_execute()

    assert runner.runtime_status() == {
        "sandbox_image": skill_runner_module.app_config.SANDBOX_IMAGE,
        "sandbox_image_available": True,
        "active_sandboxes": 0,
        "reserved_active": 0,
        "max_active": 5,
        "executing": 1,
        "max_executing": 2,
        "orphan_cleanup_failures": [],
    }

    await execute_lease.release()
    assert runner.runtime_status()["executing"] == 0


def test_capacity_status_uses_active_limit_when_execute_limit_is_disabled(monkeypatch):
    import sandbox.skill_runner as skill_runner_module

    monkeypatch.setattr(skill_runner_module.app_config, "MAX_SANDBOXES", 5)
    monkeypatch.setattr(skill_runner_module.app_config, "INSTANCE_MAX_EXECUTING", 0)
    monkeypatch.setattr(skill_runner_module, "_image_exists_locally", lambda _image: True)

    runner = SkillRunner()

    assert runner.runtime_status()["max_executing"] == 5
