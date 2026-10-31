"""AI file mutation permission gates.

This protects user-visible Knowledge files from agent/tool writes. Internal
system paths such as .ai/** remain writable for agent memory/workspace state.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from packages.core.ai.runtime.file_contracts import (
    FileApprovalOperation,
    FileApprovalOperationFactory,
    FileMutationAction,
    FilePermissionMode,
    FileResourceAction,
)
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Message
from packages.core.models.user import User
from packages.core.services.hitl_options import (
    APPROVAL_CHOICE_ALWAYS_APPROVE,
    APPROVAL_CHOICE_APPROVE,
    APPROVAL_CHOICE_REJECT,
    approval_options,
    normalize_approval_choice,
)
from packages.core.services.knowledge_visibility import is_user_visible_path, normalize_rel_path

logger = logging.getLogger(__name__)

FILE_PERMISSION_PREF_KEY = "ai_file_permission"
DEFAULT_FILE_PERMISSION = FilePermissionMode.APPROVAL.value

_ALLOW_ALIASES = {"always_approve", "always approve", "always_approval", "always approval", "allow", "allowed"}
_APPROVAL_ALIASES = {"approval", "approve", "ask", "ask_each_time", "ask each time", "manual"}
_DENY_ALIASES = {"deny", "denied", "never", "block", "blocked"}
_NEGATIVE_REPLY_HINTS = {
    "no", "n", "reject", "rejected", "deny", "decline", "cancel", "stop",
    "dont", "don't", "do not",
    "不", "不是", "否", "不要", "不用", "取消", "拒绝", "别", "别删", "不删除",
}
_ALWAYS_REPLY_HINTS = {
    "alwaysapprove", "approvealways", "alwaysallow", "allowalways",
    "以后都允许", "总是允许", "一直允许", "始终允许", "永久允许",
}
_AFFIRMATIVE_REPLY_HINTS = {
    "yes", "y", "ok", "okay", "sure", "approve", "approved", "accept",
    "confirm", "confirmed", "goahead", "doit", "proceed", "continue",
    "是", "是的", "对", "对的", "可以", "确认", "确定", "同意", "批准",
    "继续", "好", "好的", "删吧", "删除吧",
}
_ACTION_TERMS = {
    "delete": {"delete", "remove", "trash", "删除", "移除", "删掉"},
    "write": {"write", "create", "save", "生成", "写入", "创建", "保存"},
    "edit": {"edit", "modify", "update", "编辑", "修改", "更新"},
    "create_document": {"create", "generate", "save", "生成", "创建", "保存"},
    "create_code_bundle": {"create", "generate", "save", "生成", "创建", "保存"},
    "upload_document": {"upload", "save", "上传", "保存"},
    "save_file": {"save", "write", "保存", "写入"},
    "shell_modify": {"run", "execute", "modify", "执行", "运行", "修改"},
}
_CONFIRMATION_TERMS = {"confirm", "sure", "approve", "permission", "allow", "确认", "确定", "是否", "吗", "允许", "批准"}


def normalize_file_permission_mode(value: Any) -> str:
    """Return one of approval | always_approve | deny."""
    if isinstance(value, dict):
        value = value.get("mode") or value.get("policy")
    raw = str(value or DEFAULT_FILE_PERMISSION).strip().lower().replace("-", "_")
    raw_space = raw.replace("_", " ")
    if raw in _ALLOW_ALIASES or raw_space in _ALLOW_ALIASES:
        return FilePermissionMode.ALWAYS_APPROVE.value
    if raw in _DENY_ALIASES or raw_space in _DENY_ALIASES:
        return FilePermissionMode.DENY.value
    if raw in _APPROVAL_ALIASES or raw_space in _APPROVAL_ALIASES:
        return FilePermissionMode.APPROVAL.value
    return DEFAULT_FILE_PERMISSION


def classify_file_approval_reply(message: str) -> str | None:
    """Classify a short human reply as approve / always_approve / reject.

    This is intentionally conservative and only meant for direct replies like
    "yes", "是的", "approve always", not arbitrary natural-language plans.
    """
    raw = str(message or "").strip().lower()
    if not raw:
        return None
    compact = re.sub(r"[\s。.!！?？,，；;：:\"'“”‘’、]+", "", raw)
    if not compact or len(compact) > 24:
        return None
    if compact in _NEGATIVE_REPLY_HINTS or _has_reply_hint(raw, compact, _NEGATIVE_REPLY_HINTS):
        return "reject"
    # Always-hints are long compact phrases ("alwaysapprove"): no word inside them misreads.
    if compact in _ALWAYS_REPLY_HINTS or any(h in compact for h in _ALWAYS_REPLY_HINTS):
        return "always_approve"
    if compact in _AFFIRMATIVE_REPLY_HINTS or _has_reply_hint(raw, compact, _AFFIRMATIVE_REPLY_HINTS):
        return "approve"
    return None


def _has_reply_hint(raw: str, compact: str, hints: set[str]) -> bool:
    """A hint inside a longer reply: English as a whole word, Chinese as text.

    An English hint matched as a substring read "Knowledge" as "no", so a
    request mentioning Knowledge rejected the pending card.
    """
    for hint in hints:
        if len(hint) < 2:
            continue
        if hint.isascii():
            if re.search(rf"(?<![a-z0-9']){re.escape(hint)}(?![a-z0-9'])", raw):
                return True
        elif hint in compact:
            return True
    return False


def visible_user_paths(paths: Iterable[str]) -> list[str]:
    out: list[str] = []
    for path in paths:
        raw = str(path or "").strip()
        # "." is used by conservative command analysis to mean "this command
        # may mutate the visible Knowledge root". normalize_rel_path turns it
        # into "", so preserve it as an approval target.
        if raw in {".", "./", "/"}:
            out.append(".")
            continue
        rel = normalize_rel_path(raw)
        if rel and is_user_visible_path(rel):
            out.append(rel)
    return out


def canonical_visible_user_paths(entity_id: str, paths: Iterable[str]) -> list[str]:
    """Return visible paths only when their filesystem identity is canonical.

    Approval and ACL checks must describe the path that will actually be
    committed. Existing symlinks, or missing children below a symlinked
    parent, otherwise let one displayed path authorize a different resource.
    """
    visible_paths = visible_user_paths(paths)
    _require_canonical_file_paths(entity_id, visible_paths)
    return visible_paths


def _require_canonical_file_paths(entity_id: str, visible_paths: list[str]) -> None:
    from packages.core.services.entity_fs import canonical_entity_root

    if not visible_paths:
        return
    root = os.path.abspath(canonical_entity_root(entity_id))
    if os.path.lexists(root) and os.path.realpath(root) != root:
        raise ValueError("Entity filesystem root may not be a filesystem alias")
    for rel_path in visible_paths:
        if rel_path == ".":
            continue
        lexical_target = os.path.abspath(os.path.join(root, rel_path))
        resolved_target = os.path.realpath(lexical_target)
        try:
            inside_root = os.path.commonpath([root, lexical_target]) == root
            resolved_inside_root = os.path.commonpath([root, resolved_target]) == root
        except ValueError:
            inside_root = resolved_inside_root = False
        canonical_rel = normalize_rel_path(os.path.relpath(resolved_target, root))
        if (
            not inside_root
            or not resolved_inside_root
            or canonical_rel != normalize_rel_path(rel_path)
        ):
            raise ValueError(f"File path traverses a filesystem alias: {rel_path}")


def is_chat_upload_path(path: str) -> bool:
    """Recognize legacy and scope-hashed upload references, not read grants."""
    return bool(re.fullmatch(r"uploads/chat/(?:[0-9a-f]{16}/)?[0-9a-f]{16}\.[a-zA-Z0-9]+", path))


async def _chat_upload_readable(
    db, *, entity_id: str, user_id: str | None, conversation_id: str | None,
    workspace_id: str | None, path: str,
) -> bool:
    """Authorize an exact attachment or its readable Workspace projection."""
    from packages.core.models.document import Document
    from packages.core.services.document_access import (
        DocumentAccessContext, document_workspace_ids, unreadable_document_paths,
    )

    if not user_id or not conversation_id:
        return False
    conversation = await db.scalar(select(Conversation).where(
        Conversation.id == conversation_id,
        Conversation.entity_id == entity_id,
        Conversation.status == "active",
    ))
    if conversation is None or conversation.workspace_id != workspace_id:
        return False
    if conversation.scope not in {"channel", "workspace_main", "workspace_thread"}:
        return False
    if conversation.scope == "channel" and conversation.user_id != user_id:
        return False
    if conversation.scope != "channel" and not workspace_id:
        return False
    ctx = await DocumentAccessContext.load(db, entity_id=entity_id, user_id=user_id)
    if not ctx.identity_active:
        return False
    if workspace_id and not await ctx.workspace_readable(db, workspace_id):
        return False
    attachment_message = await db.scalar(select(Message.id).where(
        Message.conversation_id == conversation_id,
        Message.role == "user",
        Message.author_kind == "user",
        Message.attachments.contains([{"kind": "chat_upload", "path": path}]),
    ).limit(1))
    if not attachment_message:
        # Task threads do not repeat their Workspace's original upload message.
        # Require a durable projection into this exact Workspace before using
        # normal Knowledge ACLs; mere filesystem presence grants nothing.
        projections = list((await db.scalars(select(Document).where(
            Document.entity_id == entity_id, Document.fs_path == path,
            Document.source == "chat_upload", Document.is_trashed == False,  # noqa: E712
        ))).all()) if workspace_id else []
        for document in projections:
            if workspace_id in await document_workspace_ids(db, document):
                break
        else:
            return False
    # A persisted attachment reference cannot resurrect a deleted/restricted
    # Knowledge projection or bypass a subsequently revoked Document grant.
    document_states = list((await db.scalars(select(Document.is_trashed).where(
        Document.entity_id == entity_id, Document.fs_path == path,
    ))).all())
    if document_states:
        if all(document_states):
            return False
        return not await unreadable_document_paths(
            db, entity_id=entity_id, rel_paths=[path], user_id=user_id,
            workspace_id=workspace_id, actor_type="agent",
        )
    return True


def _visible_path_exists(entity_id: str, path: str) -> bool | None:
    from packages.core.ai.runtime.file_actions import runtime_entity_file_root

    entity_root = runtime_entity_file_root(entity_id)
    if not entity_root:
        return None
    root = os.path.realpath(entity_root)
    target = os.path.realpath(os.path.join(root, path.lstrip("/")))
    if target != root and not target.startswith(root + os.sep):
        return None
    return os.path.exists(target)


def _precise_workspace_file_action_key(
    *,
    entity_id: str,
    action: str,
    paths: Iterable[str],
) -> str:
    normalized_action = str(action or "").strip().lower().replace("-", "_")
    if normalized_action in {"delete", "remove"}:
        return "workspace.file.delete"
    if any(
        marker in normalized_action
        for marker in ("edit", "modify", "overwrite", "update", "append")
    ):
        return "workspace.file.modify"
    existence = tuple(_visible_path_exists(entity_id, path) for path in paths)
    if any(item is True for item in existence):
        return "workspace.file.modify"
    if existence and all(item is False for item in existence):
        return "workspace.file.create"
    if "create" in normalized_action or "generate" in normalized_action:
        return "workspace.file.create"
    return "workspace.file.write"


def _display_action_label(action: str) -> str:
    labels = {
        "create_document": "create file",
        "create_code_bundle": "create code files",
        "upload_document": "save file",
        "save_file": "save file",
        "write": "create file",
        "edit": "update file",
        "update": "update file",
        "delete": "delete file",
        "shell_modify": "run a command that may modify files",
    }
    normalized = str(action or "change").strip().lower().replace("-", "_").replace(" ", "_")
    return labels.get(normalized, normalized.replace("_", " ") or "change files")


def _display_path_label(path: str) -> str:
    raw = str(path or "").strip()
    if raw in {".", "./", "/"}:
        return "Knowledge root"
    return raw


async def load_user_file_permission_mode(db, user_id: str | None) -> str:
    raise ValueError("File approval preferences were replaced by unified approval settings")


def _approval_prompt(*, action: str, tool_name: str, paths: list[str], mode: str) -> str:
    joined = ", ".join(_display_path_label(path) for path in paths[:5])
    if len(paths) > 5:
        joined += f", +{len(paths) - 5} more"
    return f"Allow Manor to {_display_action_label(action)} {joined}?"


def _approval_content_preview(
    *,
    action: str,
    tool_name: str,
    paths: list[str],
    content_preview: Any = None,
) -> str:
    if isinstance(content_preview, str):
        text = content_preview.strip()
    elif content_preview is not None:
        try:
            text = json.dumps(content_preview, ensure_ascii=False, default=str, indent=2)
        except Exception:
            text = str(content_preview).strip()
    else:
        text = ""
    if not text:
        text = json.dumps({
            "tool": tool_name,
            "action": action,
            "paths": paths,
        }, ensure_ascii=False, default=str, indent=2)
    if len(text) > 1800:
        text = text[:1800] + "\n..."
    return text


def _hitl_payload(
    hitl_id: str,
    *,
    action: str,
    tool_name: str,
    paths: list[str],
    mode: str,
    content_preview: Any = None,
) -> str:
    prompt = _approval_prompt(action=action, tool_name=tool_name, paths=paths, mode=mode)
    content = _approval_content_preview(
        action=action,
        tool_name=tool_name,
        paths=paths,
        content_preview=content_preview,
    )
    return json.dumps({
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": hitl_id,
        "hitl": {
            "id": hitl_id,
            "type": "approval",
            "prompt": prompt,
            "action": action,
            "tool": tool_name,
            "paths": paths,
            "content": content,
            "options": approval_options(),
        },
        "message": (
            "User approval is required before changing user-visible files. "
            "Do not retry this operation until the user approves. "
            f"If the user approves, retry the same tool call with approval_token='{hitl_id}'."
        ),
        "operation": {"tool": tool_name, "action": action, "paths": paths},
    }, ensure_ascii=False)


def _path_was_mentioned(text: str, paths: list[str]) -> bool:
    lowered = str(text or "").lower()
    for rel in paths:
        if rel == ".":
            return False
        normalized = normalize_rel_path(rel)
        basename = os.path.basename(normalized)
        stem = os.path.splitext(basename)[0]
        candidates = {normalized, basename, stem}
        for candidate in candidates:
            if candidate and len(candidate) >= 3 and candidate.lower() in lowered:
                return True
    return False


def _assistant_confirmation_matches_operation(text: str, *, action: str, paths: list[str]) -> bool:
    lowered = str(text or "").lower()
    if not any(term in lowered for term in _CONFIRMATION_TERMS):
        return False
    action_terms = _ACTION_TERMS.get(action, {action})
    if not any(term in lowered for term in action_terms):
        return False
    return _path_was_mentioned(lowered, paths)


async def _latest_user_confirmation_allows_operation(
    db,
    *,
    conv: Conversation,
    user_id: str | None,
    operation: FileApprovalOperation,
) -> bool:
    """Consume a matching pending operation after an unambiguous short reply.

    Assistant prose alone is not authority. A durable pending approval with
    the exact payload fingerprint must already exist in conversation state.
    """
    paths = list(operation.paths)
    action = operation.action.value
    if not user_id or not paths or "." in paths:
        return False
    rows = await db.execute(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc())
        .limit(8)
    )
    recent = list(rows.scalars().all())
    latest_user_idx = next(
        (idx for idx, msg in enumerate(recent) if msg.role == "user" and msg.content),
        None,
    )
    if latest_user_idx is None:
        return False
    latest_user = recent[latest_user_idx]
    if classify_file_approval_reply(latest_user.content or "") != "approve":
        return False
    previous_assistant = next(
        (msg for msg in recent[latest_user_idx + 1:] if msg.role == "assistant" and msg.content),
        None,
    )
    if not previous_assistant:
        return False
    if not _assistant_confirmation_matches_operation(previous_assistant.content or "", action=action, paths=paths):
        return False

    meta = dict(conv.meta or {})
    approvals = dict(meta.get("file_approvals") or {})
    matching = next(
        (
            (approval_id, item)
            for approval_id, item in reversed(list(approvals.items()))
            if isinstance(item, dict)
            and item.get("status") == "pending"
            and operation.matches(item)
            and (
                not item.get("requested_by_user_id")
                or item.get("requested_by_user_id") == user_id
            )
        ),
        None,
    )
    if matching is None:
        return False
    approval_id, item = matching
    now = datetime.now(timezone.utc).isoformat()
    item = dict(item)
    item.update({
        "status": "consumed",
        "resolved_by_user_id": user_id,
        "resolved_at": now,
        "consumed_at": now,
        "source": "natural_confirmation",
    })
    approvals[approval_id] = item
    meta["file_approvals"] = approvals
    conv.meta = meta
    flag_modified(conv, "meta")
    return True


def _deny_payload(*, action: str, tool_name: str, paths: list[str], mode: str, reason: str | None = None) -> str:
    return json.dumps({
        "error": "file_permission_denied",
        "mode": mode,
        "reason": reason or "User file permission policy denies AI changes to user-visible files.",
        "operation": {"tool": tool_name, "action": action, "paths": paths},
    }, ensure_ascii=False)


AUTHORIZATION_RECEIPT_MISMATCH_ERROR = "internal_authorization_error"


def receipt_authorizes_file_mutation(
    receipt: Any | None,
    *,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None,
    conversation_id: str | None,
    task_id: str | None,
    action: str,
    visible_paths: list[str],
) -> tuple[bool, str]:
    """Whether the public call's receipt covers this inner write, and its action key.

    The one comparison ``guard_ai_file_mutation`` uses; tests evaluate the
    same function rather than re-deriving it.
    """
    action_key = _precise_workspace_file_action_key(
        entity_id=entity_id,
        action=action,
        paths=visible_paths,
    )
    authorized = bool(
        receipt
        and receipt.authorizes_workspace_file_mutation(
            capability_id="file.write",
            action_key=action_key,
            paths=visible_paths,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
        )
    )
    return authorized, action_key


def _receipt_mismatch_payload(
    *,
    action: str,
    tool_name: str,
    paths: list[str],
    authorized_action_key: str,
    authorized_paths: tuple[str, ...],
    attempted_action_key: str,
    scope_mismatch: tuple[str, ...] = (),
) -> str:
    """An internal invariant violation, never a user permission decision.

    The public call was already authorized; its handler then tried to write
    somewhere else. No grant, approval or retry can repair that, so the model
    must report an internal error instead of asking the user to "allow" it.
    ``scope_mismatch`` names the receipt scope fields that differ from the
    write's: matching paths with a differing scope is not a path bug.
    """
    logger.error(
        "authorization_receipt_mismatch tool=%s attempted=%s %s authorized=%s %s scope_mismatch=%s",
        tool_name, attempted_action_key, paths, authorized_action_key, list(authorized_paths),
        list(scope_mismatch),
    )
    message = (
        "Internal Manor error: the file write did not match the action, file or "
        "Workspace scope this tool call was authorized for. This is a platform bug, not a permission "
        "decision: no user, owner or approval can allow it, and repeating the same "
        "call fails the same way. Tell the user the file could not be saved because "
        "of an internal error. Do not ask them to grant permission."
    )
    return json.dumps({
        "error": AUTHORIZATION_RECEIPT_MISMATCH_ERROR,
        "mode": "authorization_receipt_mismatch",
        "message": message,
        "reason": message,
        "internal_error": True,
        "requires_human": False,
        "retryable": False,
        "operation": {"tool": tool_name, "action": action, "paths": paths},
        "authorized": {"action_key": authorized_action_key, "paths": list(authorized_paths)},
        "attempted": {"action_key": attempted_action_key, "paths": paths},
        **({"scope_mismatch": list(scope_mismatch)} if scope_mismatch else {}),
    }, ensure_ascii=False)


async def _resource_file_acl_denial(
    db,
    *,
    runtime_envelope: Any | None,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None,
    action: str,
    tool_name: str,
    paths: list[str],
) -> str | None:
    """Apply canonical entity-file ACLs to one AI mutation target set."""

    def denied(reason: str) -> str:
        return _deny_payload(
            action=action,
            tool_name=tool_name,
            paths=paths,
            mode="entity_file_acl_denied",
            reason=reason,
        )

    principal = getattr(runtime_envelope, "principal", None)
    execution_user_id = getattr(principal, "execution_user_id", None)
    actor_user_id = str(user_id or execution_user_id or "").strip()
    from packages.core.ai.runtime.file_actions import runtime_entity_file_root
    from packages.core.services.filesystem_access import (
        FilesystemAccessDenied,
        require_path_mutation_access,
        require_path_write_access,
    )

    entity_root = runtime_entity_file_root(entity_id)
    normalized_action = str(action or "").strip().lower()
    root = os.path.realpath(entity_root) if entity_root else None
    if not actor_user_id:
        # Conversation-less autonomous workers historically mutate Workspace
        # artifacts without impersonating a user. This exception is
        # Workspace-root-bound: the runtime approval proves which Workspace
        # authorized the call, not permission to mutate files elsewhere in the
        # entity tree. Create and in-place edit are allowed; delete remains
        # user-authenticated.
        if (
            normalized_action not in {"delete", "remove"}
            and root is not None
            and workspace_id
        ):
            from packages.core.models.workspace import Workspace
            from packages.core.services.workspace_artifacts import (
                workspace_artifact_storage_base,
            )

            artifact_folder_id = await db.scalar(
                select(Workspace.artifact_folder_id).where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                    Workspace.status == "active",
                    Workspace.deleted_at.is_(None),
                ).limit(1)
            )
            workspace_base = workspace_artifact_storage_base(artifact_folder_id)
            resolved_workspace_root = (
                os.path.realpath(os.path.join(root, workspace_base))
                if workspace_base
                else None
            )
            workspace_root = (
                resolved_workspace_root
                if resolved_workspace_root
                and resolved_workspace_root != root
                and resolved_workspace_root.startswith(root + os.sep)
                else None
            )
            workspace_root_bound = bool(workspace_root)
            for path in paths:
                rel_path = normalize_rel_path(path)
                full_path = os.path.realpath(os.path.join(root, rel_path))
                if (
                    workspace_root is None
                    or full_path == workspace_root
                    or not full_path.startswith(workspace_root + os.sep)
                ):
                    workspace_root_bound = False
                    break
            if workspace_root_bound:
                return None
        return denied(
            "An authenticated user is required unless every target is a create "
            "or edit inside the authorized Workspace artifact root."
        )

    actor = await db.scalar(
        select(User).where(
            User.id == actor_user_id,
            User.status == "active",
            User.deleted_at.is_(None),
        ).limit(1)
    )
    if actor is None:
        return denied("The file mutation actor is no longer available in this entity.")
    from packages.core.permissions import resolve_effective_user_role_name

    actor_role = await resolve_effective_user_role_name(
        db,
        user_id=actor_user_id,
        entity_id=entity_id,
        legacy_role=actor.role if actor.entity_id == entity_id else None,
    )
    if not actor_role:
        return denied("The file mutation actor is no longer available in this entity.")
    scoped_actor = SimpleNamespace(
        id=actor.id,
        entity_id=entity_id,
        role=actor_role,
    )
    if root is None:
        return denied("Entity filesystem storage is unavailable.")
    try:
        for path in paths:
            rel_path = normalize_rel_path(path)
            full_path = os.path.realpath(os.path.join(root, rel_path))
            if full_path != root and not full_path.startswith(root + os.sep):
                raise FilesystemAccessDenied(403, "Path traversal detected")
            canonical_rel_path = normalize_rel_path(os.path.relpath(full_path, root))
            if normalized_action in {"delete", "remove"}:
                await require_path_mutation_access(
                    db,
                    user=scoped_actor,
                    rel_path=canonical_rel_path,
                    full_path=full_path,
                    entity_root=root,
                    action="delete",
                    workspace_id=workspace_id,
                )
            else:
                await require_path_write_access(
                    db,
                    user=scoped_actor,
                    rel_path=canonical_rel_path,
                    exists=os.path.exists(full_path),
                    workspace_id=workspace_id,
                )
    except FilesystemAccessDenied as exc:
        return denied(exc.detail)
    return None


async def guard_ai_file_resource_access(
    *,
    entity_id: str,
    user_id: str | None,
    conversation_id: str | None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    runtime_envelope: Any | None = None,
    tool_name: str,
    action: FileMutationAction | str,
    paths: Iterable[str],
) -> str | None:
    """Check resource ACLs before a file tool reads target bytes or metadata.

    Runtime receipts bind an approved public call to its arguments and
    Workspace path. They do not contain Document ownership, grants,
    visibility, classification, or quarantine state, so every route must
    still pass the canonical Knowledge/filesystem ACL.
    """
    raw_paths = tuple(paths)
    try:
        action = FileMutationAction.normalize(action).value
    except ValueError as exc:
        return _deny_payload(
            action=str(getattr(action, "value", action) or "invalid"),
            tool_name=tool_name,
            paths=visible_user_paths(raw_paths),
            mode="invalid_file_action",
            reason=str(exc),
        )
    try:
        visible_paths = canonical_visible_user_paths(entity_id, raw_paths)
    except ValueError as exc:
        return _deny_payload(
            action=action,
            tool_name=tool_name,
            paths=visible_user_paths(raw_paths),
            mode="entity_file_acl_denied",
            reason=str(exc),
        )
    if len(visible_paths) != len(raw_paths):
        return _deny_payload(
            action=action,
            tool_name=tool_name,
            paths=visible_paths,
            mode="entity_file_acl_denied",
            reason="File access is limited to user-visible Entity paths.",
        )
    if not raw_paths:
        return None

    from packages.core.database import async_session

    async with async_session() as db:
        return await _resource_file_acl_denial(
            db,
            runtime_envelope=runtime_envelope,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=workspace_id,
            action=action,
            tool_name=tool_name,
            paths=visible_paths,
        )


async def guard_ai_file_read_access(
    *,
    entity_id: str,
    user_id: str | None,
    conversation_id: str | None = None,
    workspace_id: str | None = None,
    runtime_envelope: Any | None = None,
    tool_name: str,
    paths: Iterable[str],
) -> str | None:
    """Require Knowledge ACLs or an exact, current-conversation attachment."""
    raw_paths = tuple(paths)
    attachment_paths = [path for path in raw_paths if isinstance(path, str) and is_chat_upload_path(path)]
    try:
        visible_paths = canonical_visible_user_paths(entity_id, raw_paths)
        _require_canonical_file_paths(entity_id, attachment_paths)
    except ValueError as exc:
        return _deny_payload(
            action=FileResourceAction.READ.value,
            tool_name=tool_name,
            paths=visible_user_paths(raw_paths),
            mode="entity_file_acl_denied",
            reason=str(exc),
        )
    if len(visible_paths) + len(attachment_paths) != len(raw_paths) or not raw_paths:
        return _deny_payload(
            action=FileResourceAction.READ.value,
            tool_name=tool_name,
            paths=visible_paths,
            mode="entity_file_acl_denied",
            reason="Read access is limited to user-visible Entity files.",
        )

    principal = getattr(runtime_envelope, "principal", None)
    actor_user_id = str(
        user_id or getattr(principal, "execution_user_id", None) or ""
    ).strip() or None
    from packages.core.database import async_session
    from packages.core.services.document_access import unreadable_document_paths

    async with async_session() as db:
        blocked = await unreadable_document_paths(
            db,
            entity_id=entity_id,
            rel_paths=visible_paths,
            user_id=actor_user_id,
            workspace_id=workspace_id,
            actor_type="agent",
        ) if visible_paths else set()
        for path in attachment_paths:
            if not await _chat_upload_readable(
                db, entity_id=entity_id, user_id=actor_user_id,
                conversation_id=conversation_id, workspace_id=workspace_id, path=path,
            ):
                blocked.add(path)
    if blocked:
        return _deny_payload(
            action=FileResourceAction.READ.value,
            tool_name=tool_name,
            paths=visible_paths,
            mode="entity_file_acl_denied",
            reason="Read access is required for every source file.",
        )
    return None


async def _file_mutation_policy(
    db,
    *,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None,
    action: str,
    action_key: str,
    paths: list[str],
    task_id: str | None = None,
):
    """The governance decision (account / Workspace / Task rules) for one file mutation."""
    from packages.core.governance.service import check_step_policy

    return await check_step_policy(
        db, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
        kind="action", action_key=action_key, resource_id=paths[0] if len(paths) == 1 else None,
        resource_ids=tuple(paths), resource_kind="file",
        risk_level="high" if action in {"delete", "remove"} else "medium",
        capability_id="file.write", operation=action, task_id=task_id,
    )


async def guard_ai_file_governance_hard_block(
    *,
    entity_id: str,
    user_id: str | None,
    workspace_id: str | None,
    tool_name: str,
    action: FileMutationAction | str,
    paths: Iterable[str],
) -> str | None:
    """Apply only governance hard blocks to a file mutation whose consent is already given.

    For a write that belongs to an action the user already approved (e.g. the
    outputs of an approved delegation, saved later by a background worker),
    so no approval request is created here. The decision is the same one
    ``guard_ai_file_mutation`` evaluates (same action key, facts and rule
    chain), read-only. A ``deny`` outcome or a Workspace constraint (risk
    ceiling) blocks; an outcome that would ask for approval does not, because
    that approval was the original action's. An unreadable or missing
    governance configuration fails closed, as it does everywhere else.
    Resource ACLs are separate: see ``guard_ai_file_resource_access``.
    """
    raw_paths = tuple(paths)
    try:
        action = FileMutationAction.normalize(action).value
        visible_paths = canonical_visible_user_paths(entity_id, raw_paths)
    except ValueError as exc:
        return _deny_payload(action=str(getattr(action, "value", action) or "invalid"), tool_name=tool_name,
            paths=visible_user_paths(raw_paths), mode="entity_file_acl_denied", reason=str(exc))
    if len(visible_paths) != len(raw_paths):
        return _deny_payload(action=action, tool_name=tool_name, paths=visible_paths,
            mode="entity_file_acl_denied", reason="File changes are limited to user-visible Entity paths.")
    if not visible_paths:
        return None
    from packages.core.database import async_session
    from packages.core.governance.approval_policy_service import ApprovalPolicyIntegrityError

    action_key = _precise_workspace_file_action_key(entity_id=entity_id, action=action, paths=visible_paths)
    async with async_session() as db:
        try:
            policy = await _file_mutation_policy(
                db, entity_id=entity_id, user_id=user_id, workspace_id=workspace_id,
                action=action, action_key=action_key, paths=visible_paths,
            )
        except ApprovalPolicyIntegrityError as exc:
            return _deny_payload(action=action, tool_name=tool_name, paths=visible_paths,
                mode=FilePermissionMode.DENY.value, reason=f"Governance configuration is unavailable: {exc}")
    if policy.allowed or policy.pause_for_hitl:
        return None
    detail = policy.reason or "Denied by governance policy"
    if policy.matched_rule and policy.matched_rule != policy.reason:
        detail = f"{detail} (rule {policy.matched_rule})"
    return _deny_payload(action=action, tool_name=tool_name, paths=visible_paths,
        mode=FilePermissionMode.DENY.value, reason=detail)


async def guard_ai_file_mutation(
    *,
    entity_id: str,
    user_id: str | None,
    conversation_id: str | None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    runtime_envelope: Any | None = None,
    tool_name: str,
    action: FileMutationAction | str,
    paths: Iterable[str],
    approval_token: str | None = None,
    content_preview: Any = None,
    approval_payload: Any = None,
    execution_id: str | None = None,
) -> str | None:
    """Return None when allowed, else a JSON tool result that blocks execution."""
    raw_paths = tuple(paths)
    try:
        action = FileMutationAction.normalize(action).value
    except ValueError as exc:
        return _deny_payload(
            action=str(getattr(action, "value", action) or "invalid"),
            tool_name=tool_name,
            paths=visible_user_paths(raw_paths),
            mode="invalid_file_action",
            reason=str(exc),
        )
    try:
        visible_paths = canonical_visible_user_paths(entity_id, raw_paths)
    except ValueError as exc:
        return _deny_payload(
            action=action,
            tool_name=tool_name,
            paths=visible_user_paths(raw_paths),
            mode="entity_file_acl_denied",
            reason=str(exc),
        )
    if len(visible_paths) != len(raw_paths):
        return _deny_payload(
            action=action,
            tool_name=tool_name,
            paths=visible_paths,
            mode="entity_file_acl_denied",
            reason="File changes are limited to user-visible Entity paths.",
        )
    if not raw_paths:
        return None
    operation = FileApprovalOperationFactory.create(
        tool_name=tool_name,
        action=action,
        paths=visible_paths,
        payload=content_preview if approval_payload is None else approval_payload,
    )

    from packages.core.database import async_session

    async with async_session() as db:
        conv = None
        if conversation_id:
            conversation_query = select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.entity_id == entity_id,
            )
            conv = (await db.execute(conversation_query)).scalar_one_or_none()
        runtime_action_key = (
            "workspace.file.delete"
            if str(action or "").strip().lower() in {"delete", "remove"}
            else "workspace.file.write"
        )
        from packages.core.services.runtime_authorization import (
            authorize_runtime_action,
        )

        effective_workspace_id = workspace_id or getattr(conv, "workspace_id", None)
        from packages.core.ai.runtime.authorization_receipts import (
            runtime_current_tool_authorization_receipt,
        )

        authorization_receipt = runtime_current_tool_authorization_receipt()
        receipt_authorizes_workspace_write, receipt_action_key = receipt_authorizes_file_mutation(
            authorization_receipt,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=effective_workspace_id,
            conversation_id=conversation_id,
            task_id=task_id,
            action=action,
            visible_paths=visible_paths,
        )
        authorization_tool_name = (
            authorization_receipt.tool_name
            if receipt_authorizes_workspace_write and authorization_receipt
            else tool_name
        )
        authorization_action_key = (
            authorization_receipt.action_key
            if receipt_authorizes_workspace_write and authorization_receipt
            else runtime_action_key
        )
        principal = getattr(runtime_envelope, "principal", None)
        raw_principal_kind = getattr(principal, "kind", None) if principal is not None else None
        principal_kind = getattr(raw_principal_kind, "value", raw_principal_kind)
        bound_tool_names = set(
            getattr(runtime_envelope, "allowed_tool_names", None) or ()
        )
        for binding in getattr(runtime_envelope, "tool_bindings", None) or ():
            if isinstance(binding, dict) and binding.get("name"):
                bound_tool_names.add(str(binding["name"]))

        permission = await authorize_runtime_action(
            db,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=effective_workspace_id,
            action_key=authorization_action_key,
            capability_id="file.write",
            principal_kind=principal_kind,
            principal_agent_id=(
                getattr(principal, "agent_id", None) if principal is not None else None
            ),
            principal_execution_user_id=(
                getattr(principal, "execution_user_id", None)
                if principal is not None
                else None
            ),
            # A public tool such as generate_file may delegate to an internal
            # materializer named generate_document_file. Agent bindings are
            # attached to the public tool, so revalidate that identity rather
            # than inventing a second capability at the persistence boundary.
            tool_name=authorization_tool_name,
            bound_tool_names=bound_tool_names,
            conversation_id=conversation_id,
            task_id=task_id,
            runtime_surface=getattr(runtime_envelope, "surface", None),
        )
        if not permission.allowed:
            return _deny_payload(
                action=action,
                tool_name=tool_name,
                paths=visible_paths,
                mode="permission_denied",
                reason=permission.reason,
            )

        if (
            not receipt_authorizes_workspace_write
            and authorization_receipt is not None
            and authorization_receipt.capability_id == "file.write"
        ):
            return _receipt_mismatch_payload(
                action=action,
                tool_name=tool_name,
                paths=visible_paths,
                authorized_action_key=authorization_receipt.action_key,
                authorized_paths=authorization_receipt.resource_ids,
                attempted_action_key=receipt_action_key,
                scope_mismatch=tuple(
                    name
                    for name, attempted in (
                        ("entity_id", entity_id),
                        ("user_id", user_id),
                        ("workspace_id", effective_workspace_id),
                        ("conversation_id", conversation_id),
                        ("task_id", task_id),
                    )
                    if (getattr(authorization_receipt, name) or None) != (str(attempted or "").strip() or None)
                ),
            )

        acl_denial = await _resource_file_acl_denial(
            db,
            runtime_envelope=runtime_envelope,
            entity_id=entity_id,
            user_id=user_id,
            workspace_id=effective_workspace_id,
            action=action,
            tool_name=tool_name,
            paths=visible_paths,
        )
        if acl_denial:
            return acl_denial

        if receipt_authorizes_workspace_write:
            # The registered public call already crossed unified Workspace
            # governance/HITL. Resource ACLs above remain mandatory; only the
            # legacy per-user approval plane is skipped here.
            return None

        operation_id = execution_id or (authorization_receipt.operation_id if authorization_receipt else None)
        if not operation_id and not approval_token:
            return _deny_payload(action=action, tool_name=tool_name, paths=visible_paths,
                mode="approval_operation_identity_missing", reason="A persisted execution identity is required for this file operation.")
        from packages.core.governance.approvals import _consent_requirements
        policy = await _file_mutation_policy(
            db, entity_id=entity_id, user_id=user_id, workspace_id=effective_workspace_id,
            action=action, action_key=receipt_action_key, paths=visible_paths, task_id=task_id,
        )
        mode = FilePermissionMode.APPROVAL.value
        if not policy.allowed and not policy.pause_for_hitl:
            return _deny_payload(action=action, tool_name=tool_name, paths=visible_paths,
                mode=FilePermissionMode.DENY.value, reason=policy.reason)

        if conversation_id:
            # Every approval state transition (consume an explicit token,
            # consume a natural confirmation, or create a pending request)
            # must observe and update one serialized Conversation.meta value.
            conv = (await db.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.entity_id == entity_id,
                ).with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()

        if approval_token:
            approvals = ((conv.meta or {}).get("file_approvals") if conv else {}) or {}
            item = approvals.get(approval_token)
            if not item:
                return _deny_payload(
                    action=action, tool_name=tool_name, paths=visible_paths, mode=mode,
                    reason="Approval token was not found for this conversation.",
                )
            if item.get("status") != "approved":
                return _deny_payload(
                    action=action, tool_name=tool_name, paths=visible_paths, mode=mode,
                    reason=f"Approval token is {item.get('status') or 'not approved'}.",
                )
            if item.get("consumed_at"):
                return _deny_payload(
                    action=action, tool_name=tool_name, paths=visible_paths, mode=mode,
                    reason="Approval token was already used.",
                )
            if not operation.matches(item):
                return _deny_payload(
                    action=action, tool_name=tool_name, paths=visible_paths, mode=mode,
                    reason=(
                        "Approval token does not match this exact file operation, "
                        "including its approved payload."
                    ),
                )
            if (not item.get("operation_id") or item.get("approval_action_key") != receipt_action_key
                or not set(_consent_requirements(policy)).issubset(set(item.get("approval_requirements") or []))):
                item["status"] = "expired"
                conv.meta = {**(conv.meta or {}), "file_approvals": approvals}
                flag_modified(conv, "meta")
                await db.commit()
                return _deny_payload(action=action, tool_name=tool_name, paths=visible_paths,
                    mode="approval_requirements_changed", reason="Approval requirements changed. Start a new file action for review.")
            item["status"] = "consumed"
            item["consumed_at"] = datetime.now(timezone.utc).isoformat()
            conv.meta = {**(conv.meta or {}), "file_approvals": approvals}
            flag_modified(conv, "meta")
            await db.commit()
            return None

        existing = next((
            (request_id, item) for request_id, item in reversed(list(((conv.meta or {}).get("file_approvals") or {}).items()))
            if isinstance(item, dict) and item.get("operation_id") == operation_id
        ), None) if conv else None
        if existing is not None:
            request_id, item = existing
            if not operation.matches(item) or item.get("status") != "pending":
                return _deny_payload(action=action, tool_name=tool_name, paths=visible_paths,
                    mode="approval_operation_terminal", reason="This file operation changed or was already decided. Use its exact continuation or start a new action.")
            return _hitl_payload(request_id, action=action, tool_name=tool_name,
                paths=visible_paths, mode=mode, content_preview=content_preview)
        if policy.allowed:
            return None

        if not conversation_id:
            return _deny_payload(
                action=action, tool_name=tool_name, paths=visible_paths, mode=mode,
                reason="Approval is required, but no conversation_id is available to track approval.",
            )

        if not conv:
            return _deny_payload(
                action=action, tool_name=tool_name, paths=visible_paths, mode=mode,
                reason="Approval is required, but the conversation was not found.",
            )

        hitl_id = generate_ulid()
        meta = dict(conv.meta or {})
        approvals = dict(meta.get("file_approvals") or {})
        approvals[hitl_id] = {
            "status": "pending", "operation_id": operation_id, "approval_action_key": receipt_action_key,
            "approval_requirements": _consent_requirements(policy),
            **operation.to_record(),
            "content": _approval_content_preview(
                action=action,
                tool_name=tool_name,
                paths=visible_paths,
                content_preview=content_preview,
            ),
            "requested_by_user_id": user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        meta["file_approvals"] = approvals
        conv.meta = meta
        flag_modified(conv, "meta")
        await db.commit()
        return _hitl_payload(
            hitl_id,
            action=action,
            tool_name=tool_name,
            paths=visible_paths,
            mode=mode,
            content_preview=content_preview,
        )


async def resolve_file_approval_message(
    db,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    hitl_id: str,
    action: str,
) -> str | None:
    """Resolve an approval card. Returns a replacement message for the LLM."""
    if normalize_approval_choice(action) == APPROVAL_CHOICE_ALWAYS_APPROVE:
        raise ValueError("Always approval requires a versioned approval-settings command")
    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not conv:
        return None
    meta = dict(conv.meta or {})
    approvals = dict(meta.get("file_approvals") or {})
    item = approvals.get(hitl_id)
    if not item:
        return None
    current_status = str(item.get("status") or "pending").lower()
    if current_status != "pending":
        return (
            f"File operation {hitl_id} is already {current_status}. "
            "Do not retry it and leave files unchanged. "
            f"Operation was: tool={item.get('tool')}, action={item.get('action')}, paths={item.get('paths')}."
        )

    raw_action = normalize_approval_choice(action)
    runtime_action_key = (
        "workspace.file.delete"
        if str(item.get("action") or "").strip().lower() in {"delete", "remove"}
        else "workspace.file.write"
    )
    from packages.core.services.runtime_authorization import authorize_hitl_action

    authority = await authorize_hitl_action(
        db,
        entity_id=entity_id,
        workspace_id=conv.workspace_id,
        by_user_id=user_id,
        action_key=runtime_action_key,
        capability_id="file.write",
        standing=raw_action == APPROVAL_CHOICE_ALWAYS_APPROVE,
        requested_by=str(item.get("requested_by_user_id") or "") or None,
        origin_conversation_id=conversation_id,
    )
    if not authority.allowed:
        return (
            f"File approval permission denied: {authority.reason or 'not authorized'}. "
            "Leave the approval pending and do not retry the file operation."
        )
    if raw_action == APPROVAL_CHOICE_ALWAYS_APPROVE:
        raise ValueError("Always approval requires a versioned approval-settings command")
    elif raw_action == APPROVAL_CHOICE_APPROVE:
        normalized = "approved"
    elif raw_action == APPROVAL_CHOICE_REJECT:
        normalized = "rejected"
    else:
        normalized = "rejected"
    item["status"] = normalized
    item["resolved_by_user_id"] = user_id
    item["resolved_at"] = datetime.now(timezone.utc).isoformat()
    approvals[hitl_id] = item
    meta["file_approvals"] = approvals
    conv.meta = meta
    flag_modified(conv, "meta")
    try:
        from packages.core.services.hitl_requests import mark_hitl_request_resolved
        await mark_hitl_request_resolved(
            db,
            conversation_id=conversation_id,
            hitl_id=hitl_id,
            choice=raw_action or "reject",
        )
    except Exception:
        pass
    await db.flush()

    if normalized == "approved":
        always_note = (
            " The user also set future user-visible file operations to always approve."
            if raw_action == APPROVAL_CHOICE_ALWAYS_APPROVE else ""
        )
        return (
            f"User approved this exact file operation once. Retry the blocked tool call now with "
            f"approval_token='{hitl_id}'. Operation: tool={item.get('tool')}, "
            f"action={item.get('action')}, paths={item.get('paths')}. Do not change any other files."
            f"{always_note}"
        )
    return (
        f"User rejected file operation {hitl_id}. Do not retry it and leave files unchanged. "
        f"Operation was: tool={item.get('tool')}, action={item.get('action')}, paths={item.get('paths')}."
    )


async def cancel_pending_file_approvals(
    db,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str | None,
    hitl_ids: Iterable[str] | None = None,
    reason: str = "request_stopped",
) -> int:
    """Permanently close pending file approvals for a stopped request.

    A stopped SSE stream may leave an approval token in conversation metadata.
    Marking it cancelled prevents a stale approval card or short "yes" reply
    from reviving the abandoned file operation later.
    """
    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not conv:
        return 0

    wanted_ids = {
        str(item or "").strip()
        for item in (hitl_ids or [])
        if str(item or "").strip()
    }
    meta = dict(conv.meta or {})
    approvals = dict(meta.get("file_approvals") or {})
    if not approvals:
        return 0

    cancelled = 0
    cancelled_ids: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    for hitl_id, item in list(approvals.items()):
        if not isinstance(item, dict):
            continue
        if wanted_ids and hitl_id not in wanted_ids:
            continue
        if item.get("status") != "pending":
            continue
        requested_by = item.get("requested_by_user_id")
        if user_id and requested_by and requested_by != user_id:
            continue
        item["status"] = "cancelled"
        item["resolved_by_user_id"] = user_id
        item["resolved_at"] = now
        item["cancel_reason"] = reason
        approvals[hitl_id] = item
        cancelled += 1
        cancelled_ids.append(hitl_id)

    if not cancelled:
        return 0

    meta["file_approvals"] = approvals
    conv.meta = meta
    flag_modified(conv, "meta")
    try:
        from packages.core.services.hitl_requests import mark_hitl_request_resolved
        for hitl_id in cancelled_ids:
            await mark_hitl_request_resolved(
                db,
                conversation_id=conversation_id,
                hitl_id=hitl_id,
                choice="cancelled",
            )
    except Exception:
        pass
    await db.flush()
    return cancelled


async def resolve_pending_file_approval_from_reply(
    db,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    message: str,
) -> str | None:
    """Resolve the single latest pending file approval from a short yes/no reply."""
    decision = classify_file_approval_reply(message)
    if not decision:
        return None
    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
        ).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not conv:
        return None
    approvals = ((conv.meta or {}).get("file_approvals") if conv else {}) or {}
    pending = [
        (hitl_id, item)
        for hitl_id, item in approvals.items()
        if isinstance(item, dict)
        and item.get("status") == "pending"
        and (not item.get("requested_by_user_id") or item.get("requested_by_user_id") == user_id)
    ]
    if len(pending) != 1:
        return None
    hitl_id, _item = pending[0]
    return await resolve_file_approval_message(
        db,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_id,
        action=decision,
    )
