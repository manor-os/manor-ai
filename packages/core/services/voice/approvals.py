"""Present Chat approvals to Live without giving Live approval authority."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONPATH

from packages.core.ai.runtime.provider_approvals import provider_approval_is_expired
from packages.core.constants.approvals import ApprovalOriginKind, ApprovalStatus, HitlType
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.task import Conversation, Message

_MAX_ACTIONS = 8
_OPEN_CARD_PATH = (
    '$.hitl_requests[*] ? (exists(@.id)'
    ' && (!exists(@.resolved) || @.resolved == false || @.resolved == null)'
    ' && (!exists(@.resolution) || @.resolution == null || @.resolution == "")'
    ' && (!exists(@.status) || @.status == null || @.status == "pending"))'
)
_CHOICES = frozenset({"approve", "reject", "always_approve"})
_HUMAN_ONLY = frozenset({
    "browser_permission", "captcha", "login", "mfa", "password_change",
    "sensitive_data_transmission", "needs_login", "needs_input", "input", "choice",
    "error", "handoff", "handoff_required",
})
_REPLIES = {
    "approve": frozenset({
        "yes", "y", "ok", "okay", "sure", "approve", "approved", "accept",
        "confirm", "confirmed", "goahead", "doit", "proceed", "continue",
        "是", "是的", "对", "对的", "可以", "确认", "确定", "同意", "批准",
        "继续", "好", "好的",
    }),
    "reject": frozenset({
        "no", "n", "reject", "rejected", "deny", "decline", "cancel", "stop",
        "dont", "donot", "不", "不是", "否", "不要", "不用", "取消", "拒绝",
    }),
    "always_approve": frozenset({
        "alwaysapprove", "approvealways", "alwaysallow", "allowalways",
        "以后都允许", "总是允许", "一直允许", "始终允许", "永久允许",
    }),
}


def _voice_approval_reply(text: str) -> tuple[str, int | None] | None:
    if not isinstance(text, str) or len(text) > 80 or "?" in text or "？" in text:
        return None
    compact = re.sub(r"[\s。.!！,，；;：:\"'“”‘’、_]+", "", text.lower())
    choice = next((choice for choice, replies in _REPLIES.items() if compact in replies), None)
    if choice:
        return choice, None
    english = re.fullmatch(r"(approve|reject|alwaysapprove)item([0-9]{1,2})", compact)
    if english:
        verb, ordinal = english.groups()
        return ("always_approve" if verb == "alwaysapprove" else verb), int(ordinal)
    chinese = re.fullmatch(r"(批准|确认|同意|拒绝|总是批准)第([零一二三四五六七八九十]|[0-9]{1,2})项", compact)
    if chinese:
        verb, ordinal = chinese.groups()
        index = int(ordinal) if ordinal.isascii() else "零一二三四五六七八九十".index(ordinal)
        return ("reject" if verb == "拒绝" else "always_approve" if verb == "总是批准" else "approve"), index
    return None


def voice_approval_reply_choice(text: str) -> str | None:
    """Only a complete verdict, optionally addressing a numbered shown item."""

    reply = _voice_approval_reply(text)
    return reply[0] if reply else None


class VoiceApprovalReplyError(ValueError):
    """A speakable blocked decision; callers must not delegate its raw reply."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class VoicePendingAction:
    id: str
    title: str
    description: str
    choices: tuple[str, ...]
    requires_ui: bool
    _fingerprint: str = field(default="", repr=False)

    def to_payload(self, *, item_number: int | None = None) -> dict[str, Any]:
        """Only public display fields cross the foreground provider boundary."""

        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "choices": list(self.choices),
            "requires_ui": self.requires_ui,
            **({"item_number": item_number} if item_number is not None else {}),
        }

    def to_record(self) -> dict[str, Any]:
        """Server-owned receipt state, never provider/client authored input."""

        return {**self.to_payload(), "_fingerprint": self._fingerprint}

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> VoicePendingAction:
        """Reject malformed persisted state instead of coercing authority fields."""

        if not isinstance(record, dict) or set(record) != {
            "id", "title", "description", "choices", "requires_ui", "_fingerprint",
        }:
            raise ValueError("Malformed Voice pending-action receipt")
        for key, limit in (("id", 128), ("title", 500), ("description", 2000)):
            value = record[key]
            if not isinstance(value, str) or len(value) > limit or (
                key != "description" and not value.strip()
            ) or (
                key == "id" and isinstance(value, str) and value != value.strip()
            ):
                raise ValueError("Malformed Voice pending-action display field")
        choices = record["choices"]
        if (
            not isinstance(choices, (list, tuple)) or len(choices) > len(_CHOICES)
            or any(not isinstance(choice, str) or choice not in _CHOICES for choice in choices)
            or len(set(choices)) != len(choices)
            or type(record["requires_ui"]) is not bool
        ):
            raise ValueError("Malformed Voice pending-action choices")
        fingerprint = record["_fingerprint"]
        if (
            not isinstance(fingerprint, str)
            or (not re.fullmatch(r"[0-9a-f]{64}", fingerprint) and not (
                fingerprint == "" and record["requires_ui"]
            ))
            or (not choices and not record["requires_ui"])
        ):
            raise ValueError("Malformed Voice pending-action fingerprint")
        return cls(
            id=record["id"], title=record["title"], description=record["description"],
            choices=tuple(record["choices"]), requires_ui=record["requires_ui"],
            _fingerprint=record["_fingerprint"],
        )


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _fingerprint(raw: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(raw, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return ""
    if len(encoded) > 65_536:
        return ""
    return hashlib.sha256(encoded.encode()).hexdigest()


def voice_pending_actions(raw_hitl_requests: Any) -> tuple[VoicePendingAction, ...]:
    """Bound and sanitize real Chat card snapshots; unknown types stay UI-only."""

    if not isinstance(raw_hitl_requests, (list, tuple)):
        return ()
    actions: list[VoicePendingAction] = []
    seen: set[str] = set()
    for raw in raw_hitl_requests[:64]:
        if (
            not isinstance(raw, dict) or raw.get("resolved") or raw.get("resolution")
            or raw.get("status") not in (None, "pending")
        ):
            continue
        target_id = raw.get("id")
        if not isinstance(target_id, str) or not target_id.strip() or len(target_id) > 128:
            continue
        target_id = target_id.strip()
        if target_id in seen:
            continue
        seen.add(target_id)
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        options = raw.get("options") if isinstance(raw.get("options"), list) else []
        choices = tuple(dict.fromkeys(
            option for option in options[:16] if isinstance(option, str) and option in _CHOICES
        ))
        kind = str(raw.get("hitl_type") or raw.get("type") or "")
        requires_ui = (
            kind not in {"approval", "authorize"}
            or bool(raw.get("requires_ui"))
            or any(raw.get(key) in _HUMAN_ONLY for key in (
                "kind", "confirmation_mode", "policy_category",
            ) if isinstance(raw.get(key), str))
            or not choices
        )
        fingerprint = _fingerprint(raw)
        actions.append(VoicePendingAction(
            id=target_id,
            title=_text(raw.get("prompt") or raw.get("title"), 500) or "Human response required",
            description=_text(raw.get("content") or raw.get("description") or payload.get("why"), 2000),
            choices=choices,
            requires_ui=requires_ui or not fingerprint,
            _fingerprint=fingerprint,
        ))
        if len(actions) == _MAX_ACTIONS:
            break
    # A truncated collection must not look like one uniquely selectable card.
    if len(raw_hitl_requests) > _MAX_ACTIONS:
        actions = [replace(action, requires_ui=True) for action in actions]
    return tuple(actions)


async def load_voice_pending_actions(
    db, *, conversation_id: str, entity_id: str,
) -> tuple[VoicePendingAction, ...]:
    """Refresh bounded current Chat cards after the caller validates call scope."""

    rows = (await db.execute(
        select(Message).join(Conversation, Conversation.id == Message.conversation_id).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
            # Filter *before* the limit: historical resolved cards must not
            # crowd an older open request out of the bounded snapshot. The
            # Message's pending_action channel may be resolved independently.
            func.jsonb_path_exists(Message.meta, cast(_OPEN_CARD_PATH, JSONPATH)),
        ).order_by(Message.created_at.desc()).limit(_MAX_ACTIONS + 1)
        .execution_options(populate_existing=True)
    )).scalars().all()
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    overflow = len(rows) > _MAX_ACTIONS
    for row in rows:
        raw = (row.meta or {}).get("hitl_requests")
        if not isinstance(raw, list):
            continue
        overflow = overflow or len(raw) > 64
        for card in raw[:64]:
            if not isinstance(card, dict) or not isinstance(card.get("id"), str):
                continue
            if card["id"] in seen:
                continue
            # Newest projection, including a resolved one, wins over stale cards.
            seen.add(card["id"])
            cards.append(card)
    actions = voice_pending_actions(cards)
    if overflow:
        actions = tuple(replace(action, requires_ui=True) for action in actions)
    return actions


async def prepare_voice_approval_reply(
    db,
    *,
    conversation_id: str,
    entity_id: str,
    user_id: str,
    text: str,
    presented_actions: tuple[VoicePendingAction, ...],
    target_id: str | None = None,
) -> str | None:
    """Address a human verdict to its exact still-open presented Chat card.

    No grants or tools run here. The existing Chat resolver must reauthorize
    and consume the decision under its own locks. Voice must disable Chat's
    implicit short-reply lookup, including when this returns None, so a stale
    or mixed file/runtime reply can never select an unrelated pending action.
    """

    reply = _voice_approval_reply(text)
    if reply is None:
        return None
    choice, ordinal = reply
    if not user_id or not entity_id or not conversation_id:
        raise VoiceApprovalReplyError("approval_scope_unavailable", "This call cannot resolve an approval in the current scope.")
    if not presented_actions:
        raise VoiceApprovalReplyError("approval_not_presented", "There is no matching approval presented in this call. Please open its Chat card.")
    if ordinal is not None:
        if not 1 <= ordinal <= min(len(presented_actions), _MAX_ACTIONS):
            raise VoiceApprovalReplyError("approval_ordinal_out_of_range", "That item number is not in the presented requests. Please say the number of the request you mean.")
        selected = presented_actions[ordinal - 1]
    else:
        # A model-selected ID cannot disambiguate a bare human "yes".
        if len(presented_actions) != 1:
            raise VoiceApprovalReplyError("approval_target_ambiguous", "More than one request is waiting. Please explicitly say approve or reject and its item number.")
        selected = presented_actions[0]
    if target_id is not None and target_id != selected.id:
        raise VoiceApprovalReplyError("approval_target_mismatch", "The selected request does not match your reply. No action was approved; please repeat the item number.")
    if selected.requires_ui or choice not in selected.choices:
        raise VoiceApprovalReplyError("approval_requires_ui", "This request needs its Chat controls; a spoken confirmation cannot complete it.")

    conversation = (await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.entity_id == entity_id,
        ).execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if conversation is None or (not conversation.workspace_id and conversation.user_id != user_id):
        raise VoiceApprovalReplyError("approval_scope_unavailable", "The original conversation is no longer available to this call.")
    messages = (await db.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.meta.contains({"hitl_requests": [{"id": selected.id}]}),
        ).order_by(Message.created_at.desc()).limit(2).execution_options(populate_existing=True)
    )).scalars().all()
    if not messages:
        raise VoiceApprovalReplyError("approval_stale", "That approval is no longer available. No action was approved.")
    latest = messages[0]
    raw_cards = (latest.meta or {}).get("hitl_requests") or []
    matches = [item for item in raw_cards if isinstance(item, dict) and item.get("id") == selected.id]
    if len(matches) != 1:
        raise VoiceApprovalReplyError("approval_target_ambiguous", "This approval has conflicting cards. Please review it in Chat.")
    raw = matches[0]
    current = voice_pending_actions([raw])
    if len(current) != 1 or current[0] != selected:
        raise VoiceApprovalReplyError("approval_changed", "That approval changed or was already answered. Please review its current Chat card.")

    request = (await db.execute(
        select(HitlRequest).where(
            HitlRequest.id == selected.id,
            HitlRequest.entity_id == entity_id,
            HitlRequest.origin_conversation_id == conversation_id,
        ).execution_options(populate_existing=True)
    )).scalar_one_or_none()
    files = (conversation.meta or {}).get("file_approvals") or {}
    file_request = files.get(selected.id) if isinstance(files, dict) else None
    # The Chat resolver tries legacy files first. A colliding ID must never
    # approve a different file operation instead of the presented Runtime call.
    if request is not None and file_request is not None:
        raise VoiceApprovalReplyError("approval_target_ambiguous", "This approval has conflicting records. Please review it in Chat.")
    if request is not None:
        context = request.context or {}
        continuation = context.get("continuation") or {}
        if request.status != ApprovalStatus.PENDING or provider_approval_is_expired(continuation):
            raise VoiceApprovalReplyError("approval_stale", "That approval expired or was already answered. No new action was approved.")
        if request.origin_kind != ApprovalOriginKind.TOOL_CALL or request.hitl_type != HitlType.AUTHORIZE or (
            isinstance(continuation, dict) and (
                continuation.get("confirmation_mode") == "handoff_required"
                or _text(continuation.get("policy_category"), 80) in _HUMAN_ONLY
            )
        ):
            raise VoiceApprovalReplyError("approval_requires_ui", "This request requires the original Chat controls or sign-in flow.")
        if (
            context.get("tool") != raw.get("tool") or request.action_key != raw.get("action")
            or ("args_preview" in raw and context.get("args_preview") != raw["args_preview"])
        ):
            raise VoiceApprovalReplyError("approval_changed", "The requested action changed. Please review its current Chat card.")
    elif isinstance(file_request, dict):
        if file_request.get("status") != "pending":
            raise VoiceApprovalReplyError("approval_stale", "That file approval was already answered. No new action was approved.")
        if any(file_request.get(key) != raw.get(key) for key in ("tool", "action", "paths")):
            raise VoiceApprovalReplyError("approval_changed", "The file operation changed. Please review its current Chat card.")
    else:
        raise VoiceApprovalReplyError("approval_requires_ui", "This request cannot be resolved by voice. Please use its original Chat card.")
    return json.dumps({"hitl_id": selected.id, "action": choice}, ensure_ascii=False)
