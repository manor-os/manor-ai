"""Resource admission and process cleanup for the isolated video CLI wrapper."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
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


@pytest.mark.parametrize("pids,expected_workers", [(512, 1), (2048, 3)])  # capped at 4 workers
def test_render_uses_budget_and_preserves_failure_diagnostics(runner, monkeypatch, capsys, pids, expected_workers):
    _cgroup(runner, pids=pids)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    if hasattr(os, "sched_getaffinity"):
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(8)))
    monkeypatch.setattr(runner, "_check", lambda **_: subprocess.CompletedProcess([], 0, "{}", ""))
    calls = []

    def run(args, *, timeout):
        calls.append((args, timeout))
        return subprocess.CompletedProcess(args, 1, "capture stopped at frame 30", "unexpected renderer exit")

    monkeypatch.setattr(runner, "_run", run)
    with pytest.raises(SystemExit):
        runner._render("renders/final.mp4", "high", 4)
    args, timeout = calls[0]
    assert args[args.index("--workers") + 1] == str(expected_workers)
    assert "--no-low-memory-mode" not in args
    assert "--no-best-effort" in args and "--strict-all" in args
    assert 1600 <= timeout <= 1680
    # Not a sandbox limit: reported as it is, without a retry.
    assert len(calls) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == "render_failed"
    assert result["stderr"] == "unexpected renderer exit"
    assert result["stdout"] == "capture stopped at frame 30"
    assert result["resources"]["pids_limit"] == pids


# HyperFrames 0.7.105's own log lines for a render whose page played six
# narration clips the render project did not contain (01M58KZRHR9WJG0GMFZJEFPTTY).
_AUDIO_FAILURE_LOG = (
    '[WARN] Render completed capture with correctness warnings {"renderJobId":"r","strictness":"strict",'
    '"warningCodes":["audio_processing_failed"],"warningReasons":["source_not_found"],'
    '"warningStages":["source"],"warningOwners":["user"],"warningRetryable":false}\n'
    '[INFO] [Render] Failure summary {"renderJobId":"r","failedStage":"Processing audio tracks",'
    '"error":"Render blocked by 1 correctness warning: audio_processing_failed","browserConsoleErrors":'
    '["[Browser:HTTP404] GET http://localhost:40493/assets/references/norm-zh-s03.wav resource=media Not Found",'
    '"[Browser:HTTP404] GET http://localhost:40493/favicon.ico resource=other Not Found"]}\n'
)


def test_failed_audio_mix_is_reported_as_a_structured_render_failure(runner, monkeypatch, capsys):
    """The agent saw only render_failed and a warning code, and blamed the composition."""
    _cgroup(runner)
    monkeypatch.setattr(runner, "_check", lambda **_: subprocess.CompletedProcess([], 0, "{}", ""))
    calls = []

    def run(args, *, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 1, "20%  Failed: Render blocked by 1 correctness warning: audio_processing_failed",
            _AUDIO_FAILURE_LOG,
        )

    monkeypatch.setattr(runner, "_run", run)
    with pytest.raises(SystemExit) as stopped:
        runner._render("renders/final.mp4", "high", 2, reviewed=True)
    result = json.loads(capsys.readouterr().out)

    assert stopped.value.code == 1 and len(calls) == 1
    assert (result["code"], result["reason"], result["step"]) == (
        "render_audio_failed", "source_not_found", "audio_process",
    )
    assert result["retryable"] is False and result["owner"] == "user"
    assert result["missing_media"] == ["assets/references/norm-zh-s03.wav"]
    assert "norm-zh-s03.wav" in result["error"] and "push_assets" in result["error"]
    assert result["stderr"] == _AUDIO_FAILURE_LOG


# 8 clips cut into 14 layers on a 4-CPU, 512-PID render sandbox, as in the
# CITY PULSE montage, down to the smallest runner and a nearly full sandbox.
@pytest.mark.parametrize("pids,current,cpu,workers,media", [
    (512, 12, 4, 1, 14),
    (512, 12, 4, 1, 8),
    (512, 40, 1, 1, 14),
    (2048, 12, 4, 2, 14),
    (384, 30, 2, 1, 14),
])
def test_ffmpeg_concurrency_fits_the_sandbox_pid_budget(runner, pids, current, cpu, workers, media):
    resources = {"pids_limit": pids, "pids_current": current, "cpu_limit": cpu}
    plan = runner._media_concurrency(resources, browser_workers=workers, media_elements=media)
    assert 1 <= plan["slots"] <= cpu and 1 <= plan["cpus_per_process"] <= cpu
    claimed = (
        current + runner.RESERVED_PIDS + workers * runner.WORKER_PIDS + media
        + plan["peak_ffmpeg_tasks"]
    )
    assert claimed <= pids


def _limit_failure(runner, *, denied):
    """A render that ran the sandbox out of PIDs during extraction."""
    events = runner.CGROUP / "pids.events"
    count = int(events.read_text().split()[1]) + denied
    events.write_text(f"max {count}\n")
    return subprocess.CompletedProcess(
        [], 1, "Extracting frames from video 14/14",
        'Video "S11-video" captured 0 of expected 60 frames (coverage 0.0%, threshold 95.0%).',
    )


@pytest.mark.parametrize("second_succeeds", [True, False])
def test_pid_limit_failure_retries_once_at_reduced_concurrency(runner, monkeypatch, capsys, second_succeeds):
    _cgroup(runner, pids=512)
    (runner.CGROUP / "pids.events").write_text("max 0\n")
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    if hasattr(os, "sched_getaffinity"):
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(8)))
    monkeypatch.setattr(runner, "_check", lambda **_: subprocess.CompletedProcess([], 0, "{}", ""))
    plans = []

    def run(args, *, timeout):
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args, 0, '{"format": {"duration": "40"}}', "")
        plans.append((args[args.index("--workers") + 1], dict(runner._media_plan)))
        if len(plans) == 2 and second_succeeds:
            (runner.PROJECT / "renders/final.mp4").write_bytes(b"mp4")
            return subprocess.CompletedProcess(args, 0, "rendered", "")
        return _limit_failure(runner, denied=3)

    monkeypatch.setattr(runner, "_run", run)
    with pytest.raises(SystemExit) as stopped:
        runner._render("renders/final.mp4", "high", 2)
    result = json.loads(capsys.readouterr().out)

    assert len(plans) == 2, "exactly one automatic retry"
    first, retry = plans
    assert first[1]["reduced"] is False and first[1]["slots"] > 1
    assert retry[0] == "1"
    assert (retry[1]["slots"], retry[1]["cpus_per_process"], retry[1]["reduced"]) == (1, 1, True)
    if second_succeeds:
        assert stopped.value.code == 0
        assert result["status"] == "ok"
        assert [attempt["reason"] for attempt in result["resource_retry"]] == ["pid_limit"]
        return
    assert stopped.value.code == 1
    assert result["code"] == "render_resource_limit"
    assert (result["reason"], result["step"]) == ("pid_limit", "video_frame_extraction")
    assert result["retryable"] is False and result["automatic_retry"] == "exhausted"
    assert [attempt["pids_denied"] for attempt in result["attempts"]] == [3, 3]
    assert "inspect" not in result["error"] and "minimum concurrency" in result["error"]
    assert "captured 0 of expected 60 frames" in result["stderr"]


def test_limit_failure_without_time_for_a_retry_is_still_structured(runner, monkeypatch, capsys):
    _cgroup(runner, pids=512)
    (runner.CGROUP / "pids.events").write_text("max 0\n")
    (runner.CGROUP / "memory.events").write_text("oom 0\noom_kill 0\n")
    monkeypatch.setattr(runner, "_check", lambda **_: subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr(runner, "RESOURCE_RETRY_MIN_SECONDS", 10**6)
    calls = []

    def run(args, *, timeout):
        calls.append(args)
        (runner.CGROUP / "memory.events").write_text("oom 1\noom_kill 1\n")
        return subprocess.CompletedProcess(args, 1, "", "Error: Protocol error: Target closed")

    monkeypatch.setattr(runner, "_run", run)
    with pytest.raises(SystemExit):
        runner._render("renders/final.mp4", "high", 1)
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert (result["code"], result["reason"], result["step"]) == ("render_resource_limit", "oom", "frame_capture")
    assert result["automatic_retry"] == "insufficient_time_left"
    assert result["retryable"] is False


# HyperFrames 0.7.105's log for the local-eval motion showreel and picture book
# (runs 01M5FQ0GCWW2GC0D5V21036DRD / 01M5HA8CCV4EDA66RFGVAAW9AN): every frame was
# captured, then strict mode refused to export. The agent saw only render_failed.
_UNREGISTERED_TIMELINES_LOG = (
    "[FrameCapture] Sub-composition timelines not registered after 65300ms: scene01, scene02. "
    "Compositions that load data asynchronously (e.g. fetch) must register window.__timelines[id] "
    "after setup completes.\n"
    "[FrameCapture:sub_timeline_readiness_timeout] Sub-composition timelines did not become ready "
    "within 65300ms { timeoutMs: 65300 }\n"
    '[WARN] Render completed capture with correctness warnings {"renderJobId":"r","strictness":"strict",'
    '"warningCodes":["sub_timeline_readiness_timeout"],"warningReasons":[],"warningStages":[],"warningOwners":[]}\n'
    '[INFO] [Render] Failure summary {"renderJobId":"r","failedStage":"Streaming frame 1200/1200",'
    '"error":"Render blocked by 1 correctness warning: sub_timeline_readiness_timeout",'
    '"browserConsoleErrors":["[Browser:HTTP404] GET http://localhost:1/favicon.ico resource=other Not Found"]}\n'
    "✗  Render failed\n   Render blocked by 1 correctness warning: sub_timeline_readiness_timeout\n"
)


def _sub_compositions(runner, *, registers: bool) -> None:
    hosts = "".join(
        f'<section id="h{n}" class="clip" data-composition-id="scene0{n}" '
        f'data-composition-src="compositions/s{n}.html" data-start="0" data-duration="4" data-track-index="{n}">'
        "</section>"
        for n in (1, 2)
    )
    (runner.PROJECT / "index.html").write_text(f'<main data-composition-id="root">{hosts}</main>')
    (runner.PROJECT / "compositions").mkdir()
    script = "<script>window.__timelines['scene0{n}']=tl;</script>" if registers else ""
    for n in (1, 2):
        (runner.PROJECT / f"compositions/s{n}.html").write_text(
            f'<template><div data-composition-id="scene0{n}"></div>{script.format(n=n)}</template>'
        )


@pytest.mark.parametrize("case", [
    "killed", "oom", "browser_crash", "missing_asset", "unregistered_timelines", "slow_timelines", "unknown",
])
def test_every_render_failure_is_classified_with_a_next_step_and_the_same_diagnostics(
    runner, monkeypatch, capsys, case,
):
    """Exit status, signal, stage, error lines and memory reach the agent for every cause."""
    _cgroup(runner, memory=4 * 1024**3, used=0)
    (runner.CGROUP / "memory.events").write_text("oom 0\noom_kill 0\n")
    (runner.CGROUP / "memory.peak").write_text(str(3 * 1024**3))
    (runner.CGROUP / "pids.events").write_text("max 0\n")
    monkeypatch.setattr(runner, "RESOURCE_RETRY_MIN_SECONDS", 10**6)  # report, never retry
    if case in {"unregistered_timelines", "slow_timelines"}:
        _sub_compositions(runner, registers=case == "slow_timelines")
    returncode, stderr = {
        "killed": (-9, "[INFO] capturing frame 400/1200\n"),
        "oom": (1, "Error: Protocol error: Target closed\n"),
        "browser_crash": (1, "Error: Protocol error (Runtime.callFunctionOn): Target closed\n"),
        "missing_asset": (1, "[Browser:HTTP404] GET http://localhost:9/assets/clip.mp4 resource=media Not Found\n"
                             "Render failed: Video frame extraction failed\n"),
        "unregistered_timelines": (1, _UNREGISTERED_TIMELINES_LOG),
        "slow_timelines": (1, _UNREGISTERED_TIMELINES_LOG),
        "unknown": (1, "Error: something unexpected in the encoder bridge\n"),
    }[case]

    def run(args, *, timeout):
        if case == "oom":
            (runner.CGROUP / "memory.events").write_text("oom 1\noom_kill 1\n")
        result = subprocess.CompletedProcess(args, returncode, "  42%  Streaming frame 500/1200\n", stderr)
        result.duration_seconds = 61.5
        return result

    monkeypatch.setattr(runner, "_run", run)
    with pytest.raises(SystemExit) as stopped:
        runner._render("renders/final.mp4", "high", 2, reviewed=True)
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    expected = {
        "killed": ("render_process_killed", "killed", True),
        "oom": ("render_resource_limit", "oom", False),
        "browser_crash": ("render_browser_crashed", "browser_crashed", True),
        "missing_asset": ("render_asset_invalid", "asset_missing", False),
        "unregistered_timelines": ("render_composition_invalid", "sub_timeline_readiness_timeout", False),
        "slow_timelines": ("render_composition_invalid", "sub_timeline_slow", True),
        "unknown": ("render_failed", "unknown", False),
    }[case]
    assert stopped.value.code != 0
    assert (result["code"], result["reason"], result["retryable"]) == expected
    assert result["next_step"] and result["error"]
    diagnostics = result["diagnostics"]
    assert diagnostics["command"] == "render" and diagnostics["stage"] == "render"
    assert diagnostics["exit_code"] == returncode and diagnostics["duration_seconds"] == 61.5
    assert (diagnostics["signal"], diagnostics["signal_name"]) == ((9, "SIGKILL") if case == "killed" else (None, None))
    assert diagnostics["memory"]["limit"] == 4 * 1024**3 and diagnostics["memory"]["peak"] == 3 * 1024**3
    assert diagnostics["memory"]["oom_kill"] == (1 if case == "oom" else 0)
    assert not any("42%" in line for line in diagnostics["error_lines"])  # progress is never evidence
    # The one line the Sandbox Service logs for the failed exec.
    summary = captured.err.strip().splitlines()[-1]
    assert summary.startswith("video_runtime failure ") and f"code={expected[0]}" in summary
    if case == "missing_asset":
        assert result["missing_media"] == ["assets/clip.mp4"]
    if case == "unregistered_timelines":
        assert result["unregistered_timelines"] == ["scene01", "scene02"]
        assert "data-no-timeline" in result["next_step"]
    if case in {"killed", "browser_crash"}:
        assert "render_workers=1" in result["next_step"]


def test_review_failures_report_the_same_diagnostics_as_render(runner, monkeypatch, capsys):
    """Guard the class: a crashed check or snapshot is described like a failed render."""
    _cgroup(runner)
    shapes = []
    for crashed_step in ("quality_check", "snapshot"):
        def browser_step(step, _build, **_kwargs):
            returncode = 1 if step == crashed_step else 0
            result = subprocess.CompletedProcess([], returncode, "{}", "Error: Page crashed!\n" if returncode else "")
            result.args = ["hyperframes", "check" if step == "quality_check" else "snapshot"]
            return result

        monkeypatch.setattr(runner, "_browser_step", browser_step)
        with pytest.raises(SystemExit):
            runner._review("1,2")
        result = json.loads(capsys.readouterr().out)
        assert result["code"] == ("quality_gate_failed" if crashed_step == "quality_check" else "snapshot_failed")
        assert (result["reason"], result["retryable"]) == ("browser_crashed", True)
        assert result["diagnostics"]["stage"] == crashed_step
        assert result["diagnostics"]["error_lines"] == ["Error: Page crashed!"]
        shapes.append(set(result["diagnostics"]))
    assert shapes[0] == shapes[1] == set(runner._diagnostics())


def _governor_env(tmp_path, slots):
    log = tmp_path / "ffmpeg.log"
    fake = tmp_path / "fake-ffmpeg"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        f"log = open({str(log)!r}, 'a')\n"
        "log.write(f'start {time.monotonic()} {sys.argv[1:]}\\n'); log.flush()\n"
        "time.sleep(0.5)\n"
        "log.write(f'end {time.monotonic()}\\n')\n"
    )
    fake.chmod(0o755)
    env = {
        **os.environ, "MANOR_MEDIA_FFMPEG": str(fake), "MANOR_MEDIA_SLOTS": str(slots),
        "MANOR_MEDIA_SLOT_DIR": str(tmp_path / "slots"), "MANOR_MEDIA_CPUS": "1",
    }
    return env, log


def _max_overlap(log):
    events = sorted(
        (float(line.split()[1]), 1 if line.startswith("start") else -1)
        for line in log.read_text().splitlines()
    )
    running = peak = 0
    for _at, step in events:
        running += step
        peak = max(peak, running)
    return peak


def test_media_governor_admits_at_most_its_slots(runner, tmp_path):
    env, log = _governor_env(tmp_path, slots=2)
    procs = [
        subprocess.Popen(
            [sys.executable, str(runner.MEDIA_GOVERNOR), "-i", f"clip-{index}.mp4", f"out-{index}/%05d.jpg"],
            env=env,
        )
        for index in range(5)
    ]
    assert [proc.wait(timeout=20) for proc in procs] == [0] * 5
    assert log.read_text().count("start") == 5
    assert _max_overlap(log) == 2


def test_media_governor_never_queues_a_pipe_reader(runner, tmp_path):
    """The reader of a pipeline is fed by a process that may hold the only slot."""
    env, log = _governor_env(tmp_path, slots=1)
    producer = subprocess.Popen(
        [sys.executable, str(runner.MEDIA_GOVERNOR), "-i", "clip.mp4", "-f", "nut", "pipe:1"], env=env,
    )
    time.sleep(0.15)
    reader = subprocess.Popen(
        [sys.executable, str(runner.MEDIA_GOVERNOR), "-f", "nut", "-i", "pipe:0", "out/%05d.jpg"], env=env,
    )
    assert producer.wait(timeout=20) == 0 and reader.wait(timeout=20) == 0
    assert _max_overlap(log) == 2


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")
def test_media_governor_holds_the_last_frame_past_the_end_of_a_clip(runner, tmp_path):
    """Staging EP.04 (2026-10-26): clips fitted to their container duration end
    a frame after their last video frame; a review frame there asked FFmpeg for
    a frame past the end, got none and showed the clip black. Playback holds the
    last frame, so the governor extracts that instead."""
    clip = tmp_path / "clip.mp4"
    subprocess.run([
        "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=25:duration=1",
        "-f", "lavfi", "-i", "sine=duration=1.2", "-pix_fmt", "yuv420p", str(clip),
    ], check=True)
    env = {
        **os.environ, "MANOR_MEDIA_FFMPEG": shutil.which("ffmpeg"), "MANOR_MEDIA_FFPROBE": shutil.which("ffprobe"),
        "MANOR_MEDIA_SLOTS": "0",
    }

    def extract(at: str) -> Path:
        out = tmp_path / f"frame-{at}.png"
        subprocess.run(
            [sys.executable, str(runner.MEDIA_GOVERNOR), "-hide_banner", "-loglevel", "error",
             "-ss", at, "-i", str(clip), "-frames:v", "1", "-q:v", "2", "-y", str(out)],
            env=env, check=False, timeout=60,
        )
        return out

    assert extract("0.5").stat().st_size > 0
    past_end = extract("1.15")  # inside the container (audio), after the last video frame
    assert past_end.is_file() and past_end.stat().st_size > 0

    spec = importlib.util.spec_from_file_location("media_governor_hold", runner.MEDIA_GOVERNOR)
    governor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(governor)
    ffprobe = shutil.which("ffprobe")
    clamped = governor.hold_last_frame(["-ss", "1.15", "-i", str(clip), "-frames:v", "1", "o.png"], ffprobe)
    assert float(clamped[1]) == pytest.approx(0.96, abs=0.02)
    # Anything that is not a single-frame extraction past the end is left alone.
    within = ["-ss", "0.5", "-i", str(clip), "-frames:v", "1", "o.png"]
    assert governor.hold_last_frame(within, ffprobe) == within
    many = ["-ss", "1.15", "-i", str(clip), "-frames:v", "30", "o-%03d.png"]
    assert governor.hold_last_frame(many, ffprobe) == many


def test_media_governor_cpu_windows_rotate_across_slots(runner):
    spec = importlib.util.spec_from_file_location("media_governor_test", runner.MEDIA_GOVERNOR)
    governor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(governor)
    allowed = list(range(14))
    windows = [governor.cpu_window(allowed, 4, index, base=12) for index in range(3)]
    assert windows == [{12, 13, 0, 1}, {2, 3, 4, 5}, {6, 7, 8, 9}]
    assert governor.cpu_window([0, 1], 4, 0, base=0) == {0, 1}
    assert governor.reads_pipe(["-f", "image2pipe", "-i", "-", "out.mp4"])
    assert not governor.reads_pipe(["-i", "clip.mp4", "pipe:1"])


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


@pytest.mark.parametrize(
    "free_bytes,streaming",
    [
        (64 * 1024**3, "false"),  # room for every frame: capture, then encode
        (1024**3, "true"),  # 1 GB tmpfs on a 2 GB runner: stream the frames
    ],
)
def test_render_separates_capture_from_encoding_without_changing_other_commands(
    runner, monkeypatch, free_bytes, streaming,
):
    """Regression: capture-first filled a 1 GB sandbox /tmp and failed a
    107-second episode render at export."""
    environments = []
    monkeypatch.setattr(runner, "_free_bytes", lambda _path: free_bytes)

    def process(_args, **kwargs):
        environments.append(kwargs["env"])
        return SimpleNamespace(returncode=0, communicate=lambda **_kwargs: ("ok", ""))

    monkeypatch.delenv("PRODUCER_ENABLE_STREAMING_ENCODE", raising=False)
    monkeypatch.setattr(subprocess, "Popen", process)
    runner._run(["hyperframes", "render", ".", "--quality", "high"], timeout=30)
    runner._run(["hyperframes", "check", ".", "--strict"], timeout=30)
    assert environments[0]["PRODUCER_ENABLE_STREAMING_ENCODE"] == streaming
    assert "PRODUCER_ENABLE_STREAMING_ENCODE" not in environments[1]
    assert environments[0]["HYPERFRAMES_BROWSER_PATH"] == "/usr/bin/chromium"


@pytest.mark.parametrize("stat", [None, "file 524288000\nshmem 426770432\n"])
def test_smallest_runner_still_gets_its_single_render_worker(runner, stat):
    """Staging EP.04: a 1.5 GB sandbox holding 407 MB of tmpfs sources reported
    653 MB used after the strict check and refused to render at all."""
    _cgroup(runner, pids=512, current=6, memory=1610612736, used=653262848, cpu="100000 100000")
    if stat is not None:
        (runner.CGROUP / "memory.stat").write_text(stat)
    resources = runner._render_resources(1)
    assert resources["workers"] == 1
    assert resources["memory_reclaimable"] == (97517568 if stat else 0)


@pytest.mark.parametrize("tmp_free,workdir_free,expected", [
    (1000 * 1024**2, 1600 * 1024**2, "workdir"),  # 2 GB runner: /tmp is under 1 GB
    (64 * 1024**3, 1600 * 1024**2, "tmp"),
])
def test_render_temp_dir_is_the_roomier_filesystem(runner, monkeypatch, tmp_path, tmp_free, workdir_free, expected):
    """HyperFrames refuses to render with under 1 GB free in TMPDIR; staging
    EP.04 stopped at "Low disk space: 1.0 GB free at /tmp"."""
    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(system_tmp))
    monkeypatch.setattr(
        runner, "_free_bytes",
        lambda path: tmp_free if Path(path) == system_tmp else workdir_free,
    )
    environments = []

    def process(_args, **kwargs):
        environments.append(kwargs["env"])
        return SimpleNamespace(returncode=0, communicate=lambda **_kwargs: ("ok", ""))

    monkeypatch.setattr(subprocess, "Popen", process)
    runner._run(["hyperframes", "render", ".", "--quality", "high"], timeout=30)
    workdir_tmp = runner.PROJECT / ".hf-tmp"
    assert environments[0]["TMPDIR"] == str(workdir_tmp if expected == "workdir" else system_tmp)
    assert environments[0]["PRODUCER_ENABLE_STREAMING_ENCODE"] == ("true" if expected == "workdir" else "false")


def test_review_commands_use_the_roomier_temp_dir_too(runner, monkeypatch, tmp_path):
    """Staging EP.05 review: snapshot writes each extracted <video> frame and
    Chromium's profile to TMPDIR; with 60 MB free on /tmp every frame failed
    (snapshot_video_frames_missing) while the same files extracted fine."""
    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(system_tmp))
    monkeypatch.setattr(
        runner, "_free_bytes",
        lambda path: 60 * 1024**2 if Path(path) == system_tmp else 1600 * 1024**2,
    )
    environments = []

    def process(_args, **kwargs):
        environments.append(kwargs["env"])
        return SimpleNamespace(returncode=0, communicate=lambda **_kwargs: ("ok", ""))

    monkeypatch.setattr(subprocess, "Popen", process)
    runner._run(["hyperframes", "snapshot", ".", "--at", "1,2"], timeout=30)
    runner._run(["hyperframes", "check", "."], timeout=30)
    assert [env["TMPDIR"] for env in environments] == [str(runner.PROJECT / ".hf-tmp")] * 2


# Incident 01M50AHSWQEPZ7YTC4BQH6FDC0: on a host at load ~274 every review
# failed with "Snapshot failed: Navigation timeout of 10000 ms exceeded";
# HyperFrames' snapshot/check page.goto is 10 s unless the env var is set, and
# the runtime's --timeout=30000 never reached it.
NAVIGATION_FAILURE = "\n✗ Snapshot failed: Navigation timeout of 10000 ms exceeded"


@pytest.mark.parametrize("configured,media,expected", [
    (None, 0, 60_000),
    ("90000", 0, 90_000),
    ("5000", 0, 10_000),  # never below HyperFrames' own default
    (None, 3, 60_000 + 3 * 2_000 + 50 * 100),
    (None, 200, 240_000),  # scaled, but capped
])
def test_browser_timeouts_are_sized_for_the_sandbox_and_reach_every_hyperframes_command(
    runner, monkeypatch, configured, media, expected,
):
    if configured is None:
        monkeypatch.delenv(runner.NAVIGATION_TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(runner.NAVIGATION_TIMEOUT_ENV, configured)
    if media:
        (runner.PROJECT / "index.html").write_text("<video></video>" * media)
        (runner.PROJECT / "song.wav").write_bytes(b"\0" * (50 * 1024**2))
    assert runner._browser_timeout_plan()["navigation_ms"] == expected
    assert runner._browser_timeout_plan(scale=2)["navigation_ms"] == 2 * expected

    environments = []

    def process(_args, **kwargs):
        environments.append(kwargs["env"])
        return SimpleNamespace(returncode=0, communicate=lambda **_kwargs: ("ok", ""))

    monkeypatch.setattr(subprocess, "Popen", process)
    for command in ("snapshot", "check", "render"):
        runner._run(["hyperframes", command, "."], timeout=30)
    for env in environments:
        assert int(env["PRODUCER_PAGE_NAVIGATION_TIMEOUT_MS"]) == expected
        # Render's own window.__hf wait (45 s) is never shortened.
        assert int(env["PRODUCER_PLAYER_READY_TIMEOUT_MS"]) >= max(45_000, expected)


def _review_run(runner, snapshot_results):
    """A review whose check passes and whose snapshots end as given."""
    snapshots = []

    def run(args, *, timeout):
        if args[1] == "check":
            return subprocess.CompletedProcess(args, 0, "{}", "")
        render_ready = next(arg for arg in args if arg.startswith("--timeout="))
        snapshots.append({
            "timeout": timeout, "plan": dict(runner._browser_plan),
            "render_ready": int(render_ready.partition("=")[2]),
        })
        outcome = snapshot_results[len(snapshots) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome.returncode == 0:
            shot = runner.PROJECT / runner.REVIEW_DIR / "frame-0.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            shot.write_bytes(b"png")
        return outcome

    return run, snapshots


@pytest.mark.parametrize("first,second,expected", [
    ("navigation", "ok", "ok"),
    ("navigation", "navigation", "snapshot_timeout"),
    ("process", "process", "snapshot_timeout"),
    ("crash", None, "snapshot_failed"),
])
def test_review_snapshot_timeout_retries_once_with_longer_timeouts(
    runner, monkeypatch, capsys, first, second, expected,
):
    monkeypatch.delenv(runner.NAVIGATION_TIMEOUT_ENV, raising=False)
    outcomes = {
        "navigation": subprocess.CompletedProcess([], 1, "", NAVIGATION_FAILURE),
        "process": subprocess.TimeoutExpired(["hyperframes"], 300, b"capturing", b""),
        "crash": subprocess.CompletedProcess([], 1, "", "Snapshot failed: composition threw"),
        "ok": subprocess.CompletedProcess([], 0, "saved", ""),
    }
    run, snapshots = _review_run(runner, [outcomes[first], outcomes.get(second)])
    monkeypatch.setattr(runner, "_run", run)
    with pytest.raises(SystemExit) as stopped:
        runner._review("31.6")
    result = json.loads(capsys.readouterr().out)

    if expected == "snapshot_failed":
        # Not a timeout: reported as it is, without a retry.
        assert len(snapshots) == 1
        assert result["code"] == "snapshot_failed"
        return
    assert len(snapshots) == 2, "exactly one automatic retry"
    first_try, retry = snapshots
    assert first_try["plan"]["navigation_ms"] == 60_000
    assert retry["plan"]["navigation_ms"] == 120_000
    assert retry["timeout"] > first_try["timeout"]
    assert retry["render_ready"] > first_try["render_ready"] >= 30_000
    if expected == "ok":
        assert stopped.value.code == 0
        assert result["review_passed"] is True
        return
    assert stopped.value.code != 0
    reason = "navigation_timeout" if first == "navigation" else "process_timeout"
    assert (result["code"], result["reason"], result["step"]) == ("snapshot_timeout", reason, "snapshot")
    assert result["retryable"] is True and result["automatic_retry"] == "exhausted"
    assert [attempt["navigation_timeout_ms"] for attempt in result["attempts"]] == [60_000, 120_000]
    assert "operator" not in result["error"] and "without editing the project" in result["error"]
    assert set(result["host_load"]) == {"loadavg_1m", "cpu_pressure_avg10"}


# Staging EP.05 (2026-10-24): snapshot could not copy the presenter clips'
# frames into the page, said so only in this warning, and review passed with
# black videos while the render of the same files was correct.
BLANK_VIDEO_WARNING = (
    "   ⚠ 1/1 active <video> frame(s) could not be extracted at 77.0s — "
    "those videos will appear black/stale in this snapshot"
)


@pytest.mark.parametrize("second,ffmpeg,expected", [
    ("ok", "reads", "passed"),
    ("blank", "reads", "host"),
    ("blank", "fails", "asset"),
])
def test_review_never_passes_frames_with_black_videos(
    runner, monkeypatch, capsys, tmp_path_factory, second, ffmpeg, expected,
):
    (runner.PROJECT / "assets").mkdir()
    (runner.PROJECT / "assets/clip 1.mp4").write_bytes(b"mp4")
    (runner.PROJECT / "index.html").write_text(
        '<video id="p" src="./assets/clip%201.mp4"></video><video src="https://cdn.test/x.mp4"></video>'
    )
    # Snapshot's FFmpeg is the launcher in front of this binary; the probe uses the same.
    binary = tmp_path_factory.mktemp("bin") / "ffmpeg"
    binary.write_text(
        '#!/bin/sh\nfor out; do :; done\n'
        + ('printf png > "$out"\n' if ffmpeg == "reads" else 'echo "moov atom not found" >&2; exit 1\n')
    )
    binary.chmod(0o755)
    monkeypatch.setenv("HYPERFRAMES_FFMPEG_PATH", str(binary))
    blank = subprocess.CompletedProcess([], 0, "saved\n" + BLANK_VIDEO_WARNING, "")
    outcomes = {"blank": blank, "ok": subprocess.CompletedProcess([], 0, "saved", "")}
    run, snapshots = _review_run(runner, [blank, outcomes[second]])
    monkeypatch.setattr(runner, "_run", run)
    with pytest.raises(SystemExit) as stopped:
        runner._review("77")
    result = json.loads(capsys.readouterr().out)

    assert len(snapshots) == 2, "exactly one automatic retry"
    if expected == "passed":
        assert stopped.value.code == 0 and result["review_passed"] is True
        return
    assert stopped.value.code != 0 and "review_passed" not in result and "files" not in result
    assert (result["code"], result["step"]) == ("snapshot_video_frames_missing", "snapshot")
    assert result["automatic_retry"] == "exhausted"
    assert result["attempts"][-1]["blank_video_frames"] == [
        {"time": 77.0, "reason": "frame_not_extracted", "videos": 1, "active_videos": 1},
    ]
    [probe] = result["video_extraction"]["videos"]  # the remote video is not the sandbox's
    assert probe["src"] == "assets/clip 1.mp4"
    if expected == "host":
        assert probe["extracted"] is True and result["retryable"] is True
        assert "without editing the project" in result["error"]
    else:
        assert probe["extracted"] is False and result["retryable"] is False
        assert "moov atom not found" in probe["stderr"]
        assert "assets/clip 1.mp4" in result["error"] and "re-encode" in result["error"]


def test_review_names_a_clip_shown_past_the_end_of_its_footage(
    runner, monkeypatch, capsys, tmp_path_factory,
):
    """Staging EP.04 (2026-10-26): the end-card background was a 4.04 s clip in
    a 5 s slot; at the 110.6 s tail frame snapshot had no frame to copy and
    review blamed the host. It is the composition's timing, and says which clip."""
    (runner.PROJECT / "assets").mkdir()
    (runner.PROJECT / "assets/end.mp4").write_bytes(b"mp4")
    (runner.PROJECT / "index.html").write_text(
        '<video id="b" src="assets/end.mp4" data-start="105.702" data-duration="5.0" '
        'data-media-start="0"></video>'
    )
    bins = tmp_path_factory.mktemp("bin")
    (bins / "ffmpeg").write_text('#!/bin/sh\nfor out; do :; done\nprintf png > "$out"\n')
    (bins / "ffprobe").write_text("#!/bin/sh\necho 4.041667\n")
    for name in ("ffmpeg", "ffprobe"):
        (bins / name).chmod(0o755)
    monkeypatch.setenv("HYPERFRAMES_FFMPEG_PATH", str(bins / "ffmpeg"))
    monkeypatch.setenv("HYPERFRAMES_FFPROBE_PATH", str(bins / "ffprobe"))
    blank = subprocess.CompletedProcess([], 0, "saved\n" + BLANK_VIDEO_WARNING.replace("77.0s", "110.6s"), "")
    run, _snapshots = _review_run(runner, [blank, blank])
    monkeypatch.setattr(runner, "_run", run)
    with pytest.raises(SystemExit):
        runner._review("110.6")
    result = json.loads(capsys.readouterr().out)

    assert result["code"] == "snapshot_video_frames_missing" and result["retryable"] is False
    [past] = result["videos_past_source_end"]
    assert (past["src"], past["time"], past["footage_seconds"]) == ("assets/end.mp4", 110.6, 4.042)
    assert "assets/end.mp4" in result["error"] and "data-playback-rate" in result["error"]
    assert "sandbox host" not in result["error"]


def test_check_timeout_without_time_for_a_retry_is_still_structured(runner, monkeypatch, capsys):
    monkeypatch.setattr(runner, "REVIEW_BUDGET_SECONDS", 100)
    calls = []

    def run(args, *, timeout):
        calls.append(args[1])
        return subprocess.CompletedProcess(
            args, 1, '{"ok": false, "error": "Navigation timeout of 10000 ms exceeded"}', "",
        )

    monkeypatch.setattr(runner, "_run", run)
    with pytest.raises(SystemExit):
        runner._review("0")
    result = json.loads(capsys.readouterr().out)
    assert calls == ["check"]
    assert (result["code"], result["step"]) == ("snapshot_timeout", "quality_check")
    assert result["automatic_retry"] == "insufficient_time_left" and result["retryable"] is True


@pytest.mark.parametrize("second_succeeds", [True, False])
def test_render_navigation_timeout_retries_once_with_longer_timeouts(runner, monkeypatch, capsys, second_succeeds):
    _cgroup(runner, pids=512)
    monkeypatch.delenv(runner.NAVIGATION_TIMEOUT_ENV, raising=False)
    monkeypatch.setattr(runner, "_check", lambda **_: subprocess.CompletedProcess([], 0, "{}", ""))
    plans = []

    def run(args, *, timeout):
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args, 0, '{"format": {"duration": "40"}}', "")
        plans.append(dict(runner._browser_plan))
        if len(plans) == 2 and second_succeeds:
            (runner.PROJECT / "renders/final.mp4").write_bytes(b"mp4")
            return subprocess.CompletedProcess(args, 0, "rendered", "")
        return subprocess.CompletedProcess(
            args, 1, "[initSession:screenshot] page.goto start", "Navigation timeout of 60000 ms exceeded",
        )

    monkeypatch.setattr(runner, "_run", run)
    with pytest.raises(SystemExit):
        runner._render("renders/final.mp4", "high", 1)
    result = json.loads(capsys.readouterr().out)
    assert [plan["navigation_ms"] for plan in plans] == [60_000, 120_000]
    if second_succeeds:
        assert result["status"] == "ok"
        assert [attempt["reason"] for attempt in result["browser_timeout_retry"]] == ["navigation_timeout"]
        assert "resource_retry" not in result
        return
    assert (result["code"], result["reason"], result["step"]) == ("render_timeout", "navigation_timeout", "render")
    assert result["retryable"] is True and result["automatic_retry"] == "exhausted"
    assert len(result["attempts"]) == 2


def test_runtime_finds_the_project_beside_its_skill_wherever_the_runner_puts_it(tmp_path):
    """A runner with per-Skill work areas serves a baked Skill from /skills/<slot>
    without rewriting its code (2026-10-19 staging: every review was
    project_not_found), so the runtime must not name /skill itself."""

    source = Path(__file__).resolve().parents[1] / "packages/core/ai/skills/video-edit/scripts"
    skill = tmp_path / "skills" / "video-edit-runtime"
    (skill / "scripts").mkdir(parents=True)
    for script in source.glob("*.py"):
        (skill / "scripts" / script.name).write_bytes(script.read_bytes())
    (skill / "project").mkdir()
    (skill / "project" / "index.html").write_text("<html></html>")

    result = subprocess.run(
        [sys.executable, str(skill / "scripts" / "video_runtime.py"), "manifest"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 0, payload
    assert [item["path"] for item in payload["files"]] == ["index.html"]


_RENDERER_OUTPUT = r'''
import json, sys, time
for done, pct in [(0, 25), (30, 30), (60, 31), (150, 50), (300, 75)]:
    print("  " + "█" * 5 + "░" * 20 + f"  {pct}%  Capturing frame {done}/300", flush=True)
    print("@hf-progress " + json.dumps({"code": "capture", "done": done, "total": 300, "pct": pct}), flush=True)
    time.sleep(0.2)
print("@hf-progress " + json.dumps({"code": "encode", "done": 10, "total": 300, "pct": 76}), flush=True)
print("stderr tail", file=sys.stderr)
'''


def test_a_watched_render_reports_its_stage_and_frames_and_keeps_its_output(runner, monkeypatch, tmp_path):
    events = tmp_path / "events"
    monkeypatch.setenv("MANOR_SANDBOX_EVENT_FILE", str(events))
    monkeypatch.setattr(runner, "ACTIVITY_HEARTBEAT_SECONDS", 3600)
    monkeypatch.setattr(runner, "PROGRESS_READ_SECONDS", 0.02)

    result = runner._run_reporting_progress(
        [sys.executable, "-c", _RENDERER_OUTPUT], env=dict(os.environ), timeout=30,
    )

    assert result.returncode == 0
    # The caller still gets the whole output: render diagnostics read it.
    assert result.stdout.count("@hf-progress") == 6 and result.stderr == "stderr tail\n"
    reported = [json.loads(line) for line in events.read_text().splitlines()]
    assert {event["type"] for event in reported} == {"progress"}
    payloads = [event["payload"] for event in reported]
    # Frame counts move in steps, never once per frame; a new stage always shows.
    assert all(payload["percent"] - before["percent"] >= runner.ACTIVITY_PERCENT_STEP
               for before, payload in zip(payloads, payloads[1:]) if before.get("code") == payload.get("code"))
    assert payloads[-1] == {"code": "encoding", "percent": 76, "done": 10, "total": 300}
    assert {"code": "rendering_frames", "percent": 75, "done": 300, "total": 300} in payloads


def test_a_watched_render_past_its_deadline_is_stopped(runner, monkeypatch, tmp_path):
    monkeypatch.setenv("MANOR_SANDBOX_EVENT_FILE", str(tmp_path / "events"))

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        runner._run_reporting_progress(
            [sys.executable, "-c", "print('starting', flush=True); import time; time.sleep(60)"],
            env=dict(os.environ), timeout=1,
        )

    assert time.monotonic() - started < 20
    assert "starting" in raised.value.stdout
