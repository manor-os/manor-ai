"""Database-backed parts of the memory block: who is in the room, which Workspaces exist."""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from packages.core.ai.runtime.context_window import context_section
from packages.core.memory.layers import (
    MEMORY_BLOCK_HEADER, MemoryTurnScope, memory_layer_files, memory_scope_has_speaker_in_workspace,
    memory_turn_scope, personal_memory_path, read_layer_text, render_layer_file, workspace_charter_applies,
)

logger = logging.getLogger(__name__)

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _base_scope(ctx, *, collaborator: bool = False) -> MemoryTurnScope:
    return memory_turn_scope(
        getattr(ctx, "runtime_envelope", None),
        user_id=getattr(ctx, "user_id", None), workspace_id=getattr(ctx, "workspace_id", None),
        surface=getattr(ctx, "runtime_surface", None), profile=getattr(ctx, "runtime_profile", None),
        collaborator=collaborator,
    )


async def speaker_is_collaborator(db, *, entity_id: str, user_id: str) -> bool:
    """Whether the speaker is an external Workspace collaborator.

    Prompts are built before any tool call crosses the runtime gate, so the
    membership role decides. Without a session the answer is yes: organization
    memory stays out rather than leak.
    """
    if db is None:
        return True
    from packages.core.services.runtime_authorization.collaborator import collaborator_user_ids

    return user_id in await collaborator_user_ids(db, entity_id=entity_id, user_ids=[user_id])


async def turn_scope(ctx, db=None) -> MemoryTurnScope:
    scope = _base_scope(ctx)
    if not memory_scope_has_speaker_in_workspace(scope):
        return scope
    db = db if db is not None else getattr(ctx, "db", None)
    return _base_scope(ctx, collaborator=await speaker_is_collaborator(
        db, entity_id=ctx.entity_id, user_id=scope.speaker_id,
    ))


async def memory_layer_file_sections(ctx, *, per_speaker: bool, db=None, scope=None) -> list[dict]:
    """Layer file sections only (no header, no indexes): what a refresh reloads."""
    scope = scope or await turn_scope(ctx, db)
    stable, speaker = memory_layer_files(scope, entity_id=ctx.entity_id)
    files = speaker if per_speaker else stable
    return [s for s in (render_layer_file(ctx.entity_id, f) for f in files) if s]


async def memory_layer_sections(ctx, *, per_speaker: bool) -> list[dict]:
    """The memory block for one turn, in spec §4 order."""
    if not getattr(ctx, "entity_id", None):
        return []
    db = getattr(ctx, "db", None)
    scope = await turn_scope(ctx, db)
    if scope.external:
        return []
    files = await memory_layer_file_sections(ctx, per_speaker=per_speaker, scope=scope)
    if per_speaker:
        return files
    sections = [context_section("memory_layers", MEMORY_BLOCK_HEADER)] + files
    if db is None:
        return sections
    try:
        if scope.personal and getattr(ctx, "user", None) is not None:
            index = await workspace_index_section(db, entity_id=ctx.entity_id, user=ctx.user)
        elif scope.workspace_id and not scope.collaborator:
            index = await member_profile_index_section(db, entity_id=ctx.entity_id, workspace_id=scope.workspace_id)
        else:
            index = None
    except Exception:
        # An index is a pointer; the layers themselves still render.
        logger.warning("memory index failed for entity %s", ctx.entity_id, exc_info=True)
        index = None
    return sections + ([index] if index else [])


def speaker_preferences_reminder(ctx) -> str | None:
    """The speaker's preferences, to repeat after the turn's tool results.

    A long tool result puts the memory block far from the answer, and the
    answer then follows the document instead of the person.
    """
    for section in [*(getattr(ctx, "runtime_turn_context_sections", None) or []),
                    *(getattr(ctx, "runtime_context_sections", None) or [])]:
        if section.get("key") == "memory_layer:preferences" and section.get("content"):
            return ("Before you write the answer, apply the preferences of the person speaking "
                    "from memory. They still hold after the tool results above.\n\n" + section["content"])
    return None


async def member_profile_index_section(db, *, entity_id: str, workspace_id: str) -> dict | None:
    from sqlalchemy import select
    from packages.core.constants.agents import MANOR_AGENT_FS_ID
    from packages.core.models.user import User
    from packages.core.models.workspace import Workspace
    from packages.core.services.entity_fs import get_agent_dir
    from packages.core.services.workspace_access import user_can_read_workspace_by_identity

    users_dir = os.path.join(get_agent_dir(entity_id, MANOR_AGENT_FS_ID), "users")
    if not os.path.isdir(users_dir):
        return None
    with_profile = sorted(
        uid for uid in os.listdir(users_dir) if read_layer_text(personal_memory_path(entity_id, uid, "profile"))
    )
    if not with_profile:
        return None
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        return None
    users = {u.id: u for u in (await db.execute(
        select(User).where(User.id.in_(with_profile), User.entity_id == entity_id)
    )).scalars()}
    members = [uid for uid in with_profile if uid in users and await user_can_read_workspace_by_identity(
        db, workspace=workspace, entity_id=entity_id, user_id=uid, role=users[uid].role,
    )]
    if not members:
        return None

    def name(u) -> str:
        full = " ".join(filter(None, [getattr(u, "first_name", None), getattr(u, "last_name", None)]))
        return getattr(u, "display_name", None) or full or getattr(u, "email", None) or u.id

    lines = "\n".join(f"- {name(users[uid])} (user_id={uid})" for uid in members)
    return context_section(
        "memory_layer:member_index",
        "### Member profiles on file\nThese members of this Workspace have a profile card "
        "(role, contact details, LinkedIn). Read one with manor action read_memory, "
        "layer=profile, user_id=<id>.\n" + lines,
        source=f"memory_layer:{entity_id}:member_index:{workspace_id}",
    )


async def workspace_index_section(db, *, entity_id: str, user, limit: int = 30) -> dict | None:
    """The person's Workspaces for the outer chat's cached prefix.

    Recency picks which Workspaces are listed; they render by name with no live
    counts, so another member's message does not change the prefix.
    """
    from sqlalchemy import func, select
    from packages.core.models.task import Conversation
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_access import user_readable_workspace_ids

    ids = await user_readable_workspace_ids(
        db, entity_id=entity_id, user_id=user.id, role=getattr(user, "role", None),
    )
    if not ids:
        return None
    rows = (await db.execute(
        select(Workspace.id, Workspace.name, Workspace.description, Workspace.status)
        .where(Workspace.id.in_(ids))
    )).all()
    activity = dict((await db.execute(
        select(Conversation.workspace_id, func.max(Conversation.updated_at))
        .where(Conversation.entity_id == entity_id, Conversation.workspace_id.in_(ids))
        .group_by(Conversation.workspace_id)
    )).all())
    rows.sort(key=lambda row: ((activity.get(row.id) or _EPOCH).date(), row.id), reverse=True)
    shown = sorted(rows[:limit], key=lambda row: ((row.name or "").lower(), row.id))
    lines = []
    for row in shown:
        about = " ".join((row.description or "").split())[:140]
        lines.append(f"- {row.name} (workspace_id={row.id}; {row.status})" + (f": {about}" if about else ""))
    more = len(rows) - len(shown)
    body = (
        "### Your Workspaces\nYou are this person's main Manor AI. Each Workspace has its own AI, "
        "memory and work. Look into one, see its open decisions or arrange work in it with manor "
        "action workspace and its workspace_id; read its memory with manor action read_memory, "
        "layer=workspace, workspace_id=<id>.\n"
        + "\n".join(lines)
        + (f"\n{more} more: list them with manor action list_workspaces." if more > 0 else "")
    )
    return context_section("memory_layer:workspace_index", body,
                           source=f"memory_layer:{entity_id}:workspace_index:{user.id}")


def workspace_charter_text(ctx) -> str | None:
    """The master agent's role inside a Workspace, replacing its company-wide role.

    Only the role: an edited WORKSPACE.md stays in Workspace operating memory,
    which bounds it and reloads it on refresh.
    """
    if not workspace_charter_applies(_base_scope(ctx), getattr(ctx, "agent_id", None)):
        return None
    workspace = getattr(ctx, "workspace", None)
    name = getattr(workspace, "name", None)
    kind = getattr(workspace, "kind", None)
    purpose = (getattr(workspace, "primary_work", None) or getattr(workspace, "description", None) or "").strip()
    where = f"the {name} Workspace{f' ({kind})' if kind else ''}" if name else "this Workspace"
    parts = [
        "## Your Role in This Workspace",
        f"You are Manor AI working inside {where}. Answer and act for this Workspace. Work that "
        "belongs to another Workspace or to the whole organization goes through the person's main "
        "Manor AI chat.",
    ]
    if purpose:
        parts.append(f"Purpose: {purpose}")
    return "\n\n".join(parts)
