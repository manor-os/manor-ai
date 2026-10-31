"""The runner side of one shared sandbox per user (fakes; real Docker: test_sandbox_per_user_real_docker.py).

* the fair FIFO: commands beyond the slot cap wait instead of failing busy,
  round-robin across work areas (``fair_key``), FIFO within one; a waiting
  command can be withdrawn, times out as "not started", and fails as "never
  started" when its sandbox is lost;
* Skills coexist in ``/skills/<slot>``: ``/skill`` paths are mapped per
  request, in commands, file operations and the Skill's own files;
* kill reasons are reported accurately (OOM, pids, disk) and the launcher's
  status line never reaches the caller;
* a sandbox past its max lifetime is recycled only between commands.
"""

from __future__ import annotations

import asyncio
import io
import tarfile
import time

import pytest

from tests.sandbox_runner_fakes import completed, register_sandbox

import sandbox.docker_backend as docker_backend  # noqa: E402
from sandbox.docker_backend import (  # noqa: E402
    DockerSandbox,
    _strip_exec_status,
    _tar_skill_directory,
    exec_outcome,
    rewrite_skill_paths,
    skill_slot,
)
from sandbox.errors import (  # noqa: E402
    ExecQueueTimeout,
    ExecutionCancelled,
    InvalidRequest,
    SandboxBusy,
    SandboxLost,
)
from sandbox.models import ContainerConfig, SandboxStatus, SkillManifest  # noqa: E402
from sandbox.skill_runner import SkillRunner  # noqa: E402
from sandbox_config import config as app_config  # noqa: E402


def _sandbox(limit: int = 2) -> DockerSandbox:
    sandbox = DockerSandbox("s", ContainerConfig(max_concurrent_executions=limit, memory="1g", pids_limit=512))
    sandbox.status = SandboxStatus.READY
    sandbox.container_name = "c"
    return sandbox


# ── the fair FIFO ──


@pytest.mark.asyncio
async def test_waiters_are_dispatched_round_robin_across_work_areas_fifo_within_one() -> None:
    sandbox = _sandbox(limit=1)
    sandbox.claim_command("running", "x", 60)
    order: list[str] = []
    futures = {}
    # Task A queues three commands before task B and task C queue one each.
    for execution_id, key in (("a1", "A"), ("a2", "A"), ("a3", "A"), ("b1", "B"), ("c1", "C")):
        futures[execution_id] = sandbox.enqueue_command(execution_id, "cmd", 60, fair_key=key)
        futures[execution_id].add_done_callback(lambda _f, eid=execution_id: order.append(eid))
    assert sandbox.queued_execution_ids == ["a1", "b1", "c1", "a2", "a3"]
    assert [sandbox.queue_position(eid) for eid in ("a1", "b1", "c1", "a2", "a3")] == [1, 2, 3, 4, 5]
    current = "running"
    for _ in range(5):
        sandbox.release_command(current)
        await asyncio.sleep(0)
        current = order[-1]
        assert sandbox.active_execution_ids == [current]
    assert order == ["a1", "b1", "c1", "a2", "a3"]
    sandbox.release_command(current)
    assert sandbox.status is SandboxStatus.READY and not sandbox.has_pending_work


@pytest.mark.asyncio
async def test_a_command_never_jumps_the_queue_and_the_queue_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(app_config, "MAX_QUEUED_EXECUTIONS", 2)
    sandbox = _sandbox(limit=1)
    sandbox.claim_command("running", "x", 60)
    sandbox.enqueue_command("w1", "x", 60, fair_key="A")
    # A caller that cannot wait is told "busy" while others wait, even if a
    # slot frees between its check and theirs.
    with pytest.raises(SandboxBusy):
        sandbox.claim_command("now", "x", 60)
    sandbox.enqueue_command("w2", "x", 60, fair_key="B")
    with pytest.raises(SandboxBusy, match="queue limit 2"):
        sandbox.enqueue_command("w3", "x", 60, fair_key="C")

    # A slot is free but the only waiter's Skill is still loading: a caller
    # that cannot wait still does not take the slot ahead of it.
    loading = _sandbox(limit=1)
    loading.claim_skill_load("pptx", 30)
    waiter = loading.enqueue_command("p1", "x", 60, fair_key="A", slot="pptx")
    assert not waiter.done()
    with pytest.raises(SandboxBusy):
        loading.claim_command("now", "x", 60)


@pytest.mark.asyncio
async def test_withdrawn_and_lost_waiters_never_run() -> None:
    sandbox = _sandbox(limit=1)
    sandbox.claim_command("running", "x", 60)
    cancelled = sandbox.enqueue_command("w1", "x", 60, fair_key="A")
    lost = sandbox.enqueue_command("w2", "x", 60, fair_key="B")
    assert sandbox.withdraw_waiter("w1", ExecutionCancelled("cancelled before it started"))
    assert isinstance(cancelled.exception(), ExecutionCancelled)
    assert sandbox.withdraw_waiter("w1") is False
    sandbox._mark_lost("container no longer exists")
    error = lost.exception()
    assert isinstance(error, SandboxLost) and error.extra == {"started": False}
    assert sandbox.queued_execution_ids == []


@pytest.mark.asyncio
async def test_a_waiter_of_a_loading_skill_lets_other_work_go_first() -> None:
    sandbox = _sandbox(limit=1)
    sandbox.claim_command("running", "x", 60)
    sandbox.claim_skill_load("pptx", 30)
    pptx = sandbox.enqueue_command("p1", "x", 60, fair_key="A", slot="pptx")
    other = sandbox.enqueue_command("o1", "x", 60, fair_key="B")
    sandbox.release_command("running")
    assert other.done() and not pptx.done()
    sandbox.release_command("o1")
    assert not pptx.done(), "still loading"
    sandbox.release_skill_load("pptx")
    assert pptx.done() and sandbox.active_execution_ids == ["p1"]


@pytest.mark.asyncio
async def test_runner_exec_waits_for_a_slot_and_times_out_as_not_started(monkeypatch) -> None:
    monkeypatch.setattr(docker_backend, "_run_docker", lambda args, **_: completed(args, 0, stdout=b"ok\n"))
    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    sandbox.config = ContainerConfig(max_concurrent_executions=1, memory="1g", pids_limit=512)
    sandbox.claim_command("held", "x", 60)
    with pytest.raises(ExecQueueTimeout) as timed_out:
        await runner.exec_command("sbx-1", "echo hi", timeout=10, execution_id="late", queue_timeout=0.05)
    assert timed_out.value.extra["started"] is False and timed_out.value.status_code == 409
    assert sandbox.queued_execution_ids == []

    waiting = asyncio.create_task(
        runner.exec_command("sbx-1", "echo hi", timeout=10, execution_id="patient", queue_timeout=5)
    )
    await asyncio.sleep(0.05)
    assert sandbox.queued_execution_ids == ["patient"]
    sandbox.release_command("held")
    result = await waiting
    assert result.exit_code == 0 and result.stdout == "ok\n"


@pytest.mark.asyncio
async def test_background_command_queues_reports_its_place_and_can_be_cancelled(monkeypatch) -> None:
    monkeypatch.setattr(docker_backend, "_run_docker", lambda args, **_: completed(args, 0, stdout=b"ok\n"))
    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    sandbox.config = ContainerConfig(max_concurrent_executions=1, memory="1g", pids_limit=512)
    sandbox.claim_command("held", "x", 60)
    first = await runner.start_execution("sbx-1", "echo 1", execution_id="q1", queue_timeout=30, fair_key="A")
    second = await runner.start_execution("sbx-1", "echo 2", execution_id="q2", queue_timeout=30, fair_key="B")
    assert (first.status.value, first.queue_position) == ("queued", 1)
    assert (second.status.value, second.queue_position) == ("queued", 2)
    # Idempotent: the same request answers the same execution.
    again = await runner.start_execution("sbx-1", "echo 1", execution_id="q1", queue_timeout=30, fair_key="A")
    assert again.queue_position == 1

    assert await runner.cancel_execution("sbx-1", "q1") is True
    await asyncio.sleep(0.01)
    cancelled = await runner.get_execution_status("sbx-1", "q1")
    assert cancelled.status.value == "cancelled" and cancelled.exit_code is None
    assert (await runner.get_execution_status("sbx-1", "q2")).queue_position == 1

    sandbox.release_command("held")
    done = await runner.get_execution_status("sbx-1", "q2", wait_seconds=5)
    assert done.status.value == "completed" and done.stdout == "ok\n" and done.queue_position is None


@pytest.mark.asyncio
async def test_background_command_that_never_got_a_slot_fails_as_queue_timeout(monkeypatch) -> None:
    monkeypatch.setattr(docker_backend, "_run_docker", lambda args, **_: completed(args, 0))
    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    sandbox.config = ContainerConfig(max_concurrent_executions=1, memory="1g", pids_limit=512)
    sandbox.claim_command("held", "x", 60)
    await runner.start_execution("sbx-1", "echo 1", execution_id="q1", queue_timeout=0.05)
    status = await runner.get_execution_status("sbx-1", "q1", wait_seconds=5)
    assert status.status.value == "failed"
    assert status.outcome == "queue_timeout" and status.exit_code is None


# ── Skills side by side ──


def test_skill_view_rewrites_only_path_roots() -> None:
    assert rewrite_skill_paths("cd /skill && python /skill/scripts/a.py '/skill/x y'", "pptx") == (
        "cd /skills/pptx && python /skills/pptx/scripts/a.py '/skills/pptx/x y'"
    )
    unchanged = "ls /skills/other /skill-x /skill.md foo/skill /opt/skill ~/skill"
    assert rewrite_skill_paths(unchanged, "pptx") == unchanged
    assert rewrite_skill_paths("cat /skill;echo $(ls /skill)", "a") == "cat /skills/a;echo $(ls /skills/a)"
    assert rewrite_skill_paths("/skill/x", None) == "/skill/x"
    assert skill_slot("PPTX Builder!") == "pptx-builder"
    assert skill_slot("../..") == "skill"


def test_skill_files_are_mapped_to_their_own_directory_when_packed(tmp_path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run.py").write_text("PROJECT = '/skill/project'\n")
    (tmp_path / "logo.bin").write_bytes(b"\xff\xfe/skill\x00")
    archive = _tar_skill_directory(str(tmp_path), ["logo.bin", "scripts"], "video-edit")
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        names = tar.getnames()
        assert sorted(names) == ["logo.bin", "scripts", "scripts/run.py"]
        assert tar.extractfile("scripts/run.py").read() == b"PROJECT = '/skills/video-edit/project'\n"
        assert tar.extractfile("logo.bin").read() == b"\xff\xfe/skill\x00"  # binary untouched


def test_each_command_sees_only_its_own_skill_view_and_env() -> None:
    sandbox = _sandbox()
    sandbox._slots = {
        "alpha": SkillManifest(name="alpha", skill_dir="/x"),
        "beta": SkillManifest(name="beta", skill_dir="/y"),
    }
    sandbox._slot_envs = {"alpha": {"A": "1"}, "beta": {"B": "2"}}
    sandbox.skill = sandbox._slots["beta"]
    sandbox._skill_env = {"B": "2"}
    assert sandbox.command_placement("python /skill/run.py", None, skill="alpha", skill_env=True) == (
        "python /skills/alpha/run.py", "/skills/alpha", {"A": "1"}, "alpha",
    )
    # An agent sharing the sandbox without that Skill: no view, no env.
    assert sandbox.command_placement("ls /skill", "/workspace/c/k/agents/a", skill="", skill_env=False) == (
        "ls /skill", "/workspace/c/k/agents/a", {}, None,
    )
    # A caller naming no Skill keeps the single-Skill behaviour: the last loaded.
    assert sandbox.command_placement("ls /skill", None, skill=None, skill_env=False)[:2] == ("ls /skills/beta", "/skills/beta")
    with pytest.raises(InvalidRequest):
        sandbox.resolve_view("gamma")


def test_new_containers_are_bounded_and_mount_skills_side_by_side() -> None:
    sandbox = _sandbox()
    args = sandbox._build_create_args({})
    joined = " ".join(args)
    assert "/skills:exec,size=256m,mode=1777" in args
    assert "/workspace:exec,size=256m,mode=1777" in args
    assert "/manor-control:size=1m,mode=1777" in args
    assert "/tmp:size=64m" in args and "/var/tmp:size=64m" in args
    assert args[args.index("--memory-swap") + 1] == "1g", "no swap: a hog is killed, not thrashing the host"
    assert args[args.index("--pids-limit") + 1] == "512"
    assert ":/skill:" not in joined and " /skill:" not in joined
    # A caller that mounts /workspace itself gets no tmpfs over it.
    mounted = DockerSandbox("m", ContainerConfig(volumes=["/host/ws:/workspace:ro"]))
    assert "/workspace:exec" not in " ".join(mounted._build_create_args({}))
    assert mounted.default_cwd() == "/tmp"



def _workdir_of(args: list[str]) -> str:
    return args[args.index("--workdir") + 1]


def test_a_default_runner_keeps_the_per_user_layout() -> None:
    """SANDBOX_WORKDIR=/skill is the Skill view: no mount of its own, the
    container starts in /skills and commands in the user's /workspace."""

    sandbox = _sandbox()
    assert sandbox.config.workdir == "/skill"
    args = sandbox._build_create_args({})
    assert _workdir_of(args) == "/skills"
    assert [arg for arg in args if arg.startswith(("/skill:", "/work:"))] == []
    assert sandbox.runner_workdir() is None
    assert sandbox.default_cwd() == "/workspace"


def test_an_external_agent_pool_runner_mounts_and_starts_in_its_configured_workdir() -> None:
    """The pool runner's SANDBOX_WORKDIR=/work (runbook): a writable tmpfs
    there, sized like the workdir, the container's start directory and the
    cwd of a command that names no Skill. /skills stays for the bundle."""

    sandbox = DockerSandbox("x", ContainerConfig(workdir="/work", workdir_tmpfs_size="2g"))
    args = sandbox._build_create_args({})
    assert "/work:exec,size=2g,mode=1777" in args
    assert "/skills:exec,size=2g,mode=1777" in args
    assert _workdir_of(args) == "/work"
    assert sandbox.default_cwd() == "/work"
    assert sandbox.command_placement("python -m manor_agent_runner", "/work", skill=None, skill_env=False)[1] == "/work"
    assert sandbox.command_placement("ls", None, skill=None, skill_env=False)[1] == "/work"


@pytest.mark.parametrize("workdir", ["/", "/skills", "/skills/x", "/workspace", "/workspace/c/a", "/tmp", "/tmp/x",
                                     "/manor-control", "/root", "/home/sandbox/w", "/opt/sandbox-local",
                                     "/opt/sandbox-local/python", "relative"])
def test_a_workdir_inside_the_layout_or_a_reserved_mount_gets_no_mount_of_its_own(workdir) -> None:
    sandbox = DockerSandbox("r", ContainerConfig(workdir=workdir))
    args = sandbox._build_create_args({})
    assert sandbox.runner_workdir() is None
    assert _workdir_of(args) == "/skills"
    assert sum(arg.endswith(",mode=1777") for arg in args) == 3  # /skills, /workspace, /manor-control


# ── kill reasons ──


def test_kill_reasons_are_accurate_and_the_status_line_is_stripped() -> None:
    stderr, status = _strip_exec_status('boom\n__MANOR_EXEC_STATUS__ {"signal": 9, "oom_kill_delta": 1, "verdict": null}\n')
    assert stderr == "boom" and status == {"signal": 9, "oom_kill_delta": 1, "verdict": None}
    assert exec_outcome(137, status, memory="1g")[0] == "oom_killed"
    # SIGKILL without the OOM counter moving (a cancel, a timeout): no OOM claim.
    assert exec_outcome(137, {"signal": 9, "oom_kill_delta": 0}) == (None, None)
    assert exec_outcome(0, None) == (None, None)
    pids = exec_outcome(137, {"signal": 9, "verdict": {"reason": "pids", "count": 480, "limit": 512}})
    assert pids[0] == "pids_killed" and "480 of 512" in pids[1]
    disk = exec_outcome(137, {"signal": 9, "verdict": {"reason": "disk", "area": "/workspace/c/x", "bytes": 300 << 20}})
    assert disk[0] == "disk_killed" and "300 MB" in disk[1]
    assert _strip_exec_status("plain") == ("plain", None)


def test_the_watchdog_and_launcher_sources_compile() -> None:
    compile(docker_backend._WATCHDOG_SOURCE, "<watchdog>", "exec")
    compile(docker_backend._EXEC_LAUNCHER_SOURCE, "<launcher>", "exec")


# ── lifetime ──


def test_an_old_sandbox_is_recycled_only_between_commands(monkeypatch) -> None:
    monkeypatch.setattr(app_config, "MAX_LIFETIME_SECONDS", 3600)
    runner = SkillRunner()
    sandbox = _sandbox()
    now = time.time()
    sandbox.created_at = now - 7200
    sandbox.last_used_at = now - 60
    assert runner._reap_reason(sandbox, now) == "max-lifetime"
    sandbox.last_used_at = now - 5
    assert runner._reap_reason(sandbox, now) is None, "just used: the next command may follow"
    sandbox.last_used_at = now - 60
    sandbox.claim_command("e", "x", 60)
    assert runner._reap_reason(sandbox, now) is None, "never while a command runs"
    sandbox.release_command("e")
    sandbox.claim_skill_load("pptx", 30)
    sandbox.last_used_at = now - 60  # the load has run for a while
    assert runner._reap_reason(sandbox, now) is None, "never while a Skill loads"
    sandbox.release_skill_load("pptx")
    sandbox.last_used_at = now - 60
    sandbox.created_at = now - 60
    assert runner._reap_reason(sandbox, now) is None, "young"
    monkeypatch.setattr(app_config, "MAX_LIFETIME_SECONDS", 0)
    sandbox.created_at = now - 10**6
    assert runner._reap_reason(sandbox, now) is None, "0 disables recycling"
