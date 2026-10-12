"""One file-reference contract for upload, read, generation and download.

A file the user can open has exactly one outward address, its ``open_url``:
``/viewer/<document_id>`` once it is a Knowledge Document, otherwise
``/api/v1/fs/<entity>/<percent-encoded path>``. Every producer returns the
same ``FileRef`` shape (:func:`file_ref`), every consumer accepts any address
a FileRef or an upload ever handed out (:func:`parse_file_reference`), and an
assistant reply may link a file only through an address that resolves to a
file this user can open (:func:`rewrite_reply_file_links`). Sandbox-internal
locations (``sandbox:``, ``file:``, ``/skill/``, ``/tmp/``, ``/mnt/``) are
never an address: a file there has not been delivered.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlsplit

from packages.core.services.generated_file_refs import (
    ArtifactReferenceFactory,
    canonical_document_id,
    canonical_fs_path,
    parse_entity_fs_url,
)

# Where a sandbox mounts the entity filesystem (read-only).
SANDBOX_ENTITY_MOUNT = "/workspace/"
_KNOWLEDGE_PREFIX = "Knowledge/"

_INTERNAL_TARGET_RE = re.compile(
    r"^(?:sandbox:|file:|/(?:skill|tmp|mnt)(?:/|$)|\$SANDBOX_OUTPUT_DIR|"
    r"(?:\$?sandbox[_-]output)(?:/|$))",
    re.I,
)
_CHAT_UPLOAD_RE = re.compile(r"uploads/chat/(?:[0-9a-f]{16}/)?[0-9a-f]{16}\.[A-Za-z0-9]+")


@dataclass(frozen=True)
class ParsedFileRef:
    """The identity an address names; existence and access are not implied."""

    entity_id: str = ""
    fs_path: str = ""
    document_id: str = ""


def is_internal_file_target(value: Any) -> bool:
    """A sandbox-internal location: never an address the user can open."""
    return bool(_INTERNAL_TARGET_RE.match(str(value or "").strip()))


def is_chat_upload_storage_path(path: Any) -> bool:
    return bool(_CHAT_UPLOAD_RE.fullmatch(str(path or "").strip().lstrip("/")))


def parse_file_reference(
    value: Any, *, entity_id: Any = None, allow_relative: bool = False,
) -> ParsedFileRef | None:
    """Decode any file address the platform hands out.

    Accepts ``/api/v1/fs/<entity>/<path>`` (absolute URLs too, decoded once),
    ``/viewer/<id>``, ``/documents/<id>``, ``/workspace/<rel>`` (the sandbox
    mount of the entity filesystem) and, with ``allow_relative`` (tool inputs),
    an entity-relative path with or without the ``Knowledge/`` prefix.
    """
    text = str(value or "").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    if not text or is_internal_file_target(text):
        return None
    parsed = urlsplit(text)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return None
    path = parsed.path or ""
    if path.startswith("/api/v1/fs/") or (not parsed.scheme and path.startswith("api/v1/fs/")):
        fs_parts = parse_entity_fs_url(text)
        return ParsedFileRef(entity_id=fs_parts[0], fs_path=fs_parts[1]) if fs_parts else None
    match = re.fullmatch(r"/(?:viewer|documents)/([^/?#]+)/?", path)
    if match:
        document_id = canonical_document_id(unquote(match[1]))
        return ParsedFileRef(entity_id=str(entity_id or ""), document_id=document_id) if document_id else None
    if parsed.scheme or parsed.netloc:
        return None
    if text.startswith(SANDBOX_ENTITY_MOUNT) or text.startswith(SANDBOX_ENTITY_MOUNT[1:]):
        fs_path = canonical_fs_path(text.split("workspace/", 1)[1])
        return ParsedFileRef(entity_id=str(entity_id or ""), fs_path=fs_path) if fs_path else None
    if not allow_relative or text.startswith("/"):
        return None
    if text.startswith(_KNOWLEDGE_PREFIX):
        text = text[len(_KNOWLEDGE_PREFIX):]
    fs_path = canonical_fs_path(text)
    return ParsedFileRef(entity_id=str(entity_id or ""), fs_path=fs_path) if fs_path else None


def is_open_address(value: Any) -> bool:
    """An fs URL or ``/viewer``/``/documents`` address (an ``open_url`` shape).

    A bare entity path, the sandbox mount and signed public URLs are not.
    """
    ref = parse_file_reference(value)
    return ref is not None and bool(ref.entity_id or ref.document_id)


def file_ref(
    *,
    entity_id: Any,
    fs_path: Any = None,
    document_id: Any = None,
    name: Any = None,
    mime: Any = None,
    size: Any = None,
    sha256: Any = None,
) -> dict[str, Any]:
    """The FileRef every producer returns for a file it wrote or registered.

    A link is advertised only for an address that can open: a Document, a
    chat upload, or a user-visible path. Run scratch (``tasks/``, ``tmp/`` …)
    gets a FileRef without ``open_url``/``markdown_link``.
    """
    from packages.core.services.knowledge_visibility import is_user_visible_path

    raw: dict[str, Any] = {}
    if fs_path:
        raw["fs_path"] = fs_path
    if document_id:
        raw["document_id"] = document_id
    path = canonical_fs_path(fs_path)
    raw["name"] = str(name or "").strip() or (os.path.basename(path) if path else "")
    ref = ArtifactReferenceFactory(entity_id=entity_id).create(raw)
    result: dict[str, Any] = {"name": ref.get("name") or ""}
    openable = bool(ref.get("document_id")) or (
        bool(path) and (is_user_visible_path(path) or is_chat_upload_storage_path(path))
    )
    for key in ("document_id", "fs_path", *(("open_url", "markdown_link") if openable else ())):
        if ref.get(key):
            result[key] = ref[key]
    if mime:
        result["mime"] = str(mime)
    if isinstance(size, int) and not isinstance(size, bool):
        result["size"] = size
    if sha256:
        result["sha256"] = str(sha256)
    return result


# ── Reply links ────────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)
# A destination may carry raw spaces (models write file names unencoded).
_MARKDOWN_LINK_RE = re.compile(r"(!?)\[((?:\\.|[^\]\\\n])*)\]\(\s*(<[^>\n]+>|[^)\n]+?)\s*\)")
_LINK_TITLE_RE = re.compile(r"\s+\"[^\"]*\"$")
_BARE_FILE_TARGET_RE = re.compile(
    r"(?<![\w(<\[/:])(?:sandbox:|file:)/[^\s)\]>]+"
    r"|(?<![\w(<\[/:.])/(?:api/v1/fs|viewer|skill|mnt)/[^\s)\]>\"'`]+"
)


_FILE_LINK_HINT_RE = re.compile(r"\]\(|sandbox:|file:/|/api/v1/fs/|/viewer/|/skill/|/mnt/")


@dataclass(frozen=True)
class ReplyFileLinks:
    text: str
    unresolved: tuple[str, ...]


FileRefResolve = Callable[[ParsedFileRef], Awaitable[dict[str, Any] | None]]


def _is_file_link_target(target: str) -> bool:
    return is_internal_file_target(target) or parse_file_reference(target) is not None


def _basename(target: str) -> str:
    path = urlsplit(target).path if "://" in target else target
    name = os.path.basename(unquote(path).rstrip("/"))
    return name or target


async def rewrite_reply_file_links(text: str, resolve: FileRefResolve) -> ReplyFileLinks:
    """Every file link in a reply points at an openable file, or is not a link.

    A file address that resolves (``resolve`` returns its FileRef) becomes that
    file's canonical ``open_url``; anything else — a sandbox location, a path
    or id the user cannot open — keeps its words and loses its link. Code spans
    are left as written.
    """
    if not text:
        return ReplyFileLinks(text, ())
    cache: dict[str, dict[str, Any] | None] = {}
    unresolved: list[str] = []

    async def resolved(target: str) -> dict[str, Any] | None:
        if target not in cache:
            parsed = parse_file_reference(target)
            cache[target] = await resolve(parsed) if parsed is not None else None
            if cache[target] is None:
                unresolved.append(target)
        return cache[target]

    async def rewrite_prose(segment: str) -> str:
        out: list[str] = []
        cursor = 0
        for match in _MARKDOWN_LINK_RE.finditer(segment):
            image, label = match[1], match[2]
            target = _LINK_TITLE_RE.sub("", match[3]).strip().strip("<>").strip()
            if not _is_file_link_target(target):
                continue
            out.append(await rewrite_bare(segment[cursor:match.start()]))
            ref = await resolved(target)
            if ref and ref.get("open_url"):
                out.append(f"{image}[{label}]({ref['open_url']})")
            else:
                out.append(label or _basename(target))
            cursor = match.end()
        out.append(await rewrite_bare(segment[cursor:]))
        return "".join(out)

    async def rewrite_bare(segment: str) -> str:
        out: list[str] = []
        cursor = 0
        for match in _BARE_FILE_TARGET_RE.finditer(segment):
            target = match[0].rstrip(".,;:!?。，；：！？")
            ref = await resolved(target)
            out.append(segment[cursor:match.start()])
            out.append(ref["markdown_link"] if ref and ref.get("markdown_link") else _basename(target))
            cursor = match.start() + len(target)
        out.append(segment[cursor:])
        return "".join(out)

    pieces: list[str] = []
    cursor = 0
    for code in _FENCE_RE.finditer(text):
        pieces.append(await rewrite_prose(text[cursor:code.start()]))
        pieces.append(code[0])
        cursor = code.end()
    pieces.append(await rewrite_prose(text[cursor:]))
    return ReplyFileLinks("".join(pieces), tuple(dict.fromkeys(unresolved)))


def strip_internal_file_links(text: str) -> str:
    """Earlier replies replayed to the model never carry a sandbox location.

    History may hold replies written before links were checked; a
    ``sandbox:``/``/skill/`` target copied from there would be presented as a
    delivered file again. Only internal targets are reduced to their words —
    no lookup, so the replayed history stays byte-stable for prompt caching.
    """
    if not text or not _FILE_LINK_HINT_RE.search(text):
        return text

    def prose(segment: str) -> str:
        def link(match: re.Match) -> str:
            target = _LINK_TITLE_RE.sub("", match[3]).strip().strip("<>").strip()
            if not is_internal_file_target(target):
                return match[0]
            return match[2] or _basename(target)

        segment = _MARKDOWN_LINK_RE.sub(link, segment)

        def bare(match: re.Match) -> str:
            target = match[0].rstrip(".,;:!?。，；：！？")
            if not is_internal_file_target(target):
                return match[0]
            return _basename(target) + match[0][len(target):]

        return _BARE_FILE_TARGET_RE.sub(bare, segment)

    pieces: list[str] = []
    cursor = 0
    for code in _FENCE_RE.finditer(text):
        pieces.append(prose(text[cursor:code.start()]))
        pieces.append(code[0])
        cursor = code.end()
    pieces.append(prose(text[cursor:]))
    return "".join(pieces)


# ── Resolution: does this user's turn have a file at that address? ────────

async def chat_upload_openable(db, *, entity_id: str, user_id: str | None, fs_path: str) -> bool:
    """An unprojected chat upload opens for the user who attached it."""
    from sqlalchemy import select

    from packages.core.models.task import Conversation, Message

    if not user_id or not is_chat_upload_storage_path(fs_path):
        return False
    message_id = await db.scalar(
        select(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.entity_id == entity_id,
            Conversation.user_id == user_id,
            Message.role == "user",
            Message.attachments.contains([{"kind": "chat_upload", "path": fs_path}]),
        )
        .limit(1)
    )
    return bool(message_id)


async def chat_upload_access(db, *, entity_id: str, user_id: str | None, fs_path: str) -> str | None:
    """How a chat upload under the hidden ``uploads/`` area may be opened.

    ``"document"``: it is projected as a Knowledge Document (Workspace Chat
    uploads) and opens under that Document's ACL. ``"uploader"``: it opens for
    the user who attached it. ``None``: not an openable chat upload.
    """
    from sqlalchemy import select

    from packages.core.models.document import Document

    if not is_chat_upload_storage_path(fs_path):
        return None
    projected = await db.scalar(select(Document.id).where(
        Document.entity_id == entity_id,
        Document.fs_path == fs_path,
        Document.is_trashed == False,  # noqa: E712
    ).limit(1))
    if projected:
        return "document"
    if await chat_upload_openable(db, entity_id=entity_id, user_id=user_id, fs_path=fs_path):
        return "uploader"
    return None


async def resolve_openable_file(
    db,
    parsed: ParsedFileRef,
    *,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None = None,
    system_actor: bool = False,
) -> dict[str, Any] | None:
    """The FileRef of an existing file this user may open, else ``None``.

    ``system_actor`` resolves for a user-less system Task under the system
    agent's Workspace rule; otherwise a user is required.
    """
    from sqlalchemy import select

    from packages.core.models.document import Document
    from packages.core.services.document_access import unreadable_document_paths
    from packages.core.services.entity_fs import get_entity_root
    from packages.core.services.knowledge_visibility import is_user_visible_path

    target_entity = parsed.entity_id or entity_id
    if (not user_id and not system_actor) or target_entity != entity_id:
        return None
    document_filter = (
        Document.id == parsed.document_id if parsed.document_id
        else Document.fs_path == parsed.fs_path
    )
    document = await db.scalar(select(Document).where(
        document_filter,
        Document.entity_id == entity_id,
        Document.is_trashed == False,  # noqa: E712
    ).limit(1)) if (parsed.document_id or parsed.fs_path) else None
    if parsed.document_id and (document is None or not document.fs_path):
        return None
    fs_path = canonical_fs_path(document.fs_path) if document is not None else parsed.fs_path
    if not fs_path:
        return None
    root = os.path.realpath(get_entity_root(entity_id))
    target = os.path.realpath(os.path.join(root, fs_path))
    if not target.startswith(root + os.sep) or not os.path.isfile(target):
        return None
    if document is None and is_chat_upload_storage_path(fs_path):
        if await chat_upload_openable(db, entity_id=entity_id, user_id=user_id, fs_path=fs_path):
            return file_ref(entity_id=entity_id, fs_path=fs_path)
        return None
    if document is None and not is_user_visible_path(fs_path):
        return None
    if await unreadable_document_paths(
        db, entity_id=entity_id, rel_paths=[fs_path], user_id=user_id, workspace_id=workspace_id,
        actor_type="user" if user_id else "agent",
    ):
        return None
    return file_ref(
        entity_id=entity_id, fs_path=fs_path,
        document_id=getattr(document, "id", None),
        name=getattr(document, "name", None),
    )


def openable_file_resolver(
    *, entity_id: str | None, user_id: str | None, workspace_id: str | None = None,
    system_actor: bool = False,
) -> FileRefResolve:
    """A resolver bound to one user's turn, opening its own DB session per call."""

    async def resolve(parsed: ParsedFileRef) -> dict[str, Any] | None:
        if not entity_id or (not user_id and not system_actor):
            return None
        from packages.core.database import async_session

        try:
            async with async_session() as db:
                return await resolve_openable_file(
                    db, parsed, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
                    system_actor=system_actor,
                )
        except Exception:
            # A lookup failure is not proof the file is missing, but a link
            # that cannot be checked is not delivered either.
            return None

    return resolve


async def enforce_reply_file_links(
    text: str, *, entity_id: str | None, user_id: str | None, workspace_id: str | None = None,
    system_actor: bool = False,
) -> str:
    """The reply as delivered: every file link opens for this user or is text.

    Applied where a reply leaves the runtime (chat, Agent DM, Workspace,
    Task and delegated results). A reply without a link-shaped token is
    returned untouched without any lookup.
    """
    if not text or not _FILE_LINK_HINT_RE.search(text):
        return text
    resolver = openable_file_resolver(
        entity_id=entity_id, user_id=user_id, workspace_id=workspace_id, system_actor=system_actor,
    )
    return (await rewrite_reply_file_links(text, resolver)).text


__all__ = [
    "ParsedFileRef",
    "ReplyFileLinks",
    "SANDBOX_ENTITY_MOUNT",
    "chat_upload_access",
    "chat_upload_openable",
    "enforce_reply_file_links",
    "file_ref",
    "is_chat_upload_storage_path",
    "is_internal_file_target",
    "is_open_address",
    "openable_file_resolver",
    "parse_file_reference",
    "resolve_openable_file",
    "rewrite_reply_file_links",
    "strip_internal_file_links",
]
