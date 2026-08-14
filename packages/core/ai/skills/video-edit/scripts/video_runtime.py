#!/usr/bin/env python3
"""Run fixed video quality and render operations inside a Manor sandbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
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


def _emit(payload: dict, *, exit_code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(exit_code)


def _run(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "NO_COLOR": "1",
        "HYPERFRAMES_BROWSER_PATH": "/usr/bin/chromium",
    }
    return subprocess.run(
        args,
        cwd=PROJECT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


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
        ["hyperframes", "check", ".", "--strict", "--snapshots", "--json"],
        timeout=300,
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
            "files": files[:12],
            "check_stdout": checked.stdout[-80000:],
        }
    )


def _render(output: str, quality: str, workers: int) -> None:
    checked = _check()
    if checked.returncode != 0:
        _emit(
            {
                "status": "error",
                "code": "quality_gate_failed",
                "stdout": checked.stdout[-80000:],
                "stderr": checked.stderr[-40000:],
            },
            exit_code=checked.returncode or 1,
        )

    output_path = _safe_project_path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
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
            "--workers",
            str(max(1, min(workers, 4))),
            "--no-low-memory-mode",
        ],
        timeout=1800,
    )
    if rendered.returncode != 0 or not output_path.is_file() or output_path.stat().st_size <= 0:
        _emit(
            {
                "status": "error",
                "code": "render_failed",
                "stdout": rendered.stdout[-80000:],
                "stderr": rendered.stderr[-40000:],
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
            },
            exit_code=124,
        )
    except Exception as exc:
        _emit(
            {
                "status": "error",
                "code": "runtime_failed",
                "error": str(exc),
            },
            exit_code=1,
        )
