"""Disk quota of a user's shared sandbox: one policy (``sandbox.disk_quota``), runner side.

Real Docker: ``test_sandbox_per_user_real_docker.py`` (one work area fills
/workspace; only its commands are killed, the other completes, the container
survives).

* attribution per work area: only the commands of the area holding the most
  of a full /workspace are stopped (verdict -> ``disk_killed``), its queued
  and new commands are refused as never started, cleanup commands still run,
  every other area keeps running, and the refusal lifts once the disk is back
  under its resume level;
* a disk without areas (the install dir of PR #1052) charges the commands
  writing into it; the container is recycled only when such a disk is still
  over its limit and nothing runs in it;
* the runner's disk watchdog wires measurement, kills and recycling.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.sandbox_runner_fakes import completed, register_sandbox

import sandbox.docker_backend as docker_backend  # noqa: E402
from sandbox.disk_quota import (  # noqa: E402
    DiskRefusal,
    DiskRoot,
    DiskUsage,
    enforce_disk_quota,
    execution_digest,
    is_cleanup_command,
    plan_disk_quota,
)
from sandbox.docker_backend import DockerSandbox, exec_outcome  # noqa: E402
from sandbox.errors import SandboxDiskQuotaExceeded  # noqa: E402
from sandbox.models import ContainerConfig, SandboxStatus  # noqa: E402
from sandbox.skill_runner import SkillRunner  # noqa: E402

MB = 1024 * 1024


def _sandbox(limit: int = 2) -> DockerSandbox:
    sandbox = DockerSandbox("s", ContainerConfig(max_concurrent_executions=limit, memory="1g", pids_limit=512))
    sandbox.status = SandboxStatus.READY
    sandbox.container_name = "c"
    return sandbox


def _workspace(sandbox: DockerSandbox) -> DiskRoot:
    [root] = [root for root in sandbox.disk_roots() if root.path == "/workspace"]
    assert root.path == "/workspace" and root.areas_dir == "/workspace/c"
    assert root.limit_bytes == int(256 * MB * 0.92)
    return root


class _Recorder:
    """Stands in for the in-container kill: records what the policy stops."""

    def __init__(self, sandbox: DockerSandbox) -> None:
        self.killed: list[tuple[str, dict]] = []

        async def stop_for_disk(kills):
            self.killed.extend((kill.execution_id, kill.verdict) for kill in kills)

        sandbox.stop_for_disk = stop_for_disk


@pytest.mark.asyncio
async def test_one_work_area_fills_the_workspace_only_its_commands_stop_the_others_run_on() -> None:
    sandbox = _sandbox(limit=2)
    recorder = _Recorder(sandbox)
    root = _workspace(sandbox)
    sandbox.claim_command("a-run", "dd if=/dev/zero of=big", 60, fair_key="conv-A")
    sandbox.claim_command("b-run", "python work.py", 60, fair_key="conv-B")
    a_wait = sandbox.enqueue_command("a-wait", "python more.py", 60, fair_key="conv-A")
    b_wait = sandbox.enqueue_command("b-wait", "python next.py", 60, fair_key="conv-B")
    a_cleanup = sandbox.enqueue_command("a-clean", "rm -rf /workspace/c/conv-A/tmp", 60, fair_key="conv-A")

    full = DiskUsage(root, used_bytes=250 * MB, area_bytes={"conv-A": 240 * MB, "conv-B": 6 * MB})
    plan = await enforce_disk_quota(sandbox, [full])

    # Only the offending area's running command is stopped, with its verdict.
    assert recorder.killed == [("a-run", {
        "reason": "disk", "root": "/workspace", "area": "/workspace/c/conv-A",
        "bytes": 240 * MB, "used": 250 * MB, "limit": root.limit_bytes,
    })]
    assert plan.recycle_reason is None and not sandbox.is_lost
    # Its queued command never starts; the other area's does, and so does its cleanup.
    refused = a_wait.exception()
    assert isinstance(refused, SandboxDiskQuotaExceeded) and refused.extra["started"] is False
    assert refused.status_code == 409 and refused.code == "sandbox_disk_quota"
    assert "/workspace/c/conv-A holds 240 MB" in refused.message
    assert not b_wait.done() and not a_cleanup.done()
    assert sorted(sandbox.queued_execution_ids) == ["a-clean", "b-wait"]
    # New commands: the offending area is refused, cleanup and other areas are not.
    with pytest.raises(SandboxDiskQuotaExceeded):
        sandbox.enqueue_command("a-new", "pip install torch", 60, fair_key="conv-A")
    with pytest.raises(SandboxDiskQuotaExceeded):
        sandbox.claim_command("a-sync", "echo hi > f", 60, fair_key="conv-A")
    sandbox.enqueue_command("b-new", "python report.py", 60, fair_key="conv-B")
    sandbox.enqueue_command("a-du", "du -sh /workspace/c/conv-A; ls -la", 60, fair_key="conv-A")

    # The killed command ends; the cleanup runs; the disk drops under its
    # resume level and the area works again.
    sandbox.release_command("a-run")
    sandbox.release_command("b-run")
    await enforce_disk_quota(sandbox, [DiskUsage(root, used_bytes=120 * MB, area_bytes={"conv-A": 110 * MB})])
    assert sandbox.disk_refusals == {}
    sandbox.enqueue_command("a-again", "python more.py", 60, fair_key="conv-A")
    assert len(recorder.killed) == 1


def test_a_refusal_holds_until_the_disk_is_back_under_its_resume_level() -> None:
    root = DiskRoot(path="/workspace", limit_bytes=200 * MB, areas_dir="/workspace/c", filesystem=True)
    refusal = DiskRefusal(area="A", root="/workspace", message="refused")
    # Between the resume level (170 MB) and the limit: A is still the
    # largest, so it stays refused, but nothing is killed.
    plan = plan_disk_quota(
        [DiskUsage(root, used_bytes=180 * MB, area_bytes={"A": 150 * MB, "B": 20 * MB})],
        executions={"a": "A", "b": "B"}, refusals={"A": refusal}, idle=False,
    )
    assert set(plan.refusals) == {"A"} and plan.kills == []
    # B is now the largest (A cleaned up): A is free, and B -- not over -- is not refused.
    plan = plan_disk_quota(
        [DiskUsage(root, used_bytes=180 * MB, area_bytes={"A": 10 * MB, "B": 160 * MB})],
        executions={}, refusals={"A": refusal}, idle=True,
    )
    assert plan.refusals == {}
    # Under the resume level everything lifts; a disk not measured keeps its refusals.
    other = DiskRefusal(area="C", root="/opt/sandbox-local", message="refused")
    plan = plan_disk_quota(
        [DiskUsage(root, used_bytes=100 * MB, area_bytes={"A": 90 * MB})],
        executions={}, refusals={"A": refusal, "C": other}, idle=True,
    )
    assert plan.refusals == {"C": other}
    # A command whose processes work in the offending area is stopped even
    # without a work area of its own (an older caller sent no fair_key).
    plan = plan_disk_quota(
        [DiskUsage(
            root, used_bytes=210 * MB, area_bytes={"A": 190 * MB},
            cwd_areas={execution_digest("legacy"): frozenset({"A"})},
        )],
        executions={"legacy": None, "elsewhere": None}, refusals={}, idle=False,
    )
    assert [kill.execution_id for kill in plan.kills] == ["legacy"]


def test_bytes_outside_every_area_are_charged_to_their_writers_never_to_the_largest_area() -> None:
    """/workspace/shared belongs to no area: when it holds most of a full
    /workspace, the largest area is innocent; the command writing there pays."""

    root = DiskRoot(path="/workspace", limit_bytes=200 * MB, areas_dir="/workspace/c", filesystem=True)
    plan = plan_disk_quota(
        [DiskUsage(
            root, used_bytes=230 * MB, area_bytes={"A": 30 * MB, "B": 5 * MB},
            writers=frozenset({execution_digest("b-shared")}),
        )],
        executions={"a-run": "A", "b-shared": "B"}, refusals={}, idle=False,
    )
    assert [kill.execution_id for kill in plan.kills] == ["b-shared"]
    assert plan.kills[0].verdict["area"] is None and plan.kills[0].verdict["bytes"] == 195 * MB
    assert set(plan.refusals) == {"B"} and "outside the work areas" in plan.refusals["B"].message
    # The area that does hold the most is still the offender once it does.
    plan = plan_disk_quota(
        [DiskUsage(root, used_bytes=230 * MB, area_bytes={"A": 200 * MB, "B": 5 * MB},
                   writers=frozenset({execution_digest("b-shared")}))],
        executions={"a-run": "A", "b-shared": "B"}, refusals={}, idle=False,
    )
    assert [kill.execution_id for kill in plan.kills] == ["a-run"] and set(plan.refusals) == {"A"}


@pytest.mark.asyncio
async def test_a_shared_install_dir_charges_its_writers_and_recycles_only_an_idle_container() -> None:
    """The hook for PR #1052's /opt/sandbox-local: no areas, so the commands
    writing into it are charged; the container is recycled only when the dir
    is still over its limit with nothing running."""

    sandbox = _sandbox(limit=4)
    recorder = _Recorder(sandbox)
    install = DiskRoot(
        path="/opt/sandbox-local", limit_bytes=100 * MB, filesystem=False, areas_dir=None,
        recycle_when_idle=True, interval_seconds=60,
    )
    sandbox.claim_command("pip", "pip install torch", 600, fair_key="conv-A")
    sandbox.claim_command("other", "python work.py", 60, fair_key="conv-B")
    over = DiskUsage(install, used_bytes=150 * MB, writers=frozenset({execution_digest("pip")}))

    plan = await enforce_disk_quota(sandbox, [over])
    assert [kill for kill, _ in recorder.killed] == ["pip"]
    assert recorder.killed[0][1]["root"] == "/opt/sandbox-local" and recorder.killed[0][1]["area"] is None
    assert set(sandbox.disk_refusals) == {"conv-A"} and plan.recycle_reason is None
    with pytest.raises(SandboxDiskQuotaExceeded, match="writing to /opt/sandbox-local"):
        sandbox.enqueue_command("pip2", "pip install tensorflow", 600, fair_key="conv-A")
    sandbox.enqueue_command("pip-clean", "pip uninstall -y torch && pip cache purge", 60, fair_key="conv-A")

    # Still over, and the other area still runs: never recycled under it.
    sandbox.release_command("pip")
    plan = await enforce_disk_quota(sandbox, [DiskUsage(install, used_bytes=150 * MB)])
    assert plan.recycle_reason is None and not sandbox.is_lost
    assert set(sandbox.disk_refusals) == {"conv-A"}, "the writer's area stays refused while the dir is over"

    # Nothing runs any more and the dir is still over: the container is unusable.
    sandbox.release_command("other")
    sandbox.release_command("pip-clean")
    assert not sandbox.has_pending_work
    plan = await enforce_disk_quota(sandbox, [DiskUsage(install, used_bytes=150 * MB)])
    assert plan.recycle_reason and "/opt/sandbox-local holds 150 MB" in plan.recycle_reason
    assert sandbox.is_lost

    # /workspace alone is never a reason to recycle: its bytes are the areas'.
    idle = _sandbox()
    _Recorder(idle)
    plan = await enforce_disk_quota(idle, [DiskUsage(_workspace(idle), used_bytes=255 * MB, area_bytes={"A": 250 * MB})])
    assert plan.recycle_reason is None and not idle.is_lost and set(idle.disk_refusals) == {"A"}


@pytest.mark.parametrize(
    ("command", "cleanup"),
    [
        ("rm -rf /workspace/c/conv-A/node_modules", True),
        ("du -sh .; ls -la && df -h", True),
        ("find . -name '*.tmp' -delete", True),
        ("pip uninstall -y torch; pip cache purge", True),
        ("python3 -m pip uninstall -y torch", True),
        ("npm cache clean --force && npm uninstall -g left-pad", True),
        ("/bin/rm big.bin", True),
        ("rm big.bin; dd if=/dev/zero of=x", False),
        ("ls > listing.txt", False),
        ("find . -exec cp {} /tmp \\;", False),
        ("rm $(cat list)", False),
        ("ls | tee out", False),
        ("sleep 100 &", False),
        ("pip install torch", False),
        ("python3 -m pip install torch", False),
        ("", False),
        ("rm 'unterminated", False),
    ],
)
def test_cleanup_commands_are_only_commands_that_free_space(command, cleanup) -> None:
    assert is_cleanup_command(command) is cleanup


def test_disk_kill_reasons_name_the_area_or_the_directory() -> None:
    area = exec_outcome(137, {"signal": 9, "verdict": {
        "reason": "disk", "root": "/workspace", "area": "/workspace/c/conv-A",
        "bytes": 240 * MB, "used": 250 * MB, "limit": 235 * MB,
    }})
    assert area[0] == "disk_killed"
    assert "its work area /workspace/c/conv-A held 240 MB" in area[1]
    assert "other work in the sandbox kept running" in area[1]
    writer = exec_outcome(137, {"signal": 9, "verdict": {
        "reason": "disk", "root": "/opt/sandbox-local", "area": None,
        "bytes": 150 * MB, "used": 150 * MB, "limit": 100 * MB,
    }})
    assert writer[0] == "disk_killed" and "writing to /opt/sandbox-local, which held 150 MB, over its 100 MB" in writer[1]


def test_the_disk_probe_and_kill_sources_compile() -> None:
    compile(docker_backend._DISK_PROBE_SOURCE, "<disk-probe>", "exec")
    compile(docker_backend._DISK_KILL_SOURCE, "<disk-kill>", "exec")
    assert "disk" not in docker_backend._WATCHDOG_SOURCE.split("while True:")[1], (
        "the watchdog only guards pids; the disk policy is the runner's"
    )


@pytest.mark.asyncio
async def test_the_runner_disk_watchdog_measures_stops_refuses_and_reports(monkeypatch) -> None:
    """End to end on the runner (scripted docker): the probe's report is
    attributed, the offender's kill carries its verdict, its queued
    background command ends ``disk_quota`` (never started), the other area's
    command completes."""

    calls: list[list[str]] = []
    report = {"roots": [], "commands": {}}

    def docker(args, **_kwargs):
        calls.append(list(args))
        if "-c" in args and args[args.index("-c") + 1] == docker_backend._DISK_PROBE_SOURCE:
            return completed(args, 0, stdout=json.dumps(report).encode())
        return completed(args, 0, stdout=b"ok\n")

    monkeypatch.setattr(docker_backend, "_run_docker", docker)
    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    sandbox.config = ContainerConfig(max_concurrent_executions=1, memory="1g", pids_limit=512)
    sandbox.claim_command("a-run", "dd if=/dev/zero of=big", 60, fair_key="conv-A")
    queued = await runner.start_execution(
        "sbx-1", "python more.py", execution_id="a-wait", queue_timeout=30, fair_key="conv-A",
    )
    other = await runner.start_execution(
        "sbx-1", "python report.py", execution_id="b-wait", queue_timeout=30, fair_key="conv-B",
    )
    assert queued.queue_position == 1 and other.queue_position == 2
    report["roots"] = [{
        "path": "/workspace", "used": 250 * MB, "areas": {"conv-A": 240 * MB, "conv-B": 4 * MB},
    }]
    report["commands"] = {execution_digest("a-run"): {"cwd": ["/workspace/c/conv-A/agents/main"], "open": []}}

    await runner._enforce_disk_quotas()

    [probe] = [call for call in calls if docker_backend._DISK_PROBE_SOURCE in call]
    spec = json.loads(probe[-1])
    assert [root for root in spec if root["path"] == "/workspace"] == [{
        "path": "/workspace", "filesystem": True, "areas_dir": "/workspace/c",
        "areas_from": int(int(256 * MB * 0.92) * 0.85), "writers": True,
    }]
    # The disk-backed install dir is measured in the same probe: a directory
    # (du), no areas, its writers listed.
    [install] = [root for root in spec if root["path"] == "/opt/sandbox-local"]
    assert install["filesystem"] is False and install["areas_dir"] is None and install["writers"] is True
    [kill] = [call for call in calls if docker_backend._DISK_KILL_SOURCE in call]
    assert kill[-2] == "/manor-control/verdicts"
    assert json.loads(kill[-1]) == [{"digest": execution_digest("a-run"), "verdict": {
        "reason": "disk", "root": "/workspace", "area": "/workspace/c/conv-A",
        "bytes": 240 * MB, "used": 250 * MB, "limit": int(256 * MB * 0.92),
    }}]
    refused = await runner.get_execution_status("sbx-1", "a-wait", wait_seconds=5)
    assert refused.status.value == "failed" and refused.outcome == "disk_quota"
    assert "Not started" in refused.outcome_detail and refused.exit_code is None
    sandbox.release_command("a-run")  # the killed command ends
    done = await runner.get_execution_status("sbx-1", "b-wait", wait_seconds=5)
    assert done.status.value == "completed" and done.stdout == "ok\n"
    assert not sandbox.is_lost and runner._sandboxes.get("sbx-1") is sandbox

    # Not due again until its interval passes: no probe storm.
    calls.clear()
    await runner._enforce_disk_quotas()
    assert not any(docker_backend._DISK_PROBE_SOURCE in call for call in calls)


@pytest.mark.asyncio
async def test_the_runner_recycles_an_unusable_idle_sandbox_and_nothing_else(monkeypatch) -> None:
    monkeypatch.setattr(docker_backend, "_run_docker", lambda args, **_: completed(args, 0, stdout=b""))
    runner = SkillRunner()
    sandbox, _lease = await register_sandbox(runner)
    install = DiskRoot(path="/opt/sandbox-local", limit_bytes=100 * MB, recycle_when_idle=True)
    sandbox.disk_roots = lambda: [install]

    async def measure(roots):
        return [DiskUsage(install, used_bytes=150 * MB)]

    sandbox.measure_disk = measure
    sandbox.claim_command("busy", "sleep 60", 60, fair_key="conv-A")
    await runner._enforce_disk_quotas()
    assert runner._sandboxes.get("sbx-1") is sandbox, "never recycled under a running command"
    sandbox.release_command("busy")
    sandbox._disk_measured_at.clear()
    await runner._enforce_disk_quotas()
    await asyncio.sleep(0)
    assert "sbx-1" not in runner._sandboxes
