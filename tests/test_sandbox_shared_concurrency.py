"""One sandbox, much work: concurrent commands in a user's shared sandbox.

A user's sandbox is shared by all of the user's conversations, tasks and
agents, so the runner runs up to ``max_concurrent_executions`` commands at
once, each under its own execution id (its own pid group, cwd, env, output
and cancellation):

* N commands run together and each gets its own output and cwd;
* command N+1 answers the typed ``sandbox_busy`` (409) at once when its
  caller cannot wait (no ``queue_timeout``; the fair FIFO is
  tests/test_sandbox_per_user_runner.py);
* loading a Skill claims only that Skill's directory: busy while commands of
  the same Skill run, never because another Skill's command runs;
* cancelling one execution leaves the others running;
* a command of an agent that did not invoke the loaded Skill runs without
  the Skill's env (``skill_env=False``), and a per-agent directory under the
  shared ``/workspace`` is created on demand.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from tests.sandbox_runner_fakes import (
    API_TOKEN,
    completed,
    load_service_main,
    register_sandbox,
)

import sandbox.docker_backend as docker_backend  # noqa: E402
from sandbox.docker_backend import DockerSandbox, _memory_mib  # noqa: E402
from sandbox.errors import ExecutionConflict, SandboxBusy  # noqa: E402
from sandbox.models import ContainerConfig, SandboxStatus  # noqa: E402
from sandbox.skill_runner import SkillRunner  # noqa: E402


class ConcurrentDocker:
    """A Docker CLI fake whose launcher-wrapped commands block per command.

    Each command blocks until its own event is set (``release(command)``) or
    its own process group is signalled; pids are per execution so a cancel
    only ever reaches the command it names.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started: dict[str, threading.Event] = {}
        self.released: dict[str, threading.Event] = {}
        self.killed: dict[str, threading.Event] = {}
        self.pid_by_file: dict[str, int] = {}
        self.command_by_pid: dict[int, str] = {}
        self.env_files: dict[str, bool] = {}
        self.cwds: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self._next_pid = 400

    def _events(self, command: str) -> tuple[threading.Event, threading.Event, threading.Event]:
        with self.lock:
            return (
                self.started.setdefault(command, threading.Event()),
                self.released.setdefault(command, threading.Event()),
                self.killed.setdefault(command, threading.Event()),
            )

    def release(self, command: str) -> None:
        self._events(command)[1].set()

    def __call__(self, args, *, input_data=None, timeout=120, allow_failure=False):
        with self.lock:
            self.calls.append(list(args))
        verb = args[1]
        if verb == "inspect":
            return completed(args, 0, stdout=b"true\n")
        if verb in {"rm", "ps"}:
            return completed(args)
        joined = " ".join(args)
        if "start_new_session=True" in joined:
            return self._launcher(args)
        command = args[-1]
        if command.startswith("cat /tmp/manor-exec"):
            pid_file = command.split()[-1]
            with self.lock:
                pid = self.pid_by_file.get(pid_file)
            if pid is None:
                return completed(args, 1, stderr=b"cat: no such file")
            return completed(args, 0, stdout=str(pid).encode())
        if command.startswith("kill -0"):
            return completed(args, 1)
        if command.startswith("kill -"):
            pid = int(command.split("-")[-1])
            with self.lock:
                target = self.command_by_pid.get(pid)
            if target is not None:
                self._events(target)[2].set()
            return completed(args)
        return completed(args)

    def _launcher(self, args):
        index = args.index("-c")
        command, pid_file = args[index + 2], args[index + 3]
        cwd = args[index + 10] if len(args) > index + 10 else args[args.index("-w") + 1]
        with self.lock:
            self._next_pid += 1
            pid = self._next_pid
            self.pid_by_file[pid_file] = pid
            self.command_by_pid[pid] = command
            self.env_files[command] = "--env-file" in args
            self.cwds[command] = cwd
        started, released, killed = self._events(command)
        started.set()
        deadline = time.monotonic() + 10
        while not (released.is_set() or killed.is_set()):
            if time.monotonic() > deadline:
                raise AssertionError(f"command {command!r} was never released")
            time.sleep(0.005)
        if killed.is_set():
            return completed(args, 143)
        return completed(args, 0, stdout=f"out:{command}\n".encode())


@pytest.fixture
def docker(monkeypatch):
    fake = ConcurrentDocker()
    monkeypatch.setattr(docker_backend, "_run_docker", fake)
    monkeypatch.setattr(docker_backend, "_CANCEL_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(docker_backend, "_CANCEL_POLL_INTERVAL_SECONDS", 0.005)
    return fake


async def _started(docker: ConcurrentDocker, command: str) -> None:
    event = docker._events(command)[0]
    assert await asyncio.to_thread(event.wait, 5), f"{command} never started"


async def _runner_with_sandbox(limit: int = 4):
    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    sandbox.config = ContainerConfig(max_concurrent_executions=limit, pids_limit=512, memory="1g")
    sandbox.status = SandboxStatus.READY
    return runner, sandbox


# ── the claim state machine ──


def test_cap_is_bounded_by_the_containers_pids_and_memory() -> None:
    assert DockerSandbox("a", ContainerConfig(max_concurrent_executions=4)).max_concurrent_executions == 4
    # 64 pids leave room for 2 commands of 32 pids each.
    assert DockerSandbox("b", ContainerConfig(max_concurrent_executions=8, pids_limit=64)).max_concurrent_executions == 2
    # 512 MiB leaves room for 2 commands of 256 MiB each; 1 GiB / 512 pids for 4.
    assert DockerSandbox("c", ContainerConfig(max_concurrent_executions=8, memory="512m")).max_concurrent_executions == 2
    assert DockerSandbox("e", ContainerConfig(max_concurrent_executions=8, memory="1g", pids_limit=512)).max_concurrent_executions == 4
    assert DockerSandbox("f", ContainerConfig(max_concurrent_executions=8, memory="256m")).max_concurrent_executions == 1
    assert DockerSandbox("d", ContainerConfig(max_concurrent_executions=1)).max_concurrent_executions == 1
    assert (_memory_mib("2g"), _memory_mib("512m"), _memory_mib("1073741824"), _memory_mib("x")) == (2048, 512, 1024, 0)


def test_claims_fill_the_cap_then_answer_busy_and_free_one_slot_at_a_time() -> None:
    sandbox = DockerSandbox("s", ContainerConfig(max_concurrent_executions=3))
    sandbox.status = SandboxStatus.READY
    for index in range(3):
        sandbox.claim_command(f"e{index}", f"cmd {index}", 60)
    assert sandbox.status is SandboxStatus.EXECUTING
    assert sandbox.active_execution_ids == ["e0", "e1", "e2"]
    with pytest.raises(SandboxBusy) as busy:
        sandbox.claim_command("e3", "cmd 3", 60)
    assert busy.value.code == "sandbox_busy" and busy.value.status_code == 409
    assert "3 command(s) (limit 3)" in str(busy.value)
    with pytest.raises(ExecutionConflict):
        sandbox.claim_command("e1", "again", 60)

    sandbox.release_command("e1")
    assert sandbox.status is SandboxStatus.EXECUTING
    sandbox.claim_command("e3", "cmd 3", 60)
    for execution_id in ("e0", "e2", "e3"):
        sandbox.release_command(execution_id)
    assert sandbox.status is SandboxStatus.READY
    assert sandbox.busy_deadline is None


def test_busy_deadline_is_the_earliest_running_commands() -> None:
    sandbox = DockerSandbox("s", ContainerConfig(max_concurrent_executions=2))
    sandbox.status = SandboxStatus.READY
    sandbox.claim_command("long", "sleep 900", 900)
    sandbox.claim_command("short", "true", 10)
    short_deadline = sandbox._executions["short"][2]
    assert sandbox.busy_deadline == short_deadline


@pytest.mark.asyncio
async def test_a_skill_load_is_busy_only_for_its_own_skill() -> None:
    sandbox = DockerSandbox("s", ContainerConfig(max_concurrent_executions=2))
    sandbox.status = SandboxStatus.READY
    sandbox.last_used_at = time.time() - 600
    sandbox.claim_command("e0", "python work.py", 60, slot="pptx")
    with pytest.raises(SandboxBusy) as busy:
        await sandbox.load_skill("pptx", {"SKILL.md": "x"}, idle_threshold=0)
    assert "of Skill 'pptx' are running" in str(busy.value)
    sandbox.release_command("e0")
    # A command of a Skill being loaded waits for the load; another Skill's does not.
    sandbox.claim_skill_load("pptx", 10)
    with pytest.raises(SandboxBusy) as busy:
        sandbox.claim_command("e1", "ls", 60, slot="pptx")
    assert "is being loaded" in str(busy.value)
    sandbox.claim_command("e2", "ls", 60, slot="docx")
    sandbox.claim_command("e3", "ls", 60)
    sandbox.release_skill_load("pptx")
    # Another Skill loads while docx's command runs.
    sandbox.claim_skill_load("xlsx", 10)
    sandbox.release_skill_load("xlsx")
    # The legacy whole-sandbox claim still excludes everything.
    sandbox.release_command("e2")
    sandbox.release_command("e3")
    sandbox._claim(SandboxStatus.RUNNING, budget_seconds=10, command="load skill pptx")
    with pytest.raises(SandboxBusy) as busy:
        sandbox.claim_command("e4", "ls", 60)
    assert "loading a skill" in str(busy.value)


# ── the runner: N concurrent commands, N+1 busy, cancel one ──


@pytest.mark.asyncio
async def test_concurrent_commands_get_their_own_output_and_cwd(docker) -> None:
    runner, sandbox = await _runner_with_sandbox(limit=3)
    commands = [f"agent-{index} work" for index in range(3)]
    tasks = [
        asyncio.create_task(
            runner.exec_command(
                "sbx-1",
                command,
                timeout=30,
                workdir=f"/workspace/agents/agent-{index}",
                execution_id=f"exec-{index}",
                skill_env=index != 0,
            )
        )
        for index, command in enumerate(commands)
    ]
    for command in commands:
        await _started(docker, command)
    assert sorted(sandbox.active_execution_ids) == ["exec-0", "exec-1", "exec-2"]

    # Command N+1 answers busy at once, typed.
    with pytest.raises(SandboxBusy):
        await runner.exec_command("sbx-1", "one too many", timeout=30, execution_id="exec-3")

    for command in reversed(commands):
        docker.release(command)
    results = await asyncio.gather(*tasks)
    assert [result.stdout for result in results] == [f"out:{command}\n" for command in commands]
    assert [result.execution_id for result in results] == ["exec-0", "exec-1", "exec-2"]
    assert docker.cwds == {command: f"/workspace/agents/agent-{index}" for index, command in enumerate(commands)}
    # The agent that did not invoke the loaded Skill ran without its env.
    assert docker.env_files == {commands[0]: False, commands[1]: True, commands[2]: True}
    assert sandbox.status is SandboxStatus.READY and sandbox.active_execution_ids == []


@pytest.mark.asyncio
async def test_cancelling_one_execution_leaves_the_others_running(docker) -> None:
    runner, sandbox = await _runner_with_sandbox(limit=3)
    keep = asyncio.create_task(runner.exec_command("sbx-1", "keep running", timeout=30, execution_id="keep"))
    stop = asyncio.create_task(runner.exec_command("sbx-1", "stop me", timeout=30, execution_id="stop"))
    background = await runner.start_execution("sbx-1", "background job", timeout=30, execution_id="bg")
    await _started(docker, "keep running")
    await _started(docker, "stop me")
    await _started(docker, "background job")
    assert background.execution_id == "bg"

    assert await runner.cancel_execution("sbx-1", "stop") is True
    stopped = await stop
    assert stopped.exit_code == 143
    assert not keep.done()
    assert sorted(sandbox.active_execution_ids) == ["bg", "keep"]

    docker.release("keep running")
    docker.release("background job")
    assert (await keep).stdout == "out:keep running\n"
    for _ in range(500):
        status = await runner.get_execution_status("sbx-1", "bg")
        if status.status.value == "completed":
            break
        await asyncio.sleep(0.01)
    assert status.status.value == "completed" and status.stdout == "out:background job\n"
    assert sandbox.status is SandboxStatus.READY


@pytest.mark.asyncio
async def test_load_skill_answers_busy_over_http_while_commands_run(monkeypatch, docker) -> None:
    main = load_service_main(monkeypatch, "sandbox_main_shared_concurrency")
    runner = main.runner
    sandbox, _lease = await register_sandbox(runner)
    sandbox.config = ContainerConfig(max_concurrent_executions=1)
    sandbox.status = SandboxStatus.READY
    sandbox.claim_command("running", "python long.py", 60, slot="demo")
    headers = {"X-Manor-Sandbox-Token": API_TOKEN}
    with TestClient(main.app) as client:
        loaded = client.post(
            "/api/v1/sandbox/sbx-1/load-skill",
            json={"skill_name": "demo", "files": {"SKILL.md": "x"}, "idle_threshold": 0},
            headers=headers,
        )
        assert loaded.status_code == 409 and loaded.json()["code"] == "sandbox_busy"
        extra = client.post(
            "/api/v1/sandbox/sbx-1/exec",
            json={"command": "ls", "timeout": 5, "execution_id": "second"},
            headers=headers,
        )
        assert extra.status_code == 409 and extra.json()["code"] == "sandbox_busy"


def test_launcher_creates_a_shared_workspace_directory_but_never_escapes_it() -> None:
    sandbox = DockerSandbox("s", ContainerConfig())
    sandbox.container_name = "c"
    args = sandbox._execution_command_args("pwd", "/workspace/c/conv/agents/a1", "/tmp/p", "e", False)
    # docker starts in / and the launcher changes into the (created) directory.
    assert args[args.index("-w") + 1] == "/"
    index = args.index("-c")
    assert args[index + 10:index + 12] == ["/workspace/c/conv/agents/a1", "1"]
    for path in ("/workspace/../etc", "/skills/x", "/workspace", "/workspace//x"):
        plain = sandbox._execution_command_args("pwd", path, "/tmp/p", "e", False)
        assert plain[index + 10:index + 12] == [path, "0"]


def test_new_containers_mount_the_shared_workspace() -> None:
    sandbox = DockerSandbox("s", ContainerConfig(workspace_tmpfs_size="128m"))
    sandbox.container_name = "c"
    args = sandbox._build_create_args({})
    assert "/workspace:exec,size=128m,mode=1777" in args
    assert "/workspace:exec" not in " ".join(DockerSandbox("t", ContainerConfig(workspace_tmpfs_size=""))._build_create_args({}))


def test_the_launcher_runs_and_starts_in_a_created_agent_directory(tmp_path) -> None:
    """The launcher is code shipped inside a string: compile and run it for real.

    (A real-Docker run caught an escape that turned the launcher into a
    SyntaxError while every fake-based test still passed.)
    """

    import subprocess
    import sys

    sandbox = DockerSandbox("s", ContainerConfig())
    sandbox.container_name = "c"
    for workdir in ("/skills/demo", "/workspace/c/conv/agents/a"):
        args = sandbox._execution_command_args("pwd", workdir, "/tmp/p", "e", True)
        compile(args[args.index("-c") + 1], "<launcher>", "exec")

    args = sandbox._execution_command_args(
        "pwd; echo $SKILL_DIR $MANOR_EXEC_ID", "/workspace/c/conv/agents/a", "/tmp/p", "e-1", False,
        skill_home="/skills/demo",
    )
    launcher_argv = args[args.index("-c") + 1:]
    agent_dir = tmp_path / "workspace" / "agents" / "a"
    launcher_argv[2] = str(tmp_path / "pid")
    launcher_argv[3] = str(tmp_path / "events")
    launcher_argv[4] = str(tmp_path / "responses")
    launcher_argv[5] = str(tmp_path / "bridge")
    launcher_argv[9] = str(agent_dir)
    launcher_argv[11] = str(tmp_path / "verdict.json")
    result = subprocess.run([sys.executable, "-c", *launcher_argv], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [str(agent_dir), "/skills/demo", "e-1"]
    assert agent_dir.is_dir()
