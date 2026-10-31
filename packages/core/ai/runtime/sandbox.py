from __future__ import annotations

import logging
import posixpath
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)


RUNTIME_SANDBOX_BINDING_PREFIX = "sandbox:bind:"
RUNTIME_SANDBOX_CONTEXT_TTL = 7 * 24 * 3600
RUNTIME_SANDBOX_IDLE_THRESHOLD = 30

# Task-worker runs have no chat thread, so there is no conversation id to bind a
# sandbox to. Synthesize a deterministic binding key from the task id instead:
# it is the same value for every tool call of that task, so admission reuse, the
# authorization recheck, and the per-call owner check all agree.
#
# The key is shaped to FIT a conversation column rather than to be kept out of
# one. Conversation ids are ULIDs in ``String(26)`` columns all over the schema
# (media_jobs, hitl_requests, runtime_event_logs, token_usage_logs,
# credit_usage_logs, runtime_runs, tool_call_logs, and a dozen more), and a
# longer value raises StringDataRightTruncation at the first site that stores
# it. So the key is at most 26 characters and every
# column accepts it; ``test_task_binding_key_fits_every_conversation_column``
# holds that for the whole schema. Consequently there is no per-site shield:
# a task-scoped media job, for example, simply records the task binding as its
# conversation scope, which is the intended per-task scoping.
#
# ``U`` is outside Crockford base32 (the ULID alphabet skips I, L, O and U), so
# no real conversation id can begin with it and the namespaces cannot collide.
RUNTIME_TASK_BINDING_PREFIX = "U"
RUNTIME_TASK_BINDING_KEY_MAX_LENGTH = 26


def runtime_workflow_binding_key(run_id: str) -> str:
    """Bind Workflow work without inventing a Task or Conversation identity.

    Like task keys, this fits the existing 26-character scope columns. ``I``
    is outside the ULID alphabet and distinct from task ``U`` and room ``L``;
    task-id recovery must never mistake a Workflow scope for a Task row.
    """

    run = str(run_id or "").strip()
    return "I" + run[-25:] if run else ""


def runtime_task_binding_key(task_id: str) -> str:
    """Return the synthetic sandbox binding key for a conversation-less task.

    ``U`` replaces the task id's first character so the key stays within the
    26-character conversation columns. A ULID's leading character encodes the
    top bits of its millisecond timestamp and stays ``"0"`` until timestamps
    reach 2**45 ms (December 3084), so for real task ids the displaced
    character is recoverable; see ``runtime_task_id_from_binding_key``.
    """

    task = str(task_id or "").strip()
    if not task:
        return ""
    return RUNTIME_TASK_BINDING_PREFIX + task[-(RUNTIME_TASK_BINDING_KEY_MAX_LENGTH - 1):]


def runtime_is_task_binding_key(value: str | None) -> bool:
    """Return true when a binding key was synthesized from a task id."""

    return (
        isinstance(value, str)
        and value.startswith(RUNTIME_TASK_BINDING_PREFIX)
        and 0 < len(value) <= RUNTIME_TASK_BINDING_KEY_MAX_LENGTH
    )


def runtime_task_id_from_binding_key(value: str | None) -> str | None:
    """Recover the task id a binding key was synthesized from.

    A key at the full 26-character width came from a 26-character ULID whose
    leading ``"0"`` was displaced by the prefix, so it is restored. A shorter
    key never lost a character to truncation, so dropping the prefix is exact:
    short and non-ULID task ids round-trip verbatim.
    """

    if not runtime_is_task_binding_key(value):
        return None
    key = str(value)
    if len(key) == RUNTIME_TASK_BINDING_KEY_MAX_LENGTH:
        return "0" + key[1:]
    return key[1:]


_PERSISTED_PPTX_CONTEXT_KEYS = (
    "pptx_checkpoint_rel_path",
    "pptx_checkpoint_sha256",
    "pptx_checkpoint_size",
    "pptx_checkpoint_saved_at",
    "pptx_checkpoint_progress",
    "pptx_project_path",
    "pptx_snapshot_complete",
)


# ── Ownership scope ──────────────────────────────────────────────
#
# A sandbox belongs to its OWNER, never to an agent or a conversation:
#
#   * a human run: the USER (entity + user). One general sandbox per user is
#     shared by all of that user's conversations, tasks, agents and Skills,
#     created on demand. Each conversation or task works in an area of its
#     own inside it (``runtime_sandbox_layout``). Another user -- also in a
#     multi-member workspace conversation -- never reaches it.
#   * a user-less run (Strategist/scheduler/system task): the workspace (or
#     the entity when there is none), keyed by the task, so it is never shared
#     with a human's sandbox nor with another task.
#   * a group room run: the room runtime's one-shot per-run key (``L...``),
#     even though it carries the requesting user: room members drive it.
#
# The acting agent and the conversation are never part of ownership. They are
# recorded for audit (who created the sandbox, which agents used it) and in a
# per-agent VIEW keyed by the work (conversation or task) and the agent (which
# Skill this agent drives where), which per-agent rules read: Skill
# credentials reach only the agents that invoked that Skill, generic create is
# refused while the agent drives a governed Skill, the document media guard.
#
# Keys inside one scope (``RuntimeSandboxScope.key``)::
#
#     sbx:{sandbox_id}            -> the binding record: the sandbox belongs to the owner
#     skill:{skill_id}            -> {"sandbox_id"}: where the owner runs that Skill
#     workskill:{work_key}|{skill_id} -> {"sandbox_id"}: a Skill THIS work opened
#     agent:{work_key}|{agent}    -> the agent's view {"skill": {...}, "generic": id}
#
# Every reader and writer goes through the functions below; nothing else may
# build a binding cache key (``tests/test_sandbox_binding_scope.py`` scans the
# repository for it).

# The agent key for the master agent, which runs with no agent id.
RUNTIME_SANDBOX_MAIN_AGENT_KEY = "main"
# Shared root inside every sandbox. Each conversation or task has its own work
# area under WORK_ROOT; SHARED_DIR is where a user deliberately leaves files
# for their other conversations (same sandbox, equally ephemeral).
RUNTIME_SANDBOX_SHARED_ROOT = "/workspace"
RUNTIME_SANDBOX_WORK_ROOT = "/workspace/c"
RUNTIME_SANDBOX_SHARED_DIR = "/workspace/shared"
# Group room runs bind to "L" + run id[1:]. ``L`` is outside Crockford base32
# like ``U``, so no conversation or task key can collide with it.
RUNTIME_ROOM_BINDING_PREFIX = "L"
# The owner segment of a per-user scope. Every other segment is
# percent-encoded, which turns a literal ``*`` into ``%2A``, so no binding key
# can forge it.
_USER_OWNER_SEGMENT = "*"


def runtime_is_room_binding_key(value: str | None) -> bool:
    """True for a group room run's one-shot binding key."""

    return (
        isinstance(value, str)
        and value.startswith(RUNTIME_ROOM_BINDING_PREFIX)
        and len(value) == RUNTIME_TASK_BINDING_KEY_MAX_LENGTH
    )


def runtime_room_binding_key(run_id: str) -> str:
    """The one-shot sandbox binding key of a group room run (26 characters)."""

    run = str(run_id or "").strip()
    if len(run) != RUNTIME_TASK_BINDING_KEY_MAX_LENGTH:
        raise ValueError("a room binding key needs a 26-character run id")
    return RUNTIME_ROOM_BINDING_PREFIX + run[1:]


class RuntimeSandboxKind(str, Enum):
    """What an agent asked for. A per-agent view, never an ownership slot."""

    # The blank sandbox from ``sandbox action='create'``.
    GENERIC = "generic"
    # A governed Skill opened by ``invoke_skill`` (generate_file's office
    # kinds invoke the matching Skill, so they count here too).
    SKILL = "skill"


def runtime_sandbox_agent_key(agent_id: str | None) -> str:
    """Stable, path-safe key for an acting agent (the master agent is ``main``)."""

    raw = str(agent_id or "").strip()
    if not raw:
        return RUNTIME_SANDBOX_MAIN_AGENT_KEY
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in raw)[:64]
    return safe or RUNTIME_SANDBOX_MAIN_AGENT_KEY


def runtime_sandbox_area_key(binding_key: str | None) -> str:
    """Path-safe directory name of one conversation's or task's work area."""

    raw = str(binding_key or "").strip()
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in raw)[:64]
    return safe or "default"


@dataclass(frozen=True)
class RuntimeSandboxLayout:
    """Where one agent's work lives inside its owner's shared sandbox.

    * ``work_area``  -- ``/workspace/c/<conversation-or-task>``: the area of
      the conversation or task; its agents share it.
    * ``agent_dir``  -- ``<work_area>/agents/<agent>``: the agent's default
      working directory. It is the ONLY root a command's file changes may be
      synced back from and attributed to that agent (and that conversation or
      task): a command never owns files outside it, even when other commands
      of the same user run next to it.
    * ``shared_dir`` -- ``/workspace/shared``: files the user deliberately
      shares across their conversations and tasks; never synced back.

    Every directory is created on demand by the runner and is ephemeral:
    durable results are saved to the user's files.
    """

    work_area: str
    agent_dir: str
    shared_dir: str = RUNTIME_SANDBOX_SHARED_DIR

    def owns_path(self, path: str) -> bool:
        """True when ``path`` lies inside this agent's directory (attribution root)."""

        normalized = posixpath.normpath(str(path or ""))
        return normalized == self.agent_dir or normalized.startswith(self.agent_dir + "/")


def runtime_sandbox_layout(binding_key: str | None, agent_id: str | None) -> RuntimeSandboxLayout:
    """The work layout of one agent working for one conversation or task."""

    area = f"{RUNTIME_SANDBOX_WORK_ROOT}/{runtime_sandbox_area_key(binding_key)}"
    return RuntimeSandboxLayout(
        work_area=area,
        agent_dir=f"{area}/agents/{runtime_sandbox_agent_key(agent_id)}",
    )


def runtime_sandbox_layout_for(context: Any) -> RuntimeSandboxLayout:
    """The layout for a caller holding a ``RuntimeToolCallContext`` (bash, sandbox tool)."""

    return runtime_sandbox_layout(
        getattr(context, "sandbox_binding_key", None) or getattr(context, "conversation_id", None),
        getattr(context, "agent_id", None),
    )


def runtime_sandbox_agent_workdir(agent_id: str | None, binding_key: str | None = None) -> str:
    """The acting agent's default working directory inside its owner's sandbox."""

    return runtime_sandbox_layout(binding_key, agent_id).agent_dir


# One line of guidance for the model wherever a shared sandbox is handed out.
RUNTIME_SANDBOX_LAYOUT_NOTE = (
    "This sandbox is shared by all of your work for this user; your working directory "
    "is this conversation's own area (other conversations and tasks have theirs). Put "
    "files another of the user's conversations should see in /workspace/shared. Everything "
    "here is temporary: save results with save_result. For conflicting package versions, "
    "install into a venv inside your own area (python -m venv .venv)."
)


@dataclass(frozen=True)
class RuntimeSandboxScope:
    """The owner of a set of sandboxes, and the work a caller does in them.

    ``binding_key`` is the WORK key: the root conversation id for a human
    run, the task's synthetic key (``runtime_task_binding_key``) for a task
    or a user-less run, and the room runtime's per-run key for a group run.
    Ownership is per user for a human run (``shared``), so every conversation
    and task of the user resolves the same sandboxes; it is the binding key
    itself -- one task, one room run -- for user-less and room work.
    """

    entity_id: str
    user_id: str
    binding_key: str
    workspace_id: str = ""

    def __post_init__(self) -> None:
        for name in ("entity_id", "user_id", "binding_key", "workspace_id"):
            object.__setattr__(self, name, str(getattr(self, name) or "").strip())
        if self.user_id:
            # A human owns the scope; a workspace id never widens or narrows it.
            object.__setattr__(self, "workspace_id", "")
        if not self.entity_id or not self.binding_key:
            raise ValueError("a sandbox scope requires an entity and a binding key")

    @property
    def shared(self) -> bool:
        """True when the user's sandboxes serve all of their conversations and tasks."""

        return bool(self.user_id) and not runtime_is_room_binding_key(self.binding_key)

    @property
    def principal(self) -> str:
        """The key segment of the owner: the user, or ``system`` for user-less work.

        User-less work is owned by its workspace (``workspace_id``, recorded on
        every binding for attribution) or by the entity, but it is isolated by
        its binding key -- one task -- so the key does not depend on whether a
        caller can see the workspace.
        """

        return self.user_id or "system"

    @property
    def owner_key(self) -> str:
        """The isolating segment: the whole user, or one task / room run."""

        return _USER_OWNER_SEGMENT if self.shared else self.binding_key

    @property
    def work_key(self) -> str:
        return self.binding_key

    @property
    def prefix(self) -> str:
        # Every segment but the per-user marker is percent-encoded so no id
        # can forge another scope.
        owner = self.owner_key if self.shared else quote(self.owner_key, safe="")
        segments = (owner, quote(self.entity_id, safe=""), quote(self.principal, safe=""))
        return RUNTIME_SANDBOX_BINDING_PREFIX + ":".join(segments)

    def key(self, kind: str, value: str) -> str:
        return f"{self.prefix}:{kind}:{quote(str(value), safe='')}"

    def agent_view_key(self, agent_id: str | None) -> str:
        """The per-agent view of one agent in this scope's work (conversation or task)."""

        return self.key("agent", f"{self.binding_key}|{runtime_sandbox_agent_key(agent_id)}")

    @property
    def layout_root(self) -> str:
        return runtime_sandbox_layout(self.binding_key, None).work_area

    def layout(self, agent_id: str | None) -> RuntimeSandboxLayout:
        return runtime_sandbox_layout(self.binding_key, agent_id)

    def owns(self, ctx: Any) -> bool:
        """True when a stored binding record was written for this scope's owner."""

        return (
            isinstance(ctx, dict)
            and str(ctx.get("entity_id") or "") == self.entity_id
            and str(ctx.get("user_id") or "") == self.user_id
            and str(ctx.get("owner") or self.principal) == self.principal
        )


async def runtime_sandbox_client(sandbox_id: str):
    """A client for an existing sandbox, routed to the runner that hosts it.

    Code outside the sandbox tool that must reach a sandbox it did not create
    (image delivery, for one) goes through here: on external runners the
    container lives on its own runner, not behind ``SANDBOX_SERVICE_URL``.
    The caller closes the client. Containers are created only through
    ``sandbox_session_service.admit_sandbox``.
    """

    from packages.core.ai.tools.sandbox_tools import _get_client_for_sandbox

    return await _get_client_for_sandbox(sandbox_id)


def runtime_sandbox_kind_of(ctx: Any) -> RuntimeSandboxKind | None:
    """Whether a binding record holds a governed Skill right now."""

    if not isinstance(ctx, dict):
        return None
    return RuntimeSandboxKind.SKILL if runtime_binding_skills(ctx) else RuntimeSandboxKind.GENERIC


def runtime_binding_skills(ctx: Any) -> dict[str, str]:
    """The governed Skills loaded in a bound sandbox: ``{skill_id: skill_key}``.

    Skills coexist in one sandbox (each in its own directory), so a record
    holds every Skill loaded there; records written before that hold one
    ``skill_id``.
    """

    if not isinstance(ctx, dict):
        return {}
    skills = ctx.get("skills")
    found = (
        {str(key): str(value or "") for key, value in skills.items() if str(key)}
        if isinstance(skills, dict)
        else {}
    )
    single = str(ctx.get("skill_id") or "")
    if single and single not in found:
        found[single] = str(ctx.get("skill_key") or "")
    return found


def runtime_sandbox_scope(
    *,
    entity_id: str | None,
    user_id: str | None,
    binding_key: str | None,
    workspace_id: str | None = None,
) -> RuntimeSandboxScope | None:
    """The owner's sandbox scope for one piece of work, or None when the run has no scope.

    None means the caller has no entity or no conversation/task binding key,
    and every sandbox operation must be denied.
    """

    if not str(entity_id or "").strip() or not str(binding_key or "").strip():
        return None
    return RuntimeSandboxScope(
        entity_id=str(entity_id or ""),
        user_id=str(user_id or ""),
        binding_key=str(binding_key or ""),
        workspace_id=str(workspace_id or ""),
    )


def primary_sandbox_scope_for(context: Any) -> RuntimeSandboxScope | None:
    """The sandbox scope a running principal operates in.

    The one resolver for callers holding a ``RuntimeToolCallContext`` (the
    sandbox tool, bash-in-sandbox, specialized containers). It reads the
    trusted runtime identity only: the user, the binding key the tool-context
    choke point derived (the root conversation, or the task for user-less and
    conversation-less runs) and, for a user-less run, the envelope's
    workspace.
    """

    user_id = str(getattr(context, "user_id", None) or "")
    workspace_id = ""
    if not user_id:
        envelope = getattr(context, "runtime_envelope", None)
        workspace_id = str(getattr(envelope, "workspace_id", None) or "")
    return runtime_sandbox_scope(
        entity_id=getattr(context, "entity_id", None),
        user_id=user_id,
        binding_key=getattr(context, "sandbox_binding_key", None) or getattr(context, "conversation_id", None),
        workspace_id=workspace_id,
    )


async def _safe_get(key: str) -> Any:
    try:
        from packages.core.cache import cache

        return await cache.get(key)
    except Exception:  # noqa: BLE001 - an unreadable key is an absent one
        return None


async def _cache_set(key: str, value: Any) -> bool:
    try:
        from packages.core.cache import cache

        saved = bool(await cache.set(key, value, ttl=RUNTIME_SANDBOX_CONTEXT_TTL))
    except Exception as exc:  # noqa: BLE001 - reported as not saved
        logger.warning("[runtime.sandbox] binding save failed for %s: %s", key, exc)
        return False
    if not saved:
        logger.warning("[runtime.sandbox] binding save unavailable: %s", key)
    return saved


async def _cache_delete(key: str) -> bool:
    try:
        from packages.core.cache import cache

        await cache.delete(key)
    except Exception as exc:  # noqa: BLE001 - the key expires on its own
        logger.warning("[runtime.sandbox] binding delete failed for %s: %s", key, exc)
        return False
    return True


async def runtime_save_sandbox_binding(scope: RuntimeSandboxScope, ctx: dict[str, Any]) -> bool:
    """Persist one sandbox's binding record (keyed by the sandbox it names)."""

    sandbox_id = str(ctx.get("sandbox_id") or ctx.get("expired_sandbox_id") or ctx.get("released_sandbox_id") or "")
    if not sandbox_id:
        raise ValueError("a sandbox binding record must name its sandbox")
    return await _cache_set(scope.key("sbx", sandbox_id), ctx)


async def runtime_load_sandbox_binding(scope: RuntimeSandboxScope, sandbox_id: str) -> dict[str, Any] | None:
    """The scope's binding record for ``sandbox_id``; None when it is not the scope's.

    An expired record (the runner reclaimed the container) is returned too: it
    carries ``expired_sandbox_id`` instead of ``sandbox_id``.
    """

    sandbox_id = str(sandbox_id or "").strip()
    if not sandbox_id:
        return None
    ctx = await _safe_get(scope.key("sbx", sandbox_id))
    if isinstance(ctx, dict):
        return ctx if scope.owns(ctx) else None
    return await _adopt_legacy_sandbox_binding(scope, sandbox_id)


def runtime_binding_is_live(ctx: Any, sandbox_id: str) -> bool:
    """True when a record binds the live ``sandbox_id`` (not its expiry marker)."""

    return isinstance(ctx, dict) and bool(sandbox_id) and str(ctx.get("sandbox_id") or "") == sandbox_id


async def runtime_delete_sandbox_binding(
    scope: RuntimeSandboxScope,
    sandbox_id: str,
    *,
    tombstone: bool = False,
) -> bool:
    """Unbind ``sandbox_id`` from the scope. Returns True when a record was removed.

    The record is loaded first so a legacy binding is adopted (and its legacy
    key removed) instead of resurfacing once the scoped record is gone. The
    conversation's Skill index entry naming the sandbox goes with it; an agent
    view that still names it is harmless (every view is checked against the
    record). ``tombstone`` leaves a ``released_sandbox_id`` marker instead of
    nothing, so the scope can tell its own released sandbox (a repeated
    release is benign) from a sandbox that was never its own (denied).
    """

    ctx = await runtime_load_sandbox_binding(scope, sandbox_id)
    if not isinstance(ctx, dict):
        return False
    for skill_id in runtime_binding_skills(ctx):
        pointer = await _safe_get(scope.key("skill", skill_id))
        if _pointer_sandbox(pointer) == sandbox_id:
            await _cache_delete(scope.key("skill", skill_id))
    if tombstone:
        marker = {
            "released_sandbox_id": sandbox_id,
            "entity_id": scope.entity_id,
            "user_id": scope.user_id,
            "owner": scope.principal,
        }
        if await _cache_set(scope.key("sbx", sandbox_id), marker):
            return True
    return await _cache_delete(scope.key("sbx", sandbox_id))


def runtime_binding_was_released(ctx: Any, sandbox_id: str) -> bool:
    """True when the scope's record for ``sandbox_id`` is a release tombstone."""

    return isinstance(ctx, dict) and str(ctx.get("released_sandbox_id") or "") == sandbox_id


async def runtime_expire_sandbox_binding(scope: RuntimeSandboxScope, sandbox_id: str, skill: str | None) -> bool:
    """Mark the scope's record for a container its runner reclaimed.

    The record keeps its other keys (PPTX checkpoint, Skill) under an
    ``expired_sandbox_id`` marker, so a retry with the old id is answered as an
    expiry rather than a denial and re-opening the same Skill restores what
    the record carried.
    """

    ctx = await runtime_load_sandbox_binding(scope, sandbox_id)
    if not runtime_binding_is_live(ctx, sandbox_id):
        return False
    expired = {key: value for key, value in ctx.items() if key != "sandbox_id"}
    expired["expired_sandbox_id"] = sandbox_id
    expired["expired_skill"] = skill or str(ctx.get("skill_key") or "")
    if await runtime_save_sandbox_binding(scope, expired):
        return True
    return await runtime_delete_sandbox_binding(scope, sandbox_id)


async def runtime_agent_sandbox(
    scope: RuntimeSandboxScope,
    agent_id: str | None,
    kind: RuntimeSandboxKind,
) -> tuple[str, dict[str, Any]] | None:
    """The sandbox (and its record) this agent uses for ``kind`` in the scope.

    SKILL: where this agent invoked a Skill, while that Skill is still the one
    loaded there. GENERIC: the sandbox ``sandbox action='create'`` gave it.
    """

    view = await _safe_get(scope.agent_view_key(agent_id))
    if not isinstance(view, dict):
        return None
    if kind is RuntimeSandboxKind.SKILL:
        entry = view.get("skill")
        if not isinstance(entry, dict) or not str(entry.get("skill_id") or ""):
            return None
        sandbox_id = str(entry.get("sandbox_id") or "")
        ctx = await runtime_load_sandbox_binding(scope, sandbox_id)
        if runtime_binding_is_live(ctx, sandbox_id) and str(entry["skill_id"]) in runtime_binding_skills(ctx):
            return sandbox_id, ctx
        return None
    sandbox_id = str(view.get("generic") or "")
    ctx = await runtime_load_sandbox_binding(scope, sandbox_id)
    if runtime_binding_is_live(ctx, sandbox_id):
        return sandbox_id, ctx
    return None


async def runtime_agent_holds_skill(scope: RuntimeSandboxScope, agent_id: str | None, sandbox_id: str) -> bool:
    """True when this agent (in this work) invoked a Skill loaded in ``sandbox_id``.

    Only such an agent's commands see that Skill as /skill and receive its
    credentials; another agent or conversation sharing the sandbox runs
    without them.
    """

    return await runtime_agent_skill_in(scope, agent_id, sandbox_id) is not None


async def runtime_agent_viewed_skill_in(
    scope: RuntimeSandboxScope, agent_id: str | None, sandbox_id: str,
) -> bool:
    """True when this agent's view names a Skill in ``sandbox_id``, live or not.

    Unlike ``runtime_agent_skill_in`` it answers for a sandbox already
    reclaimed: whether the agent's work there depended on a Skill.
    """

    view = await _safe_get(scope.agent_view_key(agent_id))
    entry = view.get("skill") if isinstance(view, dict) else None
    return isinstance(entry, dict) and str(entry.get("sandbox_id") or "") == sandbox_id


async def runtime_agent_skill_in(
    scope: RuntimeSandboxScope, agent_id: str | None, sandbox_id: str,
) -> dict[str, str] | None:
    """The Skill this agent drives in ``sandbox_id``: ``{"skill_id", "skill_key"}``, or None."""

    view = await _safe_get(scope.agent_view_key(agent_id))
    entry = view.get("skill") if isinstance(view, dict) else None
    if not isinstance(entry, dict) or str(entry.get("sandbox_id") or "") != sandbox_id:
        return None
    skill_id = str(entry.get("skill_id") or "")
    ctx = await runtime_load_sandbox_binding(scope, sandbox_id)
    if not skill_id or not runtime_binding_is_live(ctx, sandbox_id) or skill_id not in runtime_binding_skills(ctx):
        return None
    return {"skill_id": skill_id, "skill_key": str(entry.get("skill_key") or runtime_binding_skills(ctx)[skill_id])}


async def runtime_skill_sandbox(scope: RuntimeSandboxScope, skill_id: str) -> tuple[str, dict[str, Any]] | None:
    """Where this owner runs ``skill_id`` (any agent, any work), while it is loaded there."""

    if not skill_id:
        return None
    sandbox_id = _pointer_sandbox(await _safe_get(scope.key("skill", skill_id)))
    ctx = await runtime_load_sandbox_binding(scope, sandbox_id)
    if runtime_binding_is_live(ctx, sandbox_id) and skill_id in runtime_binding_skills(ctx):
        return sandbox_id, ctx
    return None


async def runtime_work_skill_sandbox(scope: RuntimeSandboxScope, skill_id: str) -> tuple[str, dict[str, Any]] | None:
    """Where THIS conversation (or task) opened ``skill_id``, while it is loaded there.

    Unlike ``runtime_skill_sandbox`` (the owner's Skill, opened by any of the
    user's conversations), this answers only for a Skill an agent of the
    scope's own work opened: conversation routing must never follow another
    conversation's Skill.
    """

    if not skill_id:
        return None
    sandbox_id = _pointer_sandbox(await _safe_get(scope.key("workskill", f"{scope.binding_key}|{skill_id}")))
    ctx = await runtime_load_sandbox_binding(scope, sandbox_id)
    if runtime_binding_is_live(ctx, sandbox_id) and skill_id in runtime_binding_skills(ctx):
        return sandbox_id, ctx
    return None


async def runtime_release_agent_sandbox(scope: RuntimeSandboxScope, agent_id: str | None, sandbox_id: str) -> bool:
    """Drop this agent's view of ``sandbox_id`` (its ``destroy``); the owner keeps the sandbox."""

    key = scope.agent_view_key(agent_id)
    view = await _safe_get(key)
    if not isinstance(view, dict):
        return False
    view = dict(view)
    changed = False
    skill = view.get("skill")
    if isinstance(skill, dict) and str(skill.get("sandbox_id") or "") == sandbox_id:
        view.pop("skill", None)
        changed = True
    if str(view.get("generic") or "") == sandbox_id:
        view.pop("generic", None)
        changed = True
    if not changed:
        return False
    if view:
        return await _cache_set(key, view)
    return await _cache_delete(key)


async def runtime_init_sandbox_binding(
    scope: RuntimeSandboxScope,
    sandbox_id: str,
    skill_id: str,
    *,
    agent_id: str | None = None,
    skill_key: str | None = None,
    preserve_pptx_checkpoint: bool = True,
    coexist: bool = True,
    holds_credentials: bool = False,
    credentials_owner_user_id: str | None = None,
) -> dict[str, Any]:
    """Bind ``sandbox_id`` to the owner for one acquiring agent.

    ``skill_id`` is the Skill just loaded; "" for a generic acquire, which
    leaves the Skills the shared sandbox holds alone. With ``coexist`` (a
    runner that keeps Skills side by side) a new Skill joins the ones already
    loaded; without (an older runner whose single /skill a load replaces) it
    replaces them. Re-binding a live sandbox is the normal continuation path:
    it never erases PPTX checkpoints, the recorded project path or one-read QA
    receipts. A new sandbox for a PPTX Skill restores the checkpoint the
    owner's previous PPTX sandbox recorded. Raises when the binding cannot be
    persisted: admission fails closed rather than hand out a sandbox nobody
    may operate.

    Credential provenance is sticky per sandbox: once a skill's stored
    credentials were injected into this sandbox (``holds_credentials``), the
    context keeps ``holds_credentials`` and every credentialed skill's owner
    (``credential_owner_user_ids``, '' for a skill without one) for as long
    as the same sandbox is bound, whatever is loaded into it later -- the
    container's files and processes may still carry them. See
    ``runtime_sandbox_credentials_refused``.
    """

    skill_id = str(skill_id or "")
    skill_key_value = str(skill_key or "").strip()
    agent_key = runtime_sandbox_agent_key(agent_id)
    now = time.time()
    existing = await runtime_load_sandbox_binding(scope, sandbox_id)
    live = runtime_binding_is_live(existing, sandbox_id)
    identity = {
        "sandbox_id": sandbox_id,
        "entity_id": scope.entity_id,
        "user_id": scope.user_id,
        "owner": scope.principal,
    }
    if scope.workspace_id:
        identity["owner_workspace_id"] = scope.workspace_id
    if live:
        ctx = dict(existing)
        ctx.update(identity)
        skills = runtime_binding_skills(existing)
        if skill_id:
            if not coexist and skill_id not in skills:
                # The older runner's load replaced /skill: only this Skill is there now.
                skills = {}
                for key in _PERSISTED_PPTX_CONTEXT_KEYS:
                    ctx.pop(key, None)
                ctx["exec_history"] = []
            skills[skill_id] = skill_key_value or skills.get(skill_id, "")
            ctx["skill_id"] = skill_id
            ctx["skill_key"] = skills[skill_id]
        ctx["skills"] = skills
        ctx["last_initialized_at"] = now
        ctx.setdefault("created_at", now)
        ctx.setdefault("exec_history", [])
    else:
        ctx = {
            **identity,
            "skill_id": skill_id,
            "skill_key": skill_key_value if skill_id else "",
            "skills": {skill_id: skill_key_value} if skill_id else {},
            "created_at": now,
            "created_by_agent": "",
            "agents": [],
            "exec_history": [],
        }
        if preserve_pptx_checkpoint and skill_id and skill_key_value in {"pptx", "slides"}:
            previous_id = _pointer_sandbox(await _safe_get(scope.key("skill", skill_id)))
            previous = await runtime_load_sandbox_binding(scope, previous_id) if previous_id else None
            if isinstance(previous, dict) and str(previous.get("pptx_checkpoint_rel_path") or "").strip():
                for key in _PERSISTED_PPTX_CONTEXT_KEYS:
                    if key in previous:
                        ctx[key] = previous[key]
                ctx["pptx_checkpoint_restored_from_sandbox"] = previous_id
    if not str(ctx.get("created_by_agent") or ""):
        ctx["created_by_agent"] = agent_key
    agents = [str(item) for item in ctx.get("agents") or [] if str(item)]
    if agent_key not in agents:
        agents.append(agent_key)
    ctx["agents"] = agents
    works = [str(item) for item in ctx.get("works") or [] if str(item)]
    if scope.binding_key not in works:
        works.append(scope.binding_key)
    ctx["works"] = works[-50:]
    prior = existing if live else {}
    owners = {str(value) for value in prior.get("credential_owner_user_ids") or ()}
    if holds_credentials:
        owners.add(str(credentials_owner_user_id or ""))
    ctx["holds_credentials"] = bool(holds_credentials or prior.get("holds_credentials"))
    ctx["credential_owner_user_ids"] = sorted(owners)
    if not await runtime_save_sandbox_binding(scope, ctx):
        raise RuntimeError("Sandbox owner context could not be persisted.")

    view_key = scope.agent_view_key(agent_id)
    view = await _safe_get(view_key)
    view = dict(view) if isinstance(view, dict) else {}
    if skill_id:
        view["skill"] = {"sandbox_id": sandbox_id, "skill_id": skill_id, "skill_key": skill_key_value}
        if not await _cache_set(scope.key("skill", skill_id), {"sandbox_id": sandbox_id}):
            raise RuntimeError("Sandbox owner context could not be persisted.")
        # Which Skills THIS conversation (or task) opened: another conversation
        # of the same user shares the container, not the conversation's state.
        if not await _cache_set(scope.key("workskill", f"{scope.binding_key}|{skill_id}"), {"sandbox_id": sandbox_id}):
            raise RuntimeError("Sandbox owner context could not be persisted.")
    else:
        view["generic"] = sandbox_id
    if not await _cache_set(view_key, view):
        raise RuntimeError("Sandbox owner context could not be persisted.")
    return ctx


async def runtime_sandbox_credentials_refused(
    ctx: dict[str, Any] | None,
    *,
    entity_id: str,
    user_id: str | None,
) -> bool:
    """Whether a call may not enter a sandbox holding a skill's stored credentials.

    Refused when an external collaborator acts (the run's user, or a
    collaborator steering the run) unless that one collaborator owns every
    credentialed skill loaded into the sandbox. The run drives the sandbox
    (exec, file reads), so entering it reads whatever credentials it holds.
    """
    if not isinstance(ctx, dict) or not ctx.get("holds_credentials"):
        return False
    from packages.core.services.collaborator_steering import collaborators_acting_now
    from packages.core.services.runtime_authorization.collaborator import is_runtime_collaborator

    acting = set(collaborators_acting_now(entity_id))
    if user_id and await is_runtime_collaborator(str(user_id), entity_id):
        acting.add(str(user_id))
    if not acting:
        return False
    owners = {str(value) for value in ctx.get("credential_owner_user_ids") or ()}
    return not (len(acting) == 1 and owners == acting)


def _pointer_sandbox(pointer: Any) -> str:
    return str(pointer.get("sandbox_id") or "") if isinstance(pointer, dict) else ""


# ── Legacy single-slot bindings ─────────────────────────────────
#
# Before bindings were scoped, a conversation (or task binding key) had ONE
# slot, ``sandbox:ctx:{binding_key}``, written with a 7-day TTL. Bindings the
# previous release wrote are still live, so a scope without a record for a
# sandbox adopts the legacy binding when it names that sandbox and was written
# for the same owner (entity + user). The acting agent is not part of
# ownership: the legacy agent becomes the record's creator and gets the view.
# The binding moves to the scoped keys, so it is read only once. A legacy
# binding for another owner is ignored. Nothing writes the legacy key any more.
#
# DELETE THIS SECTION (and its one call in ``runtime_load_sandbox_binding``)
# 7 days after the scoped-binding release is deployed: by then every legacy
# binding has expired.
_LEGACY_SANDBOX_CONTEXT_PREFIX = "sandbox:ctx:"


async def _adopt_legacy_sandbox_binding(scope: RuntimeSandboxScope, sandbox_id: str) -> dict[str, Any] | None:
    legacy_key = f"{_LEGACY_SANDBOX_CONTEXT_PREFIX}{scope.binding_key}"
    legacy = await _safe_get(legacy_key)
    if (
        not isinstance(legacy, dict)
        or str(legacy.get("sandbox_id") or "") != sandbox_id
        or str(legacy.get("entity_id") or "") != scope.entity_id
        or str(legacy.get("user_id") or "") != scope.user_id
    ):
        return None
    agent_key = runtime_sandbox_agent_key(legacy.get("agent_id"))
    record = {key: value for key, value in legacy.items() if key != "agent_id"}
    record.update({"owner": scope.principal, "created_by_agent": agent_key, "agents": [agent_key]})
    if not await runtime_save_sandbox_binding(scope, record):
        # Still this owner's binding: serve it and retry the move next read.
        return record
    view_key = scope.agent_view_key(legacy.get("agent_id"))
    view = await _safe_get(view_key)
    view = dict(view) if isinstance(view, dict) else {}
    skill_id = str(legacy.get("skill_id") or "")
    if skill_id:
        view["skill"] = {"sandbox_id": sandbox_id, "skill_id": skill_id, "skill_key": str(legacy.get("skill_key") or "")}
        await _cache_set(scope.key("skill", skill_id), {"sandbox_id": sandbox_id})
    else:
        view["generic"] = sandbox_id
    await _cache_set(view_key, view)
    await _cache_delete(legacy_key)
    logger.info("[runtime.sandbox] migrated legacy binding %s -> %s", legacy_key, scope.key("sbx", sandbox_id))
    return record
