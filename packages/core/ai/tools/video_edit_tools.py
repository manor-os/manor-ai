"""Interactive, isolated video authoring, review, and render orchestration.

Video Edit keeps one owned Sandbox alive across asset exchange, project
authoring, review, revision, and approval-gated rendering.  Editable source is
incrementally synchronized back to Manor's durable entity filesystem.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from packages.core.ai.runtime.video_edit_sessions import (
    append_video_edit_session_event,
    assert_video_edit_session_owner,
    delete_video_edit_session,
    load_conversation_video_edit_session,
    load_video_edit_session,
    new_video_edit_session,
    save_video_edit_session,
    video_edit_session_events,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs


HYPERFRAMES_VERSION = "0.7.105"
GSAP_VERSION = "3.14.2"
_REQUIRED_PROJECT_FILES = {
    "BRIEF.md",
    "STORYBOARD.md",
    "video.json",
    "index.html",
    "package.json",
}
_HASH_EXCLUDED_DIRS = {
    ".git",
    ".hyperframes",
    ".hf-tmp",
    "node_modules",
    "renders",
    "snapshots",
}
_MAX_REFERENCE_BYTES = 1_000_000_000
_MAX_SANDBOX_PROJECT_BYTES = 1_500_000_000
_MAX_SANDBOX_OUTPUT_BYTES = 512 * 1024 * 1024
_SANDBOX_WORKDIR_SIZE = "2g"
_SANDBOX_MEMORY = "4g"
_SANDBOX_CPUS = 4.0
_SANDBOX_RENDER_TIMEOUT = 1800
_SANDBOX_UPLOAD_CONCURRENCY = 4
_MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
_MAX_TEXT_BATCH_BYTES = 16 * 1024 * 1024
_MAX_TEXT_FILES_PER_CALL = 64
_VIDEO_SESSION_EVENT_TYPES = {
    "asset_request",
    "progress",
    "preview_frame",
    "warning",
    "question",
    "artifact",
}


def _video_authoring_contract() -> dict[str, Any]:
    """Return the minimum deterministic project contract for direct tool users."""
    return {
        "required_files": sorted(_REQUIRED_PROJECT_FILES) + ["index.motion.json"],
        "composition_root": {
            "required_attributes": [
                "data-composition-id",
                'data-start="0"',
                "data-width",
                "data-height",
                "data-duration",
                "data-fps",
            ],
            "timed_children": (
                "Every visible timed child is a direct child of the root and has a stable id, "
                'class="clip", data-start, data-duration, and data-track-index.'
            ),
            "forbidden": ["data-end"],
        },
        "timeline": {
            "requirement": (
                "Load ./assets/vendor/gsap.min.js and synchronously register exactly one paused "
                "GSAP timeline at window.__timelines[<data-composition-id>]. The review renderer "
                "seeks this timeline; a custom object with render/setTime methods is not compatible."
            ),
            "example": (
                "const tl = gsap.timeline({paused:true}); "
                "tl.set('#scene-1',{autoAlpha:1},0).to('#scene-1 .content',"
                "{x:24,duration:3,ease:'none'},0); "
                "window.__timelines['demo']=tl;"
            ),
        },
        "motion_assertions": {
            "supported_kinds": ["appearsBy", "before", "staysInFrame", "keepsMoving"],
            "forbidden_kinds": ["exists"],
            "example": {
                "duration": 12,
                "assertions": [
                    {"kind": "appearsBy", "selector": "#headline", "bySec": 0.8},
                    {"kind": "before", "a": "#headline", "b": "#cta"},
                    {"kind": "staysInFrame", "selector": ".product-card"},
                    {
                        "kind": "keepsMoving",
                        "withinSelector": ".scene",
                        "maxStaticSec": 2,
                    },
                ],
            },
        },
        "determinism": (
            "Use composition time only: no live clocks, unseeded randomness, infinite repeats, "
            "network requests, or forward-only event callbacks."
        ),
    }


class VideoEditRuntimeUnavailable(RuntimeError):
    """Raised when the isolated video-editing runtime cannot be used."""


VIDEO_EDIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "video_edit",
        "description": (
            "Video Edit runs production video authoring, asset exchange, review, and rendering in "
            "one isolated Sandbox session. Use it for product demos, UI walkthroughs, promos, "
            "explainers, motion graphics, title sequences, and composed videos from screenshots or "
            "clips. Start a session, push attached or externally acquired assets, write editable "
            "project files inside that session, and poll session events/status. Editable source is "
            "synced back to Manor. Review creates approval frames and a review_sha256; render accepts "
            "only that exact approved source hash. Start returns the exact authoring_contract that "
            "must be followed before the first review. The legacy prepare operation remains an alias for "
            "starting a session that permits external file authoring."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "start",
                        "prepare",
                        "push_assets",
                        "write_files",
                        "sync",
                        "status",
                        "events",
                        "report",
                        "review",
                        "render",
                        "close",
                    ],
                },
                "project_path": {
                    "type": "string",
                    "description": (
                        "Entity-relative editable video project directory, for example Videos/manor-launch/source."
                    ),
                },
                "reference_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "For prepare: Knowledge-relative paths or /api/v1/fs URLs to copy "
                        "into the project's assets/references directory. Also used by "
                        "start and push_assets."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "Opaque Video Edit session id returned by start/prepare. May be omitted in "
                        "the owning conversation when its latest video session is unambiguous."
                    ),
                },
                "files": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "For write_files: project-relative UTF-8 source files to write inside the "
                        "Sandbox, for example {'index.html': '...', 'index.motion.json': '...'}."
                    ),
                },
                "delete_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For write_files: project-relative source files to remove.",
                },
                "after_event_seq": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "For events/status: return only events after this sequence.",
                },
                "event_type": {
                    "type": "string",
                    "enum": sorted(_VIDEO_SESSION_EVENT_TYPES),
                    "description": "For report: structured event emitted by the active video worker.",
                },
                "message": {
                    "type": "string",
                    "description": "For report: concise progress, request, warning, or question text.",
                },
                "event_data": {
                    "type": "object",
                    "description": "For report: optional structured event details.",
                },
                "snapshot_times": {
                    "type": "array",
                    "items": {"type": "number", "minimum": 0},
                    "description": "Optional review timestamps in seconds. Defaults to six frames across the video.",
                },
                "review_sha256": {
                    "type": "string",
                    "description": "For render: exact source hash returned by the approved review operation.",
                },
                "output_name": {
                    "type": "string",
                    "description": "For render: MP4 filename or project-relative path. Defaults to renders/final.mp4.",
                },
                "quality": {
                    "type": "string",
                    "enum": ["standard", "high"],
                    "default": "high",
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _json_error(message: str, *, code: str = "invalid_request", **details: Any) -> str:
    return _json({"status": "error", "code": code, "error": message, **details})


def _normalize_entity_relative_path(value: str, *, entity_id: str = "") -> str:
    raw = unquote(str(value or "").strip()).replace("\\", "/")
    if not raw:
        raise ValueError("A non-empty relative path is required")

    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        raw = parsed.path
    marker = f"/api/v1/fs/{entity_id}/" if entity_id else "/api/v1/fs/"
    if entity_id and raw.startswith(marker):
        raw = raw[len(marker) :]
    elif raw.startswith("/api/v1/fs/"):
        remainder = raw[len("/api/v1/fs/") :]
        parts = remainder.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"Invalid entity filesystem URL: {value!r}")
        if entity_id and parts[0] != entity_id:
            raise ValueError("A reference URL cannot target a different entity")
        raw = parts[1]

    raw = raw.lstrip("/")
    normalized = posixpath.normpath(raw)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise ValueError(f"Path traversal is not allowed: {value!r}")
    first = normalized.split("/", 1)[0]
    if first in {".ai"}:
        raise ValueError(f"Hidden system paths are not allowed: {value!r}")
    return normalized


def _resolve_inside(root: Path, rel_path: str) -> Path:
    root_resolved = root.resolve()
    target = (root_resolved / rel_path).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"Path escapes the entity root: {rel_path!r}")
    return target


def _project_source_files(project_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in project_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(project_dir)
        if any(part in _HASH_EXCLUDED_DIRS for part in rel.parts):
            continue
        if rel.parts[:2] == ("assets", "vendor"):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(project_dir).as_posix())


def _project_source_sha256(project_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in _project_source_files(project_dir):
        rel = path.relative_to(project_dir).as_posix().encode("utf-8")
        digest.update(len(rel).to_bytes(8, "big"))
        digest.update(rel)
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _project_duration_seconds(project_dir: Path) -> float:
    index = project_dir / "index.html"
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        return 15.0
    match = re.search(r"\bdata-duration\s*=\s*[\"']([0-9]+(?:\.[0-9]+)?)[\"']", text)
    if not match:
        return 15.0
    duration = float(match.group(1))
    return min(max(duration, 0.25), 3600.0)


def _default_snapshot_times(duration_seconds: float) -> list[float]:
    duration = max(0.25, float(duration_seconds))
    return [round(duration * fraction, 3) for fraction in (0.06, 0.23, 0.41, 0.59, 0.77, 0.94)]


async def _run_process(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "NO_COLOR": "1"},
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return 124, "", f"Command timed out after {timeout_seconds:.0f}s"
    return (
        int(process.returncode or 0),
        stdout_bytes.decode("utf-8", errors="replace")[-80_000:],
        stderr_bytes.decode("utf-8", errors="replace")[-40_000:],
    )


def _validate_project_contract(project_dir: Path) -> list[str]:
    missing = sorted(name for name in _REQUIRED_PROJECT_FILES if not (project_dir / name).is_file())
    motion_files = sorted(project_dir.glob("*.motion.json"))
    if not motion_files:
        missing.append("*.motion.json")
    index = project_dir / "index.html"
    if index.is_file():
        try:
            html = index.read_text(encoding="utf-8")
        except OSError:
            missing.append("index.html: unreadable")
        else:
            root_match = re.search(
                r"<[^>]+\bdata-composition-id\s*=\s*[\"'][^\"']+[\"'][^>]*>",
                html,
                flags=re.IGNORECASE,
            )
            if root_match is None:
                missing.append("index.html: missing data-composition-id root")
            else:
                root_tag = root_match.group(0)
                if not re.search(
                    r"\bdata-start\s*=\s*[\"']0(?:\.0+)?[\"']",
                    root_tag,
                    flags=re.IGNORECASE,
                ):
                    missing.append('index.html: root missing data-start="0"')
                for attr in ("data-width", "data-height", "data-duration", "data-fps"):
                    if not re.search(rf"\b{attr}\s*=", root_tag, flags=re.IGNORECASE):
                        missing.append(f"index.html: root missing {attr}")
            if "data-end=" in html or "data-end =" in html:
                missing.append("index.html: data-end is forbidden; use data-duration")
            if "./assets/vendor/gsap.min.js" not in html:
                missing.append("index.html: missing local GSAP runtime")
            timeline_count = html.count("gsap.timeline")
            if timeline_count != 1 or "window.__timelines" not in html:
                missing.append(
                    "index.html: inline exactly one paused gsap.timeline and register it on window.__timelines"
                )
            timed_children = 0
            for tag_match in re.finditer(
                r"<([a-zA-Z][\w:-]*)\b[^>]*\bdata-start\s*=\s*[\"'][^\"']+[\"'][^>]*>",
                html,
            ):
                tag = tag_match.group(0)
                if "data-composition-id" in tag:
                    continue
                timed_children += 1
                label_match = re.search(r"\bid\s*=\s*[\"']([^\"']+)[\"']", tag)
                label = f"#{label_match.group(1)}" if label_match else tag_match.group(1)
                required_patterns = {
                    "id": r"\bid\s*=",
                    'class containing "clip"': r"\bclass\s*=\s*[\"'][^\"']*\bclip\b[^\"']*[\"']",
                    "data-duration": r"\bdata-duration\s*=",
                    "data-track-index": r"\bdata-track-index\s*=",
                }
                for requirement, pattern in required_patterns.items():
                    if not re.search(pattern, tag, flags=re.IGNORECASE):
                        missing.append(f"index.html: timed element {label} missing {requirement}")
            if timed_children == 0:
                missing.append("index.html: no timed clip children")
    for motion_path in motion_files:
        try:
            spec = json.loads(motion_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            missing.append(f"{motion_path.name}: invalid JSON")
            continue
        assertions = spec.get("assertions") if isinstance(spec, dict) else None
        if not isinstance(assertions, list) or not assertions:
            missing.append(f'{motion_path.name}: requires a non-empty "assertions" array')
            continue
        supported = {"appearsBy", "before", "staysInFrame", "keepsMoving"}
        for index, assertion in enumerate(assertions):
            kind = assertion.get("kind") if isinstance(assertion, dict) else None
            if kind not in supported:
                missing.append(f"{motion_path.name}: assertion {index + 1} has unsupported kind {kind!r}")
    return missing


def _validate_local_runtime_assets(project_dir: Path) -> list[str]:
    errors: list[str] = []
    index = project_dir / "index.html"
    if index.is_file():
        try:
            html = index.read_text(encoding="utf-8")
        except OSError:
            html = ""
        if re.search(r"(?:src|href)\s*=\s*[\"']https?://", html, flags=re.IGNORECASE):
            errors.append("index.html contains a remote runtime/media URL; freeze it under assets/")
        if re.search(r"url\(\s*[\"']?https?://", html, flags=re.IGNORECASE):
            errors.append("index.html contains a remote CSS asset URL; freeze it under assets/")
    return errors


def _video_sandbox_bundle() -> dict[str, str]:
    skill_dir = Path(__file__).resolve().parents[1] / "skills" / "video-edit"
    runner = skill_dir / "scripts" / "video_runtime.py"
    instructions = skill_dir / "SKILL.md"
    if not runner.is_file() or not instructions.is_file():
        raise VideoEditRuntimeUnavailable("The bundled video-editing sandbox runner is missing from the Manor image.")
    return {
        "SKILL.md": instructions.read_text(encoding="utf-8"),
        "scripts/video_runtime.py": runner.read_text(encoding="utf-8"),
    }


def _sandbox_result_payload(stdout: str) -> dict[str, Any]:
    for line in reversed(str(stdout or "").splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


async def _upload_sandbox_sources(
    *,
    client: Any,
    sandbox_id: str,
    project_dir: Path,
    source_files: list[Path],
) -> None:
    """Upload project files concurrently without unbounded memory growth."""

    semaphore = asyncio.Semaphore(_SANDBOX_UPLOAD_CONCURRENCY)

    async def _upload(source: Path) -> None:
        async with semaphore:
            content = await asyncio.to_thread(source.read_bytes)
            rel = source.relative_to(project_dir).as_posix()
            await client.write_file_base64(
                sandbox_id=sandbox_id,
                path=f"/skill/project/{rel}",
                content_base64=base64.b64encode(content).decode("ascii"),
                mkdir=True,
            )

    await asyncio.gather(*(_upload(source) for source in source_files))


async def _create_video_session_sandbox(project_dir: Path) -> tuple[str, dict[str, Any]]:
    """Create one long-lived, network-isolated video Sandbox and seed its source."""

    from packages.core.config import get_settings
    from packages.core.services.sandbox_sdk import SandboxClient

    sandbox_url = get_settings().SANDBOX_SERVICE_URL.strip()
    if not sandbox_url:
        raise VideoEditRuntimeUnavailable("Sandbox Service is not configured; set SANDBOX_SERVICE_URL.")
    source_files = _project_source_files(project_dir) if project_dir.is_dir() else []
    source_bytes = sum(path.stat().st_size for path in source_files)
    if source_bytes > _MAX_SANDBOX_PROJECT_BYTES:
        raise ValueError("Video source project exceeds the 1.5 GB sandbox limit")

    client = SandboxClient(base_url=sandbox_url, timeout=180.0)
    sandbox_id = ""
    started_at = time.perf_counter()
    try:
        created = await client.create_from_builtin(
            skill_name="video-edit-runtime",
            files=_video_sandbox_bundle(),
            env={
                "NO_COLOR": "1",
                "HYPERFRAMES_BROWSER_PATH": "/usr/bin/chromium",
                "HYPERFRAMES_GSAP_PATH": ("/usr/local/lib/node_modules/gsap/dist/gsap.min.js"),
            },
            config={
                "network": "none",
                "memory": _SANDBOX_MEMORY,
                "cpus": _SANDBOX_CPUS,
                "pids_limit": 512,
                "workdir_tmpfs_size": _SANDBOX_WORKDIR_SIZE,
                "exec_timeout": _SANDBOX_RENDER_TIMEOUT,
            },
            auto_install=False,
        )
        sandbox_id = created.sandbox_id
        created_at = time.perf_counter()
        probe = await client.exec(
            sandbox_id=sandbox_id,
            command="python3 /skill/scripts/video_runtime.py probe",
            timeout=60,
        )
        probe_payload = _sandbox_result_payload(probe.stdout)
        if probe.exit_code != 0 or probe_payload.get("status") != "ok":
            detail = probe_payload.get("error") or probe.stderr[-2000:] or "unknown runtime probe failure"
            raise VideoEditRuntimeUnavailable(f"Video Sandbox runtime probe failed: {detail}")
        if source_files:
            await _upload_sandbox_sources(
                client=client,
                sandbox_id=sandbox_id,
                project_dir=project_dir,
                source_files=source_files,
            )
        completed_at = time.perf_counter()
        return sandbox_id, {
            "cold_start_ms": round((created_at - started_at) * 1000),
            "upload_ms": round((completed_at - created_at) * 1000),
            "total_ms": round((completed_at - started_at) * 1000),
            "runtime": probe_payload.get("runtime", {}),
            "source_files": len(source_files),
            "source_bytes": source_bytes,
        }
    except Exception as exc:
        if sandbox_id:
            try:
                await client.destroy(sandbox_id)
            except Exception:
                pass
        if isinstance(exc, (ValueError, VideoEditRuntimeUnavailable)):
            raise
        raise VideoEditRuntimeUnavailable(f"Video editing sandbox could not start: {exc}") from exc
    finally:
        await client.close()


def _sandbox_info_payload(info: Any) -> dict[str, Any]:
    status = getattr(info, "status", "unknown")
    return {
        "status": getattr(status, "value", status),
        "active_command": getattr(info, "active_command", None),
        "created_at": getattr(info, "created_at", None),
        "last_used_at": getattr(info, "last_used_at", None),
        "expires_at": getattr(info, "expires_at", None),
    }


async def _ensure_video_session_sandbox(
    state: dict[str, Any],
    *,
    project_dir: Path,
) -> tuple[dict[str, Any], bool]:
    """Touch an owned session or recover it from durable Manor source."""

    from packages.core.config import get_settings
    from packages.core.services.sandbox_sdk import SandboxClient
    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

    sandbox_url = get_settings().SANDBOX_SERVICE_URL.strip()
    if not sandbox_url:
        raise VideoEditRuntimeUnavailable("Sandbox Service is not configured; set SANDBOX_SERVICE_URL.")
    sandbox_id = str(state.get("sandbox_id") or "")
    client = SandboxClient(base_url=sandbox_url, timeout=180.0)
    try:
        try:
            info = await client.touch(sandbox_id)
            return _sandbox_info_payload(info), False
        except SandboxNotFoundError:
            pass
    finally:
        await client.close()

    replacement_id, metrics = await _create_video_session_sandbox(project_dir)
    state["sandbox_id"] = replacement_id
    append_video_edit_session_event(
        state,
        "progress",
        message="Sandbox session recovered from durable project source.",
        data={"recovered": True, "sandbox_metrics": metrics},
    )
    await save_video_edit_session(state)
    return {
        "status": "ready",
        "active_command": None,
        "expires_at": time.time() + 3600,
    }, True


def _normalize_project_file_path(value: str) -> str:
    rel_path = _normalize_entity_relative_path(value)
    if any(part in _HASH_EXCLUDED_DIRS for part in Path(rel_path).parts):
        raise ValueError(f"Generated output paths are not editable source: {value!r}")
    if rel_path == "hyperframes.json":
        raise ValueError("hyperframes.json is runtime-owned; edit video.json instead")
    return rel_path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def _sync_video_session_to_entity(
    *,
    state: dict[str, Any],
    project_dir: Path,
    entity_id: str,
) -> dict[str, Any]:
    """Copy changed editable source from the Sandbox into Manor atomically."""

    from packages.core.config import get_settings
    from packages.core.services import entity_fs
    from packages.core.services.sandbox_sdk import SandboxClient

    sandbox_url = get_settings().SANDBOX_SERVICE_URL.strip()
    client = SandboxClient(base_url=sandbox_url, timeout=180.0)
    sandbox_id = str(state.get("sandbox_id") or "")
    try:
        result = await client.exec(
            sandbox_id=sandbox_id,
            command="python3 /skill/scripts/video_runtime.py manifest",
            timeout=300,
        )
        payload = _sandbox_result_payload(result.stdout)
        if result.exit_code != 0 or payload.get("status") != "ok":
            raise VideoEditRuntimeUnavailable(
                "Video Sandbox could not produce a source manifest: " + (result.stderr[-2000:] or str(payload))
            )
        manifest = [item for item in payload.get("files", []) if isinstance(item, dict)]
        total_bytes = sum(int(item.get("size_bytes") or 0) for item in manifest)
        if total_bytes > _MAX_SANDBOX_PROJECT_BYTES:
            raise ValueError("Video source project exceeds the 1.5 GB sandbox limit")

        written: list[str] = []
        unchanged: list[str] = []
        for item in manifest:
            rel_path = _normalize_project_file_path(str(item.get("path") or ""))
            expected_size = int(item.get("size_bytes") or 0)
            expected_hash = str(item.get("sha256") or "")
            target = _resolve_inside(project_dir, rel_path)
            if target.is_file() and target.stat().st_size == expected_size and _sha256_file(target) == expected_hash:
                unchanged.append(rel_path)
                continue
            file_result = await client.read_file_base64(
                sandbox_id=sandbox_id,
                path=f"/skill/project/{rel_path}",
                max_size=_MAX_SANDBOX_OUTPUT_BYTES,
            )
            content = base64.b64decode(file_result.content_base64)
            if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_hash:
                raise VideoEditRuntimeUnavailable(f"Video source changed while syncing: {rel_path}")
            entity_rel = f"{state['project_path']}/{rel_path}"
            entity_fs.write_entity_file_atomic(
                entity_id,
                entity_rel,
                content,
                expected_size=expected_size,
                allow_empty=True,
            )
            written.append(rel_path)
    finally:
        await client.close()

    source_hash = _project_source_sha256(project_dir)
    state["last_synced_sha256"] = source_hash
    if written:
        state["source_revision"] = int(state.get("source_revision") or 0) + 1
    append_video_edit_session_event(
        state,
        "progress",
        message="Editable source synchronized to Manor.",
        data={
            "written_files": written,
            "unchanged_files": len(unchanged),
            "source_sha256": source_hash,
        },
    )
    await save_video_edit_session(state)
    return {
        "written_files": written,
        "unchanged_files": len(unchanged),
        "source_files": len(manifest),
        "source_bytes": total_bytes,
        "source_sha256": source_hash,
    }


async def _run_video_sandbox(
    *,
    project_dir: Path,
    operation: str,
    sandbox_id: str = "",
    upload_sources: bool = True,
    snapshot_times: list[float] | None = None,
    output_rel: str = "renders/final.mp4",
    quality: str = "high",
) -> tuple[int, dict[str, Any], dict[str, bytes], str]:
    from packages.core.config import get_settings
    from packages.core.services.sandbox_sdk import SandboxClient

    sandbox_url = get_settings().SANDBOX_SERVICE_URL.strip()
    if not sandbox_url:
        raise VideoEditRuntimeUnavailable("Sandbox Service is not configured; set SANDBOX_SERVICE_URL.")

    source_files = _project_source_files(project_dir)
    total_bytes = sum(path.stat().st_size for path in source_files)
    if total_bytes > _MAX_SANDBOX_PROJECT_BYTES:
        raise ValueError("Video source project exceeds the 1.5 GB sandbox limit")

    client = SandboxClient(
        base_url=sandbox_url,
        timeout=float(_SANDBOX_RENDER_TIMEOUT + 60),
    )
    active_sandbox_id = str(sandbox_id or "").strip()
    created_for_call = not active_sandbox_id
    started_at = time.perf_counter()
    created_at = started_at
    uploaded_at = started_at
    executed_at = started_at
    try:
        if created_for_call:
            created = await client.create_from_builtin(
                skill_name="video-edit-runtime",
                files=_video_sandbox_bundle(),
                env={
                    "NO_COLOR": "1",
                    "HYPERFRAMES_BROWSER_PATH": "/usr/bin/chromium",
                    "HYPERFRAMES_GSAP_PATH": ("/usr/local/lib/node_modules/gsap/dist/gsap.min.js"),
                },
                config={
                    "network": "none",
                    "memory": _SANDBOX_MEMORY,
                    "cpus": _SANDBOX_CPUS,
                    "pids_limit": 512,
                    "workdir_tmpfs_size": _SANDBOX_WORKDIR_SIZE,
                    "exec_timeout": _SANDBOX_RENDER_TIMEOUT,
                },
                auto_install=False,
            )
            active_sandbox_id = created.sandbox_id
            created_at = time.perf_counter()
        else:
            await client.touch(active_sandbox_id)
            created_at = time.perf_counter()

        if upload_sources:
            await _upload_sandbox_sources(
                client=client,
                sandbox_id=active_sandbox_id,
                project_dir=project_dir,
                source_files=source_files,
            )
        uploaded_at = time.perf_counter()

        command = [
            "python3",
            "/skill/scripts/video_runtime.py",
            operation,
        ]
        if operation == "review":
            values = snapshot_times or []
            command.extend(["--snapshot-times", ",".join(f"{value:g}" for value in values)])
        else:
            command.extend(
                [
                    "--output",
                    output_rel,
                    "--quality",
                    quality if quality in {"standard", "high"} else "high",
                    "--workers",
                    "4",
                ]
            )
        result = await client.exec(
            sandbox_id=active_sandbox_id,
            command=" ".join(shlex.quote(value) for value in command),
            timeout=(_SANDBOX_RENDER_TIMEOUT if operation == "render" else 600),
        )
        executed_at = time.perf_counter()
        payload = _sandbox_result_payload(result.stdout)
        outputs: dict[str, bytes] = {}
        if result.exit_code == 0 and payload.get("status") == "ok":
            for raw_path in list(payload.get("files") or [])[:12]:
                rel_path = _normalize_entity_relative_path(str(raw_path))
                file_result = await client.read_file_base64(
                    sandbox_id=active_sandbox_id,
                    path=f"/skill/project/{rel_path}",
                    max_size=_MAX_SANDBOX_OUTPUT_BYTES,
                )
                outputs[rel_path] = base64.b64decode(file_result.content_base64)
        completed_at = time.perf_counter()
        payload["sandbox_metrics"] = {
            "cold_start_ms": round((created_at - started_at) * 1000),
            "upload_ms": round((uploaded_at - created_at) * 1000),
            "execution_ms": round((executed_at - uploaded_at) * 1000),
            "download_ms": round((completed_at - executed_at) * 1000),
            "total_ms": round((completed_at - started_at) * 1000),
            "source_files": len(source_files),
            "source_bytes": total_bytes,
        }
        return result.exit_code, payload, outputs, result.stderr[-40_000:]
    except VideoEditRuntimeUnavailable:
        raise
    except Exception as exc:
        raise VideoEditRuntimeUnavailable(f"Video editing sandbox could not start or complete: {exc}") from exc
    finally:
        if active_sandbox_id and created_for_call:
            try:
                await client.destroy(active_sandbox_id)
            except Exception:
                pass
        await client.close()


async def _stage_references(
    *,
    entity_id: str,
    entity_root: Path,
    project_dir: Path,
    raw_paths: list[Any],
    user_id: str | None,
    workspace_base_dir: str,
) -> list[dict[str, Any]]:
    rel_paths = [
        _normalize_entity_relative_path(str(value), entity_id=entity_id)
        for value in raw_paths
        if str(value or "").strip()
    ]
    if not rel_paths:
        return []

    _assert_workspace_reference_scope(
        rel_paths,
        workspace_base_dir=workspace_base_dir,
    )

    from packages.core.database import async_session
    from packages.core.services.document_access import unreadable_document_paths

    async with async_session() as db:
        blocked = await unreadable_document_paths(
            db,
            entity_id=entity_id,
            rel_paths=rel_paths,
            user_id=user_id,
            actor_type="agent",
        )
    if blocked:
        raise PermissionError("Reference access denied: " + ", ".join(sorted(blocked)))

    destination_dir = project_dir / "assets" / "references"
    destination_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    staged: list[dict[str, Any]] = []
    for rel_path in rel_paths:
        source = _resolve_inside(entity_root, rel_path)
        if not source.is_file():
            raise FileNotFoundError(f"Reference file not found: {rel_path}")
        size = source.stat().st_size
        total_bytes += size
        if total_bytes > _MAX_REFERENCE_BYTES:
            raise ValueError("Combined reference files exceed the 1 GB staging limit")

        clean_name = re.sub(r"[^\w.\-]+", "-", source.name, flags=re.UNICODE).strip(".-")
        clean_name = clean_name or "reference"
        candidate = destination_dir / clean_name
        if candidate.exists() and candidate.resolve() != source.resolve():
            suffix = hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:8]
            candidate = destination_dir / f"{candidate.stem}-{suffix}{candidate.suffix}"
        if candidate.resolve() != source.resolve():
            shutil.copy2(source, candidate)
        staged.append(
            {
                "source_path": rel_path,
                "project_path": candidate.relative_to(project_dir).as_posix(),
                "size_bytes": size,
            }
        )
    return staged


def _assert_workspace_reference_scope(
    rel_paths: list[str],
    *,
    workspace_base_dir: str,
) -> None:
    for rel_path in rel_paths:
        if rel_path != "Workspaces" and not rel_path.startswith("Workspaces/"):
            continue
        if not workspace_base_dir or not (
            rel_path == workspace_base_dir or rel_path.startswith(f"{workspace_base_dir}/")
        ):
            raise PermissionError("Workspace reference is outside the active Workspace: " + rel_path)


async def _start_video_session(
    *,
    entity_id: str,
    entity_root: Path,
    project_dir: Path,
    project_rel: str,
    context: Any,
    raw_reference_paths: list[Any],
    workspace_base_dir: str,
    legacy_external_authoring: bool,
) -> str:
    project_dir.mkdir(parents=True, exist_ok=True)
    staged = await _stage_references(
        entity_id=entity_id,
        entity_root=entity_root,
        project_dir=project_dir,
        raw_paths=raw_reference_paths,
        user_id=context.user_id,
        workspace_base_dir=workspace_base_dir,
    )
    sandbox_id, metrics = await _create_video_session_sandbox(project_dir)
    state = new_video_edit_session(
        sandbox_id=sandbox_id,
        entity_id=entity_id,
        user_id=context.user_id,
        conversation_id=context.conversation_id,
        workspace_id=context.workspace_id,
        project_path=project_rel,
        legacy_external_authoring=legacy_external_authoring,
    )
    append_video_edit_session_event(
        state,
        "progress",
        message="Video Sandbox started and editable project source was staged.",
        data={
            "project_path": project_rel,
            "staged_references": len(staged),
            "sandbox_metrics": metrics,
        },
    )
    if staged:
        append_video_edit_session_event(
            state,
            "progress",
            message="Reference assets delivered to the active video worker.",
            data={"assets": staged},
        )
    await save_video_edit_session(state)
    status = "prepared" if legacy_external_authoring else "ready"
    return _json(
        {
            "status": status,
            "session_id": state["session_id"],
            "project_path": project_rel,
            "execution_runtime": "persistent_isolated_sandbox",
            "sandbox_status": "ready",
            "sandbox_metrics": metrics,
            "staged_references": staged,
            "event_cursor": state["last_event_seq"],
            "events": video_edit_session_events(state),
            "authoring_contract": _video_authoring_contract(),
            "next_step": (
                "Write the editable project inside this session with write_files, then run review."
                if not legacy_external_authoring
                else "Author the editable project, then call review; the same Sandbox will be reused."
            ),
        }
    )


async def _resolve_owned_video_session(
    *,
    session_id: str,
    entity_id: str,
    context: Any,
    project_rel: str = "",
) -> dict[str, Any]:
    state = await load_video_edit_session(session_id)
    if state is None and not session_id:
        state = await load_conversation_video_edit_session(str(context.conversation_id or ""))
    if state is None:
        raise ValueError("Video Edit session not found or expired; call start again")
    assert_video_edit_session_owner(
        state,
        entity_id=entity_id,
        user_id=context.user_id,
        conversation_id=context.conversation_id,
    )
    if project_rel and str(state.get("project_path") or "") != project_rel:
        raise ValueError("Video Edit session belongs to a different project path")
    return state


async def _push_video_session_assets(
    *,
    state: dict[str, Any],
    entity_id: str,
    entity_root: Path,
    project_dir: Path,
    context: Any,
    raw_paths: list[Any],
    workspace_base_dir: str,
) -> str:
    await _ensure_video_session_sandbox(state, project_dir=project_dir)
    staged = await _stage_references(
        entity_id=entity_id,
        entity_root=entity_root,
        project_dir=project_dir,
        raw_paths=raw_paths,
        user_id=context.user_id,
        workspace_base_dir=workspace_base_dir,
    )
    if not staged:
        return _json_error(
            "push_assets requires at least one readable reference path",
            code="asset_required",
        )

    from packages.core.config import get_settings
    from packages.core.services.sandbox_sdk import SandboxClient

    client = SandboxClient(
        base_url=get_settings().SANDBOX_SERVICE_URL.strip(),
        timeout=180.0,
    )
    try:
        await _upload_sandbox_sources(
            client=client,
            sandbox_id=str(state["sandbox_id"]),
            project_dir=project_dir,
            source_files=[_resolve_inside(project_dir, str(item["project_path"])) for item in staged],
        )
    finally:
        await client.close()
    append_video_edit_session_event(
        state,
        "progress",
        message="Assets received by the active video worker.",
        data={"assets": staged},
    )
    await save_video_edit_session(state)
    return _json(
        {
            "status": "assets_received",
            "session_id": state["session_id"],
            "project_path": state["project_path"],
            "staged_references": staged,
            "event_cursor": state["last_event_seq"],
            "events": [state["events"][-1]],
        }
    )


async def _write_video_session_files(
    *,
    state: dict[str, Any],
    entity_id: str,
    project_dir: Path,
    files: Any,
    delete_paths: list[Any],
) -> str:
    await _ensure_video_session_sandbox(state, project_dir=project_dir)
    if not isinstance(files, dict):
        files = {}
    if len(files) > _MAX_TEXT_FILES_PER_CALL:
        return _json_error(f"write_files accepts at most {_MAX_TEXT_FILES_PER_CALL} files per call")
    normalized_files: list[tuple[str, str, bytes]] = []
    total_bytes = 0
    for raw_path, raw_content in files.items():
        if not isinstance(raw_content, str):
            return _json_error(f"Text source content must be a string: {raw_path}")
        rel_path = _normalize_project_file_path(str(raw_path))
        content = raw_content.encode("utf-8")
        if len(content) > _MAX_TEXT_FILE_BYTES:
            return _json_error(f"Text source file exceeds 2 MB: {rel_path}")
        total_bytes += len(content)
        normalized_files.append((rel_path, raw_content, content))
    if total_bytes > _MAX_TEXT_BATCH_BYTES:
        return _json_error("write_files payload exceeds the 16 MB batch limit")

    normalized_deletes = [
        _normalize_project_file_path(str(value)) for value in delete_paths if str(value or "").strip()
    ]
    if not normalized_files and not normalized_deletes:
        return _json_error("write_files requires files or delete_paths")

    from packages.core.config import get_settings
    from packages.core.services import entity_fs
    from packages.core.services.sandbox_sdk import SandboxClient

    client = SandboxClient(
        base_url=get_settings().SANDBOX_SERVICE_URL.strip(),
        timeout=180.0,
    )
    try:
        for rel_path, content_text, content_bytes in normalized_files:
            await client.write_file(
                sandbox_id=str(state["sandbox_id"]),
                path=f"/skill/project/{rel_path}",
                content=content_text,
                mkdir=True,
            )
            entity_fs.write_entity_file_atomic(
                entity_id,
                f"{state['project_path']}/{rel_path}",
                content_bytes,
                expected_size=len(content_bytes),
                allow_empty=True,
            )
        for rel_path in normalized_deletes:
            result = await client.exec(
                sandbox_id=str(state["sandbox_id"]),
                command=f"rm -f -- {shlex.quote('/skill/project/' + rel_path)}",
                timeout=30,
            )
            if result.exit_code != 0:
                raise VideoEditRuntimeUnavailable(f"Could not remove Sandbox source file {rel_path}: {result.stderr}")
            target = _resolve_inside(project_dir, rel_path)
            if target.is_file() or target.is_symlink():
                target.unlink()
    finally:
        await client.close()

    source_hash = _project_source_sha256(project_dir)
    missing_contract = _validate_project_contract(project_dir)
    state["source_revision"] = int(state.get("source_revision") or 0) + 1
    state["last_synced_sha256"] = source_hash
    append_video_edit_session_event(
        state,
        "progress",
        message="Editable project files updated inside the Video Sandbox and synced to Manor.",
        data={
            "written_files": [item[0] for item in normalized_files],
            "deleted_files": normalized_deletes,
            "source_sha256": source_hash,
        },
    )
    await save_video_edit_session(state)
    return _json(
        {
            "status": "source_updated",
            "session_id": state["session_id"],
            "project_path": state["project_path"],
            "written_files": [item[0] for item in normalized_files],
            "deleted_files": normalized_deletes,
            "source_sha256": source_hash,
            "source_revision": state["source_revision"],
            "event_cursor": state["last_event_seq"],
            "events": [state["events"][-1]],
            "missing_project_contract": missing_contract,
            "next_step": (
                "Write the missing required project files before review: " + ", ".join(missing_contract)
                if missing_contract
                else "Project file contract is complete; run review in the same session."
            ),
        }
    )


async def _register_artifact(
    *,
    abs_path: Path,
    entity_root: Path,
    entity_id: str,
    context: Any,
    artifact_role: str,
    generation: dict[str, Any],
) -> dict[str, Any]:
    from packages.core.ai.runtime.tool_adapters import (
        runtime_register_video_edit_artifact,
    )

    return await runtime_register_video_edit_artifact(
        abs_path=abs_path,
        entity_root=entity_root,
        entity_id=entity_id,
        context=context,
        artifact_role=artifact_role,
        generation=generation,
    )


async def _review_project(
    *,
    project_dir: Path,
    project_rel: str,
    entity_root: Path,
    entity_id: str,
    context: Any,
    snapshot_times: list[Any] | None,
    session_state: dict[str, Any] | None = None,
) -> str:
    upload_sources = True
    if session_state is not None:
        await _ensure_video_session_sandbox(session_state, project_dir=project_dir)
        upload_sources = bool(session_state.get("legacy_external_authoring"))
        if not upload_sources:
            await _sync_video_session_to_entity(
                state=session_state,
                project_dir=project_dir,
                entity_id=entity_id,
            )
    missing = _validate_project_contract(project_dir)
    missing.extend(_validate_local_runtime_assets(project_dir))
    if missing:
        return _json_error(
            "Video project contract is incomplete",
            code="project_contract_incomplete",
            missing=missing,
        )

    if snapshot_times:
        times = sorted({round(float(value), 3) for value in snapshot_times if float(value) >= 0})[:12]
    else:
        times = _default_snapshot_times(_project_duration_seconds(project_dir))
    if not times:
        times = _default_snapshot_times(_project_duration_seconds(project_dir))

    source_hash = _project_source_sha256(project_dir)
    try:
        exit_code, payload, outputs, stderr = await _run_video_sandbox(
            project_dir=project_dir,
            operation="review",
            sandbox_id=(str(session_state.get("sandbox_id") or "") if session_state is not None else ""),
            upload_sources=upload_sources,
            snapshot_times=times,
        )
    except VideoEditRuntimeUnavailable as exc:
        return _json_error(str(exc), code="runtime_unavailable")
    if exit_code != 0 or payload.get("status") != "ok":
        if session_state is not None:
            append_video_edit_session_event(
                session_state,
                "warning",
                message="Video review gate failed; the project needs another edit pass.",
                data={
                    "code": str(payload.get("code") or "quality_gate_failed"),
                    "exit_code": exit_code,
                },
            )
            await save_video_edit_session(session_state)
        return _json_error(
            "Video sandbox review failed; fix every reported issue before asking for approval.",
            code=str(payload.get("code") or "quality_gate_failed"),
            exit_code=exit_code,
            runtime_result=payload,
            stderr=stderr,
        )

    review_rel = "snapshots/manor-review"
    review_dir = project_dir / review_rel
    if review_dir.exists():
        shutil.rmtree(review_dir)
    for rel_path, content in outputs.items():
        if not rel_path.startswith(f"{review_rel}/"):
            continue
        target = _resolve_inside(project_dir, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    review_files = sorted(
        [
            path
            for path in review_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ],
        key=lambda path: ("contact" not in path.name.lower(), path.name.lower()),
    )
    artifacts: list[dict[str, Any]] = []
    for path in review_files[:12]:
        artifacts.append(
            await _register_artifact(
                abs_path=path,
                entity_root=entity_root,
                entity_id=entity_id,
                context=context,
                artifact_role="review",
                generation={
                    "kind": "video_review",
                    "project_path": project_rel,
                    "source_sha256": source_hash,
                    "snapshot_times": times,
                    "video_runtime_version": HYPERFRAMES_VERSION,
                },
            )
        )

    if session_state is not None:
        session_state["legacy_external_authoring"] = False
        session_state["last_review_sha256"] = source_hash
        session_state["last_review_artifacts"] = artifacts
        append_video_edit_session_event(
            session_state,
            "preview_frame",
            message="Review frames are ready for user approval.",
            data={
                "review_sha256": source_hash,
                "review_artifacts": artifacts,
                "snapshot_times": times,
            },
        )
        await save_video_edit_session(session_state)

    return _json(
        {
            "status": "ready_for_review",
            "session_id": (session_state.get("session_id") if session_state is not None else None),
            "project_path": project_rel,
            "review_sha256": source_hash,
            "quality_gate": "passed",
            "snapshot_times": times,
            "review_artifacts": artifacts,
            "approval_required": True,
            "execution_runtime": "isolated_sandbox",
            "sandbox_metrics": payload.get("sandbox_metrics", {}),
            "event_cursor": (session_state.get("last_event_seq") if session_state is not None else None),
            "events": ([session_state["events"][-1]] if session_state is not None else []),
            "next_step": (
                "Show the review artifacts to the user and ask for explicit approval. "
                "After approval, call render with this exact review_sha256."
            ),
        }
    )


async def _probe_render(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    code, stdout, _ = await _run_process(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        cwd=path.parent,
        timeout_seconds=60,
    )
    if code != 0:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {}


async def _render_project(
    *,
    project_dir: Path,
    project_rel: str,
    entity_root: Path,
    entity_id: str,
    context: Any,
    review_sha256: str,
    output_name: str,
    quality: str,
    session_state: dict[str, Any] | None = None,
) -> str:
    upload_sources = True
    if session_state is not None:
        await _ensure_video_session_sandbox(session_state, project_dir=project_dir)
        upload_sources = bool(session_state.get("legacy_external_authoring"))
        if not upload_sources:
            await _sync_video_session_to_entity(
                state=session_state,
                project_dir=project_dir,
                entity_id=entity_id,
            )
    approved_hash = str(review_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", approved_hash):
        return _json_error(
            "render requires the review_sha256 returned by an approved review",
            code="approval_required",
        )
    if session_state is not None and str(session_state.get("last_review_sha256") or "") != approved_hash:
        return _json_error(
            "This active session has not reviewed the supplied source hash. Run review again.",
            code="review_required",
        )
    current_hash = _project_source_sha256(project_dir)
    if current_hash != approved_hash:
        return _json_error(
            "Project source changed after review. Run review again and obtain fresh approval.",
            code="source_changed_after_review",
            approved_sha256=approved_hash,
            current_sha256=current_hash,
        )

    requested_output = str(output_name or "renders/final.mp4").strip().replace("\\", "/")
    if "/" not in requested_output:
        requested_output = f"renders/{requested_output}"
    output_rel = _normalize_entity_relative_path(requested_output)
    if not output_rel.lower().endswith(".mp4"):
        output_rel += ".mp4"
    output_path = _resolve_inside(project_dir, output_rel)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        exit_code, payload, outputs, stderr = await _run_video_sandbox(
            project_dir=project_dir,
            operation="render",
            sandbox_id=(str(session_state.get("sandbox_id") or "") if session_state is not None else ""),
            upload_sources=upload_sources,
            output_rel=output_rel,
            quality=quality,
        )
    except VideoEditRuntimeUnavailable as exc:
        return _json_error(str(exc), code="runtime_unavailable")
    output_bytes = outputs.get(output_rel, b"")
    if exit_code != 0 or payload.get("status") != "ok" or not output_bytes:
        if session_state is not None:
            append_video_edit_session_event(
                session_state,
                "warning",
                message="Final video render failed inside the active Sandbox.",
                data={
                    "code": str(payload.get("code") or "render_failed"),
                    "exit_code": exit_code,
                },
            )
            await save_video_edit_session(session_state)
        return _json_error(
            "Video sandbox render failed or produced no MP4.",
            code=str(payload.get("code") or "render_failed"),
            exit_code=exit_code,
            runtime_result=payload,
            stderr=stderr,
        )
    output_path.write_bytes(output_bytes)

    probe = await _probe_render(output_path)
    artifact = await _register_artifact(
        abs_path=output_path,
        entity_root=entity_root,
        entity_id=entity_id,
        context=context,
        artifact_role="final",
        generation={
            "kind": "edited_video",
            "project_path": project_rel,
            "source_sha256": current_hash,
            "quality": quality,
            "video_runtime_version": HYPERFRAMES_VERSION,
        },
    )
    if session_state is not None:
        session_state["legacy_external_authoring"] = False
        session_state["last_render_sha256"] = current_hash
        session_state["last_render_artifact"] = artifact
        append_video_edit_session_event(
            session_state,
            "artifact",
            message="Approved video render completed.",
            data={"video": artifact, "source_sha256": current_hash},
        )
        await save_video_edit_session(session_state)
    return _json(
        {
            "status": "completed",
            "session_id": (session_state.get("session_id") if session_state is not None else None),
            "project_path": project_rel,
            "source_sha256": current_hash,
            "quality_gate": "passed",
            "execution_runtime": "isolated_sandbox",
            "sandbox_metrics": payload.get("sandbox_metrics", {}),
            "video": artifact,
            "media_probe": probe,
            "event_cursor": (session_state.get("last_event_seq") if session_state is not None else None),
            "events": ([session_state["events"][-1]] if session_state is not None else []),
        }
    )


async def _video_edit_handler(
    entity_id: str = "",
    user_id: str = "",
    **kwargs: Any,
) -> str:
    if not entity_id:
        return _json_error("No entity context", code="missing_entity")
    operation = str(kwargs.get("operation") or "").strip().lower()
    supported_operations = {
        "start",
        "prepare",
        "push_assets",
        "write_files",
        "sync",
        "status",
        "events",
        "report",
        "review",
        "render",
        "close",
    }
    if operation not in supported_operations:
        return _json_error(
            "Unsupported Video Edit operation",
            supported_operations=sorted(supported_operations),
        )

    try:
        from packages.core.services import entity_fs

        entity_fs.assert_entity_filesystem_ready()
        entity_root = Path(entity_fs.get_entity_root(entity_id))
        context = runtime_tool_call_context_from_kwargs(
            {**kwargs, "_user_id_from_context": kwargs.get("_user_id_from_context") or user_id}
        )
        workspace_base_dir = ""
        if context.workspace_id:
            from packages.core.services.generated_media_naming import (
                resolve_workspace_artifact_base_dir,
                scope_workspace_artifact_path,
            )

            workspace_base_dir = await resolve_workspace_artifact_base_dir(
                entity_id=entity_id,
                workspace_id=context.workspace_id,
                task_id=context.task_id,
            )
        raw_project_path = str(kwargs.get("project_path") or "").strip()
        project_rel = ""
        if raw_project_path:
            project_rel = _normalize_entity_relative_path(
                raw_project_path,
                entity_id=entity_id,
            )
            if workspace_base_dir:
                project_rel = scope_workspace_artifact_path(
                    project_rel,
                    workspace_base_dir,
                )

        if operation in {"start", "prepare"}:
            if not project_rel:
                return _json_error(
                    f"{operation} requires project_path",
                    code="project_path_required",
                )
            project_dir = _resolve_inside(entity_root, project_rel)
            return await _start_video_session(
                entity_id=entity_id,
                entity_root=entity_root,
                project_dir=project_dir,
                project_rel=project_rel,
                context=context,
                raw_reference_paths=list(kwargs.get("reference_paths") or []),
                workspace_base_dir=workspace_base_dir,
                legacy_external_authoring=operation == "prepare",
            )

        session_id = str(kwargs.get("session_id") or "").strip()
        try:
            session_state = await _resolve_owned_video_session(
                session_id=session_id,
                entity_id=entity_id,
                context=context,
                project_rel=project_rel,
            )
        except ValueError:
            if session_id or operation not in {"review", "render"} or not project_rel:
                raise
            # Backward compatibility for callers that authored durable source
            # with the old prepare/review/render contract and lost its session.
            project_dir = _resolve_inside(entity_root, project_rel)
            if not project_dir.is_dir():
                raise
            sandbox_id, metrics = await _create_video_session_sandbox(project_dir)
            session_state = new_video_edit_session(
                sandbox_id=sandbox_id,
                entity_id=entity_id,
                user_id=context.user_id,
                conversation_id=context.conversation_id,
                workspace_id=context.workspace_id,
                project_path=project_rel,
                legacy_external_authoring=True,
            )
            append_video_edit_session_event(
                session_state,
                "progress",
                message="Video Sandbox restored for a legacy durable project.",
                data={"sandbox_metrics": metrics},
            )
            await save_video_edit_session(session_state)

        if (
            context.workspace_id
            and str(session_state.get("workspace_id") or "")
            and str(session_state.get("workspace_id")) != str(context.workspace_id)
        ):
            raise PermissionError("Video Edit session belongs to a different Workspace")
        project_rel = str(session_state.get("project_path") or "")
        project_dir = _resolve_inside(entity_root, project_rel)
        if not project_dir.is_dir():
            return _json_error(
                f"Video project directory not found: {project_rel}",
                code="project_not_found",
            )

        if operation == "push_assets":
            return await _push_video_session_assets(
                state=session_state,
                entity_id=entity_id,
                entity_root=entity_root,
                project_dir=project_dir,
                context=context,
                raw_paths=list(kwargs.get("reference_paths") or []),
                workspace_base_dir=workspace_base_dir,
            )
        if operation == "write_files":
            return await _write_video_session_files(
                state=session_state,
                entity_id=entity_id,
                project_dir=project_dir,
                files=kwargs.get("files"),
                delete_paths=list(kwargs.get("delete_paths") or []),
            )
        if operation == "report":
            event_type = str(kwargs.get("event_type") or "progress").strip().lower()
            message = str(kwargs.get("message") or "").strip()
            if event_type not in _VIDEO_SESSION_EVENT_TYPES:
                return _json_error("Unsupported Video Edit event_type")
            if not message:
                return _json_error("report requires message")
            if len(message) > 2000:
                return _json_error("report message exceeds 2,000 characters")
            raw_event_data = kwargs.get("event_data")
            if raw_event_data is not None and not isinstance(raw_event_data, dict):
                return _json_error("report event_data must be an object")
            if len(json.dumps(raw_event_data or {}, ensure_ascii=False)) > 16_000:
                return _json_error("report event_data exceeds 16,000 characters")
            await _ensure_video_session_sandbox(
                session_state,
                project_dir=project_dir,
            )
            event = append_video_edit_session_event(
                session_state,
                event_type,
                message=message,
                data=(dict(raw_event_data or {}) if isinstance(raw_event_data, dict) else None),
            )
            await save_video_edit_session(session_state)
            return _json(
                {
                    "status": "event_reported",
                    "session_id": session_state["session_id"],
                    "project_path": project_rel,
                    "event": event,
                    "event_cursor": session_state["last_event_seq"],
                }
            )
        if operation == "events":
            after_seq = max(0, int(kwargs.get("after_event_seq") or 0))
            await _ensure_video_session_sandbox(
                session_state,
                project_dir=project_dir,
            )
            events = video_edit_session_events(
                session_state,
                after_seq=after_seq,
            )
            await save_video_edit_session(session_state)
            return _json(
                {
                    "status": "events",
                    "session_id": session_state["session_id"],
                    "project_path": project_rel,
                    "events": events,
                    "event_cursor": session_state["last_event_seq"],
                }
            )
        if operation == "status":
            after_seq = max(0, int(kwargs.get("after_event_seq") or 0))
            sandbox_status, recovered = await _ensure_video_session_sandbox(
                session_state,
                project_dir=project_dir,
            )
            return _json(
                {
                    "status": "active",
                    "session_id": session_state["session_id"],
                    "project_path": project_rel,
                    "source_revision": session_state.get("source_revision", 0),
                    "last_synced_sha256": session_state.get("last_synced_sha256", ""),
                    "sandbox": sandbox_status,
                    "recovered": recovered,
                    "events": video_edit_session_events(
                        session_state,
                        after_seq=after_seq,
                    ),
                    "event_cursor": session_state["last_event_seq"],
                }
            )
        if operation == "sync":
            await _ensure_video_session_sandbox(
                session_state,
                project_dir=project_dir,
            )
            sync = await _sync_video_session_to_entity(
                state=session_state,
                project_dir=project_dir,
                entity_id=entity_id,
            )
            return _json(
                {
                    "status": "synchronized",
                    "session_id": session_state["session_id"],
                    "project_path": project_rel,
                    "sync": sync,
                    "event_cursor": session_state["last_event_seq"],
                    "events": [session_state["events"][-1]],
                }
            )
        if operation == "close":
            await _ensure_video_session_sandbox(
                session_state,
                project_dir=project_dir,
            )
            sync = await _sync_video_session_to_entity(
                state=session_state,
                project_dir=project_dir,
                entity_id=entity_id,
            )
            from packages.core.config import get_settings
            from packages.core.services.sandbox_sdk import SandboxClient

            client = SandboxClient(
                base_url=get_settings().SANDBOX_SERVICE_URL.strip(),
                timeout=180.0,
            )
            try:
                await client.destroy(str(session_state["sandbox_id"]))
            finally:
                await client.close()
            await delete_video_edit_session(session_state)
            return _json(
                {
                    "status": "closed",
                    "session_id": session_state["session_id"],
                    "project_path": project_rel,
                    "sync": sync,
                }
            )
        if operation == "review":
            return await _review_project(
                project_dir=project_dir,
                project_rel=project_rel,
                entity_root=entity_root,
                entity_id=entity_id,
                context=context,
                snapshot_times=list(kwargs.get("snapshot_times") or []),
                session_state=session_state,
            )
        return await _render_project(
            project_dir=project_dir,
            project_rel=project_rel,
            entity_root=entity_root,
            entity_id=entity_id,
            context=context,
            review_sha256=str(kwargs.get("review_sha256") or ""),
            output_name=str(kwargs.get("output_name") or "renders/final.mp4"),
            quality=str(kwargs.get("quality") or "high"),
            session_state=session_state,
        )
    except PermissionError as exc:
        return _json_error(str(exc), code="permission_denied")
    except VideoEditRuntimeUnavailable as exc:
        return _json_error(str(exc), code="runtime_unavailable")
    except (FileNotFoundError, ValueError) as exc:
        return _json_error(str(exc))
    except Exception as exc:  # Keep tool failures actionable for the calling agent.
        return _json_error(f"Video editing operation failed: {exc}", code="operation_failed")


def get_tools():
    return [(VIDEO_EDIT_SCHEMA, _video_edit_handler)]
