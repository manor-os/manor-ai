#!/usr/bin/env python3
"""Run fixed video quality and render operations inside a Manor sandbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote


# The project sits beside this script's Skill. A runner with per-Skill work
# areas serves /skill as /skills/<slot> by rewriting commands and uploaded files,
# but never the code of a Skill baked into its image, so a literal /skill here
# would name the image's own copy.
PROJECT = Path(__file__).resolve().parent.parent / "project"
REVIEW_DIR = Path("snapshots/manor-review")
SOURCE_EXCLUDED_DIRS = {
    ".git",
    ".hyperframes",
    ".hf-tmp",
    "node_modules",
    "renders",
    "snapshots",
}
SOURCE_EXCLUDED_FILES = {"hyperframes.json"}
CGROUP = Path("/sys/fs/cgroup")
# Chromium uses many threads per worker. Leave room for the runtime and cleanup.
WORKER_PIDS = 256
RESERVED_PIDS = 64
WORKER_MEMORY = 1024**3
RESERVED_MEMORY = 256 * 1024**2
# One worker is the floor, not a parallel slot: the strict check before render
# runs the same single Chromium inside this budget. Smallest runners (2 GB)
# give the sandbox 1.5 GB, a third of which the project's tmpfs copy holds.
FIRST_WORKER_MEMORY = 512 * 1024**2
TERMINATE_GRACE_SECONDS = 5
# Capture-then-encode writes every frame to the temp dir first: a two-minute
# 1080x1920 episode is ~3,600 frames, several GB. Sandbox /tmp is a tmpfs
# sized from host RAM (~1 GB on a 2 GB runner), so below this headroom the
# frames stream straight into the encoder instead.
CAPTURE_FIRST_MIN_FREE_BYTES = 12 * 1024**3
# HyperFrames extracts every video layer at once, one FFmpeg each, and FFmpeg
# sizes its decoder/filter/encoder thread pools from the CPUs it may run on,
# not the sandbox's CPU quota. Measured in the sandbox image (FFmpeg 5.1, the
# extraction filter chain): 86 threads on 14 CPUs, 14 on 2, 1 on 1. Fourteen
# layers on a 14-CPU runner asked for ~1,200 tasks inside a 512-PID sandbox.
FFMPEG_TASKS_PER_CPU = 6
FFMPEG_BASE_TASKS = 4
MAX_MEDIA_SLOTS = 4
MEDIA_GOVERNOR = Path(__file__).with_name("media_governor.py")
MEDIA_ELEMENT = re.compile(r"<(?:video|audio|img)\b", re.IGNORECASE)
# HyperFrames times each FFmpeg from spawn, so time spent queued for a slot
# counts; stretch its per-process limit (default 300 s) by the queue depth.
FFMPEG_PROCESS_TIMEOUT_SECONDS = 300
FFMPEG_QUEUE_SECONDS_PER_ROUND = 30
# A render that failed on a sandbox limit is retried once at minimum
# concurrency, if this much of the exec budget is left.
RESOURCE_RETRY_MIN_SECONDS = 300
# HyperFrames' diagnostic browser passes (`snapshot`, the layout audit inside
# `check`) open the composition with a fixed 10 s page.goto budget unless
# PRODUCER_PAGE_NAVIGATION_TIMEOUT_MS is set; their `--timeout` flag only
# bounds the render-ready wait after navigation. Software Chromium on a busy
# runner misses 10 s on a healthy page. The same variable sets render's own
# navigation budget (default 60 s), so one plan covers every browser step.
NAVIGATION_TIMEOUT_ENV = "MANOR_VIDEO_NAVIGATION_TIMEOUT_MS"
NAVIGATION_BASE_MS = 60_000
NAVIGATION_MIN_MS = 10_000
# The page and the media proxy touch every media element and its bytes.
NAVIGATION_MS_PER_MEDIA_ELEMENT = 2_000
NAVIGATION_MS_PER_MEDIA_MB = 100
NAVIGATION_SCALED_MAX_MS = 240_000
# Render's window.__hf poll after navigation (HyperFrames default 45 s).
PAGE_READY_MIN_MS = 45_000
# Snapshot's render-ready wait; it returns as soon as the runtime is ready.
RENDER_READY_MIN_MS = 30_000
# A browser step that timed out is retried once with every timeout doubled.
BROWSER_RETRY_SCALE = 2
BROWSER_RETRY_MIN_SECONDS = 60
# Work after navigation: check's passes, snapshot seeks and screenshots.
CHECK_WORK_SECONDS = 420
SNAPSHOT_WORK_SECONDS = 240
# The host gives the review exec 900 s; keep a margin to report the result.
REVIEW_BUDGET_SECONDS = 840
MEDIA_SUFFIXES = {
    ".mp4", ".mov", ".webm", ".mkv", ".m4v", ".wav", ".mp3", ".m4a", ".aac", ".ogg",
    ".flac", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif",
}
NAVIGATION_TIMEOUT = re.compile(r"Navigation timeout|net::ERR_TIMED_OUT", re.IGNORECASE)
PAGE_READY_TIMEOUT = re.compile(r"window\.__hf not ready after", re.IGNORECASE)
BROWSER_TIMEOUT_LABELS = {
    "navigation_timeout": "the composition page did not finish loading",
    "page_ready_timeout": "the composition runtime did not become ready",
    "process_timeout": "the browser command did not finish",
}
BROWSER_STEP_LABELS = {
    "quality_check": "the strict quality check",
    "snapshot": "review frame capture",
    "render": "render frame capture",
}
# Snapshot copies each active <video>'s frame into the page with one FFmpeg per
# frame (30 s each) and injects it. A failed copy only prints one of these
# warnings and leaves the element black; the command still succeeds.
SNAPSHOT_FRAME_NOT_EXTRACTED = re.compile(
    r"(\d+)/(\d+) active <video> frame\(s\) could not be extracted at ([\d.]+)s"
)
SNAPSHOT_FRAME_NOT_INJECTED = re.compile(r"video frame injection failed at ([\d.]+)s")
SNAPSHOT_ENGINE_UNAVAILABLE = "@hyperframes/engine unavailable"
VIDEO_SOURCE = re.compile(r"<video\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
# A failed review extracts one frame of up to this many of the project's
# videos the way snapshot does, each within snapshot's own per-frame budget.
VIDEO_PROBE_SOURCES = 4
VIDEO_PROBE_TIMEOUT_SECONDS = 30
THREAD_SPAWN_FAILURE = re.compile(
    r"pthread_create|Resource temporarily unavailable|\bEAGAIN\b|fork: retry|Cannot allocate memory",
    re.IGNORECASE,
)
FAILED_STEPS = (
    ("video_frame_extraction", re.compile(
        r"Video (?:frame )?extraction failed|Video HDR probe failed|VIDEO_EXTRACTION_FAILED"
        r"|VIDEO_SOURCE_UNRENDERABLE|captured \d+ of expected \d+ frames",
        re.IGNORECASE,
    )),
    ("audio_mix", re.compile(
        r"Audio (?:processing|duration normalization|muxing) failed|audio (?:mix|mux|pad/trim) failed",
        re.IGNORECASE,
    )),
    ("encode", re.compile(
        r"encod(?:e|ing) failed|Segment concat failed|Faststart failed|ffmpegEncodeTimeout"
        r"|\[assemble\] ffmpeg",
        re.IGNORECASE,
    )),
    ("frame_capture", re.compile(
        r"Target closed|Protocol error|browser has disconnected|Browser closed|Navigation timeout",
        re.IGNORECASE,
    )),
)
LIMIT_LABELS = {
    "pid_limit": "process/thread (PID) limit",
    "oom": "memory limit (the kernel killed a process)",
    "thread_spawn_failed": "process/thread limit (a thread or process could not be created)",
}
STEP_LABELS = {
    "video_frame_extraction": "video frame extraction",
    "audio_mix": "audio mixing",
    "encode": "encoding",
    "frame_capture": "browser frame capture",
    "render": "the render",
}
# The FFmpeg admission plan the next HyperFrames command runs under.
_media_plan: dict | None = None
# The browser timeout plan the next HyperFrames command runs under.
_browser_plan: dict | None = None
# What the host shows while this runs: Sandbox execution events, kept well
# under the runner's per-execution cap so the last phase always gets through.
ACTIVITY_EVENT_LIMIT = 80
ACTIVITY_PERCENT_STEP = 10
ACTIVITY_HEARTBEAT_SECONDS = 20
# How often the render's output is read for progress; the latest update per read is reported.
PROGRESS_READ_SECONDS = 2.0
_activity_sent = 0
# The renderer's stage codes, as the phases every long-running tool shares.
RENDER_STAGE_CODES = {"capture": "rendering_frames", "encode": "encoding", "assemble": "encoding"}
HF_MACHINE_PROGRESS = "@hf-progress "
HF_PROGRESS_LINE = re.compile(r"^\s*[\u2588\u2591]+\s+(\d{1,3})%\s+(.*\S)\s*$")


def _activity(code: str | None, message: str = "", *, kind: str = "progress", **numbers: int | None) -> None:
    """Tell the host what this command is doing now; a no-op outside a watched execution."""

    global _activity_sent
    path = os.environ.get("MANOR_SANDBOX_EVENT_FILE")
    if not path or _activity_sent >= ACTIVITY_EVENT_LIMIT:
        return
    payload = {"code": code, **{key: value for key, value in numbers.items() if value is not None}}
    event = {
        "event_id": f"video-{os.getpid()}-{_activity_sent}",
        "type": kind,
        "message": message[:280],
        "payload": {key: value for key, value in payload.items() if value is not None},
    }
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        _activity_sent += 1
    except OSError:
        pass


class _RenderProgress:
    """Reads the renderer's output as it grows and reports its stage and frame count.

    The renderer prints a plain progress line per update (``bar  42%  stage``)
    and, when its stdout is not a terminal, a machine line
    (``@hf-progress {"code", "done", "total", "pct"}``) right after it.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._stage = ""
        # Once machine lines appear, plain lines only name the stage.
        self._machine = False
        self._last: tuple[str | None, int] = (None, -ACTIVITY_PERCENT_STEP)
        self._last_at = 0.0

    def feed(self, text: str) -> None:
        if not text:
            return
        lines = re.split(r"[\r\n]", self._pending + text)
        self._pending = lines.pop()
        latest = None
        for line in lines:
            if line.startswith(HF_MACHINE_PROGRESS):
                try:
                    stage = json.loads(line[len(HF_MACHINE_PROGRESS):])
                except ValueError:
                    continue
                if isinstance(stage, dict):
                    self._machine = True
                    # The plain line just before it names this update's stage.
                    latest = (
                        RENDER_STAGE_CODES.get(str(stage.get("code") or ""), "preparing"),
                        stage.get("pct"), stage.get("done"), stage.get("total"), self._stage,
                    )
                    self._stage = ""
            elif match := HF_PROGRESS_LINE.match(line):
                self._stage = match.group(2)
                if not self._machine:
                    latest = (None, int(match.group(1)), None, None, self._stage)
        if latest is not None:
            self._report(*latest)

    def _report(self, code: str | None, percent: object, done: object, total: object, stage: str) -> None:
        pct = percent if isinstance(percent, int) and not isinstance(percent, bool) else -1
        now = time.monotonic()
        last_code, last_pct = self._last
        if (
            code == last_code
            and pct - last_pct < ACTIVITY_PERCENT_STEP
            and now - self._last_at < ACTIVITY_HEARTBEAT_SECONDS
        ):
            return
        self._last = (code, pct)
        self._last_at = now
        numbers = {key: value for key, value in (("done", done), ("total", total)) if isinstance(value, int)}
        _activity(code, stage, percent=pct if pct >= 0 else None, **numbers)


_STARTED = time.monotonic()
# What a failure's diagnostics keep of the command output; the full tails stay
# in stdout/stderr, which a caller cuts first when it runs short of room.
DIAGNOSTIC_STDERR_CHARS = 2000
DIAGNOSTIC_ERROR_LINES = 12
DIAGNOSTIC_LINE_CHARS = 400
ERROR_LINE = re.compile(
    r"error|fail|fatal|kill|crash|timed? ?out|exception|blocked|not found|not registered"
    r"|ENOSPC|ENOMEM|EAGAIN|cannot|unable",
    re.IGNORECASE,
)
# Progress bars and trace checkpoints repeat thousands of times; never evidence.
NOISE_LINE = re.compile(
    r"^\s*[█░]|\[Render:trace\]|Streaming frame \d+/\d+\s*$|^\[INFO\](?! \[Render\] Failure summary)"
)
BROWSER_CRASH = re.compile(
    r"Target closed|Protocol error|browser has disconnected|Browser closed|Page crashed"
    r"|Session closed|Failed to launch the browser|SIGSEGV|SIGBUS|SIGTRAP|Aw, Snap",
    re.IGNORECASE,
)
MISSING_ASSET = re.compile(
    r"\[Browser:HTTP404\] GET \S+?://[^/\s]+/(\S+) resource=media"
    r"|(?:No such file or directory|ENOENT)[^\n]*?((?:assets|compositions)/[^\s'\":]+)",
)
INVALID_ASSET = re.compile(
    r"Invalid data found when processing input|moov atom not found|VIDEO_SOURCE_UNRENDERABLE"
    r"|does not contain any stream|Unsupported codec|Decoder \S+ not found",
    re.IGNORECASE,
)
CORRECTNESS_BLOCK = re.compile(r"Render blocked by \d+ correctness warning", re.IGNORECASE)
UNREGISTERED_TIMELINES = re.compile(r"Sub-composition timelines not registered after \d+ms: ([^\n.]+)")
# What the agent should do about each failure, by code and then by reason.
NEXT_STEPS = {
    ("render_resource_limit", None): (
        "Render with quality='standard' and render_workers=1, or reduce how many video layers "
        "play at the same time. If an attempt at minimum concurrency already failed (see "
        "automatic_retry), the sandbox needs more memory/PIDs (an operator setting), not a project edit."
    ),
    ("render_resources_exhausted", None): (
        "The sandbox had no room for one browser worker before the render started. Run the render "
        "again once other work in the sandbox has finished; this is not a project defect."
    ),
    ("render_timeout", None): "Run the same render again later without editing the project.",
    ("snapshot_timeout", None): "Run the same review again later without editing the project.",
    ("runtime_timeout", None): (
        "Run the operation again with render_workers=1; if it times out again, shorten the "
        "composition or render with quality='standard'."
    ),
    ("render_process_killed", None): (
        "Render again with render_workers=1. If it is killed again, render with quality='standard' "
        "and check diagnostics.memory against the sandbox limit."
    ),
    ("render_browser_crashed", None): (
        "Render again with render_workers=1. If the browser crashes again, render with "
        "quality='standard' or reduce simultaneous layers, and report diagnostics.error_lines."
    ),
    ("render_asset_invalid", None): (
        "Replace or re-stage the asset files named in missing_media / diagnostics.error_lines with "
        "valid local media, then run review and render again."
    ),
    ("render_composition_invalid", "sub_timeline_readiness_timeout"): (
        "Each sub-composition listed in unregistered_timelines must register its own paused GSAP "
        "timeline at window.__timelines['<its data-composition-id>'] (inside its <template>), or its "
        "host element must carry data-no-timeline when it is intentionally not GSAP-driven. Fix the "
        "composition, then run review and render again; re-rendering unchanged source fails the same way."
    ),
    ("render_composition_invalid", None): (
        "HyperFrames blocked the render on the correctness warnings in warning_codes. Fix the "
        "composition as diagnostics.error_lines describe, then run review and render again."
    ),
    ("render_failed", None): (
        "Read diagnostics.error_lines and diagnostics.stderr_tail for the renderer's own error. If it "
        "names a project file, fix it and review again; otherwise render once more with "
        "render_workers=1 and report the diagnostics if it fails the same way."
    ),
    ("snapshot_failed", None): (
        "Read diagnostics.error_lines for the browser's error. If it names a project file, fix it; "
        "otherwise run review again."
    ),
    ("runtime_failed", None): "Report diagnostics to an operator; the video runtime itself failed.",
}
def _next_step(code: str, reason: str | None) -> str | None:
    return NEXT_STEPS.get((code, reason)) or NEXT_STEPS.get((code, None))


def _emit(payload: dict, *, exit_code: int = 0, result=None, stage: str | None = None) -> None:
    """Print the operation's one JSON result and exit.

    Every error carries the same ``diagnostics`` block (command exit status,
    signal, duration, stage, error lines, memory/PID peaks) and, where the
    runtime knows one, a ``next_step``; a one-line summary goes to stderr,
    which the Sandbox Service logs for a failed command.
    """
    if payload.get("status") == "error":
        code = str(payload.get("code") or "runtime_failed")
        payload.setdefault("diagnostics", _diagnostics(result, stage=stage))
        next_step = _next_step(code, payload.get("reason"))
        if next_step and "next_step" not in payload:
            payload["next_step"] = next_step
        print(_failure_summary(payload), file=sys.stderr)
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(exit_code)


def _signal_of(returncode: int | None) -> int | None:
    """The signal that ended a command: negative from Popen, 128+n from a shell."""
    if not isinstance(returncode, int):
        return None
    if returncode < 0:
        return -returncode
    if 128 < returncode < 128 + 65:
        return returncode - 128
    return None


def _signal_name(number: int | None) -> str | None:
    if number is None:
        return None
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"SIG{number}"


def _cgroup_counters(name: str) -> dict[str, int]:
    try:
        return {
            key: int(value)
            for key, value in (line.split() for line in (CGROUP / name).read_text().splitlines())
        }
    except (OSError, ValueError):
        return {}


def _cgroup_number(name: str) -> int | None:
    try:
        return int((CGROUP / name).read_text().strip())
    except (OSError, ValueError):
        return None


def _error_lines(text: str) -> list[str]:
    lines = [
        line.strip()[:DIAGNOSTIC_LINE_CHARS]
        for line in text.splitlines()
        if line.strip() and ERROR_LINE.search(line) and not NOISE_LINE.search(line)
    ]
    unique = list(dict.fromkeys(lines))
    return unique[-DIAGNOSTIC_ERROR_LINES:]


def _diagnostics(result=None, *, stage: str | None = None) -> dict:
    """The same failure facts for every exec path, with or without a command result."""
    stdout = _text_tail(getattr(result, "stdout", None), 200_000)
    stderr = _text_tail(getattr(result, "stderr", None), 200_000)
    text = f"{stdout}\n{stderr}"
    args = list(getattr(result, "args", None) or [])
    returncode = getattr(result, "returncode", None)
    number = _signal_of(returncode)
    summary = _log_record(text, FAILURE_SUMMARY_LOG) or {}
    warnings = _log_record(text, AUDIO_WARNINGS_LOG) or {}
    memory_events = _cgroup_counters("memory.events")
    return {
        "command": (args[1] if args[:1] == ["hyperframes"] and len(args) > 1 else (args[:1] or [None])[0]),
        "exit_code": returncode,
        "signal": number,
        "signal_name": _signal_name(number),
        "duration_seconds": getattr(result, "duration_seconds", None),
        "elapsed_seconds": round(time.monotonic() - _STARTED, 1),
        "stage": stage,
        "failed_stage": summary.get("failedStage") or (_failed_step(text) if result is not None else None),
        "renderer_error": summary.get("error"),
        "warning_codes": [str(code) for code in warnings.get("warningCodes") or []],
        "error_lines": _error_lines(text),
        "stderr_tail": stderr[-DIAGNOSTIC_STDERR_CHARS:],
        "memory": {
            "limit": _cgroup_number("memory.max"),
            "peak": _cgroup_number("memory.peak"),
            "current": _cgroup_number("memory.current"),
            "oom": memory_events.get("oom"),
            "oom_kill": memory_events.get("oom_kill"),
            "max_events": memory_events.get("max"),
        },
        "pids": {
            "limit": _cgroup_number("pids.max"),
            "peak": _cgroup_number("pids.peak"),
            "denied": _cgroup_counters("pids.events").get("max"),
        },
    }


def _failure_summary(payload: dict) -> str:
    """One stderr line: what the Sandbox Service logs about a failed command."""
    diagnostics = payload.get("diagnostics") or {}
    memory = diagnostics.get("memory") or {}

    def mib(value) -> str:
        return f"{value // 1024**2}MiB" if isinstance(value, int) else "?"

    last = (diagnostics.get("error_lines") or [""])[-1]
    return (
        f"video_runtime failure code={payload.get('code')} reason={payload.get('reason')} "
        f"retryable={payload.get('retryable')} command={diagnostics.get('command')} "
        f"exit={diagnostics.get('exit_code')} signal={diagnostics.get('signal_name')} "
        f"stage={diagnostics.get('stage')}/{diagnostics.get('failed_stage')} "
        f"duration={diagnostics.get('duration_seconds') or '?'}s "
        f"memory_peak={mib(memory.get('peak'))}/{mib(memory.get('limit'))} "
        f"oom_kill={memory.get('oom_kill')} :: {last[:300]}"
    )


def _text_tail(value: str | bytes | None, limit: int) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value or "")[-limit:]


def _process_stat(pid: int) -> tuple[int, str] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
        return int(fields[1]), fields[19]  # Parent PID and birth time prevent PID reuse.
    except (OSError, ValueError, IndexError):
        return None


def _descendants(pid: int) -> dict[int, str]:
    processes = {
        int(path.name): stat
        for path in Path("/proc").glob("[0-9]*")
        if (stat := _process_stat(int(path.name))) is not None
    }
    children: dict[int, str] = {}
    parents = {pid}
    while parents:
        found = {child: stat[1] for child, stat in processes.items() if stat[0] in parents}
        children.update(found)
        parents = set(found)
    return children


def _stop_process(proc: subprocess.Popen[str]) -> tuple[str, str]:
    # Puppeteer can detach Chrome into its own session, outside the CLI's group.
    children = _descendants(proc.pid)

    def send(sig: int) -> None:
        for pid, birth in children.items():
            current = _process_stat(pid)
            if current is not None and current[1] == birth:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        return proc.communicate(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        send(signal.SIGKILL)
        try:
            return proc.communicate(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired as exc:
            # Never wait indefinitely on an inherited pipe after cancellation.
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            return _text_tail(exc.output, 80000), _text_tail(exc.stderr, 40000)
    finally:
        send(signal.SIGKILL)


def _free_bytes(path: Path | str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _render_temp_dir() -> Path:
    """The roomier of /tmp and the project workdir, for render temp files.

    HyperFrames refuses to render with under 1 GB free in its temp dir, and a
    2 GB runner's /tmp tmpfs is just below that while the 2 GB workdir tmpfs
    has room to spare.
    """
    candidates = [Path(tempfile.gettempdir()), PROJECT / ".hf-tmp"]
    try:
        candidates[1].mkdir(parents=True, exist_ok=True)
    except OSError:
        candidates.pop()
    return max(candidates, key=_free_bytes)


def _run(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "NO_COLOR": "1",
        "HYPERFRAMES_BROWSER_PATH": "/usr/bin/chromium",
    }
    if args[:1] == ["hyperframes"]:
        env.update(_media_env(_media_plan or _media_concurrency(
            _resource_snapshot(), browser_workers=1, media_elements=_media_element_count(),
        )))
        browser = _browser_plan or _browser_timeout_plan()
        env["PRODUCER_PAGE_NAVIGATION_TIMEOUT_MS"] = str(browser["navigation_ms"])
        env["PRODUCER_PLAYER_READY_TIMEOUT_MS"] = str(browser["page_ready_ms"])
        # Every HyperFrames command, not only render: Chromium's profile and
        # snapshot's per-frame PNGs (one FFmpeg write per <video> frame) live
        # in TMPDIR. On a runner whose /tmp tmpfs had 60 MB free, snapshot
        # could not write any extracted frame, so every <video> in the review
        # frames was black (snapshot_video_frames_missing).
        env["TMPDIR"] = str(_render_temp_dir())
    if args[:2] == ["hyperframes", "render"]:
        # Capture first, then encode: software Chromium and the high-quality
        # encoder otherwise compete inside the same bounded execution sandbox.
        # Only when the temp dir can hold every frame; otherwise the render
        # fails for lack of space, so stream instead.
        temp_dir = Path(env["TMPDIR"])
        capture_first = _free_bytes(temp_dir) >= CAPTURE_FIRST_MIN_FREE_BYTES
        env["PRODUCER_ENABLE_STREAMING_ENCODE"] = "false" if capture_first else "true"
        if os.environ.get("MANOR_SANDBOX_EVENT_FILE"):
            return _run_reporting_progress(args, env=env, timeout=timeout)
    proc = subprocess.Popen(
        args,
        cwd=PROJECT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    started = time.monotonic()
    previous = signal.getsignal(signal.SIGTERM)

    def cancelled(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, cancelled)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        stdout, stderr = _stop_process(proc)
        raise subprocess.TimeoutExpired(args, timeout, stdout, stderr) from exc
    except BaseException:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _stop_process(proc)
        raise
    finally:
        signal.signal(signal.SIGTERM, previous)
    if proc.returncode != 0 and args[:1] == ["hyperframes"]:
        # A failed CLI can leave a wedged FFmpeg in its group holding PIDs
        # that a retry in this sandbox needs back.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    completed = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    completed.duration_seconds = round(time.monotonic() - started, 1)
    return completed


def _media_element_count() -> int:
    """Media elements HyperFrames may probe or extract at the same moment."""
    count = 0
    for path in PROJECT.rglob("*.html"):
        if any(part in SOURCE_EXCLUDED_DIRS for part in path.relative_to(PROJECT).parts):
            continue
        try:
            count += len(MEDIA_ELEMENT.findall(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            pass
    return count


def _media_bytes() -> int:
    """Bytes of media files in the project the page and media proxy may read."""
    total = 0
    for path in PROJECT.rglob("*"):
        if path.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        if any(part in SOURCE_EXCLUDED_DIRS for part in path.relative_to(PROJECT).parts):
            continue
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            pass
    return total


def _configured_navigation_ms() -> int:
    try:
        configured = int(float(os.environ.get(NAVIGATION_TIMEOUT_ENV) or 0))
    except ValueError:
        configured = 0
    return max(NAVIGATION_MIN_MS, configured) if configured > 0 else NAVIGATION_BASE_MS


def _browser_timeout_plan(*, scale: int = 1) -> dict:
    """Browser timeouts for HyperFrames, sized for this sandbox and project.

    The base (MANOR_VIDEO_NAVIGATION_TIMEOUT_MS, default 60 s) grows with the
    media the page loads, up to a cap; ``scale`` multiplies everything for
    the one retry after a timeout.
    """
    base = _configured_navigation_ms()
    elements = _media_element_count()
    media_bytes = _media_bytes()
    grown = (
        base + NAVIGATION_MS_PER_MEDIA_ELEMENT * elements
        + NAVIGATION_MS_PER_MEDIA_MB * (media_bytes // 1024**2)
    )
    navigation = min(grown, max(base, NAVIGATION_SCALED_MAX_MS)) * scale
    return {
        "navigation_ms": navigation,
        "page_ready_ms": max(PAGE_READY_MIN_MS * scale, navigation),
        "render_ready_ms": max(RENDER_READY_MIN_MS * scale, navigation),
        "scale": scale,
        "media_elements": elements,
        "media_bytes": media_bytes,
    }


def _browser_timeout_reason(result: subprocess.CompletedProcess[str]) -> str | None:
    text = f"{result.stdout or ''}\n{result.stderr or ''}"
    if NAVIGATION_TIMEOUT.search(text):
        return "navigation_timeout"
    if PAGE_READY_TIMEOUT.search(text):
        return "page_ready_timeout"
    return None


def _host_load() -> dict[str, float | None]:
    """How busy the machine is: a timeout under load is contention, not a bad page."""
    try:
        load = round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        load = None
    pressure = None
    try:
        some = (CGROUP / "cpu.pressure").read_text().splitlines()[0].split()
        pressure = float(dict(field.split("=") for field in some[1:])["avg10"])
    except (OSError, ValueError, IndexError, KeyError):
        pass
    return {"loadavg_1m": load, "cpu_pressure_avg10": pressure}


def _browser_timeout_error(
    code: str, attempts: list[dict], *, retry: str,
    result: subprocess.CompletedProcess[str], plan: dict,
) -> dict:
    last = attempts[-1]
    where = f"{BROWSER_STEP_LABELS[last['step']]} ({BROWSER_TIMEOUT_LABELS[last['reason']]})"
    seconds = f"{last['navigation_timeout_ms'] / 1000:g}"
    if retry == "exhausted":
        detail = (
            f"The sandbox browser timed out during {where}, and again after an automatic "
            f"retry with doubled timeouts (page load budget {seconds}s)."
        )
    else:
        detail = (
            f"The sandbox browser timed out during {where} with a {seconds}s page load "
            f"budget; no automatic retry was possible ({retry})."
        )
    return {
        "status": "error",
        "code": code,
        "reason": last["reason"],
        "step": last["step"],
        # Usually a busy host starving software Chromium; the same request can
        # succeed once the host has capacity, with no change to the project.
        "retryable": True,
        "automatic_retry": retry,
        "error": detail + (
            " This is usually a busy sandbox host, not a project defect: run the same "
            "operation again later without editing the project. If it keeps timing out on "
            "an idle host, look for synchronous work in the composition's scripts that "
            "delays page load."
        ),
        "attempts": attempts,
        "browser_timeouts": plan,
        "host_load": _host_load(),
        "stdout": (result.stdout or "")[-80000:],
        "stderr": (result.stderr or "")[-40000:],
        "resources": _resource_snapshot(),
    }


def _browser_step(
    step: str, build_args, *, deadline: float, work_seconds: int, error_code: str,
) -> subprocess.CompletedProcess[str]:
    """Run one HyperFrames browser command; once more, slower, if its page timed out.

    Any outcome other than a browser timeout is returned as it is. A second
    timeout, or no time left for the retry, ends the operation with a
    structured ``error_code`` result.
    """
    global _browser_plan
    attempts: list[dict] = []
    scale = 1
    while True:
        plan = _browser_plan = _browser_timeout_plan(scale=scale)
        args = build_args(plan)
        timeout = max(1, int(min(
            plan["navigation_ms"] / 1000 + work_seconds * scale, deadline - time.monotonic(),
        )))
        try:
            result = _run(args, timeout=timeout)
            reason = _browser_timeout_reason(result) if result.returncode != 0 else None
        except subprocess.TimeoutExpired as exc:
            result = subprocess.CompletedProcess(
                args, 124, _text_tail(exc.stdout, 80000), _text_tail(exc.stderr, 40000),
            )
            result.duration_seconds = timeout
            reason = "process_timeout"
        if reason is None:
            return result
        attempts.append({
            "reason": reason, "step": step, "exit_code": result.returncode,
            "navigation_timeout_ms": plan["navigation_ms"], "process_timeout_seconds": timeout,
        })
        if len(attempts) > 1:
            retry = "exhausted"
        elif deadline - time.monotonic() < (
            BROWSER_RETRY_SCALE * plan["navigation_ms"] / 1000 + BROWSER_RETRY_MIN_SECONDS
        ):
            retry = "insufficient_time_left"
        else:
            scale = BROWSER_RETRY_SCALE
            continue
        _emit(
            _browser_timeout_error(error_code, attempts, retry=retry, result=result, plan=plan),
            exit_code=result.returncode or 1,
            result=result,
            stage=step,
        )


def _ffmpeg_tasks(cpus: int) -> int:
    return FFMPEG_BASE_TASKS + FFMPEG_TASKS_PER_CPU * cpus


def _media_concurrency(
    resources: dict, *, browser_workers: int, media_elements: int, reduced: bool = False,
) -> dict:
    """How many FFmpeg processes may run at once, on how many CPUs each.

    Derived from the PIDs left after the runtime reserve, the browser workers
    and one waiting launcher per media element. A slot may hold a two-process
    pipeline, so it is budgeted twice. ``reduced`` is the floor a retry after a
    limit failure uses: one FFmpeg at a time, on one CPU.
    """
    cpu = max(1, int(resources.get("cpu_limit") or 1))
    limit, current = resources.get("pids_limit"), resources.get("pids_current")
    budget = None
    if reduced:
        slots, cpus = 1, 1
    elif limit is None or current is None:
        # An unavailable/unlimited cgroup is not evidence of a safe parallel budget.
        slots, cpus = 1, cpu
    else:
        budget = limit - current - RESERVED_PIDS - browser_workers * WORKER_PIDS - media_elements
        cpus = cpu
        while cpus > 1 and 2 * _ffmpeg_tasks(cpus) > budget:
            cpus -= 1
        slots = max(1, min(cpu, MAX_MEDIA_SLOTS, budget // (2 * _ffmpeg_tasks(cpus))))
    return {
        "slots": slots,
        "cpus_per_process": cpus,
        "reduced": reduced,
        "media_elements": media_elements,
        "pid_budget": budget,
        "peak_ffmpeg_tasks": 2 * slots * _ffmpeg_tasks(cpus),
    }


def _media_env(plan: dict) -> dict[str, str]:
    """Route HyperFrames' FFmpeg through the media governor under ``plan``."""
    real = os.environ.get("HYPERFRAMES_FFMPEG_PATH") or shutil.which("ffmpeg")
    if not real or not MEDIA_GOVERNOR.is_file():
        return {}
    try:
        root = PROJECT / ".hf-tmp" / "media-governor"
        root.mkdir(parents=True, exist_ok=True)
        launcher = root / "ffmpeg"
        launcher.write_text(
            "#!/bin/sh\n"
            f'exec {shlex.quote(sys.executable)} -I -S {shlex.quote(str(MEDIA_GOVERNOR))} "$@"\n'
        )
        launcher.chmod(0o755)
        # Fresh slots per command: a wedged FFmpeg from a failed attempt must
        # never hold a slot the retry waits on.
        slot_dir = tempfile.mkdtemp(prefix="slots-", dir=root)
    except OSError:
        return {}
    rounds = math.ceil(max(1, int(plan.get("media_elements") or 0)) / max(1, int(plan["slots"])))
    return {
        "HYPERFRAMES_FFMPEG_PATH": str(launcher),
        "MANOR_MEDIA_FFMPEG": real,
        # Lets the governor hold a <video>'s last frame when a frame is
        # extracted past its end (snapshot), as playback and render do.
        "MANOR_MEDIA_FFPROBE": os.environ.get("HYPERFRAMES_FFPROBE_PATH") or shutil.which("ffprobe") or "",
        "MANOR_MEDIA_SLOTS": str(plan["slots"]),
        "MANOR_MEDIA_CPUS": str(plan["cpus_per_process"]),
        "MANOR_MEDIA_SLOT_DIR": slot_dir,
        # Rotate the CPU windows so sandboxes sharing a runner spread out.
        "MANOR_MEDIA_CPU_BASE": str(random.randrange(1024)),
        "FFMPEG_PROCESS_TIMEOUT_MS": os.environ.get("FFMPEG_PROCESS_TIMEOUT_MS") or str(
            1000 * (FFMPEG_PROCESS_TIMEOUT_SECONDS + FFMPEG_QUEUE_SECONDS_PER_ROUND * rounds)
        ),
    }


def _resource_snapshot() -> dict[str, int | None]:
    def number(name: str) -> int | None:
        try:
            return int((CGROUP / name).read_text().strip())
        except (OSError, ValueError):
            return None

    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    try:
        quota, period = (CGROUP / "cpu.max").read_text().split()
        if quota != "max":
            cpus = min(cpus, max(1, int(quota) // int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    try:
        pid_events = dict(line.split() for line in (CGROUP / "pids.events").read_text().splitlines())
        denied_pids = int(pid_events.get("max", "0"))
    except (OSError, ValueError):
        denied_pids = None
    try:
        memory_events = dict(line.split() for line in (CGROUP / "memory.events").read_text().splitlines())
        oom_kills = int(memory_events.get("oom_kill", "0"))
    except (OSError, ValueError):
        oom_kills = None
    return {
        "pids_limit": number("pids.max"),
        "pids_current": number("pids.current"),
        "pids_denied": denied_pids,
        "memory_limit": number("memory.max"),
        "memory_current": number("memory.current"),
        "memory_peak": number("memory.peak"),
        "memory_reclaimable": _reclaimable_page_cache(),
        "memory_oom_kills": oom_kills,
        "cpu_limit": cpus,
    }


@contextmanager
def _memory_peak_sampler(interval: float = 2.0):
    """Where the memory went at the render's highest point.

    ``memory.peak`` gives only the total; sizing a render machine needs the
    split: ``anon`` (browsers, encoder), ``shmem`` (the tmpfs work area:
    project, extracted and captured frames) and ``file`` (page cache).
    """

    peak: dict[str, int] = {}
    stop = threading.Event()

    def sample() -> None:
        while True:
            try:
                current = int((CGROUP / "memory.current").read_text().strip())
                stat = dict(line.split() for line in (CGROUP / "memory.stat").read_text().splitlines())
            except (OSError, ValueError):
                current = -1
            if current > peak.get("memory_bytes", -1):
                peak.update(
                    memory_bytes=current,
                    anon_bytes=int(stat.get("anon", 0)),
                    shmem_bytes=int(stat.get("shmem", 0)),
                    file_bytes=int(stat.get("file", 0)),
                )
            if stop.wait(interval):
                return

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        yield peak
    finally:
        stop.set()
        thread.join(timeout=interval + 1)


def _reclaimable_page_cache() -> int:
    """File cache the kernel drops under pressure; tmpfs (shmem) stays charged."""
    try:
        stat = dict(line.split() for line in (CGROUP / "memory.stat").read_text().splitlines())
        return max(0, int(stat.get("file", 0)) - int(stat.get("shmem", 0)))
    except (OSError, ValueError):
        return 0


def _render_resources(requested: int) -> dict[str, int | None]:
    resources = _fit_workers(requested)
    if resources["workers"] < 1:
        _emit({
            "status": "error", "code": "render_resources_exhausted", "reason": "no_headroom",
            "retryable": True,
            "error": "Insufficient sandbox PID or memory headroom for one render worker.",
            "resources": resources,
        }, exit_code=2)
    return resources


def _fit_workers(requested: int) -> dict[str, int | None]:
    resources = _resource_snapshot()
    limits = [max(1, min(requested, 4)), resources["cpu_limit"] or 1]
    for kind, reserve, per_worker in (
        ("pids", RESERVED_PIDS, WORKER_PIDS),
        ("memory", RESERVED_MEMORY, WORKER_MEMORY),
    ):
        maximum, current = resources[f"{kind}_limit"], resources[f"{kind}_current"]
        if maximum is not None and current is not None:
            if kind == "memory":
                # Page cache from reading the sources is not a claim on memory.
                current = max(0, current - (resources.get("memory_reclaimable") or 0))
            headroom = max(0, maximum - current - reserve)
            fit = headroom // per_worker
            if kind == "memory" and fit < 1 and headroom >= FIRST_WORKER_MEMORY:
                fit = 1
            limits.append(fit)
        else:
            # An unavailable/unlimited cgroup is not evidence of a safe parallel budget.
            limits.append(1)
    workers = min(limits)
    resources.update(workers_requested=requested, workers=workers)
    return resources


def _counter_delta(before: dict, after: dict, key: str) -> int | None:
    old, new = before.get(key), after.get(key)
    return new - old if isinstance(old, int) and isinstance(new, int) else None


def _failed_step(text: str) -> str:
    for step, pattern in FAILED_STEPS:
        if pattern.search(text):
            return step
    return "render"


def _resource_limit(before: dict, after: dict, rendered: subprocess.CompletedProcess[str]) -> dict | None:
    """Classify a failed render that ran into the sandbox's own limits."""
    text = f"{rendered.stdout or ''}\n{rendered.stderr or ''}"
    pids = _counter_delta(before, after, "pids_denied")
    ooms = _counter_delta(before, after, "memory_oom_kills")
    if pids:
        reason = "pid_limit"
    elif ooms:
        reason = "oom"
    elif THREAD_SPAWN_FAILURE.search(text):
        reason = "thread_spawn_failed"
    else:
        return None
    return {"reason": reason, "step": _failed_step(text), "pids_denied": pids, "oom_kills": ooms}


def _resource_limit_error(
    limit: dict, attempts: list[dict], *, retry: str,
    rendered: subprocess.CompletedProcess[str], resources: dict, after: dict,
) -> dict:
    where = f"{LIMIT_LABELS[limit['reason']]} during {STEP_LABELS[limit['step']]}"
    if retry == "exhausted":
        detail = (
            f"The render sandbox hit its {where}, and again after an automatic retry at "
            "minimum concurrency (one FFmpeg at a time on one CPU, one browser worker)."
        )
    else:
        detail = f"The render sandbox hit its {where}; no automatic retry was possible ({retry})."
    return {
        "status": "error",
        "code": "render_resource_limit",
        "reason": limit["reason"],
        "step": limit["step"],
        # The runtime already stepped down to its floor, so repeating the same
        # export reproduces this. It needs more sandbox capacity or a project
        # with fewer simultaneous video layers.
        "retryable": False,
        "automatic_retry": retry,
        "error": detail + " Repeating the same export will fail the same way.",
        "attempts": attempts,
        "stdout": rendered.stdout[-80000:],
        "stderr": rendered.stderr[-40000:],
        "resources": resources,
        "resources_after": after,
    }


AUDIO_WARNINGS_LOG = "Render completed capture with correctness warnings "
FAILURE_SUMMARY_LOG = "[Render] Failure summary "
MISSING_MEDIA_LOG = re.compile(r"\[Browser:HTTP404\] GET \S+?://[^/\s]+/(\S+) resource=media")
AUDIO_FAILURE_HINTS = {
    "source_not_found": (
        "An audio element the page plays has no source file in the render project. Stage the "
        "file and point the element's src at the project_path push_assets returned."
    ),
    "invalid_media": "An audio element's source is a text document, not audio. Replace that file.",
}


def _log_record(text: str, marker: str) -> dict | None:
    """The JSON object HyperFrames logged after ``marker``, last occurrence first."""
    for line in reversed(text.splitlines()):
        _, found, rest = line.partition(marker)
        if not found:
            continue
        try:
            record = json.loads(rest)
        except ValueError:
            continue
        if isinstance(record, dict):
            return record
    return None


def _audio_failure(rendered: subprocess.CompletedProcess[str]) -> dict | None:
    """Classify HyperFrames' fatal ``audio_processing_failed`` warning.

    HyperFrames refuses to ship a silent video: a failed audio mix stops the
    render at "Processing audio tracks" with only a warning code. Its log says
    why (reason/stage/owner per failed track) and which media 404ed in the page.
    """
    text = f"{rendered.stdout or ''}\n{rendered.stderr or ''}"
    warnings = _log_record(text, AUDIO_WARNINGS_LOG)
    if warnings is None or "audio_processing_failed" not in (warnings.get("warningCodes") or []):
        return None
    summary = _log_record(text, FAILURE_SUMMARY_LOG) or {}
    missing = sorted({
        match.group(1)
        for line in summary.get("browserConsoleErrors") or []
        if (match := MISSING_MEDIA_LOG.search(str(line)))
    })
    owners = [str(owner) for owner in warnings.get("warningOwners") or []]
    return {
        "reasons": [str(reason) for reason in warnings.get("warningReasons") or []] or ["unknown"],
        "stages": [str(stage) for stage in warnings.get("warningStages") or []],
        "owner": "system" if "system" in owners else ("user" if owners else None),
        "retryable": warnings.get("warningRetryable") is True,
        "missing_media": missing,
    }


def _audio_failure_error(
    failure: dict, *, rendered: subprocess.CompletedProcess[str], resources: dict, after: dict,
) -> dict:
    reason = failure["reasons"][0]
    detail = (
        f"The render's audio mix failed ({', '.join(failure['reasons'])}) before frame capture, "
        "so the renderer stopped instead of exporting a silent video."
    )
    if failure["missing_media"]:
        detail += f" Media the page requested but the render project lacks: {', '.join(failure['missing_media'])}."
    hint = AUDIO_FAILURE_HINTS.get(reason)
    if hint:
        detail += " " + hint
    elif failure["retryable"]:
        detail += " Run the same export again; the project does not need to change."
    return {
        "status": "error",
        "code": "render_audio_failed",
        "reason": reason,
        "step": "audio_process",
        "retryable": failure["retryable"],
        "automatic_retry": "none",
        "error": detail,
        "next_step": hint or (
            "Run the same render again; the project does not need to change."
            if failure["retryable"]
            else "Fix the audio sources named in missing_media / diagnostics.error_lines, then review and render again."
        ),
        **failure,
        "stdout": (rendered.stdout or "")[-80000:],
        "stderr": (rendered.stderr or "")[-40000:],
        "resources": resources,
        "resources_after": after,
    }


# A failure cause's kind -> the render code the agent sees.
RENDER_CODES = {
    "composition": "render_composition_invalid",
    "asset": "render_asset_invalid",
    "browser": "render_browser_crashed",
    "killed": "render_process_killed",
    None: "render_failed",
}


def _unregistered_timelines(text: str) -> list[str]:
    found = UNREGISTERED_TIMELINES.findall(text)
    return [part.strip() for part in found[-1].split(",") if part.strip()] if found else []


def _composition_sources_register_timelines(composition_ids: list[str]) -> bool:
    """Whether any listed sub-composition's own source registers a timeline at all.

    One that never does fails every render the same way; one that does but too
    late (asynchronous setup on a busy host) can pass on a later attempt.
    """
    wanted = set(composition_ids)
    for page in PROJECT.rglob("*.html"):
        if any(part in SOURCE_EXCLUDED_DIRS for part in page.relative_to(PROJECT).parts):
            continue
        try:
            html = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for tag in re.findall(r"<[^>]+\bdata-composition-src\s*=[^>]*>", html):
            ident = re.search(r"\bdata-composition-id\s*=\s*[\"']([^\"']+)[\"']", tag)
            source = re.search(r"\bdata-composition-src\s*=\s*[\"']([^\"']+)[\"']", tag)
            if not ident or not source or ident.group(1) not in wanted:
                continue
            try:
                text = (page.parent / source.group(1)).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "__timelines" in text:
                return True
    return False


def _failure_cause(result: subprocess.CompletedProcess[str]) -> dict:
    """Why a HyperFrames command failed, beyond sandbox limits, timeouts and audio.

    ``kind`` selects the operation's code; ``reason``/``retryable`` and the
    evidence fields are reported as they are on every exec path.
    """
    text = f"{result.stdout or ''}\n{result.stderr or ''}"
    warnings = _log_record(text, AUDIO_WARNINGS_LOG) or {}
    codes = [str(code) for code in warnings.get("warningCodes") or []]
    if CORRECTNESS_BLOCK.search(text) and codes:
        cause = {"kind": "composition", "reason": codes[0], "retryable": False, "warning_codes": codes}
        if codes[0] == "sub_timeline_readiness_timeout":
            unregistered = _unregistered_timelines(text)
            cause["unregistered_timelines"] = unregistered
            if unregistered and _composition_sources_register_timelines(unregistered):
                cause.update(reason="sub_timeline_slow", retryable=True)
        return cause
    missing = sorted({
        name for match in MISSING_ASSET.finditer(text) if (name := match.group(1) or match.group(2))
    })
    if missing or INVALID_ASSET.search(text):
        return {
            "kind": "asset", "reason": "asset_missing" if missing else "asset_invalid",
            "retryable": False, "missing_media": missing,
        }
    if BROWSER_CRASH.search(text):
        return {"kind": "browser", "reason": "browser_crashed", "retryable": True}
    if _signal_of(result.returncode) is not None:
        return {"kind": "killed", "reason": "killed", "retryable": True}
    if result.returncode == 0:
        return {"kind": None, "reason": "no_output", "retryable": False}
    return {"kind": None, "reason": "unknown", "retryable": False}


def _failure_detail(cause: dict, result: subprocess.CompletedProcess[str], *, step: str) -> str:
    text = f"{result.stdout or ''}\n{result.stderr or ''}"
    summary = _log_record(text, FAILURE_SUMMARY_LOG) or {}
    where = BROWSER_STEP_LABELS.get(step, step)
    stage = summary.get("failedStage") or STEP_LABELS.get(_failed_step(text), "the command")
    kind, reason = cause["kind"], cause["reason"]
    if kind == "composition":
        if reason in {"sub_timeline_readiness_timeout", "sub_timeline_slow"}:
            names = ", ".join(cause.get("unregistered_timelines") or []) or "some sub-compositions"
            detail = (
                f"HyperFrames blocked {where} on correctness warning sub_timeline_readiness_timeout: "
                f"sub-compositions {names} never registered a timeline at window.__timelines. "
            )
            if reason == "sub_timeline_slow":
                return detail + "Their sources do register one, so setup was too slow on this host."
            return detail + (
                "Review does not wait for sub-composition timelines, so it passed; every render of "
                "this source fails the same way until the composition changes."
            )
        return (
            f"HyperFrames blocked {where} on correctness warnings {', '.join(cause['warning_codes'])}; "
            "the composition must change before it can render."
        )
    if kind == "asset":
        names = ", ".join(cause.get("missing_media") or []) or "a media file"
        return f"{where[0].upper()}{where[1:]} could not use project media ({names}) during {stage}."
    if kind == "browser":
        return f"The sandbox browser crashed during {where} ({stage})."
    if kind == "killed":
        name = _signal_name(_signal_of(result.returncode))
        return f"The renderer was killed by {name} during {where} without a recorded out-of-memory kill."
    if reason == "no_output":
        return f"{where[0].upper()}{where[1:]} exited successfully but wrote no output file."
    last = summary.get("error") or (_error_lines(text) or ["no error output"])[-1]
    return f"The renderer exited with code {result.returncode} during {where} ({stage}): {last}"


def _command_failure(
    code: str, result: subprocess.CompletedProcess[str], *, step: str, cause: dict | None = None,
) -> dict:
    """A classified failure payload for one HyperFrames command."""
    cause = dict(cause or _failure_cause(result))
    kind = cause.pop("kind")
    return {
        "status": "error",
        "code": code,
        **cause,
        "step": step,
        "error": _failure_detail({"kind": kind, **cause}, result, step=step),
        "stdout": (result.stdout or "")[-80000:],
        "stderr": (result.stderr or "")[-40000:],
    }


def _require_runtime() -> dict[str, str]:
    cli_path = Path(shutil.which("hyperframes") or "/usr/local/bin/hyperframes")
    browser_path = Path(
        os.environ.get("HYPERFRAMES_BROWSER_PATH") or "/usr/bin/chromium"
    )
    gsap_path = Path(
        os.environ.get("HYPERFRAMES_GSAP_PATH")
        or "/usr/local/lib/node_modules/gsap/dist/gsap.min.js"
    )
    missing = []
    if not cli_path.is_file() or not os.access(cli_path, os.X_OK):
        missing.append(str(cli_path))
    if not browser_path.is_file() or not os.access(browser_path, os.X_OK):
        missing.append(str(browser_path))
    if not gsap_path.is_file():
        missing.append(str(gsap_path))
    if missing:
        _emit(
            {
                "status": "error",
                "code": "runtime_unavailable",
                "error": "Pinned video runtime is missing inside the sandbox image.",
                "missing": missing,
            },
            exit_code=2,
        )
    return {
        "renderer": str(cli_path),
        "browser": str(browser_path),
        "animation": str(gsap_path),
    }


def _probe_runtime() -> None:
    _emit(
        {
            "status": "ok",
            "operation": "probe",
            "runtime": _require_runtime(),
        }
    )


def _stage_runtime() -> None:
    runtime = _require_runtime()

    video_config = PROJECT / "video.json"
    engine_config = PROJECT / "hyperframes.json"
    if not video_config.is_file():
        _emit(
            {
                "status": "error",
                "code": "project_contract_incomplete",
                "error": "video.json is missing from the editable project.",
            },
            exit_code=2,
        )
    shutil.copy2(video_config, engine_config)

    gsap_source = Path(runtime["animation"])
    destination = PROJECT / "assets/vendor/gsap.min.js"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(gsap_source, destination)


def _safe_project_path(value: str) -> Path:
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        _emit(
            {
                "status": "error",
                "code": "invalid_output_path",
                "error": f"Unsafe project-relative output path: {value}",
            },
            exit_code=2,
        )
    target = (PROJECT / raw).resolve()
    if PROJECT.resolve() not in target.parents:
        _emit(
            {
                "status": "error",
                "code": "invalid_output_path",
                "error": f"Output escapes the project: {value}",
            },
            exit_code=2,
        )
    return target


def _check(*, deadline: float, error_code: str) -> subprocess.CompletedProcess[str]:
    # `--timeout` stays fixed: check also sleeps for it after page load.
    return _browser_step(
        "quality_check",
        lambda _plan: ["hyperframes", "check", ".", "--strict", "--no-browser-gpu", "--json", "--timeout=30000"],
        deadline=deadline,
        work_seconds=CHECK_WORK_SECONDS,
        error_code=error_code,
    )


def _manifest() -> None:
    files = []
    total_bytes = 0
    for path in sorted(PROJECT.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(PROJECT)
        if rel.as_posix() in SOURCE_EXCLUDED_FILES:
            continue
        if any(part in SOURCE_EXCLUDED_DIRS for part in rel.parts):
            continue
        if rel.parts[:2] == ("assets", "vendor"):
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        size = path.stat().st_size
        total_bytes += size
        files.append(
            {
                "path": rel.as_posix(),
                "size_bytes": size,
                "sha256": digest.hexdigest(),
            }
        )
    _emit(
        {
            "status": "ok",
            "operation": "manifest",
            "files": files,
            "source_files": len(files),
            "source_bytes": total_bytes,
        }
    )


def _project_relative_source(finding: dict) -> None:
    """Lint names the file it read by absolute path; the agent edits project paths."""
    source = finding.get("sourceFile")
    if not isinstance(source, str) or not os.path.isabs(source):
        return
    for root in (PROJECT, PROJECT.resolve()):
        try:
            finding["sourceFile"] = Path(source).relative_to(root).as_posix()
            return
        except ValueError:
            continue


def _blank_video_frames(result: subprocess.CompletedProcess[str]) -> list[dict]:
    """Review frames in which snapshot left a <video> black, from its warnings."""
    text = f"{result.stdout or ''}\n{result.stderr or ''}"
    blank = [
        {"time": float(at), "reason": "frame_not_extracted", "videos": int(missed), "active_videos": int(active)}
        for missed, active, at in SNAPSHOT_FRAME_NOT_EXTRACTED.findall(text)
    ]
    blank += [{"time": float(at), "reason": "frame_not_injected"} for at in SNAPSHOT_FRAME_NOT_INJECTED.findall(text)]
    if SNAPSHOT_ENGINE_UNAVAILABLE in text:
        blank.append({"reason": "engine_unavailable"})
    return blank


def _project_videos() -> list[str]:
    """Project-relative sources of the compositions' <video> elements, first use first."""
    videos: list[str] = []
    for page in sorted(PROJECT.rglob("*.html")):
        if any(part in SOURCE_EXCLUDED_DIRS for part in page.relative_to(PROJECT).parts):
            continue
        try:
            text = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for src in VIDEO_SOURCE.findall(text):
            rel = unquote(re.split(r"[?#]", src, maxsplit=1)[0]).removeprefix("./")
            if "://" in rel or rel.startswith(("data:", "/")) or rel in videos:
                continue
            videos.append(rel)
    return videos


def _video_extraction_probe(deadline: float) -> dict:
    """Extract one frame of the project's videos as snapshot does, to show why it could not.

    The same FFmpeg launcher, temp dir and per-frame budget as snapshot: a
    failure names the source and FFmpeg's error, a success points at the host.
    """
    env = {**os.environ, **_media_env(_media_plan or _media_concurrency(
        _resource_snapshot(), browser_workers=1, media_elements=_media_element_count(),
    ))}
    ffmpeg = env.get("HYPERFRAMES_FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"
    videos: list[dict] = []
    try:
        # The temp dir snapshot used: a full one is a cause the probe must see.
        scratch = Path(tempfile.mkdtemp(prefix="hf-review-probe-", dir=_render_temp_dir()))
    except OSError as exc:
        return {"error": f"temp dir unavailable: {exc}", "videos": videos}
    try:
        for index, rel in enumerate(_project_videos()[:VIDEO_PROBE_SOURCES]):
            budget = min(VIDEO_PROBE_TIMEOUT_SECONDS, int(deadline - time.monotonic()) - 5)
            if budget < 1:
                break
            source, frame = PROJECT / rel, scratch / f"{index}.png"
            started = time.monotonic()
            outcome: dict = {"src": rel}
            if not source.is_file():
                videos.append({**outcome, "error": "file_not_found"})
                continue
            try:
                done = subprocess.run(
                    [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "0", "-i", str(source),
                     "-frames:v", "1", "-q:v", "2", "-y", str(frame)],
                    cwd=PROJECT, env=env, capture_output=True, text=True, timeout=budget,
                )
                outcome.update(exit_code=done.returncode, stderr=done.stderr[-2000:])
            except subprocess.TimeoutExpired as exc:
                outcome.update(exit_code=None, timed_out=True, stderr=_text_tail(exc.stderr, 2000))
            except OSError as exc:
                outcome.update(exit_code=None, stderr=str(exc))
            outcome.update(
                seconds=round(time.monotonic() - started, 2),
                extracted=frame.is_file() and frame.stat().st_size > 0,
            )
            videos.append(outcome)
        return {"temp_dir": str(scratch.parent), "temp_free_bytes": _free_bytes(scratch), "videos": videos}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


VIDEO_TAG = re.compile(r"<video\b[^>]*>", re.IGNORECASE)
# A frame this close to the end of the footage counts as past it: FFmpeg has
# no frame to return at or after the last timestamp.
PAST_END_TOLERANCE_SECONDS = 0.04


def _attr(tag: str, name: str) -> str | None:
    match = re.search(rf"\b{name}\s*=\s*[\"']([^\"']*)[\"']", tag, re.IGNORECASE)
    return match.group(1) if match else None


def _videos_past_source_end(times: list[float]) -> list[dict]:
    """Root-timeline <video> clips that are on screen at ``times`` past their footage.

    The browser and the render hold such a clip's last frame, but snapshot asks
    FFmpeg for a frame after the end of the file, gets none and leaves the clip
    black: a composition defect (slot longer than footage), not a host fault.
    """
    page = PROJECT / "index.html"
    ffprobe = os.environ.get("HYPERFRAMES_FFPROBE_PATH") or shutil.which("ffprobe")
    if not times or not ffprobe or not page.is_file():
        return []
    try:
        text = page.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    durations: dict[str, float | None] = {}
    found: list[dict] = []
    for tag in VIDEO_TAG.findall(text):
        src = unquote(re.split(r"[?#]", _attr(tag, "src") or "", maxsplit=1)[0]).removeprefix("./")
        if not src or "://" in src or src.startswith(("data:", "/")) or re.search(r"\bloop\b", tag):
            continue
        try:
            start = float(_attr(tag, "data-start") or "nan")
            slot = float(_attr(tag, "data-duration") or "nan")
            rate = float(_attr(tag, "data-playback-rate") or 1) or 1.0
            media_start = float(_attr(tag, "data-media-start") or _attr(tag, "data-playback-start") or 0)
        except ValueError:
            continue
        active = [t for t in times if start <= t < start + slot]
        if not active:
            continue
        if src not in durations:
            try:
                probed = subprocess.run(
                    [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(PROJECT / src)],
                    capture_output=True, text=True, timeout=VIDEO_PROBE_TIMEOUT_SECONDS,
                )
                durations[src] = float(probed.stdout.strip())
            except (OSError, ValueError, subprocess.TimeoutExpired):
                durations[src] = None
        footage = durations[src]
        if footage is None:
            continue
        for t in active:
            needed = (t - start) * rate + media_start
            if needed >= footage - PAST_END_TOLERANCE_SECONDS:
                found.append({
                    "src": src, "time": t, "clip_start": start, "clip_duration": slot,
                    "playback_rate": rate, "needed_seconds": round(needed, 3),
                    "footage_seconds": round(footage, 3),
                })
    return found


def _blank_video_frames_error(
    attempts: list[dict], *, retry: str, result: subprocess.CompletedProcess[str], deadline: float,
) -> dict:
    blank = attempts[-1]["blank_video_frames"]
    times = sorted({entry["time"] for entry in blank if "time" in entry})
    where = f" at {', '.join(f'{t:g}s' for t in times)}" if times else ""
    tried = (
        "again after an automatic retry" if retry == "exhausted"
        else f"with no automatic retry possible ({retry})"
    )
    past_end = _videos_past_source_end(times)
    past_end_times = {entry["time"] for entry in past_end}
    probe = _video_extraction_probe(deadline)
    unreadable = [video for video in probe["videos"] if not video.get("extracted")]
    if past_end and times and set(times) <= past_end_times:
        # Every black frame is a clip shown past its footage: fix the timing.
        names = ", ".join(dict.fromkeys(entry["src"] for entry in past_end))
        retryable = False
        cause = (
            f" At those times {names} is on screen past the end of its footage (see "
            "videos_past_source_end: the slot is longer than the file), so there is no frame to copy"
        )
        next_step = (
            f"Fit {names} to its footage: shorten its data-duration, slow it with "
            "data-playback-rate, loop it, or use a longer clip; then review again."
        )
    elif "error" in probe or (len(probe["videos"]) > 1 and len(unreadable) == len(probe["videos"])):
        # Not one source works: the Sandbox's FFmpeg or temp dir is broken.
        retryable = False
        cause = (
            " FFmpeg in this sandbox could not extract a frame from any project video either (see "
            "video_extraction), so the sandbox is at fault, not the assets"
        )
        next_step = "Do not edit or replace the assets; report the blocker with video_extraction."
    elif unreadable:
        # FFmpeg cannot take a frame from these files here, so neither can
        # snapshot: the asset needs replacing (or re-encoding), not a re-run.
        names = ", ".join(video["src"] for video in unreadable)
        retryable = False
        cause = (
            f" FFmpeg in this sandbox could not extract a frame from {names} either (see "
            "video_extraction for its error)"
        )
        next_step = f"Replace or re-encode {names}, then review again."
    else:
        retryable = True
        cause = (
            " FFmpeg extracted a frame from each project video when tried directly, so this is the "
            "sandbox host, not a project defect"
        )
        next_step = "Run review again without editing the project."
    return {
        "status": "error",
        "code": "snapshot_video_frames_missing",
        "reason": blank[0]["reason"],
        "step": "snapshot",
        "retryable": retryable,
        "automatic_retry": retry,
        "error": (
            f"Review frame capture could not copy <video> frames into its frames{where}, {tried}; "
            "those videos would have shown as black, so no review frames were kept."
            f"{cause}. {next_step}"
        ),
        "next_step": next_step,
        "attempts": attempts,
        "video_extraction": probe,
        **({"videos_past_source_end": past_end} if past_end else {}),
        "stdout": (result.stdout or "")[-80000:],
        "stderr": (result.stderr or "")[-40000:],
        "resources": _resource_snapshot(),
    }


def _review(snapshot_times: str) -> None:
    deadline = time.monotonic() + REVIEW_BUDGET_SECONDS
    _activity("checking")
    checked = _check(deadline=deadline, error_code="snapshot_timeout")
    if checked.returncode != 0:
        try:
            report = json.loads(checked.stdout)
        except (TypeError, ValueError):
            report = None
        sections = ("lint", "runtime", "layout", "motion", "contrast")
        if (
            checked.returncode == 1
            and isinstance(report, dict)
            and report.get("ok") is False
            and not report.get("error")
            and all(
                isinstance(report.get(section), dict)
                and isinstance(report[section].get("findings"), list)
                for section in sections
            )
        ):
            # The check ran to completion and rejected the project. Staging
            # and check caches may have changed, so this is a completed review
            # observation, not a no-effect receipt or a successful render.
            for section in sections:
                for finding in report[section]["findings"]:
                    if isinstance(finding, dict):
                        _project_relative_source(finding)
            _emit({
                "status": "ok",
                "operation": "review",
                "review_passed": False,
                "check_report": report,
            })
        _emit(
            _command_failure("quality_gate_failed", checked, step="quality_check"),
            exit_code=checked.returncode or 1,
            result=checked,
            stage="quality_check",
        )

    review_dir = PROJECT / REVIEW_DIR
    blank_attempts: list[dict] = []
    while True:
        if review_dir.exists():
            shutil.rmtree(review_dir)
        reviewed = _browser_step(
            "snapshot",
            lambda plan: [
                "hyperframes",
                "snapshot",
                ".",
                "--at",
                snapshot_times,
                "--no-end",
                f"--timeout={plan['render_ready_ms']}",
                "--output",
                REVIEW_DIR.as_posix(),
            ],
            deadline=deadline,
            work_seconds=SNAPSHOT_WORK_SECONDS,
            error_code="snapshot_timeout",
        )
        if reviewed.returncode != 0:
            _emit(
                _command_failure("snapshot_failed", reviewed, step="snapshot"),
                exit_code=reviewed.returncode or 1,
                result=reviewed,
                stage="snapshot",
            )
        blank = _blank_video_frames(reviewed)
        if not blank:
            break
        # Frames with a black <video> are not a review of the project.
        blank_attempts.append({"blank_video_frames": blank, "host_load": _host_load()})
        if len(blank_attempts) > 1:
            retry = "exhausted"
        elif deadline - time.monotonic() < BROWSER_RETRY_MIN_SECONDS + SNAPSHOT_WORK_SECONDS:
            retry = "insufficient_time_left"
        else:
            continue
        _emit(
            _blank_video_frames_error(blank_attempts, retry=retry, result=reviewed, deadline=deadline),
            exit_code=1,
            result=reviewed,
            stage="snapshot",
        )

    files = sorted(
        path.relative_to(PROJECT).as_posix()
        for path in review_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    )
    _emit(
        {
            "status": "ok",
            "operation": "review",
            "review_passed": True,
            "files": files[:12],
            "check_stdout": checked.stdout[-80000:],
        }
    )


def _run_reporting_progress(
    args: list[str], *, env: dict[str, str], timeout: int,
) -> subprocess.CompletedProcess[str]:
    """``_run`` for the render: its output goes to files read as they grow, so progress reaches the host."""

    with tempfile.TemporaryDirectory(prefix="manor-render-output-") as output_dir:
        out_path = Path(output_dir) / "stdout"
        err_path = Path(output_dir) / "stderr"
        with out_path.open("w") as out, err_path.open("w") as err:
            proc = subprocess.Popen(
                args, cwd=PROJECT, env=env, text=True, stdout=out, stderr=err, start_new_session=True,
            )
        previous = signal.getsignal(signal.SIGTERM)

        def cancelled(signum: int, _frame: object) -> None:
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, cancelled)
        progress = _RenderProgress()
        deadline = time.monotonic() + timeout
        try:
            with out_path.open("r", errors="replace") as reader:
                while True:
                    try:
                        proc.wait(timeout=max(0.01, min(PROGRESS_READ_SECONDS, deadline - time.monotonic())))
                        progress.feed(reader.read())
                        break
                    except subprocess.TimeoutExpired:
                        progress.feed(reader.read())
                        if time.monotonic() < deadline:
                            continue
                        signal.signal(signal.SIGTERM, signal.SIG_IGN)
                        _stop_process(proc)
                        raise subprocess.TimeoutExpired(
                            args, timeout,
                            _text_tail(out_path.read_text(errors="replace"), 80000),
                            _text_tail(err_path.read_text(errors="replace"), 40000),
                        ) from None
        except subprocess.TimeoutExpired:
            raise
        except BaseException:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            _stop_process(proc)
            raise
        finally:
            signal.signal(signal.SIGTERM, previous)
        stdout = out_path.read_text(errors="replace")
        stderr = err_path.read_text(errors="replace")
    if proc.returncode != 0:
        # As in ``_run``: free the PIDs a wedged FFmpeg in the group still holds.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def _render(output: str, quality: str, workers: int, reviewed: bool = False) -> None:
    # The outer Sandbox exec allows 1800s, including preflight and output probing.
    deadline = time.monotonic() + 1740
    # The host passes --reviewed only for a source whose hash passed review's
    # identical strict check; `render --strict-all --no-best-effort` still
    # fails on lint findings and missing media.
    if not reviewed:
        _activity("checking")
    checked = None if reviewed else _check(deadline=deadline, error_code="render_timeout")
    if checked is not None and checked.returncode != 0:
        _emit(
            {
                **_command_failure("quality_gate_failed", checked, step="quality_check"),
                "resources": _resource_snapshot(),
            },
            exit_code=checked.returncode or 1,
            result=checked,
            stage="quality_check",
        )

    output_path = _safe_project_path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    resources = _render_resources(workers)
    media_elements = _media_element_count()
    attempts: list[dict] = []
    timeout_attempts: list[dict] = []
    render_memory_peak: dict[str, int] = {}
    global _media_plan, _browser_plan
    while True:
        workers = int(resources["workers"])
        resources["media"] = _media_plan = _media_concurrency(
            resources, browser_workers=workers, media_elements=media_elements, reduced=bool(attempts),
        )
        browser = _browser_plan = _browser_timeout_plan(
            scale=BROWSER_RETRY_SCALE if timeout_attempts else 1,
        )
        if attempts or timeout_attempts:
            _activity("retrying", "Retrying the render with more time or fewer workers", kind="warning")
        else:
            _activity("preparing")
        with _memory_peak_sampler() as memory_peak:
            rendered = _run(
                [
                    "hyperframes",
                    "render",
                    ".",
                    "--quality",
                    quality,
                    "--output",
                    output,
                    "--strict-all",
                    "--no-best-effort",
                    "--no-browser-gpu",
                    "--workers",
                    str(max(1, min(workers, 4))),
                ],
                timeout=max(1, int(deadline - time.monotonic() - 60)),
            )
        if memory_peak.get("memory_bytes", -1) > render_memory_peak.get("memory_bytes", -1):
            render_memory_peak = dict(memory_peak)
        resources["memory_peak_breakdown"] = dict(render_memory_peak)
        if rendered.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0:
            break
        after = _resource_snapshot()
        limit = _resource_limit(resources, after, rendered)
        timed_out = _browser_timeout_reason(rendered) if limit is None else None
        if timed_out is not None:
            timeout_attempts.append({
                "reason": timed_out, "step": "render", "exit_code": rendered.returncode,
                "navigation_timeout_ms": browser["navigation_ms"],
                "page_ready_timeout_ms": browser["page_ready_ms"], "workers": workers,
            })
            if len(timeout_attempts) > 1:
                retry = "exhausted"
            elif deadline - time.monotonic() < RESOURCE_RETRY_MIN_SECONDS:
                retry = "insufficient_time_left"
            else:
                continue
            _emit(
                {
                    **_browser_timeout_error(
                        "render_timeout", timeout_attempts, retry=retry, result=rendered, plan=browser,
                    ),
                    "resources": resources,
                    "resources_after": after,
                },
                exit_code=rendered.returncode or 1,
                result=rendered,
                stage="render",
            )
            return
        audio = _audio_failure(rendered) if limit is None else None
        if audio is not None:
            _emit(
                _audio_failure_error(audio, rendered=rendered, resources=resources, after=after),
                exit_code=rendered.returncode or 1,
                result=rendered,
                stage="render",
            )
            return
        if limit is None:
            cause = _failure_cause(rendered)
            _emit(
                {
                    **_command_failure(RENDER_CODES[cause["kind"]], rendered, step="render", cause=cause),
                    "automatic_retry": "none",
                    "workers": workers,
                    "quality": quality,
                    "resources": resources,
                    "resources_after": after,
                    **({"attempts": attempts} if attempts else {}),
                },
                exit_code=rendered.returncode or 1,
                result=rendered,
                stage="render",
            )
            return
        attempts.append({
            **limit, "exit_code": rendered.returncode, "workers": workers, "media": resources["media"],
        })
        if len(attempts) > 1:
            retry = "exhausted"
        elif deadline - time.monotonic() < RESOURCE_RETRY_MIN_SECONDS:
            retry = "insufficient_time_left"
        else:
            # Once, at the floor: one browser worker, one FFmpeg on one CPU.
            retried = _fit_workers(1)
            if retried["workers"] >= 1:
                resources = retried
                continue
            retry = "no_headroom_left"
        _emit(
            _resource_limit_error(
                limit, attempts, retry=retry, rendered=rendered, resources=resources, after=after,
            ),
            exit_code=rendered.returncode or 1,
            result=rendered,
            stage="render",
        )
        return
    _activity("verifying_output")
    probed = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_name,width,height,r_frame_rate",
            "-of",
            "json",
            output_path.as_posix(),
        ],
        timeout=60,
    )
    probe = {}
    if probed.returncode == 0:
        try:
            probe = json.loads(probed.stdout)
        except json.JSONDecodeError:
            probe = {}
    _emit(
        {
            "status": "ok",
            "operation": "render",
            "files": [output],
            "size_bytes": output_path.stat().st_size,
            "probe": probe,
            "render_stdout": rendered.stdout[-80000:],
            "resources": resources,
            **({"resource_retry": attempts} if attempts else {}),
            **({"browser_timeout_retry": timeout_attempts} if timeout_attempts else {}),
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("probe", "manifest", "review", "render"))
    parser.add_argument("--snapshot-times", default="0.6,2.3,4.1,5.9,7.7,9.4")
    parser.add_argument("--output", default="renders/final.mp4")
    parser.add_argument("--quality", choices=("standard", "high"), default="high")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--reviewed", action="store_true")
    args = parser.parse_args()

    if args.operation == "probe":
        _probe_runtime()
    if not PROJECT.is_dir():
        _emit(
            {
                "status": "error",
                "code": "project_not_found",
                "error": "/skill/project was not staged.",
            },
            exit_code=2,
        )
    if args.operation == "manifest":
        _manifest()
    _stage_runtime()
    if args.operation == "review":
        _review(args.snapshot_times)
    _render(args.output, args.quality, args.workers, reviewed=args.reviewed)


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired as exc:
        _emit(
            {
                "status": "error",
                "code": "runtime_timeout",
                "reason": "process_timeout",
                "retryable": True,
                "error": f"Video render command timed out after {exc.timeout}s.",
                "stdout": _text_tail(exc.stdout, 80000),
                "stderr": _text_tail(exc.stderr, 40000),
                "resources": _resource_snapshot(),
            },
            exit_code=124,
            result=subprocess.CompletedProcess(
                exc.cmd, 124, _text_tail(exc.stdout, 80000), _text_tail(exc.stderr, 40000),
            ),
        )
    except Exception as exc:
        _emit(
            {
                "status": "error",
                "code": "runtime_failed",
                "reason": type(exc).__name__,
                "retryable": False,
                "error": str(exc),
                "resources": _resource_snapshot(),
            },
            exit_code=1,
        )
