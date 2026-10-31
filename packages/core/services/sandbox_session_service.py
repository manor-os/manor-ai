"""Owner-scoped sandbox sessions: the one way Manor gets a runner container.

Every consumer of the sandbox runner admits its container here: skill
invocation, the generic ``sandbox`` tool and ``bash`` (which runs in the
caller's general sandbox), ``generate_file`` office kinds, workflow code
nodes and Video Edit (session and render containers). Outside this module
only the scheduler's allocation code in ``packages.core.tasks.runtime_tasks``
-- which this module's reservations drive -- creates containers;
``tests/test_sandbox_single_entry_guard.py`` scans the repository for any
other ``SandboxClient`` construction or create call.
Callers describe *what* they need through ``SandboxBundle``/``SandboxBinding``
and receive a ``SandboxSession``.

A bundle's ``lifetime`` says how long its container lives (``SandboxLifetime``):

* ``WARM``: reused by later calls of the same scope and bound to the caller's
  sandbox-tool slot (skills, the generic tool);
* ``SESSION``: one tool-owned container per scope (Video Edit's authoring
  sandbox). Registered warm so cancels, the queue handover and routing treat it
  like any warm container, never bound to a model-visible slot, and replaced --
  never reused through ``load_skill`` -- whenever it is admitted again;
* ``ONE_SHOT``: never registered or bound; the caller destroys it with
  ``discard_one_shot_sandbox`` once its command finished (workflow code nodes,
  video renders).

A sandbox belongs to its OWNER (``RuntimeSandboxScope`` in
``packages.core.ai.runtime.sandbox``): the USER for human work -- one general
sandbox shared by all of the user's conversations, tasks, agents and Skills,
created on demand -- or, for user-less system work and group room runs, the
task (room run) itself. Skills coexist in that one container (each in
``/skills/<name>``, seen as ``/skill`` by the agents that invoked it), and
each conversation or task works in its own area of it. A bundle that needs
another container configuration (Remotion, video) gets a dedicated container
in the same owner scope (``sandbox_registry_scope`` roles). A warm container
is never handed to another owner. The acting agent and the conversation are
recorded for audit and in per-agent views, never in ownership.

Under durable external-runner coordination admission goes through the
reservation queue: a durable Runtime run is suspended on its ticket, while a
caller with no ``RuntimeRun`` to suspend (a task worker, a delegated agent)
joins the scope's in-flight ticket -- or enqueues it -- and waits
synchronously for the scheduler to allocate it. Concurrent admissions of one
scope coalesce onto one ticket and one container: under the scope's Postgres
lock (``sandbox_queue_service.lock_sandbox_scope``) Postgres says what the
scope has -- a ticket creating its container, or the container itself -- and
only a scope with neither gets a new ticket. The Redis owner registry is a
cache of that answer. A call that can neither park nor wait -- a tool inside a
prompt skill's nested loop -- gets a SESSION or ONE_SHOT container only when a
runner slot is free right now (``_allocate_now``); a WARM one is refused there.

Releasing keeps a container only while it is still its scope's warm session
(``sandbox:warm:{sandbox_id}`` names that scope); anything else — anonymous
owners, unregistered containers, containers superseded by a newer
registration — is destroyed at release instead of waiting for the sandbox
service's idle reaper.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from packages.core.ai.runtime.sandbox import (
    RUNTIME_SANDBOX_CONTEXT_TTL,
    RUNTIME_SANDBOX_IDLE_THRESHOLD,
    RuntimeSandboxKind,
    RuntimeSandboxScope,
    runtime_binding_is_live,
    runtime_delete_sandbox_binding,
    runtime_expire_sandbox_binding,
    runtime_init_sandbox_binding,
    runtime_load_sandbox_binding,
    runtime_release_agent_sandbox,
    runtime_sandbox_scope,
)
from packages.core.services.sandbox_baked_skills import (
    BakedSkillRef,
    create_builtin_sandbox,
    load_builtin_skill,
)
from packages.core.services.sandbox_sdk.capabilities import RunnerCapabilities

logger = logging.getLogger(__name__)

SANDBOX_OWNER_REGISTRY_PREFIX = "sandbox:owner:"
SANDBOX_WARM_MARKER_PREFIX = "sandbox:warm:"
SANDBOX_RELEASE_PROBE_PREFIX = "sandbox:release-probe:"
# The generic sandbox tool's sub-scope in the warm registry; the same value
# selects the GENERIC binding kind, so allocation and binding cannot disagree.
GENERIC_SANDBOX_REUSE_SCOPE = RuntimeSandboxKind.GENERIC.value
SANDBOX_SESSION_ERROR_CODES = frozenset(
    {
        "not_configured",
        "image_unavailable",
        "capacity_full",
        "busy_timeout",
        "create_failed",
        "context_persist_failed",
        "nested_allocation_unavailable",
    }
)
_RETRYABLE_CODES = frozenset({"capacity_full", "busy_timeout"})
_BUSY_STATUS_CODE = 409
# The runner answers 409 for several conflicts; only "busy" ends by waiting.
# A 409 without a code comes from a runner that predates error codes, where
# 409 meant busy.
_BUSY_ERROR_CODE = "sandbox_busy"
_CLIENT_TIMEOUT_SECONDS = 180.0
_LOCK_TTL_SECONDS = 240
_LOCK_HEARTBEAT_SECONDS = 60


@dataclass(frozen=True)
class SandboxOwner:
    """The principal that owns a scope's sandboxes.

    A human run is owned by its user. A user-less run (Strategist/scheduler/
    system task) is owned by its workspace, or by the entity when it has no
    workspace; its scope is keyed by the task, so it never shares a
    container with a human. Every owner registers its warm sandboxes: the
    registry ``scope`` is always conversation-prefixed (see
    ``sandbox_reuse_scope``), so a container warmed by one conversation (or
    task) is never handed to another.
    """

    entity_id: str
    user_id: str
    workspace_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "entity_id", str(self.entity_id or "").strip())
        object.__setattr__(self, "user_id", str(self.user_id or "").strip())
        object.__setattr__(self, "workspace_id", "" if self.user_id else str(self.workspace_id or "").strip())
        if not self.entity_id:
            raise ValueError("SandboxOwner requires entity_id")
        if ":" in self.entity_id or ":" in self.user_id or ":" in self.workspace_id:
            raise ValueError("SandboxOwner ids must not contain ':'")

    @property
    def principal(self) -> str:
        """The registry segment of the owner: the user, or ``system`` for user-less work.

        User-less work belongs to ``workspace_id`` (or the entity) for
        attribution, and is isolated by its scope: one task.
        """

        return self.user_id or "system"

    @property
    def registrable(self) -> bool:
        # Every owner has a principal now; user-less work is owned by its
        # workspace (or entity) and scoped by its task.
        return True

    @property
    def registry_prefix(self) -> str:
        return f"{SANDBOX_OWNER_REGISTRY_PREFIX}{self.entity_id}:{self.principal}:"

    def registry_key(self, scope: str) -> str:
        if not scope:
            raise ValueError("registry scope is required")
        return f"{self.registry_prefix}{scope}"

    def lock_key(self, scope: str) -> str:
        return f"{self.registry_key(scope)}:lock"


@dataclass
class SandboxSession:
    """Registry record plus per-acquire details that callers render."""

    sandbox_id: str
    base_url: str
    created_at: float
    last_acquired_at: float
    loaded_skills: list[str] = field(default_factory=list)
    # How the sandbox service hands skill env to processes: "exec" means each
    # docker exec receives the current skill's env and load_skill replaces it;
    # "create" (older services) means the first skill's env is baked into the
    # container and must never be shared with another credentialed skill.
    env_injection: str = "create"
    # The skill whose files are in /skill right now, and whether any skill
    # ever put credentials into this container. Both matter only for
    # containers with env baked at create ("create" mode): there the env
    # cannot be replaced, so reuse is limited to the same skill.
    current_skill: str | None = None
    holds_credentials: bool = False
    # Fingerprint of the env the container was last given; on "create" mode
    # reuse requires the same env, which covers gained, rotated, and revoked
    # credentials in one rule.
    env_fingerprint: str | None = None
    # The container settings the container was created for (the bundle's
    # explicit overrides; ``{}`` = runner defaults; ``None`` = a record from
    # before this was tracked) and its effective config as the runner
    # reported it. Warm reuse compares a new bundle's asks against both, so a
    # 512m container is never handed to a Skill that asks for 2g.
    requested_config: dict[str, Any] | None = None
    container_config: dict[str, Any] | None = None
    # True on a runner that keeps Skills side by side (/skills/<name>): a
    # load never touches another Skill, so one container holds them all.
    skill_slots: bool = False
    # Env fingerprint per Skill loaded side by side (skill_slots only).
    skill_envs: dict[str, str | None] = field(default_factory=dict)
    # The isolation the container was created for (``SandboxBundle.isolation_key``).
    isolation_key: str = ""
    # Per-acquire, never persisted.
    reused: bool = False
    workdir: str = "/skill"
    scripts: list[str] = field(default_factory=list)
    entry_hint: str | None = None
    requirements_installed: bool = False
    env_blocked: list[str] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        return {
            "sandbox_id": self.sandbox_id,
            "base_url": self.base_url,
            "created_at": self.created_at,
            "last_acquired_at": self.last_acquired_at,
            "loaded_skills": list(self.loaded_skills),
            "env_injection": self.env_injection,
            "current_skill": self.current_skill,
            "holds_credentials": self.holds_credentials,
            "env_fingerprint": self.env_fingerprint,
            "requested_config": self.requested_config,
            "container_config": self.container_config,
            "skill_slots": self.skill_slots,
            "skill_envs": dict(self.skill_envs),
            "isolation_key": self.isolation_key,
        }

    @staticmethod
    def _as_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def from_record(cls, record: Any) -> "SandboxSession | None":
        if not isinstance(record, dict):
            return None
        sandbox_id = str(record.get("sandbox_id") or "").strip()
        if not sandbox_id:
            return None
        raw_skills = record.get("loaded_skills")
        return cls(
            sandbox_id=sandbox_id,
            base_url=str(record.get("base_url") or ""),
            created_at=cls._as_float(record.get("created_at")),
            last_acquired_at=cls._as_float(record.get("last_acquired_at")),
            loaded_skills=([str(item) for item in raw_skills if str(item)] if isinstance(raw_skills, list) else []),
            env_injection=str(record.get("env_injection") or "create"),
            current_skill=(str(record.get("current_skill")) if record.get("current_skill") else None),
            holds_credentials=bool(record.get("holds_credentials")),
            env_fingerprint=(str(record.get("env_fingerprint")) if record.get("env_fingerprint") else None),
            requested_config=(
                dict(record["requested_config"]) if isinstance(record.get("requested_config"), dict) else None
            ),
            container_config=(
                dict(record["container_config"]) if isinstance(record.get("container_config"), dict) else None
            ),
            skill_slots=bool(record.get("skill_slots")),
            skill_envs=(
                {str(key): (str(value) if value else None) for key, value in record["skill_envs"].items()}
                if isinstance(record.get("skill_envs"), dict)
                else {}
            ),
            isolation_key=str(record.get("isolation_key") or ""),
        )


def env_fingerprint(env: dict[str, str] | None) -> str | None:
    """Keyed digest of a skill env (None for an empty env).

    Only ever compared, never published: it is an HMAC under the sandbox API
    token so a reader of the registry cannot confirm a guessed credential
    value against it.
    """

    if not env:
        return None
    material = "\n".join(f"{key}={env[key]}" for key in sorted(env))
    key = str(_settings().SANDBOX_API_TOKEN or "").strip() or "manor-sandbox-env-fingerprint"
    return hmac.new(key.encode("utf-8"), material.encode("utf-8"), hashlib.sha256).hexdigest()


class SandboxLifetime(str, Enum):
    """How long an admitted container lives, and who may reach it."""

    # Reused by later calls of its scope through ``load_skill``; bound to the
    # caller's sandbox-tool slot so the model operates it by id.
    WARM = "warm"
    # One container per scope, owned by the tool that admitted it (its own
    # record is the binding): registered warm, never bound, and replaced --
    # the registered one destroyed -- every time the scope admits again.
    SESSION = "session"
    # Never registered or bound: the caller runs its command and destroys the
    # container with ``discard_one_shot_sandbox``.
    ONE_SHOT = "one_shot"
    # One long-lived container per owner scope (``user:<reuse_scope>``), owned
    # by the service that admitted it and reached from any surface (REST,
    # tools, background jobs): no conversation is needed. Like SESSION it is
    # never bound to a sandbox-tool slot; unlike SESSION a later admission
    # reuses it while it is alive and fits (config, ``isolation_key``), and
    # only ``discard_session_sandbox`` destroys it.
    PERSISTENT = "persistent"


@dataclass(frozen=True)
class SandboxBundle:
    """Everything needed to create a sandbox or load a skill into one."""

    skill_name: str
    files: dict[str, str]
    env: dict[str, str] = field(default_factory=dict)
    allowed_sensitive_keys: tuple[str, ...] = ()
    source: str = "builtin"  # "builtin" | "workspace"
    config: Any = None
    auto_install: bool = True
    # Optional sub-scope, nested under the conversation
    # (``GENERIC_SANDBOX_REUSE_SCOPE`` for the generic tool).
    reuse_scope: str | None = None
    # Runner pool the reservation must be placed in (see
    # ``claim_next_sandbox_allocation``).
    pool: str = "default"
    lifetime: SandboxLifetime = SandboxLifetime.WARM
    # Distinguishes the reservations one durable tool call queues: a Video
    # Edit render may need its session container and a render container, and
    # a reservation is unique per (run, tool call). Empty for the classic
    # one-container-per-call consumers, whose tickets keep the bare call id.
    purpose: str = ""
    # A packaged built-in Skill the sandbox image may already hold: when the
    # runner advertises this digest the files are not sent, and ``files`` may
    # be empty (they are read only for an upload). See sandbox_baked_skills.
    baked_skill: BakedSkillRef | None = None
    # The Skill's relative paths when ``files`` was left empty for a baked Skill.
    baked_file_paths: tuple[str, ...] = ()
    # What the container must be created with beyond its config
    # (packages/core/services/sandbox_isolation.py): ``isolation_ref`` is
    # resolved into create arguments only where the container is created, and
    # a container is reused only for a bundle with the same ``isolation_key``.
    isolation_key: str = ""
    isolation_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifetime", SandboxLifetime(self.lifetime))
        if self.lifetime in (SandboxLifetime.SESSION, SandboxLifetime.PERSISTENT) and not self.reuse_scope:
            # Without its own sub-scope a session would replace the agent's
            # Skill sandbox registered under the bare conversation scope.
            raise ValueError(f"a {self.lifetime.value.upper()} sandbox bundle requires its own reuse_scope")
        if self.isolation_ref and not self.isolation_key:
            raise ValueError("an isolation_ref names the isolation_key its container is made for")
        if self.lifetime is not SandboxLifetime.WARM and self.reuse_scope == GENERIC_SANDBOX_REUSE_SCOPE:
            raise ValueError("the generic sandbox scope is WARM")

    def file_paths(self) -> list[str]:
        """Relative paths of the bundle's files, whether or not they were read."""

        return list(self.files) if self.files else list(self.baked_file_paths)

    @property
    def binding_kind(self) -> RuntimeSandboxKind | None:
        """The binding slot a sandbox admitted for this bundle occupies.

        ``None`` for SESSION and ONE_SHOT containers: no model-visible sandbox
        tool may operate them, so admitting one never touches (or overwrites)
        the agent's generic or Skill slot.
        """

        if self.lifetime is not SandboxLifetime.WARM:
            return None
        if self.reuse_scope == GENERIC_SANDBOX_REUSE_SCOPE:
            return RuntimeSandboxKind.GENERIC
        return RuntimeSandboxKind.SKILL

    @property
    def reservation_suffix(self) -> str:
        return f":{self.purpose}" if self.purpose else ""


@dataclass(frozen=True)
class SandboxBinding:
    """Conversation and durable-runtime identity of the acquiring call."""

    conversation_id: str | None = None
    agent_id: str | None = None
    skill_id: str = ""
    runtime_run_id: str | None = None
    runtime_tool_call_id: str | None = None
    # Task-worker identity: set when the call runs under a task instead of a
    # durable Runtime run, and is what a task reservation is requested for.
    task_id: str | None = None
    # Workflow-run identity: a Workflow step is a background requester like a
    # task worker (no durable Runtime run to park), so it waits on its own
    # ticket for the run instead of failing when no runner is free yet.
    workflow_run_id: str | None = None
    # The prompt skill whose nested loop is acquiring. Set only inside such a
    # loop; see ``acquire_sandbox_session`` for why it refuses a reservation.
    parent_skill: str | None = None
    # Owner of the skill whose stored credentials the bundle carries; recorded
    # on the conversation's sandbox context (credential provenance).
    credentials_owner_user_id: str | None = None
    # Any other background caller that waits on its own ticket, named for the
    # reservation (``"<kind>:<id>"``), e.g. a service booting a PERSISTENT
    # container outside every runtime.
    background_requester: str | None = None

    @property
    def waits_for_ticket(self) -> bool:
        """A background requester (task, Workflow run, service) can wait for a runner.

        A prompt skill's nested loop cannot (it cannot be suspended), and a
        caller outside both runtimes allocates now or not at all.
        """

        return not self.parent_skill and bool(self.task_id or self.workflow_run_id or self.background_requester)


@dataclass(frozen=True)
class SandboxSessionError:
    """Admission outcome returned (never raised) by the session service."""

    code: str
    message: str
    retryable: bool
    # The code the create was refused with (the runner's, or
    # ``isolation_unavailable``), so a caller can tell its user why; "" otherwise.
    cause: str = ""

    @classmethod
    def of(cls, code: str, message: str, *, cause: str = "") -> "SandboxSessionError":
        if code not in SANDBOX_SESSION_ERROR_CODES:
            raise ValueError(f"unknown sandbox session error code: {code}")
        return cls(code=code, message=message, retryable=code in _RETRYABLE_CODES, cause=cause)


_ERROR_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _ticket_error_cause(last_error: str) -> str:
    """The runner's code a failed ticket recorded (``"<code>: <detail>"``), or ""."""

    label = str(last_error or "").split(":", 1)[0].strip()
    return label if _ERROR_LABEL.match(label) else ""


# ── Registry ────────────────────────────────────────────────────


async def load_sandbox_session(owner: SandboxOwner, *, scope: str) -> SandboxSession | None:
    if not owner.registrable:
        return None
    key = owner.registry_key(scope)
    try:
        from packages.core.cache import cache

        record = await cache.get(key)
        return SandboxSession.from_record(record)
    except Exception:
        logger.warning("[sandbox.session] registry record unreadable for %s", key)
        return None


def _warm_marker_key(sandbox_id: str) -> str:
    """Reverse index: which registry key (if any) holds this sandbox."""

    return f"{SANDBOX_WARM_MARKER_PREFIX}{sandbox_id}"


async def warm_registry_key_for(sandbox_id: str) -> str | None:
    """Registry key this sandbox was last registered under, or None."""

    try:
        from packages.core.cache import cache

        value = await cache.get(_warm_marker_key(sandbox_id))
    except Exception as exc:  # noqa: BLE001 - a cache blip is not a marker
        logger.warning("[sandbox.session] warm marker unreadable for sandbox=%s: %s", sandbox_id, exc)
        return None
    key = str(value or "").strip()
    return key or None


async def _delete_warm_marker(sandbox_id: str) -> None:
    from packages.core.cache import cache

    await cache.delete(_warm_marker_key(sandbox_id))


async def warm_sandbox_base_url(sandbox_id: str) -> str | None:
    """Runner address recorded for a warm-registered sandbox, if any.

    Routing fallback: the instance row is the primary route, but a warm
    container the registry still points at must stay reachable even if that
    row was released by mistake.
    """

    registry_key = await warm_registry_key_for(sandbox_id)
    if not registry_key or not await _is_current_warm_session(registry_key, sandbox_id):
        return None
    try:
        from packages.core.cache import cache

        record = await cache.get(registry_key)
    except Exception as exc:  # noqa: BLE001 - cache down means no fallback route
        logger.debug("[sandbox.session] warm route lookup failed for sandbox=%s: %s", sandbox_id, exc)
        return None
    session = SandboxSession.from_record(record)
    return (session.base_url or None) if session is not None else None


async def sandbox_holds_credentials(sandbox_id: str) -> bool | None:
    """Whether any Skill ever put credentials into ``sandbox_id`` (None: no record of it).

    Matters on a runner that cannot keep a Skill's env from other commands
    (no ``skill_slots``): there every command in the container may see it.
    """

    registry_key = await warm_registry_key_for(sandbox_id)
    if not registry_key:
        return None
    try:
        from packages.core.cache import cache

        session = SandboxSession.from_record(await cache.get(registry_key))
    except Exception as exc:  # noqa: BLE001 - unknown, the caller stays conservative
        logger.warning("[sandbox.session] registry record unreadable for sandbox=%s: %s", sandbox_id, exc)
        return None
    if session is None or session.sandbox_id != sandbox_id:
        return None
    return session.holds_credentials


async def is_sandbox_kept_warm(sandbox_id: str) -> bool:
    """True when ``sandbox_id`` may be some scope's current warm session.

    Run-scoped cleanups (cancel) must not destroy such a container: it is
    owned by the owner registry and reused by later turns. An unreadable cache
    cannot prove the container is unclaimed, so it answers True, the same rule
    ``release_sandbox_session`` applies before destroying anything.
    """

    registry_key = await warm_registry_key_for(sandbox_id)
    if registry_key and await _is_current_warm_session(registry_key, sandbox_id):
        return True
    return not await _cache_reachable(sandbox_id)


async def sandbox_runner_base_url(sandbox_id: str) -> str:
    """Address of the runner that owns an existing sandbox.

    The one route lookup for stateful calls on a sandbox: outside
    external-runner mode the single configured service owns every sandbox;
    in external-runner mode the instance row names the runner, and a
    container the owner registry still keeps warm stays reachable through its
    registry record if run-level bookkeeping released that row.
    """

    from packages.core.config import get_settings

    settings = get_settings()
    if settings.SANDBOX_COORDINATION_MODE != "external-runner":
        return settings.SANDBOX_SERVICE_URL.strip()
    from packages.core.database import async_session
    from packages.core.services.sandbox_queue_service import (
        SandboxRouteNotFoundError,
        resolve_sandbox_runner,
    )

    try:
        async with async_session() as db:
            runner = await resolve_sandbox_runner(db, sandbox_id)
        return runner.base_url
    except SandboxRouteNotFoundError:
        base_url = await warm_sandbox_base_url(sandbox_id)
        if not base_url:
            raise
        logger.info("[sandbox.session] routing sandbox=%s through its warm registry record", sandbox_id)
        return base_url


async def register_sandbox_session(owner: SandboxOwner, session: SandboxSession, *, scope: str) -> bool:
    if not owner.registrable:
        return False
    key = owner.registry_key(scope)
    try:
        from packages.core.cache import cache

        written = bool(
            await cache.set(
                key,
                session.to_record(),
                ttl=RUNTIME_SANDBOX_CONTEXT_TTL,
            )
        )
        if written:
            # Reverse marker: release/destroy can ask "is this container the
            # current warm session of its scope?" without knowing the scope.
            # Its failure never invalidates the registration itself; the worst
            # case is a release that destroys a still-registered container.
            try:
                await cache.set(
                    _warm_marker_key(session.sandbox_id),
                    key,
                    ttl=RUNTIME_SANDBOX_CONTEXT_TTL,
                )
            except Exception as exc:  # noqa: BLE001 - registration still stands
                logger.warning(
                    "[sandbox.session] warm marker write failed for sandbox=%s: %s",
                    session.sandbox_id,
                    exc,
                )
        return written
    except Exception as exc:
        logger.warning("[sandbox.session] registry write failed: %s", exc)
        return False


async def forget_sandbox_session(owner: SandboxOwner, sandbox_id: str, *, scope: str) -> None:
    current = await load_sandbox_session(owner, scope=scope)
    if current is None or current.sandbox_id != sandbox_id:
        return
    key = owner.registry_key(scope)
    try:
        from packages.core.cache import cache

        await cache.delete(key)
        await cache.delete(_warm_marker_key(sandbox_id))
    except Exception as exc:
        logger.warning("[sandbox.session] registry delete failed for %s: %s", key, exc)


async def forget_vanished_sandbox(
    sandbox_id: str,
    *,
    scope: RuntimeSandboxScope | None = None,
) -> str | None:
    """Drop the registry entry and binding of a container its runner no longer has.

    The runner's idle reaper removes containers without telling anyone, while
    the bindings and the warm registry live for days. Left alone they keep
    routing the model's calls to a dead id. The caller's ``scope`` record for
    the sandbox keeps its other keys (an ``expired_sandbox_id`` marker
    replaces ``sandbox_id``) so a retry with the old id -- by any agent of the
    conversation -- is answered as an expiry rather than a denial, and
    re-opening the same Skill still restores whatever the record carried.

    Returns the skill last loaded into the container, for the re-open hint.
    Never raises.
    """

    skill: str | None = None
    try:
        registry_key = await warm_registry_key_for(sandbox_id)
        if registry_key:
            from packages.core.cache import cache

            current = SandboxSession.from_record(await cache.get(registry_key))
            if current is not None and current.sandbox_id == sandbox_id:
                skill = current.current_skill or (current.loaded_skills[-1] if current.loaded_skills else None)
                await cache.delete(registry_key)
            await _delete_warm_marker(sandbox_id)
    except Exception as exc:  # noqa: BLE001 - a stale registry entry is probed on next acquire anyway
        logger.warning("[sandbox.session] vanished sandbox=%s registry cleanup failed: %s", sandbox_id, exc)

    if scope is not None:
        try:
            ctx = await runtime_load_sandbox_binding(scope, sandbox_id)
            if runtime_binding_is_live(ctx, sandbox_id):
                skill = skill or str(ctx.get("skill_key") or "") or None
                await runtime_expire_sandbox_binding(scope, sandbox_id, skill)
        except Exception as exc:  # noqa: BLE001 - the record expires on its own
            logger.warning("[sandbox.session] vanished sandbox=%s binding cleanup failed: %s", sandbox_id, exc)

    if _settings().SANDBOX_COORDINATION_MODE == "external-runner":
        try:
            async with _release_db_session(None) as session:
                await _mark_sandbox_released(sandbox_id, db=session)
        except Exception as exc:  # noqa: BLE001 - the runner reconcile sweep releases it too
            logger.warning("[sandbox.session] vanished sandbox=%s release bookkeeping failed: %s", sandbox_id, exc)
    logger.info("[sandbox.session] vanished sandbox=%s unbound (skill=%s)", sandbox_id, skill or "-")
    return skill


async def _is_current_warm_session(registry_key: str, sandbox_id: str) -> bool:
    try:
        from packages.core.cache import cache

        record = await cache.get(registry_key)
    except Exception as exc:  # noqa: BLE001 - unreadable registry: keep the container
        logger.warning(
            "[sandbox.session] registry unreadable while releasing sandbox=%s: %s",
            sandbox_id,
            exc,
        )
        return True
    current = SandboxSession.from_record(record)
    return current is not None and current.sandbox_id == sandbox_id


async def _cache_reachable(sandbox_id: str) -> bool:
    """Is Redis actually up? ``cache.get`` returns None for both "missing" and
    "unavailable", so an absent warm marker alone is not proof that nothing can
    reuse this container. A one-second lease is the cheapest write-path probe:
    ``acquire_lease`` returns None (and only None) when the cache is down.
    """

    from packages.core.cache import cache

    key = f"{SANDBOX_RELEASE_PROBE_PREFIX}{sandbox_id}"
    token = uuid.uuid4().hex
    try:
        probe = await cache.acquire_lease(key, token, ttl=1)
    except Exception as exc:  # noqa: BLE001 - an unusable cache is an unreachable one
        logger.warning("[sandbox.session] release probe failed for sandbox=%s: %s", sandbox_id, exc)
        return False
    if probe is None:
        return False
    if probe:
        try:
            await cache.release_lease(key, token)
        except Exception as exc:  # noqa: BLE001 - the 1s ttl cleans up after us
            logger.debug("[sandbox.session] release probe lease not returned: %s", exc)
    return True


@asynccontextmanager
async def _release_db_session(db: Any | None):
    """Yield the caller's session, or open one session for the whole release."""

    if db is not None:
        yield db
        return
    from packages.core.database import async_session

    async with async_session() as session:
        yield session


async def _resolve_release_base_url(sandbox_id: str, *, db: Any | None, external: bool) -> str:
    if not external:
        return _settings().SANDBOX_SERVICE_URL.strip()
    from packages.core.services.sandbox_queue_service import resolve_sandbox_runner

    runner = await resolve_sandbox_runner(db, sandbox_id)
    return str(runner.base_url or "").strip()


async def _mark_sandbox_released(sandbox_id: str, *, db: Any | None) -> None:
    """External-runner bookkeeping: release the instance and its runner slot."""

    from packages.core.services.sandbox_queue_service import release_sandbox_instance

    await release_sandbox_instance(db, sandbox_id=sandbox_id)
    await db.commit()


async def _destroy_container(sandbox_id: str, *, db: Any | None, external: bool) -> str:
    """Destroy the container, then do best-effort bookkeeping.

    The outcome is latched at the destroy call: once the container is gone it
    stays ``"destroyed"``, whatever the DB bookkeeping or marker cleanup do.
    """

    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

    try:
        base_url = await _resolve_release_base_url(sandbox_id, db=db, external=external)
    except Exception as exc:  # noqa: BLE001 - unroutable sandbox: leave it to the reaper
        logger.warning(
            "[sandbox.session] release could not route sandbox=%s for destroy: %s",
            sandbox_id,
            exc,
        )
        return "destroy_failed"
    if not base_url:
        logger.warning(
            "[sandbox.session] release has no sandbox service url; sandbox=%s not destroyed",
            sandbox_id,
        )
        return "destroy_failed"

    destroyed = False
    client = None
    try:
        client = _new_client(base_url)
        try:
            await client.destroy(sandbox_id=sandbox_id)
        except SandboxNotFoundError:
            logger.info("[sandbox.session] release: sandbox=%s already gone", sandbox_id)
        destroyed = True
    except Exception as exc:  # noqa: BLE001 - never raise out of a release
        logger.warning("[sandbox.session] release destroy failed for sandbox=%s: %s", sandbox_id, exc)
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001 - the container is what matters
                logger.warning("[sandbox.session] release client close failed: %s", exc)
    if not destroyed:
        return "destroy_failed"

    if external:
        try:
            await _mark_sandbox_released(sandbox_id, db=db)
        except Exception as exc:  # noqa: BLE001 - the container is already gone
            logger.warning(
                "[sandbox.session] release bookkeeping failed for destroyed sandbox=%s: %s",
                sandbox_id,
                exc,
            )
    try:
        await _delete_warm_marker(sandbox_id)
    except Exception as exc:  # noqa: BLE001 - a stale marker expires on its own
        logger.warning("[sandbox.session] warm marker delete failed for sandbox=%s: %s", sandbox_id, exc)
    logger.info("[sandbox.session] released and destroyed (not reusable): sandbox=%s", sandbox_id)
    return "destroyed"


async def _destroy_released_sandbox(sandbox_id: str, *, db: Any | None) -> str:
    """Destroy a container nothing can reuse. Returns "destroyed"/"destroy_failed"."""

    if _settings().SANDBOX_COORDINATION_MODE != "external-runner":
        return await _destroy_container(sandbox_id, db=None, external=False)

    # One session for routing and bookkeeping both.
    outcome = "destroy_failed"
    try:
        async with _release_db_session(db) as session:
            outcome = await _destroy_container(sandbox_id, db=session, external=True)
    except Exception as exc:  # noqa: BLE001 - a teardown blip never unmakes a destroy
        logger.warning("[sandbox.session] release db session failed for sandbox=%s: %s", sandbox_id, exc)
    return outcome


async def release_sandbox_session(
    *,
    owner: SandboxOwner | None,
    sandbox_id: str,
    scope: RuntimeSandboxScope | None = None,
    agent_id: str | None = None,
    db: Any | None = None,
) -> str:
    """Release one agent's use of a conversation sandbox; destroy only what nobody can reuse.

    The sandbox belongs to the owner x conversation, not to the agent: the
    acting agent's view of it is dropped (it no longer drives a Skill there),
    while the conversation keeps the sandbox for its other agents and later
    turns as long as it is its scope's warm session.

    Returns ``"kept"`` when the sandbox is still its scope's current warm
    session (the scope binding stays), ``"destroyed"`` when it was destroyed
    because nothing can reuse it (unregistered container, or one superseded
    by a newer registration; the scope binding is dropped), and
    ``"destroy_failed"`` when the destroy did not go through and the idle
    reaper has to reclaim it. A container is also kept when the cache is
    unreachable: without Redis, "no warm marker" is indistinguishable from
    "cannot read the warm marker". Never raises.
    """

    if scope is not None:
        await runtime_release_agent_sandbox(scope, agent_id, sandbox_id)

    owner_label = f"{owner.entity_id}:{owner.principal}" if owner is not None else "(anonymous)"
    registry_key = await warm_registry_key_for(sandbox_id)
    if registry_key and await _is_current_warm_session(registry_key, sandbox_id):
        logger.info(
            "[sandbox.session] released by agent=%s, kept warm for the conversation: sandbox=%s owner=%s scope=%s",
            agent_id or "main",
            sandbox_id,
            owner_label,
            registry_key,
        )
        # The instance row stays "active": it records which runner hosts the
        # container, and every later exec in a reused container resolves the
        # runner through it. A run cancel is kept from destroying the
        # container by the warm-marker check in cancel_runtime_run_resources.
        return "kept"
    if registry_key is None and not await _cache_reachable(sandbox_id):
        logger.warning(
            "[sandbox.session] cannot verify warm status; leaving sandbox=%s to the idle reaper",
            sandbox_id,
        )
        return "kept"
    if scope is not None:
        await runtime_delete_sandbox_binding(scope, sandbox_id, tombstone=True)
    return await _destroy_released_sandbox(sandbox_id, db=db)


async def discard_sandbox_session(
    *,
    owner: SandboxOwner,
    sandbox_id: str,
    conversation_id: str,
    agent_id: str,
    db: Any | None = None,
) -> str:
    """Destroy a one-shot skill sandbox instead of warming an unrepeatable scope.

    A group Agent run has a unique synthetic conversation key. Keeping its
    container warm cannot serve a later run and would consume runner capacity.
    Verify the exact owner/scope/id before destroying anything.
    """

    del agent_id  # ownership is the owner x run key; the agent is not part of it
    _, base_scope = sandbox_reuse_scope(
        SandboxBundle(skill_name="", files={}), SandboxBinding(conversation_id=conversation_id), owner,
    )
    session = None
    registry_scope = None
    registry_key = await warm_registry_key_for(sandbox_id)
    owner_prefix = owner.registry_key(base_scope) if base_scope else ""
    if registry_key and owner_prefix and (registry_key == owner_prefix or registry_key.startswith(owner_prefix + ":")):
        registry_scope = registry_key[len(owner.registry_prefix):]
        found = await load_sandbox_session(owner, scope=registry_scope)
        if found is not None and found.sandbox_id == sandbox_id:
            session = found
    binding_scope = runtime_sandbox_scope(
        entity_id=owner.entity_id,
        user_id=owner.user_id,
        binding_key=conversation_id,
        workspace_id=owner.workspace_id,
    )
    bound = binding_scope is not None and runtime_binding_is_live(
        await runtime_load_sandbox_binding(binding_scope, sandbox_id), sandbox_id
    )
    if session is None and not bound:
        return "not_owned"
    if binding_scope is not None:
        await runtime_delete_sandbox_binding(binding_scope, sandbox_id)
    outcome = await _destroy_released_sandbox(sandbox_id, db=db)
    if outcome == "destroyed" and registry_scope is not None:
        await forget_sandbox_session(owner, sandbox_id, scope=registry_scope)
    return outcome


async def discard_one_shot_sandbox(sandbox_id: str, *, db: Any | None = None) -> str:
    """Destroy a ONE_SHOT container after its command, and return its slot.

    Under external-runner coordination the instance row is released, which
    hands the runner slot back to the queue. Returns "destroyed" or
    "destroy_failed" (the idle reaper and the runner's vanished-instance sweep
    reclaim it then). Never raises.
    """

    sandbox_id = str(sandbox_id or "").strip()
    if not sandbox_id:
        return "destroy_failed"
    try:
        return await _destroy_released_sandbox(sandbox_id, db=db)
    except Exception as exc:  # noqa: BLE001 - cleanup never raises at the caller
        logger.warning("[sandbox.session] one-shot sandbox=%s discard failed: %s", sandbox_id, exc)
        return "destroy_failed"


async def discard_session_sandbox(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    sandbox_id: str,
    db: Any | None = None,
) -> str:
    """Destroy a SESSION or PERSISTENT container its owner closed or replaced, and forget it.

    The tool's own record is the binding and the caller has checked it, so the
    container is destroyed whether or not the warm registry still names it;
    the registry entry is dropped only while it names this container. Never
    raises.
    """

    if bundle.lifetime not in (SandboxLifetime.SESSION, SandboxLifetime.PERSISTENT):
        raise ValueError("discard_session_sandbox takes a SESSION or PERSISTENT bundle")
    outcome = await discard_one_shot_sandbox(sandbox_id, db=db)
    _, scope = sandbox_reuse_scope(bundle, binding, owner)
    if scope and owner.registrable:
        try:
            await forget_sandbox_session(owner, sandbox_id, scope=scope)
        except Exception as exc:  # noqa: BLE001 - a stale entry is replaced on the next admission
            logger.warning("[sandbox.session] session sandbox=%s not forgotten: %s", sandbox_id, exc)
    return outcome


def sandbox_runner_client(base_url: str, *, timeout: float | None = None):
    """A client for one runner, carrying the sandbox API token.

    The single place outside the scheduler that constructs ``SandboxClient``:
    every other module operates containers through this, ``sandbox_client_for``,
    or a ``SandboxSession`` it was admitted. Creating a container is not one of
    the operations it may be used for; that goes through ``admit_sandbox``.
    """

    return _new_client(base_url, timeout=timeout)


async def sandbox_client_for(sandbox_id: str, *, timeout: float | None = None):
    """A client for the runner that hosts ``sandbox_id``. The caller closes it.

    Raises ``SandboxRouteNotFoundError`` when external-runner bookkeeping no
    longer routes the container (it was released or never recorded).
    """

    return _new_client(await sandbox_runner_base_url(sandbox_id), timeout=timeout)


@asynccontextmanager
async def track_runtime_execution(runtime_run_id: str | None, *, sandbox_id: str, execution_id: str):
    """Record the command a durable run is executing, so Stop can kill it.

    ``cancel_runtime_run_resources`` cancels the run's recorded execution
    first, on every coordination mode, before it destroys anything. Recording
    is best-effort: a failed write only means a cancel falls back to
    destroying the container. A no-op without a run.
    """

    run_id = str(runtime_run_id or "").strip()
    recorded = False
    if run_id:
        try:
            from packages.core.database import async_session
            from packages.core.services.runtime_run_service import set_runtime_run_active_execution

            async with async_session() as session:
                await set_runtime_run_active_execution(
                    session, run_id=run_id, sandbox_id=sandbox_id, execution_id=execution_id,
                )
                await session.commit()
            recorded = True
        except Exception as exc:  # noqa: BLE001 - the command still runs
            logger.warning("[sandbox.session] run=%s execution=%s not recorded: %s", run_id, execution_id, exc)
    try:
        yield
    finally:
        if recorded:
            try:
                from packages.core.database import async_session
                from packages.core.services.runtime_run_service import clear_runtime_run_active_execution

                async with async_session() as session:
                    await clear_runtime_run_active_execution(
                        session, run_id=run_id, execution_id=execution_id,
                    )
                    await session.commit()
            except Exception as exc:  # noqa: BLE001 - the next command overwrites it
                logger.warning("[sandbox.session] run=%s execution=%s not cleared: %s", run_id, execution_id, exc)


def sandbox_exec_queue_params(*, work_area: str | None, skill: str | None = None) -> dict[str, Any]:
    """The fair-queue parameters of a command a caller runs itself (``client.exec``).

    A container may be shared (sandbox-user-conversation): when every command
    slot is taken, a command carrying these waits its turn in the runner's
    fair FIFO -- round-robin across work areas (``fair_key``: the conversation
    or task it belongs to), up to ``SANDBOX_EXEC_QUEUE_WAIT_SECONDS`` -- instead
    of being answered ``sandbox_busy``. ``skill`` is the loaded Skill the
    command sees as ``/skill``. Every direct ``exec`` caller passes these; the
    ``sandbox`` tool parks a durable run instead of waiting in-process.
    """

    try:
        wait = max(1, int(_settings().SANDBOX_EXEC_QUEUE_WAIT_SECONDS))
    except Exception:  # noqa: BLE001 - the runner's own bound applies
        wait = 600
    params: dict[str, Any] = {"queue_timeout": wait}
    if work_area:
        params["fair_key"] = str(work_area)[:255]
    if skill is not None:
        params["skill"] = skill
    return params


def new_execution_id() -> str:
    """A fresh runner execution id; the runner can stop a command only by it."""

    return f"manor-{uuid.uuid4().hex}"


def admission_is_durable(binding: SandboxBinding) -> bool:
    """Whether ``admit_sandbox`` admits this call through its durable ticket.

    The ticket is keyed by the run's tool call: admitting again within the
    same call re-attaches the container already allocated to it, so a call
    whose container was lost gets a fresh one only as a new tool call.
    """

    return bool(
        _durable_external(_settings())
        and binding.runtime_run_id
        and binding.runtime_tool_call_id
        and not binding.parent_skill
    )


async def admit_sandbox(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    db: Any | None = None,
    sleep: SleepFn = asyncio.sleep,
    clock: ClockFn = time.monotonic,
) -> "SandboxSession | SandboxSessionError | Any":
    """The one admission entry point: attach a durable ticket, else acquire.

    A durable run is resumed by re-executing the tool call that queued its
    ticket, so a call that can park first attaches the container its ticket
    was allocated (or keeps waiting on it), exactly like ``sandbox
    action='create'`` and ``invoke_skill``. Everything else goes straight to
    ``acquire_sandbox_session``. Returns the ``SandboxSession``, a
    ``SandboxSessionError``, or a ``RuntimeToolSuspension`` the caller must
    return from its tool unchanged. Never raises for admission outcomes.
    """

    if admission_is_durable(binding):
        try:
            async with _release_db_session(db) as session_db:
                attached = await attach_durable_reservation(
                    owner=owner, bundle=bundle, binding=binding, db=session_db,
                )
                if attached is not None:
                    return attached
                return await acquire_sandbox_session(
                    owner=owner, bundle=bundle, binding=binding, db=session_db, sleep=sleep, clock=clock,
                )
        except Exception as exc:  # noqa: BLE001 - admission never raises at the caller
            logger.warning(
                "[sandbox.session] durable admission failed skill=%s: %s", bundle.skill_name, exc, exc_info=True,
            )
            return SandboxSessionError.of("create_failed", _CREATE_FAILED_MESSAGE.format(exc=exc))
    return await acquire_sandbox_session(
        owner=owner, bundle=bundle, binding=binding, db=db, sleep=sleep, clock=clock,
    )


# ── Admission ───────────────────────────────────────────────────

SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]

_IMAGE_UNAVAILABLE_MESSAGE = (
    "Sandbox skill execution failed before the skill could run.\n"
    "Sandbox runtime image '{image}' is not available on the "
    "Sandbox Service Docker host.{extra}\n\n"
    "Build/load the image with `docker build -t {image} "
    "-f docker/Dockerfile.sandbox .` or deploy the tagged "
    "sandbox skill image and set SANDBOX_IMAGE to that tag."
)
_CAPACITY_MESSAGE = "Sandbox resources are busy. Please retry later.\nDetails: {exc}"
_CREATE_FAILED_MESSAGE = (
    "Sandbox skill execution failed before the skill could run.\n"
    "Details: {exc}\n\n"
    "This skill is script-backed and must run through the Sandbox "
    "Service. Check the sandbox service health, image availability, "
    "and the built-in/workspace skill creation route."
)
_NEW_CONTEXT_FAILED_MESSAGE = (
    "Sandbox owner context could not be persisted; the newly created Sandbox was destroyed. Details: {exc}"
)
_REUSED_CONTEXT_FAILED_MESSAGE = (
    "Sandbox owner context could not be persisted; no Sandbox tool access was granted. Details: {exc}"
)
_BUSY_TIMEOUT_MESSAGE = (
    "The owner's sandbox stayed busy for {seconds}s. Complete or wait for the running command, then retry."
)
_QUEUE_FULL_MESSAGE = "Sandbox reservation queue is full ({exc}). Please retry later."
_NESTED_ALLOCATION_MESSAGE = (
    "No warm sandbox is available for skill '{skill}', and a new one cannot be "
    "awaited from inside skill '{parent}'. Finish '{parent}' without the sandbox "
    "and tell the parent agent to invoke skill '{skill}' directly."
)
_TASK_TICKET_TERMINAL_MESSAGE = (
    "Sandbox allocation did not complete for this task (reservation {status}).{detail} Please retry later."
)
_TASK_WAIT_TIMEOUT_MESSAGE = "Sandbox allocation timed out after {seconds}s while waiting for a runner."
# Once the scheduler has claimed a ticket (ALLOCATING) the runner is building
# the container right now, typically for a few seconds. Giving up at that
# instant abandons a container that is seconds from usable and makes the model
# queue a second ticket, so the wait is extended once by this much.
_TASK_ALLOCATING_GRACE_SECONDS = 60
_TASK_WAIT_TIMEOUT_CANCELLED_SUFFIX = " The reservation was cancelled. Please retry later."
_TASK_WAIT_TIMEOUT_ALLOCATING_SUFFIX = (
    " The reservation was already claimed by a runner, so the runner will finish creating the "
    "container and the sandbox service's idle reaper will reclaim it. Please retry later."
)
# Bundle keys that are not runnable scripts; everything else in a bundle is.
_NON_SCRIPT_BUNDLE_FILES = frozenset({"SKILL.md", "requirements.txt", "credentials.json"})


def _settings():
    from packages.core.config import get_settings

    return get_settings()


def _durable_external(settings) -> bool:
    return (
        settings.MANOR_RUNTIME_EXECUTION_MODE == "durable" and settings.SANDBOX_COORDINATION_MODE == "external-runner"
    )


def _new_client(base_url: str, *, timeout: float | None = None):
    from packages.core.services.sandbox_sdk import SandboxClient

    client = SandboxClient(
        base_url=base_url,
        timeout=_CLIENT_TIMEOUT_SECONDS if timeout is None else timeout,
        api_token=_settings().SANDBOX_API_TOKEN.strip(),
    )
    # Every request a new-runner feature shapes is gated on what this runner
    # reports (read once per runner, on the first such request).
    client.capability_loader = functools.partial(runner_capabilities, base_url)
    return client


def sandbox_binding_scope(
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
) -> RuntimeSandboxScope | None:
    """The owner x conversation scope an admission binds its sandbox to.

    Every container a conversation uses (its primary sandbox, a Skill's own
    container, a dedicated-config container) is bound to the same scope, so
    every agent of the conversation may operate it. ``None`` only when the
    call has no binding key; nobody could operate a sandbox admitted for it,
    and for a SESSION or ONE_SHOT bundle, which never binds a sandbox-tool slot.
    """

    if bundle.binding_kind is None:
        return None
    return runtime_sandbox_scope(
        entity_id=owner.entity_id,
        user_id=owner.user_id,
        binding_key=binding.conversation_id,
        workspace_id=owner.workspace_id,
    )


async def _bind_sandbox(
    *,
    owner: SandboxOwner,
    session: SandboxSession,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    holds_credentials: bool | None = None,
) -> Exception | None:
    scope = sandbox_binding_scope(owner, bundle, binding)
    if scope is None:
        return None
    skill = bundle.binding_kind is RuntimeSandboxKind.SKILL
    try:
        await runtime_init_sandbox_binding(
            scope,
            session.sandbox_id,
            binding.skill_id if skill else "",
            agent_id=binding.agent_id,
            holds_credentials=session.holds_credentials if holds_credentials is None else holds_credentials,
            credentials_owner_user_id=binding.credentials_owner_user_id,
            skill_key=bundle.skill_name if skill else None,
            coexist=session.skill_slots,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
        return exc
    return None


def sandbox_registry_scope(
    *,
    user_id: str | None,
    binding_key: str | None,
    role: str | None = None,
) -> str | None:
    """The owner-registry scope of one container role. The one place scopes are built.

    A human user's work (any conversation or task) shares ONE scope per
    role: ``user`` for the general sandbox, ``user:<role>`` for a dedicated
    container (``skill:<name>`` for a bundle with its own container config;
    video session/render containers pass their own role). User-less work and
    group room runs are isolated by their binding key:
    ``conversation:<key>[:<role>]``. Segments are percent-encoded so no id can
    forge another scope. None when the call has no binding key.
    """

    key = str(binding_key or "").strip()
    if not key:
        return None
    scope = runtime_sandbox_scope(entity_id="-", user_id=user_id, binding_key=key)
    base = "user" if scope is not None and scope.shared else f"conversation:{quote(key, safe='')}"
    return f"{base}:{role}" if role else base


def _skill_role(skill_name: str) -> str:
    return f"skill:{quote(skill_name, safe='')}"


def sandbox_reuse_scope(
    bundle: SandboxBundle,
    binding: SandboxBinding,
    owner: SandboxOwner | None = None,
) -> tuple[bool, str | None]:
    """Return (reusable, registry scope) of the container this bundle runs in.

    One GENERAL container per owner -- per user for human work, shared by all
    of the user's conversations, tasks and agents -- holds the generic
    sandbox and the user's Skills side by side. A bundle that needs its own
    container configuration (memory, tmpfs, pids: Remotion, for one) gets a
    DEDICATED container in the same owner scope instead::

        no binding key, ONE_SHOT -> (False, None)
        SESSION                  -> user:{reuse_scope}   (a tool-owned container of the user's)
        bundle.config set        -> user:skill:{skill}   (conversation:{key}:skill:{skill} when isolated)
        anything else            -> user                 (conversation:{key} when isolated)

    On an older runner (one ``/skill`` that a load replaces) a Skill whose
    request finds the general container holding ANOTHER Skill in use falls
    back to a container of its own under the dedicated key (see
    ``acquire_sandbox_session``). Files are never migrated between
    containers. ``owner`` decides isolation; without it the scope is the
    binding key's own (never shared).
    """

    if bundle.lifetime is SandboxLifetime.ONE_SHOT:
        # Nothing registers or reuses a ONE_SHOT container.
        return False, None
    if bundle.lifetime is SandboxLifetime.PERSISTENT:
        # A person's long-lived container: no conversation needed or used.
        if owner is None or not owner.user_id:
            return False, None
        return True, f"user:{quote(str(bundle.reuse_scope), safe='')}"
    user_id = owner.user_id if owner is not None else ""
    if bundle.lifetime is SandboxLifetime.SESSION:
        role = quote(str(bundle.reuse_scope), safe="")
    else:
        role = _skill_role(bundle.skill_name) if bundle.config else None
    scope = sandbox_registry_scope(user_id=user_id, binding_key=binding.conversation_id, role=role)
    if scope is None:
        return False, None
    return True, scope


def _skill_container_scope(
    bundle: SandboxBundle,
    binding: SandboxBinding,
    owner: SandboxOwner | None = None,
) -> str | None:
    """The registry scope of a Skill's own container (older runners' conflict fallback)."""

    if bundle.binding_kind is not RuntimeSandboxKind.SKILL:
        return None
    return sandbox_registry_scope(
        user_id=owner.user_id if owner is not None else "",
        binding_key=binding.conversation_id,
        role=_skill_role(bundle.skill_name),
    )


def _misfit_role(bundle: SandboxBundle, role: str | None) -> str:
    """The dedicated container role a bundle gets when ``role``'s container cannot serve it.

    From the general container (``role`` None) a Skill goes to its own
    container (``skill:<name>``, the role older runners' conflicts use too)
    and a generic bundle to one sized for its request. From a dedicated
    container, a container for exactly this request: ``<role>:fit:<digest>``.
    """

    import json

    digest = hashlib.sha256(
        json.dumps(requested_container_config(bundle.config), sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
    if role is None:
        if bundle.binding_kind is RuntimeSandboxKind.SKILL:
            return _skill_role(bundle.skill_name)
        return f"fit:{digest}"
    return f"{role}:fit:{digest}"


def _is_busy_error(exc: BaseException) -> bool:
    """A 409 that waiting can fix: the sandbox is running something right now.

    Other 409s (quarantined or being destroyed, execution conflicts) never
    clear by waiting, so the caller falls through to a fresh sandbox.
    """
    if getattr(exc, "status_code", None) != _BUSY_STATUS_CODE:
        return False
    code = getattr(exc, "code", None)
    return code in (None, "", _BUSY_ERROR_CODE)


async def _create_session(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    settings,
    register: bool = True,
    scope: str | None = None,
) -> SandboxSession | SandboxSessionError:
    from packages.core.services.sandbox_sdk.exceptions import (
        SandboxCapacityError,
        SandboxError,
    )

    base_url = settings.SANDBOX_SERVICE_URL.strip()
    if not base_url:
        return SandboxSessionError.of(
            "not_configured",
            "Sandbox service not configured (SANDBOX_SERVICE_URL unset).",
        )

    client = _new_client(base_url)
    try:
        health: dict = {}
        try:
            health = await client.health()
        except Exception as exc:  # noqa: BLE001 - health is best-effort
            logger.debug("[sandbox.session] health check failed before create: %s", exc)

        if health.get("sandbox_image_available") is False:
            image = str(health.get("sandbox_image") or "sandbox-skill:latest")
            detail = str(health.get("sandbox_image_error") or "").strip()
            return SandboxSessionError.of(
                "image_unavailable",
                _IMAGE_UNAVAILABLE_MESSAGE.format(
                    image=image,
                    extra=f"\nDetails: {detail}" if detail else "",
                ),
            )

        create_kwargs = {
            "skill_name": bundle.skill_name,
            "env": dict(bundle.env),
            "allowed_sensitive_keys": list(bundle.allowed_sensitive_keys),
            "auto_install": bundle.auto_install,
            "config": bundle.config,
        }
        if bundle.isolation_ref:
            from packages.core.services.sandbox_isolation import (
                SandboxIsolationUnavailable,
                resolve_isolation_ref,
            )

            try:
                create_kwargs.update(await resolve_isolation_ref(bundle.isolation_ref))
            except SandboxIsolationUnavailable as exc:
                logger.warning("[sandbox.session] isolation refused skill=%s: %s", bundle.skill_name, exc)
                return SandboxSessionError.of(
                    "create_failed", _CREATE_FAILED_MESSAGE.format(exc=exc), cause=exc.code,
                )
        try:
            if bundle.source == "workspace":
                created = await client.create_from_files(files=dict(bundle.files), **create_kwargs)
            else:
                created = await create_builtin_sandbox(
                    client,
                    files=dict(bundle.files),
                    baked=bundle.baked_skill,
                    health=health,
                    **create_kwargs,
                )
        except SandboxCapacityError as exc:
            logger.warning("[sandbox.session] capacity full skill=%s: %s", bundle.skill_name, exc)
            return SandboxSessionError.of("capacity_full", _CAPACITY_MESSAGE.format(exc=exc))
        except SandboxError as exc:
            logger.warning("[sandbox.session] create failed skill=%s: %s", bundle.skill_name, exc)
            return SandboxSessionError.of(
                "create_failed", _CREATE_FAILED_MESSAGE.format(exc=exc), cause=str(exc.code or ""),
            )
        except Exception as exc:  # noqa: BLE001 - transport errors are admission outcomes
            logger.warning(
                "[sandbox.session] create transport failure skill=%s: %s",
                bundle.skill_name,
                exc,
            )
            return SandboxSessionError.of("create_failed", _CREATE_FAILED_MESSAGE.format(exc=exc))

        now = time.time()
        session = SandboxSession(
            sandbox_id=created.sandbox_id,
            base_url=base_url,
            created_at=now,
            last_acquired_at=now,
            loaded_skills=[bundle.skill_name],
            env_injection=str(getattr(created, "env_injection", None) or "create"),
            current_skill=bundle.skill_name,
            holds_credentials=bool(bundle.env),
            env_fingerprint=env_fingerprint(bundle.env),
            requested_config=requested_container_config(bundle.config),
            container_config=dict(getattr(created, "config", None) or {}) or None,
            skill_slots=bool(getattr(created, "skill_dir", None)),
            skill_envs={bundle.skill_name: env_fingerprint(bundle.env)},
            isolation_key=bundle.isolation_key,
            reused=False,
            workdir=created.workdir or "/skill",
            scripts=list(created.skill.scripts or []),
            entry_hint=created.skill.entry_hint,
            requirements_installed=bool(created.skill.requirements_txt),
            env_blocked=list(created.env_blocked or []),
        )
        bind_error = await _bind_sandbox(owner=owner, session=session, bundle=bundle, binding=binding)
        if bind_error is not None:
            try:
                await client.destroy(sandbox_id=session.sandbox_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[sandbox.session] failed to destroy unadmitted sandbox=%s",
                    session.sandbox_id,
                    exc_info=True,
                )
            return SandboxSessionError.of(
                "context_persist_failed",
                _NEW_CONTEXT_FAILED_MESSAGE.format(exc=bind_error),
            )
        if register:
            if owner.registrable and not await register_sandbox_session(owner, session, scope=scope):
                logger.warning(
                    "[sandbox.session] registry write failed; sandbox=%s will not be reused",
                    session.sandbox_id,
                )
        if owner.registrable and not register and bundle.lifetime is not SandboxLifetime.ONE_SHOT:
            logger.info(
                "[sandbox.session] no conversation scope; sandbox=%s not registered for reuse",
                session.sandbox_id,
            )
        logger.info(
            "[sandbox.session] created: skill=%s sandbox=%s owner=%s",
            bundle.skill_name,
            session.sandbox_id,
            owner.registry_key(scope) if owner.registrable and scope else "(unscoped)",
        )
        return session
    finally:
        await client.close()


async def _probe_sandbox(client, sandbox_id: str) -> tuple[str, Any]:
    """Return ("alive", info), ("gone", None), or ("unknown", None)."""

    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

    try:
        return "alive", await client.status(sandbox_id)
    except SandboxNotFoundError as exc:
        logger.info("[sandbox.session] registry entry gone: sandbox=%s error=%s", sandbox_id, exc)
        return "gone", None
    except Exception as exc:  # noqa: BLE001 - a blip is not proof of death
        logger.info("[sandbox.session] registry probe inconclusive: sandbox=%s error=%s", sandbox_id, exc)
        return "unknown", None


class _Conflict:
    """``_reuse_session`` outcome: the shared primary cannot take this Skill right now."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<sandbox conflict>"


_CONFLICT = _Conflict()


@dataclass(frozen=True)
class _Misfit:
    """``_reuse_session`` outcome: the container is alive but cannot serve this bundle.

    Too small, or another network posture (``_warm_config_shortfall``). It is
    left exactly as it is -- it is the user's shared general container, or
    other work runs or waits in it -- and the bundle gets a dedicated
    container of the user's alongside it (``_misfit_role``).
    """

    sandbox_id: str
    reason: str
# The placeholder skill name of the generic sandbox; nothing in ``/skill``
# needs protecting while only this is loaded.
GENERIC_SANDBOX_SKILL_NAME = "generic-sandbox"


def _reused_session(existing: SandboxSession, info: Any, **changes: Any) -> SandboxSession:
    """A registry record refreshed for one more acquire, without loading anything."""

    values = {
        "sandbox_id": existing.sandbox_id,
        "base_url": existing.base_url,
        "created_at": existing.created_at,
        "last_acquired_at": time.time(),
        "loaded_skills": list(existing.loaded_skills),
        "env_injection": existing.env_injection,
        "current_skill": existing.current_skill,
        "holds_credentials": existing.holds_credentials,
        "env_fingerprint": existing.env_fingerprint,
        # The container is unchanged by a skill load: it keeps what it was
        # created for and what the runner gave it.
        "requested_config": existing.requested_config,
        "container_config": _info_container_config(info) or existing.container_config,
        "skill_slots": existing.skill_slots,
        "skill_envs": dict(existing.skill_envs),
        "reused": True,
        "workdir": str(getattr(info, "workdir", None) or "/skill"),
    }
    values.update(changes)
    return SandboxSession(**values)


async def _commit_reuse(
    *,
    owner: SandboxOwner,
    session: SandboxSession,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    scope: str | None,
) -> SandboxSession | SandboxSessionError:
    bind_error = await _bind_sandbox(owner=owner, session=session, bundle=bundle, binding=binding)
    if bind_error is not None:
        return SandboxSessionError.of(
            "context_persist_failed",
            _REUSED_CONTEXT_FAILED_MESSAGE.format(exc=bind_error),
        )
    if scope and not await register_sandbox_session(owner, session, scope=scope):
        logger.warning("[sandbox.session] registry refresh failed; sandbox=%s", session.sandbox_id)
    logger.info(
        "[sandbox.session] reused: skill=%s sandbox=%s owner=%s agent=%s",
        bundle.skill_name,
        session.sandbox_id,
        owner.registry_key(scope) if scope else "(unscoped)",
        binding.agent_id or "main",
    )
    return session


async def _reuse_session(
    *,
    client,
    owner: SandboxOwner,
    existing: SandboxSession,
    info: Any,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    settings,
    sleep: SleepFn,
    clock: ClockFn,
    deadline: float,
    scope: str | None,
    shared: bool = False,
) -> "SandboxSession | SandboxSessionError | _Conflict | None":
    """Attach ``bundle`` to a live warm container.

    ``None`` means: unusable, replace it. ``_Misfit``: the container is too
    small (or has another network) for this bundle and must not be replaced;
    the bundle gets a dedicated container alongside. ``_CONFLICT`` (only for the
    ``shared`` primary of a conversation) means: the primary cannot take this
    Skill now without disturbing another agent, so the Skill gets a container
    of its own; the primary is left untouched for the agents using it.

    * generic bundle on the shared primary: nothing is loaded, the caller is
      just bound (the generic sandbox needs nothing in ``/skill``, and
      reloading it would wipe the Skill another agent drives);
    * the Skill already loaded there (same env): reloaded as an overlay when
      the container is free, else bound as it is -- parallel delegates on one
      Skill share one container;
    * another Skill: loaded only while the container is idle (no command
      running, nothing used within ``RUNTIME_SANDBOX_IDLE_THRESHOLD``), the
      runner's exclusive ``load_skill``; busy is a conflict, never a wait.

    ``deadline`` is the admission deadline shared with the owner-lock wait.
    """

    from packages.core.services.sandbox_sdk.exceptions import SandboxError

    poll = max(1, int(settings.SANDBOX_QUEUE_POLL_SECONDS))
    started = clock()
    generic = bundle.binding_kind is RuntimeSandboxKind.GENERIC

    shortfall = await _warm_config_shortfall(client=client, existing=existing, info=info, bundle=bundle)
    if shortfall is not None:
        # The container cannot serve this bundle (too small, or another
        # network posture). Replacing it destroys it, and a destroy kills
        # whatever runs or waits there: the user's shared general container
        # is never replaced, nor is a dedicated one while any command runs
        # or waits in it. The bundle gets a dedicated container alongside
        # instead. Only an idle dedicated container is replaced in place.
        if shared or _sandbox_in_use(info):
            logger.info(
                "[sandbox.session] sandbox=%s cannot serve skill=%s (%s); %s",
                existing.sandbox_id,
                bundle.skill_name,
                shortfall,
                "it is the shared general sandbox" if shared else "work runs or waits in it",
            )
            return _Misfit(sandbox_id=existing.sandbox_id, reason=shortfall)
        logger.info(
            "[sandbox.session] replacing idle dedicated sandbox=%s for skill=%s: %s",
            existing.sandbox_id,
            bundle.skill_name,
            shortfall,
        )
        return None

    if generic and not existing.skill_slots and existing.holds_credentials:
        # A runner without Skill slots cannot keep a Skill's env from other
        # commands (baked at create, or handed to every exec): a generic
        # sandbox never attaches to a container a credentialed Skill used; it
        # gets its own alongside (that container keeps serving the Skill).
        logger.info(
            "[sandbox.session] not attaching generic work to sandbox=%s: its runner cannot isolate "
            "the credentials a Skill put there",
            existing.sandbox_id,
        )
        return _Misfit(sandbox_id=existing.sandbox_id, reason="holds a Skill's credentials")

    if generic and (shared or existing.skill_slots):
        return await _commit_reuse(
            owner=owner,
            session=_reused_session(existing, info),
            bundle=bundle,
            binding=binding,
            scope=scope,
        )

    if existing.skill_slots:
        return await _reuse_slots(
            client=client, owner=owner, existing=existing, info=info, bundle=bundle,
            binding=binding, settings=settings, sleep=sleep, clock=clock,
            deadline=deadline, scope=scope,
        )

    same_env = env_fingerprint(bundle.env) == existing.env_fingerprint
    # Records written before ``current_skill`` existed name it last in
    # ``loaded_skills``.
    holding = existing.current_skill or (existing.loaded_skills[-1] if existing.loaded_skills else None)
    same_skill = holding == bundle.skill_name
    placeholder = holding in (None, "", GENERIC_SANDBOX_SKILL_NAME)

    if existing.env_injection != "exec":
        # Env baked at create cannot be replaced, so this container is only
        # safe for the skill it already holds: a credentialed bundle cannot
        # get its own env in, and a plain bundle must not inherit credentials
        # another skill left behind.
        if ((bundle.env or existing.holds_credentials) and not same_skill) or (same_skill and not same_env):
            logger.info(
                "[sandbox.session] not reusing sandbox=%s for skill=%s: env baked at create (holds=%s)",
                existing.sandbox_id,
                bundle.skill_name,
                existing.current_skill,
            )
            return _CONFLICT if shared else None

    idle_threshold = 0 if (same_skill or placeholder) else RUNTIME_SANDBOX_IDLE_THRESHOLD
    # A packaged built-in Skill loads from the image's copy when the runner
    # holds this exact version; anything else uploads its files as before.
    baked = bundle.baked_skill if bundle.source != "workspace" else None
    while True:
        try:
            loaded = await load_builtin_skill(
                client,
                baked=baked,
                sandbox_id=existing.sandbox_id,
                skill_name=bundle.skill_name,
                files=dict(bundle.files),
                auto_install=bundle.auto_install,
                idle_threshold=idle_threshold,
                source=bundle.source,
                # Always sent, even when empty: this replaces the previous
                # skill's env so a plain skill after a credentialed one runs
                # without those secrets.
                env=dict(bundle.env),
                allowed_sensitive_keys=list(bundle.allowed_sensitive_keys),
            )
            break
        except SandboxError as exc:
            if _is_busy_error(exc):
                if same_skill and same_env:
                    # The Skill is already there and someone is using it:
                    # share it as it is instead of waiting for a reload.
                    logger.info(
                        "[sandbox.session] sandbox=%s busy; binding skill=%s as loaded",
                        existing.sandbox_id,
                        bundle.skill_name,
                    )
                    return await _commit_reuse(
                        owner=owner,
                        session=_reused_session(existing, info, current_skill=bundle.skill_name),
                        bundle=bundle,
                        binding=binding,
                        scope=scope,
                    )
                if shared:
                    logger.info(
                        "[sandbox.session] shared sandbox=%s busy with skill=%s; skill=%s gets its own container",
                        existing.sandbox_id,
                        existing.current_skill,
                        bundle.skill_name,
                    )
                    return _CONFLICT
                remaining = deadline - clock()
                if remaining <= 0:
                    return SandboxSessionError.of(
                        "busy_timeout",
                        _BUSY_TIMEOUT_MESSAGE.format(seconds=int(clock() - started)),
                    )
                await sleep(min(poll, remaining))
                continue
            # Not busy: the load failed (the runner quarantines a container a
            # failed load touched) or the sandbox is unavailable. Replace it.
            logger.info(
                "[sandbox.session] reuse failed (%s); creating new: skill=%s",
                exc,
                bundle.skill_name,
            )
            return None
        except Exception as exc:  # noqa: BLE001 - transport failure: sandbox status unknown
            logger.warning(
                "[sandbox.session] reuse transport failure loading skill=%s into sandbox=%s: %s",
                bundle.skill_name,
                existing.sandbox_id,
                exc,
            )
            return SandboxSessionError.of(
                "create_failed",
                f"Sandbox service unreachable while loading skill '{bundle.skill_name}' "
                f"into sandbox {existing.sandbox_id}: {exc}",
            )

    session = _reused_session(
        existing,
        info,
        loaded_skills=[
            *existing.loaded_skills,
            *([bundle.skill_name] if bundle.skill_name not in existing.loaded_skills else []),
        ],
        current_skill=bundle.skill_name,
        holds_credentials=(
            bool(bundle.env)
            if existing.env_injection == "exec"
            else (existing.holds_credentials or bool(bundle.env))
        ),
        env_fingerprint=(
            env_fingerprint(bundle.env) if existing.env_injection == "exec" else existing.env_fingerprint
        ),
        env_blocked=list(getattr(loaded, "env_blocked", None) or []),
        scripts=list(loaded.skill.scripts or []),
        entry_hint=loaded.skill.entry_hint,
        requirements_installed=bool(loaded.skill.requirements_txt),
    )
    return await _commit_reuse(owner=owner, session=session, bundle=bundle, binding=binding, scope=scope)


#: Container settings that decide whether a warm container can serve a bundle.
_REUSE_RESOURCE_FIELDS = ("memory", "cpus", "pids_limit", "workdir_tmpfs_size")


def requested_container_config(config: Any) -> dict[str, Any]:
    """The container settings a bundle explicitly asks for (``{}`` = runner defaults)."""

    if config is None:
        return {}
    if isinstance(config, dict):
        source = config
    elif hasattr(config, "to_dict"):
        source = config.to_dict()
    else:
        source = {name: getattr(config, name, None) for name in (*_REUSE_RESOURCE_FIELDS, "network")}
    return {
        name: source[name]
        for name in (*_REUSE_RESOURCE_FIELDS, "network")
        if source.get(name) is not None
    }


def _info_container_config(info: Any) -> dict[str, Any] | None:
    config = getattr(info, "config", None)
    return dict(config) if isinstance(config, dict) and config else None


_IN_USE_SANDBOX_STATUSES = frozenset({"creating", "running", "installing", "executing", "destroying"})


def _sandbox_in_use(info: Any) -> bool:
    """Does a command (or a skill load) run -- or wait for a slot -- in the container?

    A shared sandbox runs several commands at once and queues more (a durable
    run parks on its queued command): any of them is work a destroy would kill.
    """

    if getattr(info, "active_execution_id", None):
        return True
    if list(getattr(info, "active_execution_ids", None) or []):
        return True
    if list(getattr(info, "queued_execution_ids", None) or []):
        return True
    return str(getattr(info, "status", "") or "").lower() in _IN_USE_SANDBOX_STATUSES


def _setting_satisfies(name: str, have: Any, want: Any) -> bool:
    from packages.core.services.sandbox_queue_service import parse_memory_mb

    try:
        if name in {"memory", "workdir_tmpfs_size"}:
            have_mb, want_mb = parse_memory_mb(have), parse_memory_mb(want)
            return have_mb is not None and want_mb is not None and have_mb >= want_mb
        return float(have) >= float(want)
    except (TypeError, ValueError):
        return False


async def _warm_config_shortfall(*, client, existing: SandboxSession, info: Any, bundle: SandboxBundle) -> str | None:
    """Why the warm container cannot serve ``bundle`` (None when it can).

    It can when every resource it was created with (or, failing that, that
    the runner reports it has) is at least what the bundle asks, and its
    network is the one the bundle asks for. A setting the bundle leaves to
    the runner default is satisfied by a container that also got the
    default; a container created with an explicit value is compared against
    the runner's default (from ``/health``). A request the runner clamped
    (memory above what the host can give) is satisfied by a container
    created for at least the same request, so a clamped container is reused
    rather than replaced by another identical clamped one.
    """

    if (existing.isolation_key or "") != (bundle.isolation_key or ""):
        # Made for another isolation (in the cloud: another exit IP).
        return "isolation differs from the one requested"
    wanted = requested_container_config(bundle.config)
    created_with = existing.requested_config
    effective = _info_container_config(info) or existing.container_config or {}
    defaults: dict[str, Any] | None = None

    async def runner_default(name: str) -> Any:
        nonlocal defaults
        if defaults is None:
            try:
                health = await client.health()
            except Exception as exc:  # noqa: BLE001 - unknown default = refuse reuse
                logger.info("[sandbox.session] runner defaults unavailable: %s", exc)
                health = {}
            raw = (health or {}).get("container_defaults") if isinstance(health, dict) else None
            defaults = dict(raw) if isinstance(raw, dict) else {}
        return defaults.get(name)

    for name in (*_REUSE_RESOURCE_FIELDS, "network"):
        want = wanted.get(name)
        if want is None:
            if created_with is None or created_with.get(name) is None:
                # Default asked of a container that got the default (or a
                # record from before configs were tracked: reused as before).
                continue
            want = await runner_default(name)
            if want is None:
                return f"{name}: the runner's default is unknown"
        origin = created_with.get(name) if created_with else None
        if name == "network":
            have = effective.get("network") or origin
            if have != want:
                return f"network {have or 'unknown'} differs from requested {want}"
            continue
        if origin is not None and _setting_satisfies(name, origin, want):
            continue
        have = effective.get(name)
        if have is None or not _setting_satisfies(name, have, want):
            return f"{name} {have if have is not None else 'unknown'} is below the requested {want}"
    return None
async def _reuse_slots(
    *,
    client,
    owner: SandboxOwner,
    existing: SandboxSession,
    info: Any,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    settings,
    sleep: SleepFn,
    clock: ClockFn,
    deadline: float,
    scope: str | None,
) -> "SandboxSession | SandboxSessionError":
    """Load a Skill next to the others in a runner that keeps them side by side.

    Loading claims only this Skill's directory, so it never disturbs another
    agent's or conversation's Skill. It is busy only while commands of the
    same Skill run: then the Skill is already there and, with the same env,
    shared as loaded; with another env (rotated credentials) the load waits
    for them within the admission deadline. A failed load leaves the shared
    container serving everyone else: it is reported, never "replaced".
    """

    from packages.core.services.sandbox_sdk.exceptions import SandboxError

    poll = max(1, int(settings.SANDBOX_QUEUE_POLL_SECONDS))
    started = clock()
    fingerprint = env_fingerprint(bundle.env)
    loaded_already = bundle.skill_name in existing.loaded_skills
    same_env = existing.skill_envs.get(bundle.skill_name, existing.env_fingerprint) == fingerprint
    # A packaged built-in Skill is materialized from the image's copy into
    # its own /skills/<slot> when the runner holds this exact version.
    baked = bundle.baked_skill if bundle.source != "workspace" else None
    while True:
        try:
            loaded = await load_builtin_skill(
                client,
                baked=baked,
                sandbox_id=existing.sandbox_id,
                skill_name=bundle.skill_name,
                files=dict(bundle.files),
                auto_install=bundle.auto_install,
                idle_threshold=0,
                source=bundle.source,
                env=dict(bundle.env),
                allowed_sensitive_keys=list(bundle.allowed_sensitive_keys),
            )
            break
        except SandboxError as exc:
            if _is_busy_error(exc):
                if loaded_already and same_env:
                    logger.info(
                        "[sandbox.session] skill=%s busy in sandbox=%s; bound as loaded",
                        bundle.skill_name,
                        existing.sandbox_id,
                    )
                    return await _commit_reuse(
                        owner=owner,
                        session=_reused_session(existing, info, current_skill=bundle.skill_name),
                        bundle=bundle,
                        binding=binding,
                        scope=scope,
                    )
                remaining = deadline - clock()
                if remaining <= 0:
                    return SandboxSessionError.of(
                        "busy_timeout",
                        _BUSY_TIMEOUT_MESSAGE.format(seconds=int(clock() - started)),
                    )
                await sleep(min(poll, remaining))
                continue
            logger.warning(
                "[sandbox.session] skill=%s could not be loaded into shared sandbox=%s: %s",
                bundle.skill_name,
                existing.sandbox_id,
                exc,
            )
            return SandboxSessionError.of(
                "create_failed",
                _CREATE_FAILED_MESSAGE.format(exc=f"loading skill '{bundle.skill_name}' failed: {exc}"),
            )
        except Exception as exc:  # noqa: BLE001 - transport failure: sandbox status unknown
            return SandboxSessionError.of(
                "create_failed",
                f"Sandbox service unreachable while loading skill '{bundle.skill_name}' "
                f"into sandbox {existing.sandbox_id}: {exc}",
            )
    skill_envs = dict(existing.skill_envs)
    skill_envs[bundle.skill_name] = fingerprint
    session = _reused_session(
        existing,
        info,
        loaded_skills=[
            *existing.loaded_skills,
            *([bundle.skill_name] if bundle.skill_name not in existing.loaded_skills else []),
        ],
        current_skill=bundle.skill_name,
        holds_credentials=any(skill_envs.values()),
        env_fingerprint=fingerprint,
        skill_envs=skill_envs,
        env_blocked=list(getattr(loaded, "env_blocked", None) or []),
        scripts=list(loaded.skill.scripts or []),
        entry_hint=loaded.skill.entry_hint,
        requirements_installed=bool(loaded.skill.requirements_txt),
    )
    return await _commit_reuse(owner=owner, session=session, bundle=bundle, binding=binding, scope=scope)


@dataclass(frozen=True)
class _ScopeContainer:
    """Admission outcome: Postgres names a container of the scope; probe it, never create."""

    sandbox_id: str
    base_url: str
    created_with: dict[str, Any]


def _scope_container_outcome(container: Any) -> _ScopeContainer:
    return _ScopeContainer(
        sandbox_id=container.sandbox_id,
        base_url=container.base_url,
        created_with=dict(container.created_with),
    )


async def _enqueue_durable_reservation(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    db: Any,
    settings,
    scope: str | None = None,
    exclude_sandbox_ids: frozenset[str] = frozenset(),
) -> "SandboxSessionError | _ScopeContainer | Any":
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.models.runtime_run import RuntimeRun
    from packages.core.services import sandbox_queue_service
    from packages.core.services.sandbox_queue_service import SandboxQueueFullError

    run = await db.get(RuntimeRun, binding.runtime_run_id)
    if run is None or run.entity_id != owner.entity_id or run.user_id != owner.user_id:
        return SandboxSessionError.of(
            "not_configured",
            "Durable Runtime identity could not be verified for Sandbox admission.",
        )
    # Single flight across the owner's runs: while another ticket is already
    # creating this scope's container (another conversation of the same user,
    # or a delegate), this run's ticket follows it and is allocated to that
    # same container instead of a second one.
    leader = None
    if scope:
        # Look up and enqueue under one lock per scope (held to the commit
        # below), with Postgres as the authority on what the scope has: a
        # ticket creating its container (follow it), or a container (probe
        # and use it -- the caller does). Only a scope with neither gets a
        # new ticket, so concurrent admissions never create two containers,
        # including while the leader attaches (Redis registration, CONSUMED)
        # outside every lock.
        await sandbox_queue_service.lock_sandbox_scope(db, owner.registry_key(scope))
        leader, container = await sandbox_queue_service.find_scope_head(
            db, scope_key=owner.registry_key(scope), exclude_sandbox_ids=exclude_sandbox_ids,
        )
        if leader is not None and leader.runtime_run_id == run.id:
            leader = None
            container = await sandbox_queue_service.find_scope_container(
                db, scope_key=owner.registry_key(scope), exclude_sandbox_ids=exclude_sandbox_ids,
            )
        if container is not None:
            outcome = _scope_container_outcome(container)
            await db.commit()  # ends the transaction: releases the scope lock
            return outcome
    try:
        reservation = await sandbox_queue_service.enqueue_sandbox_reservation(
            db,
            run=run,
            tool_call_id=_reservation_tool_call_id(bundle, binding),
            request_payload=_reservation_request_payload(bundle, owner=owner, scope=scope),
            activate=False,
            **({"follows": leader.id} if leader is not None else {}),
        )
    except SandboxQueueFullError as exc:
        # A full queue is an admission outcome, not a crash: the caller gets a
        # retryable capacity error. The failed enqueue wrote nothing, and this
        # is the caller's session: rolling it back would expire every instance
        # the tool still holds (skill, run) and discard its pending work.
        logger.warning(
            "[sandbox.session] reservation queue full skill=%s: %s", bundle.skill_name, exc,
        )
        return SandboxSessionError.of("capacity_full", _QUEUE_FULL_MESSAGE.format(exc=exc))
    await db.commit()
    return RuntimeToolSuspension(
        kind="waiting_resource",
        reservation_id=reservation.id,
        poll_after_seconds=settings.SANDBOX_QUEUE_POLL_SECONDS,
        deadline_at=reservation.deadline_at,
    )


#: ``request_payload`` key naming the registry scope (owner x conversation x
#: container role) a ticket allocates for. Concurrent admissions of one scope
#: coalesce onto the ticket that names it, so parallel delegates of one
#: conversation get ONE container.
SANDBOX_SCOPE_PAYLOAD_KEY = "sandbox_scope"


def _reservation_tool_call_id(bundle: SandboxBundle, binding: SandboxBinding) -> str:
    """The reservation key of this bundle's container within the durable call."""

    return f"{binding.runtime_tool_call_id}{bundle.reservation_suffix}"


def _reservation_request_payload(
    bundle: SandboxBundle,
    *,
    owner: SandboxOwner | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    from packages.core.services.sandbox_queue_service import normalize_runner_pool

    payload = {
        "skill_name": bundle.skill_name,
        "files": dict(bundle.files),
        "env": dict(bundle.env),
        "allowed_sensitive_keys": list(bundle.allowed_sensitive_keys),
        "auto_install": bundle.auto_install,
        "config": bundle.config,
        "source": bundle.source,
        "pool": normalize_runner_pool(bundle.pool),
    }
    if bundle.baked_skill is not None:
        # The claiming worker sends this when its runner holds the digest and
        # otherwise reads the packaged files itself (files may be empty here).
        payload["baked_skill"] = bundle.baked_skill.payload()
    if bundle.isolation_key:
        # Only the reference: the claiming worker resolves it at create time.
        payload["isolation_key"] = bundle.isolation_key
        payload["isolation_ref"] = bundle.isolation_ref
    if owner is not None and scope:
        payload[SANDBOX_SCOPE_PAYLOAD_KEY] = owner.registry_key(scope)
    return payload


def _bundle_from_payload(payload: Any, fallback: SandboxBundle) -> SandboxBundle:
    """The bundle a (possibly another caller's) ticket created its container with."""

    if not isinstance(payload, dict) or not payload.get("skill_name"):
        return fallback
    return SandboxBundle(
        skill_name=str(payload.get("skill_name") or ""),
        files=dict(payload.get("files") or {}),
        env={str(key): str(value) for key, value in dict(payload.get("env") or {}).items()},
        allowed_sensitive_keys=tuple(payload.get("allowed_sensitive_keys") or ()),
        source=str(payload.get("source") or "builtin"),
        config=payload.get("config"),
        auto_install=bool(payload.get("auto_install", True)),
        pool=str(payload.get("pool") or fallback.pool),
        baked_skill=BakedSkillRef.from_payload(payload.get("baked_skill")),
        isolation_key=str(payload.get("isolation_key") or ""),
        isolation_ref=(str(payload["isolation_ref"]) if payload.get("isolation_ref") else None),
    )


# A health probe answers in milliseconds; a hung runner must not hold an
# allocation for the full client timeout.
_HEALTH_PROBE_TIMEOUT_SECONDS = 5.0


async def _runner_env_injection(base_url: str) -> str:
    """Ask a runner how it hands skill env to processes ("exec" or "create").

    The task flow never sees the create response (the scheduler created the
    container), so the runner's health report is the only source. Unknown or
    unreachable means "create": the conservative answer that keeps a
    credentialed skill from reusing the container.
    """

    if not base_url:
        return "create"
    client = None
    try:
        client = _new_client(base_url, timeout=_HEALTH_PROBE_TIMEOUT_SECONDS)
        health = await client.health()
    except Exception as exc:  # noqa: BLE001 - health is best-effort
        # Recorded as "create": the container will be retired at its next skill
        # switch instead of reused. Visible so a flapping runner is diagnosable.
        logger.info(
            "[sandbox.session] runner %s health unavailable; recording env_injection=create: %s",
            base_url,
            exc,
        )
        return "create"
    finally:
        if client is not None:
            await client.close()
    return "exec" if str((health or {}).get("env_injection") or "") == "exec" else "create"


_RUNNER_CAPABILITIES_CACHE: dict[str, tuple[float, "RunnerCapabilities"]] = {}
_RUNNER_CAPABILITIES_TTL_SECONDS = 60.0
# An unreadable report is retried soon: a runner restart is not an old runner.
_RUNNER_CAPABILITIES_UNKNOWN_TTL_SECONDS = 5.0


async def runner_capabilities(base_url: str) -> "RunnerCapabilities":
    """What ``base_url``'s runner can do: one cached /health read per runner.

    Every new-runner behaviour is gated on this (``sandbox_sdk.capabilities``).
    An unreachable runner answers ``UNKNOWN`` (``reported`` False): it is
    never called outdated, and every capability check that guards safety
    (``has``) treats it as lacking the capability.
    """

    from packages.core.services.sandbox_sdk.capabilities import UNKNOWN, capabilities_from_health

    if not base_url:
        return UNKNOWN
    cached = _RUNNER_CAPABILITIES_CACHE.get(base_url)
    now = time.monotonic()
    if cached is not None:
        ttl = _RUNNER_CAPABILITIES_TTL_SECONDS if cached[1].reported else _RUNNER_CAPABILITIES_UNKNOWN_TTL_SECONDS
        if now - cached[0] < ttl:
            return cached[1]
    client = None
    try:
        client = _new_client(base_url, timeout=_HEALTH_PROBE_TIMEOUT_SECONDS)
        capabilities = capabilities_from_health(await client.health())
    except Exception as exc:  # noqa: BLE001 - health is best-effort
        logger.info("[sandbox.session] runner %s health unavailable; capabilities unknown: %s", base_url, exc)
        capabilities = UNKNOWN
    finally:
        if client is not None:
            await client.close()
    _RUNNER_CAPABILITIES_CACHE[base_url] = (now, capabilities)
    return capabilities


async def runner_capabilities_for_sandbox(sandbox_id: str) -> "RunnerCapabilities":
    """Capabilities of the runner hosting ``sandbox_id`` (``UNKNOWN`` when unroutable)."""

    from packages.core.services.sandbox_sdk.capabilities import UNKNOWN

    try:
        base_url = await sandbox_runner_base_url(sandbox_id)
    except Exception as exc:  # noqa: BLE001 - the operation itself reports the route
        logger.debug("[sandbox.session] no route for sandbox=%s capabilities: %s", sandbox_id, exc)
        return UNKNOWN
    return await runner_capabilities(base_url)


async def _runner_skill_slots(base_url: str) -> bool:
    """Does ``base_url``'s runner keep Skills side by side (``/skills/<name>``)?

    Unknown or unreachable means False: the conservative answer that never
    loads a second Skill into a runner whose load would replace /skill.
    """

    from packages.core.services.sandbox_sdk.capabilities import SKILL_SLOTS

    return (await runner_capabilities(base_url)).has(SKILL_SLOTS)


async def runner_env_injection(base_url: str) -> str:
    """Public alias: how ``base_url``'s runner injects skill env ("exec"/"create")."""

    return await _runner_env_injection(base_url)


async def build_allocated_session(
    *,
    sandbox_id: str,
    base_url: str,
    bundle: SandboxBundle,
) -> SandboxSession:
    """Registry record for a container the scheduler created on a caller's behalf.

    The durable chat path never sees the create response (the scheduler
    allocated the container), so the injection mode comes from the runner's
    health report. Recording "create" by default would make the next
    credentialed acquire retire a perfectly reusable container.
    """

    now = time.time()
    return SandboxSession(
        sandbox_id=sandbox_id,
        base_url=base_url,
        created_at=now,
        last_acquired_at=now,
        loaded_skills=[bundle.skill_name],
        env_injection=await _runner_env_injection(base_url),
        current_skill=bundle.skill_name,
        holds_credentials=bool(bundle.env),
        env_fingerprint=env_fingerprint(bundle.env),
        requested_config=requested_container_config(bundle.config),
        skill_slots=await _runner_skill_slots(base_url),
        skill_envs={bundle.skill_name: env_fingerprint(bundle.env)},
        isolation_key=bundle.isolation_key,
    )


async def attach_durable_reservation(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    db: Any,
) -> "SandboxSession | SandboxSessionError | Any | None":
    """Re-entry point of a durable run whose tool call queued a sandbox.

    A durable run that queued a reservation is resumed by re-executing that
    same tool call, so every sandbox admission (Skills and the generic
    sandbox alike) checks its ticket here first. Returns ``None`` when this
    call has no reservation yet (admit through ``acquire_sandbox_session``),
    a ``RuntimeToolSuspension`` while the ticket waits, the attached session
    once the scheduler allocated the container, or an error when the ticket
    ended without one.
    """

    from sqlalchemy import select

    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.models.runtime_run import SandboxReservation, SandboxReservationStatus
    from packages.core.services.sandbox_queue_service import resolve_sandbox_runner

    if not (binding.runtime_run_id and binding.runtime_tool_call_id):
        return None
    reservation = (
        await db.execute(
            select(SandboxReservation).where(
                SandboxReservation.runtime_run_id == binding.runtime_run_id,
                SandboxReservation.tool_call_id == _reservation_tool_call_id(bundle, binding),
            )
        )
    ).scalar_one_or_none()
    if reservation is None:
        return None
    status = reservation.status
    if status in {
        SandboxReservationStatus.PENDING_CHECKPOINT.value,
        SandboxReservationStatus.QUEUED.value,
        SandboxReservationStatus.REQUEUED.value,
        SandboxReservationStatus.ALLOCATING.value,
    }:
        return RuntimeToolSuspension(
            kind="waiting_resource",
            reservation_id=reservation.id,
            poll_after_seconds=_settings().SANDBOX_QUEUE_POLL_SECONDS,
            deadline_at=reservation.deadline_at,
        )
    if status not in {
        SandboxReservationStatus.ALLOCATED.value,
        SandboxReservationStatus.CONSUMED.value,
    }:
        return SandboxSessionError.of(
            "create_failed", f"Sandbox reservation ended with status '{status}'.",
        )
    from packages.core.services.sandbox_queue_service import reservation_sandbox_id

    sandbox_id = reservation_sandbox_id(reservation)
    # A follower was allocated to the container its leader created; it never
    # owned that container, so it never destroys it.
    follower = not reservation.sandbox_id
    if not sandbox_id:
        return SandboxSessionError.of(
            "create_failed", "Sandbox allocation is missing its Sandbox identity.",
        )
    runner = await resolve_sandbox_runner(db, sandbox_id)
    base_url = str(runner.base_url or "")
    was_allocated = status == SandboxReservationStatus.ALLOCATED.value
    # The scheduler created this container; ask the runner how it injects
    # skill env and whether Skills coexist, so the binding and the registry
    # record describe it.
    session = await build_allocated_session(
        sandbox_id=sandbox_id, base_url=base_url, bundle=bundle,
    )
    bind_error = await _bind_sandbox(
        owner=owner,
        session=session,
        bundle=bundle,
        binding=binding,
        holds_credentials=bool(bundle.env),
    )
    if bind_error is not None:
        if was_allocated and not follower:
            # Nobody can reach a container whose owner context was never
            # written: destroy it rather than leave it pinning a runner slot.
            reservation.last_error = "sandbox_context_persistence_failed"
            if await _destroy_container(sandbox_id, db=db, external=True) != "destroyed":
                reservation.status = SandboxReservationStatus.RELEASE_PENDING.value
                reservation.version += 1
                await db.commit()
        return SandboxSessionError.of(
            "context_persist_failed", _NEW_CONTEXT_FAILED_MESSAGE.format(exc=bind_error),
        )
    if was_allocated:
        reservation.status = SandboxReservationStatus.CONSUMED.value
        reservation.consumed_at = datetime.now(timezone.utc)
        reservation.version += 1
    scope = (
        _payload_registry_scope(owner, reservation.request_payload)
        or sandbox_reuse_scope(bundle, binding, owner)[1]
    )
    if scope:
        await register_sandbox_session(owner, session, scope=scope)
    await db.commit()
    return session


def _payload_registry_scope(owner: SandboxOwner, payload: Any) -> str | None:
    """The registry scope a ticket was enqueued for, when it is this owner's."""

    key = str((payload or {}).get(SANDBOX_SCOPE_PAYLOAD_KEY) or "") if isinstance(payload, dict) else ""
    if key.startswith(owner.registry_prefix) and len(key) > len(owner.registry_prefix):
        return key[len(owner.registry_prefix):]
    return None


def _ticket_seconds_left(ticket: Any) -> float:
    """Seconds until the ticket's own deadline (0 when it has none or it passed)."""

    deadline_at = getattr(ticket, "deadline_at", None)
    if not isinstance(deadline_at, datetime):
        return 0.0
    if deadline_at.tzinfo is None:
        deadline_at = deadline_at.replace(tzinfo=timezone.utc)
    return max(0.0, (deadline_at - datetime.now(timezone.utc)).total_seconds())


async def _wait_for_task_allocation(
    *,
    session_db: Any,
    reservation_id: str,
    poll: int,
    deadline: float,
    sleep: SleepFn,
    clock: ClockFn,
    owns_ticket: bool = True,
) -> "Any | SandboxSessionError":
    """Poll one ticket until it is allocated, terminal, or out of budget.

    Returns the allocated ticket (its ``sandbox_id`` is set), or the admission outcome to hand
    back to the caller. Every read ends the previous transaction and expires
    the identity map first: the ticket is written by the scheduler in another
    transaction, so a cached row — or a snapshot held open across polls — would
    report ``queued`` forever. The rollback also keeps this session from
    holding an idle-in-transaction connection for the whole wait; the ticket
    itself was committed before the first poll.

    ``owns_ticket`` is False for a caller that coalesced onto another
    admission's ticket (a parallel delegate of the same conversation): giving
    up never cancels a ticket others are waiting on.
    """

    from packages.core.models.runtime_run import SandboxReservationStatus
    from packages.core.services import sandbox_queue_service

    started = clock()
    terminal = SandboxReservationStatus.terminal()
    allocating_grace_granted = False
    while True:
        await sleep(poll)
        await session_db.rollback()
        session_db.expire_all()
        ticket = await sandbox_queue_service.load_sandbox_reservation(session_db, reservation_id)
        status = str(getattr(ticket, "status", "") or "")
        if ticket is None:
            logger.warning("[sandbox.session] task reservation %s disappeared while waiting", reservation_id)
            await session_db.rollback()
            return SandboxSessionError.of(
                "capacity_full",
                _TASK_TICKET_TERMINAL_MESSAGE.format(status="missing", detail=""),
            )
        if status in {
            SandboxReservationStatus.ALLOCATED.value,
            SandboxReservationStatus.CONSUMED.value,
        }:
            if str(getattr(ticket, "sandbox_id", "") or ""):
                return ticket
        elif status in terminal:
            detail = str(getattr(ticket, "last_error", "") or "").strip()
            await session_db.rollback()
            return SandboxSessionError.of(
                "capacity_full",
                _TASK_TICKET_TERMINAL_MESSAGE.format(
                    status=status,
                    detail=f" Details: {detail}" if detail else "",
                ),
                cause=_ticket_error_cause(detail),
            )
        if clock() >= deadline:
            ticket_left = _ticket_seconds_left(ticket)
            if ticket_left > 0 and status in {
                SandboxReservationStatus.QUEUED.value,
                SandboxReservationStatus.REQUEUED.value,
            }:
                # The ticket's own deadline is the wait budget: a pool whose
                # fleet boots a machine for it gives it longer than the
                # queue's default, and the expiry sweep ends it on time.
                deadline = clock() + ticket_left
                continue
            if (
                status == SandboxReservationStatus.ALLOCATING.value
                and not allocating_grace_granted
            ):
                allocating_grace_granted = True
                deadline = clock() + _TASK_ALLOCATING_GRACE_SECONDS
                logger.info(
                    "[sandbox.session] task reservation %s is allocating at the deadline; waiting up to %ss more",
                    reservation_id,
                    _TASK_ALLOCATING_GRACE_SECONDS,
                )
                continue
            # Only an unclaimed ticket may be cancelled here; once it is
            # ALLOCATING the runner owns the slot and finishes on its own.
            # A ticket other admissions share is never cancelled by a joiner.
            cancelled = False
            if owns_ticket:
                cancelled = await sandbox_queue_service.cancel_queued_sandbox_reservation(
                    session_db,
                    reservation_id=reservation_id,
                    reason="task_wait_timeout",
                )
                await session_db.commit()
            await session_db.rollback()
            logger.warning(
                "[sandbox.session] task reservation %s timed out (status=%s cancelled=%s)",
                reservation_id,
                status or "unknown",
                cancelled,
            )
            return SandboxSessionError.of(
                "capacity_full",
                _TASK_WAIT_TIMEOUT_MESSAGE.format(seconds=int(max(0.0, clock() - started)))
                + (_TASK_WAIT_TIMEOUT_CANCELLED_SUFFIX if cancelled else _TASK_WAIT_TIMEOUT_ALLOCATING_SUFFIX),
            )


async def _root_run_id(db: Any, runtime_run_id: str | None) -> str | None:
    """Root of the durable run a ticket is requested for, when there is one."""

    if not runtime_run_id:
        return None
    from packages.core.models.runtime_run import RuntimeRun

    run = await db.get(RuntimeRun, runtime_run_id)
    root = str(getattr(run, "root_run_id", "") or "") if run is not None else ""
    # Nothing outlives this read: the ticket is enqueued in a fresh transaction.
    await db.rollback()
    return root or None


_NO_FREE_RUNNER_MESSAGE = (
    "No sandbox runner has a free slot right now{detail}, and this call runs inside a "
    "skill's own tool loop, which cannot wait for one. Retry shortly."
)


async def _allocate_now(*, session_db: Any, reservation_id: str) -> "Any | SandboxSessionError":
    """Allocate one ticket in this call, or give up at once.

    For a call that can neither park its run (a prompt skill's nested loop
    cannot be suspended and resumed) nor hold its worker waiting on a ticket
    (the allocation itself runs on the interactive queue). The ticket is
    claimed in queue order inside its enqueue transaction -- tickets already
    waiting are claimed first, for the scheduler to allocate -- and, once
    claimed, created right here through
    the scheduler's own allocation code, so the container gets its instance
    row and runner slot like any other. No free slot: the ticket is cancelled
    and the caller gets a retryable ``capacity_full``. Returns the allocated
    ticket. Only ever called with the caller's own new ticket: a scope whose
    container another admission is creating refuses an inline admission
    before any ticket is written (``_enqueue_poller_ticket``).
    """

    from packages.core.models.runtime_run import SandboxReservationStatus
    from packages.core.services import sandbox_queue_service
    from packages.core.tasks.runtime_tasks import allocate_reserved_sandbox_once

    claimed = await sandbox_queue_service.claim_sandbox_reservation_now(
        session_db, reservation_id=reservation_id,
    )
    if claimed is None:
        # The ticket was never committed: rolling back leaves no row behind.
        await session_db.rollback()
        logger.info("[sandbox.session] no free runner slot for inline reservation %s", reservation_id)
        return SandboxSessionError.of("capacity_full", _NO_FREE_RUNNER_MESSAGE.format(detail=""))
    version = claimed.version
    await session_db.commit()

    await allocate_reserved_sandbox_once(reservation_id=reservation_id, reservation_version=version)

    await session_db.rollback()
    session_db.expire_all()
    ticket = await sandbox_queue_service.load_sandbox_reservation(session_db, reservation_id)
    status = str(getattr(ticket, "status", "") or "")
    sandbox_id = str(getattr(ticket, "sandbox_id", "") or "")
    if status in {
        SandboxReservationStatus.ALLOCATED.value,
        SandboxReservationStatus.CONSUMED.value,
    } and sandbox_id:
        return ticket
    detail = str(getattr(ticket, "last_error", "") or "").strip()
    # A failed create requeued the ticket for the scheduler; nobody here will
    # wait for it, so it must not be allocated later.
    await sandbox_queue_service.cancel_queued_sandbox_reservation(
        session_db, reservation_id=reservation_id, reason="inline_allocation_failed",
    )
    await session_db.commit()
    logger.warning(
        "[sandbox.session] inline allocation of reservation %s failed (status=%s): %s",
        reservation_id,
        status or "missing",
        detail or "-",
    )
    return SandboxSessionError.of(
        "create_failed",
        _CREATE_FAILED_MESSAGE.format(exc=f"inline allocation failed ({detail or status or 'unknown'})"),
    )


@asynccontextmanager
async def _task_ticket_db_session():
    """Open a session owned by the task-ticket flow alone.

    The flow commits the ticket, then rolls back before every poll and commits
    again on consume. Each rollback expires every object loaded on the
    session, so it must never run on the caller's session: the skill service
    still holds the ``Skill`` row it builds the execution contract from, and an
    expired row lazy-loads on attribute access, which SQLAlchemy's async
    adapter rejects with ``MissingGreenlet``. Nothing the caller loaded is
    touched here, and nothing loaded here outlives the flow.
    """

    from packages.core.database import async_session

    async with async_session() as session:
        yield session


@dataclass(frozen=True)
class _TicketWait:
    """An admission parked on a scope ticket; waited on outside the owner lock."""

    reservation_id: str
    owns_ticket: bool
    scope: str | None
    # Set when the ticket was allocated inline (``immediate``): attach it now.
    ticket: Any = None


def _poller_requester_id(binding: SandboxBinding) -> str:
    """Who asks for a ticket it will poll: the task, the Workflow run, else the conversation scope.

    A delegated agent runs inside its parent's tool call with neither a
    durable run nor a task of its own; it waits on a ticket the same way a
    task worker does, identified by the conversation it works in.
    """

    if binding.task_id:
        return str(binding.task_id)
    if binding.workflow_run_id:
        return f"workflow-run:{binding.workflow_run_id}"[:255]
    if binding.background_requester:
        return str(binding.background_requester)[:255]
    return f"conversation:{binding.conversation_id}"[:255]


async def _enqueue_poller_ticket(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    scope: str | None,
    exclude_sandbox_ids: frozenset[str] = frozenset(),
    immediate: bool = False,
) -> "_TicketWait | _ScopeContainer | SandboxSessionError":
    """Join the scope's in-flight ticket, or enqueue one (single flight).

    Runs under the scope's owner lock (when Redis has one) and always under
    the scope's Postgres lock, where Postgres decides: a ticket creating the
    scope's container is joined, an existing container is handed back to be
    probed and used (``_ScopeContainer``), and only a scope with neither gets
    a new ticket -- every concurrent admission of one scope shares ONE
    container. Without a scope (a ONE_SHOT container) the ticket is the
    caller's alone.

    ``immediate`` (a call that can neither park nor wait) allocates the new
    ticket now or not at all (``_allocate_now``); the returned wait then
    already carries its allocated ticket.
    """

    from packages.core.models.runtime_run import SandboxRequesterKind
    from packages.core.services import sandbox_queue_service
    from packages.core.services.sandbox_queue_service import SandboxQueueFullError

    # A call inside a durable run that cannot park (immediate) requests for its
    # run; anything else for its task or conversation scope.
    requester_id = (
        f"run:{binding.runtime_run_id}"[:255]
        if immediate and binding.runtime_run_id
        else _poller_requester_id(binding)
    )
    # One ticket per acquire attempt: the reservation is idempotent on
    # (requester, tool_call_id), so a fresh suffix keeps a retry from adopting
    # a spent ticket. The bundle's purpose keeps the containers one call
    # needs apart.
    call_key = (
        f"{binding.runtime_tool_call_id}{bundle.reservation_suffix}"
        if immediate and binding.runtime_tool_call_id
        else f"{requester_id}{bundle.reservation_suffix}"
    )
    tool_call_id = f"{call_key}:{uuid.uuid4().hex}"[:255]
    async with _task_ticket_db_session() as session_db:
        # The ticket names the run's root when it has one, so the instance
        # row it gets makes a cancel of that run reach the container.
        root_run_id = await _root_run_id(session_db, binding.runtime_run_id)
        scope_key = owner.registry_key(scope) if scope else None
        if scope_key:
            await sandbox_queue_service.lock_sandbox_scope(session_db, scope_key)
            leader, container = await sandbox_queue_service.find_scope_head(
                session_db, scope_key=scope_key, exclude_sandbox_ids=exclude_sandbox_ids,
            )
            if leader is None and container is not None:
                outcome = _scope_container_outcome(container)
                await session_db.rollback()
                return outcome
            if leader is not None and immediate:
                # Another admission is creating the scope's container. A call
                # that cannot wait can neither join that ticket nor claim it
                # (giving up would cancel a ticket others wait on): nothing is
                # written, and it retries shortly.
                leader_id = leader.id
                await session_db.rollback()
                logger.info(
                    "[sandbox.session] scope=%s is still creating its container (ticket %s); "
                    "inline admission refused",
                    scope_key,
                    leader_id,
                )
                return SandboxSessionError.of(
                    "capacity_full",
                    _NO_FREE_RUNNER_MESSAGE.format(detail=" (this sandbox is still being created by another call)"),
                )
        try:
            reservation = await sandbox_queue_service.enqueue_sandbox_reservation(
                session_db,
                run=None,
                tool_call_id=tool_call_id,
                request_payload=_reservation_request_payload(bundle, owner=owner, scope=scope),
                activate=True,
                entity_id=owner.entity_id,
                user_id=owner.user_id,
                requester_kind=SandboxRequesterKind.TASK.value,
                requester_id=requester_id,
                scope_key=scope_key,
                **({"root_run_id": root_run_id} if root_run_id else {}),
            )
        except SandboxQueueFullError as exc:
            # The failed enqueue still opened a transaction (and took the
            # queue advisory lock); this session is the flow's own, so ending
            # it here expires nothing the caller holds.
            await session_db.rollback()
            logger.warning(
                "[sandbox.session] requester=%s reservation queue full skill=%s: %s",
                requester_id,
                bundle.skill_name,
                exc,
            )
            return SandboxSessionError.of("capacity_full", _QUEUE_FULL_MESSAGE.format(exc=exc))
        owns = reservation.tool_call_id == tool_call_id
        reservation_id = reservation.id
        if immediate:
            # Claimed in the enqueue's own transaction: until it commits the
            # scheduler cannot see the ticket, so nobody else claims it.
            allocated = await _allocate_now(session_db=session_db, reservation_id=reservation_id)
            if isinstance(allocated, SandboxSessionError):
                return allocated
            return _TicketWait(reservation_id=reservation_id, owns_ticket=True, scope=scope, ticket=allocated)
        await session_db.commit()
    logger.info(
        "[sandbox.session] %s ticket %s for scope=%s agent=%s",
        "enqueued" if owns else "joined",
        reservation_id,
        scope_key or "(unscoped)",
        binding.agent_id or "main",
    )
    return _TicketWait(reservation_id=reservation_id, owns_ticket=owns, scope=scope)


async def _session_for_container(
    *,
    sandbox_id: str,
    base_url: str,
    created_with: SandboxBundle,
) -> SandboxSession:
    """Registry record of a container the scheduler created with ``created_with``."""

    now = time.time()
    return SandboxSession(
        sandbox_id=sandbox_id,
        base_url=base_url,
        created_at=now,
        last_acquired_at=now,
        loaded_skills=[created_with.skill_name],
        env_injection=await _runner_env_injection(base_url),
        current_skill=created_with.skill_name,
        holds_credentials=bool(created_with.env),
        env_fingerprint=env_fingerprint(created_with.env),
        requested_config=requested_container_config(created_with.config),
        skill_slots=await _runner_skill_slots(base_url),
        skill_envs={created_with.skill_name: env_fingerprint(created_with.env)},
        isolation_key=created_with.isolation_key,
        reused=False,
        scripts=[key for key in created_with.file_paths() if key not in _NON_SCRIPT_BUNDLE_FILES],
        requirements_installed="requirements.txt" in created_with.file_paths(),
        env_blocked=[],
    )


async def _attach_polled_ticket(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    wait: _TicketWait,
    ticket: Any,
) -> "SandboxSession | SandboxSessionError | None":
    """Make an allocated ticket's container the scope's warm session.

    Runs under the scope's owner lock. Returns None when the registry already
    names the container (another joiner attached it first): the caller then
    takes the warm-reuse path, which binds it and loads the caller's Skill.
    """

    from packages.core.services import sandbox_queue_service
    from packages.core.services.sandbox_queue_service import SandboxAllocationStateError

    sandbox_id = str(ticket.sandbox_id)
    current = await load_sandbox_session(owner, scope=wait.scope) if wait.scope else None
    if current is not None and current.sandbox_id == sandbox_id:
        return None
    async with _task_ticket_db_session() as session_db:
        created_with = _bundle_from_payload(getattr(ticket, "request_payload", None), bundle)
        run_owned = bool(getattr(ticket, "runtime_run_id", None))
        runner = await sandbox_queue_service.resolve_sandbox_runner(session_db, sandbox_id)
        session = await _session_for_container(
            sandbox_id=sandbox_id, base_url=str(runner.base_url or ""), created_with=created_with,
        )
        if created_with.skill_name != bundle.skill_name and bundle.binding_kind is RuntimeSandboxKind.SKILL:
            # The container holds another admission's bundle: register it and
            # let the warm-reuse path load this caller's Skill.
            bind_error = None
        else:
            bind_error = await _bind_sandbox(owner=owner, session=session, bundle=bundle, binding=binding)
        if bind_error is not None:
            if wait.owns_ticket and not run_owned:
                # Nothing can reach a container nobody is bound to; destroy it
                # rather than leave an allocated sandbox to the idle reaper.
                try:
                    await _destroy_container(sandbox_id, db=session_db, external=True)
                except Exception as exc:  # noqa: BLE001 - the admission outcome stands
                    logger.warning(
                        "[sandbox.session] unbound task sandbox=%s could not be destroyed: %s",
                        sandbox_id,
                        exc,
                    )
                await session_db.commit()
            return SandboxSessionError.of(
                "context_persist_failed",
                _NEW_CONTEXT_FAILED_MESSAGE.format(exc=bind_error),
            )
        if not run_owned:
            # A run's ticket is consumed by the run when it resumes; a polled
            # ticket is consumed by whoever attaches it first (idempotent).
            try:
                await sandbox_queue_service.consume_sandbox_reservation(
                    session_db,
                    reservation_id=wait.reservation_id,
                )
            except SandboxAllocationStateError as exc:
                # The ticket stopped being ALLOCATED between the poll and here —
                # the abandoned-allocation sweep released it while this worker
                # was binding. The binding above is now a promise to a
                # container this call no longer owns, so take it back before
                # returning. The container is NOT destroyed: the sweep already
                # released the instance and the runner's idle reaper owns it.
                lost_scope = sandbox_binding_scope(owner, bundle, binding)
                if lost_scope is not None:
                    try:
                        await runtime_delete_sandbox_binding(lost_scope, sandbox_id)
                    except Exception as unbind_exc:  # noqa: BLE001 - the outcome stands
                        logger.warning(
                            "[sandbox.session] could not unbind %s after a lost task ticket: %s",
                            lost_scope.prefix,
                            unbind_exc,
                        )
                await session_db.rollback()
                logger.warning(
                    "[sandbox.session] task reservation %s was released before consume: %s",
                    wait.reservation_id,
                    exc,
                )
                return SandboxSessionError.of(
                    "capacity_full",
                    f"Sandbox reservation {wait.reservation_id} was released before it could be "
                    f"consumed ({exc}); retry.",
                )
            await session_db.commit()
    if wait.scope and not await register_sandbox_session(owner, session, scope=wait.scope):
        logger.warning(
            "[sandbox.session] registry write failed; sandbox=%s will not be reused",
            session.sandbox_id,
        )
    logger.info(
        "[sandbox.session] task reservation allocated: requester=%s skill=%s sandbox=%s reservation=%s",
        _poller_requester_id(binding),
        bundle.skill_name,
        session.sandbox_id,
        wait.reservation_id,
    )
    if created_with.skill_name != bundle.skill_name and bundle.binding_kind is RuntimeSandboxKind.SKILL:
        return None
    return session


async def _await_ticket(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    wait: _TicketWait,
    settings,
    sleep: SleepFn,
    clock: ClockFn,
) -> "Any | SandboxSessionError":
    """Wait (outside the owner lock) for a scope ticket's container. Never raises."""

    poll = max(1, int(settings.SANDBOX_QUEUE_POLL_SECONDS))
    # A fresh wait budget: the ticket only starts existing here, so the time
    # already spent on the owner lock is not charged against the scheduler.
    from packages.core.services.sandbox_queue_service import pool_wait_seconds

    max_wait = max(poll, pool_wait_seconds(bundle.pool))
    try:
        async with _task_ticket_db_session() as session_db:
            return await _wait_for_task_allocation(
                session_db=session_db,
                reservation_id=wait.reservation_id,
                poll=poll,
                deadline=clock() + max_wait,
                sleep=sleep,
                clock=clock,
                owns_ticket=wait.owns_ticket,
            )
    except Exception as exc:  # noqa: BLE001 - admission never raises at the caller
        logger.warning(
            "[sandbox.session] reservation wait failed requester=%s skill=%s: %s",
            _poller_requester_id(binding),
            bundle.skill_name,
            exc,
            exc_info=True,
        )
        return SandboxSessionError.of("create_failed", _CREATE_FAILED_MESSAGE.format(exc=exc))


class _OwnerLock:
    """Single-flight lease per (owner, scope), kept alive by a heartbeat.

    ``held`` stays False when Redis is unavailable (``acquire_lease`` returns
    None); admission then proceeds unlocked rather than waiting on a lock
    that cannot exist (the reservation queue still coalesces tickets).
    """

    def __init__(self, owner: SandboxOwner, scope: str | None) -> None:
        self.key = owner.lock_key(scope)
        self.token = uuid.uuid4().hex
        self.held = False
        self._heartbeat: asyncio.Task | None = None

    async def acquire(self, *, poll: int, deadline: float, sleep: SleepFn, clock: ClockFn) -> bool:
        """Return True when this call may proceed; False on wait timeout."""

        from packages.core.cache import cache

        while True:
            acquired = await cache.acquire_lease(self.key, self.token, ttl=_LOCK_TTL_SECONDS)
            if acquired is None:
                return True
            if acquired:
                self.held = True
                self._heartbeat = asyncio.create_task(self._keep_alive())
                return True
            remaining = deadline - clock()
            if remaining <= 0:
                return False
            await sleep(min(poll, remaining))

    async def _keep_alive(self) -> None:
        from packages.core.cache import cache

        while True:
            await asyncio.sleep(_LOCK_HEARTBEAT_SECONDS)
            extended = await cache.extend_lease(self.key, self.token, ttl=_LOCK_TTL_SECONDS)
            if extended is None:
                logger.debug(
                    "[sandbox.session] owner lock heartbeat skipped (cache unavailable): %s",
                    self.key,
                )
                continue
            if not extended:
                logger.warning("[sandbox.session] owner lock lost: %s", self.key)
                return

    async def release(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            try:
                await self._heartbeat
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._heartbeat = None
        if not self.held:
            return
        from packages.core.cache import cache

        await cache.release_lease(self.key, self.token)
        self.held = False


async def _scope_container_in_postgres(
    owner: SandboxOwner,
    *,
    scope: str,
    exclude_sandbox_ids: frozenset[str],
) -> Any:
    """The scope's container according to Postgres (read-only, on a session of its own), or None."""

    from packages.core.services import sandbox_queue_service

    async with _task_ticket_db_session() as session_db:
        container = await sandbox_queue_service.find_scope_container(
            session_db, scope_key=owner.registry_key(scope), exclude_sandbox_ids=exclude_sandbox_ids,
        )
        await session_db.rollback()
    return container


# Rounds of "use the scope's container, else create one" per admission: one
# for the registry's record, one for a container Postgres names, one spare.
_SCOPE_SETTLE_ROUNDS = 3


async def _reuse_or_retire(
    *,
    owner: SandboxOwner,
    existing: SandboxSession,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    settings,
    sleep: SleepFn,
    clock: ClockFn,
    deadline: float,
    scope: str,
    shared: bool,
    db: Any | None,
) -> "SandboxSession | SandboxSessionError | _Conflict | None":
    """Attach to a known container of the scope if it is alive; else retire it and return None.

    Retiring removes it from everything that could name it again: the
    registry, and (external runners) its Postgres instance row -- destroyed
    when it may still run, released when the runner no longer has it.

    A SESSION bundle never reuses: its tool admits only when it wants a fresh
    container (start, or recovery of a dead one), so whatever container the
    scope has -- the registry's, or one only Postgres still names -- is
    replaced (destroyed and forgotten) before a new one is created.
    """

    if bundle.lifetime is SandboxLifetime.SESSION:
        try:
            outcome = await _destroy_released_sandbox(existing.sandbox_id, db=db)
        except Exception as exc:  # noqa: BLE001 - the replacement proceeds regardless
            outcome = f"destroy_failed: {exc}"
        logger.info(
            "[sandbox.session] replaced session sandbox=%s of scope=%s: %s",
            existing.sandbox_id,
            owner.registry_key(scope),
            outcome,
        )
        await forget_sandbox_session(owner, existing.sandbox_id, scope=scope)
        return None

    poll = max(1, int(settings.SANDBOX_QUEUE_POLL_SECONDS))
    client = _new_client(existing.base_url)
    try:
        state, info = await _probe_sandbox(client, existing.sandbox_id)
        if state == "unknown":
            await sleep(poll)
            state, info = await _probe_sandbox(client, existing.sandbox_id)
        if state == "alive":
            reused = await _reuse_session(
                client=client,
                owner=owner,
                existing=existing,
                info=info,
                bundle=bundle,
                binding=binding,
                settings=settings,
                sleep=sleep,
                clock=clock,
                deadline=deadline,
                scope=scope,
                shared=shared,
            )
            if reused is not None:
                return reused
        elif state == "unknown":
            logger.warning(
                "[sandbox.session] two inconclusive probes; treating sandbox=%s as dead",
                existing.sandbox_id,
            )
    finally:
        await client.close()
    # Forgetting alone would leak: nothing else knows this container
    # exists once its registry entry is gone. "gone" is the one state
    # with nothing left to destroy -- only its bookkeeping to release.
    if state != "gone":
        try:
            outcome = await _destroy_released_sandbox(existing.sandbox_id, db=db)
        except Exception as exc:  # noqa: BLE001 - admission outcome stands regardless
            logger.warning(
                "[sandbox.session] abandoned sandbox=%s could not be destroyed: %s",
                existing.sandbox_id,
                exc,
            )
        else:
            logger.info(
                "[sandbox.session] abandoned unusable sandbox=%s (probe=%s): %s",
                existing.sandbox_id,
                state,
                outcome,
            )
    elif _settings().SANDBOX_COORDINATION_MODE == "external-runner":
        try:
            async with _release_db_session(None) as session:
                await _mark_sandbox_released(existing.sandbox_id, db=session)
        except Exception as exc:  # noqa: BLE001 - the runner reconcile sweep releases it too
            logger.warning("[sandbox.session] gone sandbox=%s release failed: %s", existing.sandbox_id, exc)
    await forget_sandbox_session(owner, existing.sandbox_id, scope=scope)
    return None


async def _acquire_in_scope(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    scope: str,
    shared: bool,
    create: bool,
    db: Any | None,
    settings,
    sleep: SleepFn,
    clock: ClockFn,
    deadline: float,
) -> "SandboxSession | SandboxSessionError | _Conflict | _TicketWait | Any | None":
    """One container role of the owner, under its owner lock.

    The Redis registry is a cache of the scope's container; Postgres is the
    authority (external runners): a registry miss never creates a container
    by itself -- the enqueue step, under the scope's Postgres lock, either
    finds the ticket creating the scope's container, or the scope's container
    (an active instance row), which is then probed and used like a registry
    hit. Either way a container that is dead is retired (registry and
    instance row) and never offered again, so loss recovery still moves work
    to a new container.

    Returns the session, an error, ``_CONFLICT`` (shared primary cannot take
    this Skill now), a ``_TicketWait`` (poll outside the lock), a
    ``RuntimeToolSuspension`` (durable run parked on its ticket), or None
    (``create=False`` and nothing warm).
    """

    poll = max(1, int(settings.SANDBOX_QUEUE_POLL_SECONDS))
    lock = _OwnerLock(owner, scope)
    if not await lock.acquire(poll=poll, deadline=deadline, sleep=sleep, clock=clock):
        return SandboxSessionError.of(
            "busy_timeout",
            _BUSY_TIMEOUT_MESSAGE.format(seconds=int(settings.SANDBOX_QUEUE_MAX_WAIT_SECONDS)),
        )
    try:
        # Containers this admission found dead: Postgres may still name one
        # until its release commits, and it must never be offered again.
        dead: set[str] = set()
        existing = await load_sandbox_session(owner, scope=scope)
        for _ in range(_SCOPE_SETTLE_ROUNDS):
            if existing is not None:
                reused = await _reuse_or_retire(
                    owner=owner, existing=existing, bundle=bundle, binding=binding, settings=settings,
                    sleep=sleep, clock=clock, deadline=deadline, scope=scope, shared=shared, db=db,
                )
                if reused is not None:
                    return reused
                dead.add(existing.sandbox_id)
                existing = None

            if not create:
                return None
            if not _durable_external(settings):
                return await _create_session(
                    owner=owner,
                    bundle=bundle,
                    binding=binding,
                    settings=settings,
                    register=True,
                    scope=scope,
                )
            if binding.parent_skill:
                # A prompt skill's nested loop runs inside the durable run's
                # tool call: a reservation queued here would suspend the outer
                # run at that call, and resuming re-runs the whole nested loop
                # from scratch, whose fresh model turn need not ask for the
                # same skill again. Nothing can safely wait on such a ticket,
                # so refuse before one is written; the parent agent can invoke
                # the sandbox skill directly and be parked the normal way. A
                # container Postgres knows (the registry missed it) is used.
                known = await _scope_container_in_postgres(owner, scope=scope, exclude_sandbox_ids=frozenset(dead))
                if known is not None:
                    existing = await _session_for_container(
                        sandbox_id=known.sandbox_id,
                        base_url=known.base_url,
                        created_with=_bundle_from_payload(known.created_with, bundle),
                    )
                    continue
                if bundle.lifetime is SandboxLifetime.WARM:
                    return SandboxSessionError.of(
                        "nested_allocation_unavailable",
                        _NESTED_ALLOCATION_MESSAGE.format(
                            skill=bundle.skill_name, parent=binding.parent_skill,
                        ),
                    )
                # A tool-owned container is instead allocated now, if a runner
                # has room, without waiting (``_allocate_now``).
                outcome = await _enqueue_poller_ticket(
                    owner=owner, bundle=bundle, binding=binding, scope=scope,
                    exclude_sandbox_ids=frozenset(dead), immediate=True,
                )
            elif binding.runtime_run_id and binding.runtime_tool_call_id and db is not None:
                outcome = await _enqueue_durable_reservation(
                    owner=owner, bundle=bundle, binding=binding, db=db, settings=settings, scope=scope,
                    exclude_sandbox_ids=frozenset(dead),
                )
            else:
                # No durable run to park: a task worker, or a delegated agent
                # running inside its parent's tool call. It joins the scope's
                # ticket (or enqueues it) and waits for the allocation itself.
                try:
                    outcome = await _enqueue_poller_ticket(
                        owner=owner, bundle=bundle, binding=binding, scope=scope,
                        exclude_sandbox_ids=frozenset(dead),
                    )
                except Exception as exc:  # noqa: BLE001 - admission never raises at the caller
                    logger.warning(
                        "[sandbox.session] reservation failed requester=%s skill=%s: %s",
                        _poller_requester_id(binding),
                        bundle.skill_name,
                        exc,
                        exc_info=True,
                    )
                    return SandboxSessionError.of("create_failed", _CREATE_FAILED_MESSAGE.format(exc=exc))
            if isinstance(outcome, _TicketWait) and outcome.ticket is not None:
                # Allocated inline: attach it now, under this lock.
                return await _attach_polled_ticket(
                    owner=owner, bundle=bundle, binding=binding, wait=outcome, ticket=outcome.ticket,
                ) or SandboxSessionError.of(
                    "create_failed",
                    _CREATE_FAILED_MESSAGE.format(exc=f"allocated sandbox {outcome.ticket.sandbox_id} could not be attached"),
                )
            if not isinstance(outcome, _ScopeContainer):
                return outcome
            # Postgres names a container the registry did not: the leader
            # attached it meanwhile, or the registry lost it. Use it if alive.
            logger.info(
                "[sandbox.session] scope=%s has container %s in Postgres; probing it instead of creating one",
                owner.registry_key(scope),
                outcome.sandbox_id,
            )
            existing = await _session_for_container(
                sandbox_id=outcome.sandbox_id,
                base_url=outcome.base_url,
                created_with=_bundle_from_payload(outcome.created_with, bundle),
            )
        return SandboxSessionError.of(
            "create_failed",
            _CREATE_FAILED_MESSAGE.format(
                exc=f"the containers named for this owner ({', '.join(sorted(dead)) or '-'}) were all unusable"
            ),
        )
    finally:
        await lock.release()


async def _acquire_role(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    scope: str,
    shared: bool,
    create: bool,
    db: Any | None,
    settings,
    sleep: SleepFn,
    clock: ClockFn,
    deadline: float,
) -> "SandboxSession | SandboxSessionError | _Conflict | Any | None":
    """``_acquire_in_scope`` plus the out-of-lock wait on a scope ticket."""

    outcome = await _acquire_in_scope(
        owner=owner, bundle=bundle, binding=binding, scope=scope, shared=shared, create=create,
        db=db, settings=settings, sleep=sleep, clock=clock, deadline=deadline,
    )
    if not isinstance(outcome, _TicketWait):
        return outcome
    allocated = await _await_ticket(
        owner=owner, bundle=bundle, binding=binding, wait=outcome, settings=settings, sleep=sleep, clock=clock,
    )
    if isinstance(allocated, SandboxSessionError):
        return allocated
    poll = max(1, int(settings.SANDBOX_QUEUE_POLL_SECONDS))
    # Every waiter of the ticket resumes here; the first to take the lock
    # makes the container the scope's warm session, the others then share it.
    lock = _OwnerLock(owner, scope)
    attach_deadline = clock() + max(poll, int(settings.SANDBOX_QUEUE_MAX_WAIT_SECONDS))
    if not await lock.acquire(poll=poll, deadline=attach_deadline, sleep=sleep, clock=clock):
        return SandboxSessionError.of(
            "busy_timeout",
            _BUSY_TIMEOUT_MESSAGE.format(seconds=int(settings.SANDBOX_QUEUE_MAX_WAIT_SECONDS)),
        )
    try:
        try:
            attached = await _attach_polled_ticket(
                owner=owner, bundle=bundle, binding=binding, wait=outcome, ticket=allocated,
            )
        except Exception as exc:  # noqa: BLE001 - admission never raises at the caller
            logger.warning(
                "[sandbox.session] attaching reservation %s failed: %s", outcome.reservation_id, exc, exc_info=True,
            )
            return SandboxSessionError.of("create_failed", _CREATE_FAILED_MESSAGE.format(exc=exc))
    finally:
        await lock.release()
    if attached is not None:
        return attached
    # Registered (by this caller or another waiter): bind through warm reuse,
    # which also loads this caller's Skill when the container holds another.
    return await _acquire_in_scope(
        owner=owner, bundle=bundle, binding=binding, scope=scope, shared=shared, create=False,
        db=db, settings=settings, sleep=sleep, clock=clock, deadline=attach_deadline,
    ) or SandboxSessionError.of(
        "create_failed",
        _CREATE_FAILED_MESSAGE.format(exc=f"allocated sandbox {allocated.sandbox_id} could not be attached"),
    )


async def acquire_sandbox_session(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    db: Any | None = None,
    sleep: SleepFn = asyncio.sleep,
    clock: ClockFn = time.monotonic,  # monotonic: busy-wait deadline only, never persisted
) -> "SandboxSession | SandboxSessionError | Any":
    """Return the owner's sandbox with ``bundle`` loaded, creating it on demand.

    The sandbox belongs to the owner -- the user, for human work -- and all
    of the owner's conversations, tasks and agents share it (see
    ``sandbox_reuse_scope``):

    1. a Skill that already runs in a container of its own for this owner
       goes there (older runners only);
    2. otherwise the owner's general container (or the dedicated container
       of a bundle with its own container config): warm reuse, or a new
       container on demand; Skills load side by side there;
    3. on an older runner, a Skill the general container cannot take right
       now (another Skill in use there, or env baked into an older
       container) gets its own container, without disturbing anyone.

    Each container role is single-flight: an owner lock per role, and under
    durable external-runner coordination one reservation ticket per role
    that concurrent admissions join. A durable run is suspended on its
    ticket (``RuntimeToolSuspension``); a caller with no run to suspend (a
    task worker, a delegated agent) waits for the ticket itself and returns
    the attached session. Never raises for admission outcomes.
    """

    settings = _settings()
    poll = max(1, int(settings.SANDBOX_QUEUE_POLL_SECONDS))
    from packages.core.services.sandbox_queue_service import pool_wait_seconds

    max_wait = max(poll, pool_wait_seconds(bundle.pool))
    reusable, scope = sandbox_reuse_scope(bundle, binding, owner)
    # One admission budget for this call: the lock wait and the busy wait draw
    # from the same deadline instead of each getting a full ``max_wait``.
    deadline = clock() + max_wait

    if not reusable or not scope:
        return await _acquire_unscoped(
            owner=owner, bundle=bundle, binding=binding, db=db, settings=settings, sleep=sleep, clock=clock,
        )

    common = {
        "owner": owner, "bundle": bundle, "binding": binding, "db": db,
        "settings": settings, "sleep": sleep, "clock": clock, "deadline": deadline,
    }
    dedicated = bool(bundle.config)
    own_scope = None if dedicated else _skill_container_scope(bundle, binding, owner)

    async def alongside(role: str | None) -> Any:
        # A container that cannot serve this bundle is never replaced while
        # it serves other work: a dedicated one runs alongside it. One hop:
        # that container is created for exactly this request.
        fit_scope = sandbox_registry_scope(
            user_id=owner.user_id, binding_key=binding.conversation_id, role=_misfit_role(bundle, role),
        )
        if fit_scope is None or fit_scope == scope:
            return None
        return await _acquire_role(scope=fit_scope, shared=False, create=True, **common)

    if own_scope:
        outcome = await _acquire_role(scope=own_scope, shared=False, create=False, **common)
        if isinstance(outcome, _Misfit):
            outcome = await alongside(_skill_role(bundle.skill_name))
        if outcome is not None and not isinstance(outcome, _Misfit):
            return outcome
    outcome = await _acquire_role(scope=scope, shared=not dedicated, create=True, **common)
    if isinstance(outcome, _Misfit):
        outcome = await alongside(_skill_role(bundle.skill_name) if dedicated else None)
    if outcome is _CONFLICT and own_scope:
        outcome = await _acquire_role(scope=own_scope, shared=False, create=True, **common)
    if outcome is _CONFLICT or outcome is None or isinstance(outcome, _Misfit):
        return SandboxSessionError.of("busy_timeout", _BUSY_TIMEOUT_MESSAGE.format(seconds=max_wait))
    return outcome


async def _acquire_unscoped(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    db: Any | None,
    settings,
    sleep: SleepFn = asyncio.sleep,
    clock: ClockFn = time.monotonic,
) -> "SandboxSession | SandboxSessionError | Any":
    """No scope (a ONE_SHOT container, or no binding key): nothing is bound, reused, or registered."""

    if _durable_external(settings):
        # A tool-owned container (SESSION, ONE_SHOT) has no registry identity
        # to need a user for, and an inline allocation never waits; only a
        # WARM one keeps the stricter rules below.
        tool_owned = bundle.lifetime is not SandboxLifetime.WARM
        if binding.parent_skill and not tool_owned:
            return SandboxSessionError.of(
                "nested_allocation_unavailable",
                _NESTED_ALLOCATION_MESSAGE.format(skill=bundle.skill_name, parent=binding.parent_skill),
            )
        if not binding.parent_skill and binding.runtime_run_id and binding.runtime_tool_call_id and db is not None:
            return await _enqueue_durable_reservation(
                owner=owner, bundle=bundle, binding=binding, db=db, settings=settings,
            )
        if tool_owned:
            # A task (a workflow run) waits on its own ticket; a prompt
            # skill's nested loop, or a caller outside both runtimes,
            # allocates now or not at all.
            return await _acquire_polled_unscoped(
                owner=owner, bundle=bundle, binding=binding, settings=settings, sleep=sleep, clock=clock,
                immediate=not binding.waits_for_ticket,
            )
        return SandboxSessionError.of(
            "not_configured",
            "Sandbox admission requires runtime context (runtime_run_id and "
            "tool_call_id) under external-runner coordination; refusing to "
            "bypass reservation and runner admission.",
        )
    return await _create_session(
        owner=owner,
        bundle=bundle,
        binding=binding,
        settings=settings,
        register=False,
        scope=None,
    )


async def _acquire_polled_unscoped(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    settings,
    sleep: SleepFn,
    clock: ClockFn,
    immediate: bool,
) -> "SandboxSession | SandboxSessionError":
    """A tool-owned container of the caller's own ticket: allocated now, or waited for."""

    try:
        outcome = await _enqueue_poller_ticket(
            owner=owner, bundle=bundle, binding=binding, scope=None, immediate=immediate,
        )
    except Exception as exc:  # noqa: BLE001 - admission never raises at the caller
        logger.warning(
            "[sandbox.session] reservation failed requester=%s skill=%s: %s",
            _poller_requester_id(binding),
            bundle.skill_name,
            exc,
            exc_info=True,
        )
        return SandboxSessionError.of("create_failed", _CREATE_FAILED_MESSAGE.format(exc=exc))
    if not isinstance(outcome, _TicketWait):
        return outcome
    ticket = outcome.ticket
    if ticket is None:
        ticket = await _await_ticket(
            owner=owner, bundle=bundle, binding=binding, wait=outcome, settings=settings, sleep=sleep, clock=clock,
        )
        if isinstance(ticket, SandboxSessionError):
            return ticket
    try:
        attached = await _attach_polled_ticket(
            owner=owner, bundle=bundle, binding=binding, wait=outcome, ticket=ticket,
        )
    except Exception as exc:  # noqa: BLE001 - admission never raises at the caller
        logger.warning(
            "[sandbox.session] attaching reservation %s failed: %s", outcome.reservation_id, exc, exc_info=True,
        )
        return SandboxSessionError.of("create_failed", _CREATE_FAILED_MESSAGE.format(exc=exc))
    return attached or SandboxSessionError.of(
        "create_failed",
        _CREATE_FAILED_MESSAGE.format(exc=f"allocated sandbox {ticket.sandbox_id} could not be attached"),
    )
