"""Resource admission and process cleanup for the isolated video CLI wrapper."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "packages/core/ai/skills/video-edit/scripts/video_runtime.py"
    spec = importlib.util.spec_from_file_location("video_resource_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PROJECT", tmp_path)
    monkeypatch.setattr(module, "CGROUP", tmp_path / "cgroup")
    module.CGROUP.mkdir()
    return module


def _cgroup(runner, *, pids=512, current=12, memory=4 * 1024**3, used=0, cpu="400000 100000"):
    for name, value in {
        "pids.max": pids, "pids.current": current,
        "memory.max": memory, "memory.current": used, "cpu.max": cpu,
    }.items():
        (runner.CGROUP / name).write_text(str(value))


@pytest.mark.parametrize("limits,expected", [
    ({}, 1),
    ({"pids": 2048, "memory": 8 * 1024**3}, 4),
    ({"pids": 2048, "memory": 8 * 1024**3, "cpu": "150000 100000"}, 1),
    ({"pids": 2048, "memory": 4 * 1024**3, "used": 2 * 1024**3}, 1),
])
def test_workers_fit_remaining_pid_cpu_and_memory_budget(runner, monkeypatch, limits, expected):
    _cgroup(runner, **limits)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    if hasattr(os, "sched_getaffinity"):
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(8)))
    assert runner._render_resources(4)["workers"] == expected


def test_worker_request_is_an_upper_bound_and_unknown_limits_use_one(runner):
    _cgroup(runner, pids=2048, memory=8 * 1024**3)
    assert runner._render_resources(1)["workers"] == 1
    (runner.CGROUP / "pids.max").write_text("max")
    assert runner._render_resources(4)["workers"] == 1


@pytest.mark.parametrize("limits", [{"current": 480}, {"used": 4 * 1024**3}])
def test_exhausted_budget_fails_before_starting_render(runner, limits, capsys):
    _cgroup(runner, **limits)
    with pytest.raises(SystemExit) as stopped:
        runner._render_resources(4)
    assert stopped.value.code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == "render_resources_exhausted"
    assert result["resources"]["workers"] == 0


@pytest.mark.parametrize("pids,expected_workers", [(512, 1), (2048, 2)])
def test_render_uses_budget_and_preserves_failure_diagnostics(runner, monkeypatch, capsys, pids, expected_workers):
    _cgroup(runner, pids=pids)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    if hasattr(os, "sched_getaffinity"):
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(8)))
    monkeypatch.setattr(runner, "_check", lambda: subprocess.CompletedProcess([], 0, "{}", ""))
    calls = []

    def run(args, *, timeout):
        calls.append((args, timeout))
        return subprocess.CompletedProcess(args, 1, "capture stopped at frame 30", "pthread_create failed")

    monkeypatch.setattr(runner, "_run", run)
    with pytest.raises(SystemExit):
        runner._render("renders/final.mp4", "high", 4)
    args, timeout = calls[0]
    assert args[args.index("--workers") + 1] == str(expected_workers)
    assert "--no-low-memory-mode" not in args
    assert "--no-best-effort" in args and "--strict-all" in args
    assert 1600 <= timeout <= 1680
    result = json.loads(capsys.readouterr().out)
    assert result["stderr"] == "pthread_create failed"
    assert result["stdout"] == "capture stopped at frame 30"
    assert result["resources"]["pids_limit"] == pids


def test_cleanup_targets_only_owned_processes_with_unchanged_identity(runner, monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "_descendants", lambda _pid: {101: "old", 102: "same"})
    monkeypatch.setattr(runner, "_process_stat", lambda pid: (100, "new" if pid == 101 else "same"))
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append(("pid", pid, sig)))
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append(("group", pid, sig)))
    proc = SimpleNamespace(pid=100, communicate=lambda **_: ("stdout", "stderr"))
    assert runner._stop_process(proc) == ("stdout", "stderr")
    assert ("pid", 102, signal.SIGKILL) in calls
    assert not any(pid == 101 for _kind, pid, _sig in calls)
    assert {pid for kind, pid, _sig in calls if kind == "group"} == {100}


@pytest.mark.skipif(sys.platform != "linux", reason="Detached Chrome descendants are discovered through Linux /proc")
def test_timeout_kills_detached_descendant_and_keeps_output(runner, monkeypatch):
    monkeypatch.setattr(runner, "TERMINATE_GRACE_SECONDS", 0.1)
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    script = (
        "import subprocess,sys,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"child=subprocess.Popen([sys.executable,'-c',{child!r}], start_new_session=True); "
        "starttime=Path(f'/proc/{child.pid}/stat').read_text().rpartition(')')[2].split()[19]; "
        "print(child.pid,starttime,flush=True); print('capture stalled',file=sys.stderr,flush=True); time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired) as stopped:
        runner._run([sys.executable, "-c", script], timeout=0.4)
    assert "capture stalled" in stopped.value.stderr
    child_pid, child_starttime = stopped.value.stdout.strip().split()
    stat = Path(f"/proc/{child_pid}/stat")
    # SIGKILL delivery is asynchronous even after the parent has exited.
    deadline = time.monotonic() + 3
    while True:
        try:
            fields = stat.read_text().rpartition(")")[2].split()
        except (FileNotFoundError, ProcessLookupError):
            break
        if fields[19] != child_starttime or fields[0] == "Z":
            break
        assert time.monotonic() < deadline, f"Detached child {child_pid} remains in state {fields[0]}"
        time.sleep(0.01)


def test_timeout_keeps_output_and_restores_cancellation_handler(runner, monkeypatch):
    monkeypatch.setattr(runner, "TERMINATE_GRACE_SECONDS", 0.1)
    before = signal.getsignal(signal.SIGTERM)
    script = "import time; print('capture started',flush=True); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired) as stopped:
        runner._run([sys.executable, "-c", script], timeout=0.2)
    assert stopped.value.stdout == "capture started\n"
    assert signal.getsignal(signal.SIGTERM) == before


def test_outer_cancellation_cleans_cli_before_restoring_handler(runner, monkeypatch):
    before = signal.getsignal(signal.SIGTERM)
    stopped = []

    def communicate(**_kwargs):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    proc = SimpleNamespace(communicate=communicate)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(runner, "_stop_process", lambda child: stopped.append(child))
    with pytest.raises(SystemExit) as cancelled:
        runner._run(["hyperframes", "render"], timeout=30)
    assert cancelled.value.code == 128 + signal.SIGTERM
    assert stopped == [proc]
    assert signal.getsignal(signal.SIGTERM) == before


def test_timeout_diagnostics_accept_partial_bytes(runner):
    assert runner._text_tail(b"partial\xff", 7) == "artial\ufffd"


def test_render_separates_capture_from_encoding_without_changing_other_commands(runner, monkeypatch):
    environments = []

    def process(_args, **kwargs):
        environments.append(kwargs["env"])
        return SimpleNamespace(returncode=0, communicate=lambda **_kwargs: ("ok", ""))

    monkeypatch.delenv("PRODUCER_ENABLE_STREAMING_ENCODE", raising=False)
    monkeypatch.setattr(subprocess, "Popen", process)
    runner._run(["hyperframes", "render", ".", "--quality", "high"], timeout=30)
    runner._run(["hyperframes", "check", ".", "--strict"], timeout=30)
    assert environments[0]["PRODUCER_ENABLE_STREAMING_ENCODE"] == "false"
    assert "PRODUCER_ENABLE_STREAMING_ENCODE" not in environments[1]
    assert environments[0]["HYPERFRAMES_BROWSER_PATH"] == "/usr/bin/chromium"
