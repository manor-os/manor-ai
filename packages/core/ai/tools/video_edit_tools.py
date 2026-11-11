"""Interactive, isolated video authoring, review, and render orchestration.

Video Edit keeps one owned Sandbox alive across asset exchange, project
authoring, review, revision, and approval-gated rendering.  Editable source is
incrementally synchronized back to Manor's durable entity filesystem.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import functools
import hashlib
import json
import logging
import math
import os
import posixpath
import re
import shlex
import shutil
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from jsonschema import Draft202012Validator

from packages.core.ai.runtime.video_edit_sessions import (
    append_video_edit_session_event,
    assert_video_edit_session_owner,
    close_video_edit_session,
    load_closed_video_edit_session,
    load_owner_video_edit_session,
    make_current_video_edit_session,
    load_video_edit_session,
    new_video_edit_session,
    current_video_edit_session_id,
    save_video_edit_session,
    video_edit_owner_key,
    video_edit_session_events,
)
from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.services.generated_file_refs import parse_entity_fs_url

logger = logging.getLogger(__name__)

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
# The runtime regenerates hyperframes.json from video.json before every check
# and render, and its manifest never syncs it: it is not source. The hash, the
# sandbox manifest (video_runtime.py) and the uploads must agree on one set,
# or a durable copy hashes differently from the sandbox that was reviewed.
_HASH_EXCLUDED_FILES = {"hyperframes.json"}
_MAX_REFERENCE_BYTES = 1_000_000_000
_MAX_SANDBOX_PROJECT_BYTES = 1_500_000_000
_MAX_SANDBOX_OUTPUT_BYTES = 512 * 1024 * 1024
_SANDBOX_WORKDIR_SIZE = "2g"
# Video sandboxes ask for these ceilings everywhere and the runner fits them
# to its host: memory above its per-container maximum and CPUs above its CPU
# count are clamped (Docker refuses ``--cpus`` above the host's count, which
# failed every start on 1-2 vCPU runners). A stable ask also keeps a warm,
# clamped session container reusable. Render workers follow the CPUs the
# container actually got.
_SANDBOX_MEMORY = "4g"
_SANDBOX_CPUS = 4.0
# Runners that report neither the container's CPUs nor their host's get the
# smallest deployed runner's share.
_SANDBOX_FALLBACK_CPUS = 1.0
_SANDBOX_RENDER_TIMEOUT = 1800
_SANDBOX_UPLOAD_CONCURRENCY = 2
# A Sandbox Service restart (deploy, or a liveness restart on a loaded node)
# refuses connections for tens of seconds and drops sandboxes it could not
# drain. Retry-safe steps wait this long for it before reporting the runtime
# unavailable.
_SANDBOX_RESTART_RETRY_WINDOW_SECONDS = 180.0
_SANDBOX_RESTART_RETRY_INITIAL_DELAY_SECONDS = 2.0
_SANDBOX_RESTART_RETRY_MAX_DELAY_SECONDS = 20.0
# Indirection so tests can drive the retry window without waiting.
_restart_retry_clock = time.monotonic
_restart_retry_sleep = asyncio.sleep


_SANDBOX_PIDS_LIMIT = 512
# The warm-registry sub-scope of the authoring session: one dedicated session
# container per owner (the user; entity × task for a user-less run), apart from
# the owner's general sandbox.
VIDEO_SESSION_REUSE_SCOPE = "video-edit"
_VIDEO_RUNTIME_SKILL = "video-edit-runtime"
_VIDEO_SANDBOX_ENV = {
    "NO_COLOR": "1",
    "HYPERFRAMES_BROWSER_PATH": "/usr/bin/chromium",
    "HYPERFRAMES_GSAP_PATH": "/usr/local/lib/node_modules/gsap/dist/gsap.min.js",
}


def _video_sandbox_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "network": "none",
        "memory": _SANDBOX_MEMORY,
        "cpus": _SANDBOX_CPUS,
        "pids_limit": _SANDBOX_PIDS_LIMIT,
        "workdir_tmpfs_size": _SANDBOX_WORKDIR_SIZE,
        "exec_timeout": _SANDBOX_RENDER_TIMEOUT,
        **(overrides or {}),
    }


# A render in the render pool asks for a container sized to its composition.
# Calibrated on a measured run: 109 s of 1080x1920 at 4 workers peaked at
# 10.7 GB and took 7.5 min on 8 vCPU.
_RENDER_MAX_WORKERS = 4
_RENDER_PIDS_LIMIT = 1536  # the runtime budgets 256 PIDs per browser worker
_RENDER_BASE_MEMORY_MB = 2048
_RENDER_MEMORY_MB_PER_1080P_SECOND = 80
_RENDER_MIN_MEMORY_MB = 4096


def _project_frame_pixels(project_dir: Path) -> int:
    try:
        text = (project_dir / "index.html").read_text(encoding="utf-8")
    except OSError:
        return 1080 * 1920
    sizes = [
        re.search(rf"\bdata-{name}\s*=\s*[\"']([0-9]+)[\"']", text) for name in ("width", "height")
    ]
    if not all(sizes):
        return 1080 * 1920
    return max(1, int(sizes[0].group(1)) * int(sizes[1].group(1)))


def _render_size_request(project_dir: Path) -> dict[str, Any]:
    """Container memory/CPUs/PIDs/work area for rendering this composition in the render pool."""

    from packages.core.config import get_settings

    ceiling = max(_RENDER_MIN_MEMORY_MB, int(getattr(get_settings(), "VIDEO_RENDER_MAX_MEMORY_MB", 14336) or 14336))
    scale = _project_frame_pixels(project_dir) / (1080 * 1920)
    need = _RENDER_BASE_MEMORY_MB + _RENDER_MEMORY_MB_PER_1080P_SECOND * _project_duration_seconds(project_dir) * scale
    memory_mb = int(min(ceiling, max(_RENDER_MIN_MEMORY_MB, need)))
    return {
        "memory": f"{memory_mb}m",
        "cpus": 8.0 if memory_mb > 8192 else 4.0,
        "pids_limit": _RENDER_PIDS_LIMIT,
        # The project, extracted frames and encoder output share the tmpfs work
        # area, whose pages count against the memory limit anyway (the memory
        # need was measured with them there). The default 2g ran out at frame
        # 3120/3274 of a 109 s 1080p episode (staging 2026-10-20, ENOSPC).
        "workdir_tmpfs_size": f"{memory_mb}m",
    }


async def _render_runner_pool() -> str:
    """Use the default pool, or a serving Cloud render pool when configured."""

    from packages.core.services.sandbox_queue_service import DEFAULT_RUNNER_POOL

    return DEFAULT_RUNNER_POOL


def _render_workers_for(cpus: float) -> int:
    """Browser workers for a render container with ``cpus``.

    Small runners keep one per CPU; larger containers run one per two CPUs, so
    the encoder keeps up (measured: 8 vCPU / 4 workers beat 8 workers on memory).
    """

    if cpus < 4:
        return max(1, int(cpus))
    return min(_RENDER_MAX_WORKERS, int(cpus) // 2)


class VideoSandboxParked(Exception):
    """The durable run was parked on a sandbox reservation.

    Raised from wherever an operation needed a container; the handler returns
    ``suspension`` unchanged, and the runtime re-executes the same call once
    the scheduler allocated the container.
    """

    def __init__(self, suspension: Any) -> None:
        super().__init__("video sandbox reservation queued")
        self.suspension = suspension


def _video_binding_key(context: Any) -> str:
    return str(getattr(context, "sandbox_binding_key", None) or getattr(context, "conversation_id", None) or "")


def _video_owner_key(context: Any) -> str:
    return video_edit_owner_key(
        entity_id=getattr(context, "entity_id", None),
        user_id=getattr(context, "user_id", None),
        task_key=_video_binding_key(context),
    )


def _video_exec_queue(context: Any) -> dict[str, Any]:
    """Fair-queue parameters of a command in a Video Edit container.

    The authoring session is the user's, shared by all of their conversations
    and agents: a command that finds every slot taken waits its turn (fair
    across the user's work areas) instead of failing busy, and always sees
    the video runtime Skill as /skill.
    """

    from packages.core.services.sandbox_session_service import sandbox_exec_queue_params

    work_area = _video_binding_key(context) if context is not None else None
    return sandbox_exec_queue_params(work_area=work_area or None, skill=_VIDEO_RUNTIME_SKILL)


def _video_sandbox_scope(context: Any) -> tuple[Any, str]:
    """The owner and binding key every Video Edit container is admitted under.

    The caller's sandbox scope (``primary_sandbox_scope_for``, the one
    resolver every sandbox consumer shares): per user for human work -- the
    authoring session is the user's dedicated ``user:video-edit`` role
    container, whichever conversation or agent admits it -- and per task or
    workflow run (in its workspace) for user-less work. Conversation and agent
    are attribution only.
    """

    from packages.core.ai.runtime.sandbox import primary_sandbox_scope_for
    from packages.core.services.sandbox_session_service import SandboxOwner

    scope = primary_sandbox_scope_for(context)
    if scope is None or not _video_owner_key(context):
        raise PermissionError("Video Edit sandboxes need an entity and a user (or, user-less, a task run).")
    owner = SandboxOwner(entity_id=scope.entity_id, user_id=scope.user_id, workspace_id=scope.workspace_id)
    return owner, scope.binding_key


def _video_sandbox_identity(
    context: Any,
    *,
    lifetime: Any,
    purpose: str,
    pool: str | None = None,
    config_overrides: dict[str, Any] | None = None,
):
    """The owner, bundle and binding of one Video Edit container."""

    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxBundle,
    )

    owner, scope_key = _video_sandbox_scope(context)
    bundle = SandboxBundle(
        skill_name=_VIDEO_RUNTIME_SKILL,
        files=_video_sandbox_bundle(),
        env=dict(_VIDEO_SANDBOX_ENV),
        source="builtin",
        config=_video_sandbox_config(config_overrides),
        auto_install=False,
        reuse_scope=VIDEO_SESSION_REUSE_SCOPE if purpose == "video-session" else None,
        lifetime=lifetime,
        purpose=purpose,
        # The image's baked copy when the runner holds this version; the
        # runtime files above are uploaded otherwise.
        baked_skill=_video_runtime_baked_skill(),
        **({"pool": pool} if pool else {}),
    )
    binding = SandboxBinding(
        # The binding key: the session admission maps it to the user's role
        # container (or, user-less, this task's own).
        conversation_id=scope_key or None,
        # Not per agent or conversation: the session is the user's.
        agent_id="",
        runtime_run_id=getattr(context, "runtime_run_id", None) or None,
        runtime_tool_call_id=getattr(context, "runtime_tool_call_id", None) or None,
        task_id=getattr(context, "task_id", None) or None,
        workflow_run_id=getattr(context, "workflow_run_id", None) or None,
        parent_skill=getattr(context, "parent_skill", None) or None,
    )
    return owner, bundle, binding


def _is_container_start_race(message: str) -> bool:
    detail = str(message or "").casefold()
    return "file injection failed" in detail and any(
        marker in detail for marker in ("is not running", "no such container")
    )


async def _admit_video_sandbox(
    context: Any,
    *,
    session: bool,
    pool: str | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> Any:
    """Admit a Video Edit container through the sandbox session service.

    ``session`` admits the authoring session (SESSION: replaces whatever this
    scope registered before); otherwise a ONE_SHOT render container the
    caller discards. Raises ``VideoSandboxParked`` when the durable run was
    parked on a reservation and ``VideoEditRuntimeUnavailable`` for any
    refusal.
    """

    from packages.core.services.sandbox_session_service import (
        SandboxLifetime,
        SandboxSession,
        SandboxSessionError,
        admit_sandbox,
    )

    owner, bundle, binding = _video_sandbox_identity(
        context,
        lifetime=SandboxLifetime.SESSION if session else SandboxLifetime.ONE_SHOT,
        purpose="video-session" if session else "video-render",
        pool=pool,
        config_overrides=config_overrides,
    )
    for attempt in range(2):
        admitted = await admit_sandbox(owner=owner, bundle=bundle, binding=binding)
        if isinstance(admitted, SandboxSession):
            return admitted
        if not isinstance(admitted, SandboxSessionError):
            raise VideoSandboxParked(admitted)
        if attempt == 0 and admitted.code == "create_failed" and _is_container_start_race(admitted.message):
            # No project work ran yet. A one-time fresh container is safe
            # when Docker stopped the container during initial injection.
            await asyncio.sleep(0.25)
            continue
        raise VideoEditRuntimeUnavailable(
            f"Video editing sandbox could not start: {admitted.message}",
            code=admitted.code,
            retryable=admitted.retryable,
        )
    raise AssertionError("unreachable")


def _video_sandbox_cpus(container_config: Any, health: Any, ceiling: float = _SANDBOX_CPUS) -> float:
    """The CPUs a video container got: its effective config, else its runner's host.

    The runner clamps the ``_SANDBOX_CPUS`` ask to its host and reports the
    effective config on create; a runner that does not reports ``host_cpus``
    on ``/health``; one that reports neither gets the smallest runner's share.
    """

    for source, key in ((container_config, "cpus"), (health, "host_cpus")):
        if not isinstance(source, dict):
            continue
        try:
            cpus = float(source.get(key) or 0)
        except (TypeError, ValueError):
            cpus = 0.0
        if cpus > 0:
            return min(ceiling, cpus)
    return _SANDBOX_FALLBACK_CPUS


async def _granted_render_workers(admitted: Any, client: Any, cpu_ceiling: float = _SANDBOX_CPUS) -> int:
    """Browser workers for the CPUs the render container got (see _render_workers_for)."""

    container_config = getattr(admitted, "container_config", None)
    health = None
    if not (isinstance(container_config, dict) and container_config.get("cpus")):
        try:
            health = await client.health()
        except Exception as exc:  # noqa: BLE001 - unknown size = smallest runner
            logger.info("video sandbox capacity unknown, using smallest runner size: %s", exc)
    return _render_workers_for(_video_sandbox_cpus(container_config, health, cpu_ceiling))


async def _discard_video_session_sandbox(context: Any, sandbox_id: str) -> str:
    """Destroy an authoring session container this caller owns."""

    from packages.core.services.sandbox_session_service import (
        SandboxLifetime,
        discard_session_sandbox,
    )

    if not sandbox_id:
        return "destroy_failed"
    owner, bundle, binding = _video_sandbox_identity(
        context, lifetime=SandboxLifetime.SESSION, purpose="video-session",
    )
    return await discard_session_sandbox(owner=owner, bundle=bundle, binding=binding, sandbox_id=sandbox_id)


async def _video_sandbox_client(sandbox_id: str, *, timeout: float):
    """A client for the runner hosting this Video Edit container (routed).

    Every video-edit call reaches its container through the session service,
    which routes it to its runner and attaches the sandbox API token.
    """

    from packages.core.services.sandbox_session_service import sandbox_client_for

    return await sandbox_client_for(sandbox_id, timeout=timeout)


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
_LOCAL_GSAP_PATH = "assets/vendor/gsap.min.js"
_VIDEO_AUTHORING_CONTRACT_VERSION = "video-edit-authoring-v1"
_MOTION_ASSERTION_SCHEMAS = {
    "appearsBy": {
        "required": ["selector", "bySec"],
        "properties": {"selector": {"type": "string", "minLength": 1}, "bySec": {"type": "number", "minimum": 0}},
    },
    "before": {
        "required": ["a", "b"],
        "properties": {"a": {"type": "string", "minLength": 1}, "b": {"type": "string", "minLength": 1}},
    },
    "staysInFrame": {
        "required": ["selector"],
        "properties": {"selector": {"type": "string", "minLength": 1}},
    },
    "keepsMoving": {
        "required": [],
        "properties": {"withinSelector": {"type": "string", "minLength": 1, "not": {"const": "*"}}, "maxStaticSec": {"type": "number", "exclusiveMinimum": 0}},
    },
}
_MOTION_METADATA_SCHEMA = {
    "type": "object",
    "properties": {"version": {"const": 1}, "duration": {"type": "number", "exclusiveMinimum": 0}},
}
_MINIMAL_MOTION_SPEC = {"duration": 4, "assertions": [{"kind": "staysInFrame", "selector": "#headline"}]}
_MINIMAL_COMPOSITION_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{margin:0;width:1280px;height:720px;overflow:hidden}
#demo{position:relative;width:1280px;height:720px;background:#142522;color:#fff}
.clip{position:absolute;inset:0;display:grid;place-items:center;padding:80px;box-sizing:border-box}
#headline{margin:0;font:64px/1.2 sans-serif;max-width:1000px;text-align:center}
</style></head><body>
<main id="demo" data-composition-id="demo" data-start="0" data-duration="4" data-fps="30" data-width="1280" data-height="720">
<section id="scene-1" class="clip" data-start="0" data-duration="4" data-track-index="0"><h1 id="headline">Your video content</h1></section>
</main>
<script src="./assets/vendor/gsap.min.js"></script>
<script>
window.__timelines = window.__timelines || {};
const tl = gsap.timeline({paused:true});
tl.to('#headline',{x:24,duration:4,ease:'none'},0);
window.__timelines['demo'] = tl;
</script></body></html>"""


def _video_authoring_contract() -> dict[str, Any]:
    """Return the minimum deterministic project contract for direct tool users."""
    return {
        "version": _VIDEO_AUTHORING_CONTRACT_VERSION,
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
            "sub_compositions": (
                "Each sub-composition mounted with data-composition-src registers its own paused "
                "timeline at window.__timelines[<its data-composition-id>] inside its <template>. "
                "A sub-composition animated only from the root timeline (or CSS) instead marks its "
                "host element with data-no-timeline. Otherwise the render waits for a timeline that "
                "never arrives and refuses to export (sub_timeline_readiness_timeout)."
            ),
        },
        "motion_assertions": {
            "purpose": (
                "Motion sidecars contain validation assertions, not playback tracks/keyframes. "
                "Author playback in index.html's GSAP timeline. Every existing root *.motion.json "
                "is checked; adding a new sidecar does not fix an invalid old one."
            ),
            "preferred_file": "index.motion.json",
            "supported_kinds": list(_MOTION_ASSERTION_SCHEMAS),
            "schema": {
                "type": "object",
                "required": ["assertions"],
                "properties": {**_MOTION_METADATA_SCHEMA["properties"], "assertions": {"type": "array", "minItems": 1, "items": {
                    "oneOf": [
                        {"type": "object", "required": ["kind", *schema["required"]],
                         "properties": {"kind": {"const": kind}, **schema["properties"]}}
                        for kind, schema in _MOTION_ASSERTION_SCHEMAS.items()
                    ],
                }}},
            },
            "forbidden_kinds": ["exists"],
            "example": _MINIMAL_MOTION_SPEC,
        },
        "determinism": (
            "Use composition time only: no live clocks, unseeded randomness, infinite repeats, "
            "network requests, or forward-only event callbacks."
        ),
        "minimal_example": {
            "purpose": "Structural example only; preserve and adapt the user's content/assets before review.",
            "index.html": _MINIMAL_COMPOSITION_HTML,
            "index.motion.json": _MINIMAL_MOTION_SPEC,
        },
        "runtime_assets": {
            "local_gsap_path": _LOCAL_GSAP_PATH,
            "provider": "The Sandbox supplies this library during review; add the local script import, not a download.",
        },
        "completion": "contract_valid is static preflight only; strict review, exact-hash approval and rendering are still required.",
    }


class VideoEditRuntimeUnavailable(RuntimeError):
    """Raised when the isolated video-editing runtime cannot be used.

    ``code`` is ``capacity_full`` when admission found no capacity (the
    caller may retry later, ``retryable``), else ``runtime_unavailable``.
    ``transient`` marks a Sandbox Service restart that outlasted the retry
    window (``attempts`` tries over ``retried_seconds``), or one a durable run
    cannot wait out in the same call: the same operation can be retried.
    Otherwise retrying the same call will not help.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "runtime_unavailable",
        retryable: bool = False,
        transient: bool = False,
        attempts: int = 1,
        retried_seconds: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.code = "capacity_full" if code in {"capacity_full", "busy_timeout"} else "runtime_unavailable"
        self.retryable = bool(retryable)
        self.transient = transient
        self.attempts = attempts
        self.retried_seconds = retried_seconds


def _is_sandbox_restart_failure(exc: BaseException, *, lost_sandbox_is_transient: bool) -> bool:
    """Whether ``exc`` (or what it wraps) looks like a Sandbox Service restart.

    Connection refused/reset, a dropped response and 502/503/504 (including
    the draining service refusing new sandboxes) are restart symptoms. A 404
    for the sandbox only counts when the caller created that sandbox for this
    step from durable source: a restart removes it, and a fresh one is
    equivalent. Timeouts other than connect are not: a slow render would just
    time out again.
    """

    import httpx

    from packages.core.services.sandbox_sdk.exceptions import (
        SandboxConnectionError,
        SandboxError,
        SandboxNotFoundError,
    )

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, VideoEditRuntimeUnavailable) and current.transient:
            # An inner step already spent its retry window; do not stack another.
            return False
        if isinstance(
            current,
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadError,
                httpx.WriteError,
                httpx.RemoteProtocolError,
                SandboxConnectionError,
                ConnectionRefusedError,
                ConnectionResetError,
            ),
        ):
            return True
        if isinstance(current, SandboxNotFoundError):
            return lost_sandbox_is_transient
        if isinstance(current, SandboxError) and current.status_code in {502, 503, 504}:
            return True
        current = current.__cause__ or current.__context__
    return False


def _innermost_error_text(exc: BaseException) -> str:
    current = exc
    while current.__cause__ is not None:
        current = current.__cause__
    return str(current) or type(current).__name__


async def _retry_sandbox_restart(
    label: str,
    attempt_once,
    *,
    lost_sandbox_is_transient: bool = False,
    before_retry=None,
    retry_in_process: bool = True,
):
    """Run a retry-safe Sandbox step, waiting out a Sandbox Service restart.

    Only steps that are idempotent end to end may use this (touch/recover a
    session, hash-checked source sync, a render in a sandbox created for the
    call). Non-restart failures propagate unchanged on the first attempt.
    ``retry_in_process=False`` (a step whose container a repeat in this call
    could not replace) reports a restart as transient at once.
    """

    window = _SANDBOX_RESTART_RETRY_WINDOW_SECONDS if retry_in_process else 0.0
    started = _restart_retry_clock()
    delay = _SANDBOX_RESTART_RETRY_INITIAL_DELAY_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            return await attempt_once()
        except Exception as exc:
            if not _is_sandbox_restart_failure(exc, lost_sandbox_is_transient=lost_sandbox_is_transient):
                raise
            elapsed = _restart_retry_clock() - started
            if elapsed + delay > window:
                raise VideoEditRuntimeUnavailable(
                    "Video Edit runtime is temporarily unavailable: the Sandbox Service "
                    f"could not be reached ({_innermost_error_text(exc)}). {label} was "
                    f"tried {attempt} times over {elapsed:.0f}s; the service is most likely "
                    "restarting. Retry the same operation in a few minutes — the project "
                    "source is intact.",
                    transient=True,
                    attempts=attempt,
                    retried_seconds=elapsed,
                ) from exc
            logger.warning(
                "[video_edit] %s hit a Sandbox Service restart symptom (attempt %d, %.0fs): %s; "
                "retrying in %.0fs",
                label,
                attempt,
                elapsed,
                _innermost_error_text(exc),
                delay,
            )
            await _restart_retry_sleep(delay)
            delay = min(delay * 2, _SANDBOX_RESTART_RETRY_MAX_DELAY_SECONDS)
            if before_retry is not None:
                await before_retry()


def _admission_retries_in_process(context: Any) -> bool:
    """Whether a container admitted for this call can be admitted again in it.

    A durable run admits through a ticket keyed by its tool call; admitting
    again within the call re-attaches the container a restart removed, so
    only a new call gets a fresh one.
    """

    from packages.core.services.sandbox_session_service import SandboxBinding, admission_is_durable

    return not admission_is_durable(SandboxBinding(
        runtime_run_id=getattr(context, "runtime_run_id", None) or None,
        runtime_tool_call_id=getattr(context, "runtime_tool_call_id", None) or None,
        parent_skill=getattr(context, "parent_skill", None) or None,
    ))


def _runtime_unavailable_error(exc: VideoEditRuntimeUnavailable) -> str:
    if exc.transient:
        return _json_error(
            str(exc),
            code=exc.code,
            # Out of the restart window or a durable call: the same operation
            # can be retried.
            retryable=True,
            transient=True,
            retry_attempts=exc.attempts,
            retried_seconds=round(exc.retried_seconds),
        )
    return _json_error(str(exc), code=exc.code, retryable=exc.retryable, transient=False)


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
            "synced back to Manor. Use write_files to create a file or replace it in full; use "
            "patch_files for revisions of an existing file (exact old_text -> new_text edits, so "
            "only the changed text is sent). Large compositions may be split across files: "
            "sub-compositions under compositions/ (data-composition-src) and local CSS files "
            "referenced from index.html, which keeps the inline root timeline and at least one "
            "inline timed clip. Review creates approval frames and a review_sha256; render "
            "accepts only that exact approved source hash, so any write or patch requires a new "
            "review. Start returns the exact authoring_contract that must be followed before the "
            "first review. The legacy prepare operation remains an alias for starting a session "
            "that permits external file authoring."
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
                        "patch_files",
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
                        "start and push_assets. Public web URLs are not fetched: save a "
                        "downloaded asset (font, image, audio) into Knowledge first, e.g. bash "
                        "`curl -L -o /tmp/f <url> && manor push /tmp/f <project_path parent>/downloads/f`, "
                        "then pass the saved path."
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
                "source_paths": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "For write_files: project-relative target -> Knowledge path or /api/v1/fs URL "
                        "of a UTF-8 file to copy in, for example {'index.html': "
                        "'Workspaces/.../EP.04-builder-generated-index.html'}. Use it for generated "
                        "sources (a Skill's saved output) instead of re-typing them into files."
                    ),
                },
                "delete_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For write_files: project-relative source files to remove.",
                },
                "patches": {
                    "type": "array",
                    "description": (
                        "For patch_files: exact-text edits to existing project-relative UTF-8 source "
                        "files, applied in order per file. Each old_text must match exactly once "
                        "(including whitespace) unless replace_all is true; a missing or ambiguous "
                        "match fails the whole call and nothing is written. Use write_files to "
                        "create new files."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "edits": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "old_text": {"type": "string", "minLength": 1},
                                        "new_text": {"type": "string"},
                                        "replace_all": {"type": "boolean", "default": False},
                                    },
                                    "required": ["old_text", "new_text"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["path", "edits"],
                        "additionalProperties": False,
                    },
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
                "render_workers": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4,
                    "description": "For render: an upper bound on browser workers (default: what the render container's CPUs afford). Use 1 to reduce peak memory on a busy self-hosted server; review and output quality are unchanged.",
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
    return _json(tool_error_payload(tool_name="video_edit", code=code, message=message, details=details))


# ── Write boundary ──────────────────────────────────────────────────────
#
# Every change this tool makes outside its own session record goes through
# ``_tracked_write`` / ``_tracked_mkdir`` (guarded by a source scan in
# tests/test_video_edit_runtime.py): Workspace files and directories, Knowledge
# registrations, and new content in the session Sandbox. A failed call is then
# settled by what the trace saw, never by its error text:
#   * no write started              -> none
#   * every started write completed -> confirmed, the written files listed
#   * a write was in flight         -> unconfirmed, its targets listed (with the
#                                      hash it was writing, when known) so a
#                                      later host read can reconcile it.
# Not tracked, by design: the session record and its event log (tool-internal
# bookkeeping), and mirroring durable Workspace source into a Sandbox or running
# the network-isolated review/render runtime (they add nothing new outside the
# Sandbox; generated outputs only reach the Workspace through tracked writes).

class _VideoWriteTrace:
    def __init__(self, entity_root: Path | None) -> None:
        self.entity_root = entity_root
        self.started = False
        self.completed: list[dict[str, Any]] = []
        self.in_flight: list[dict[str, Any]] = []


_VIDEO_WRITE_TRACE: contextvars.ContextVar[_VideoWriteTrace | None] = contextvars.ContextVar(
    "video_edit_write_trace", default=None,
)
_MAX_TRACED_WRITES = 32


def _trace_target(path: Path | str, sha256: str | None) -> dict[str, Any]:
    trace = _VIDEO_WRITE_TRACE.get()
    if isinstance(path, Path):
        root = trace.entity_root if trace is not None else None
        try:
            fs_path = path.resolve().relative_to(root.resolve()).as_posix() if root is not None else str(path)
        except ValueError:
            fs_path = str(path)
    else:
        fs_path = path
    return {"fs_path": fs_path, **({"sha256": sha256} if sha256 else {})}


@contextlib.contextmanager
def _tracked_write(path: Path | str, *, sha256: str | None = None):
    """Mark one write: started before it runs, completed only if it returns."""
    trace = _VIDEO_WRITE_TRACE.get()
    if trace is None:
        yield
        return
    target = _trace_target(path, sha256)
    trace.started = True
    trace.in_flight.append(target)
    yield  # an exception leaves the write in flight: its outcome is unknown
    trace.in_flight.remove(target)
    if len(trace.completed) < _MAX_TRACED_WRITES and target not in trace.completed:
        trace.completed.append(target)


def _tracked_mkdir(path: Path) -> None:
    """Create a directory; only creating a missing one is a write."""
    if path.is_dir():
        return
    with _tracked_write(path):
        path.mkdir(parents=True, exist_ok=True)


def _session_sandbox_target(state: dict[str, Any], rel_path: str = "") -> str:
    return f"sandbox:{state.get('sandbox_id') or ''}/{rel_path}".rstrip("/")


def _settle_video_edit_failure(result: Any, trace: _VideoWriteTrace) -> Any:
    """Settle a failed call from its write trace (host facts only).

    Success and results that already carry a host proof pass through.
    """
    from packages.core.ai.runtime.streams import runtime_tool_call_error
    from packages.core.ai.runtime.tool_results import RuntimeSettledToolResult

    if type(result) is not str or runtime_tool_call_error(result) is None:
        return result
    if not trace.started:
        return RuntimeSettledToolResult.not_applied(result, "before_first_write")
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        payload["details"] = {
            **details,
            "written_files": list(trace.completed),
            **({"uncertain_targets": list(trace.in_flight)} if trace.in_flight else {}),
        }
        result = _json(payload)
    if trace.in_flight:
        # A write was cut off: stays unconfirmed, with what it was writing.
        return result
    return RuntimeSettledToolResult.completed_steps_only(result, "completed_writes_only")


_EXTERNAL_URL_REFERENCE_MESSAGE = (
    "Video Edit does not download URLs ({value!r}); it stages files already saved in "
    "Manor. Save the file into Knowledge first, for example with bash: "
    "`curl -L --fail -o /tmp/<name> <url> && manor push /tmp/<name> <project_path parent>/downloads/<name>` "
    "(without bash, ask the caller to save it), then pass that saved path in reference_paths."
)


def _normalize_entity_relative_path(value: str, *, entity_id: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("A non-empty relative path is required")

    fs_url = parse_entity_fs_url(text)
    if fs_url is not None:
        if entity_id and fs_url[0] != entity_id:
            raise ValueError("A reference URL cannot target a different entity")
        raw = fs_url[1]
    else:
        raw = unquote(text).replace("\\", "/")
        parsed = urlparse(raw)
        if parsed.scheme or parsed.netloc:
            # Only Manor file URLs name entity files. Any other URL would
            # silently become a made-up path (``https://host/a/b`` -> ``a/b``).
            raise ValueError(_EXTERNAL_URL_REFERENCE_MESSAGE.format(value=value))

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
        if rel.as_posix() in _HASH_EXCLUDED_FILES:
            continue
        if any(part in _HASH_EXCLUDED_DIRS for part in rel.parts):
            continue
        if rel.parts[:2] == ("assets", "vendor"):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(project_dir).as_posix())


_RENDER_TEXT_SOURCE_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg"}
# The literal part of an asset path in page text: a whole path, or the start of
# one a script completes at runtime ('assets/vo-' + i + '.wav', `assets/s${n}.png`).
_ASSET_PATH_LITERAL = re.compile(r"assets/[^\s\"'`$+(){}\[\],;<>\\?#|*]*")


def _render_source_files(project_dir: Path, source_files: list[Path]) -> list[Path]:
    """The sources a render needs: every non-asset file, and the assets the page can load.

    Projects accumulate staged references the page no longer uses (old takes,
    retried assets); the render sandbox holds its sources in memory, so EP.04
    carried 282 MB of them into a 1.5 GB sandbox. An asset is kept when a text
    source names it, or names the literal start of a path a script builds at
    runtime: picture book 01M58KZRHR9WJG0GMFZJEFPTTY created its six narration
    clips as 'assets/references/norm-zh-s0' + n + '.wav', review (which sees
    the whole project) passed, and the render's audio mix found no files.
    """
    from urllib.parse import quote

    named = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in source_files
        if path.suffix.lower() in _RENDER_TEXT_SOURCE_SUFFIXES
    )
    prefixes = {unquote(match) for match in _ASSET_PATH_LITERAL.findall(named)}
    kept = []
    for path in source_files:
        rel = path.relative_to(project_dir).as_posix()
        if (
            rel.split("/", 1)[0] != "assets"
            or rel in named
            or quote(rel) in named
            or any(rel.startswith(prefix) for prefix in prefixes)
        ):
            kept.append(path)
    return kept


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


class _LocalVideoScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.has_gsap = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        try:
            source = urlparse(dict(attrs).get("src") or "")
        except ValueError:
            return
        path = unquote(source.path).replace("\\", "/")
        if not source.scheme and not source.netloc and not path.startswith("/"):
            self.has_gsap |= posixpath.normpath(path) == _LOCAL_GSAP_PATH


def _project_contract_violations(project_dir: Path) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []

    def fail(file: str, rule: str, message: str, expected: Any, pointer: str = "") -> None:
        violations.append({"file": file, "rule": rule, "message": message, "expected": expected,
                           "pointer": pointer})

    for name in sorted(_REQUIRED_PROJECT_FILES):
        if not (project_dir / name).is_file():
            fail(name, "required_file", name, "A readable project source file")
    motion_files = sorted(project_dir.glob("*.motion.json"))
    if not motion_files:
        fail("index.motion.json", "motion_sidecar", "*.motion.json", {"assertions": [{"kind": "staysInFrame", "selector": "#headline"}]})
    index = project_dir / "index.html"
    if index.is_file():
        try:
            html = index.read_text(encoding="utf-8")
        except OSError:
            fail("index.html", "readable_html", "index.html: unreadable", "UTF-8 HTML")
        else:
            root_match = re.search(
                r"<[^>]+\bdata-composition-id\s*=\s*[\"'][^\"']+[\"'][^>]*>",
                html,
                flags=re.IGNORECASE,
            )
            if root_match is None:
                fail("index.html", "composition_root", "index.html: missing data-composition-id root", "A data-composition-id root")
            else:
                root_tag = root_match.group(0)
                if not re.search(
                    r"\bdata-start\s*=\s*[\"']0(?:\.0+)?[\"']",
                    root_tag,
                    flags=re.IGNORECASE,
                ):
                    fail("index.html", "root_start", 'index.html: root missing data-start="0"', 'data-start="0"')
                for attr in ("data-width", "data-height", "data-duration", "data-fps"):
                    if not re.search(rf"\b{attr}\s*=", root_tag, flags=re.IGNORECASE):
                        fail("index.html", f"root_{attr}", f"index.html: root missing {attr}", attr)
            if "data-end=" in html or "data-end =" in html:
                fail("index.html", "duration_not_end", "index.html: data-end is forbidden; use data-duration", "data-duration")
            scripts = _LocalVideoScripts()
            scripts.feed(html)
            if not scripts.has_gsap:
                fail("index.html", "local_gsap_import", "index.html: missing local GSAP script import",
                     '<script src="./assets/vendor/gsap.min.js"></script>')
            timeline_count = html.count("gsap.timeline")
            if timeline_count != 1 or "window.__timelines" not in html:
                fail("index.html", "registered_timeline",
                     "index.html: inline exactly one paused gsap.timeline and register it on window.__timelines",
                     _video_authoring_contract()["timeline"]["example"])
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
                        fail("index.html", f"clip_{requirement}", f"index.html: timed element {label} missing {requirement}", requirement, label)
            if timed_children == 0:
                fail("index.html", "timed_clip_children", "index.html: no timed clip children",
                     '<section id="scene-1" class="clip" data-start="0" data-duration="4" data-track-index="0">...</section>')
    for motion_path in motion_files:
        try:
            spec = json.loads(motion_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fail(motion_path.name, "motion_json", f"{motion_path.name}: invalid JSON", "JSON object")
            continue
        assertions = spec.get("assertions") if isinstance(spec, dict) else None
        if not isinstance(assertions, list) or not assertions:
            fail(motion_path.name, "motion_assertions", f'{motion_path.name}: requires a non-empty "assertions" array',
                 {"type": "array", "minItems": 1}, "/assertions")
            continue
        for error in Draft202012Validator(_MOTION_METADATA_SCHEMA).iter_errors(spec):
            fail(motion_path.name, "motion_metadata", f"{motion_path.name}: {error.message}", _MOTION_METADATA_SCHEMA,
                 "/" + "/".join(map(str, error.absolute_path)))
        if isinstance(spec.get("duration"), (int, float)) and not math.isfinite(spec["duration"]):
            fail(motion_path.name, "motion_metadata", f"{motion_path.name}: duration must be finite", "finite positive duration", "/duration")
        for index, assertion in enumerate(assertions):
            kind = assertion.get("kind") if isinstance(assertion, dict) else None
            pointer = f"/assertions/{index}"
            if not isinstance(kind, str) or kind not in _MOTION_ASSERTION_SCHEMAS:
                fail(motion_path.name, "motion_assertion_kind", f"{motion_path.name}: assertion {index + 1} has unsupported kind {kind!r}", list(_MOTION_ASSERTION_SCHEMAS),
                     pointer + "/kind")
                continue
            schema = _MOTION_ASSERTION_SCHEMAS[kind]
            for error in Draft202012Validator(schema).iter_errors(assertion):
                fail(motion_path.name, "motion_assertion_shape", f"{motion_path.name}: assertion {index + 1}: {error.message}", schema,
                     pointer + "/" + "/".join(map(str, error.absolute_path)))
            for field, field_schema in schema["properties"].items():
                value = assertion.get(field)
                if field_schema.get("type") == "number" and isinstance(value, (int, float)) and not math.isfinite(value):
                    fail(motion_path.name, "motion_assertion_shape", f"{motion_path.name}: assertion {index + 1}: {field} must be finite", "finite number",
                         pointer + "/" + field)
    return violations


def _validate_project_contract(project_dir: Path) -> list[str]:
    """Compatibility view of the shared structured authoring validation."""
    return [item["message"] for item in _project_contract_violations(project_dir)]


_COMPOSITION_HOST = re.compile(r"<[^>]+\bdata-composition-src\s*=[^>]*>", re.IGNORECASE)
_TIMELINE_KEY = re.compile(r"__timelines\s*\[\s*([^\]]*?)\s*\]")


def _unregistered_sub_compositions(project_dir: Path) -> list[dict[str, Any]]:
    """Sub-compositions whose source can never register the timeline the render waits for.

    HyperFrames' render waits for window.__timelines[<id>] of every mounted
    sub-composition whose host lacks data-no-timeline, then refuses to export
    (sub_timeline_readiness_timeout). Its check and snapshots do not wait, so
    review passed two projects that failed every render after full capture.
    Only a source that cannot register one is reported: a sub-composition whose
    own file mentions __timelines, or any project script that registers
    timelines under a computed key, may register it at runtime.
    """
    root = project_dir.resolve()
    pages: dict[Path, str] = {}
    scripts: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or any(part in _HASH_EXCLUDED_DIRS for part in rel.parts):
            continue
        suffix = path.suffix.lower()
        if suffix not in {".html", ".js", ".mjs"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if suffix == ".html":
            pages[path] = text
        else:
            scripts.append(text)
    literal_keys: set[str] = set()
    for text in [*pages.values(), *scripts]:
        for key in _TIMELINE_KEY.findall(text):
            if len(key) >= 2 and key[0] == key[-1] and key[0] in "'\"`" and "${" not in key:
                literal_keys.add(key[1:-1])
            else:
                return []  # a computed key may register any composition at runtime
    unregistered = []
    for page, html in pages.items():
        for tag in _COMPOSITION_HOST.findall(html):
            if re.search(r"\bdata-no-timeline\b", tag, flags=re.IGNORECASE):
                continue
            ident = re.search(r"\bdata-composition-id\s*=\s*[\"']([^\"']+)[\"']", tag)
            source = re.search(r"\bdata-composition-src\s*=\s*[\"']([^\"']+)[\"']", tag)
            if ident is None or source is None or ident.group(1) in literal_keys:
                continue
            source_path = (page.parent / source.group(1)).resolve()
            if "__timelines" in pages.get(source_path, "__timelines"):
                continue  # registers one itself, or is not a project page we can read
            unregistered.append({
                "file": page.relative_to(root).as_posix(),
                "composition_id": ident.group(1),
                "source": source.group(1),
            })
    return unregistered


def _video_project_feedback(project_dir: Path) -> dict[str, Any]:
    violations = _project_contract_violations(project_dir)
    violations.extend(
        {
            "file": item["file"],
            "rule": "sub_composition_timeline",
            "message": (
                f"{item['source']}: sub-composition '{item['composition_id']}' registers no timeline; "
                "the render waits for window.__timelines and refuses to export "
                "(sub_timeline_readiness_timeout)"
            ),
            "expected": (
                f"window.__timelines['{item['composition_id']}'] = <paused gsap.timeline> inside "
                f"{item['source']}'s <template>, or data-no-timeline on its host element when it "
                "is animated only from the root timeline"
            ),
            "pointer": f"[data-composition-id='{item['composition_id']}']",
        }
        for item in _unregistered_sub_compositions(project_dir)
    )
    violations.extend({"file": "index.html", "rule": "local_assets", "message": message,
                       "expected": "Local frozen assets", "pointer": ""}
                      for message in _validate_local_runtime_assets(project_dir))
    return {
        "authoring_contract": _video_authoring_contract(),
        "contract_valid": not violations,
        "contract_violations": violations,
        "missing_project_contract": [item["message"] for item in violations],
    }


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
    # Admits HyperFrames' FFmpeg processes into the sandbox's PID/CPU budget.
    governor = skill_dir / "scripts" / "media_governor.py"
    instructions = skill_dir / "SKILL.md"
    if not runner.is_file() or not governor.is_file() or not instructions.is_file():
        raise VideoEditRuntimeUnavailable("The bundled video-editing sandbox runner is missing from the Manor image.")
    return {
        "SKILL.md": instructions.read_text(encoding="utf-8"),
        "scripts/video_runtime.py": runner.read_text(encoding="utf-8"),
        "scripts/media_governor.py": governor.read_text(encoding="utf-8"),
    }


def _video_runtime_baked_skill() -> Any:
    """The image's baked ``video-edit`` Skill, used when the runner holds this version.

    Any other runner gets the two runtime files uploaded, as before.
    """

    from packages.core.services.sandbox_baked_skills import baked_skill_ref_for_name

    return baked_skill_ref_for_name("video-edit")


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


async def _create_video_session_sandbox(project_dir: Path, context: Any) -> tuple[str, dict[str, Any]]:
    """Admit the scope's long-lived, network-isolated video Sandbox and seed its source.

    Admission goes through the sandbox session service as a SESSION: it
    replaces (destroys) the container this scope registered before, waits for
    capacity the way the caller's runtime does (parks a durable run, waits in
    a task worker, allocates inline from a skill's tool loop) and records the
    container's runner and run like every other sandbox.

    The session is built fresh from durable Manor source, so a Sandbox Service
    restart during creation is waited out and the creation repeated -- in this
    call unless the run admits through a durable ticket, which would re-attach
    the lost container (then the error is transient and a new call retries).
    """

    return await _retry_sandbox_restart(
        "Starting the Video Edit sandbox session",
        lambda: _create_video_session_sandbox_once(project_dir, context),
        lost_sandbox_is_transient=True,
        retry_in_process=_admission_retries_in_process(context),
    )


async def _create_video_session_sandbox_once(project_dir: Path, context: Any) -> tuple[str, dict[str, Any]]:
    source_files = _project_source_files(project_dir) if project_dir.is_dir() else []
    source_bytes = sum(path.stat().st_size for path in source_files)
    if source_bytes > _MAX_SANDBOX_PROJECT_BYTES:
        raise ValueError("Video source project exceeds the 1.5 GB sandbox limit")

    started_at = time.perf_counter()
    admitted = await _admit_video_sandbox(context, session=True)
    sandbox_id = admitted.sandbox_id
    created_at = time.perf_counter()
    client = None
    try:
        client = await _video_sandbox_client(sandbox_id, timeout=180.0)
        probe = await client.exec(
            sandbox_id=sandbox_id,
            command="python3 /skill/scripts/video_runtime.py probe",
            timeout=60,
            **_video_exec_queue(context),
        )
        probe_payload = _sandbox_result_payload(probe.stdout)
        if probe.exit_code != 0 or probe_payload.get("status") != "ok":
            detail = probe_payload.get("error") or probe.stderr[-2000:] or "unknown runtime probe failure"
            raise VideoEditRuntimeUnavailable(f"Video Sandbox runtime probe failed: {detail}")
        # The readiness probe of every later call checks the project directory;
        # a session started on an empty project must not read as dead and be
        # replaced on its first write.
        prepared = await client.exec(
            sandbox_id=sandbox_id, command="mkdir -p /skill/project", timeout=30, **_video_exec_queue(context),
        )
        if prepared.exit_code != 0:
            raise VideoEditRuntimeUnavailable(
                f"Video Sandbox project directory could not be created: {str(prepared.stderr or '')[-500:]}"
            )
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
    except BaseException as exc:
        # Nothing references a session that never finished starting.
        await _discard_video_session_sandbox(context, sandbox_id)
        if isinstance(exc, (ValueError, VideoEditRuntimeUnavailable)) or not isinstance(exc, Exception):
            raise
        raise VideoEditRuntimeUnavailable(f"Video editing sandbox could not start: {exc}") from exc
    finally:
        if client is not None:
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


_DEAD_CONTAINER_MARKERS = ("no such container", "is not running", "no such object")


async def _release_video_session_sandbox(state: dict[str, Any], context: Any) -> None:
    """Free the authoring sandbox; _ensure_video_session_sandbox recreates it.

    Discarded through the session service like every Video Edit container, so
    the scope's registry no longer names it and the next session operation
    rebuilds it from durable source.
    """

    sandbox_id = str(state.get("sandbox_id") or "")
    if not sandbox_id:
        return
    try:
        outcome = await _discard_video_session_sandbox(context, sandbox_id)
    except Exception:
        logger.info("video session sandbox %s release failed", sandbox_id, exc_info=True)
        return
    if outcome != "destroyed":
        logger.info("video session sandbox %s release: %s", sandbox_id, outcome)


async def _ensure_video_session_sandbox(
    state: dict[str, Any],
    *,
    project_dir: Path,
    context: Any,
) -> tuple[dict[str, Any], bool]:
    """Touch an owned session or recover it from durable Manor source.

    Touch, probe and recovery are all idempotent, so a Sandbox Service restart
    is waited out (the recovery then rebuilds a session the restart dropped).
    """

    return await _retry_sandbox_restart(
        "Reconnecting to the Video Edit sandbox session",
        lambda: _ensure_video_session_sandbox_once(state, project_dir=project_dir, context=context),
    )


async def _ensure_video_session_sandbox_once(
    state: dict[str, Any],
    *,
    project_dir: Path,
    context: Any,
) -> tuple[dict[str, Any], bool]:
    from packages.core.services.sandbox_queue_service import SandboxRouteNotFoundError
    from packages.core.services.sandbox_sdk.exceptions import (
        SandboxNotFoundError,
        SandboxRuntimeError,
    )

    sandbox_id = str(state.get("sandbox_id") or "")
    client = None
    if sandbox_id:
        try:
            client = await _video_sandbox_client(sandbox_id, timeout=180.0)
        except SandboxRouteNotFoundError:
            # Its instance row was released: the runner no longer has it.
            client = None
    if client is not None:
        try:
            try:
                info = await client.touch(sandbox_id)
                status = _sandbox_info_payload(info)
                if status["status"] != "ready":
                    return status, False
                # The Sandbox Service can retain a ready session record after Docker
                # has removed or killed its container. touch() only renews that
                # record, so verify the actual workdir before reusing the session.
                probe = await client.exec(
                    sandbox_id=sandbox_id,
                    command="test -d /skill/project",
                    timeout=15,
                    **_video_exec_queue(context),
                )
                if probe.exit_code == 0:
                    return status, False
                detail = str(probe.stderr or "").casefold()
                if probe.exit_code != 1 and not any(marker in detail for marker in _DEAD_CONTAINER_MARKERS):
                    raise VideoEditRuntimeUnavailable(
                        f"Video Sandbox readiness probe failed (exit {probe.exit_code})."
                    )
            except SandboxNotFoundError:
                pass
            except SandboxRuntimeError as exc:
                if not any(marker in str(exc).casefold() for marker in _DEAD_CONTAINER_MARKERS):
                    raise
        finally:
            await client.close()

    # The recorded container is gone or unusable. Only the owner's current
    # session may be rebuilt: an older record (replaced by a later start)
    # would otherwise take the owner's one session container back.
    current = await current_video_edit_session_id(str(state.get("owner_key") or ""))
    if current and current != str(state.get("session_id") or ""):
        raise ValueError(
            "This Video Edit session was replaced by a newer session of the same owner; "
            "continue in that session or call start again."
        )
    # Destroy what is left of it and start a replacement from the durable source.
    await _discard_video_session_sandbox(context, sandbox_id)
    replacement_id, metrics = await _create_video_session_sandbox(project_dir, context)
    state["sandbox_id"] = replacement_id
    append_video_edit_session_event(
        state,
        "progress",
        message="Sandbox session recovered from durable project source.",
        data={"recovered": True, "sandbox_metrics": metrics, "authoring_contract_version": _VIDEO_AUTHORING_CONTRACT_VERSION},
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
    context: Any = None,
) -> dict[str, Any]:
    """Copy changed editable source from the Sandbox into Manor atomically.

    Hash-compared and atomic per file, so repeating it after a Sandbox Service
    restart only copies what is still missing.
    """

    return await _retry_sandbox_restart(
        "Syncing Video Edit source",
        lambda: _sync_video_session_to_entity_once(
            state=state,
            project_dir=project_dir,
            entity_id=entity_id,
            context=context,
        ),
    )


async def _sync_video_session_to_entity_once(
    *,
    state: dict[str, Any],
    project_dir: Path,
    entity_id: str,
    context: Any = None,
) -> dict[str, Any]:
    from packages.core.services import entity_fs

    sandbox_id = str(state.get("sandbox_id") or "")
    client = await _video_sandbox_client(sandbox_id, timeout=180.0)
    try:
        result = await client.exec(
            sandbox_id=sandbox_id,
            command="python3 /skill/scripts/video_runtime.py manifest",
            timeout=300,
            **_video_exec_queue(context),
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
            with _tracked_write(target, sha256=expected_hash):
                entity_fs.write_entity_file_atomic(
                    entity_id,
                    entity_rel,
                    content,
                    expected_size=expected_size,
                    allow_empty=True,
                )
            written.append(rel_path)
        # Source another tool saved straight into the durable project is hashed
        # and rendered, so the session sandbox that reviews it must hold it too.
        listed = {str(item.get("path") or "") for item in manifest}
        durable_only = [
            path for path in _project_source_files(project_dir)
            if path.relative_to(project_dir).as_posix() not in listed
        ]
        if durable_only:
            await _upload_sandbox_sources(
                client=client, sandbox_id=sandbox_id, project_dir=project_dir, source_files=durable_only,
            )
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
            "uploaded_files": [path.relative_to(project_dir).as_posix() for path in durable_only],
            "source_sha256": source_hash,
        },
    )
    await save_video_edit_session(state)
    return {
        "written_files": written,
        "unchanged_files": len(unchanged),
        "uploaded_files": [path.relative_to(project_dir).as_posix() for path in durable_only],
        "source_files": len(manifest) + len(durable_only),
        "source_bytes": total_bytes,
        "source_sha256": source_hash,
    }


async def _run_video_sandbox(
    *,
    project_dir: Path,
    operation: str,
    context: Any,
    sandbox_id: str = "",
    upload_sources: bool = True,
    snapshot_times: list[float] | None = None,
    output_rel: str = "renders/final.mp4",
    quality: str = "high",
    render_workers: int = 2,
    expected_source_sha256: str = "",
    all_sources: bool = False,
) -> tuple[int, dict[str, Any], dict[str, bytes], str]:
    """Run review/render; a step in a sandbox made for the call is retry-safe.

    A render admits its own ONE_SHOT sandbox, uploads the durable source, runs
    and downloads, so after a Sandbox Service restart the whole step is
    repeated in a fresh sandbox (a durable run's repeat is a new tool call:
    its ticket would re-attach the lost container). ``expected_source_sha256``
    is re-checked before each retry so a retry never renders unreviewed
    source. A review inside the editable session runs once.
    """

    def attempt_once():
        return _run_video_sandbox_once(
            project_dir=project_dir,
            operation=operation,
            context=context,
            sandbox_id=sandbox_id,
            upload_sources=upload_sources,
            snapshot_times=snapshot_times,
            output_rel=output_rel,
            quality=quality,
            render_workers=render_workers,
            all_sources=all_sources,
        )

    if operation != "render" and str(sandbox_id or "").strip():
        return await attempt_once()

    async def source_unchanged() -> None:
        if not expected_source_sha256:
            return
        current = await asyncio.to_thread(_project_source_sha256, project_dir)
        if current != expected_source_sha256:
            raise ValueError(
                "Project source changed after review while waiting for the Sandbox Service; "
                "run review again and obtain fresh approval."
            )

    return await _retry_sandbox_restart(
        f"Video {operation}",
        attempt_once,
        lost_sandbox_is_transient=True,
        before_retry=source_unchanged,
        retry_in_process=_admission_retries_in_process(context),
    )


async def _run_video_sandbox_once(
    *,
    project_dir: Path,
    operation: str,
    context: Any,
    sandbox_id: str = "",
    upload_sources: bool = True,
    snapshot_times: list[float] | None = None,
    output_rel: str = "renders/final.mp4",
    quality: str = "high",
    render_workers: int = 2,
    all_sources: bool = False,
) -> tuple[int, dict[str, Any], dict[str, bytes], str]:
    from packages.core.ai.runtime.bash_sandbox import runtime_await_sandbox_execution
    from packages.core.services.runtime_activity import activity_event, activity_scope, record_activity
    from packages.core.services.sandbox_session_service import (
        discard_one_shot_sandbox,
        new_execution_id,
        track_runtime_execution,
    )

    source_files = _project_source_files(project_dir)
    total_bytes = sum(path.stat().st_size for path in source_files)
    if total_bytes > _MAX_SANDBOX_PROJECT_BYTES:
        raise ValueError("Video source project exceeds the 1.5 GB sandbox limit")

    # Rendering consumes an already synchronized, review-hash-checked snapshot.
    # Keep browser/encoder children out of the authoring session, and destroy
    # the entire execution sandbox even if PID exhaustion prevents an exec to
    # clean them up. A retry must not inherit a failed render's processes.
    # The render container is a ONE_SHOT admitted like every other sandbox:
    # it waits for capacity, holds an instance row (so a cancel of the run
    # destroys it), and is discarded here whatever happens.
    # A review gets one too while the render pool can serve: its strict check
    # and snapshots load the whole composition like a render and overrun a small
    # authoring runner. Sized alike, the render that follows reuses the machine.
    from packages.core.services.sandbox_queue_service import DEFAULT_RUNNER_POOL

    render_pool = await _render_runner_pool() if operation in {"render", "review"} else DEFAULT_RUNNER_POOL
    created_for_call = operation == "render" or (operation == "review" and render_pool != DEFAULT_RUNNER_POOL)
    omitted_assets = 0
    if created_for_call:
        upload_sources = True
        if operation == "render" and not all_sources:
            kept = _render_source_files(project_dir, source_files)
            omitted_assets = len(source_files) - len(kept)
            source_files = kept
        total_bytes = sum(path.stat().st_size for path in source_files)
    active_sandbox_id = "" if created_for_call else str(sandbox_id or "").strip()
    started_at = time.perf_counter()
    created_at = started_at
    uploaded_at = started_at
    executed_at = started_at
    client = None
    max_render_workers = 2
    activity = activity_scope(context)
    try:
        if created_for_call:
            await record_activity(activity, activity_event(code="preparing_machine"))
            size_request = _render_size_request(project_dir) if render_pool != DEFAULT_RUNNER_POOL else None
            admitted = await _admit_video_sandbox(
                context, session=False, pool=render_pool, config_overrides=size_request,
            )
            active_sandbox_id = admitted.sandbox_id
            client = await _video_sandbox_client(active_sandbox_id, timeout=float(_SANDBOX_RENDER_TIMEOUT + 60))
            max_render_workers = await _granted_render_workers(
                admitted, client, cpu_ceiling=float((size_request or {}).get("cpus") or _SANDBOX_CPUS),
            )
            created_at = time.perf_counter()
        else:
            client = await _video_sandbox_client(active_sandbox_id, timeout=float(_SANDBOX_RENDER_TIMEOUT + 60))
            await client.touch(active_sandbox_id)
            created_at = time.perf_counter()

        if upload_sources:
            await record_activity(activity, activity_event(code="uploading_files", count=len(source_files)))
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
                    str(max(1, min(int(render_workers), _RENDER_MAX_WORKERS, max_render_workers))),
                    # _render_project only renders a source whose hash passed
                    # review's strict check; repeating it cost ~2.5 min per render.
                    "--reviewed",
                ]
            )
        # Named, and recorded on the run, so Stop kills the command itself.
        execution_id = new_execution_id()
        exec_timeout = _SANDBOX_RENDER_TIMEOUT if operation == "render" else 900
        queue = _video_exec_queue(context)
        async with track_runtime_execution(
            getattr(context, "runtime_run_id", None),
            sandbox_id=active_sandbox_id,
            execution_id=execution_id,
        ):
            # Started, then polled: the runtime reports its phases and frame
            # counts as it goes, and the step shows them while it runs.
            started_exec = await client.start_exec(
                sandbox_id=active_sandbox_id,
                command=" ".join(shlex.quote(value) for value in command),
                timeout=exec_timeout,
                execution_id=execution_id,
                **queue,
            )
            try:
                result, _last = await runtime_await_sandbox_execution(
                    client,
                    sandbox_id=active_sandbox_id,
                    execution_id=execution_id,
                    first=started_exec,
                    budget_seconds=exec_timeout + float(queue.get("queue_timeout") or 0) + 30,
                    runtime_context=context,
                )
            except BaseException:
                await asyncio.shield(_cancel_video_execution(client, active_sandbox_id, execution_id))
                raise
        if str(getattr(result, "outcome", "") or "") == "queue_timeout":
            raise RuntimeError("sandbox_queue_timeout: no command slot freed in time; nothing ran")
        executed_at = time.perf_counter()
        payload = _sandbox_result_payload(result.stdout or "")
        if getattr(result, "outcome", None):
            # The runner killed the runtime itself (memory, PIDs, disk): it
            # never got to print its own classification.
            payload["sandbox_outcome"] = {
                "outcome": result.outcome, "detail": getattr(result, "outcome_detail", None),
            }
        outputs: dict[str, bytes] = {}
        if result.exit_code == 0 and payload.get("status") == "ok":
            await record_activity(activity, activity_event(code="saving_outputs"))
            for raw_path in list(payload.get("files") or [])[:12]:
                rel_path = _normalize_entity_relative_path(str(raw_path))
                file_result = await client.read_file_base64(
                    sandbox_id=active_sandbox_id,
                    path=f"/skill/project/{rel_path}",
                    max_size=_MAX_SANDBOX_OUTPUT_BYTES,
                )
                outputs[rel_path] = base64.b64decode(file_result.content_base64)
            await record_activity(activity, activity_event(code="finished"))
        completed_at = time.perf_counter()
        payload["sandbox_metrics"] = {
            "cold_start_ms": round((created_at - started_at) * 1000),
            "upload_ms": round((uploaded_at - created_at) * 1000),
            "execution_ms": round((executed_at - uploaded_at) * 1000),
            "download_ms": round((completed_at - executed_at) * 1000),
            "total_ms": round((completed_at - started_at) * 1000),
            "source_files": len(source_files),
            "source_bytes": total_bytes,
            "omitted_assets": omitted_assets,
        }
        return result.exit_code, payload, outputs, (result.stderr or "")[-40_000:]
    except (VideoEditRuntimeUnavailable, VideoSandboxParked):
        raise
    except Exception as exc:
        raise VideoEditRuntimeUnavailable(f"Video editing sandbox could not start or complete: {exc}") from exc
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - the container is what matters
                pass
        if active_sandbox_id and created_for_call:
            await discard_one_shot_sandbox(active_sandbox_id)


async def _cancel_video_execution(client: Any, sandbox_id: str, execution_id: str) -> None:
    """Stop a command whose caller stopped waiting for it; best-effort."""

    try:
        await client.cancel_execution(sandbox_id, execution_id)
    except Exception:  # noqa: BLE001 - a gone sandbox already stopped it
        logger.debug("video execution %s cancel failed", execution_id, exc_info=True)


class VideoReferenceAccessDenied(PermissionError):
    """A reference failed Workspace scope or ACL before any Video Edit effect."""


class VideoReferenceUnavailable(ValueError):
    """A reference path is malformed, missing or over budget before any Video Edit effect."""


async def _preflight_reference_paths(
    *,
    entity_id: str,
    entity_root: Path,
    raw_paths: list[Any],
    user_id: str | None,
    workspace_id: str | None,
    workspace_base_dir: str,
) -> list[str]:
    """Validate references with reads only, before the project, sandbox or session is touched.

    Every rejection raised here is a host-proven no-op (see the handler).
    """
    rel_paths = []
    for value in raw_paths:
        if not str(value or "").strip():
            continue
        try:
            rel_paths.append(_normalize_entity_relative_path(str(value), entity_id=entity_id))
        except ValueError as exc:
            raise VideoReferenceUnavailable(str(exc)) from exc
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
            # User-less (scheduler/Strategist) runs may read only their own
            # Workspace store; without the scope every reference is denied.
            workspace_id=workspace_id,
            actor_type="agent",
        )
    if blocked:
        raise VideoReferenceAccessDenied("Reference access denied: " + ", ".join(sorted(blocked)))

    # Existence and the staging budget are checked only after authorization,
    # so an unreadable path cannot be probed for existence or size.
    total_bytes = 0
    for rel_path in rel_paths:
        try:
            source = _resolve_inside(entity_root, rel_path)
        except ValueError as exc:
            raise VideoReferenceUnavailable(str(exc)) from exc
        if not source.is_file():
            raise VideoReferenceUnavailable(f"Reference file not found: {rel_path}")
        total_bytes += source.stat().st_size
        if total_bytes > _MAX_REFERENCE_BYTES:
            raise VideoReferenceUnavailable("Combined reference files exceed the 1 GB staging limit")
    return rel_paths


def _same_file_bytes(left: Path, right: Path) -> bool:
    """Whether two files hold identical bytes (the same reference under another path)."""
    try:
        if not left.is_file() or left.stat().st_size != right.stat().st_size:
            return False
        digests = []
        for path in (left, right):
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            digests.append(digest.digest())
        return digests[0] == digests[1]
    except OSError:
        return False


def _identical_reference(destination_dir: Path, source: Path) -> Path | None:
    """A staged reference already holding ``source``'s bytes, under any name."""
    if not destination_dir.is_dir():
        return None
    size = source.stat().st_size
    for candidate in sorted(destination_dir.iterdir()):
        if candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size == size:
            if _same_file_bytes(candidate, source):
                return candidate
    return None


async def _stage_references(
    *,
    entity_root: Path,
    project_dir: Path,
    rel_paths: list[str],
) -> list[dict[str, Any]]:
    """Copy references that _preflight_reference_paths already validated."""
    if not rel_paths:
        return []

    destination_dir = project_dir / "assets" / "references"
    _tracked_mkdir(destination_dir)
    staged: list[dict[str, Any]] = []
    for rel_path in rel_paths:
        source = _resolve_inside(entity_root, rel_path)
        # A reference removed after preflight fails here, after effects began:
        # a plain error, so the outcome correctly stays unconfirmed.
        if not source.is_file():
            raise FileNotFoundError(f"Reference file not found: {rel_path}")
        size = source.stat().st_size

        # A file of this project (its source, review frames or renders: a
        # later step gets them as inputs) is not copied into it again; nor is
        # a reference whose bytes it already holds. Restarting a reviewed
        # project with the same inputs must leave its source, and hash, alone.
        project_resolved = project_dir.resolve()
        existing = (
            source
            if project_resolved in source.resolve().parents
            else _identical_reference(destination_dir, source)
        )
        if existing is not None:
            staged.append(
                {
                    "source_path": rel_path,
                    "project_path": existing.resolve().relative_to(project_resolved).as_posix(),
                    "size_bytes": size,
                }
            )
            continue

        clean_name = re.sub(r"[^\w.\-]+", "-", source.name, flags=re.UNICODE).strip(".-")
        clean_name = clean_name or "reference"
        candidate = destination_dir / clean_name
        if (
            candidate.exists()
            and candidate.resolve() != source.resolve()
            and not _same_file_bytes(candidate, source)
        ):
            suffix = hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:8]
            candidate = destination_dir / f"{candidate.stem}-{suffix}{candidate.suffix}"
        if candidate.resolve() != source.resolve() and not _same_file_bytes(candidate, source):
            with _tracked_write(candidate):
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
    from packages.core.services.generated_media_naming import workspace_storage_root

    # Inside a Workflow/Task the resolved base is that execution's archive, but
    # the Workspace home (generate_file(storage_scope='workspace')) and every
    # archive under it belong to the same active Workspace.
    home = workspace_storage_root(workspace_base_dir) if workspace_base_dir else ""
    for rel_path in rel_paths:
        if rel_path != "Workspaces" and not rel_path.startswith("Workspaces/"):
            continue
        if not home or not rel_path.startswith(f"{home}/"):
            raise VideoReferenceAccessDenied("Workspace reference is outside the active Workspace: " + rel_path)


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
    reference_paths = await _preflight_reference_paths(
        entity_id=entity_id,
        raw_paths=raw_reference_paths,
        entity_root=entity_root,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workspace_base_dir=workspace_base_dir,
    )
    if not _video_owner_key(context):
        raise PermissionError(
            "Video Edit sessions can only be started by a user or from a task run."
        )
    _tracked_mkdir(project_dir)
    staged = await _stage_references(
        entity_root=entity_root,
        project_dir=project_dir,
        rel_paths=reference_paths,
    )
    await _retire_previous_video_session(context, entity_id=entity_id, entity_root=entity_root)
    sandbox_id, metrics = await _create_video_session_sandbox(project_dir, context)
    state = new_video_edit_session(
        sandbox_id=sandbox_id,
        entity_id=entity_id,
        user_id=context.user_id,
        conversation_id=_video_binding_key(context),
        agent_id=context.agent_id,
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
    await make_current_video_edit_session(state)
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
            **_video_project_feedback(project_dir),
        }
    )


async def _retire_previous_video_session(context: Any, *, entity_id: str, entity_root: Path) -> None:
    """``start`` replaces the owner's current session, wherever it was started.

    Its editable source is synchronized first (best effort: ``write_files``
    already wrote every file to Manor), then its container is destroyed and
    the record dropped, so a restart never leaves the old container running.
    """

    previous = await load_owner_video_edit_session(_video_owner_key(context))
    if previous is None:
        return
    try:
        assert_video_edit_session_owner(
            previous,
            entity_id=entity_id,
            user_id=context.user_id,
            conversation_id=_video_binding_key(context),
        )
    except PermissionError:
        return
    sandbox_id = str(previous.get("sandbox_id") or "")
    try:
        previous_dir = _resolve_inside(entity_root, str(previous.get("project_path") or ""))
        if sandbox_id and previous_dir.is_dir() and not previous.get("legacy_external_authoring"):
            await _sync_video_session_to_entity(
                state=previous, project_dir=previous_dir, entity_id=entity_id, context=context,
            )
    except Exception:  # noqa: BLE001 - the container may already be gone
        pass
    await _discard_video_session_sandbox(context, sandbox_id)
    await close_video_edit_session(previous)


async def _resolve_owned_video_session(
    *,
    session_id: str,
    entity_id: str,
    context: Any,
    project_rel: str = "",
) -> dict[str, Any]:
    state = await load_video_edit_session(session_id)
    if state is None and not session_id:
        state = await load_owner_video_edit_session(_video_owner_key(context))
    if state is None:
        raise ValueError("Video Edit session not found or expired; call start again")
    assert_video_edit_session_owner(
        state,
        entity_id=entity_id,
        user_id=context.user_id,
        conversation_id=_video_binding_key(context),
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
    reference_paths = await _preflight_reference_paths(
        entity_id=entity_id,
        raw_paths=raw_paths,
        entity_root=entity_root,
        user_id=context.user_id,
        workspace_id=context.workspace_id,
        workspace_base_dir=workspace_base_dir,
    )
    await _ensure_video_session_sandbox(state, project_dir=project_dir, context=context)
    staged = await _stage_references(
        entity_root=entity_root,
        project_dir=project_dir,
        rel_paths=reference_paths,
    )
    if not staged:
        return _json_error(
            "push_assets requires at least one readable reference path",
            code="asset_required",
        )

    client = await _video_sandbox_client(str(state["sandbox_id"]), timeout=180.0)
    try:
        await _upload_sandbox_sources(
            client=client,
            sandbox_id=str(state["sandbox_id"]),
            project_dir=project_dir,
            # A reference that was already one of the project's own files is
            # in the session as it is; only staged copies are delivered.
            source_files=[
                _resolve_inside(project_dir, str(item["project_path"]))
                for item in staged
                if str(item["project_path"]).startswith("assets/references/")
            ],
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


async def _read_video_source_paths(
    source_paths: Any,
    *,
    entity_id: str,
    entity_root: Path,
    context: Any,
    workspace_base_dir: str,
) -> dict[str, str] | str:
    """Read Knowledge text files to copy into the project, under reference ACLs.

    A generated source (a builder Skill's 40 KB page) cannot be passed through
    the model intact: tool results are truncated and re-typing is lossy.
    Returns target -> text, or a JSON error string.
    """
    if not isinstance(source_paths, dict):
        return _json_error("source_paths must map project paths to Knowledge paths")
    copied: dict[str, str] = {}
    for target, source in source_paths.items():
        rel_paths = await _preflight_reference_paths(
            entity_id=entity_id,
            entity_root=entity_root,
            raw_paths=[source],
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            workspace_base_dir=workspace_base_dir,
        )
        if not rel_paths:
            return _json_error(f"source_paths entry has no source path: {target}")
        path = _resolve_inside(entity_root, rel_paths[0])
        if path.stat().st_size > _MAX_TEXT_FILE_BYTES:
            return _json_error(f"Text source file exceeds 2 MB: {rel_paths[0]}")
        try:
            copied[str(target)] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _json_error(f"source_paths only copies UTF-8 text files: {rel_paths[0]}")
    return copied


async def _write_video_session_files(
    *,
    state: dict[str, Any],
    entity_id: str,
    project_dir: Path,
    files: Any,
    delete_paths: list[Any],
    context: Any,
) -> str:
    await _ensure_video_session_sandbox(state, project_dir=project_dir, context=context)
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

    saved = await _persist_video_session_sources(
        state=state,
        entity_id=entity_id,
        project_dir=project_dir,
        files=normalized_files,
        deletes=normalized_deletes,
        context=context,
    )
    return _json(
        {
            "status": "source_updated",
            "written_files": [item[0] for item in normalized_files],
            "deleted_files": normalized_deletes,
            **saved,
        }
    )


async def _persist_video_session_sources(
    *,
    state: dict[str, Any],
    entity_id: str,
    project_dir: Path,
    files: list[tuple[str, str, bytes]],
    deletes: list[str],
    context: Any,
) -> dict[str, Any]:
    """Write validated sources into the Sandbox and Manor; record a new source revision.

    The one persist path for editable source (write_files and patch_files):
    every byte change lands in both copies and moves the source hash, so a
    prior review_sha256 no longer matches and render requires a new review.
    """
    from packages.core.services import entity_fs

    client = await _video_sandbox_client(str(state["sandbox_id"]), timeout=180.0)
    try:
        for rel_path, content_text, content_bytes in files:
            content_hash = hashlib.sha256(content_bytes).hexdigest()
            with _tracked_write(_session_sandbox_target(state, rel_path), sha256=content_hash):
                await client.write_file(
                    sandbox_id=str(state["sandbox_id"]),
                    path=f"/skill/project/{rel_path}",
                    content=content_text,
                    mkdir=True,
                )
            with _tracked_write(_resolve_inside(project_dir, rel_path), sha256=content_hash):
                entity_fs.write_entity_file_atomic(
                    entity_id,
                    f"{state['project_path']}/{rel_path}",
                    content_bytes,
                    expected_size=len(content_bytes),
                    allow_empty=True,
                )
        for rel_path in deletes:
            with _tracked_write(_session_sandbox_target(state, rel_path)):
                result = await client.exec(
                    sandbox_id=str(state["sandbox_id"]),
                    command=f"rm -f -- {shlex.quote('/skill/project/' + rel_path)}",
                    timeout=30,
                    **_video_exec_queue(context),
                )
            if result.exit_code != 0:
                raise VideoEditRuntimeUnavailable(f"Could not remove Sandbox source file {rel_path}: {result.stderr}")
            target = _resolve_inside(project_dir, rel_path)
            if target.is_file() or target.is_symlink():
                with _tracked_write(target):
                    target.unlink()
    finally:
        await client.close()

    source_hash = _project_source_sha256(project_dir)
    feedback = _video_project_feedback(project_dir)
    state["source_revision"] = int(state.get("source_revision") or 0) + 1
    state["last_synced_sha256"] = source_hash
    append_video_edit_session_event(
        state,
        "progress",
        message="Editable project files updated inside the Video Sandbox and synced to Manor.",
        data={
            "written_files": [item[0] for item in files],
            "deleted_files": deletes,
            "source_sha256": source_hash,
        },
    )
    await save_video_edit_session(state)
    return {
        "source_saved": True,
        "session_id": state["session_id"],
        "project_path": state["project_path"],
        "source_sha256": source_hash,
        "source_revision": state["source_revision"],
        "event_cursor": state["last_event_seq"],
        "events": [state["events"][-1]],
        **feedback,
    }


class _VideoPatchError(ValueError):
    def __init__(self, message: str, *, code: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _apply_video_text_edits(rel_path: str, content: str, edits: Any) -> tuple[str, int]:
    """Apply ordered exact-text edits; a missing or ambiguous match raises."""
    if not isinstance(edits, list) or not edits:
        raise _VideoPatchError(f"patch_files entry for {rel_path} needs a non-empty edits list",
                               code="invalid_request", path=rel_path)
    replacements = 0
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise _VideoPatchError("Each edit must be an object with old_text and new_text",
                                   code="invalid_request", path=rel_path, edit_index=index)
        old, new = edit.get("old_text"), edit.get("new_text")
        if not isinstance(old, str) or not old:
            raise _VideoPatchError("old_text must be a non-empty string",
                                   code="invalid_request", path=rel_path, edit_index=index)
        if not isinstance(new, str):
            raise _VideoPatchError("new_text must be a string", code="invalid_request",
                                   path=rel_path, edit_index=index)
        count = content.count(old)
        if count == 0:
            raise _VideoPatchError(
                f"old_text not found in {rel_path} (edit {index}). Ensure an exact match including "
                "whitespace against the file as changed by this call's earlier edits.",
                code="patch_text_not_found", path=rel_path, edit_index=index,
            )
        replace_all = bool(edit.get("replace_all", False))
        if count > 1 and not replace_all:
            raise _VideoPatchError(
                f"old_text matches {count} places in {rel_path} (edit {index}). Include more "
                "surrounding text to make it unique, or set replace_all.",
                code="patch_text_ambiguous", path=rel_path, edit_index=index, match_count=count,
            )
        content = content.replace(old, new)
        replacements += count
    return content, replacements


async def _read_video_session_sources(
    *,
    state: dict[str, Any],
    project_dir: Path,
    rel_paths: list[str],
    context: Any,
) -> dict[str, str]:
    """Current text of existing editable sources, from the copy that is authoritative.

    The Sandbox holds the session's working source (review and render sync it
    back to Manor); a legacy external-authoring session edits Manor's copy and
    uploads it instead.
    """
    raw: dict[str, bytes] = {}
    if state.get("legacy_external_authoring"):
        for rel_path in rel_paths:
            target = _resolve_inside(project_dir, rel_path)
            if not target.is_file():
                raise _VideoPatchError(f"Source file not found: {rel_path}. Use write_files to create it.",
                                       code="patch_target_not_found", path=rel_path)
            if target.stat().st_size > _MAX_TEXT_FILE_BYTES:
                raise _VideoPatchError(f"Text source file exceeds 2 MB: {rel_path}",
                                       code="invalid_request", path=rel_path)
            raw[rel_path] = target.read_bytes()
    else:
        sandbox_id = str(state.get("sandbox_id") or "")
        client = await _video_sandbox_client(sandbox_id, timeout=180.0)
        try:
            result = await client.exec(
                sandbox_id=sandbox_id,
                command="python3 /skill/scripts/video_runtime.py manifest",
                timeout=300,
                **_video_exec_queue(context),
            )
            payload = _sandbox_result_payload(result.stdout)
            if result.exit_code != 0 or payload.get("status") != "ok":
                raise VideoEditRuntimeUnavailable(
                    "Video Sandbox could not produce a source manifest: " + (result.stderr[-2000:] or str(payload))
                )
            manifest = {
                str(item.get("path") or ""): item
                for item in payload.get("files", []) if isinstance(item, dict)
            }
            for rel_path in rel_paths:
                item = manifest.get(rel_path)
                if item is None:
                    raise _VideoPatchError(f"Source file not found: {rel_path}. Use write_files to create it.",
                                           code="patch_target_not_found", path=rel_path)
                if int(item.get("size_bytes") or 0) > _MAX_TEXT_FILE_BYTES:
                    raise _VideoPatchError(f"Text source file exceeds 2 MB: {rel_path}",
                                           code="invalid_request", path=rel_path)
                file_result = await client.read_file_base64(
                    sandbox_id=sandbox_id,
                    path=f"/skill/project/{rel_path}",
                    max_size=_MAX_TEXT_FILE_BYTES,
                )
                content = base64.b64decode(file_result.content_base64)
                if hashlib.sha256(content).hexdigest() != str(item.get("sha256") or ""):
                    raise VideoEditRuntimeUnavailable(f"Video source changed while reading: {rel_path}")
                raw[rel_path] = content
        finally:
            await client.close()
    texts: dict[str, str] = {}
    for rel_path, content in raw.items():
        try:
            texts[rel_path] = content.decode("utf-8")
        except UnicodeDecodeError:
            raise _VideoPatchError(f"patch_files only edits UTF-8 text files: {rel_path}",
                                   code="invalid_request", path=rel_path) from None
    return texts


async def _patch_video_session_files(
    *,
    state: dict[str, Any],
    entity_id: str,
    project_dir: Path,
    patches: Any,
    context: Any,
) -> str:
    """Apply exact-text edits to existing sources, all or nothing, then persist like write_files."""
    if not isinstance(patches, list) or not patches:
        return _json_error("patch_files requires a non-empty patches list")
    if len(patches) > _MAX_TEXT_FILES_PER_CALL:
        return _json_error(f"patch_files accepts at most {_MAX_TEXT_FILES_PER_CALL} files per call")
    planned: list[tuple[str, Any]] = []
    for entry in patches:
        if not isinstance(entry, dict):
            return _json_error("Each patches entry must be an object with path and edits")
        rel_path = _normalize_project_file_path(str(entry.get("path") or ""))
        if any(rel_path == seen for seen, _ in planned):
            return _json_error(f"patch_files lists {rel_path} more than once; put all its edits in one entry",
                               path=rel_path)
        planned.append((rel_path, entry.get("edits")))

    await _ensure_video_session_sandbox(state, project_dir=project_dir, context=context)
    try:
        current = await _read_video_session_sources(
            state=state, project_dir=project_dir, rel_paths=[path for path, _ in planned], context=context,
        )
        normalized_files: list[tuple[str, str, bytes]] = []
        results: list[dict[str, Any]] = []
        total_bytes = 0
        for rel_path, edits in planned:
            text, replacements = _apply_video_text_edits(rel_path, current[rel_path], edits)
            content = text.encode("utf-8")
            if len(content) > _MAX_TEXT_FILE_BYTES:
                raise _VideoPatchError(f"Patched source file exceeds 2 MB: {rel_path}",
                                       code="invalid_request", path=rel_path)
            total_bytes += len(content)
            normalized_files.append((rel_path, text, content))
            results.append({
                "path": rel_path,
                "edits_applied": len(edits),
                "replacements": replacements,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            })
        if total_bytes > _MAX_TEXT_BATCH_BYTES:
            raise _VideoPatchError("patch_files result exceeds the 16 MB batch limit", code="invalid_request")
    except _VideoPatchError as exc:
        return _json_error(str(exc), code=exc.code, applied=False, **exc.details)

    saved = await _persist_video_session_sources(
        state=state,
        entity_id=entity_id,
        project_dir=project_dir,
        files=normalized_files,
        deletes=[],
        context=context,
    )
    return _json(
        {
            "status": "source_updated",
            "written_files": [item[0] for item in normalized_files],
            "deleted_files": [],
            "patched_files": results,
            **saved,
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

    from packages.core.services.file_refs import file_ref
    from packages.core.services.generated_file_refs import entity_fs_open_url

    artifact = await runtime_register_video_edit_artifact(
        abs_path=abs_path,
        entity_root=entity_root,
        entity_id=entity_id,
        context=context,
        artifact_role=artifact_role,
        generation=generation,
    )
    # Legacy ``url`` stays an fs address (callers decode it), now encoded.
    artifact["url"] = entity_fs_open_url(entity_id, artifact.get("fs_path"))
    artifact["file"] = file_ref(
        entity_id=entity_id,
        fs_path=artifact.get("fs_path"),
        document_id=artifact.get("document_id"),
        name=artifact.get("name"),
        mime=artifact.get("mime_type"),
        size=artifact.get("size_bytes"),
    )
    return artifact


# Every code `hyperframes check` can report at HYPERFRAMES_VERSION, generated
# from the pinned CLI by scripts/hyperframes_check_rules.py.
_CHECK_RULES_PATH = Path(__file__).with_name("hyperframes_check_rules.json")
_CHECK_SECTIONS = ("lint", "runtime", "layout", "motion", "contrast")
# `review` runs `check --strict`: errors and warnings fail it, info does not.
_STRICT_FAILING_SEVERITIES = ("error", "warning")
# Locator noise that does not help the model find or fix the source.
_FINDING_NOISE_FIELDS = {"bbox", "rect", "containerRect", "dataAttributes"}
# Manor's own fix where the CLI's hint is generic or misses Manor's conventions.
_REVIEW_FINDING_GUIDANCE = {
    "timeline_track_too_dense": (
        "More than 3 timed elements (other than video/audio, caption cues and sub-composition hosts) "
        "share one data-track-index in this HTML file. Give each layer its own track index, or move "
        "each scene into compositions/<scene>.html mounted by one host clip with data-composition-src."
    ),
    "duplicate_media_discovery_risk": (
        "Two or more <img>/<video>/<audio> elements in this file have the same src, data-start and "
        "data-duration (for example one illustration used as both a blurred backdrop and the "
        "foreground, or a copied layer). Keep one media element per source and time window: paint "
        "an extra image layer as a CSS background-image on a <div>, animate one shared element or a "
        "wrapper instead of a copy, or drop the duplicate <video>/<audio>."
    ),
    "duplicate_media_id": "Give every media element its own id; copied layers must not share one.",
    "missing_local_asset": (
        "The page names a file the project does not have. Stage it with "
        "video_edit(operation=\"push_assets\") and use the returned project_path, or fix the src."
    ),
    "audio_src_not_found": (
        "Stage the audio with video_edit(operation=\"push_assets\") and set src to the returned "
        "project_path (assets/references/...)."
    ),
    "google_fonts_import": (
        "The Sandbox has no network: remove the Google Fonts <link>/@import, stage the font file "
        "with push_assets and declare @font-face { src: url('assets/references/<file>') }."
    ),
    "font_family_without_font_face": (
        "Declare @font-face for that family: src: local('<font name>') for a font installed in the Sandbox "
        "(no file needed), otherwise a font file staged under assets/ (push_assets). A generic family such "
        "as sans-serif also passes."
    ),
    "contrast_aa_failure": (
        "Set the text color to the finding's suggestedColor, or darken/lighten the area behind the "
        "text (scrim, panel) until it meets requiredRatio."
    ),
    "request_failed": "The Sandbox has no network: every script, font and media file must be a local file under assets/.",
    "http_error": "A local file the page requested is missing: stage it with push_assets or fix the path.",
    "console_error": "Fix the script error named in the message; review fails on any console error.",
    "page_error": "Fix the uncaught script error named in the message; review fails on it.",
}


@functools.cache
def _check_rule_hints() -> dict[str, str]:
    """The CLI's fix hint per code, where it is one fixed text."""
    try:
        rules = json.loads(_CHECK_RULES_PATH.read_text(encoding="utf-8"))["rules"]
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("hyperframes check rules unavailable at %s", _CHECK_RULES_PATH)
        return {}
    return {
        rule["code"]: rule["fix_hints"][0]
        for rule in rules
        if len(rule.get("fix_hints") or []) == 1 and "${" not in rule["fix_hints"][0]
    }


def _review_findings(report: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a strict check report into failing findings and non-blocking advisories.

    Each failing finding carries the source file, the CLI's fixHint (or the
    pinned rule's fixed hint when the finding has none) and Manor's guidance
    where it has a better fix, errors first, so the model can act on it.
    """
    failing: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []
    hints = _check_rule_hints()
    for section in _CHECK_SECTIONS:
        for finding in report[section]["findings"]:
            if not isinstance(finding, dict):
                continue
            code = str(finding.get("code") or "")
            severity = finding.get("severity")
            item: dict[str, Any] = {
                "code": code,
                "severity": severity,
                "section": section,
                "file": finding.get("sourceFile") or finding.get("file"),
                "message": finding.get("message"),
            }
            if severity not in _STRICT_FAILING_SEVERITIES:
                advisories.append(item)
                continue
            fix_hint = finding.get("fixHint") or hints.get(code)
            if fix_hint:
                item["fix_hint"] = fix_hint
            if code in _REVIEW_FINDING_GUIDANCE:
                item["manor_hint"] = _REVIEW_FINDING_GUIDANCE[code]
            for key, value in finding.items():
                if key in item or key in {"sourceFile", "file", "fixHint"} or key in _FINDING_NOISE_FIELDS:
                    continue
                if section == "lint" and key == "time":
                    continue  # lint findings are static; time is always 0
                item[key] = value
            failing.append(item)
    failing.sort(key=lambda item: _STRICT_FAILING_SEVERITIES.index(item["severity"]))
    return failing, advisories


async def _review_validation_result(
    *,
    project_rel: str,
    feedback: dict[str, Any],
    diagnostics: list[dict[str, Any]],
    session_state: dict[str, Any] | None,
    advisories: list[dict[str, Any]] | None = None,
    sandbox_metrics: dict[str, Any] | None = None,
) -> str:
    """Return a completed check with unmet criteria, preserving its effects."""
    if session_state is not None:
        session_state["last_review_sha256"] = ""
        session_state["last_review_artifacts"] = []
        append_video_edit_session_event(
            session_state,
            "warning",
            message="Video review completed with validation findings.",
            data={"review_passed": False, "diagnostics": diagnostics},
        )
        await save_video_edit_session(session_state)
    return _json({
        **feedback,
        "status": "completed",
        "operation": "review",
        "review_passed": False,
        "quality_gate": "failed",
        "static_contract_valid": feedback["contract_valid"],
        "contract_valid": False,
        "diagnostics": diagnostics,
        # Only the strict check's findings (not the static contract) carry these.
        **({
            "quality_gate_rule": (
                "Strict review fails on every error and warning in diagnostics; fix each one, using "
                "its manor_hint and fix_hint, then review again. Advisories do not fail review."
            ),
            "advisories": advisories,
        } if advisories is not None else {}),
        "session_id": session_state.get("session_id") if session_state is not None else None,
        "project_path": project_rel,
        "sandbox_metrics": sandbox_metrics or {},
    })


# Runtime failures the runtime classified (and, where it can, already retried).
_STRUCTURED_RUNTIME_FAILURES = {
    "render_resource_limit", "render_timeout", "snapshot_timeout", "render_audio_failed",
    "snapshot_video_frames_missing",
}


_SANDBOX_KILL_REASONS = {"oom_killed": "oom", "pids_killed": "pid_limit", "disk_killed": "disk_full"}
_RUNTIME_EVIDENCE_FIELDS = ("missing_media", "unregistered_timelines", "warning_codes")


def _signal_from_exit(exit_code: int) -> int | None:
    if exit_code < 0:
        return -exit_code
    return exit_code - 128 if 128 < exit_code < 128 + 65 else None


def _structured_runtime_failure(
    failure_code: str, payload: dict[str, Any], *, exit_code: int = 0, stderr: str = "",
) -> dict[str, Any]:
    """The classification and diagnostics the agent acts on, the same for every exec path.

    The runtime classifies its own failures (``reason``/``retryable``/
    ``next_step``/``diagnostics``). A runtime that died before printing a
    result is described from the exec itself: its exit status, its signal, and
    the runner's kill verdict when it gave one.
    """
    if payload.get("code"):
        fields = {
            "reason": payload.get("reason"),
            "step": payload.get("step"),
            "retryable": bool(payload.get("retryable")),
            "automatic_retry": payload.get("automatic_retry"),
            "attempts": len(payload.get("attempts") or []),
            "next_step": payload.get("next_step"),
            **{key: payload[key] for key in _RUNTIME_EVIDENCE_FIELDS if payload.get(key)},
            "diagnostics": payload.get("diagnostics"),
        }
        if failure_code not in _STRUCTURED_RUNTIME_FAILURES and not payload.get("diagnostics"):
            return {}  # an older runtime: nothing classified to lift
        return {key: value for key, value in fields.items() if value is not None}
    outcome = payload.get("sandbox_outcome") or {}
    number = _signal_from_exit(exit_code)
    reason = _SANDBOX_KILL_REASONS.get(str(outcome.get("outcome") or ""))
    if reason is None:
        reason = "killed" if number is not None else "no_result"
    return {
        "reason": reason,
        "retryable": reason != "no_result",
        "next_step": (
            "Render again with render_workers=1; if it is killed again, render with "
            "quality='standard' or reduce how many layers play at once."
            if reason in {"oom", "pid_limit", "killed"}
            else "Free space in the sandbox work area and run the operation again."
            if reason == "disk_full"
            else "Report diagnostics to an operator: the video runtime exited without a result."
        ),
        "diagnostics": {
            "exit_code": exit_code,
            "signal": number,
            "sandbox_outcome": outcome.get("outcome"),
            "sandbox_outcome_detail": outcome.get("detail"),
            "stderr_tail": str(stderr or "")[-2000:],
        },
    }


def _log_runtime_failure(operation: str, failure_code: str, exit_code: int, fields: dict[str, Any]) -> None:
    diagnostics = fields.get("diagnostics") or {}
    memory = diagnostics.get("memory") or {}
    logger.warning(
        "video_edit %s failed: code=%s reason=%s retryable=%s exit=%s signal=%s stage=%s/%s "
        "duration=%ss memory_peak=%s memory_limit=%s oom_kill=%s outcome=%s last_error=%r",
        operation, failure_code, fields.get("reason"), fields.get("retryable"), exit_code,
        diagnostics.get("signal_name") or diagnostics.get("signal"), diagnostics.get("stage"),
        diagnostics.get("failed_stage"), diagnostics.get("duration_seconds"), memory.get("peak"),
        memory.get("limit"), memory.get("oom_kill"), diagnostics.get("sandbox_outcome"),
        ((diagnostics.get("error_lines") or [diagnostics.get("stderr_tail") or ""])[-1] or "")[-300:],
    )


def _render_missed_omitted_asset(exit_code: int, payload: dict[str, Any]) -> bool:
    """A render that left assets out and then could not find an audio source.

    Review runs against the whole project, so a page that builds asset paths in
    a way the render's asset selection did not foresee passes review and then
    finds files missing. Repeating the render with every source settles it.
    """
    return (
        exit_code != 0
        and payload.get("code") == "render_audio_failed"
        and "source_not_found" in (payload.get("reasons") or [payload.get("reason")])
        and int((payload.get("sandbox_metrics") or {}).get("omitted_assets") or 0) > 0
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
        await _ensure_video_session_sandbox(session_state, project_dir=project_dir, context=context)
        upload_sources = bool(session_state.get("legacy_external_authoring"))
        if not upload_sources:
            await _sync_video_session_to_entity(
                state=session_state,
                project_dir=project_dir,
                entity_id=entity_id,
                context=context,
            )
    feedback = _video_project_feedback(project_dir)
    missing = feedback["missing_project_contract"]
    if missing:
        return await _review_validation_result(
            project_rel=project_rel,
            feedback=feedback,
            diagnostics=[{
                "code": "project_contract_incomplete",
                "message": "Video project contract is incomplete.",
                "missing": missing,
                "violations": feedback["contract_violations"],
            }],
            session_state=session_state,
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
            context=context,
            sandbox_id=(str(session_state.get("sandbox_id") or "") if session_state is not None else ""),
            upload_sources=upload_sources,
            snapshot_times=times,
        )
    except VideoEditRuntimeUnavailable as exc:
        return _runtime_unavailable_error(exc)
    if exit_code != 0 or payload.get("status") != "ok":
        failure_code = (
            "sandbox_worker_killed"
            if exit_code == 137 and not payload.get("code")
            else str(payload.get("code") or "quality_gate_failed")
        )
        structured = _structured_runtime_failure(failure_code, payload, exit_code=exit_code, stderr=stderr)
        _log_runtime_failure("review", failure_code, exit_code, structured)
        if failure_code == "sandbox_worker_killed":
            event_message = "Video review worker was killed; recover the Sandbox and retry."
            message = "Video review worker was killed before returning quality findings."
        elif failure_code == "snapshot_timeout":
            # The sandbox browser timed out even after the runtime's own retry
            # with longer timeouts; editing the project does not fix a busy host.
            event_message = "Video review timed out in the sandbox browser; the project is unchanged."
            message = str(payload.get("error") or "Video review timed out in the sandbox browser.")
        elif failure_code == "snapshot_video_frames_missing":
            # Frames with black videos are never offered for approval; the
            # runtime's message says whether an asset or the host is at fault.
            event_message = "Video review could not capture video frames; no review frames were kept."
            message = str(payload.get("error") or "Video review could not capture video frames.")
        elif structured.get("retryable") and payload.get("error"):
            # The browser crashed or was killed: not a finding about the project.
            event_message = "Video review did not complete in the sandbox; the project is unchanged."
            message = str(payload["error"])
        else:
            event_message = "Video review gate failed; the project needs another edit pass."
            message = "Video sandbox review failed; fix every reported issue before asking for approval."
        if session_state is not None:
            append_video_edit_session_event(
                session_state,
                "warning",
                message=event_message,
                data={
                    "code": failure_code,
                    "exit_code": exit_code,
                },
            )
            await save_video_edit_session(session_state)
        return _json_error(
            message,
            code=failure_code,
            # Ahead of the bulky runtime output, which is cut first.
            **structured,
            exit_code=exit_code,
            runtime_result={key: value for key, value in payload.items() if key != "diagnostics"},
            stderr=stderr,
            **feedback,
        )

    if payload.get("review_passed") is False:
        report = payload.get("check_report")
        if (
            payload.get("operation") != "review"
            or not isinstance(report, dict)
            or report.get("ok") is not False
            or report.get("error")
            or not all(
                isinstance(report.get(section), dict)
                and isinstance(report[section].get("findings"), list)
                for section in _CHECK_SECTIONS
            )
        ):
            return _json_error("Video review returned an incomplete result.", code="review_result_invalid")
        diagnostics, advisories = _review_findings(report)
        return await _review_validation_result(
            project_rel=project_rel,
            feedback=feedback,
            diagnostics=diagnostics,
            advisories=advisories,
            session_state=session_state,
            sandbox_metrics=payload.get("sandbox_metrics"),
        )

    review_rel = "snapshots/manor-review"
    review_dir = project_dir / review_rel
    if review_dir.exists():
        with _tracked_write(review_dir):
            shutil.rmtree(review_dir)
    for rel_path, content in outputs.items():
        if not rel_path.startswith(f"{review_rel}/"):
            continue
        target = _resolve_inside(project_dir, rel_path)
        _tracked_mkdir(target.parent)
        with _tracked_write(target, sha256=hashlib.sha256(content).hexdigest()):
            target.write_bytes(content)
    review_files = sorted(
        [
            path
            for path in review_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ],
        key=lambda path: ("contact" not in path.name.lower(), path.name.lower()),
    )
    from packages.core.services.document_service import StorageLimitExceeded

    artifacts: list[dict[str, Any]] = []
    try:
        for path in review_files[:12]:
            with _tracked_write(path):
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
    except StorageLimitExceeded as exc:
        if session_state is not None:
            append_video_edit_session_event(
                session_state,
                "warning",
                message="Review passed, but Knowledge storage is full; free space and retry review.",
                data={"code": "knowledge_storage_limit", "source_sha256": source_hash},
            )
            await save_video_edit_session(session_state)
        return _json({
            "status": "blocked",
            "code": "knowledge_storage_limit",
            "error": str(exc),
            "quality_gate": "passed",
            "session_id": (session_state.get("session_id") if session_state is not None else None),
            "project_path": project_rel,
            "source_sha256": source_hash,
            "preview_frames_written": len(review_files),
            "next_step": "Free Knowledge storage or upgrade the plan, then retry review on this session.",
        })

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
            "files": [artifact["file"] for artifact in artifacts if artifact.get("file")],
            "approval_required": True,
            "execution_runtime": "isolated_sandbox",
            "sandbox_metrics": payload.get("sandbox_metrics", {}),
            "event_cursor": (session_state.get("last_event_seq") if session_state is not None else None),
            "events": ([session_state["events"][-1]] if session_state is not None else []),
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
    render_workers: int = 2,
    session_state: dict[str, Any] | None = None,
) -> str:
    upload_sources = True
    approved_hash = str(review_sha256 or "").strip().lower()
    # A durable run re-executes this call after its render container parked.
    # The first pass synced the session to Manor and released its sandbox for
    # this exact hash; while no new session sandbox was created since, there is
    # nothing left to sync, and recreating it would queue a default-pool
    # container the render never uses (staging 2026-10-19: that ticket expired).
    released = (session_state or {}).get("released_for_render") or {}
    already_released = (
        session_state is not None
        and released.get("sha256") == approved_hash
        and released.get("sandbox_id") == str(session_state.get("sandbox_id") or "")
    )
    if session_state is not None:
        upload_sources = bool(session_state.get("legacy_external_authoring"))
    if session_state is not None and not already_released:
        await _ensure_video_session_sandbox(session_state, project_dir=project_dir, context=context)
        if not upload_sources:
            await _sync_video_session_to_entity(
                state=session_state,
                project_dir=project_dir,
                entity_id=entity_id,
                context=context,
            )
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
    _tracked_mkdir(output_path.parent)

    if session_state is not None:
        # Render runs in its own sandbox, sized to the whole runner. Keeping the
        # authoring sandbox alive beside it doubles the memory claim (2 x 1.5 GB
        # on a 2 GB runner) and Chromium is killed mid-check. The source is
        # synced to Manor above; the next session operation recreates it.
        if not already_released:
            await _release_video_session_sandbox(session_state, context)
            session_state["released_for_render"] = {
                "sandbox_id": str(session_state.get("sandbox_id") or ""),
                "sha256": approved_hash,
            }
            await save_video_edit_session(session_state)

    render = functools.partial(
        _run_video_sandbox,
        project_dir=project_dir,
        operation="render",
        context=context,
        sandbox_id=(str(session_state.get("sandbox_id") or "") if session_state is not None else ""),
        upload_sources=upload_sources,
        output_rel=output_rel,
        quality=quality,
        render_workers=render_workers,
        expected_source_sha256=approved_hash,
    )
    try:
        exit_code, payload, outputs, stderr = await render()
        if _render_missed_omitted_asset(exit_code, payload):
            if await asyncio.to_thread(_project_source_sha256, project_dir) != approved_hash:
                return _json_error(
                    "Project source changed after review. Run review again and obtain fresh approval.",
                    code="source_changed_after_review",
                    approved_sha256=approved_hash,
                )
            exit_code, payload, outputs, stderr = await render(all_sources=True)
            payload["automatic_retry"] = "all_sources"
    except VideoEditRuntimeUnavailable as exc:
        return _runtime_unavailable_error(exc)
    output_bytes = outputs.get(output_rel, b"")
    if exit_code != 0 or payload.get("status") != "ok" or not output_bytes:
        failure_code = (
            "sandbox_worker_killed"
            if exit_code == 137 and not payload.get("code")
            else str(payload.get("code") or "render_failed")
        )
        if session_state is not None:
            append_video_edit_session_event(
                session_state,
                "warning",
                message="Final video render failed; the editable project is preserved.",
                data={
                    "code": failure_code,
                    "exit_code": exit_code,
                },
            )
            await save_video_edit_session(session_state)
        structured = _structured_runtime_failure(failure_code, payload, exit_code=exit_code, stderr=stderr)
        _log_runtime_failure("render", failure_code, exit_code, structured)
        if failure_code == "sandbox_worker_killed":
            message = "Video render worker was killed before producing an MP4."
        elif payload.get("error"):
            # The runtime classified the failure (and retried where it could);
            # say so in terms the agent can act on instead of "inspect stderr".
            message = str(payload["error"])
        else:
            message = "Video sandbox render failed or produced no MP4."
        return _json_error(
            message,
            code=failure_code,
            # Ahead of the bulky runtime output, which is cut first.
            **structured,
            exit_code=exit_code,
            runtime_result={key: value for key, value in payload.items() if key != "diagnostics"},
            stderr=stderr,
        )
    output_hash = hashlib.sha256(output_bytes).hexdigest()
    with _tracked_write(output_path, sha256=output_hash):
        output_path.write_bytes(output_bytes)

    probe = await _probe_render(output_path)
    with _tracked_write(output_path, sha256=output_hash):
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
            "file": artifact.get("file"),
            "media_probe": probe,
            "event_cursor": (session_state.get("last_event_seq") if session_state is not None else None),
            "events": ([session_state["events"][-1]] if session_state is not None else []),
        }
    )


def _settled_by_write_trace(handler):
    """Run one operation under a fresh write trace and settle its failure."""

    @functools.wraps(handler)
    async def settled(*args: Any, **kwargs: Any) -> Any:
        trace = _VideoWriteTrace(None)
        token = _VIDEO_WRITE_TRACE.set(trace)
        try:
            result = await handler(*args, **kwargs)
        finally:
            _VIDEO_WRITE_TRACE.reset(token)
        return _settle_video_edit_failure(result, trace)

    return settled


async def _render_closed_video_session(
    closed: dict[str, Any],
    *,
    entity_id: str,
    entity_root: Path,
    context: Any,
    project_rel: str,
    kwargs: dict[str, Any],
) -> str:
    """Render the hash a closed session reviewed, from the durable source.

    Close leaves exactly the reviewed source in Manor, so the render needs no
    authoring sandbox: it runs in its own and still renders only when the
    durable source hashes to the hash this session's passed review returned.
    """

    assert_video_edit_session_owner(
        closed,
        entity_id=entity_id,
        user_id=context.user_id,
        conversation_id=_video_binding_key(context),
    )
    if (
        context.workspace_id
        and str(closed.get("workspace_id") or "")
        and str(closed.get("workspace_id")) != str(context.workspace_id)
    ):
        raise PermissionError("Video Edit session belongs to a different Workspace")
    closed_project = str(closed.get("project_path") or "")
    if project_rel and project_rel != closed_project:
        raise ValueError("Video Edit session belongs to a different project path")
    approved = str(kwargs.get("review_sha256") or "").strip().lower()
    if approved != str(closed.get("last_review_sha256") or ""):
        return _json_error(
            "This closed session's last passed review did not return the supplied hash. "
            "Call start on its project_path and review again.",
            code="review_required",
            project_path=closed_project,
        )
    project_dir = _resolve_inside(entity_root, closed_project)
    if not project_dir.is_dir():
        return _json_error(f"Video project directory not found: {closed_project}", code="project_not_found")
    return await _render_project(
        project_dir=project_dir,
        project_rel=closed_project,
        entity_root=entity_root,
        entity_id=entity_id,
        context=context,
        review_sha256=approved,
        output_name=str(kwargs.get("output_name") or "renders/final.mp4"),
        quality=str(kwargs.get("quality") or "high"),
        render_workers=int(kwargs.get("render_workers") or _RENDER_MAX_WORKERS),
    )


@_settled_by_write_trace
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
        "patch_files",
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
        trace = _VIDEO_WRITE_TRACE.get()
        if trace is not None:
            trace.entity_root = entity_root
        context = runtime_tool_call_context_from_kwargs(
            {**kwargs, "_user_id_from_context": kwargs.get("_user_id_from_context") or user_id},
            entity_id=entity_id,
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
            closed = (
                await load_closed_video_edit_session(session_id)
                if session_id and operation == "render"
                else None
            )
            if closed is not None:
                return await _render_closed_video_session(
                    closed,
                    entity_id=entity_id,
                    entity_root=entity_root,
                    context=context,
                    project_rel=project_rel,
                    kwargs=kwargs,
                )
            if session_id or operation not in {"review", "render"} or not project_rel:
                raise
            # Backward compatibility for callers that authored durable source
            # with the old prepare/review/render contract and lost its session.
            project_dir = _resolve_inside(entity_root, project_rel)
            if not project_dir.is_dir():
                raise
            if not _video_owner_key(context):
                raise PermissionError(
                    "Video Edit sessions can only be started by a user or from a task run."
                )
            await _retire_previous_video_session(context, entity_id=entity_id, entity_root=entity_root)
            sandbox_id, metrics = await _create_video_session_sandbox(project_dir, context)
            session_state = new_video_edit_session(
                sandbox_id=sandbox_id,
                entity_id=entity_id,
                user_id=context.user_id,
                conversation_id=_video_binding_key(context),
                agent_id=context.agent_id,
                workspace_id=context.workspace_id,
                project_path=project_rel,
                legacy_external_authoring=True,
            )
            append_video_edit_session_event(
                session_state,
                "progress",
                message="Video Sandbox restored for a legacy durable project.",
                data={"sandbox_metrics": metrics, "authoring_contract_version": _VIDEO_AUTHORING_CONTRACT_VERSION},
            )
            await save_video_edit_session(session_state)
            await make_current_video_edit_session(session_state)

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
            files = kwargs.get("files")
            source_paths = kwargs.get("source_paths")
            if source_paths:
                copied = await _read_video_source_paths(
                    source_paths,
                    entity_id=entity_id,
                    entity_root=entity_root,
                    context=context,
                    workspace_base_dir=workspace_base_dir,
                )
                if isinstance(copied, str):
                    return copied
                files = dict(files) if isinstance(files, dict) else {}
                overlap = sorted(set(files) & set(copied))
                if overlap:
                    return _json_error("write_files got both content and source_paths for: " + ", ".join(overlap))
                files.update(copied)
            return await _write_video_session_files(
                state=session_state,
                entity_id=entity_id,
                project_dir=project_dir,
                files=files,
                delete_paths=list(kwargs.get("delete_paths") or []),
                context=context,
            )
        if operation == "patch_files":
            return await _patch_video_session_files(
                state=session_state,
                entity_id=entity_id,
                project_dir=project_dir,
                patches=kwargs.get("patches"),
                context=context,
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
                context=context,
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
                context=context,
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
                context=context,
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
                    **_video_project_feedback(project_dir),
                }
            )
        if operation == "sync":
            await _ensure_video_session_sandbox(
                session_state,
                project_dir=project_dir,
                context=context,
            )
            sync = await _sync_video_session_to_entity(
                state=session_state,
                project_dir=project_dir,
                entity_id=entity_id,
                context=context,
            )
            return _json(
                {
                    "status": "synchronized",
                    "session_id": session_state["session_id"],
                    "project_path": project_rel,
                    "sync": sync,
                    "event_cursor": session_state["last_event_seq"],
                    "events": [session_state["events"][-1]],
                    **_video_project_feedback(project_dir),
                }
            )
        if operation == "close":
            await _ensure_video_session_sandbox(
                session_state,
                project_dir=project_dir,
                context=context,
            )
            sync = await _sync_video_session_to_entity(
                state=session_state,
                project_dir=project_dir,
                entity_id=entity_id,
                context=context,
            )
            with _tracked_write(_session_sandbox_target(session_state)):
                closed = await _discard_video_session_sandbox(context, str(session_state["sandbox_id"]))
            if closed != "destroyed":
                return _json_error(
                    "The Video Edit session was synchronized, but its Sandbox could not be "
                    "destroyed yet; call close again.",
                    code="runtime_unavailable",
                    sync=sync,
                )
            await close_video_edit_session(session_state)
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
            render_workers=int(kwargs.get("render_workers") or _RENDER_MAX_WORKERS),
            session_state=session_state,
        )
    except VideoSandboxParked as parked:
        # A durable run waits for its container: the runtime re-executes this
        # very call once the scheduler allocated it.
        return parked.suspension
    except VideoReferenceAccessDenied as exc:
        # Raised only by the reference preflight, before the project directory,
        # sandbox or session record is touched: a host-proven no-op the agent
        # may correct or report, not an unconfirmed outcome.
        from packages.core.ai.runtime.tool_results import RuntimeNoEffectToolResult

        return RuntimeNoEffectToolResult.video_reference_denied(
            _json_error(str(exc), code="permission_denied", applied=False)
        )
    except VideoReferenceUnavailable as exc:
        # Same read-only preflight: nothing was created, staged or started.
        from packages.core.ai.runtime.tool_results import RuntimeNoEffectToolResult

        return RuntimeNoEffectToolResult.video_reference_unavailable(
            _json_error(str(exc), code="reference_unavailable", applied=False)
        )
    except PermissionError as exc:
        return _json_error(str(exc), code="permission_denied")
    except VideoEditRuntimeUnavailable as exc:
        return _runtime_unavailable_error(exc)
    except (FileNotFoundError, ValueError) as exc:
        return _json_error(str(exc))
    except Exception as exc:  # Keep tool failures actionable for the calling agent.
        return _json_error(f"Video editing operation failed: {exc}", code="operation_failed")


TOOL_GUIDANCE = {"video_edit": "video-edit"}


def get_tools():
    return [(VIDEO_EDIT_SCHEMA, _video_edit_handler)]
