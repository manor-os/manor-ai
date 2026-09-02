"""Durable admission state for browser voice instructions.

The Message row is the user's visible receipt and the queue record.  This
keeps voice on the ordinary conversation timeline without introducing a
second chat runtime or a second source of truth.  A pending receipt can be
recovered after an API restart.  A running receipt is never replayed because
the Chat Agent may already have completed an external side effect before the
process stopped; it is marked interrupted instead.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.task import Conversation, Message
from packages.core.services.conversation_messages import add_message
from packages.core.services.voice.work_types import (
    VoiceControlReplyKind,
    VoiceWorkContext,
    VoiceWorkState,
)

VOICE_WORK_META_KEY = "voice_work"
VOICE_WORK_RECOVERY_LIMIT = 32
VOICE_WORK_MAX_RECOVERY_AGE_SECONDS = 2 * 60 * 60
_TERMINAL_STATES = frozenset(
    state.value
    for state in (
        VoiceWorkState.COMPLETED,
        VoiceWorkState.FAILED,
        VoiceWorkState.INTERRUPTED,
    )
)
_ACTIVE_STATES = (
    VoiceWorkState.PENDING.value,
    VoiceWorkState.RUNNING.value,
)


@dataclass(frozen=True)
class VoiceWorkReceipt:
    id: str
    message_id: str
    text: str
    recovered: bool = False
    scope_id: str | None = None


class VoiceWorkNotPendingError(RuntimeError):
    """The durable receipt was terminalized before this worker claimed it."""


def is_voice_progress_query(text: str) -> bool:
    """Return true only for a complete, short progress/status utterance.

    Substring matching is unsafe here: commands such as "update the project
    status" contain ``status`` but are new work.  These patterns deliberately
    require the whole utterance to be a question about the active work.
    """

    normalized = re.sub(r"[\s，。！？、,.!?;:：；]+", " ", text.strip().casefold()).strip()
    if not normalized or len(normalized) > 80:
        return False
    patterns = (
        r"(?:what(?:'s| is) (?:the )?)?(?:status|progress)(?: (?:now|update))?",
        r"(?:any|got an?) updates?",
        r"how(?:'s| is) (?:it|that|the work|the task) going",
        r"(?:are you|is it|is that) (?:done|finished|still working)",
        r"(?:done|finished) yet",
        r"(?:is there|do you have) (?:an? )?updates?",
        r"(?:现在|目前)?(?:进度|状态)(?:怎么样了?|如何|呢|是什么|到哪了)",
        r"(?:完成|做好|结束)了吗",
        r"(?:好了没|好了吗|查到了吗|结果呢|有消息吗)",
        r"(?:还在|仍在)(?:处理|执行|查询|工作)(?:吗|中吗)",
        r"(?:你|你们)?(?:这|现在)?(?:在)?(?:处理|执行|查询|忙)(?:什么|啥)(?:呢|呀|啊|吗)?",
        r"what (?:are you|is it) (?:doing|working on|processing)",
        r"進捗(?:は)?(?:どう|どうですか|を教えて)",
        r"(?:完了|終わり)ましたか",
        r"진행 상황(?:은)?(?: 어때| 알려줘| 어떻게 돼)",
        r"어떻게 됐(?:어|나요|습니까)?",
        r"(?:완료|끝났)(?:어|나요|습니까)",
    )
    return any(re.fullmatch(pattern, normalized) for pattern in patterns)


def voice_call_control_kind(text: str) -> VoiceControlReplyKind | None:
    """Classify local call-level corrections while background work is active.

    These narrow patterns intentionally avoid an additional model call and do
    not consume substantive instructions. Unknown utterances remain durable
    Chat work.
    """

    normalized = re.sub(r"[\s，。！？、,.!?;:：；]+", " ", text.strip().casefold()).strip()
    if not normalized or len(normalized) > 120:
        return None
    silence_patterns = (
        r"(?:我)?(?:让|叫)你说(?:话)?(?:了)?吗",
        r"(?:别|不要|不用)(?:再)?(?:说话|出声|回复|播报)(?:了|啦|啊|呀)?",
        r"(?:stop|do not|don't) (?:talking|speak|reply|say anything)",
        r"(?:말하지 마|대답하지 마|그만 말해)",
        r"(?:話さないで|返事しないで|黙って)",
    )
    if any(re.fullmatch(pattern, normalized) for pattern in silence_patterns):
        return VoiceControlReplyKind.SILENCE
    language_patterns = (
        r"(?:为什么|怎么)(?:在)?(?:说|用)(?:韩语|韩文|日语|日文|英语|英文|中文)(?:呢|啊|呀|了)?",
        r"why (?:are you|did you) (?:speaking|speak|reply in) [a-z -]+",
        r"왜 (?:한국어|중국어|영어|일본어)(?:로)? (?:말해|대답해)",
        r"なぜ(?:韓国語|中国語|英語|日本語)で(?:話す|答える)の",
    )
    if any(re.fullmatch(pattern, normalized) for pattern in language_patterns):
        return VoiceControlReplyKind.LANGUAGE
    confusion_patterns = (
        r"(?:你)?(?:这|刚才)?(?:在)?说(?:什么|啥)(?:呢|啊|呀)?",
        r"what (?:are you|were you) (?:saying|talking about)",
        r"(?:뭐라고|무슨 말) (?:했어|하는 거야)",
        r"(?:何を|なんて)(?:言ってる|言った)の",
    )
    if (
        any(re.fullmatch(pattern, normalized) for pattern in confusion_patterns)
        or "叽里咕噜" in normalized
        or "乱七八糟" in normalized
    ):
        return VoiceControlReplyKind.CONFUSED
    return None


def _work_meta(message: Message) -> dict:
    meta = message.meta if isinstance(message.meta, dict) else {}
    work = meta.get(VOICE_WORK_META_KEY)
    return dict(work) if isinstance(work, dict) else {}


def _set_work_state(
    message: Message,
    state: VoiceWorkState,
    *,
    error: str | None = None,
) -> None:
    meta = dict(message.meta or {})
    work = _work_meta(message)
    work["state"] = state.value
    work[f"{state.value}_at"] = datetime.now(timezone.utc).isoformat()
    if error:
        work["error"] = error[:500]
    meta[VOICE_WORK_META_KEY] = work
    message.meta = meta


async def admit_voice_work(
    db: AsyncSession,
    *,
    conversation_id: str,
    text: str,
    user_id: str | None,
    public_channel: bool,
    scope_id: str | None = None,
) -> VoiceWorkReceipt:
    """Persist one user instruction before the call claims it as queued."""

    conversation = (
        await db.execute(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise LookupError("Voice conversation not found")
    active_query = select(Message).where(
        Message.conversation_id == conversation.id,
        Message.role == "user",
        Message.meta[VOICE_WORK_META_KEY]["state"].astext.in_(_ACTIVE_STATES),
    )
    if scope_id:
        active_query = active_query.where(
            Message.meta[VOICE_WORK_META_KEY]["scope_id"].astext == scope_id
        )
    active = (
        await db.execute(active_query.limit(VOICE_WORK_RECOVERY_LIMIT))
    ).scalars().all()
    if len(active) >= VOICE_WORK_RECOVERY_LIMIT:
        raise RuntimeError("Voice work queue is full")
    work_id = uuid.uuid4().hex
    work = {
        "id": work_id,
        "state": VoiceWorkState.PENDING.value,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **({"scope_id": scope_id} if scope_id else {}),
    }
    if public_channel:
        from packages.core.services.channel_conversations import (
            add_channel_inbound_message,
        )

        conversation_meta = dict(conversation.meta or {})
        message = await add_channel_inbound_message(
            db,
            conversation_id=conversation.id,
            channel_type=str(conversation.channel or "webchat"),
            sender_id=str(
                conversation_meta.get("sender_id")
                or conversation_meta.get("session_id")
                or "visitor"
            ),
            sender_name=conversation_meta.get("sender_name"),
            chat_id=(
                conversation_meta.get("chat_id")
                or conversation_meta.get("session_id")
            ),
            content=text,
            meta={VOICE_WORK_META_KEY: work},
        )
    else:
        message = await add_message(
            db,
            conversation.id,
            role="user",
            content=text,
            meta={
                **({"author_user_id": user_id} if user_id else {}),
                VOICE_WORK_META_KEY: work,
            },
        )
    await db.commit()
    return VoiceWorkReceipt(
        id=work_id,
        message_id=message.id,
        text=text,
        scope_id=scope_id,
    )


async def claim_voice_work(
    db: AsyncSession,
    receipt: VoiceWorkReceipt,
    *,
    conversation_id: str,
) -> Message:
    message = (
        await db.execute(
            select(Message)
            .where(
                Message.id == receipt.message_id,
                Message.conversation_id == conversation_id,
                Message.role == "user",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if message is None:
        raise LookupError("Voice work receipt not found")
    work = _work_meta(message)
    if (
        work.get("id") != receipt.id
        or work.get("state") != VoiceWorkState.PENDING.value
    ):
        raise VoiceWorkNotPendingError("Voice work is no longer pending")
    _set_work_state(message, VoiceWorkState.RUNNING)
    await db.commit()
    return message


async def finish_voice_work(
    db: AsyncSession,
    receipt: VoiceWorkReceipt,
    *,
    conversation_id: str,
    state: VoiceWorkState | str,
    error: str | None = None,
) -> None:
    try:
        terminal_state = VoiceWorkState.from_value(state)
    except ValueError as error_value:
        raise ValueError("Invalid terminal voice work state") from error_value
    if terminal_state.value not in _TERMINAL_STATES:
        raise ValueError("Invalid terminal voice work state")
    message = (
        await db.execute(
            select(Message)
            .where(
                Message.id == receipt.message_id,
                Message.conversation_id == conversation_id,
                Message.role == "user",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if message is None:
        return
    work = _work_meta(message)
    if work.get("id") != receipt.id or work.get("state") in _TERMINAL_STATES:
        return
    _set_work_state(message, terminal_state, error=error)
    await db.commit()


async def interrupt_voice_work(
    db: AsyncSession,
    receipt: VoiceWorkReceipt,
    *,
    conversation_id: str,
    reason: str,
    superseded_by: str | None = None,
) -> bool:
    """Terminalize work after an explicit foreground stop or replacement."""

    message = (
        await db.execute(
            select(Message)
            .where(
                Message.id == receipt.message_id,
                Message.conversation_id == conversation_id,
                Message.role == "user",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if message is None:
        return False
    work = _work_meta(message)
    if work.get("id") != receipt.id:
        return False
    state = work.get("state")
    if state in _ACTIVE_STATES:
        _set_work_state(message, VoiceWorkState.INTERRUPTED, error=reason)
    elif (
        state == VoiceWorkState.INTERRUPTED.value
        and work.get("superseded_by")
    ):
        displaced_id = str(work["superseded_by"])
        displaced = (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.role == "user",
                    Message.meta[VOICE_WORK_META_KEY]["id"].astext
                    == displaced_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            displaced is None
            or _work_meta(displaced).get("state") not in _ACTIVE_STATES
        ):
            return False
        _set_work_state(displaced, VoiceWorkState.INTERRUPTED, error=reason)
    else:
        return False
    meta = dict(message.meta or {})
    updated = _work_meta(message)
    updated["interrupt_reason"] = str(reason or "voice_control")[:120]
    if superseded_by:
        updated["superseded_by"] = str(superseded_by)[:120]
    else:
        updated.pop("superseded_by", None)
    meta[VOICE_WORK_META_KEY] = updated
    message.meta = meta
    await db.commit()
    return True


async def voice_work_context(
    db: AsyncSession,
    receipt: VoiceWorkReceipt,
    *,
    conversation_id: str,
) -> VoiceWorkContext:
    """Read the active request and latest non-control output for local routing."""

    message = (
        await db.execute(
            select(Message).where(
                Message.id == receipt.message_id,
                Message.conversation_id == conversation_id,
                Message.role == "user",
            )
        )
    ).scalar_one_or_none()
    if message is None or _work_meta(message).get("id") != receipt.id:
        raise LookupError("Voice work receipt not found")
    rows = (
        await db.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == "assistant",
                Message.created_at >= message.created_at,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(8)
        )
    ).scalars().all()
    assistant_output = next(
        (
            str(row.content or "").strip()
            for row in rows
            if str(row.content or "").strip()
            and not bool((row.meta or {}).get("voice_control"))
        ),
        "",
    )
    return VoiceWorkContext(
        request=str(message.content or receipt.text),
        state=VoiceWorkState.from_value(_work_meta(message).get("state")),
        assistant_output=assistant_output,
        superseded_by=str(_work_meta(message).get("superseded_by") or ""),
    )


async def voice_work_was_interrupted(
    db: AsyncSession,
    *,
    message_id: str,
    conversation_id: str,
) -> bool:
    """Check cancellation for one Voice receipt without affecting sibling turns."""

    message = (
        await db.execute(
            select(Message).where(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
                Message.role == "user",
            )
        )
    ).scalar_one_or_none()
    return bool(
        message is not None
        and _work_meta(message).get("state") == VoiceWorkState.INTERRUPTED.value
    )


async def recover_voice_work(
    db: AsyncSession,
    *,
    conversation_id: str,
) -> list[VoiceWorkReceipt]:
    """Return pending work and fail closed for work interrupted mid-run."""

    recovery_query = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.role == "user",
        Message.meta[VOICE_WORK_META_KEY]["state"].astext.in_(_ACTIVE_STATES),
    )
    rows = (
        await db.execute(
            recovery_query.order_by(Message.created_at.asc(), Message.id.asc())
            .limit(VOICE_WORK_RECOVERY_LIMIT)
            .with_for_update()
        )
    ).scalars().all()
    pending: list[VoiceWorkReceipt] = []
    changed = False
    for message in rows:
        work = _work_meta(message)
        work_id = str(work.get("id") or "").strip()
        state = work.get("state")
        if not work_id:
            continue
        if state == VoiceWorkState.PENDING.value:
            created_at = message.created_at
            if created_at is not None:
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                age_seconds = (
                    datetime.now(timezone.utc) - created_at
                ).total_seconds()
                if age_seconds > VOICE_WORK_MAX_RECOVERY_AGE_SECONDS:
                    _set_work_state(
                        message,
                        VoiceWorkState.INTERRUPTED,
                        error="This queued voice instruction expired before it could run.",
                    )
                    changed = True
                    continue
            pending.append(
                VoiceWorkReceipt(
                    id=work_id,
                    message_id=message.id,
                    text=str(message.content or ""),
                    recovered=True,
                    scope_id=str(work.get("scope_id") or "").strip() or None,
                )
            )
        elif state == VoiceWorkState.RUNNING.value:
            _set_work_state(
                message,
                VoiceWorkState.INTERRUPTED,
                error="The server stopped while this voice instruction was running.",
            )
            changed = True
    if changed:
        await db.commit()
    return pending


def validate_voice_origin_message(
    message: Message | None,
    *,
    conversation_id: str,
    content: str,
) -> Message:
    """Validate the internal origin receipt before suppressing a duplicate."""

    work = _work_meta(message) if message is not None else {}
    if (
        message is None
        or message.conversation_id != conversation_id
        or message.role != "user"
        or str(message.content or "").strip() != content.strip()
        or not str(work.get("id") or "").strip()
        or work.get("state") != VoiceWorkState.RUNNING.value
    ):
        raise RuntimeError("Invalid voice work origin")
    return message


async def load_valid_voice_origin_message(
    db: AsyncSession,
    *,
    message_id: str,
    conversation_id: str,
    content: str,
) -> Message:
    """Load and validate the internal origin receipt for a channel turn."""

    message = await db.scalar(select(Message).where(Message.id == message_id))
    return validate_voice_origin_message(
        message,
        conversation_id=conversation_id,
        content=content,
    )
