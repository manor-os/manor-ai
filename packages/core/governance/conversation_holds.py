"""Approval holds the user asked for in one conversation.

"Anything you write to Knowledge needs my approval" or "stop at the approval
card" is a requirement for this conversation, not a Workspace rule. A hold
records it on the conversation and makes matching actions ask. It can only
tighten: it never turns an ask or a deny into an allow, so setting one needs
no approval of its own. It ends with the conversation.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

HOLDS_META_KEY = "approval_holds"
# Categories a user can ask to review; reads never pause and an unknown
# effect already follows the approval mode.
HOLDABLE_CATEGORIES = (
    "files", "communication", "publishing", "deletion", "purchases", "automation", "browser", "workspace",
)


async def add_conversation_holds(
    db: AsyncSession, *, entity_id: str, conversation_id: str, categories: list[str],
    reason: str = "",
) -> list[str]:
    """Record holds on this conversation; returns every category now held."""
    from packages.core.models.task import Conversation

    conversation = await db.get(Conversation, conversation_id, with_for_update=True)
    if conversation is None or str(conversation.entity_id) != str(entity_id):
        raise LookupError("conversation not found")
    meta = dict(conversation.meta or {})
    holds = [hold for hold in meta.get(HOLDS_META_KEY) or [] if isinstance(hold, dict)]
    held = {hold.get("category") for hold in holds}
    now = datetime.now(timezone.utc).isoformat()
    for category in categories:
        if category not in held:
            holds.append({"category": category, "reason": reason[:300], "created_at": now})
            held.add(category)
    meta[HOLDS_META_KEY] = holds
    conversation.meta = meta
    await db.flush()
    return sorted(c for c in held if c)


# Require a prospective review instruction. Merely mentioning an approval
# card (including reporting an earlier approval) must not create a hold.
_DEMAND = re.compile(
    r"(?:必须|需要|需|得)(?:先|经过|得到|获得|由)?(?:我(?:的)?|人工|手动|用户)?(?:审批|批准|确认|同意)"
    r"|(?:等我|待我|经我|让我(?:先)?)(?:审批|批准|确认|同意)"
    r"|(?<!不)要(?:先)?(?:我|人工)?(?:审批|批准|确认|同意)"
    r"|先(?:审批|批准)(?:后|再)"
    r"|(?:先停在|停在|停留在|先出示|先展示|先给我)(?:这张|这个)?(?:审批|批准|确认)卡"
    r"|\b(need|needs|require|requires|wait for|until)\b[^.!?\n]{0,24}\b(my )?(approval|sign-?off|review)\b"
    r"|\bask me (first|before)\b|\bstop at the (approval )?card\b",
    re.IGNORECASE,
)
_NEGATED = re.compile(
    r"(不需要|不用|无需|不必|免)[^。！？!?\n]{0,6}(审批|批准|确认)"
    r"|不要(?:再|再次)?让我(?:审批|批准|确认)"
    r"|\bno (approval|review) needed\b|\b(?:don't|do not|never) ask me (first|before)\b",
    re.IGNORECASE,
)
# "If approval is needed, show the diff" describes the existing gate; it
# does not ask for a new one. Remove that clause, not other demands nearby.
_CONDITIONAL = re.compile(
    r"(?:(?:如果|如|若|当|遇到)(?:需(?:要)?|必须|要)(?:我(?:的)?|人工)?(?:审批|批准|确认)"
    r"|每次(?:需(?:要)?|必须|要)(?:我(?:的)?|人工)?(?:审批|批准|确认)(?=就|时|的话|[，,]))"
    r"[^。！？!?\n，,；;]*"
    r"|\bif\s+(?:\w+\s+){0,3}(?:approval|review)\s+(?:is\s+)?(?:needed|required)\b[^,;.!?\n]*",
    re.IGNORECASE,
)
_OBJECTS = {
    "files": re.compile(r"写入|写到|保存|存到|存入|知识库|knowledge|文件|文档|\b(write|save|file|document)s?\b", re.IGNORECASE),
    "publishing": re.compile(r"发布|发帖|上线|\b(publish|post)(ing|s)?\b", re.IGNORECASE),
    "communication": re.compile(r"发送|发给|邮件|短信|消息|通知|\b(send|email|message)s?\b", re.IGNORECASE),
    "deletion": re.compile(r"删除|\b(delete|remove)s?\b", re.IGNORECASE),
    "purchases": re.compile(r"购买|付款|支付|下单|\b(purchase|pay|buy)s?\b", re.IGNORECASE),
    "workspace": re.compile(r"配置|工作区设置|\b(workspace|workflow|configuration|config)\b", re.IGNORECASE),
}
# Writes a demand with no named object most plausibly means.
_UNNAMED = ("files", "publishing", "communication", "deletion", "purchases")


def stated_hold_categories(text: str) -> set[str]:
    """Categories a user message asks to review first, in their own words.

    Read at the gate, only when an action of that kind is about to run, so a
    misread costs one extra review card and never lets anything through.
    """
    held: set[str] = set()
    for sentence in re.split(r"[。！？!?\n；;]+", str(text or "")):
        sentence = _CONDITIONAL.sub("", sentence)
        if not _DEMAND.search(sentence) or _NEGATED.search(sentence):
            continue
        named = {category for category, pattern in _OBJECTS.items() if pattern.search(sentence)}
        held |= named or set(_UNNAMED)
    return held


async def conversation_hold_rules(db: AsyncSession, conversation_id: str | None) -> list[Any]:
    """The conversation's holds as ask-only approval rules.

    Explicit holds (``hold_for_approval``) and the review demands in the
    user's own messages both count: the user's words, not the Agent's memory
    of them, are what stops the action.
    """
    if not conversation_id:
        return []
    from sqlalchemy import select

    from packages.core.governance.approval_policy import ApprovalRule, CategorySelector
    from packages.core.models.task import Conversation, Message

    conversation = await db.get(Conversation, conversation_id)
    holds = (conversation.meta or {}).get(HOLDS_META_KEY) if conversation is not None else None
    categories = {hold.get("category") for hold in holds or [] if isinstance(hold, dict)}
    said = (await db.execute(
        select(Message.content).where(
            Message.conversation_id == conversation_id, Message.role == "user",
        ).order_by(Message.created_at.desc()).limit(50)
    )).scalars().all()
    for text in said:
        categories |= stated_hold_categories(text)
    return [
        ApprovalRule(id=f"conversation_hold:{category}", effect="ask",
                     selector=CategorySelector(category=category))
        for category in sorted(c for c in categories if c in HOLDABLE_CATEGORIES)
    ]
