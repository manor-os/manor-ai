"""Layered memory: which memory files a turn may see, and how they render.

Visibility is decided here and only here: from the runtime surface, the
memory mounts, whether the speaker is an external collaborator, and the file
name. Visibility never depends on file content.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from packages.core.ai.runtime.memory import memory_mounts_from_envelope
from packages.core.ai.runtime.profiles import runtime_profile_is_external, runtime_surface_is_external
from packages.core.ai.runtime.surfaces import ChatSurface

MEMORY_LAYERS: tuple[str, ...] = ("org", "profile", "preferences", "private", "workspace")

SOFT_LIMITS: dict[str, int] = {
    "org": 3000, "profile": 1000, "preferences": 2000, "private": 2000, "workspace": 6000,
}
_PERSONAL_FILES = {"profile": "PROFILE.md", "preferences": "PREFERENCES.md", "private": "PRIVATE.md"}
_TITLES = {
    "org": "Organization",
    "profile": "Profile of the person speaking",
    "preferences": "Preferences of the person speaking",
    "private": "Private notes (this person's own chat only)",
    "workspace": "This Workspace",
}

# A person wrote the current message. Workflow steps and scheduled runs have
# no speaker, so no personal layer applies to them.
_SPEAKER_SURFACES = frozenset({
    ChatSurface.GLOBAL_OWNER_CHAT, ChatSurface.AGENT_DM, ChatSurface.WORKSPACE_CHAT,
    ChatSurface.VOICE_CHAT, ChatSurface.FILE_EDITOR_CHAT, ChatSurface.TASK_COMMENT_THREAD,
    ChatSurface.WORKSPACE_DRAFT_ARCHITECT,
})
# Without an envelope there are no mounts: only these surfaces are a person's own chat.
_OWN_CHAT_SURFACES = frozenset({ChatSurface.GLOBAL_OWNER_CHAT, ChatSurface.AGENT_DM})

MEMORY_BLOCK_HEADER = (
    "## Memory\n"
    "What follows is what you were told in earlier conversations. When two layers disagree, "
    "the more specific one wins: this Workspace, then the speaker's preferences, then the "
    "organization. A past decision that is not here may still be in chat history: search it "
    "with manor action search_conversation_messages before saying there is no record."
)


@dataclass(frozen=True)
class MemoryTurnScope:
    external: bool
    personal: bool  # the speaker's own chat (owner mount)
    speaker_id: str | None
    workspace_id: str | None
    collaborator: bool = False  # the speaker is an external Workspace collaborator


@dataclass(frozen=True)
class MemoryLayerFile:
    layer: str
    path: str
    owner_id: str | None = None  # user id for personal layers, Workspace id for workspace


def _surface(value) -> ChatSurface | None:
    try:
        return ChatSurface(str(getattr(value, "value", value) or "").strip())
    except ValueError:
        return None


def memory_turn_scope(
    envelope, *, user_id: str | None, workspace_id: str | None = None,
    surface=None, profile=None, collaborator: bool = False,
) -> MemoryTurnScope:
    """Resolve who is speaking and which memory mounts this turn has.

    Without an envelope the context's surface and profile decide, and an
    unknown or missing surface gets no personal layer: external channels
    build their base prompt this way.
    """
    user = str(user_id or "").strip() or None
    if envelope is None:
        kind = _surface(surface)
        if runtime_surface_is_external(kind) or runtime_profile_is_external(profile):
            return MemoryTurnScope(True, False, None, None)
        workspace = str(workspace_id or "").strip() or None
        speaker = user if kind in _SPEAKER_SURFACES else None
        personal = bool(speaker) and kind in _OWN_CHAT_SURFACES and not workspace
        return MemoryTurnScope(False, personal, speaker, workspace, collaborator and not personal)
    kind = _surface(getattr(envelope, "surface", None))
    if runtime_surface_is_external(kind):
        return MemoryTurnScope(True, False, None, None)
    mounts = [mount for mount in memory_mounts_from_envelope(envelope) if mount.readable]
    speaker = user if kind in _SPEAKER_SURFACES else None
    workspace = next(
        (str(m.key).split(":", 1)[1] for m in mounts if str(m.key).startswith("workspace:")), None,
    )
    personal = bool(speaker) and any(m.scope == "owner" and m.key == f"user:{speaker}" for m in mounts)
    return MemoryTurnScope(False, personal, speaker, workspace, collaborator and not personal)


def memory_scope_has_speaker_in_workspace(scope: MemoryTurnScope) -> bool:
    """Whether the speaker's collaborator status matters for this turn."""
    return not scope.external and not scope.personal and bool(scope.workspace_id and scope.speaker_id)


def workspace_charter_applies(scope: MemoryTurnScope, agent_id: str | None) -> bool:
    """The master agent in a Workspace turn other than the owner's own chat."""
    from packages.core.constants.agents import MANOR_AGENT_FS_ID
    from packages.core.services.agent_files import effective_agent_id

    return (not scope.external and not scope.personal and bool(scope.workspace_id)
            and effective_agent_id(agent_id) == MANOR_AGENT_FS_ID)


def org_memory_path(entity_id: str) -> str:
    from packages.core.services.entity_fs import get_entity_root

    return os.path.join(get_entity_root(entity_id), ".ai", "memory", "ORG.md")


def personal_memory_path(entity_id: str, user_id: str, layer: str) -> str:
    from packages.core.constants.agents import MANOR_AGENT_FS_ID
    from packages.core.services.agent_files import agent_user_dir

    return os.path.join(agent_user_dir(entity_id, MANOR_AGENT_FS_ID, user_id), _PERSONAL_FILES[layer])


def workspace_memory_path(entity_id: str, workspace_id: str) -> str:
    from packages.core.memory.paths import workspace_memory_root

    return os.path.join(workspace_memory_root(entity_id, workspace_id), "MEMORY.md")


def memory_layer_files(
    scope: MemoryTurnScope, *, entity_id: str,
) -> tuple[list[MemoryLayerFile], list[MemoryLayerFile]]:
    """Return (stable, per-speaker) layer files in render order (spec §4)."""
    if scope.external or not entity_id:
        return [], []
    stable: list[MemoryLayerFile] = []
    speaker: list[MemoryLayerFile] = []
    if not scope.collaborator:
        stable.append(MemoryLayerFile("org", org_memory_path(entity_id)))
    if scope.workspace_id:
        stable.append(MemoryLayerFile(
            "workspace", workspace_memory_path(entity_id, scope.workspace_id), scope.workspace_id,
        ))
    if scope.speaker_id:
        mine = lambda layer: MemoryLayerFile(  # noqa: E731
            layer, personal_memory_path(entity_id, scope.speaker_id, layer), scope.speaker_id,
        )
        if scope.personal:
            # One person per conversation: their layers belong to the cached prefix.
            stable.extend(mine(layer) for layer in ("profile", "preferences", "private"))
        else:
            speaker.extend(mine(layer) for layer in ("profile", "preferences"))
    return stable, speaker


def read_layer_text(path: str) -> str:
    from packages.core.memory.cache import read_memory_text

    return (read_memory_text(path) or "").strip()


def _read_hint(file: MemoryLayerFile) -> str:
    target = "workspace_id" if file.layer == "workspace" else "user_id" if file.layer in _PERSONAL_FILES else None
    extra = f", {target}={file.owner_id}" if target and file.owner_id else ""
    return f"manor action read_memory with layer={file.layer}{extra}"


def render_layer_file(entity_id: str, file: MemoryLayerFile) -> dict | None:
    """One context section for a non-empty layer file, cut at its soft limit."""
    from packages.core.ai.runtime.context_window import context_section

    text = read_layer_text(file.path)
    if file.layer == "workspace":
        # Every existing Workspace was seeded with a MEMORY.md template.
        from packages.core.memory.canonical import workspace_memory_text

        text = workspace_memory_text(text)
    if not text:
        return None
    limit = SOFT_LIMITS[file.layer]
    if len(text) > limit:
        text = (text[:limit].rstrip()
                + f"\n\n(Shortened at {limit} characters. Read the rest with {_read_hint(file)}.)")
    return context_section(
        f"memory_layer:{file.layer}", f"### {_TITLES[file.layer]}\n{text}",
        category="memory_files",
        source=f"memory_layer:{entity_id}:{file.layer}:{file.owner_id or ''}",
    )


async def user_reads_workspace(db, *, entity_id: str, workspace_id: str, user_id: str) -> bool:
    from sqlalchemy import select
    from packages.core.models.user import User
    from packages.core.services.workspace_access import user_can_read_workspace_id

    return await user_can_read_workspace_id(
        db, workspace_id=workspace_id, entity_id=entity_id, user_id=user_id,
        role=await db.scalar(select(User.role).where(User.id == user_id)),
    )


async def read_memory_layer(
    db, scope: MemoryTurnScope, *, entity_id: str, layer: str,
    workspace_id: str | None = None, user_id: str | None = None,
) -> dict:
    """Return one layer file this turn may read, or ``{"error": ...}``."""
    def refused(message: str) -> dict:
        return {"error": message, "layer": layer}

    if scope.external:
        return refused("Memory is not available in this conversation.")
    if layer == "org":
        if scope.collaborator:
            return refused("Organization memory is not shared with external collaborators.")
        file = MemoryLayerFile("org", org_memory_path(entity_id))
    elif layer == "workspace":
        target = str(workspace_id or scope.workspace_id or "").strip()
        if not target:
            return refused("Pass params.workspace_id.")
        if target != scope.workspace_id and not (
            scope.personal and await user_reads_workspace(
                db, entity_id=entity_id, workspace_id=target, user_id=scope.speaker_id,
            )
        ):
            return refused("That Workspace's memory cannot be read from this conversation.")
        file = MemoryLayerFile("workspace", workspace_memory_path(entity_id, target), target)
    elif layer in _PERSONAL_FILES:
        target = str(user_id or scope.speaker_id or "").strip()
        if not target:
            return refused("Pass params.user_id.")
        own = scope.speaker_id is not None and target == scope.speaker_id
        if layer == "private" and not (own and scope.personal):
            return refused("Private notes are read only in their owner's own chat.")
        if layer == "preferences" and not own:
            return refused("Preferences are read only by the person they belong to.")
        if layer == "profile" and not own and (scope.collaborator or not (
            scope.workspace_id and await user_reads_workspace(
                db, entity_id=entity_id, workspace_id=scope.workspace_id, user_id=target,
            )
        )):
            return refused("Profiles are shared only with members of the same Workspace.")
        file = MemoryLayerFile(layer, personal_memory_path(entity_id, target, layer), target)
    else:
        return refused(f"Unknown layer. Use one of: {', '.join(MEMORY_LAYERS)}.")
    content = read_layer_text(file.path)
    if layer == "workspace":
        from packages.core.memory.canonical import workspace_memory_text

        content = workspace_memory_text(content)
    return {"layer": layer, "content": content, "chars": len(content), "empty": not content}
