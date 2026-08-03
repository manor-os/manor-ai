"""Who acted on a task.

Every task-log row answers "what happened". Until now it answered "who did
it" with a free-text ``created_by`` string, and each writer invented its own
spelling: a specific agent arrived as its display name, the master agent as
"Manor AI", the plan supervisor as "AI Supervisor", the executor's own
summary as "AI Agent", a task an agent created as "ai-agent", a workspace
reply with no resolved agent as "workspace-agent", a person as a ULID or a
display name or an email local-part, and the platform as "system".

A reader cannot tell those apart without guessing, which is how
"workspace-agent" ended up rendered as a person with an initials avatar.

``TaskActor`` is the closed set of things that can act on a task. It is
stamped into the log's metadata at write time, so reading it back is a lookup
rather than an inference. The free-text ``created_by`` stays as the display
name — it is what to *call* the actor, never what *kind* of actor it is.
"""
from __future__ import annotations

from enum import Enum


class TaskActor(str, Enum):
    """The kind of actor that produced a task log entry."""

    #: A person — the assignee, the workspace owner, a teammate.
    USER = "user"

    #: A specific workspace agent, identified by its subscription. This is the
    #: "which agent ran this step" case; the display name is the agent's own.
    AGENT = "agent"

    #: The Manor master agent acting as the workspace agent — the default
    #: responder when no specific agent owns the work.
    MANOR = "manor"

    #: The plan supervisor: the pass that judges step results and decides
    #: whether a plan is done, needs replanning, or needs a human. It is not
    #: the agent that executed the work, and conflating the two makes a
    #: supervisor's verdict look like the worker's own report.
    SUPERVISOR = "supervisor"

    #: The platform itself — state machine transitions, schedulers, reminders,
    #: retries. No agent reasoned about these.
    SYSTEM = "system"

    #: An external portal client commenting on their own request.
    CLIENT = "client"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


#: Metadata key carrying the actor kind on a TaskLog.
TASK_ACTOR_META_KEY = "actor_kind"


def task_actor_meta(
    actor: TaskActor,
    *,
    metadata: dict | None = None,
) -> dict:
    """Merge ``actor`` into a task-log metadata dict.

    The kind is authoritative: a caller that already put an ``actor_kind`` in
    its metadata gets it replaced, because the parameter is the one the call
    site declared.
    """
    merged = dict(metadata or {})
    merged[TASK_ACTOR_META_KEY] = TaskActor(actor).value
    return merged


def task_actor_from_meta(metadata: dict | None) -> TaskActor | None:
    """Read the actor kind back, or None for rows written before this existed."""
    raw = (metadata or {}).get(TASK_ACTOR_META_KEY)
    if not raw:
        return None
    try:
        return TaskActor(str(raw))
    except ValueError:
        return None
