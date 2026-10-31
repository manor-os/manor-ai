"""The Sandbox runner's liveness and state-machine contracts.

1. A container Docker reports gone is never a command result: the sandbox is
   marked lost, reclaimed (registry, capacity, lease, open executions) and
   every later request for its id answers 404.
2. ``load_skill`` is all-or-nothing, and no sandbox can sit in a state that
   no background job reclaims.
3. "Busy" is decided immediately, never behind a lock held by a running
   command; side-channel file operations work next to a running command; a
   running synchronous exec is cancellable by its execution id.
6. destroy always proceeds to removal.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from tests.sandbox_runner_fakes import (
    API_TOKEN,
    FakeDocker,
    completed,
    describe_app_routes,
    load_service_main,
    register_sandbox,
)

import sandbox.docker_backend as docker_backend  # noqa: E402
import sandbox.skill_runner as skill_runner_module  # noqa: E402
from sandbox.docker_backend import DockerSandbox  # noqa: E402
from sandbox.errors import (  # noqa: E402
    CancelFailed,
    CommandTimeout,
    DependencyInstallFailed,
    DockerUnavailable,
    ExecutionNotFound,
    LoadSkillFailed,
    SandboxBusy,
    SandboxGone,
    SandboxLost,
)
from sandbox.models import (  # noqa: E402
    ContainerConfig,
    ExecutionStatus,
    ExecutionStatusResponse,
    SandboxStatus,
)
from sandbox.skill_runner import SkillRunner  # noqa: E402


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(docker_backend, "_run_docker", fake)
    monkeypatch.setattr(docker_backend, "_CANCEL_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(docker_backend, "_CANCEL_POLL_INTERVAL_SECONDS", 0.005)
    return fake


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def _assert_reclaimed(runner: SkillRunner, sandbox_id: str, lease) -> None:
    assert sandbox_id not in runner._sandboxes
    assert sandbox_id not in runner._fs_bridges
    assert sandbox_id not in runner._active_leases
    assert runner._local_active_capacity.reserved == 0
    assert sandbox_id not in runner._memory_reservations
    assert runner._memory.reserved == 0
    assert lease.released == 1


# ── 1. a lost container ──


@pytest.mark.asyncio
async def test_daemon_failure_is_never_returned_as_a_command_exit_code(docker):
    sandbox = DockerSandbox("sbx-1", ContainerConfig())
    sandbox.container_name = "skill-sbx-demo"
    sandbox.status = SandboxStatus.READY
    docker.running = False

    with pytest.raises(SandboxLost) as exc_info:
        await sandbox.exec("echo hi", timeout=5)

    assert exc_info.value.status_code == 404
    assert sandbox.is_lost
    assert sandbox.status == SandboxStatus.ERROR
    # Every later operation reports the loss without asking Docker again.
    calls = len(docker.calls)
    with pytest.raises(SandboxLost):
        sandbox.claim_command("x", "echo", 5)
    with pytest.raises(SandboxLost):
        await sandbox.side_exec("cat x", timeout=5)
    assert len(docker.calls) == calls


@pytest.mark.asyncio
async def test_command_output_that_mimics_daemon_text_stays_the_commands_result(docker):
    """The hint is only a hint: the inspect decides, so a script cannot fake a loss."""
    sandbox = DockerSandbox("sbx-1", ContainerConfig())
    sandbox.container_name = "skill-sbx-demo"
    sandbox.status = SandboxStatus.READY
    docker.launcher_result = (1, b"", b"Error response from daemon: container x is not running")

    result = await sandbox.exec("echo pretend", timeout=5)

    assert result.exit_code == 1
    assert not sandbox.is_lost
    assert sandbox.status == SandboxStatus.READY


@pytest.mark.asyncio
async def test_unreachable_daemon_is_an_infrastructure_error_not_an_exit_code(monkeypatch):
    def daemon_down(args, **_kwargs):
        return docker_backend.subprocess.CompletedProcess(
            args, 1, stdout=b"", stderr=b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
        )

    monkeypatch.setattr(docker_backend, "_run_docker", daemon_down)
    sandbox = DockerSandbox("sbx-1", ContainerConfig())
    sandbox.container_name = "skill-sbx-demo"
    sandbox.status = SandboxStatus.READY

    with pytest.raises(DockerUnavailable) as exc_info:
        await sandbox.exec("echo hi", timeout=5)

    assert exc_info.value.status_code == 503
    assert not sandbox.is_lost


@pytest.mark.asyncio
async def test_lost_container_is_reclaimed_and_fails_in_flight_executions(docker):
    runner = SkillRunner()
    sandbox, lease = await register_sandbox(runner)
    docker.block = True
    background = await runner.start_execution(
        "sbx-1", "python long.py", execution_id="bg-1"
    )
    record = runner._execution_records[("sbx-1", "bg-1")]
    await asyncio.to_thread(docker.launcher_started.wait, 5)

    # The container dies under the running command.
    docker.running = False
    docker.exists = False
    docker.killed.set()
    await _wait_until(lambda: "sbx-1" not in runner._sandboxes)

    assert background.status == ExecutionStatus.QUEUED
    assert record.status == ExecutionStatus.FAILED
    assert "gone" in (record.error or "") or "lost" in (record.error or "")
    _assert_reclaimed(runner, "sbx-1", lease)
    with pytest.raises(SandboxGone):
        await runner.get_execution_status("sbx-1", "bg-1")


@pytest.mark.asyncio
async def test_reaper_sweep_reclaims_a_container_docker_no_longer_runs(docker, monkeypatch):
    runner = SkillRunner()
    _sandbox, lease = await register_sandbox(runner)
    monkeypatch.setattr(
        skill_runner_module, "list_running_managed_container_names", lambda _scope: set()
    )
    docker.exists = False
    docker.running = False

    await runner._sweep_lost_containers()

    _assert_reclaimed(runner, "sbx-1", lease)


@pytest.mark.asyncio
async def test_sweep_never_reclaims_a_container_the_inspect_confirms_running(docker, monkeypatch):
    runner = SkillRunner()
    await register_sandbox(runner)
    # Listed before the container appeared: the inspect is authoritative.
    monkeypatch.setattr(
        skill_runner_module, "list_running_managed_container_names", lambda _scope: set()
    )

    await runner._sweep_lost_containers()

    assert "sbx-1" in runner._sandboxes
    assert runner._local_active_capacity.reserved == 1


@pytest.mark.asyncio
async def test_lost_sandbox_whose_removal_fails_still_answers_404_and_is_retried(docker):
    runner = SkillRunner()
    _sandbox, lease = await register_sandbox(runner)
    docker.running = False
    docker.rm_fails = True

    with pytest.raises(SandboxLost):
        await runner.exec_command("sbx-1", "echo hi")

    # Not removable yet: capacity stays held, but the id is already gone to callers.
    assert "sbx-1" in runner._sandboxes
    with pytest.raises(SandboxGone):
        runner.touch_sandbox("sbx-1")
    with pytest.raises(SandboxGone):
        await runner.read_file("sbx-1", "/skill/a.txt")
    assert runner.list_sandboxes() == []

    docker.rm_fails = False
    await runner._prune_idle()
    _assert_reclaimed(runner, "sbx-1", lease)


@pytest.mark.asyncio
async def test_idempotent_create_whose_sandbox_was_lost_is_a_retryable_conflict(docker):
    """The id is derived from the key, so a re-create must wait for the reclaim
    (409), never answer a create with 404 or overwrite the lost registration."""
    from types import SimpleNamespace

    from sandbox.errors import SandboxUnavailable

    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    runner._idempotency_completed["key-1"] = SimpleNamespace(sandbox_id="sbx-1")
    sandbox._mark_lost("container no longer exists")

    with pytest.raises(SandboxUnavailable) as exc_info:
        await runner._prepare_idempotent_create("key-1")
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_terminal_execution_status_is_served_without_touching_docker(monkeypatch):
    def no_docker(*_args, **_kwargs):
        raise AssertionError("a terminal record must be served from memory")

    monkeypatch.setattr(docker_backend, "_run_docker", no_docker)
    runner = SkillRunner()
    await register_sandbox(runner)
    key = ("sbx-1", "done-1")
    runner._execution_records[key] = ExecutionStatusResponse(
        sandbox_id="sbx-1",
        execution_id="done-1",
        status=ExecutionStatus.COMPLETED,
        created_at=1.0,
        finished_at=2.0,
        exit_code=0,
        stdout="ok",
    )
    runner._execution_locks[key] = asyncio.Lock()

    for _ in range(3):
        status = await runner.get_execution_status("sbx-1", "done-1")
        assert status.status == ExecutionStatus.COMPLETED
        assert status.stdout == "ok"


@pytest.mark.asyncio
async def test_background_execution_reads_its_final_events_before_turning_terminal(docker):
    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    reads: list[str] = []

    async def read_events(execution_id):
        reads.append(runner._execution_records[("sbx-1", execution_id)].status.value)
        return ['{"event_id":"r1","type":"result","message":"done","payload":{}}'], False

    sandbox.read_execution_event_lines = read_events
    await runner.start_execution("sbx-1", "python job.py", execution_id="bg-final")
    await runner._execution_tasks[("sbx-1", "bg-final")]

    assert reads == ["running"]
    status = await runner.get_execution_status("sbx-1", "bg-final")
    assert status.status == ExecutionStatus.COMPLETED
    assert [event.event_id for event in status.events] == ["r1"]
    assert reads == ["running"], "terminal polls never read the container"


# ── 3. busy is immediate; side channel and cancel work while a command runs ──


@pytest.mark.asyncio
async def test_busy_is_answered_immediately_and_files_work_while_a_command_runs(docker):
    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    # One command slot: the running command fills the sandbox (concurrent
    # commands up to the cap: tests/test_sandbox_shared_concurrency.py).
    sandbox.config = ContainerConfig(max_concurrent_executions=1)
    docker.block = True
    running = asyncio.create_task(
        runner.exec_command("sbx-1", "python long.py", timeout=600, execution_id="sync-1")
    )
    await asyncio.to_thread(docker.launcher_started.wait, 5)
    assert sandbox.status == SandboxStatus.EXECUTING

    async def immediately(coro):
        return await asyncio.wait_for(coro, timeout=1.0)

    with pytest.raises(SandboxBusy):
        await immediately(runner.exec_command("sbx-1", "echo second"))
    with pytest.raises(SandboxBusy):
        await immediately(runner.start_execution("sbx-1", "echo bg", execution_id="bg-2"))
    # Reloading the Skill whose command runs is busy (another Skill would load
    # beside it: tests/test_sandbox_per_user_layout.py).
    with pytest.raises(SandboxBusy):
        await immediately(
            runner.load_skill_into_sandbox(
                "sbx-1", "demo", {"SKILL.md": "# Demo"}, auto_install=False, idle_threshold=0
            )
        )
    assert ("sbx-1", "bg-2") not in runner._execution_records

    before = sandbox.last_used_at = time.time() - 50
    read = await immediately(runner.read_file("sbx-1", "/skill/out.txt"))
    assert read.content == "file-content"
    assert sandbox.last_used_at > before
    sandbox.last_used_at = before
    await immediately(runner.write_file("sbx-1", "/skill/out.txt", "x", mkdir=False))
    assert sandbox.last_used_at > before, "a write without mkdir refreshes the lease too"
    await immediately(runner.write_file_base64("sbx-1", "/skill/a.bin", "eA=="))
    await immediately(runner.read_file_base64("sbx-1", "/skill/a.bin"))
    context = await immediately(runner.get_skill_context("sbx-1"))
    assert context.file_contents == {"run.py": "file-content"}
    assert sandbox.status == SandboxStatus.EXECUTING

    docker.release.set()
    result = await running
    assert result.exit_code == 0
    assert sandbox.status == SandboxStatus.READY
    assert sandbox.active_execution_id is None


@pytest.mark.asyncio
async def test_cancel_finds_and_kills_a_running_synchronous_exec(docker):
    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    docker.block = True
    running = asyncio.create_task(
        runner.exec_command("sbx-1", "python long.py", timeout=600, execution_id="sync-1")
    )
    await asyncio.to_thread(docker.launcher_started.wait, 5)

    with pytest.raises(ExecutionNotFound):
        await runner.cancel_execution("sbx-1", "some-other-id")
    assert await runner.cancel_execution("sbx-1", "sync-1") is True
    result = await asyncio.wait_for(running, timeout=5)

    assert docker.signals == ["kill -TERM -321"]
    assert result.exit_code == 143
    assert sandbox.status == SandboxStatus.READY
    # It ended and was never recorded: nothing to cancel any more.
    with pytest.raises(ExecutionNotFound):
        await runner.cancel_execution("sbx-1", "sync-1")


@pytest.mark.asyncio
async def test_cancel_of_a_running_exec_whose_process_never_started_is_a_typed_failure(docker, monkeypatch):
    monkeypatch.setattr(docker_backend, "_EXECUTION_PID_WAIT_SECONDS", 0.02)
    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    sandbox.claim_command("sync-x", "python long.py", 60)

    async def no_pid(_execution_id, _pid_file):
        return None

    sandbox._wait_for_execution_pid = no_pid
    with pytest.raises(CancelFailed) as exc_info:
        await runner.cancel_execution("sbx-1", "sync-x")
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_destroy_kills_a_running_synchronous_exec_without_waiting_for_it(docker):
    runner = SkillRunner()
    _sandbox, lease = await register_sandbox(runner)
    docker.block = True
    running = asyncio.create_task(
        runner.exec_command("sbx-1", "python long.py", timeout=600, execution_id="sync-1")
    )
    await asyncio.to_thread(docker.launcher_started.wait, 5)

    await asyncio.wait_for(runner.destroy_sandbox("sbx-1"), timeout=5)

    _assert_reclaimed(runner, "sbx-1", lease)
    with pytest.raises(SandboxGone) as exc_info:
        await asyncio.wait_for(running, timeout=5)
    assert not isinstance(exc_info.value, SandboxLost), "a requested destroy is not a loss"


@pytest.mark.asyncio
async def test_destroy_proceeds_when_cancelling_an_execution_would_fail(docker):
    runner = SkillRunner()
    sandbox, lease = await register_sandbox(runner)
    docker.block = True

    async def broken_cancel(_execution_id):
        raise CancelFailed("container is not running")

    sandbox.cancel_execution = broken_cancel
    await runner.start_execution("sbx-1", "python long.py", execution_id="bg-1")
    record = runner._execution_records[("sbx-1", "bg-1")]
    await asyncio.to_thread(docker.launcher_started.wait, 5)

    await asyncio.wait_for(runner.destroy_sandbox("sbx-1"), timeout=5)

    _assert_reclaimed(runner, "sbx-1", lease)
    assert record.status in ExecutionStatus.terminal()
    assert runner._execution_tasks == {}


@pytest.mark.asyncio
async def test_timed_out_command_that_cannot_be_killed_destroys_its_sandbox(docker, monkeypatch):
    """A command that may still run must never share the sandbox with the next one."""
    from sandbox.docker_backend import DockerCommandTimeout

    monkeypatch.setattr(docker_backend, "_EXECUTION_PID_WAIT_SECONDS", 0.02)
    runner = SkillRunner()
    _sandbox, lease = await register_sandbox(runner)
    docker.launcher_timeout = DockerCommandTimeout(5, b"partial", b"")
    docker.pid_available = False

    with pytest.raises(CommandTimeout) as exc_info:
        await runner.exec_command("sbx-1", "python stuck.py", timeout=5, execution_id="t-1")

    assert exc_info.value.extra["sandbox_destroyed"] is True
    _assert_reclaimed(runner, "sbx-1", lease)


@pytest.mark.asyncio
async def test_timed_out_command_that_was_killed_keeps_its_sandbox(docker):
    from sandbox.docker_backend import DockerCommandTimeout

    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    docker.launcher_timeout = DockerCommandTimeout(5, b"partial", b"")

    with pytest.raises(CommandTimeout) as exc_info:
        await runner.exec_command("sbx-1", "python slow.py", timeout=5, execution_id="t-2")

    assert exc_info.value.extra["stdout"] == "partial"
    assert "sandbox_destroyed" not in exc_info.value.extra
    assert docker.signals == ["kill -TERM -321"]
    assert sandbox.status == SandboxStatus.READY


# ── 2. load_skill is all-or-nothing; the reaper covers every state ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,expected_error,status_code",
    [
        ("inject", LoadSkillFailed, 500),
        ("install_exit", DependencyInstallFailed, 422),
        ("install_timeout", CommandTimeout, 504),
    ],
)
async def test_failed_load_skill_discards_only_that_skill_and_keeps_the_shared_sandbox(
    docker, failure, expected_error, status_code
):
    """A Skill that fails to load is removed; the sandbox keeps serving everyone else.

    The sandbox is shared by all of a user's work, so one Skill's broken
    install must not destroy it (it used to: one Skill per container).
    """
    runner = SkillRunner()
    sandbox, lease = await register_sandbox(runner)
    if failure == "inject":
        docker.inject_fails = True
    elif failure == "install_exit":
        docker.install_result = (1, b"", b"No matching distribution found for nope")
    else:
        docker.install_timeout = True

    with pytest.raises(expected_error) as exc_info:
        await runner.load_skill_into_sandbox(
            "sbx-1",
            "other",
            {"SKILL.md": "# Other", "requirements.txt": "nope==0\n"},
            auto_install=True,
            idle_threshold=0,
            env={"OTHER_KEY": "new"},
        )

    assert exc_info.value.status_code == status_code
    assert "sandbox_destroyed" not in exc_info.value.extra
    if failure == "install_timeout":
        assert exc_info.value.code == "install_timeout"
    # The half-loaded Skill's directory was removed; nothing was committed.
    assert any(call[-2:] == ["--", "/skills/other"] and "rm" in call for call in docker.calls)
    assert "sbx-1" in runner._sandboxes and lease.released == 0
    assert sandbox.status == SandboxStatus.READY
    assert sandbox.skill.name == "demo"
    assert sandbox._skill_env == {"DEMO_KEY": "old"}
    assert sandbox.skills == ["demo"]
    assert sandbox._loading == {}


@pytest.mark.asyncio
async def test_failed_load_whose_directory_cannot_be_removed_destroys_the_sandbox(docker, monkeypatch):
    """Only a half-loaded Skill that cannot be removed takes the sandbox down."""
    runner = SkillRunner()
    sandbox, lease = await register_sandbox(runner)
    docker.inject_fails = True
    real = docker.__call__

    def failing_rm(args, **kwargs):
        if "rm" in args and args[-1] == "/skills/other":
            return completed(args, 1, stderr=b"rm: cannot remove: Device or resource busy")
        return real(args, **kwargs)

    monkeypatch.setattr(docker_backend, "_run_docker", failing_rm)
    with pytest.raises(LoadSkillFailed) as exc_info:
        await runner.load_skill_into_sandbox(
            "sbx-1", "other", {"SKILL.md": "# Other"}, auto_install=False, idle_threshold=0,
        )
    assert exc_info.value.extra["sandbox_destroyed"] is True
    _assert_reclaimed(runner, "sbx-1", lease)


@pytest.mark.asyncio
async def test_load_skill_failing_before_the_container_is_touched_leaves_it_ready(docker, monkeypatch):
    runner = SkillRunner()
    sandbox, lease = await register_sandbox(runner)

    from sandbox import scanner

    def broken_scan(self, _skill_dir):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(scanner.SkillScanner, "scan", broken_scan)
    with pytest.raises(RuntimeError, match="scanner exploded"):
        await runner.load_skill_into_sandbox(
            "sbx-1", "other", {"SKILL.md": "# Other"}, auto_install=False, idle_threshold=0
        )

    assert runner._sandboxes["sbx-1"] is sandbox
    assert sandbox.status == SandboxStatus.READY
    assert sandbox.skill.name == "demo"
    assert lease.released == 0
    assert not any(call[1] == "exec" for call in docker.calls)


@pytest.mark.asyncio
async def test_successful_load_commits_skill_env_and_ready_together(docker):
    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)

    response = await runner.load_skill_into_sandbox(
        "sbx-1",
        "other",
        {"SKILL.md": "# Other", "requirements.txt": "ok==1\n"},
        auto_install=True,
        idle_threshold=0,
        env={"OTHER_KEY": "new"},
    )

    assert response.skill.name == "other"
    assert sandbox.skill.name == "other"
    assert sandbox._skill_env == {"OTHER_KEY": "new"}
    assert sandbox.status == SandboxStatus.READY
    assert sandbox.busy_deadline is None


def _stale(sandbox: DockerSandbox, status: SandboxStatus) -> None:
    sandbox.status = status
    sandbox.last_used_at = time.time() - 10 * 24 * 3600
    sandbox._busy_deadline = time.time() - 1 if status != SandboxStatus.READY else None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(SandboxStatus))
async def test_reaper_reclaims_every_state_once_it_is_stale(docker, status):
    """Guard the class: derived from the status enum, so a new state that the
    reaper does not know is a failing test, not capacity held forever."""
    runner = SkillRunner()
    sandbox, lease = await register_sandbox(runner)
    _stale(sandbox, status)

    assert runner._reap_reason(sandbox, time.time()) is not None
    await runner._prune_idle()

    _assert_reclaimed(runner, "sbx-1", lease)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        SandboxStatus.RUNNING,
        SandboxStatus.INSTALLING,
        SandboxStatus.EXECUTING,
        SandboxStatus.DESTROYING,
    ],
)
async def test_reaper_leaves_busy_states_alone_within_their_deadline(docker, status):
    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    sandbox.status = status
    sandbox.last_used_at = time.time() - 10 * 24 * 3600
    sandbox._busy_deadline = time.time() + 60

    await runner._prune_idle()

    assert runner._sandboxes["sbx-1"] is sandbox


@pytest.mark.asyncio
async def test_claim_deadline_is_derived_from_the_operation_timeout(docker):
    sandbox = DockerSandbox("sbx-1", ContainerConfig(install_timeout=300))
    sandbox.status = SandboxStatus.READY
    now = time.time()
    sandbox.claim_command("e-1", "python job.py", 900)
    assert 900 < sandbox.busy_deadline - now < 900 + docker_backend.BUSY_MARGIN_SECONDS + 5
    sandbox.release_command("e-1")
    assert sandbox.busy_deadline is None
    assert sandbox._load_budget_seconds() >= 2 * 300


@pytest.mark.asyncio
async def test_failed_destroy_is_retried_by_the_reaper(docker):
    runner = SkillRunner()
    sandbox, lease = await register_sandbox(runner)
    docker.rm_fails = True

    with pytest.raises(RuntimeError, match="still exists"):
        await runner.destroy_sandbox("sbx-1")
    assert sandbox.status == SandboxStatus.ERROR
    assert runner._local_active_capacity.reserved == 1
    # The container may still exist: its memory stays reserved too (the
    # runner's default size: 1g since one sandbox serves all of a user's work).
    from sandbox.capacity import parse_size
    from sandbox.models import ContainerConfig

    assert runner._memory.reserved == parse_size(ContainerConfig().memory)

    docker.rm_fails = False
    await runner._prune_idle()
    _assert_reclaimed(runner, "sbx-1", lease)


# ── the HTTP surface: every sandbox route reports a lost container as 404 ──


_EXECUTION_ID = "bg-1"

# Every route that names a sandbox: (method, path template) -> (body, setup).
# setup prepares the state the route needs to reach the container.
_SANDBOX_ROUTE_REQUESTS = {
    ("POST", "/api/v1/sandbox/{sandbox_id}/load-skill"): (
        {"skill_name": "other", "files": {"SKILL.md": "# Other"}, "auto_install": False, "idle_threshold": 0},
        None,
    ),
    ("GET", "/api/v1/sandbox/{sandbox_id}"): (None, None),
    ("POST", "/api/v1/sandbox/{sandbox_id}/touch"): (None, None),
    ("DELETE", "/api/v1/sandbox/{sandbox_id}"): (None, None),
    ("POST", "/api/v1/sandbox/{sandbox_id}/exec"): ({"command": "echo hi", "execution_id": "sync-1"}, None),
    ("POST", "/api/v1/sandbox/{sandbox_id}/exec/start"): (
        {"command": "echo hi", "execution_id": "bg-new"},
        None,
    ),
    ("GET", "/api/v1/sandbox/{sandbox_id}/executions/{execution_id}"): (None, "record"),
    ("POST", "/api/v1/sandbox/{sandbox_id}/executions/{execution_id}/cancel"): (None, "running"),
    ("POST", "/api/v1/sandbox/{sandbox_id}/executions/{execution_id}/responses"): (
        {"event_id": "ev-1", "payload": {}},
        "record",
    ),
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/read"): ({"path": "/skill/a.txt"}, None),
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/read-base64"): ({"path": "/skill/a.bin"}, None),
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/tail"): ({"path": "/work/.agent/run.log"}, None),
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/write"): ({"path": "/skill/a.txt", "content": "x"}, None),
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/write-base64"): (
        {"path": "/skill/a.bin", "content_base64": "eA=="},
        None,
    ),
    ("GET", "/api/v1/sandbox/{sandbox_id}/context"): (None, None),
}
# Registry-only routes: they never reach the container, so they cannot detect
# a loss themselves (the reaper's sweep does). After detection they answer 404
# like every other route; DELETE stays idempotent.
_REGISTRY_ONLY = {
    ("POST", "/api/v1/sandbox/{sandbox_id}/touch"),
    ("DELETE", "/api/v1/sandbox/{sandbox_id}"),
}
# Admits the command, then the background task meets the dead container.
_ASYNC_DETECTION = {("POST", "/api/v1/sandbox/{sandbox_id}/exec/start")}


def _sandbox_routes(app) -> set[tuple[str, str]]:
    routes = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if "{sandbox_id}" not in path:
            continue
        for method in getattr(route, "methods", set()) or set():
            if method in {"HEAD", "OPTIONS"}:
                continue
            routes.add((method, path))
    return routes


@pytest.fixture
def service(monkeypatch):
    main = load_service_main(monkeypatch, "_sandbox_service_main_liveness_test")
    return main


def test_route_table_covers_every_sandbox_route(service):
    assert _sandbox_routes(service.app) == set(_SANDBOX_ROUTE_REQUESTS), describe_app_routes(service.app)


def _prepare(runner, sandbox, setup):
    key = ("sbx-1", _EXECUTION_ID)
    if setup in {"record", "running"}:
        runner._execution_records[key] = ExecutionStatusResponse(
            sandbox_id="sbx-1",
            execution_id=_EXECUTION_ID,
            status=ExecutionStatus.RUNNING,
            created_at=time.time(),
        )
        runner._execution_locks[key] = asyncio.Lock()
    if setup == "running":
        sandbox.claim_command(_EXECUTION_ID, "python long.py", 60)


@pytest.mark.parametrize("route", sorted(_SANDBOX_ROUTE_REQUESTS), ids=" ".join)
def test_every_sandbox_route_reports_a_lost_container_as_404(service, monkeypatch, docker, route):
    method, template = route
    body, setup = _SANDBOX_ROUTE_REQUESTS[route]
    runner = SkillRunner()

    async def no_op():
        return None

    monkeypatch.setattr(runner, "startup", no_op)
    monkeypatch.setattr(runner, "shutdown", no_op)
    monkeypatch.setattr(service, "runner", runner)
    path = template.format(sandbox_id="sbx-1", execution_id=_EXECUTION_ID)

    with TestClient(service.app, headers={"X-Manor-Sandbox-Token": API_TOKEN}) as client:
        sandbox, lease = client.portal.call(register_sandbox, runner)
        _prepare(runner, sandbox, setup)
        docker.running = False  # the container died behind the runner's back

        response = client.request(method, path, json=body)

        if route in _REGISTRY_ONLY:
            assert response.status_code == 200, response.text
            if method == "POST":
                # Not detected by a registry-only route; the sweep finds it.
                monkeypatch.setattr(
                    skill_runner_module,
                    "list_running_managed_container_names",
                    lambda _scope: set(),
                )
                client.portal.call(runner._sweep_lost_containers)
        elif route in _ASYNC_DETECTION:
            assert response.status_code == 200, response.text
        else:
            assert response.status_code == 404, response.text
            assert response.json()["code"] == "sandbox_lost", response.json()

        deadline = time.monotonic() + 5
        while "sbx-1" in runner._sandboxes and time.monotonic() < deadline:
            time.sleep(0.01)
        _assert_reclaimed(runner, "sbx-1", lease)

        # Every subsequent request for the id answers 404 (DELETE: idempotent).
        for other_method, other_template in sorted(_SANDBOX_ROUTE_REQUESTS):
            other_body, _setup = _SANDBOX_ROUTE_REQUESTS[(other_method, other_template)]
            other_path = other_template.format(sandbox_id="sbx-1", execution_id=_EXECUTION_ID)
            later = client.request(other_method, other_path, json=other_body)
            if other_method == "DELETE":
                assert later.status_code == 200, later.text
                continue
            assert later.status_code == 404, (other_method, other_template, later.text)
            assert later.json()["code"] == "sandbox_not_found"
