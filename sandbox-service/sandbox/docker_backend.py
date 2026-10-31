"""
Docker container backend for sandbox execution.

Manages the full lifecycle of a sandbox container:
  create → inject files → install deps → exec commands → destroy

Design mirrors OpenClaw's docker.ts / docker-backend.ts:
- Long-lived container (`sleep infinity`) reused across exec calls
- read-only root + tmpfs for workdir
- cap-drop ALL, no-new-privileges, network isolation
- Config hash tracking for stale container detection
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import hashlib
import json
import logging
import re
import shlex
import os
import posixpath
import subprocess
import time
import uuid
from typing import Optional

from sandbox_config import config as app_config

from .baked_skills import (
    BAKED_INDEX_FILE,
    BAKED_SKILL_MISMATCH_EXIT,
    BAKED_SKILLS_ROOT,
    MATERIALIZE_PROGRAM,
    BakedSkill,
    BakedSkillIndex,
)
from .errors import (
    BakedSkillUnavailable,
    CancelFailed,
    CommandTimeout,
    DependencyInstallFailed,
    DestroyFailed,
    DockerUnavailable,
    ExecutionConflict,
    InvalidRequest,
    LoadSkillFailed,
    RunnerError,
    SandboxBusy,
    SandboxDiskQuotaExceeded,
    SandboxGone,
    SandboxLost,
    SandboxUnavailable,
    bounded_text,
)
from .disk_quota import (
    DiskKill,
    DiskRefusal,
    DiskRoot,
    DiskUsage,
    is_cleanup_command,
    work_area_key,
    workspace_root,
)
from .models import ContainerConfig, ExecResponse, SandboxStatus, SkillManifest
from .security import EVENT_SECRET_TEXT_PATTERNS, EVENT_SENSITIVE_KEYS, validate_skill_name

logger = logging.getLogger(__name__)

# Grace window: if a container was used within this window, don't auto-recreate
# on config mismatch (mirrors OpenClaw's HOT_CONTAINER_WINDOW_MS).
_HOT_WINDOW_SECONDS = 300
_CANCEL_GRACE_SECONDS = 1.0
_CANCEL_POLL_INTERVAL_SECONDS = 0.1
# The launcher writes the pid file right after spawning the command; under
# load ``docker exec`` startup alone can take seconds, so a cancel that arrives
# just after the command started must wait that long before giving up.
_EXECUTION_PID_WAIT_SECONDS = 5.0
# Every claim of the sandbox (a command, a skill load, a destroy) records a
# deadline: its own timeout plus this margin (cancel grace, docker startup and
# teardown). A sandbox still busy past its deadline is stuck, and the runner's
# reaper reclaims it.
BUSY_MARGIN_SECONDS = 120
_FILE_INJECT_TIMEOUT = 60
_DESTROY_TIMEOUT = 120
_EXECUTION_EVENT_BYTES_LIMIT = 128 * 1024
_EXECUTION_RESPONSE_BYTES_LIMIT = 16 * 1024

_SANDBOX_BRIDGE_SOURCE = r'''#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
import time
import uuid

EVENT_TYPES = {
    "progress", "warning", "error", "need_input", "need_file",
    "need_tool", "need_credential", "result",
}
SECRET_KEYS = __EVENT_SENSITIVE_KEYS__
SECRET_TEXT_PATTERNS = tuple(
    re.compile(pattern) for pattern in __EVENT_SECRET_TEXT_PATTERNS__
)
MAX_EVENT_BYTES = 8192
MAX_STREAM_BYTES = 128 * 1024


def _response_path(event_id):
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:32]
    return os.path.join(os.environ["MANOR_SANDBOX_RESPONSE_DIR"], digest + ".json")


def _normalize_key(key):
    value = str(key or "").strip().replace("-", "_")
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return value.casefold()


def _has_secret_key(value):
    if isinstance(value, dict):
        return any(_normalize_key(key) in SECRET_KEYS or _has_secret_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_secret_key(item) for item in value)
    return False


def _has_secret_text(value):
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in SECRET_TEXT_PATTERNS)
    if isinstance(value, dict):
        return any(_has_secret_text(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_secret_text(item) for item in value)
    return False


def emit(args):
    if args.type not in EVENT_TYPES:
        raise SystemExit("unsupported event type")
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid payload JSON: {exc}")
    if not isinstance(payload, dict):
        raise SystemExit("payload must be a JSON object")
    if _has_secret_key(payload) or _has_secret_text({"message": args.message, "payload": payload}):
        raise SystemExit("sandbox events accept references only, not secret values")
    event_id = args.event_id or uuid.uuid4().hex
    if len(event_id) > 128 or not event_id or any(not (char.isalnum() or char in "_.:-") for char in event_id):
        raise SystemExit("invalid event id")
    event = {
        "event_id": event_id,
        "type": args.type,
        "message": args.message[:2000],
        "payload": payload,
        "requires_response": bool(args.requires_response or args.type.startswith("need_")),
    }
    encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_EVENT_BYTES:
        raise SystemExit("event exceeds 8192 bytes")
    event_file = os.environ["MANOR_SANDBOX_EVENT_FILE"]
    try:
        current_size = os.path.getsize(event_file)
    except FileNotFoundError:
        current_size = 0
    if current_size + len(encoded) > MAX_STREAM_BYTES:
        raise SystemExit("execution event stream exceeds 131072 bytes")
    fd = os.open(event_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)
    print(event_id)


def wait(args):
    path = _response_path(args.event_id)
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                print(handle.read())
            return
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise SystemExit("timed out waiting for Agent response")
            time.sleep(0.1)


parser = argparse.ArgumentParser(description="Sandbox-to-Agent event bridge")
subparsers = parser.add_subparsers(dest="action", required=True)
emit_parser = subparsers.add_parser("emit")
emit_parser.add_argument("--type", required=True)
emit_parser.add_argument("--message", default="")
emit_parser.add_argument("--payload", default="{}")
emit_parser.add_argument("--event-id")
emit_parser.add_argument("--requires-response", action="store_true")
emit_parser.set_defaults(handler=emit)
wait_parser = subparsers.add_parser("wait")
wait_parser.add_argument("--event-id", required=True)
wait_parser.add_argument("--timeout", type=float, default=300.0)
wait_parser.set_defaults(handler=wait)
arguments = parser.parse_args()
arguments.handler(arguments)
'''

_SANDBOX_BRIDGE_SOURCE = _SANDBOX_BRIDGE_SOURCE.replace(
    "__EVENT_SENSITIVE_KEYS__",
    repr(tuple(sorted(EVENT_SENSITIVE_KEYS))),
).replace(
    "__EVENT_SECRET_TEXT_PATTERNS__",
    repr(EVENT_SECRET_TEXT_PATTERNS),
)


def _redact_docker_args(args: list[str]) -> list[str]:
    """Hide env values (``--env KEY=VALUE``) before an argv reaches a log or an error body."""
    redacted: list[str] = []
    hide_next = False
    for item in args:
        if hide_next:
            key = item.split("=", 1)[0]
            redacted.append(f"{key}=<redacted>")
            hide_next = False
            continue
        if item in ("--env", "-e"):
            hide_next = True
        redacted.append(item)
    return redacted


class DockerCommandTimeout(CommandTimeout):
    """The docker CLI did not return within its timeout.

    Carries whatever output the command produced before it was stopped. The
    message never includes the docker argv: it can hold the exec launcher
    source and paths that mean nothing to a caller.
    """

    code = "docker_timeout"

    def __init__(self, timeout: float, stdout: bytes | None, stderr: bytes | None) -> None:
        super().__init__(f"Docker command timed out after {timeout}s")
        self.timeout = timeout
        self.stdout = stdout or b""
        self.stderr = stderr or b""


# Text the docker CLI prints when the failure is Docker's, not the command's.
_DAEMON_UNREACHABLE_MARKERS = (
    "cannot connect to the docker daemon",
    "error during connect",
    "is the docker daemon running",
)
_CONTAINER_FAILURE_MARKERS = (
    "error response from daemon",
    "no such container",
    "no such object",
    "is not running",
    "is restarting",
    "is paused",
    *_DAEMON_UNREACHABLE_MARKERS,
)
# ``docker exec`` reserves 125-127 for its own failures, and a container that
# dies under an attached exec surfaces as a signal exit (>=128). Any of these
# can be the container's death rather than the command's own result.
_DOCKER_LEVEL_EXIT_CODE = 125


def _printable(text: str, limit: int = 500) -> str:
    normalized = " ".join(
        "".join(character if character.isprintable() else " " for character in text).split()
    )
    if len(normalized) > limit:
        normalized = f"{normalized[: limit - 3]}..."
    return normalized


def _is_daemon_unreachable(stderr: str) -> bool:
    lowered = stderr.casefold()
    return any(marker in lowered for marker in _DAEMON_UNREACHABLE_MARKERS)


def _decode_output(result: subprocess.CompletedProcess[bytes]) -> tuple[str, str]:
    """Decode a docker exec's stdout/stderr. Blocking for large output: run it in a thread."""
    return (
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


def _exec_response(
    result: subprocess.CompletedProcess[bytes], execution_id: str | None = None
) -> ExecResponse:
    """Decode a docker exec result. Blocking for large output: run it in a thread."""
    stdout, stderr = _decode_output(result)
    return ExecResponse(
        stdout=stdout,
        stderr=stderr,
        exit_code=result.returncode,
        execution_id=execution_id,
    )


def _may_be_container_failure(result: subprocess.CompletedProcess[bytes]) -> bool:
    """Whether a failed ``docker exec`` might be Docker's failure, not the command's.

    Only a hint: the command's own stderr can contain the same words, so the
    caller confirms with an inspect before treating the sandbox as lost.
    """
    if result.returncode == 0:
        return False
    if result.returncode >= _DOCKER_LEVEL_EXIT_CODE:
        return True
    stderr = (result.stderr or b"").decode("utf-8", errors="replace").casefold()
    return any(marker in stderr for marker in _CONTAINER_FAILURE_MARKERS)


def _run_docker(
    args: list[str],
    *,
    input_data: bytes | None = None,
    timeout: float = 120,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Run a docker CLI command synchronously (callers run it off the event loop)."""
    try:
        result = subprocess.run(
            args,
            input=input_data,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise DockerUnavailable(
            'Sandbox requires Docker, but the "docker" command was not found. '
            "Install Docker and ensure it is on PATH."
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "Docker command timed out after %ss: %s",
            timeout,
            _printable(" ".join(_redact_docker_args(args)), 300),
        )
        raise DockerCommandTimeout(timeout, exc.stdout, exc.stderr) from None

    if result.returncode != 0 and not allow_failure:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if _is_daemon_unreachable(stderr):
            raise DockerUnavailable(
                f"Docker command failed (exit {result.returncode}): {_printable(stderr)}"
            )
        raise RuntimeError(f"Docker command failed (exit {result.returncode}): {stderr}")
    return result


def _container_state(name: str, *, timeout: float = 30) -> dict[str, bool]:
    result = _run_docker(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        timeout=timeout,
        allow_failure=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        lowered = _printable(stderr, 10_000).casefold()
        if "no such object" in lowered or "no such container" in lowered:
            return {"exists": False, "running": False}
        detail = _printable(stderr) or "no error details"
        message = (
            f"Docker inspect failed for container {name!r} "
            f"(exit {result.returncode}): {detail}"
        )
        if _is_daemon_unreachable(stderr):
            raise DockerUnavailable(message)
        raise RuntimeError(message)
    running = result.stdout.decode().strip() == "true"
    return {"exists": True, "running": running}


def _image_exists_locally(image: str, timeout: float = 120) -> bool:
    """Check the local Docker image cache without triggering a pull."""
    result = _run_docker(
        ["docker", "image", "inspect", image],
        timeout=timeout,
        allow_failure=True,
    )
    return result.returncode == 0


def write_skill_files(tmp_dir: str, files: dict[str, str]) -> None:
    """Write in-memory skill files under ``tmp_dir``, skipping unsafe paths.

    Blocking filesystem work: callers run it in a worker thread.
    """
    from pathlib import Path

    for rel_path, content in files.items():
        safe_rel = rel_path.replace("\\", "/").strip().lstrip("/")
        if not safe_rel or ".." in safe_rel:
            continue
        full_path = Path(tmp_dir) / safe_rel
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")


def docker_host_resources(timeout: float = 10) -> tuple[int, float] | None:
    """``(MemTotal bytes, NCPU)`` of the host the Docker daemon runs containers on.

    The runner reaches the daemon over its socket (and itself runs in a
    container), so the daemon's host is where sandboxes live; the runner's
    own cgroup says nothing about them. None when the daemon cannot say.
    """
    try:
        result = _run_docker(
            ["docker", "info", "--format", "{{.MemTotal}} {{.NCPU}}"],
            timeout=timeout,
            allow_failure=True,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        memory, cpus = result.stdout.decode("utf-8", errors="replace").split()
        return int(memory), float(cpus)
    except ValueError:
        return None


def _missing_image_message(image: str) -> str:
    return (
        f"Sandbox base image '{image}' is not available locally. "
        "Skill execution cannot start until the image exists on the Docker host. "
        f"Build it with: docker build -t {image} -f docker/Dockerfile.sandbox . "
        "Or set SANDBOX_IMAGE to an existing sandbox runtime image."
    )


def image_id(image: str, timeout: float = 120) -> str | None:
    """The local image's id, or None when it is not present (never pulls)."""
    result = _run_docker(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        timeout=timeout,
        allow_failure=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace").strip() or None


def read_baked_skill_index(image: str, timeout: float = 120) -> BakedSkillIndex:
    """Read the image's baked Skills index (an image without one has none).

    One short-lived, network-less container per call; the runner calls it once
    per image id.
    """
    result = _run_docker(
        [
            "docker", "run", "--rm", "--pull=never", "--network", "none",
            "--entrypoint", "cat", image, f"{BAKED_SKILLS_ROOT}/{BAKED_INDEX_FILE}",
        ],
        timeout=timeout,
        allow_failure=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if "No such file" in stderr:
            return BakedSkillIndex()
        raise RuntimeError(f"Reading the baked Skills index of {image} failed: {_printable(stderr)}")
    return BakedSkillIndex.parse(result.stdout.decode("utf-8"))


def load_baked_skill_index(
    image: str,
    previous: tuple[str, BakedSkillIndex] | None,
    timeout: float = 120,
) -> tuple[str, BakedSkillIndex] | None:
    """``(image id, index)`` for ``image``, reusing ``previous`` for the same id.

    None when the image is not present. Reading the index starts a container,
    so it happens once per image id, not on every health probe.
    """
    current = image_id(image, timeout=timeout)
    if current is None:
        return None
    if previous is not None and previous[0] == current:
        return previous
    return current, read_baked_skill_index(image, timeout=timeout)


def list_managed_sandbox_containers(service_scope: str = "1") -> list[str]:
    """List only containers belonging to this sandbox service deployment."""
    result = _run_docker(
        ["docker", "ps", "-aq", "--filter", f"label=sandbox.service={service_scope}"],
        allow_failure=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Failed to list managed sandbox containers: {stderr}")
    container_ids: list[str] = []
    for line in result.stdout.decode().splitlines():
        if container_id := line.strip():
            container_ids.append(container_id)
    return container_ids


def list_running_managed_container_names(service_scope: str = "1") -> set[str]:
    """Names of this deployment's containers that Docker reports as running."""
    result = _run_docker(
        [
            "docker", "ps",
            "--filter", f"label=sandbox.service={service_scope}",
            "--filter", "status=running",
            "--format", "{{.Names}}",
        ],
        timeout=30,
        allow_failure=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Failed to list running sandbox containers: {_printable(stderr)}")
    return {line.strip() for line in result.stdout.decode().splitlines() if line.strip()}


def remove_managed_sandbox_container(container_id: str) -> None:
    """Force-remove one managed container discovered during reconciliation."""
    result = _run_docker(
        ["docker", "rm", "-f", "-v", container_id],
        allow_failure=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Failed to remove managed sandbox container: {stderr}")


# ── Disk-backed local install dir ─────────────────────────────────────────
# Every container gets an anonymous Docker volume at SANDBOX_LOCAL_DIR, where
# the image points runtime installs and caches (PYTHONUSERBASE, npm prefix and
# cache; see docker/Dockerfile.sandbox). It lives on the Docker host's disk, so
# `pip install` / `npm install -g` no longer fill the memory-backed tmpfs that
# counts against the container's memory limit. It is a runner default, never a
# caller override.
#
# Cleanup: an anonymous volume belongs to its container. Every removal here is
# `docker rm -f -v`, which deletes it with the container (destroy, reap, lost
# reclaim, failed create, stale-container replacement, startup reconcile). A
# container removed some other way leaves the volume dangling; it carries the
# deployment's `sandbox.service` label, and the reaper deletes dangling
# volumes with that label every tick. The local volume driver cannot cap a
# volume's size, so the dir is one of the disks the runner's disk watchdog
# bounds (``DockerSandbox.disk_roots``, policy in ``sandbox.disk_quota``): over
# SANDBOX_LOCAL_DISK_LIMIT_MB the commands writing into it are stopped and
# their work areas refused, and the container is recycled only once nothing
# runs or waits in it and the dir is still over.
SANDBOX_LOCAL_DIR = "/opt/sandbox-local"


def _local_disk_mount(service_scope: str, sandbox_id: str) -> str:
    return (
        f"type=volume,dst={SANDBOX_LOCAL_DIR},"
        f"volume-label=sandbox.service={service_scope},"
        f"volume-label=sandbox.id={sandbox_id}"
    )


def remove_dangling_sandbox_volumes(service_scope: str = "1") -> int:
    """Delete this deployment's local-disk volumes no container references."""
    result = _run_docker(
        [
            "docker", "volume", "ls", "-q",
            "--filter", "dangling=true",
            "--filter", f"label=sandbox.service={service_scope}",
        ],
        timeout=30,
        allow_failure=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Failed to list dangling sandbox volumes: {_printable(stderr)}")
    removed = 0
    for volume in result.stdout.decode().split():
        # A volume a new container claimed in between is "in use": skip it.
        rm = _run_docker(["docker", "volume", "rm", volume], timeout=60, allow_failure=True)
        if rm.returncode == 0:
            removed += 1
    return removed


def _tar_directory(directory: str, entries: list[str]) -> bytes:
    """Archive skill files on the host so they can be piped into the container."""
    result = subprocess.run(
        ["tar", "cf", "-", "-C", directory, *entries],
        capture_output=True,
        timeout=_FILE_INJECT_TIMEOUT,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Packing skill files failed (exit {result.returncode}): {stderr}")
    return result.stdout


def _tar_skill_directory(directory: str, entries: list[str], slot: str) -> bytes:
    """Archive a Skill's files for ``/skills/<slot>``, mapping its /skill paths there.

    A Skill written for a single ``/skill`` directory (scripts with a
    hard-coded ``/skill/...`` path, docs that say ``cd /skill``) keeps
    working next to other Skills: every UTF-8 text file has its ``/skill``
    path roots rewritten to the Skill's own directory. Binary files are
    copied unchanged.
    """
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for entry in entries:
            base = os.path.join(directory, entry)
            paths = [base]
            if os.path.isdir(base) and not os.path.islink(base):
                paths = [base]
                for root, dirs, files in os.walk(base):
                    dirs.sort()
                    paths.extend(os.path.join(root, name) for name in sorted(dirs + files))
            for path in paths:
                arcname = os.path.relpath(path, directory)
                info = archive.gettarinfo(path, arcname=arcname)
                if not info.isfile():
                    archive.addfile(info)
                    continue
                with open(path, "rb") as handle:
                    data = handle.read()
                if b"/skill" in data:
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                    else:
                        data = rewrite_skill_paths(text, slot).encode("utf-8")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _config_hash(cfg: ContainerConfig, skill_dir: str) -> str:
    """Deterministic hash of container config + skill directory for staleness check."""
    payload = (
        f"{cfg.image}|{cfg.network}|{cfg.memory}|{cfg.cpus}|{cfg.pids_limit}"
        f"|{cfg.read_only_root}|{cfg.workdir}|{cfg.workdir_tmpfs_size}|{skill_dir}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


# The sandbox image preinstalls the dependencies of every Skill shipped in the
# repository (docker/Dockerfile.sandbox, skill-manifests stage), keyed by
# ``preinstalled_manifest_digest`` of the manifest bytes:
#   python/<digest>.txt             its packages are in system site-packages
#   node/<digest>/node_modules      a read-only tree linked into the workdir
# Installing at create time instead puts hundreds of MB into the workdir and
# home tmpfs, which count against the container's memory limit.
PREINSTALLED_SKILL_DEPS_ROOT = "/opt/manor/skill-deps"


def preinstalled_manifest_digest(content: bytes) -> str:
    """Key of a dependency manifest in the image (must match the Dockerfile)."""
    return hashlib.sha256(content).hexdigest()[:16]


def _host_manifest_digest(skill_dir: str, relative_path: str) -> str | None:
    try:
        with open(os.path.join(skill_dir, relative_path), "rb") as handle:
            return preinstalled_manifest_digest(handle.read())
    except OSError:
        return None


_SKILL_ENV_FILE_PREFIX = "sbx-env-"
# Env-file variable listing the keys whose values are base64-encoded (multi-line).
_B64_ENV_KEYS_VAR = "MANOR_SANDBOX_B64_ENV"
_SKILL_ENV_FILE_SUFFIX = ".env"
# A secret file older than this is an orphan (an exec never runs that long
# without the service noticing); the startup sweep removes it.
_SKILL_ENV_FILE_STALE_SECONDS = 24 * 3600


def _remove_skill_env_file(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("Could not remove sandbox env file %s", path, exc_info=True)


def sweep_stale_skill_env_files(*, max_age_seconds: int = _SKILL_ENV_FILE_STALE_SECONDS) -> int:
    """Remove leftover per-exec secret files (a SIGKILL mid-exec leaves them)."""
    import glob
    import tempfile

    removed = 0
    cutoff = time.time() - max_age_seconds
    pattern = os.path.join(tempfile.gettempdir(), f"{_SKILL_ENV_FILE_PREFIX}*{_SKILL_ENV_FILE_SUFFIX}")
    for path in glob.glob(pattern):
        try:
            if os.stat(path).st_mtime <= cutoff:
                os.unlink(path)
                removed += 1
        except FileNotFoundError:
            continue
        except Exception:
            logger.warning("Could not sweep sandbox env file %s", path, exc_info=True)
    return removed


SHARED_WORKSPACE_ROOT = "/workspace"
# Skills live side by side, one directory each: /skills/<slot>. A Skill's own
# commands, files and docs keep addressing it as /skill (SKILL_VIEW): the
# runner maps that view per request (``rewrite_skill_paths``), so several
# Skills coexist in one container without swapping a single /skill directory
# under each other's running commands.
SKILLS_ROOT = "/skills"
SKILL_VIEW = "/skill"
# Runner-owned control files (watchdog pid, kill verdicts): a tiny tmpfs of
# its own, so a command that fills /tmp cannot hide why it was killed.
CONTROL_DIR = "/manor-control"
# Memory a command needs to be worth a slot: office renders and headless
# browsers peak around 200-400 MiB, so 256 MiB per concurrent command.
_MIB_PER_SLOT = 256
# pids a command needs to be worth a slot (a shell, an interpreter and its
# workers; LibreOffice/Chromium spawn ~10-30).
_PIDS_PER_SLOT = 32
# Marker the exec launcher prints (last stderr line) when its command died by
# a signal: how it died, so the runner can say "out of memory" instead of 137.
EXEC_STATUS_SENTINEL = "__MANOR_EXEC_STATUS__ "
_SKILL_VIEW_RE = re.compile(r"(?<![A-Za-z0-9_.~/-])/skill(?![A-Za-z0-9_.-])")


def _is_shared_workspace_path(path: str | None) -> bool:
    """True for a normalised directory strictly inside the shared /workspace."""

    if not path or not path.startswith(SHARED_WORKSPACE_ROOT + "/"):
        return False
    parts = path.split("/")
    return ".." not in parts and "" not in parts[1:]


def skill_slot(skill_name: str | None) -> str:
    """The directory name a Skill occupies under /skills (path-safe, stable)."""

    raw = str(skill_name or "").strip().lower()
    safe = "".join(char if char.isalnum() or char in "-_." else "-" for char in raw).strip(".-")
    return safe[:64] or "skill"


def skill_dir(slot: str) -> str:
    return f"{SKILLS_ROOT}/{slot}"


def rewrite_skill_paths(text: str, slot: str | None) -> str:
    """Map the /skill view onto one Skill's own directory.

    Only a path root is rewritten: ``/skill`` alone or followed by ``/``,
    whitespace, a quote or shell punctuation -- never ``/skills``,
    ``/skill-x``, ``/skill.md`` or ``foo/skill``.
    """

    if not slot or not text or SKILL_VIEW not in text:
        return text
    return _SKILL_VIEW_RE.sub(skill_dir(slot), text)


def _memory_mib(value: str) -> int:
    """Docker memory string ("512m", "2g", "1073741824") in MiB; 0 when unknown."""

    text = value.strip().lower()
    if not text:
        return 0
    units = {"b": 1 / (1024 * 1024), "k": 1 / 1024, "m": 1, "g": 1024, "t": 1024 * 1024}
    unit = text[-1] if text[-1] in units else "b"
    number = text[:-1] if text[-1] in units else text
    try:
        return int(float(number) * units[unit])
    except ValueError:
        return 0


def _strip_exec_status(stderr: str) -> tuple[str, dict | None]:
    """Split the launcher's status line off a command's stderr."""

    index = stderr.rfind(EXEC_STATUS_SENTINEL)
    if index < 0:
        return stderr, None
    line_end = stderr.find("\n", index)
    raw = stderr[index + len(EXEC_STATUS_SENTINEL): line_end if line_end >= 0 else len(stderr)]
    head = stderr[:index]
    if head.endswith("\n"):
        head = head[:-1]
    try:
        status = json.loads(raw)
    except (TypeError, ValueError):
        return head, None
    return head, status if isinstance(status, dict) else None


def exec_outcome(
    exit_code: int,
    status: dict | None,
    *,
    memory: str = "",
    pids_limit: int = 0,
) -> tuple[str | None, str | None]:
    """Why the runner (or the kernel on its behalf) killed a command, if it did.

    A watchdog verdict names the command it killed (pids, disk). Otherwise a
    SIGKILL while the container's ``memory.events`` ``oom_kill`` counter moved
    during the command's lifetime is the kernel OOM killer: in a shared
    container it picks the largest user process -- the one that exceeded
    memory -- and leaves the others (and the container) running.
    """

    status = status or {}
    verdict = status.get("verdict") if isinstance(status.get("verdict"), dict) else None
    if verdict:
        reason = str(verdict.get("reason") or "")
        if reason == "pids":
            return "pids_killed", (
                "killed: the command's processes exhausted the sandbox's process limit "
                f"({verdict.get('count')} of {verdict.get('limit') or pids_limit} pids; a runaway "
                "fork loop?). Its whole process group was stopped; other commands kept running."
            )
        if reason == "disk":
            root = str(verdict.get("root") or SHARED_WORKSPACE_ROOT)
            limit = int(verdict.get("limit") or 0) // (1024 * 1024)
            used = int(verdict.get("used") or verdict.get("bytes") or 0) // (1024 * 1024)
            held = int(verdict.get("bytes") or 0) // (1024 * 1024)
            where = (
                f"its work area {verdict.get('area')} held {held} MB while this user's shared {root} "
                f"was full ({used} MB used{f', limit {limit} MB' if limit else ''})"
                if verdict.get("area")
                else f"it was writing to {root}, which held {used} MB, over its {limit} MB limit"
            )
            who = "this work area's commands" if verdict.get("area") else "the commands writing there"
            return "disk_killed", (
                f"killed: {where}. Only {who} were stopped; other work in the "
                "sandbox kept running. New commands of this work area are refused until space is "
                "freed: delete what you do not need (rm -rf ...), or save results with save_result."
            )
    signal_number = status.get("signal")
    oom_delta = status.get("oom_kill_delta")
    if (signal_number == 9 or exit_code in (137, -9)) and isinstance(oom_delta, int) and oom_delta > 0:
        return "oom_killed", (
            f"killed: out of memory (the sandbox's memory limit is {memory or 'unknown'}). "
            "Only this command was stopped; the sandbox and other commands kept running. "
            "Process less data at once, or stream it."
        )
    return None, None


# The exec launcher: every command runs under it (``python -c``), so the runner
# can always find, cancel and explain the command.
#
# * the command gets its own session/process group, records its pid, and
#   starts in its cwd (created on demand under the shared /workspace);
# * MANOR_EXEC_ID tags every process of the command (inherited env), so the
#   watchdog attributes processes to commands even after they re-parent;
# * oom_score_adj=1000 for the command only: under memory pressure the
#   kernel kills the largest *user* process, never the launcher, the
#   watchdog or the container's init;
# * when the command dies by a signal, a last stderr line reports the signal,
#   how far the container's oom_kill counter moved meanwhile, and a watchdog
#   verdict naming this command, if any (the runner strips it).
_EXEC_LAUNCHER_SOURCE = """
import json
import os
import shutil
import subprocess
import sys

command, pid_file, event_file, response_dir, bridge_file, bridge_source, execution_id, interactive = sys.argv[1:9]
cwd = sys.argv[9] if len(sys.argv) > 9 else ""
create_cwd = len(sys.argv) > 10 and sys.argv[10] == "1"
verdict_file = sys.argv[11] if len(sys.argv) > 11 else ""
skill_home = sys.argv[12] if len(sys.argv) > 12 else ""
if cwd and create_cwd:
    try:
        os.makedirs(cwd, exist_ok=True)
    except OSError as exc:
        sys.stderr.write("cannot create working directory " + cwd + ": " + str(exc) + chr(10))
        raise SystemExit(2)
_b64_keys = os.environ.pop("MANOR_SANDBOX_B64_ENV", "")
if _b64_keys:
    import base64 as _b64
    for _k in [_x for _x in _b64_keys.split(",") if _x]:
        if _k in os.environ:
            os.environ[_k] = _b64.b64decode(os.environ[_k]).decode("utf-8")
for path in (pid_file, event_file, bridge_file):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
shutil.rmtree(response_dir, ignore_errors=True)
env = dict(os.environ)
env["MANOR_EXEC_ID"] = execution_id
if skill_home:
    env["SKILL_DIR"] = skill_home
if interactive == "1":
    os.makedirs(response_dir, mode=0o700, exist_ok=True)
    fd = os.open(bridge_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    try:
        os.write(fd, bridge_source.encode("utf-8"))
    finally:
        os.close(fd)
    env.update({
        "MANOR_EXECUTION_ID": execution_id,
        "MANOR_SANDBOX_EVENT_FILE": event_file,
        "MANOR_SANDBOX_RESPONSE_DIR": response_dir,
        "MANOR_SANDBOX_BRIDGE": bridge_file,
    })


def _oom_kills():
    try:
        with open("/sys/fs/cgroup/memory.events") as handle:
            for line in handle:
                key, _, value = line.partition(" ")
                if key == "oom_kill":
                    return int(value)
    except (OSError, ValueError):
        return None
    return None


def _prefer_me_for_oom():
    try:
        with open("/proc/self/oom_score_adj", "w") as handle:
            handle.write("1000")
    except OSError:
        pass


before = _oom_kills()
if verdict_file:
    try:
        os.unlink(verdict_file)
    except OSError:
        pass
proc = subprocess.Popen(
    ["sh", "-c", command], start_new_session=True, env=env, cwd=cwd or None,
    preexec_fn=_prefer_me_for_oom,
)
fd = os.open(pid_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
try:
    os.write(fd, str(proc.pid).encode("ascii"))
finally:
    os.close(fd)
code = proc.wait()
# A signal death: the command itself (code < 0) or a child its shell reports
# as 128 + signal.
if code < 0 or code > 128:
    after = _oom_kills()
    verdict = None
    if verdict_file:
        try:
            with open(verdict_file) as handle:
                verdict = json.load(handle)
        except (OSError, ValueError):
            verdict = None
    status = {
        "signal": -code if code < 0 else code - 128,
        "oom_kill_delta": (after - before) if before is not None and after is not None else None,
        "verdict": verdict,
    }
    sys.stderr.flush()
    sys.stderr.write(chr(10) + "__MANOR_EXEC_STATUS__ " + json.dumps(status) + chr(10))
    sys.stderr.flush()
raise SystemExit(code if code >= 0 else 128 - code)
"""

# In-container helpers shared by the watchdog and by the disk probe and disk
# kill below: which processes belong to which command, and how a command is
# stopped with a verdict its launcher reports. A script using them defines
# ``me`` (its own pid) and ``verdicts`` (the verdict directory).
_EXEC_PROCESS_HELPERS = """
import hashlib
import json
import os
import signal
import sys
import time


def session_of(pid):
    try:
        with open("/proc/" + str(pid) + "/stat") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
        return int(fields[3])  # session id (field 6 of stat)
    except (OSError, ValueError, IndexError):
        return None


def digest_of(execution_id):
    return hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:24]


def command_leaders():
    # The launcher records each command's leader pid in
    # /tmp/manor-exec-<digest>.pid; the leader starts its own session, so the
    # session id names the command even for a process that cleared its env.
    leaders = {}
    try:
        names = os.listdir("/tmp")
    except OSError:
        return leaders
    for name in names:
        if not (name.startswith("manor-exec-") and name.endswith(".pid")):
            continue
        try:
            with open("/tmp/" + name) as handle:
                leaders[int(handle.read().strip())] = name[len("manor-exec-"):-len(".pid")]
        except (OSError, ValueError):
            continue
    return leaders


def exec_processes():
    # Each command's processes, keyed by the digest of its execution id: by
    # the MANOR_EXEC_ID they inherit, else (env cleared) by their session.
    leaders = command_leaders()
    groups = {}
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        pid = int(name)
        try:
            with open("/proc/" + name + "/environ", "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        digest = None
        for item in data.split(b"\\0"):
            if item.startswith(b"MANOR_EXEC_ID="):
                digest = digest_of(item[14:].decode("utf-8", "replace"))
                break
        if digest is None:
            digest = leaders.get(session_of(pid))
        if digest is not None:
            groups.setdefault(digest, []).append(pid)
    return groups


def write_verdict(digest, verdict):
    path = os.path.join(verdicts, digest + ".json")
    try:
        os.makedirs(verdicts, exist_ok=True)
        with open(path + ".tmp", "w") as handle:
            json.dump(verdict, handle)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def kill_command(digest, pids):
    for _ in range(50):
        for sig in (signal.SIGSTOP, signal.SIGKILL):
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
        time.sleep(0.02)
        pids = exec_processes().get(digest, [])
        if not pids:
            return
"""

# The watchdog stops one command from burning out a shared sandbox's pids.
# It runs detached inside the container from creation on (no fork needed to
# act): when the container nears its pids limit, the command (MANOR_EXEC_ID)
# holding the most processes is frozen and killed -- the whole group, until
# none is left -- and a verdict names it; other commands keep forking.
# Memory needs no watchdog: the kernel OOM killer stops the largest user
# process (oom_score_adj, above) and the launcher reports it. Disk is the
# runner's: ``sandbox.disk_quota`` decides (one policy for every disk), the
# disk probe below measures and the disk kill acts.
_WATCHDOG_PIDS_FRACTION = 0.85
_WATCHDOG_SOURCE = _EXEC_PROCESS_HELPERS + """

control, pids_fraction = sys.argv[1], float(sys.argv[2])
verdicts = os.path.join(control, "verdicts")
os.makedirs(verdicts, exist_ok=True)
pid_path = os.path.join(control, "watchdog.pid")
try:
    with open(pid_path) as handle:
        other = int(handle.read().strip() or 0)
    if other > 1 and other != os.getpid():
        os.kill(other, 0)
        raise SystemExit(0)  # one watchdog per container
except (OSError, ValueError):
    pass
with open(pid_path, "w") as handle:
    handle.write(str(os.getpid()))
me = os.getpid()


def read_int(path):
    try:
        with open(path) as handle:
            text = handle.read().strip()
        return None if text == "max" else int(text)
    except (OSError, ValueError):
        return None


def task_count(pid):
    try:
        return max(1, len(os.listdir("/proc/" + str(pid) + "/task")))
    except OSError:
        return 1


while True:
    try:
        limit = read_int("/sys/fs/cgroup/pids.max")
        current = read_int("/sys/fs/cgroup/pids.current")
        if limit and current is not None and current >= pids_fraction * limit:
            groups = exec_processes()
            if groups:
                # pids.max counts tasks (threads too), so weigh each command by its tasks.
                weights = {key: sum(task_count(pid) for pid in pids) for key, pids in groups.items()}
                digest = max(weights, key=weights.get)
                if weights[digest] >= max(2, limit // 4):
                    write_verdict(
                        digest,
                        {"reason": "pids", "count": weights[digest], "limit": limit},
                    )
                    kill_command(digest, groups[digest])
    except Exception:
        pass
    time.sleep(0.1)
"""

# The disk probe (one ``docker exec``, reads only): the usage of each requested
# root; bytes per work area once the root is at its threshold; and for each
# command (execution digest) the directories its processes work in and the
# shared roots it has files open under. Prints one JSON object.
_DISK_PROBE_SOURCE = _EXEC_PROCESS_HELPERS + """

me = os.getpid()
verdicts = ""
spec = json.loads(sys.argv[1])


def under(path, root):
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def tree_bytes(path):
    total = 0
    try:
        device = os.lstat(path).st_dev
    except OSError:
        return 0
    for current, dirs, files in os.walk(path):
        kept = []
        for name in dirs:
            try:
                if os.lstat(os.path.join(current, name)).st_dev == device:
                    kept.append(name)
            except OSError:
                pass
        dirs[:] = kept
        for name in files:
            try:
                total += os.lstat(os.path.join(current, name)).st_blocks * 512
            except OSError:
                pass
    return total


# A command "writes" a root when it has files open under it outside the
# root's work areas (an area's bytes are charged to the area) -- or, for a
# root without areas, works in it.
writer_roots = [(root["path"], root.get("areas_dir") or "") for root in spec if root.get("writers")]


def writes(path, root, areas_dir):
    return under(path, root) and not (areas_dir and under(path, areas_dir))


commands = {}
for digest, pids in exec_processes().items():
    cwds, opened = set(), set()
    for pid in pids:
        try:
            cwds.add(os.readlink("/proc/" + str(pid) + "/cwd"))
        except OSError:
            pass
        if not writer_roots:
            continue
        try:
            fds = os.listdir("/proc/" + str(pid) + "/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink("/proc/" + str(pid) + "/fd/" + fd)
            except OSError:
                continue
            for root, areas_dir in writer_roots:
                if writes(target, root, areas_dir):
                    opened.add(root)
    for root, areas_dir in writer_roots:
        if not areas_dir and any(under(cwd, root) for cwd in cwds):
            opened.add(root)
    commands[digest] = {"cwd": sorted(cwds), "open": sorted(opened)}

roots = []
for root in spec:
    path = root["path"]
    if not os.path.isdir(path):
        continue
    if root.get("filesystem"):
        stat = os.statvfs(path)
        used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
    else:
        used = tree_bytes(path)
    areas = {}
    areas_dir = root.get("areas_dir")
    if areas_dir and used >= int(root.get("areas_from") or 0) and os.path.isdir(areas_dir):
        for entry in os.listdir(areas_dir):
            full = os.path.join(areas_dir, entry)
            if os.path.isdir(full) and not os.path.islink(full):
                areas[entry] = tree_bytes(full)
    roots.append({"path": path, "used": used, "areas": areas})
print(json.dumps({"roots": roots, "commands": commands}))
"""

# The disk kill (one ``docker exec``): for each command, the verdict its
# launcher reports (``disk_killed``), then every one of its processes stopped.
_DISK_KILL_SOURCE = _EXEC_PROCESS_HELPERS + """

me = os.getpid()
verdicts = sys.argv[1]
for item in json.loads(sys.argv[2]):
    write_verdict(item["digest"], item["verdict"])
    kill_command(item["digest"], exec_processes().get(item["digest"], []))
"""
_DISK_DOCKER_TIMEOUT_SECONDS = 30


@dataclasses.dataclass
class _SlotWaiter:
    """A command waiting for one of the sandbox's execution slots."""

    execution_id: str
    command: str
    timeout: float
    fair_key: str
    slot: str | None
    future: asyncio.Future
    enqueued_at: float
    # Its work area (``disk_quota.work_area_key`` of the caller's fair_key).
    area: str | None = None


class DockerSandbox:
    """
    Manages a single Docker container for skill execution.

    One container is shared by every task, conversation and agent of a user,
    so it runs up to ``max_concurrent_executions`` commands at once, each
    under its own execution id (its own pid group, cwd, env, output,
    cancellation and kill verdict). Skills coexist, each in ``/skills/<slot>``.

    State machine (all transitions are synchronous, so they are atomic on the
    event loop and no lock is ever held across a running command):

        CREATING -> RUNNING/INSTALLING (setup) -> READY
        READY/EXECUTING -> EXECUTING (one more command, up to the cap)
        EXECUTING -> READY (its last command ended)
        any -> DESTROYING -> DESTROYED | ERROR

    Loading a Skill claims only that Skill's directory: it runs next to
    commands of other Skills and is busy only while commands of the same
    Skill run (or it is already loading). A command beyond the cap waits in a
    fair FIFO when its caller allows (``queue_timeout``): round-robin across
    work areas (``fair_key``), FIFO within one; otherwise it answers
    ``SandboxBusy`` at once. Side-channel file operations (``side_exec``) run
    alongside commands. Every ``docker exec`` goes through ``_docker``, which
    turns a container Docker reports gone into ``SandboxLost`` instead of a
    command exit code.
    """

    _BUSY_STATES = (SandboxStatus.RUNNING, SandboxStatus.INSTALLING, SandboxStatus.EXECUTING)
    # States in which the container is expected to be running.
    _LIVE_STATES = (SandboxStatus.READY, *_BUSY_STATES)

    def __init__(
        self,
        sandbox_id: str,
        config: ContainerConfig,
    ):
        self._skill_env: dict[str, str] = {}
        self.sandbox_id = sandbox_id
        self.config = config
        self.container_name: str = ""
        self.status: SandboxStatus = SandboxStatus.CREATING
        # The most recently loaded Skill: what a caller that names no Skill
        # sees as /skill (single-Skill containers, older control planes).
        self.skill: Optional[SkillManifest] = None
        # Every Skill loaded side by side, by slot, and its env.
        self._slots: dict[str, SkillManifest] = {}
        self._slot_envs: dict[str, dict[str, str]] = {}
        # Skills being loaded right now: slot -> deadline.
        self._loading: dict[str, float] = {}
        self.created_at: float = 0.0
        self.last_used_at: float = 0.0
        self._config_hash: str = ""
        # Running commands by execution id: (command, started_at, deadline, slot).
        self._executions: dict[str, tuple[str, float, float, str | None]] = {}
        # Commands waiting for a slot: per work area FIFO, areas round-robin.
        self._waiters: dict[str, _SlotWaiter] = {}
        self._wait_queues: dict[str, collections.deque[_SlotWaiter]] = {}
        self._wait_order: collections.deque[str] = collections.deque()
        # The exclusive claim (legacy whole-sandbox claim): its label and deadline.
        self._active_command: str | None = None
        self._exec_started_at: float | None = None
        self._busy_deadline: float | None = None
        self._destroy_requested: bool = False
        self._lost: bool = False
        self._lost_reason: str = ""
        # Disk quota (``sandbox.disk_quota``): each running command's work
        # area, the work areas whose new commands are refused, and when each
        # disk was last measured / which are at or over their resume level.
        self._execution_areas: dict[str, str | None] = {}
        self._disk_refusals: dict[str, DiskRefusal] = {}
        self._disk_measured_at: dict[str, float] = {}
        self._disk_over: set[str] = set()

    @property
    def container_names(self) -> list[str]:
        """Every container this sandbox needs running."""
        names = [self.container_name]
        return [name for name in names if name]

    @property
    def max_concurrent_executions(self) -> int:
        """How many commands may run at once, bounded by the container's pids and memory.

        Each command gets at least 32 pids and 256 MiB of the container's
        limits, so the cap never promises more concurrency than the container
        can hold: a 1 GiB / 512-pid sandbox runs 4 commands at once.
        """

        configured = max(1, int(getattr(self.config, "max_concurrent_executions", 1) or 1))
        bounds = [configured]
        pids = int(getattr(self.config, "pids_limit", 0) or 0)
        if pids > 0:
            bounds.append(max(1, pids // _PIDS_PER_SLOT))
        memory_mib = _memory_mib(str(getattr(self.config, "memory", "") or ""))
        if memory_mib:
            bounds.append(max(1, memory_mib // _MIB_PER_SLOT))
        return max(1, min(bounds))

    @property
    def max_queued_executions(self) -> int:
        return max(0, int(getattr(app_config, "MAX_QUEUED_EXECUTIONS", 64) or 0))

    @property
    def active_execution_ids(self) -> list[str]:
        return list(self._executions)

    @property
    def queued_execution_ids(self) -> list[str]:
        """Waiting commands in the order they would be dispatched now."""

        order: list[str] = []
        queues = {key: list(queue) for key, queue in self._wait_queues.items()}
        rotation = list(self._wait_order)
        while rotation:
            key = rotation.pop(0)
            queue = queues.get(key) or []
            if not queue:
                continue
            order.append(queue.pop(0).execution_id)
            if queue:
                rotation.append(key)
        return order

    def queue_position(self, execution_id: str) -> int | None:
        try:
            return self.queued_execution_ids.index(execution_id) + 1
        except ValueError:
            return None

    @property
    def skills(self) -> list[str]:
        slots = list(self._slots)
        if self.skill is not None and skill_slot(self.skill.name) not in slots:
            slots.append(skill_slot(self.skill.name))
        return slots

    @property
    def has_pending_work(self) -> bool:
        """Commands running or waiting, or a Skill loading: never idle."""

        return bool(self._executions or self._waiters or self._loading or self._active_command)

    @property
    def active_command(self) -> str | None:
        if self._active_command:
            return self._active_command
        for entry in self._executions.values():
            return entry[0]
        return None

    @property
    def active_execution_id(self) -> str | None:
        for execution_id in self._executions:
            return execution_id
        return None

    @property
    def busy_deadline(self) -> float | None:
        """When the current claim must have ended: the earliest of every claim's deadline.

        A command (or a Skill load) past its own deadline means the runner
        failed to stop it, so the earliest deadline (not the latest) decides
        that the sandbox is stuck, even while other commands keep starting.
        """
        deadlines = [entry[2] for entry in self._executions.values()]
        deadlines.extend(self._loading.values())
        if self._busy_deadline is not None:
            deadlines.append(self._busy_deadline)
        return min(deadlines) if deadlines else None

    @property
    def is_lost(self) -> bool:
        return self._lost

    @property
    def expires_at(self) -> float | None:
        if self.status in (SandboxStatus.DESTROYED, SandboxStatus.DESTROYING):
            return None
        return (
            self.last_used_at + app_config.IDLE_TIMEOUT_SECONDS
            if self.last_used_at
            else None
        )

    def touch(self) -> None:
        """Refresh the idle lease without executing untrusted project code."""

        self.ensure_available()
        self.last_used_at = time.time()

    def is_active(self) -> bool:
        return self.status in (SandboxStatus.CREATING, SandboxStatus.INSTALLING, SandboxStatus.EXECUTING, SandboxStatus.DESTROYING)

    def ensure_available(self) -> None:
        """Reject operations once the sandbox is lost, quarantined or being removed."""
        if self._lost:
            raise SandboxLost(
                f"Sandbox {self.sandbox_id} is gone: {self._lost_reason or 'container lost'}"
            )
        if self._destroy_requested or self.status in (
            SandboxStatus.ERROR,
            SandboxStatus.DESTROYING,
            SandboxStatus.DESTROYED,
        ):
            raise SandboxUnavailable(f"Sandbox unavailable (status={self.status.value})")

    # ── Skill views ──

    def slot_manifest(self, slot: str) -> SkillManifest | None:
        manifest = self._slots.get(slot)
        if manifest is None and self.skill is not None and skill_slot(self.skill.name) == slot:
            return self.skill
        return manifest

    def slot_env(self, slot: str | None) -> dict[str, str]:
        if not slot:
            return {}
        if slot in self._slot_envs:
            return dict(self._slot_envs[slot])
        if self.skill is not None and skill_slot(self.skill.name) == slot:
            return dict(self._skill_env)
        return {}

    def resolve_view(self, skill: str | None) -> str | None:
        """The slot a request sees as /skill.

        ``None``: the most recently loaded Skill (single-Skill callers).
        ``""``: no Skill view. A name: that Skill, which must be loaded.
        """
        if skill is None:
            return skill_slot(self.skill.name) if self.skill is not None else None
        if not str(skill).strip():
            return None
        slot = skill_slot(skill)
        if self.slot_manifest(slot) is None:
            raise InvalidRequest(
                f"Skill {skill!r} is not loaded in sandbox {self.sandbox_id}; load it first."
            )
        return slot

    # ── state transitions (synchronous: atomic on the event loop) ──

    def _busy_message(self) -> str:
        if self._active_command:
            return "Sandbox is loading a skill — please wait and retry."
        if self.status == SandboxStatus.EXECUTING:
            running = len(self._executions)
            return (
                f"Sandbox is already running {running} command(s) "
                f"(limit {self.max_concurrent_executions}) — please wait and retry."
            )
        if self.status == SandboxStatus.INSTALLING:
            return "Sandbox is installing dependencies — please wait and retry."
        if self.status == SandboxStatus.RUNNING:
            return "Sandbox is loading a skill — please wait and retry."
        return f"Sandbox is not ready (status={self.status.value}) — please wait and retry."

    def _claim(
        self,
        status: SandboxStatus,
        *,
        budget_seconds: float,
        command: str | None = None,
        execution_id: str | None = None,
    ) -> None:
        """Take the sandbox exclusively: only from READY, no command running or waiting."""
        del execution_id  # exclusive claims are not executions
        self.ensure_available()
        if self.status != SandboxStatus.READY or self._executions or self._loading:
            raise SandboxBusy(self._busy_message())
        now = time.time()
        self.status = status
        self._active_command = command[:500] if command else None
        self._exec_started_at = now
        self._busy_deadline = now + budget_seconds + BUSY_MARGIN_SECONDS
        self.last_used_at = now

    def _release_claim(self) -> None:
        self._active_command = None
        self._exec_started_at = None
        self._busy_deadline = None
        if not self._lost:
            self.last_used_at = time.time()
        if (
            self.status in self._BUSY_STATES
            and not self._executions
            and not self._destroy_requested
            and not self._lost
        ):
            self.status = SandboxStatus.READY
        self._dispatch()

    def _slot_admissible(self, slot: str | None) -> bool:
        return (
            not self._lost
            and not self._destroy_requested
            and not self._active_command
            and self.status in (SandboxStatus.READY, SandboxStatus.EXECUTING)
            and len(self._executions) < self.max_concurrent_executions
            and (slot is None or slot not in self._loading)
        )

    def _take_slot(
        self,
        execution_id: str,
        command: str,
        timeout: float,
        slot: str | None,
        area: str | None = None,
    ) -> None:
        self._execution_areas[execution_id] = area
        now = time.time()
        budget = float(timeout or self.config.exec_timeout) + app_config.GATE_WAIT_TIMEOUT_SECONDS
        self._executions[execution_id] = (
            command[:500] if command else "",
            now,
            now + budget + BUSY_MARGIN_SECONDS,
            slot,
        )
        self.status = SandboxStatus.EXECUTING
        self._exec_started_at = self._exec_started_at or now
        self.last_used_at = now

    def claim_command(
        self,
        execution_id: str | None,
        command: str,
        timeout: float,
        *,
        slot: str | None = None,
        fair_key: str | None = None,
    ) -> None:
        """Reserve one of the sandbox's command slots now, or raise ``SandboxBusy``.

        A command never jumps the queue: while others wait, a caller that
        cannot wait is told the sandbox is busy. A command of a work area the
        disk quota refuses raises ``SandboxDiskQuotaExceeded`` (never started).
        """
        self.ensure_available()
        area = self.check_disk_admission(fair_key, command)
        if self._active_command or self.status not in (SandboxStatus.READY, SandboxStatus.EXECUTING):
            raise SandboxBusy(self._busy_message())
        key = execution_id or uuid.uuid4().hex
        if key in self._executions or key in self._waiters:
            raise ExecutionConflict(f"Execution {key} is already running in this sandbox")
        if slot is not None and slot in self._loading:
            raise SandboxBusy(f"Skill {slot!r} is being loaded — please wait and retry.")
        if len(self._executions) >= self.max_concurrent_executions or self._waiters:
            raise SandboxBusy(self._busy_message())
        self._take_slot(key, command, timeout, slot, area)

    def enqueue_command(
        self,
        execution_id: str,
        command: str,
        timeout: float,
        *,
        fair_key: str | None = None,
        slot: str | None = None,
    ) -> asyncio.Future:
        """Take a slot now, or join the sandbox's fair FIFO for the next free one.

        Returns a future that resolves once the command holds its slot. Order:
        round-robin across work areas (``fair_key``), FIFO within one, so one
        task's burst of commands cannot starve another task's single command.
        A command of a work area the disk quota refuses raises
        ``SandboxDiskQuotaExceeded`` (never started).
        """
        self.ensure_available()
        area = self.check_disk_admission(fair_key, command)
        if execution_id in self._executions or execution_id in self._waiters:
            raise ExecutionConflict(f"Execution {execution_id} is already running in this sandbox")
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        if not self._waiters and self._slot_admissible(slot):
            self._take_slot(execution_id, command, timeout, slot, area)
            future.set_result(None)
            return future
        if len(self._waiters) >= self.max_queued_executions:
            raise SandboxBusy(
                f"Sandbox has {len(self._executions)} command(s) running and "
                f"{len(self._waiters)} waiting (queue limit {self.max_queued_executions})."
            )
        key = str(fair_key or "").strip() or execution_id
        waiter = _SlotWaiter(
            execution_id=execution_id,
            command=command,
            timeout=float(timeout or self.config.exec_timeout),
            fair_key=key,
            slot=slot,
            future=future,
            enqueued_at=time.time(),
            area=area,
        )
        self._waiters[execution_id] = waiter
        queue = self._wait_queues.get(key)
        if queue is None:
            queue = self._wait_queues[key] = collections.deque()
            self._wait_order.append(key)
        queue.append(waiter)
        self.last_used_at = time.time()
        self._dispatch()
        return future

    def _dispatch(self) -> None:
        """Hand free slots to waiting commands, fairly. Synchronous: atomic on the loop."""
        while self._waiters:
            if not self._slot_admissible(None):
                return
            granted = False
            for _ in range(len(self._wait_order)):
                key = self._wait_order[0]
                queue = self._wait_queues.get(key)
                if not queue:
                    self._wait_order.popleft()
                    self._wait_queues.pop(key, None)
                    continue
                head = queue[0]
                if head.future.done():
                    # Cancelled or timed out while it waited.
                    queue.popleft()
                    self._waiters.pop(head.execution_id, None)
                    granted = True
                    break
                if not self._slot_admissible(head.slot):
                    # Its Skill is loading: let the next area go first.
                    self._wait_order.rotate(-1)
                    continue
                queue.popleft()
                self._waiters.pop(head.execution_id, None)
                self._wait_order.rotate(-1)
                if not queue:
                    self._wait_order.remove(key)
                    self._wait_queues.pop(key, None)
                self._take_slot(head.execution_id, head.command, head.timeout, head.slot, head.area)
                head.future.set_result(None)
                granted = True
                break
            if not granted:
                return

    def withdraw_waiter(self, execution_id: str, exc: BaseException | None = None) -> bool:
        """Remove a command that never got a slot. True when it was still waiting."""
        waiter = self._waiters.pop(execution_id, None)
        if waiter is None:
            return False
        queue = self._wait_queues.get(waiter.fair_key)
        if queue is not None:
            try:
                queue.remove(waiter)
            except ValueError:
                pass
            if not queue:
                self._wait_queues.pop(waiter.fair_key, None)
                try:
                    self._wait_order.remove(waiter.fair_key)
                except ValueError:
                    pass
        if not waiter.future.done():
            if exc is None:
                waiter.future.cancel()
            else:
                waiter.future.set_exception(exc)
                waiter.future.exception()  # retrieved: never "never retrieved"
        self._dispatch()
        return True

    def _fail_waiters(self, exc_factory) -> None:
        for execution_id in list(self._waiters):
            self.withdraw_waiter(execution_id, exc_factory())

    def release_command(self, execution_id: str | None) -> None:
        """End the command claim held by ``execution_id`` (idempotent)."""
        if execution_id is None or self._executions.pop(execution_id, None) is None:
            return
        self._execution_areas.pop(execution_id, None)
        if not self._lost:
            self.last_used_at = time.time()
        if not self._executions:
            self._exec_started_at = None
            if (
                self.status == SandboxStatus.EXECUTING
                and not self._destroy_requested
                and not self._lost
            ):
                self.status = SandboxStatus.READY
        self._dispatch()

    # ── disk quota (the policy lives in ``sandbox.disk_quota``) ──

    @property
    def disk_refusals(self) -> dict[str, DiskRefusal]:
        return dict(self._disk_refusals)

    def execution_areas(self) -> dict[str, str | None]:
        """Running commands and their work areas (None: the caller named none)."""
        return {execution_id: self._execution_areas.get(execution_id) for execution_id in self._executions}

    def check_disk_admission(self, fair_key: str | None, command: str) -> str | None:
        """The command's work area; ``SandboxDiskQuotaExceeded`` when the disk quota refuses it."""
        area = work_area_key(fair_key)
        refusal = self._disk_refusals.get(area) if area else None
        if refusal is not None and not is_cleanup_command(command):
            raise SandboxDiskQuotaExceeded(
                refusal.message, extra={"started": False, "area": area, "disk": refusal.root},
            )
        return area

    def set_disk_refusals(self, refusals: "dict[str, DiskRefusal]") -> None:
        """Install the disk quota's refusals; a refused area's waiting commands never start."""
        self._disk_refusals = dict(refusals)
        for waiter in list(self._waiters.values()):
            refusal = self._disk_refusals.get(waiter.area) if waiter.area else None
            if refusal is None or is_cleanup_command(waiter.command):
                continue
            self.withdraw_waiter(
                waiter.execution_id,
                SandboxDiskQuotaExceeded(
                    refusal.message, extra={"started": False, "area": waiter.area, "disk": refusal.root},
                ),
            )

    def disk_roots(self) -> list[DiskRoot]:
        """The disks of this container the runner bounds (``sandbox.disk_quota``).

        The shared /workspace tmpfs (per work area), and the disk-backed
        install dir every container gets (``SANDBOX_LOCAL_DIR``): shared by
        all of the user's work areas, so its writers are charged, and the
        container is recycled only once it is idle and the dir still over
        (``recycle_when_idle``). Measured with ``du`` at the old reaper's
        cadence; ``SANDBOX_LOCAL_DISK_LIMIT_MB=0`` leaves it unbounded.
        """
        roots: list[DiskRoot] = []
        if self._mounts_shared_workspace():
            size = _memory_mib(str(self.config.workspace_tmpfs_size)) * 1024 * 1024
            if size > 0:
                roots.append(workspace_root(
                    size, interval_seconds=float(app_config.SANDBOX_DISK_WATCHDOG_INTERVAL_SECONDS),
                ))
        local_limit_mb = int(app_config.SANDBOX_LOCAL_DISK_LIMIT_MB)
        if local_limit_mb > 0:
            roots.append(DiskRoot(
                path=SANDBOX_LOCAL_DIR,
                limit_bytes=local_limit_mb * 1024 * 1024,
                areas_dir=None,
                filesystem=False,
                recycle_when_idle=True,
                interval_seconds=60,
            ))
        return roots

    async def measure_disk(self, roots: list[DiskRoot]) -> list[DiskUsage]:
        """Measure ``roots`` with one read-only ``docker exec`` (``_DISK_PROBE_SOURCE``).

        Returns [] when the container cannot answer now (the next tick
        retries); raises ``SandboxLost`` when Docker reports it gone. Never
        refreshes the idle lease.
        """
        if self._lost or self.status not in self._LIVE_STATES or not self.container_name or not roots:
            return []
        spec = [
            {
                "path": root.path,
                "filesystem": root.filesystem,
                "areas_dir": root.areas_dir,
                "areas_from": root.resume_bytes,
                "writers": True,
            }
            for root in roots
        ]
        result = await self._docker(
            ["docker", "exec", self.container_name, "python", "-c", _DISK_PROBE_SOURCE, json.dumps(spec)],
            timeout=_DISK_DOCKER_TIMEOUT_SECONDS,
        )
        try:
            report = json.loads(result.stdout.decode("utf-8", errors="replace").strip().splitlines()[-1])
            measured = {str(item["path"]): item for item in report["roots"]}
            commands = dict(report.get("commands") or {})
        except (ValueError, KeyError, IndexError, TypeError):
            logger.warning(
                "Disk probe of sandbox %s gave no report (exit %s): %s",
                self.sandbox_id,
                result.returncode,
                _printable(result.stderr.decode("utf-8", errors="replace"), 300),
            )
            return []
        usages = []
        for root in roots:
            item = measured.get(root.path)
            if item is None:
                continue
            cwd_areas: dict[str, frozenset[str]] = {}
            if root.areas_dir:
                prefix = root.areas_dir.rstrip("/") + "/"
                for digest, entry in commands.items():
                    areas = {
                        str(cwd)[len(prefix):].split("/", 1)[0]
                        for cwd in (entry or {}).get("cwd") or []
                        if str(cwd).startswith(prefix)
                    }
                    if areas:
                        cwd_areas[str(digest)] = frozenset(areas)
            usages.append(DiskUsage(
                root=root,
                used_bytes=int(item.get("used") or 0),
                area_bytes={str(area): int(size) for area, size in dict(item.get("areas") or {}).items()},
                writers=frozenset(
                    str(digest) for digest, entry in commands.items()
                    if root.path in ((entry or {}).get("open") or [])
                ),
                cwd_areas=cwd_areas,
            ))
        return usages

    async def stop_for_disk(self, kills: list[DiskKill]) -> None:
        """Stop commands the disk quota names (``_DISK_KILL_SOURCE``): verdict, then every process."""
        if not kills or self._lost or not self.container_name:
            return
        items = [{"digest": kill.digest, "verdict": kill.verdict} for kill in kills]
        result = await self._docker(
            [
                "docker", "exec", self.container_name, "python", "-c", _DISK_KILL_SOURCE,
                f"{CONTROL_DIR}/verdicts", json.dumps(items),
            ],
            timeout=_DISK_DOCKER_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            logger.warning(
                "Disk kill in sandbox %s exited %s: %s",
                self.sandbox_id,
                result.returncode,
                _printable(result.stderr.decode("utf-8", errors="replace"), 300),
            )

    def mark_disk_unusable(self, reason: str) -> None:
        """Retire an idle sandbox the disk quota found unusable; the runner reclaims it."""
        self._mark_lost(reason)

    def holds_command(self, execution_id: str | None) -> bool:
        return (
            execution_id is not None
            and self.status == SandboxStatus.EXECUTING
            and execution_id in self._executions
        )

    def claim_skill_load(self, slot: str, budget_seconds: float) -> None:
        """Claim one Skill's directory for a load; commands of other Skills keep running."""
        self.ensure_available()
        if self._active_command or self.status not in (SandboxStatus.READY, SandboxStatus.EXECUTING):
            raise SandboxBusy(self._busy_message())
        if slot in self._loading:
            raise SandboxBusy(f"Skill {slot!r} is already being loaded — please wait and retry.")
        running = [eid for eid, entry in self._executions.items() if len(entry) > 3 and entry[3] == slot]
        if running:
            raise SandboxBusy(
                f"{len(running)} command(s) of Skill {slot!r} are running; it can be reloaded "
                "once they finish."
            )
        now = time.time()
        self._loading[slot] = now + budget_seconds + BUSY_MARGIN_SECONDS
        self.last_used_at = now

    def release_skill_load(self, slot: str) -> None:
        if self._loading.pop(slot, None) is not None and not self._lost:
            self.last_used_at = time.time()
        self._dispatch()

    def _quarantine(self, reason: str) -> None:
        """Leave the sandbox unusable; the runner destroys it."""
        logger.warning("Quarantining sandbox %s: %s", self.sandbox_id, reason)
        self._active_command = None
        self._executions.clear()
        self._execution_areas.clear()
        self._loading.clear()
        self._exec_started_at = None
        self._busy_deadline = None
        if self.status not in (SandboxStatus.DESTROYING, SandboxStatus.DESTROYED):
            self.status = SandboxStatus.ERROR
        # Commands still waiting for a slot never started: their callers may
        # retry them on another sandbox.
        self._fail_waiters(
            lambda: (
                SandboxLost(f"Sandbox {self.sandbox_id} is gone: {reason}", extra={"started": False})
                if self._lost
                else SandboxUnavailable(
                    f"Sandbox {self.sandbox_id} became unusable: {reason}", extra={"started": False}
                )
            )
        )

    def _mark_lost(self, reason: str) -> None:
        if not self._lost:
            logger.error("Sandbox %s lost its container: %s", self.sandbox_id, reason)
        self._lost = True
        self._lost_reason = reason
        self._quarantine(reason)

    # ── lifecycle ──

    async def setup(
        self,
        skill: SkillManifest,
        env: dict[str, str],
        auto_install: bool = True,
        baked: BakedSkill | None = None,
    ) -> None:
        """Full setup: create container, inject files, optionally install deps.

        ``baked`` materializes the image's copy of a built-in Skill instead of
        copying ``skill.skill_dir`` from the host.

        Runs before the sandbox is registered, so nothing else can observe it;
        on failure the runner destroys the half-built container.
        """
        self.skill = skill
        self.created_at = time.time()
        self.last_used_at = self.created_at

        name = self._resolve_container_name(skill)
        self.container_name = name
        self._config_hash = _config_hash(self.config, skill.skill_dir)

        # Skill env is handed to each ``docker exec`` (see
        # ``_skill_env_args``) rather than baked into the container, so a
        # later ``load_skill`` can replace it and a reused container never
        # carries the previous skill's credentials.
        self._skill_env = dict(env)
        slot = skill_slot(skill.name)
        await self._ensure_container(skill, {})

        self.status = SandboxStatus.RUNNING
        if baked is not None:
            # The Skill's own /skills/<slot>, exactly where an upload goes.
            await self.materialize_builtin_skill(baked.name, baked.digest, skill_dir(slot))
        else:
            await self._inject_files(skill, slot=slot)

        if auto_install and (skill.requirements_txt or skill.package_json):
            self.status = SandboxStatus.INSTALLING
            result = await self._install_dependencies(skill, env=self._skill_env, slot=slot)
            if result.exit_code != 0:
                self.status = SandboxStatus.ERROR
                raise DependencyInstallFailed(
                    f"dependency install failed (exit {result.exit_code}):\n"
                    f"{bounded_text(result.stderr, 8000)}"
                )

        self._slots[slot] = skill
        self._slot_envs[slot] = dict(env)
        self.last_used_at = time.time()
        self.status = SandboxStatus.READY

    def _mounts_shared_workspace(self) -> bool:
        """The shared /workspace tmpfs, unless disabled or a caller mounts /workspace itself."""
        if not getattr(self.config, "workspace_tmpfs_size", ""):
            return False
        for volume in getattr(self.config, "volumes", []) or []:
            parts = str(volume).split(":")
            if len(parts) >= 2 and parts[1].rstrip("/") == SHARED_WORKSPACE_ROOT:
                return False
        return True

    def runner_workdir(self) -> str | None:
        """A workdir the runner's config places outside the per-user layout.

        A default runner's ``SANDBOX_WORKDIR`` is the Skill view (``/skill``):
        its containers work in /skills and /workspace. A dedicated pool's
        runner may name its own (the external-agent pool: ``/work``); its
        containers then get a writable tmpfs there and start in it. Never a
        caller's choice: ``workdir`` is runner-owned (overrides.py).
        """
        workdir = posixpath.normpath(str(getattr(self.config, "workdir", "") or "/"))
        if not workdir.startswith("/") or workdir in {"/", SKILL_VIEW, SKILLS_ROOT}:
            return None
        reserved = (
            SKILLS_ROOT, SKILL_VIEW, SHARED_WORKSPACE_ROOT, CONTROL_DIR, SANDBOX_LOCAL_DIR,
            "/tmp", "/var/tmp", "/root", "/home/sandbox",
        )
        if any(workdir == root or workdir.startswith(root + "/") for root in reserved):
            return None
        return workdir

    def default_cwd(self) -> str:
        """Where a command with no Skill view and no workdir starts."""
        if (runner_workdir := self.runner_workdir()) is not None:
            return runner_workdir
        if self._mounts_shared_workspace():
            return SHARED_WORKSPACE_ROOT
        return "/tmp"

    def command_placement(
        self,
        command: str,
        workdir: str | None,
        *,
        skill: str | None,
        skill_env: bool,
    ) -> tuple[str, str, dict[str, str], str | None]:
        """(command, cwd, env, slot) of one command under its Skill view.

        With a Skill view the command's ``/skill`` paths are that Skill's
        directory, it starts there unless told otherwise, the launcher sets
        ``SKILL_DIR`` to it, and -- only with ``skill_env`` -- it gets the
        Skill's env. Without one it starts in ``workdir`` (or the shared
        /workspace) with no Skill env at all.
        """
        slot = self.resolve_view(skill)
        if slot is None:
            return command, workdir or self.default_cwd(), {}, None
        env = self.slot_env(slot) if skill_env else {}
        cwd = rewrite_skill_paths(workdir, slot) if workdir else skill_dir(slot)
        return rewrite_skill_paths(command, slot), cwd, env, slot

    async def exec(
        self,
        command: str,
        timeout: int | None = None,
        workdir: str | None = None,
        execution_id: str | None = None,
        interactive: bool = False,
        skill_env: bool = True,
        skill: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResponse:
        """Execute one command; it must hold (or get) one of the sandbox's command slots.

        The runner claims the slot first (``claim_command`` or the fair
        queue) so a full sandbox is reported -- or waited for -- before the
        command starts; a direct call claims here. ``skill`` picks the Skill
        view (see ``command_placement``); ``skill_env=False`` runs without
        that Skill's env: an agent sharing the sandbox that did not invoke the
        Skill never receives its credentials. ``env`` is layered over that
        for this command only; it travels the same 0600 env-file path and is
        never stored.
        """
        timeout = timeout or self.config.exec_timeout
        extra_env = dict(env or {})
        command, wd, env, slot = self.command_placement(
            command, workdir, skill=skill, skill_env=skill_env,
        )
        if extra_env:
            env = {**env, **extra_env}
        # Every command gets an id: it runs under the launcher (pid file), so
        # it can always be cancelled and killed on timeout.
        execution_id = execution_id or uuid.uuid4().hex
        owns_claim = not self.holds_command(execution_id)
        if owns_claim:
            self.claim_command(execution_id, command, timeout, slot=slot)
        else:
            self.ensure_available()
        try:
            return await self._run_in_container(
                command,
                timeout=timeout,
                workdir=wd,
                execution_id=execution_id,
                interactive=interactive,
                env=env,
                skill_home=skill_dir(slot) if slot else "",
            )
        finally:
            if owns_claim:
                self.release_command(execution_id)

    async def side_exec(
        self,
        command: str,
        *,
        timeout: float,
        input_data: bytes | None = None,
        workdir: str = "/",
    ) -> ExecResponse:
        """Run a short side-channel command (file read/write) next to any running command.

        Not gated on EXECUTING: reading a file while a background command runs
        is the point. Rejected while a skill load replaces the workdir, and on
        an unavailable sandbox. Never receives the skill env.
        """
        self.ensure_available()
        if self.status not in (SandboxStatus.READY, SandboxStatus.EXECUTING):
            raise SandboxBusy(self._busy_message())
        self.last_used_at = time.time()
        try:
            result = await self._docker(
                [
                    "docker", "exec",
                    *(["-i"] if input_data is not None else []),
                    "-w", workdir or "/",
                    self.container_name,
                    "sh", "-c", command,
                ],
                timeout=timeout,
                input_data=input_data,
            )
        finally:
            if not self._lost:
                self.last_used_at = time.time()
        # Base64 file reads come through here: decode tens of MB off the loop.
        return await asyncio.to_thread(_exec_response, result)

    async def verify_alive(self) -> None:
        """Confirm with Docker that the container still runs; raise ``SandboxLost`` if not."""
        if self._lost:
            raise SandboxLost(
                f"Sandbox {self.sandbox_id} is gone: {self._lost_reason or 'container lost'}"
            )
        if self.status not in self._LIVE_STATES or not self.container_name:
            return
        for name in self.container_names:
            state = await asyncio.to_thread(_container_state, name)
            if state["exists"] and state["running"]:
                continue
            if self._destroy_requested:
                return
            what = "container"
            reason = f"{what} is not running" if state["exists"] else f"{what} no longer exists"
            self._mark_lost(reason)
            raise SandboxLost(f"Sandbox {self.sandbox_id} is gone: {reason}")

    def _prepare_skill_env(
        self,
        *,
        decode_multiline: bool = True,
        env: dict[str, str] | None = None,
    ) -> tuple[str | None, list[str]]:
        """Materialise a skill env for one ``docker exec``.

        Values travel through a 0600 temp file and ``--env-file`` so they never
        appear on the docker command line (``ps`` on the host). ``--env-file``
        cannot express a newline, so a multi-line value (a PEM key, say) is
        base64-encoded in the file and its key listed in
        ``MANOR_SANDBOX_B64_ENV``; the exec launcher decodes those before the
        command starts, so the process sees the original value. The file lives
        until the exec completes; ``_docker`` removes it.

        ``decode_multiline`` is False on the non-launcher path (dependency
        installs), where nothing decodes the marker: multi-line values are
        omitted there so pip/npm see the variable absent rather than a
        base64 blob under the real name.

        ``env`` defaults to the committed skill env; a skill load passes the
        env it is about to commit so its dependency install sees it.

        Returns ``(env_file, extra_argv)``; ``extra_argv`` is always empty and
        kept only so callers need not change when the transport does.
        """
        skill_env = self._skill_env if env is None else env
        if not skill_env:
            return None, []
        import base64
        import tempfile

        file_lines: list[str] = []
        encoded_keys: list[str] = []
        for key, value in skill_env.items():
            if key == _B64_ENV_KEYS_VAR:
                # Reserved control name; a skill must not steer the launcher.
                continue
            text = str(value)
            if "\n" in text or "\r" in text:
                if not decode_multiline:
                    continue
                file_lines.append(f"{key}={base64.b64encode(text.encode('utf-8')).decode('ascii')}")
                encoded_keys.append(key)
            else:
                file_lines.append(f"{key}={text}")
        if not file_lines:
            return None, []
        if encoded_keys:
            file_lines.append(f"{_B64_ENV_KEYS_VAR}={','.join(encoded_keys)}")
        fd, env_file = tempfile.mkstemp(prefix=_SKILL_ENV_FILE_PREFIX, suffix=_SKILL_ENV_FILE_SUFFIX)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(file_lines) + "\n")
        except Exception:
            os.unlink(env_file)
            raise
        return env_file, []

    @staticmethod
    def _skill_env_args(env_file: str | None, argv: list[str] | None = None) -> list[str]:
        args = ["--env-file", env_file] if env_file else []
        return [*args, *(argv or [])]

    async def _docker(
        self,
        args: list[str],
        *,
        timeout: float,
        input_data: bytes | None = None,
        cleanup_file: str | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one ``docker exec`` against this container. The only exec path.

        A failure that may be Docker's rather than the command's is confirmed
        with an inspect: a container that is gone or stopped marks the sandbox
        lost and raises ``SandboxLost`` — never a command exit code. The
        thread is awaited to completion even if the caller is cancelled, so
        ``cleanup_file`` (a secret env file) is always removed.
        """

        def _run() -> subprocess.CompletedProcess[bytes]:
            try:
                return _run_docker(
                    args,
                    input_data=input_data,
                    timeout=timeout,
                    allow_failure=True,
                )
            finally:
                _remove_skill_env_file(cleanup_file)

        task = asyncio.ensure_future(asyncio.to_thread(_run))
        try:
            result = await asyncio.shield(task)
        except BaseException:
            await self._wait_for_task_terminal(task)
            raise
        if _may_be_container_failure(result):
            await self._raise_if_container_gone(result)
        return result

    async def _raise_if_container_gone(self, result: subprocess.CompletedProcess[bytes]) -> None:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        try:
            state = await asyncio.to_thread(_container_state, self.container_name)
        except DockerUnavailable:
            raise
        except RunnerError:
            raise
        except RuntimeError as exc:
            raise DockerUnavailable(f"Cannot confirm sandbox container state: {exc}") from exc
        if state["exists"] and state["running"]:
            return  # The command's own failure: report its exit code.
        if self._destroy_requested:
            raise SandboxGone(f"Sandbox {self.sandbox_id} was destroyed")
        reason = (
            "container is not running" if state["exists"] else "container no longer exists"
        )
        detail = _printable(stderr, 300)
        self._mark_lost(f"{reason}" + (f" ({detail})" if detail else ""))
        raise SandboxLost(f"Sandbox {self.sandbox_id} is gone: {reason}")

    async def _run_in_container(
        self,
        command: str,
        *,
        timeout: float,
        workdir: str,
        execution_id: str | None = None,
        interactive: bool = False,
        env: dict[str, str] | None = None,
        skill_home: str = "",
    ) -> ExecResponse:
        """Run a command in the container. Status is the caller's business."""
        pid_file: str | None = None
        if execution_id is not None:
            pid_file = self._execution_pid_file(execution_id)
        # The secret file is written as late as possible and removed on every
        # exit from here on, so a cancellation or a failure while building the
        # command cannot leave live credentials in the host tmp dir.
        env_file, env_argv = self._prepare_skill_env(
            decode_multiline=execution_id is not None,
            env=env,
        )
        try:
            if execution_id is not None:
                args = self._execution_command_args(
                    command,
                    workdir,
                    pid_file,
                    execution_id,
                    interactive,
                    env_file=env_file,
                    env_argv=env_argv,
                    skill_home=skill_home,
                )
            else:
                args = [
                    "docker", "exec", "-i",
                    "-w", workdir,
                    *self._skill_env_args(env_file, env_argv),
                    self.container_name,
                    "sh", "-c", command,
                ]
        except BaseException:
            _remove_skill_env_file(env_file)
            raise

        try:
            result = await self._docker(args, timeout=timeout, cleanup_file=env_file)
        except DockerCommandTimeout as exc:
            if execution_id is not None:
                # docker exec returning does not stop the process inside the
                # container; kill its process group so nothing keeps running.
                try:
                    await self.cancel_execution(execution_id)
                except SandboxGone:
                    raise
                except Exception:
                    logger.exception(
                        "Failed to terminate timed-out sandbox execution %s",
                        execution_id,
                    )
                    # A command that may still be running must never share
                    # the sandbox with the next one: quarantine it, and the
                    # runner destroys it.
                    self._quarantine(
                        f"timed-out execution {execution_id} could not be terminated"
                    )
            raise CommandTimeout(
                f"Command timed out after {timeout}s",
                extra={
                    "timeout_seconds": timeout,
                    "execution_id": execution_id,
                    "stdout": bounded_text(exc.stdout),
                    "stderr": bounded_text(exc.stderr),
                },
            ) from None
        finally:
            if pid_file is not None and not self._lost:
                await self._remove_execution_pid_file(pid_file)

        # Decode in a worker thread: base64 file reads return tens of MB.
        stdout, stderr = await asyncio.to_thread(_decode_output, result)
        outcome = detail = None
        if execution_id is not None:
            stderr, status = _strip_exec_status(stderr)
            outcome, detail = exec_outcome(
                result.returncode,
                status,
                memory=str(getattr(self.config, "memory", "") or ""),
                pids_limit=int(getattr(self.config, "pids_limit", 0) or 0),
            )
            if outcome:
                logger.warning(
                    "Sandbox %s execution %s: %s", self.sandbox_id, execution_id, outcome,
                )
        return ExecResponse(
            stdout=stdout,
            stderr=stderr,
            exit_code=result.returncode,
            execution_id=execution_id,
            outcome=outcome,
            outcome_detail=detail,
        )

    async def cancel_execution(self, execution_id: str) -> bool:
        """Terminate the exact active execution (sync or background).

        Returns False when that execution is not (or no longer) running.
        """
        if not await self._is_active_execution(execution_id):
            return False

        pid_file = self._execution_pid_file(execution_id)
        pid = await self._wait_for_execution_pid(execution_id, pid_file)
        if pid is None:
            if not await self._is_active_execution(execution_id):
                return False
            raise CancelFailed(f"Execution process was not ready for cancellation: {execution_id}")

        if not await self._signal_execution_group(pid, "TERM"):
            return False
        deadline = asyncio.get_running_loop().time() + _CANCEL_GRACE_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if not await self._is_active_execution(execution_id):
                return True
            if not await self._execution_group_alive(pid):
                return True
            await asyncio.sleep(_CANCEL_POLL_INTERVAL_SECONDS)

        if (
            await self._is_active_execution(execution_id)
            and await self._execution_group_alive(pid)
        ):
            await self._signal_execution_group(pid, "KILL")
        return True

    def _execution_command_args(
        self,
        command: str,
        workdir: str,
        pid_file: str,
        execution_id: str,
        interactive: bool,
        env_file: str | None = None,
        env_argv: list[str] | None = None,
        skill_home: str = "",
    ) -> list[str]:
        event_file, response_dir, bridge_file = self._execution_mailbox_paths(
            execution_id=execution_id
        )
        # A directory under the shared /workspace may not exist yet: docker
        # cannot ``-w`` into it, so the launcher creates it and starts there.
        create_cwd = _is_shared_workspace_path(workdir)
        return [
            "docker", "exec", "-i",
            "-w", "/",
            *self._skill_env_args(env_file, env_argv),
            self.container_name,
            "python", "-c", _EXEC_LAUNCHER_SOURCE, command, pid_file, event_file,
            response_dir, bridge_file,
            _SANDBOX_BRIDGE_SOURCE if interactive else "", execution_id,
            "1" if interactive else "0",
            workdir,
            "1" if create_cwd else "0",
            self._verdict_file(execution_id),
            skill_home,
        ]

    @staticmethod
    def _verdict_file(execution_id: str) -> str:
        safe_id = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:24]
        return f"{CONTROL_DIR}/verdicts/{safe_id}.json"

    async def start_watchdog(self) -> bool:
        """Start the container's resource watchdog (detached; best effort).

        It runs inside the container from creation on, so it never needs to
        fork to act: a fork bomb that exhausted pids cannot stop it from
        killing the bomb. See ``_WATCHDOG_SOURCE``.
        """
        if not self.container_name:
            return False
        try:
            result = await self._docker(
                [
                    "docker", "exec", "-d", self.container_name,
                    "python", "-c", _WATCHDOG_SOURCE, CONTROL_DIR,
                    str(_WATCHDOG_PIDS_FRACTION),
                ],
                timeout=30,
            )
        except (SandboxGone, DockerUnavailable):
            raise
        except Exception:
            logger.warning("Could not start the watchdog of sandbox %s", self.sandbox_id, exc_info=True)
            return False
        if result.returncode != 0:
            logger.warning(
                "Watchdog of sandbox %s did not start: %s",
                self.sandbox_id,
                _printable(result.stderr.decode("utf-8", errors="replace"), 300),
            )
            return False
        return True

    @staticmethod
    def _execution_pid_file(execution_id: str) -> str:
        safe_id = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:24]
        return f"/tmp/manor-exec-{safe_id}.pid"

    @staticmethod
    def _execution_mailbox_paths(execution_id: str) -> tuple[str, str, str]:
        safe_id = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:24]
        prefix = f"/tmp/manor-exec-{safe_id}"
        return f"{prefix}.events.jsonl", f"{prefix}.responses", f"{prefix}.bridge.py"

    async def read_execution_event_lines(
        self,
        execution_id: str,
    ) -> tuple[list[str], bool]:
        """Read the bounded structured event stream without claiming the sandbox."""

        event_file, _response_dir, _bridge_file = self._execution_mailbox_paths(
            execution_id
        )
        reader = (
            "import pathlib,sys;"
            "path=pathlib.Path(sys.argv[1]);limit=int(sys.argv[2]);"
            "data=path.open('rb').read(limit+1) if path.exists() else b'';"
            "sys.stdout.buffer.write(data)"
        )
        result = await self._docker(
            [
                "docker", "exec", self.container_name, "python", "-c", reader,
                event_file, str(_EXECUTION_EVENT_BYTES_LIMIT),
            ],
            # Include Docker startup while reading the bounded event mailbox.
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Failed to read sandbox execution events: {stderr}")
        truncated = len(result.stdout) > _EXECUTION_EVENT_BYTES_LIMIT
        data = result.stdout[:_EXECUTION_EVENT_BYTES_LIMIT]
        return data.decode("utf-8", errors="replace").splitlines(), truncated

    async def read_file_range(self, path: str, offset: int, limit: int) -> tuple[bytes, int]:
        """Read ``limit`` bytes at ``offset`` without claiming the sandbox.

        A background execution holds the sandbox's command claim for its
        whole run, so log tailing must not need it, like
        ``read_execution_event_lines``. Returns ``(data, current_file_size)``.
        """
        reader = "\n".join((
            "import os,sys",
            "p,o,n=sys.argv[1],int(sys.argv[2]),int(sys.argv[3])",
            "s=os.path.getsize(p) if os.path.isfile(p) else -1",
            "sys.stdout.buffer.write(str(s).encode()+b'\\n')",
            "if s>=0:",
            "    f=open(p,'rb');f.seek(o);sys.stdout.buffer.write(f.read(n))",
        ))
        # The liveness-checked exec path: a gone container is SandboxLost,
        # never misread as a missing file.
        result = await self._docker(
            ["docker", "exec", self.container_name, "python", "-c", reader, path, str(int(offset)), str(int(limit))],
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Failed to tail sandbox file: {stderr}")
        header, _, data = result.stdout.partition(b"\n")
        size = int(header or b"-1")
        if size < 0:
            raise FileNotFoundError(f"File not found in sandbox: {path}")
        return data, size

    async def write_execution_response(
        self,
        execution_id: str,
        event_id: str,
        response_json: bytes,
    ) -> None:
        """Atomically deliver one bounded response to the running process."""

        if len(response_json) > _EXECUTION_RESPONSE_BYTES_LIMIT:
            raise InvalidRequest("Sandbox execution response exceeds 16384 bytes")
        _event_file, response_dir, _bridge_file = self._execution_mailbox_paths(
            execution_id
        )
        digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:32]
        response_file = f"{response_dir}/{digest}.json"
        temp_file = f"{response_file}.{uuid.uuid4().hex}.tmp"
        writer = """
import os
import sys

target, temporary, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
data = sys.stdin.buffer.read(limit + 1)
if len(data) > limit:
    raise SystemExit(2)
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.write(fd, data)
    os.fsync(fd)
finally:
    os.close(fd)
os.replace(temporary, target)
"""
        result = await self._docker(
            [
                "docker", "exec", "-i", self.container_name, "python", "-c",
                writer, response_file, temp_file, str(_EXECUTION_RESPONSE_BYTES_LIMIT),
            ],
            input_data=response_json,
            timeout=5,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Failed to deliver sandbox execution response: {stderr}")

    async def cleanup_execution_mailbox(self, execution_id: str) -> None:
        await self.cleanup_execution_mailboxes([execution_id])

    async def cleanup_execution_mailboxes(self, execution_ids: list[str]) -> None:
        if not execution_ids or self._lost:
            return
        paths: list[str] = []
        for execution_id in execution_ids:
            event_file, response_dir, bridge_file = self._execution_mailbox_paths(
                execution_id
            )
            paths.extend((event_file, bridge_file, response_dir))
        cleanup = """
import os
import shutil
import sys

paths = sys.argv[1:]
for index in range(0, len(paths), 3):
    event_file, bridge_file, response_dir = paths[index:index + 3]
    for path in (event_file, bridge_file):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    shutil.rmtree(response_dir, ignore_errors=True)
"""
        try:
            result = await self._docker(
                [
                    "docker", "exec", self.container_name, "python", "-c", cleanup,
                    *paths,
                ],
                timeout=5,
            )
            if result.returncode != 0:
                logger.debug(
                    "Execution mailbox cleanup failed for %s", execution_ids
                )
        except Exception:
            logger.debug("Failed to clean execution mailbox", exc_info=True)

    async def _is_active_execution(self, execution_id: str) -> bool:
        return execution_id in self._executions

    async def _wait_for_execution_pid(
        self,
        execution_id: str,
        pid_file: str,
    ) -> int | None:
        deadline = asyncio.get_running_loop().time() + _EXECUTION_PID_WAIT_SECONDS
        while True:
            if not await self._is_active_execution(execution_id):
                return None
            result = await self._docker(
                ["docker", "exec", self.container_name, "sh", "-c", f"cat {shlex.quote(pid_file)}"],
                timeout=5,
            )
            if result.returncode == 0:
                raw_pid = result.stdout.decode("ascii", errors="ignore").strip()
                if raw_pid.isdigit() and int(raw_pid) > 1:
                    return int(raw_pid)
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(_CANCEL_POLL_INTERVAL_SECONDS)

    async def _signal_execution_group(self, pid: int, signal: str) -> bool:
        result = await self._docker(
            [
                "docker", "exec", self.container_name, "sh", "-c",
                f"kill -{signal} -{pid}",
            ],
            timeout=5,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            if "no such process" in stderr.casefold() or not stderr:
                return False
            raise CancelFailed(f"Failed to send {signal} to execution process group: {stderr}")
        return True

    async def _execution_group_alive(self, pid: int) -> bool:
        result = await self._docker(
            ["docker", "exec", self.container_name, "sh", "-c", f"kill -0 -{pid}"],
            timeout=5,
        )
        if result.returncode == 0:
            return True
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if "no such process" in stderr.casefold() or not stderr:
            return False
        raise CancelFailed(f"Failed to inspect execution process group: {stderr}")

    async def _remove_execution_pid_file(self, pid_file: str) -> None:
        try:
            await self._docker(
                ["docker", "exec", self.container_name, "sh", "-c", f"rm -f {shlex.quote(pid_file)}"],
                timeout=5,
            )
        except Exception:
            logger.debug("Failed to remove execution pid file", exc_info=True)

    @staticmethod
    async def _wait_for_task_terminal(task: asyncio.Future[object]) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except BaseException:
                pass

    async def destroy(self) -> None:
        """Force-remove the container. Never waits for a running command.

        ``docker rm -f`` kills every process in the container, including a
        running command, whose exec then reports the sandbox destroyed.
        """
        self._destroy_requested = True
        self.status = SandboxStatus.DESTROYING
        self._busy_deadline = time.time() + _DESTROY_TIMEOUT + BUSY_MARGIN_SECONDS
        try:
            if self.container_name:
                result = await asyncio.to_thread(
                    _run_docker,
                    ["docker", "rm", "-f", "-v", self.container_name],
                    timeout=_DESTROY_TIMEOUT,
                    allow_failure=True,
                )
                if result.returncode != 0:
                    state = await asyncio.to_thread(_container_state, self.container_name)
                    if state["exists"]:
                        stderr = result.stderr.decode("utf-8", errors="replace").strip()
                        raise DestroyFailed(
                            f"Failed to destroy sandbox container {self.container_name}: "
                            f"docker rm exited {result.returncode}, but the container still exists"
                            + (f": {_printable(stderr)}" if stderr else "")
                        )
        except BaseException:
            self.status = SandboxStatus.ERROR
            self._busy_deadline = None
            raise
        self._active_command = None
        self._executions.clear()
        self._exec_started_at = None
        self._busy_deadline = None
        self.status = SandboxStatus.DESTROYED

    # ── internals ──

    def _resolve_container_name(self, skill: SkillManifest) -> str:
        normalized_skill_name = "".join(
            char.lower() if char.isalnum() else "-" for char in skill.name
        ).strip("-") or "skill"
        scope = hashlib.sha256(
            f"{self.sandbox_id}:{normalized_skill_name}".encode()
        ).hexdigest()[:10]
        raw = f"{self.config.container_prefix}{normalized_skill_name}-{scope}"
        # Docker container names: [a-zA-Z0-9][a-zA-Z0-9_.-], max 63 chars
        safe = "".join(c if c.isalnum() or c in "_.-" else "-" for c in raw)
        return safe[:63]

    async def _ensure_container(
        self, skill: SkillManifest, env: dict[str, str],
    ) -> None:
        """Create (or reuse) the Docker container."""
        state = await asyncio.to_thread(_container_state, self.container_name)

        reusable = state["exists"] and state["running"]
        if reusable:
            logger.info("Reusing running container %s", self.container_name)
            return

        if state["exists"]:
            await asyncio.to_thread(
                _run_docker,
                ["docker", "rm", "-f", "-v", self.container_name],
                allow_failure=True,
            )

        if not await asyncio.to_thread(_image_exists_locally, self.config.image):
            raise DockerUnavailable(_missing_image_message(self.config.image), code="image_unavailable")

        create_args = self._build_create_args(env)
        await asyncio.to_thread(_run_docker, create_args)
        await asyncio.to_thread(
            _run_docker, ["docker", "start", self.container_name],
        )
        logger.info("Created and started container %s", self.container_name)
        await self.start_watchdog()

    def _build_create_args(self, env: dict[str, str]) -> list[str]:
        cfg = self.config
        # Reap orphaned browser and worker processes in long-lived sandboxes.
        args = ["docker", "create", "--init", "--name", self.container_name]

        # Security hardening
        if cfg.read_only_root:
            args.append("--read-only")
        # Every writable tmpfs is bounded: tmpfs pages are charged to the
        # container's memory limit, so an unbounded one would let a single
        # command's files push other commands into the OOM killer.
        for t in cfg.tmpfs:
            if ":" in t:
                args.extend(["--tmpfs", t])
            else:
                args.extend(["--tmpfs", f"{t}:size={getattr(cfg, 'tmp_tmpfs_size', '') or '64m'}"])
        # Skills live side by side under /skills (see SKILLS_ROOT).
        # mode=1777 ensures any user (including non-root sandbox user) can write.
        args.extend(
            [
                "--tmpfs",
                f"{SKILLS_ROOT}:exec,size={cfg.workdir_tmpfs_size},mode=1777",
            ]
        )
        # Shared space of the user's work: each conversation or task has its
        # own area /workspace/c/<area>, its agents subdirectories of it.
        if self._mounts_shared_workspace():
            args.extend(
                [
                    "--tmpfs",
                    f"{SHARED_WORKSPACE_ROOT}:exec,size={cfg.workspace_tmpfs_size},mode=1777",
                ]
            )
        # A runner-configured workdir outside the layout (a dedicated pool's
        # runner, e.g. /work for the external agent): writable, and where the
        # container starts.
        runner_workdir = self.runner_workdir()
        if runner_workdir is not None:
            args.extend(
                ["--tmpfs", f"{runner_workdir}:exec,size={cfg.workdir_tmpfs_size},mode=1777"]
            )
        # Runner-owned control files (watchdog, kill verdicts).
        args.extend(["--tmpfs", f"{CONTROL_DIR}:size=1m,mode=1777"])
        # pip cache needs /root writable (root user installs).
        args.extend(["--tmpfs", "/root:exec,size=32m"])
        # npm / sandbox user home: npm cache, .npm config, office profiles.
        home_size = getattr(cfg, "home_tmpfs_size", "") or "128m"
        args.extend(["--tmpfs", f"/home/sandbox:exec,size={home_size},uid=1000,gid=1000,mode=0755"])

        network_args: list[str] | None = None
        cap_drop = list(cfg.cap_drop)
        if network_args is None:
            network_args = ["--network", cfg.network]
            if cfg.network != "none":
                for dns_server in cfg.dns:
                    network_args.extend(["--dns", dns_server])
        args.extend(network_args)
        for cap in cap_drop:
            args.extend(["--cap-drop", cap])
        args.extend(["--security-opt", "no-new-privileges"])

        if cfg.memory:
            args.extend(["--memory", cfg.memory])
            # No swap: a command over its memory is killed (and reported)
            # at once instead of thrashing the host for every other sandbox.
            args.extend(["--memory-swap", cfg.memory])
        if cfg.cpus > 0:
            args.extend(["--cpus", str(cfg.cpus)])
        if cfg.pids_limit > 0:
            args.extend(["--pids-limit", str(cfg.pids_limit)])

        for key, val in env.items():
            args.extend(["--env", f"{key}={val}"])

        for volume in getattr(cfg, "volumes", []) or []:
            args.extend(["--volume", volume])
        # Runner-owned, disk-backed install dir (see SANDBOX_LOCAL_DIR).
        args.extend(["--mount", _local_disk_mount(cfg.service_scope, self.sandbox_id)])

        args.extend(["--label", f"sandbox.service={cfg.service_scope}"])
        args.extend(["--label", f"sandbox.id={self.sandbox_id}"])
        if self._config_hash:
            args.extend(["--label", f"sandbox.configHash={self._config_hash}"])

        args.extend(["--workdir", runner_workdir or SKILLS_ROOT])
        args.extend([cfg.image, "sleep", "infinity"])
        return args


    async def _inject_files(
        self,
        skill: SkillManifest,
        *,
        slot: str | None = None,
        clear_existing: bool = True,
    ) -> None:
        """
        Copy a Skill's directory into its own ``/skills/<slot>``.

        A different Skill never shares the directory, so only a reload of
        the same Skill finds files there: it overlays the packaged files and
        keeps session-owned directories such as ``projects/`` so a later turn
        can continue the project. ``clear_existing`` empties the directory
        first (a Skill loaded fresh).
        """
        slot = slot or skill_slot(skill.name)
        target = skill_dir(slot)
        prepare = f"mkdir -p {shlex.quote(target)}"
        if clear_existing:
            prepare += f" && find {shlex.quote(target)} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +"
        await self._docker(
            ["docker", "exec", self.container_name, "sh", "-c", prepare],
            timeout=_FILE_INJECT_TIMEOUT,
        )
        entries = await asyncio.to_thread(os.listdir, skill.skill_dir)
        if not entries:
            return
        archive = await asyncio.to_thread(_tar_skill_directory, skill.skill_dir, sorted(entries), slot)
        result = await self._docker(
            [
                "docker", "exec", "-i", self.container_name,
                "tar", "xf", "-", "-C", target,
                "--no-same-owner", "--no-same-permissions",
            ],
            timeout=_FILE_INJECT_TIMEOUT,
            input_data=archive,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"File injection failed (exit {result.returncode}): {stderr}")
        await self._docker(
            [
                "docker", "exec", self.container_name, "sh", "-c",
                f"find {shlex.quote(target)} -type f"
                r" \( -name '*.sh' -o -name '*.bash' -o -name '*.py' \)"
                r" -exec chmod +x {} +",
            ],
            timeout=_FILE_INJECT_TIMEOUT,
        )
        logger.info("Injected skill files into %s:%s", self.container_name, target)

    async def materialize_builtin_skill(
        self,
        name: str,
        digest: str,
        target: str,
        *,
        replace: bool = True,
    ) -> None:
        """Make the image's baked copy of built-in Skill ``name`` available at ``target``.

        The one place a baked Skill reaches a container, for whatever layout
        the caller uses: the Skill's own ``/skills/<slot>`` (``skill_dir``),
        where Skills live side by side and an upload would put its files. Nothing crosses the
        network and nothing is read on the host: one ``docker exec`` copies
        the Skill's files from the image into ``target`` as real, writable
        files, except the subtrees the Skill declared read-only
        (``sandbox_read_only_paths``), which become symlinks into the image so
        bulky assets (pptx's 11k icons) never fill the memory-backed tmpfs.

        ``replace`` clears ``target`` first; ``replace=False`` overlays the
        packaged files and keeps everything else (a same-Skill reload keeps
        the session's work). Raises ``BakedSkillUnavailable`` without
        touching ``target`` when the container's image lacks ``digest``.
        """
        validate_skill_name(name)
        if not digest or not all(c in "0123456789abcdef" for c in digest):
            raise InvalidRequest(f"Invalid baked Skill digest for {name!r}")
        if not target.startswith("/") or ".." in target.split("/"):
            raise InvalidRequest(f"Invalid baked Skill target {target!r}")
        result = await self._docker(
            [
                "docker", "exec", self.container_name,
                "python3", "-c", MATERIALIZE_PROGRAM,
                BAKED_SKILLS_ROOT, name, digest, target, "1" if replace else "0",
            ],
            timeout=_FILE_INJECT_TIMEOUT,
        )
        if result.returncode == BAKED_SKILL_MISMATCH_EXIT:
            raise BakedSkillUnavailable(
                f"The sandbox image does not hold built-in Skill {name!r} at digest {digest[:12]}."
            )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Materializing built-in Skill {name!r} failed (exit {result.returncode}): "
                f"{bounded_text(stderr, 2000)}"
            )
        logger.info("Materialized baked Skill %s (%s) into %s:%s", name, digest[:12], self.container_name, target)

    def is_idle(self, threshold: int = 30) -> bool:
        """Return True if the sandbox is ready and has been inactive for at least `threshold` seconds."""
        if self.status != SandboxStatus.READY:
            return False
        return (time.time() - self.last_used_at) > threshold

    def _load_budget_seconds(self) -> float:
        # File injection (clear, copy, chmod) plus pip and npm installs.
        return 3 * _FILE_INJECT_TIMEOUT + 2 * float(self.config.install_timeout)

    async def load_skill(
        self,
        skill_name: str,
        files: dict[str, str],
        auto_install: bool = True,
        idle_threshold: int = 30,
        env: dict[str, str] | None = None,
        baked: BakedSkill | None = None,
    ) -> "SkillManifest":
        """
        Load a skill into its own ``/skills/<slot>`` of this sandbox. All or nothing.

        Skills coexist: loading one claims only its own directory, so it runs
        next to other Skills' commands and is busy (``SandboxBusy``) only
        while commands of the same Skill run or it is already loading.
        ``idle_threshold`` is accepted for compatibility and ignored: no other
        Skill's files are touched. ``env`` replaces this Skill's env for later
        commands that see it (``None`` or ``{}`` clears it). ``baked``
        materializes the image's copy of a built-in Skill instead of the files.

        The Skill and its env are committed together only on success. A
        failure before the container is touched leaves everything as it was;
        a failure after removes this Skill's half-written directory (the
        sandbox and the other Skills keep serving); only when even that fails
        is the sandbox quarantined, so a half-loaded Skill is never served.
        """
        import shutil
        import tempfile

        del idle_threshold  # loads never touch another Skill's files
        validate_skill_name(skill_name)
        slot = skill_slot(skill_name)
        self.claim_skill_load(slot, self._load_budget_seconds())

        container_touched = False
        tmp_dir = ""
        try:
            # Temp-dir writes and the scan are blocking filesystem work; a
            # stalled event loop starves /health and gets the pod killed.
            tmp_dir = await asyncio.to_thread(tempfile.mkdtemp, prefix="sbx-load-")
            if baked is not None:
                await asyncio.to_thread(baked.write_root_texts, tmp_dir)
                new_skill = await asyncio.to_thread(baked.manifest, skill_name, tmp_dir)
            else:
                await asyncio.to_thread(write_skill_files, tmp_dir, files)

                from .scanner import SkillScanner
                new_skill = await asyncio.to_thread(SkillScanner().scan, tmp_dir)
                new_skill = new_skill.model_copy(update={"name": skill_name, "skill_dir": tmp_dir})
            new_env = dict(env or {})

            same_skill = self.slot_manifest(slot) is not None
            if baked is not None:
                container_touched = True
                try:
                    await self.materialize_builtin_skill(
                        baked.name, baked.digest, skill_dir(slot), replace=not same_skill,
                    )
                except BakedSkillUnavailable:
                    # Checked before anything is touched: the sandbox is
                    # exactly as it was and the caller sends the files instead.
                    container_touched = False
                    raise
            else:
                container_touched = True
                await self._inject_files(
                    new_skill,
                    slot=slot,
                    clear_existing=not same_skill,
                )

            if auto_install and (new_skill.requirements_txt or new_skill.package_json):
                result = await self._install_dependencies(new_skill, env=new_env, slot=slot)
                if result.exit_code != 0:
                    raise DependencyInstallFailed(
                        f"dependency install failed (exit {result.exit_code}):\n"
                        f"{bounded_text(result.stderr, 8000)}"
                    )

            # Commit: the Skill and its env become visible together.
            self._slots[slot] = new_skill
            self._slot_envs[slot] = new_env
            self.skill = new_skill
            self._skill_env = new_env
            logger.info(
                "Loaded skill '%s' into sandbox %s at %s", skill_name, self.sandbox_id, skill_dir(slot),
            )
            return new_skill
        except BaseException as exc:
            if container_touched and not self._lost:
                await self._discard_slot(slot, reason=f"load_skill {skill_name!r} failed: {exc}")
            if not container_touched or isinstance(exc, (RunnerError, asyncio.CancelledError)):
                raise
            raise LoadSkillFailed(f"Loading skill {skill_name!r} failed: {exc}") from exc
        finally:
            self.release_skill_load(slot)
            if tmp_dir:
                await asyncio.to_thread(shutil.rmtree, tmp_dir, ignore_errors=True)

    async def _discard_slot(self, slot: str, *, reason: str) -> None:
        """Forget a Skill whose load failed half-way and remove its directory.

        If the directory cannot be removed, the sandbox is quarantined: a
        half-loaded Skill must never be served.
        """
        self._slots.pop(slot, None)
        self._slot_envs.pop(slot, None)
        if self.skill is not None and skill_slot(self.skill.name) == slot:
            self.skill = next(reversed(self._slots.values()), None) if self._slots else None
            self._skill_env = self.slot_env(skill_slot(self.skill.name)) if self.skill else {}
        try:
            result = await self._docker(
                ["docker", "exec", self.container_name, "rm", "-rf", "--", skill_dir(slot)],
                timeout=_FILE_INJECT_TIMEOUT,
            )
            if result.returncode == 0:
                logger.warning("Sandbox %s: discarded Skill %s (%s)", self.sandbox_id, slot, reason)
                return
        except SandboxGone:
            raise
        except BaseException:  # noqa: BLE001 - quarantined below
            logger.warning("Could not remove Skill %s from sandbox %s", slot, self.sandbox_id, exc_info=True)
        self._quarantine(reason)

    async def _install_dependencies(
        self,
        skill: SkillManifest,
        *,
        env: dict[str, str] | None = None,
        slot: str | None = None,
    ) -> ExecResponse:
        """Install the Skill's dependencies, reusing what the image preinstalled.

        A manifest the image already holds (``PREINSTALLED_SKILL_DEPS_ROOT``)
        needs no create-time install: its Python packages are in the system
        site-packages and its node_modules is linked into the workdir. Any
        other manifest is installed with pip / npm as before.
        """
        needs_pip = bool(skill.requirements_txt)
        needs_npm = bool(skill.package_json)
        root = skill_dir(slot or skill_slot(skill.name))
        if needs_pip and await self._python_requirements_preinstalled(skill, env=env, root=root):
            needs_pip = False
        if needs_npm and await self._link_preinstalled_node_modules(skill, env=env, root=root):
            needs_npm = False

        if not needs_pip and not needs_npm:
            return ExecResponse(stdout="", stderr="", exit_code=0)

        combined_stdout: list[str] = []
        combined_stderr: list[str] = []
        last_exit_code = 0

        async def _install(command: str, workdir: str) -> ExecResponse:
            try:
                return await self._run_in_container(
                    command,
                    timeout=self.config.install_timeout,
                    workdir=workdir,
                    env=env,
                )
            except CommandTimeout as exc:
                raise CommandTimeout(
                    f"Dependency install timed out after {self.config.install_timeout}s",
                    code="install_timeout",
                    extra=exc.extra,
                ) from None

        if needs_pip:
            req_path = f"{root}/{skill.requirements_txt}"
            logger.info("install_dependencies: pip install from %s", req_path)
            result = await _install(
                f"pip install --no-cache-dir --root-user-action=ignore -r {req_path}",
                root,
            )
            if result.stdout:
                combined_stdout.append(result.stdout)
            if result.stderr:
                combined_stderr.append(result.stderr)
            last_exit_code = result.exit_code
            logger.info(
                "install_dependencies: pip install exit_code=%d", result.exit_code,
            )
            if result.exit_code != 0:
                return ExecResponse(
                    stdout="\n".join(combined_stdout),
                    stderr="\n".join(combined_stderr),
                    exit_code=result.exit_code,
                )

        if needs_npm:
            pkg_dir = f"{root}/{skill.package_json}".rsplit("/", 1)[0]
            logger.info(
                "install_dependencies: npm install in %s", pkg_dir,
            )
            result = await _install("npm install --no-fund --no-audit", pkg_dir)
            if result.stdout:
                combined_stdout.append(result.stdout)
            if result.stderr:
                combined_stderr.append(result.stderr)
            last_exit_code = result.exit_code
            logger.info(
                "install_dependencies: npm install exit_code=%d", result.exit_code,
            )

        return ExecResponse(
            stdout="\n".join(combined_stdout),
            stderr="\n".join(combined_stderr),
            exit_code=last_exit_code,
        )

    async def _python_requirements_preinstalled(
        self,
        skill: SkillManifest,
        *,
        env: dict[str, str] | None = None,
        root: str | None = None,
    ) -> bool:
        digest = _host_manifest_digest(skill.skill_dir, skill.requirements_txt or "")
        if digest is None:
            return False
        marker = f"{PREINSTALLED_SKILL_DEPS_ROOT}/python/{digest}.txt"
        result = await self._run_in_container(
            f"test -f {shlex.quote(marker)}",
            timeout=30,
            workdir=root or skill_dir(skill_slot(skill.name)),
            env=env,
        )
        if result.exit_code != 0:
            return False
        logger.info(
            "install_dependencies: requirements %s preinstalled in the image (%s)",
            skill.requirements_txt, digest,
        )
        return True

    async def _link_preinstalled_node_modules(
        self,
        skill: SkillManifest,
        *,
        env: dict[str, str] | None = None,
        root: str | None = None,
    ) -> bool:
        """Link the image's node_modules for this package.json into the workdir.

        ``node_modules`` and its ``@scope`` / ``.bin`` directories are real
        writable directories holding one symlink per package, so an entry the
        Skill installed itself is kept and npm can rename or remove links
        without writing into the read-only tree. Node and bundlers resolve the
        links to their real paths, where the hoisted dependencies live.
        """
        digest = _host_manifest_digest(skill.skill_dir, skill.package_json or "")
        if digest is None:
            return False
        source = f"{PREINSTALLED_SKILL_DEPS_ROOT}/node/{digest}/node_modules"
        root = root or skill_dir(skill_slot(skill.name))
        package_json = f"{root}/{skill.package_json}"
        target = package_json.rsplit("/", 1)[0] + "/node_modules"
        script = (
            f"src={shlex.quote(source)}; dst={shlex.quote(target)}; "
            '[ -d "$src" ] || exit 64; '
            'link() { [ -e "$2" ] || [ -L "$2" ] || ln -s "$1" "$2"; }; '
            'mkdir -p "$dst" || exit 1; '
            'for entry in "$src"/* "$src"/.bin; do '
            '[ -e "$entry" ] || continue; '
            'name="$(basename "$entry")"; '
            'case "$name" in '
            '@*|.bin) mkdir -p "$dst/$name" || exit 1; '
            'for child in "$entry"/*; do '
            '[ -e "$child" ] || [ -L "$child" ] || continue; '
            'link "$child" "$dst/$name/$(basename "$child")" || exit 1; '
            "done ;; "
            '*) link "$entry" "$dst/$name" || exit 1 ;; '
            "esac; "
            "done"
        )
        result = await self._run_in_container(
            script,
            timeout=30,
            workdir=root,
            env=env,
        )
        if result.exit_code != 0:
            if result.exit_code != 64:
                logger.warning(
                    "install_dependencies: linking preinstalled node_modules failed "
                    "(exit %d): %s", result.exit_code, result.stderr[-500:],
                )
            return False
        logger.info(
            "install_dependencies: %s linked to the image's node_modules (%s)",
            skill.package_json, digest,
        )
        return True
