#!/usr/bin/env python3
"""Run fixed video quality and render operations inside a Manor sandbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path


PROJECT = Path("/skill/project")
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
TERMINATE_GRACE_SECONDS = 5


def _emit(payload: dict, *, exit_code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(exit_code)


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


def _run(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "NO_COLOR": "1",
        "HYPERFRAMES_BROWSER_PATH": "/usr/bin/chromium",
    }
    if args[:2] == ["hyperframes", "render"]:
        # Capture first, then encode: software Chromium and the high-quality
        # encoder otherwise compete inside the same bounded execution sandbox.
        env["PRODUCER_ENABLE_STREAMING_ENCODE"] = "false"
    proc = subprocess.Popen(
        args,
        cwd=PROJECT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
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
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


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
    return {
        "pids_limit": number("pids.max"),
        "pids_current": number("pids.current"),
        "pids_denied": denied_pids,
        "memory_limit": number("memory.max"),
        "memory_current": number("memory.current"),
        "cpu_limit": cpus,
    }


def _render_resources(requested: int) -> dict[str, int | None]:
    resources = _resource_snapshot()
    limits = [max(1, min(requested, 4)), resources["cpu_limit"] or 1]
    for kind, reserve, per_worker in (
        ("pids", RESERVED_PIDS, WORKER_PIDS),
        ("memory", RESERVED_MEMORY, WORKER_MEMORY),
    ):
        maximum, current = resources[f"{kind}_limit"], resources[f"{kind}_current"]
        if maximum is not None and current is not None:
            limits.append(max(0, maximum - current - reserve) // per_worker)
        else:
            # An unavailable/unlimited cgroup is not evidence of a safe parallel budget.
            limits.append(1)
    workers = min(limits)
    resources.update(workers_requested=requested, workers=workers)
    if workers < 1:
        _emit({
            "status": "error", "code": "render_resources_exhausted",
            "error": "Insufficient sandbox PID or memory headroom for one render worker.",
            "resources": resources,
        }, exit_code=2)
    return resources


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


def _check() -> subprocess.CompletedProcess[str]:
    return _run(
        ["hyperframes", "check", ".", "--strict", "--no-browser-gpu", "--json", "--timeout=30000"],
        timeout=480,
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


def _review(snapshot_times: str) -> None:
    checked = _check()
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
            _emit({
                "status": "ok",
                "operation": "review",
                "review_passed": False,
                "check_report": report,
            })
        _emit(
            {
                "status": "error",
                "code": "quality_gate_failed",
                "stdout": checked.stdout[-80000:],
                "stderr": checked.stderr[-40000:],
            },
            exit_code=checked.returncode or 1,
        )

    review_dir = PROJECT / REVIEW_DIR
    if review_dir.exists():
        shutil.rmtree(review_dir)
    reviewed = _run(
        [
            "hyperframes",
            "snapshot",
            ".",
            "--at",
            snapshot_times,
            "--no-end",
            "--timeout=30000",
            "--output",
            REVIEW_DIR.as_posix(),
        ],
        timeout=300,
    )
    if reviewed.returncode != 0:
        _emit(
            {
                "status": "error",
                "code": "snapshot_failed",
                "stdout": reviewed.stdout[-80000:],
                "stderr": reviewed.stderr[-40000:],
            },
            exit_code=reviewed.returncode or 1,
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


def _render(output: str, quality: str, workers: int) -> None:
    # The outer Sandbox exec allows 1800s, including preflight and output probing.
    deadline = time.monotonic() + 1740
    checked = _check()
    if checked.returncode != 0:
        _emit(
            {
                "status": "error",
                "code": "quality_gate_failed",
                "stdout": checked.stdout[-80000:],
                "stderr": checked.stderr[-40000:],
                "resources": _resource_snapshot(),
            },
            exit_code=checked.returncode or 1,
        )

    output_path = _safe_project_path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    resources = _render_resources(workers)
    workers = int(resources["workers"])
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
            str(max(1, min(workers, 2))),
        ],
        timeout=max(1, int(deadline - time.monotonic() - 60)),
    )
    if rendered.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
        _emit(
            {
                "status": "error",
                "code": "render_failed",
                "stdout": rendered.stdout[-80000:],
                "stderr": rendered.stderr[-40000:],
                "resources": resources,
                "resources_after": _resource_snapshot(),
            },
            exit_code=rendered.returncode or 1,
        )
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
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("probe", "manifest", "review", "render"))
    parser.add_argument("--snapshot-times", default="0.6,2.3,4.1,5.9,7.7,9.4")
    parser.add_argument("--output", default="renders/final.mp4")
    parser.add_argument("--quality", choices=("standard", "high"), default="high")
    parser.add_argument("--workers", type=int, default=4)
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
    _render(args.output, args.quality, args.workers)


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired as exc:
        _emit(
            {
                "status": "error",
                "code": "runtime_timeout",
                "error": f"Video render command timed out after {exc.timeout}s.",
                "stdout": _text_tail(exc.stdout, 80000),
                "stderr": _text_tail(exc.stderr, 40000),
                "resources": _resource_snapshot(),
            },
            exit_code=124,
        )
    except Exception as exc:
        _emit(
            {
                "status": "error",
                "code": "runtime_failed",
                "error": str(exc),
                "resources": _resource_snapshot(),
            },
            exit_code=1,
        )
