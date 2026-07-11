"""AI file mutation permission gates.

This protects user-visible Knowledge files from agent/tool writes. Internal
system paths such as .ai/** remain writable for agent memory/workspace state.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

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

FILE_PERMISSION_PREF_KEY = "ai_file_permission"
DEFAULT_FILE_PERMISSION = "approval"

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
        return "always_approve"
    if raw in _DENY_ALIASES or raw_space in _DENY_ALIASES:
        return "deny"
    if raw in _APPROVAL_ALIASES or raw_space in _APPROVAL_ALIASES:
        return "approval"
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
    if compact in _NEGATIVE_REPLY_HINTS or any(h in compact for h in _NEGATIVE_REPLY_HINTS if len(h) > 1):
        return "reject"
    if compact in _ALWAYS_REPLY_HINTS or any(h in compact for h in _ALWAYS_REPLY_HINTS):
        return "always_approve"
    if compact in _AFFIRMATIVE_REPLY_HINTS or any(h in compact for h in _AFFIRMATIVE_REPLY_HINTS if len(h) > 1):
        return "approve"
    return None


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


def _display_action_label(action: str) -> str:
    labels = {
        "create_document": "create file",
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
    if not user_id:
        return DEFAULT_FILE_PERMISSION
    row = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    prefs = row.preferences if row else {}
    value = (prefs or {}).get(FILE_PERMISSION_PREF_KEY)
    # Backward/alternate shapes, useful while UI wording settles.
    if value is None:
        value = (prefs or {}).get("file_permission")
    if value is None:
        value = (prefs or {}).get("file_permissions")
    return normalize_file_permission_mode(value)


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
    action: str,
    tool_name: str,
    paths: list[str],
) -> bool:
    """Treat a just-sent yes/approve as permission when it answers a matching prompt."""
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

    now = datetime.now(timezone.utc).isoformat()
    meta = dict(conv.meta or {})
    approvals = dict(meta.get("file_approvals") or {})
    approval_id = generate_ulid()
    approvals[approval_id] = {
        "status": "consumed",
        "tool": tool_name,
        "action": action,
        "paths": paths,
        "requested_by_user_id": user_id,
        "resolved_by_user_id": user_id,
        "created_at": now,
        "resolved_at": now,
        "consumed_at": now,
        "source": "natural_confirmation",
    }
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


async def guard_ai_file_mutation(
    *,
    entity_id: str,
    user_id: str | None,
    conversation_id: str | None,
    tool_name: str,
    action: str,
    paths: Iterable[str],
    approval_token: str | None = None,
    content_preview: Any = None,
) -> str | None:
    """Return None when allowed, else a JSON tool result that blocks execution."""
    visible_paths = visible_user_paths(paths)
    if not visible_paths:
        return None

    from packages.core.database import async_session

    async with async_session() as db:
        mode = await load_user_file_permission_mode(db, user_id)
        if mode == "always_approve":
            return None
        if mode == "deny":
            return _deny_payload(action=action, tool_name=tool_name, paths=visible_paths, mode=mode)
        from packages.core.config import get_settings
        if not get_settings().MANOR_AI_FILE_HITL_ENABLED:
            return None

        if approval_token:
            conv = None
            if conversation_id:
                conv = (await db.execute(
                    select(Conversation).where(
                        Conversation.id == conversation_id,
                        Conversation.entity_id == entity_id,
                    )
                )).scalar_one_or_none()
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
            if item.get("tool") != tool_name or item.get("action") != action or item.get("paths") != visible_paths:
                return _deny_payload(
                    action=action, tool_name=tool_name, paths=visible_paths, mode=mode,
                    reason="Approval token does not match this exact file operation.",
                )
            item["status"] = "consumed"
            item["consumed_at"] = datetime.now(timezone.utc).isoformat()
            conv.meta = {**(conv.meta or {}), "file_approvals": approvals}
            flag_modified(conv, "meta")
            await db.commit()
            return None

        if not conversation_id:
            return _deny_payload(
                action=action, tool_name=tool_name, paths=visible_paths, mode=mode,
                reason="Approval is required, but no conversation_id is available to track approval.",
            )

        conv = (await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.entity_id == entity_id,
            )
        )).scalar_one_or_none()
        if not conv:
            return _deny_payload(
                action=action, tool_name=tool_name, paths=visible_paths, mode=mode,
                reason="Approval is required, but the conversation was not found.",
            )

        if await _latest_user_confirmation_allows_operation(
            db,
            conv=conv,
            user_id=user_id,
            action=action,
            tool_name=tool_name,
            paths=visible_paths,
        ):
            await db.commit()
            return None

        hitl_id = generate_ulid()
        meta = dict(conv.meta or {})
        approvals = dict(meta.get("file_approvals") or {})
        approvals[hitl_id] = {
            "status": "pending",
            "tool": tool_name,
            "action": action,
            "paths": visible_paths,
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
    conv = (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
        )
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
    if raw_action == APPROVAL_CHOICE_ALWAYS_APPROVE:
        normalized = "approved"
        row = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if row:
            prefs = dict(row.preferences or {})
            prefs[FILE_PERMISSION_PREF_KEY] = "always_approve"
            row.preferences = prefs
            flag_modified(row, "preferences")
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
        )
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
        )
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
