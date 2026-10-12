"""Owner-scoped sandbox sessions.

This module is the only admission path for skill and generic-tool sandboxes;
other producers (``workflow_runner``, ``bash_tool``, ``video_edit_tools``,
``runtime_tasks`` allocation) still create containers directly and are
outside this module's guard. Callers on this path (skill invocation, the
generic ``sandbox`` tool, ``generate_file`` office kinds) describe *what*
they need through ``SandboxBundle``/``SandboxBinding`` and receive a
``SandboxSession``; they never construct ``SandboxClient`` for admission.

Ownership unit is (entity_id, user_id), but every registry scope is
conversation-prefixed: a warm container is only ever reused inside the
conversation that warmed it. The conversation context
``sandbox:ctx:{conversation_id}`` is kept as the per-conversation binding
(sandbox id, driving agent, PPTX checkpoint) and points at that sandbox.

Under durable external-runner coordination admission goes through the
reservation queue: a durable Runtime run is suspended on its ticket, while a
task worker (which has no ``RuntimeRun`` to suspend) enqueues a task
reservation and waits synchronously for the scheduler to allocate it.

Releasing keeps a container only while it is still its scope's warm session
(``sandbox:warm:{sandbox_id}`` names that scope); anything else — anonymous
owners, unregistered containers, containers superseded by a newer
registration — is destroyed at release instead of waiting for the sandbox
service's idle reaper.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from packages.core.ai.runtime.sandbox import (
    RUNTIME_SANDBOX_CONTEXT_TTL,
    RUNTIME_SANDBOX_IDLE_THRESHOLD,
    runtime_delete_sandbox_context,
    runtime_init_sandbox_context,
    runtime_load_sandbox_context,
    runtime_save_sandbox_context,
)

logger = logging.getLogger(__name__)

SANDBOX_OWNER_REGISTRY_PREFIX = "sandbox:owner:"
SANDBOX_WARM_MARKER_PREFIX = "sandbox:warm:"
SANDBOX_RELEASE_PROBE_PREFIX = "sandbox:release-probe:"
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
_CLIENT_TIMEOUT_SECONDS = 180.0
_LOCK_TTL_SECONDS = 240
_LOCK_HEARTBEAT_SECONDS = 60


@dataclass(frozen=True)
class SandboxOwner:
    """Reuse unit for a warm sandbox. Anonymous owners never register.

    ``scope`` is required and always conversation-prefixed (see
    ``sandbox_reuse_scope``): there is no bare owner key, so a container
    warmed by one conversation is never handed to another.
    """

    entity_id: str
    user_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "entity_id", str(self.entity_id or "").strip())
        object.__setattr__(self, "user_id", str(self.user_id or "").strip())
        if not self.entity_id:
            raise ValueError("SandboxOwner requires entity_id")
        if ":" in self.entity_id or ":" in self.user_id:
            raise ValueError("SandboxOwner ids must not contain ':'")

    @property
    def registrable(self) -> bool:
        return bool(self.user_id)

    def registry_key(self, scope: str) -> str:
        if not self.registrable:
            raise ValueError("anonymous SandboxOwner has no registry key")
        if not scope:
            raise ValueError("registry scope is required")
        return f"{SANDBOX_OWNER_REGISTRY_PREFIX}{self.entity_id}:{self.user_id}:{scope}"

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
    # Optional sub-scope, nested under the conversation (``"generic"`` for
    # the generic tool).
    reuse_scope: str | None = None


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
    # The prompt skill whose nested loop is acquiring. Set only inside such a
    # loop; see ``acquire_sandbox_session`` for why it refuses a reservation.
    parent_skill: str | None = None


@dataclass(frozen=True)
class SandboxSessionError:
    """Admission outcome returned (never raised) by the session service."""

    code: str
    message: str
    retryable: bool

    @classmethod
    def of(cls, code: str, message: str) -> "SandboxSessionError":
        if code not in SANDBOX_SESSION_ERROR_CODES:
            raise ValueError(f"unknown sandbox session error code: {code}")
        return cls(code=code, message=message, retryable=code in _RETRYABLE_CODES)


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


async def is_sandbox_kept_warm(sandbox_id: str) -> bool:
    """True when ``sandbox_id`` is some scope's current warm session.

    Run-scoped cleanups (cancel) must not destroy such a container: it is
    owned by the owner registry and reused by later turns.
    """

    registry_key = await warm_registry_key_for(sandbox_id)
    return bool(registry_key) and await _is_current_warm_session(registry_key, sandbox_id)


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


async def forget_vanished_sandbox(sandbox_id: str, *, conversation_id: str | None) -> str | None:
    """Drop every binding that still names a container its runner no longer has.

    The runner's idle reaper removes containers without telling anyone, while
    the conversation context and the warm registry live for days. Left alone
    they keep routing the model's calls to a dead id. The context keeps its
    other keys (an ``expired_sandbox_id`` marker replaces ``sandbox_id``) so a
    retry with the old id is answered as an expiry rather than a denial, and
    re-opening the same skill still restores whatever the context carried.

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

    if conversation_id:
        ctx = await runtime_load_sandbox_context(conversation_id)
        if isinstance(ctx, dict) and str(ctx.get("sandbox_id") or "") == sandbox_id:
            skill = skill or str(ctx.get("skill_key") or "") or None
            expired = {key: value for key, value in ctx.items() if key != "sandbox_id"}
            expired["expired_sandbox_id"] = sandbox_id
            expired["expired_skill"] = skill or ""
            if not await runtime_save_sandbox_context(conversation_id, expired):
                await runtime_delete_sandbox_context(conversation_id)

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
    conversation_id: str | None,
    db: Any | None = None,
) -> str:
    """Unbind the conversation; keep the container only if it is still warm.

    Returns ``"kept"`` when the sandbox is its scope's current warm session,
    ``"destroyed"`` when it was destroyed because nothing can reuse it
    (anonymous owner, unregistered container, or one superseded by a newer
    registration), and ``"destroy_failed"`` when the destroy did not go
    through and the idle reaper has to reclaim it. A container is also kept
    when the cache is unreachable: without Redis, "no warm marker" is
    indistinguishable from "cannot read the warm marker". Never raises.
    """

    if conversation_id:
        ctx = await runtime_load_sandbox_context(conversation_id)
        if isinstance(ctx, dict) and str(ctx.get("sandbox_id") or "") == sandbox_id:
            await runtime_delete_sandbox_context(conversation_id)

    owner_label = f"{owner.entity_id}:{owner.user_id}" if owner is not None and owner.registrable else "(anonymous)"
    registry_key = await warm_registry_key_for(sandbox_id)
    if registry_key and await _is_current_warm_session(registry_key, sandbox_id):
        logger.info(
            "[sandbox.session] released, kept warm: sandbox=%s owner=%s scope=%s",
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

    _, scope = sandbox_reuse_scope(
        SandboxBundle(skill_name="", files={}),
        SandboxBinding(conversation_id=conversation_id, agent_id=agent_id),
    )
    if not owner.registrable or not scope:
        return "not_owned"
    session = await load_sandbox_session(owner, scope=scope)
    if session is None or session.sandbox_id != sandbox_id:
        return "not_owned"
    bound = await runtime_load_sandbox_context(conversation_id)
    if isinstance(bound, dict) and str(bound.get("sandbox_id") or "") == sandbox_id:
        await runtime_delete_sandbox_context(conversation_id)
    outcome = await _destroy_released_sandbox(sandbox_id, db=db)
    if outcome == "destroyed":
        await forget_sandbox_session(owner, sandbox_id, scope=scope)
    return outcome


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

    return SandboxClient(
        base_url=base_url,
        timeout=_CLIENT_TIMEOUT_SECONDS if timeout is None else timeout,
        api_token=_settings().SANDBOX_API_TOKEN.strip(),
    )


async def _bind_conversation(
    *,
    owner: SandboxOwner,
    session: SandboxSession,
    binding: SandboxBinding,
) -> Exception | None:
    if not binding.conversation_id:
        return None
    try:
        await runtime_init_sandbox_context(
            binding.conversation_id,
            session.sandbox_id,
            binding.skill_id,
            entity_id=owner.entity_id,
            user_id=owner.user_id,
            agent_id=binding.agent_id,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a structured error
        return exc
    return None


def sandbox_reuse_scope(bundle: SandboxBundle, binding: SandboxBinding) -> tuple[bool, str | None]:
    """Return (reusable, registry scope) for this bundle.

    Every scope is conversation-prefixed and, when there is one, gated on the
    driving agent; without a conversation nothing is reused or registered::

        no conversation      -> (False, None)
        bundle.reuse_scope   -> conversation:{conv}:agent:{agent}:{reuse_scope}
        any skill            -> conversation:{conv}:agent:{agent}

    Warmth is per conversation because ``load_skill`` clears ``/skill`` when
    the skill name changes: one container shared across conversations lets
    one of them wipe another's in-flight work. It is also per agent, for the
    same reason one level down — two agents in one conversation must not
    share a container. ``load_skill`` would let either wipe the other's
    ``/skill``, and because ``_sandbox_instance_access_error`` still requires
    the binding's agent to match, rebinding the shared container to the
    second agent locks the first one out of the sandbox it is using.

    The generic tool nests under its own ``"generic"`` sub-scope so an ad hoc
    sandbox never lands in a skill's container. Credentialed bundles
    (non-empty ``env``) share the conversation scope: ``load_skill`` replaces
    the exec-time env, so the next skill never sees the previous secrets.
    That only holds for containers the service created with exec-time
    injection; ``_reuse_session`` refuses a credentialed bundle on an older
    container and creates a fresh one instead.

    Segments are percent-encoded so a ``:`` or ``%`` inside a conversation
    id, agent id, skill name, or sub-scope cannot forge a collision with
    another scope.
    """

    if not binding.conversation_id:
        return False, None
    scope = f"conversation:{quote(binding.conversation_id, safe='')}"
    if binding.agent_id:
        scope = f"{scope}:agent:{quote(binding.agent_id, safe='')}"
    if bundle.reuse_scope:
        return True, f"{scope}:{quote(bundle.reuse_scope, safe='')}"
    return True, scope


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
            "files": dict(bundle.files),
            "env": dict(bundle.env),
            "allowed_sensitive_keys": list(bundle.allowed_sensitive_keys),
            "auto_install": bundle.auto_install,
            "config": bundle.config,
        }
        try:
            if bundle.source == "workspace":
                created = await client.create_from_files(**create_kwargs)
            else:
                created = await client.create_from_builtin(**create_kwargs)
        except SandboxCapacityError as exc:
            logger.warning("[sandbox.session] capacity full skill=%s: %s", bundle.skill_name, exc)
            return SandboxSessionError.of("capacity_full", _CAPACITY_MESSAGE.format(exc=exc))
        except SandboxError as exc:
            logger.warning("[sandbox.session] create failed skill=%s: %s", bundle.skill_name, exc)
            return SandboxSessionError.of("create_failed", _CREATE_FAILED_MESSAGE.format(exc=exc))
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
            reused=False,
            workdir=created.workdir or "/skill",
            scripts=list(created.skill.scripts or []),
            entry_hint=created.skill.entry_hint,
            requirements_installed=bool(created.skill.requirements_txt),
            env_blocked=list(created.env_blocked or []),
        )
        bind_error = await _bind_conversation(owner=owner, session=session, binding=binding)
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
        if owner.registrable and not register:
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
) -> "SandboxSession | SandboxSessionError | None":
    """Load ``bundle`` into the warm sandbox. ``None`` means: gone, create instead.

    ``deadline`` is the admission deadline shared with the owner-lock wait, so
    a call that already spent budget waiting for the lock does not get a fresh
    full budget for the busy wait.
    """

    from packages.core.services.sandbox_sdk.exceptions import SandboxError

    poll = max(1, int(settings.SANDBOX_QUEUE_POLL_SECONDS))
    started = clock()

    if existing.env_injection != "exec":
        # Env baked at create cannot be replaced, so this container is only
        # safe for the skill it already holds: a credentialed bundle cannot
        # get its own env in, and a plain bundle must not inherit credentials
        # another skill left behind. Anything else gets a fresh container.
        same_skill = existing.current_skill == bundle.skill_name
        # Same skill but a different env (credentials gained, rotated, or
        # revoked since the container was built): the old runner cannot
        # replace baked env, so the skill would silently run with the wrong
        # values.
        env_changed = env_fingerprint(bundle.env) != existing.env_fingerprint
        if ((bundle.env or existing.holds_credentials) and not same_skill) or (same_skill and env_changed):
            logger.info(
                "[sandbox.session] not reusing sandbox=%s for skill=%s: env baked at create (holds=%s)",
                existing.sandbox_id,
                bundle.skill_name,
                existing.current_skill,
            )
            return None

    while True:
        try:
            loaded = await client.load_skill(
                sandbox_id=existing.sandbox_id,
                skill_name=bundle.skill_name,
                files=dict(bundle.files),
                auto_install=bundle.auto_install,
                idle_threshold=RUNTIME_SANDBOX_IDLE_THRESHOLD,
                source=bundle.source,
                # Always sent, even when empty: this replaces the previous
                # skill's env so a plain skill after a credentialed one runs
                # without those secrets.
                env=dict(bundle.env),
                allowed_sensitive_keys=list(bundle.allowed_sensitive_keys),
            )
            break
        except SandboxError as exc:
            if getattr(exc, "status_code", None) == _BUSY_STATUS_CODE:
                remaining = deadline - clock()
                if remaining <= 0:
                    return SandboxSessionError.of(
                        "busy_timeout",
                        _BUSY_TIMEOUT_MESSAGE.format(seconds=int(clock() - started)),
                    )
                await sleep(min(poll, remaining))
                continue
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

    session = SandboxSession(
        sandbox_id=existing.sandbox_id,
        base_url=existing.base_url,
        created_at=existing.created_at,
        last_acquired_at=time.time(),
        loaded_skills=[
            *existing.loaded_skills,
            *([bundle.skill_name] if bundle.skill_name not in existing.loaded_skills else []),
        ],
        env_injection=existing.env_injection,
        current_skill=bundle.skill_name,
        holds_credentials=(
            bool(bundle.env)
            if existing.env_injection == "exec"
            else (existing.holds_credentials or bool(bundle.env))
        ),
        env_fingerprint=(
            env_fingerprint(bundle.env) if existing.env_injection == "exec" else existing.env_fingerprint
        ),
        reused=True,
        env_blocked=list(getattr(loaded, "env_blocked", None) or []),
        workdir=str(info.workdir or "/skill"),
        scripts=list(loaded.skill.scripts or []),
        entry_hint=loaded.skill.entry_hint,
        requirements_installed=bool(loaded.skill.requirements_txt),
    )
    bind_error = await _bind_conversation(owner=owner, session=session, binding=binding)
    if bind_error is not None:
        return SandboxSessionError.of(
            "context_persist_failed",
            _REUSED_CONTEXT_FAILED_MESSAGE.format(exc=bind_error),
        )
    if owner.registrable and not await register_sandbox_session(owner, session, scope=scope):
        logger.warning(
            "[sandbox.session] registry refresh failed; sandbox=%s",
            session.sandbox_id,
        )
    logger.info(
        "[sandbox.session] reused: skill=%s sandbox=%s owner=%s",
        bundle.skill_name,
        session.sandbox_id,
        owner.registry_key(scope),
    )
    return session


async def _enqueue_durable_reservation(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    db: Any,
    settings,
) -> "SandboxSessionError | Any":
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
    try:
        reservation = await sandbox_queue_service.enqueue_sandbox_reservation(
            db,
            run=run,
            tool_call_id=binding.runtime_tool_call_id,
            request_payload=_reservation_request_payload(bundle),
            activate=False,
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


def _reservation_request_payload(bundle: SandboxBundle) -> dict[str, Any]:
    return {
        "skill_name": bundle.skill_name,
        "files": dict(bundle.files),
        "env": dict(bundle.env),
        "allowed_sensitive_keys": list(bundle.allowed_sensitive_keys),
        "auto_install": bundle.auto_install,
        "config": bundle.config,
        "source": bundle.source,
    }


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
    client = _new_client(base_url, timeout=_HEALTH_PROBE_TIMEOUT_SECONDS)
    try:
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
        await client.close()
    return "exec" if str((health or {}).get("env_injection") or "") == "exec" else "create"


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

    from datetime import datetime, timezone

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
                SandboxReservation.tool_call_id == binding.runtime_tool_call_id,
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
    if not reservation.sandbox_id:
        return SandboxSessionError.of(
            "create_failed", "Sandbox allocation is missing its Sandbox identity.",
        )
    runner = await resolve_sandbox_runner(db, reservation.sandbox_id)
    base_url = str(runner.base_url or "")
    was_allocated = status == SandboxReservationStatus.ALLOCATED.value
    now = time.time()
    bind_error = await _bind_conversation(
        owner=owner,
        session=SandboxSession(
            sandbox_id=reservation.sandbox_id, base_url=base_url,
            created_at=now, last_acquired_at=now,
        ),
        binding=binding,
    )
    if bind_error is not None:
        if was_allocated:
            # Nobody can reach a container whose owner context was never
            # written: destroy it rather than leave it pinning a runner slot.
            reservation.last_error = "sandbox_context_persistence_failed"
            if await _destroy_container(reservation.sandbox_id, db=db, external=True) != "destroyed":
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
    # The scheduler created this container; ask the runner how it injects
    # skill env so the registry record is reusable.
    session = await build_allocated_session(
        sandbox_id=reservation.sandbox_id, base_url=base_url, bundle=bundle,
    )
    reusable, scope = sandbox_reuse_scope(bundle, binding)
    if reusable and owner.registrable:
        await register_sandbox_session(owner, session, scope=scope)
    await db.commit()
    return session


async def _wait_for_task_allocation(
    *,
    session_db: Any,
    reservation_id: str,
    poll: int,
    deadline: float,
    sleep: SleepFn,
    clock: ClockFn,
) -> "str | SandboxSessionError":
    """Poll one task ticket until it is allocated, terminal, or out of budget.

    Returns the allocated ``sandbox_id``, or the admission outcome to hand
    back to the caller. Every read ends the previous transaction and expires
    the identity map first: the ticket is written by the scheduler in another
    transaction, so a cached row — or a snapshot held open across polls — would
    report ``queued`` forever. The rollback also keeps this session from
    holding an idle-in-transaction connection for the whole wait; the ticket
    itself was committed before the first poll.
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
            sandbox_id = str(getattr(ticket, "sandbox_id", "") or "")
            if sandbox_id:
                return sandbox_id
        elif status in terminal:
            detail = str(getattr(ticket, "last_error", "") or "").strip()
            await session_db.rollback()
            return SandboxSessionError.of(
                "capacity_full",
                _TASK_TICKET_TERMINAL_MESSAGE.format(
                    status=status,
                    detail=f" Details: {detail}" if detail else "",
                ),
            )
        if clock() >= deadline:
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


async def _task_reservation_flow(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    settings,
    sleep: SleepFn,
    clock: ClockFn,
    reusable: bool,
    scope: str | None,
) -> "SandboxSession | SandboxSessionError":
    from packages.core.models.runtime_run import SandboxRequesterKind
    from packages.core.services import sandbox_queue_service
    from packages.core.services.sandbox_queue_service import (
        SandboxAllocationStateError,
        SandboxQueueFullError,
    )

    poll = max(1, int(settings.SANDBOX_QUEUE_POLL_SECONDS))
    # A fresh wait budget: the ticket only starts existing here, so the time
    # already spent on the owner lock is not charged against the scheduler.
    max_wait = max(poll, int(settings.SANDBOX_QUEUE_MAX_WAIT_SECONDS))
    # One ticket per acquire attempt: the reservation is idempotent on
    # (requester, tool_call_id), and a task worker has no tool call id of its
    # own, so a fresh suffix keeps a retry from adopting a spent ticket.
    tool_call_id = f"{binding.task_id}:{uuid.uuid4().hex}"

    async with _task_ticket_db_session() as session_db:
        try:
            reservation = await sandbox_queue_service.enqueue_sandbox_reservation(
                session_db,
                run=None,
                tool_call_id=tool_call_id,
                request_payload=_reservation_request_payload(bundle),
                activate=True,
                entity_id=owner.entity_id,
                user_id=owner.user_id,
                requester_kind=SandboxRequesterKind.TASK.value,
                requester_id=binding.task_id,
            )
        except SandboxQueueFullError as exc:
            # The failed enqueue still opened a transaction (and took the
            # queue advisory lock); this session is the flow's own, so ending
            # it here expires nothing the caller holds.
            await session_db.rollback()
            logger.warning(
                "[sandbox.session] task=%s reservation queue full skill=%s: %s",
                binding.task_id,
                bundle.skill_name,
                exc,
            )
            return SandboxSessionError.of("capacity_full", _QUEUE_FULL_MESSAGE.format(exc=exc))
        reservation_id = reservation.id
        await session_db.commit()

        allocated = await _wait_for_task_allocation(
            session_db=session_db,
            reservation_id=reservation_id,
            poll=poll,
            deadline=clock() + max_wait,
            sleep=sleep,
            clock=clock,
        )
        if isinstance(allocated, SandboxSessionError):
            return allocated

        runner = await sandbox_queue_service.resolve_sandbox_runner(session_db, allocated)
        env_injection = await _runner_env_injection(str(runner.base_url or ""))
        now = time.time()
        session = SandboxSession(
            sandbox_id=allocated,
            base_url=str(runner.base_url or ""),
            created_at=now,
            last_acquired_at=now,
            loaded_skills=[bundle.skill_name],
            env_injection=env_injection,
            current_skill=bundle.skill_name,
            holds_credentials=bool(bundle.env),
            env_fingerprint=env_fingerprint(bundle.env),
            reused=False,
            scripts=[key for key in bundle.files if key not in _NON_SCRIPT_BUNDLE_FILES],
            requirements_installed="requirements.txt" in bundle.files,
            env_blocked=[],
        )
        bind_error = await _bind_conversation(owner=owner, session=session, binding=binding)
        if bind_error is not None:
            # Nothing can reach a container nobody is bound to; destroy it
            # rather than leave an allocated sandbox to the idle reaper.
            try:
                await _destroy_container(allocated, db=session_db, external=True)
            except Exception as exc:  # noqa: BLE001 - the admission outcome stands
                logger.warning(
                    "[sandbox.session] unbound task sandbox=%s could not be destroyed: %s",
                    allocated,
                    exc,
                )
            await session_db.commit()
            return SandboxSessionError.of(
                "context_persist_failed",
                _NEW_CONTEXT_FAILED_MESSAGE.format(exc=bind_error),
            )
        try:
            await sandbox_queue_service.consume_sandbox_reservation(
                session_db,
                reservation_id=reservation_id,
            )
        except SandboxAllocationStateError as exc:
            # The ticket stopped being ALLOCATED between the poll and here —
            # the abandoned-allocation sweep released it while this worker was
            # binding. The binding above is now a promise to a container this
            # call no longer owns, so take it back before returning; leaving it
            # would deny the conversation every later sandbox call. The
            # container is NOT destroyed: the sweep already released the
            # instance and the runner's idle reaper owns what is left.
            if binding.conversation_id:
                try:
                    await runtime_delete_sandbox_context(binding.conversation_id)
                except Exception as unbind_exc:  # noqa: BLE001 - the outcome stands
                    logger.warning(
                        "[sandbox.session] could not unbind conversation=%s after a lost task ticket: %s",
                        binding.conversation_id,
                        unbind_exc,
                    )
            await session_db.rollback()
            logger.warning(
                "[sandbox.session] task reservation %s was released before consume: %s",
                reservation_id,
                exc,
            )
            return SandboxSessionError.of(
                "capacity_full",
                f"Sandbox reservation {reservation_id} was released before it could be consumed ({exc}); retry.",
            )
        await session_db.commit()
        if reusable and scope and not await register_sandbox_session(owner, session, scope=scope):
            logger.warning(
                "[sandbox.session] registry write failed; sandbox=%s will not be reused",
                session.sandbox_id,
            )
        logger.info(
            "[sandbox.session] task reservation allocated: task=%s skill=%s sandbox=%s reservation=%s",
            binding.task_id,
            bundle.skill_name,
            session.sandbox_id,
            reservation_id,
        )
        return session


async def _acquire_task_reservation(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    settings,
    sleep: SleepFn,
    clock: ClockFn,
    reusable: bool,
    scope: str | None,
) -> "SandboxSession | SandboxSessionError":
    """Enqueue a task ticket, wait for allocation, and attach to the Sandbox.

    A task worker has no ``RuntimeRun`` to suspend and nothing resumes it, so
    unlike the durable branch it does not return a suspension: it blocks on
    its own ticket for the queue wait budget. Never raises — an unexpected
    failure is an admission outcome like any other. The caller's database
    session is deliberately not accepted: the wait runs on its own session
    (see ``_task_ticket_db_session``).
    """

    try:
        return await _task_reservation_flow(
            owner=owner,
            bundle=bundle,
            binding=binding,
            settings=settings,
            sleep=sleep,
            clock=clock,
            reusable=reusable,
            scope=scope,
        )
    except Exception as exc:  # noqa: BLE001 - admission never raises at the caller
        logger.warning(
            "[sandbox.session] task reservation failed task=%s skill=%s: %s",
            binding.task_id,
            bundle.skill_name,
            exc,
            exc_info=True,
        )
        return SandboxSessionError.of("create_failed", _CREATE_FAILED_MESSAGE.format(exc=exc))


class _OwnerLock:
    """Single-flight lease per (owner, scope), kept alive by a heartbeat.

    ``held`` stays False when Redis is unavailable (``acquire_lease`` returns
    None); admission then proceeds unlocked rather than waiting on a lock
    that cannot exist.
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


async def acquire_sandbox_session(
    *,
    owner: SandboxOwner,
    bundle: SandboxBundle,
    binding: SandboxBinding,
    db: Any | None = None,
    sleep: SleepFn = asyncio.sleep,
    clock: ClockFn = time.monotonic,  # monotonic: busy-wait deadline only, never persisted
) -> "SandboxSession | SandboxSessionError | Any":
    """Return the owner's warm sandbox with ``bundle`` loaded, creating one if needed.

    Order: single-flight lock → registry → liveness probe → load_skill (wait
    while busy) → create (local direct, or a reservation under durable
    external-runner coordination) → register → bind conversation. Returns
    ``RuntimeToolSuspension`` when a durable run's reservation was queued; a
    task worker has no run to suspend, so it waits on its own task reservation
    and returns the attached session instead. Never raises for admission
    outcomes.
    """

    settings = _settings()
    poll = max(1, int(settings.SANDBOX_QUEUE_POLL_SECONDS))
    max_wait = max(poll, int(settings.SANDBOX_QUEUE_MAX_WAIT_SECONDS))
    reusable, scope = sandbox_reuse_scope(bundle, binding)
    reusable = reusable and owner.registrable

    # One admission budget for this call: the lock wait and the busy wait draw
    # from the same deadline instead of each getting a full ``max_wait``.
    deadline = clock() + max_wait

    lock = _OwnerLock(owner, scope) if reusable else None
    if lock is not None:
        proceed = await lock.acquire(poll=poll, deadline=deadline, sleep=sleep, clock=clock)
        if not proceed:
            return SandboxSessionError.of("busy_timeout", _BUSY_TIMEOUT_MESSAGE.format(seconds=max_wait))
    try:
        if reusable:
            existing = await load_sandbox_session(owner, scope=scope)
            if existing is not None:
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
                # Forgetting alone would leak: nothing else knows this
                # container exists once its registry entry is gone. "gone"
                # is the one state with nothing left to destroy.
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
                await forget_sandbox_session(owner, existing.sandbox_id, scope=scope)

        if _durable_external(settings):
            if binding.parent_skill:
                # A prompt skill's nested loop runs inside the durable run's
                # tool call: a reservation queued here would suspend the outer
                # run at that call, and resuming re-runs the whole nested loop
                # from scratch, whose fresh model turn need not ask for the
                # same skill again. Nothing can safely wait on such a ticket,
                # so refuse before one is written; the parent agent can invoke
                # the sandbox skill directly and be parked the normal way.
                return SandboxSessionError.of(
                    "nested_allocation_unavailable",
                    _NESTED_ALLOCATION_MESSAGE.format(
                        skill=bundle.skill_name, parent=binding.parent_skill,
                    ),
                )
            if binding.runtime_run_id and binding.runtime_tool_call_id and db is not None:
                return await _enqueue_durable_reservation(
                    owner=owner, bundle=bundle, binding=binding, db=db, settings=settings
                )
            if binding.task_id and owner.registrable:
                return await _acquire_task_reservation(
                    owner=owner,
                    bundle=bundle,
                    binding=binding,
                    settings=settings,
                    sleep=sleep,
                    clock=clock,
                    reusable=reusable,
                    scope=scope,
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
            register=reusable,
            scope=scope,
        )
    finally:
        if lock is not None:
            await lock.release()
