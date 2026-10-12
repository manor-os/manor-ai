"""Private Knowledge filing for files produced by an ordinary web chat."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.conversation import ConversationSurfaceKind
from packages.core.models.document import DocumentFolder
from packages.core.models.task import Conversation
from packages.core.services.conversation_surfaces import ConversationSurfaceMetadataFactory
from packages.core.services.knowledge_sync import ensure_folder_path
from packages.core.services.knowledge_visibility import is_user_visible_folder_path
from packages.core.services.workspace_layout import WorkspaceArtifactDir


_FOLDER_META_KEY = "generated_file_folder_id"
_ROOT_NAME = "Chat sessions"
_DEFAULT_STORAGE_DIRS = WorkspaceArtifactDir.values() | {"video", "subtitles"}


def is_default_chat_output_path(rel_path: str) -> bool:
    """Keep a caller's explicit destination folder outside chat auto-filing."""
    parent = PurePosixPath(rel_path.replace("\\", "/")).parent.as_posix()
    return parent == "." or parent in _DEFAULT_STORAGE_DIRS


def _folder_title(title: str | None) -> str:
    # Folder names are one logical Knowledge segment, not filesystem paths.
    clean = re.sub(r"[/\\\x00-\x1f\x7f]+", " ", title or "")
    name = re.sub(r"\s+", " ", clean).strip(" .")[:180] or "Untitled"
    return name if is_user_visible_folder_path(f"{_ROOT_NAME}/{name}") else f"Session {name}"


def _is_ordinary_chat(conv: Conversation, *, entity_id: str, user_id: str) -> bool:
    return (
        conv.entity_id == entity_id
        and conv.user_id == user_id
        and conv.workspace_id is None
        and conv.scope == "channel"
        and conv.channel == "web"
        and ConversationSurfaceMetadataFactory.matches(
            conv.meta, ConversationSurfaceKind.ORDINARY_CHAT,
        )
    )


async def _available_name(
    db: AsyncSession, *, entity_id: str, owner_id: str,
    parent_id: str, title: str, conversation_id: str,
    current_folder_id: str | None = None,
) -> str:
    existing = (await db.execute(select(DocumentFolder.id).where(
        DocumentFolder.entity_id == entity_id,
        DocumentFolder.owner_id == owner_id,
        DocumentFolder.parent_id == parent_id,
        DocumentFolder.name == title,
    ))).scalar_one_or_none()
    if existing is None or existing == current_folder_id:
        return title
    unique_name = f"{title} · {conversation_id}"
    collision = (await db.execute(select(DocumentFolder.id).where(
        DocumentFolder.entity_id == entity_id,
        DocumentFolder.owner_id == owner_id,
        DocumentFolder.parent_id == parent_id,
        DocumentFolder.name == unique_name,
    ))).scalar_one_or_none()
    if collision is not None and collision != current_folder_id:
        raise RuntimeError("Conversation Knowledge folder name is already in use")
    return unique_name


async def _linked_folder(
    db: AsyncSession, conv: Conversation, *, owner_id: str,
    lock: bool = True,
) -> DocumentFolder | None:
    meta = conv.meta if isinstance(conv.meta, dict) else {}
    folder_id = meta.get(_FOLDER_META_KEY)
    if not isinstance(folder_id, str) or not folder_id:
        return None
    query = select(DocumentFolder).where(
        DocumentFolder.id == folder_id,
        DocumentFolder.entity_id == conv.entity_id,
        DocumentFolder.owner_id == owner_id,
        DocumentFolder.visibility == "private",
        DocumentFolder.client_visible.is_(False),
    )
    folder = (await db.execute(query.with_for_update() if lock else query)).scalar_one_or_none()
    return folder


async def ensure_conversation_generated_folder(
    db: AsyncSession, *, entity_id: str, user_id: str,
    conversation_id: str,
) -> str | None:
    """Return this chat's stable private folder, or None for another surface."""
    conv = (await db.execute(select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.entity_id == entity_id,
        Conversation.user_id == user_id,
    ).with_for_update())).scalar_one_or_none()
    if conv is None or not _is_ordinary_chat(conv, entity_id=entity_id, user_id=user_id):
        return None

    root_id = await ensure_folder_path(entity_id, _ROOT_NAME, owner_id=user_id, db=db)
    if root_id is None:
        raise RuntimeError("Could not create Chat sessions Knowledge folder")
    # Serialize same-name sessions before selecting an available child name.
    locked_root_id = (await db.execute(select(DocumentFolder.id).where(
        DocumentFolder.id == root_id,
        DocumentFolder.entity_id == entity_id,
        DocumentFolder.owner_id == user_id,
    ).with_for_update())).scalar_one_or_none()
    if locked_root_id != root_id:
        raise RuntimeError("Chat sessions Knowledge folder is unavailable")
    folder = await _linked_folder(db, conv, owner_id=user_id)
    if folder is not None and folder.parent_id == root_id:
        return folder.id

    title = _folder_title(conv.title)
    name = await _available_name(
        db, entity_id=entity_id, owner_id=user_id, parent_id=root_id,
        title=title, conversation_id=conv.id,
    )
    folder_id = await ensure_folder_path(
        entity_id, f"{_ROOT_NAME}/{name}", owner_id=user_id, db=db,
    )
    if folder_id is None:
        raise RuntimeError("Could not create conversation Knowledge folder")
    conv.meta = {**(conv.meta if isinstance(conv.meta, dict) else {}), _FOLDER_META_KEY: folder_id}
    await db.flush()
    return folder_id


async def rename_conversation_generated_folder(
    db: AsyncSession, conv: Conversation,
) -> None:
    """Follow a user rename without creating a folder for chats without files."""
    if not conv.user_id or not _is_ordinary_chat(
        conv, entity_id=conv.entity_id, user_id=conv.user_id,
    ):
        return
    folder = await _linked_folder(db, conv, owner_id=conv.user_id, lock=False)
    if folder is None or folder.parent_id is None:
        return
    parent = (await db.execute(select(DocumentFolder).where(
        DocumentFolder.id == folder.parent_id,
        DocumentFolder.entity_id == conv.entity_id,
        DocumentFolder.owner_id == conv.user_id,
        DocumentFolder.name == _ROOT_NAME,
    ).with_for_update())).scalar_one_or_none()
    if parent is None:
        return
    folder = await _linked_folder(db, conv, owner_id=conv.user_id)
    if folder is None or folder.parent_id != parent.id:
        return
    title = _folder_title(conv.title)
    folder.name = await _available_name(
        db, entity_id=conv.entity_id, owner_id=conv.user_id,
        parent_id=parent.id, title=title, conversation_id=conv.id,
        current_folder_id=folder.id,
    )
    await db.flush()
