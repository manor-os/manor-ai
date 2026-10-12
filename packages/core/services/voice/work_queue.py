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
from packages.core.services.voice.approvals import VoicePendingAction
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
    pending_action_id: str | None = None
    presented_actions: tuple[VoicePendingAction, ...] = ()


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


def is_voice_backchannel(text: str) -> bool:
    """Return true only for a brief acknowledgement with no instruction."""

    normalized = re.sub(
        r"[\s，。！？、,.!?;:：；]+",
        " ",
        text.strip().casefold(),
    ).strip()
    if not normalized or len(normalized) > 24:
        return False
    patterns = (
        r"(?:嗯+|恩+|唔+|哦+|噢+|好(?:的)?|行|知道了|明白了)",
        r"(?:m+[- ]?h+m+|uh[- ]?huh|uh+|um+|erm+|hmm+|okay|ok|got it|right)",
        r"(?:うん+|はい|ええ|なるほど)",
        r"(?:응+|네+|예+|알겠어|알겠습니다)",
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
    if is_voice_backchannel(normalized):
        return VoiceControlReplyKind.SILENCE
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


def _request_text(message: Message) -> str:
    request = _work_meta(message).get("request_text")
    return request if isinstance(request, str) else str(message.content or "")


def _validate_source_author(message: Message, conversation: Conversation, *, user_id: str | None, public: bool) -> None:
    meta = message.meta or {}
    if meta.get("voice_control") is not True:
        raise ValueError("Voice source is not a saved foreground input")
    if not public:
        if not user_id or meta.get("author_user_id") != user_id:
            raise ValueError("Voice source author does not match")
        return
    conversation_meta = conversation.meta or {}
    sender = str(conversation_meta.get("sender_id") or conversation_meta.get("session_id") or "visitor")
    chat_id = conversation_meta.get("chat_id") or conversation_meta.get("session_id")
    if (
        conversation.channel != "webchat"
        or meta.get("channel_type") != "webchat"
        or meta.get("sender_id") != sender
        or meta.get("chat_id") != chat_id
    ):
        raise ValueError("Voice source visitor does not match")


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
    source_message_ids: tuple[str, ...] = (),
    pending_action_id: str | None = None,
    presented_actions: tuple[VoicePendingAction, ...] = (),
) -> VoiceWorkReceipt:
    """Persist one user instruction before the call claims it as queued."""

    if pending_action_id is not None and (
        not isinstance(pending_action_id, str)
        or not pending_action_id.strip()
        or len(pending_action_id) > 128
    ):
        raise ValueError("Invalid voice pending action ID")
    if len(presented_actions) > 8 or any(
        not isinstance(item, VoicePendingAction) for item in presented_actions
    ):
        raise ValueError("Invalid voice approval presentation context")
    conversation = (
        await db.execute(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise LookupError("Voice conversation not found")
    sources: list[Message] = []
    anchor: Message | None = None
    if source_message_ids:
        if (
            len(source_message_ids) > VOICE_WORK_RECOVERY_LIMIT
            or any(not isinstance(item, str) or not item.strip() for item in source_message_ids)
            or len(set(source_message_ids)) != len(source_message_ids)
        ):
            raise ValueError("Invalid voice source message IDs")
        rows = list((await db.execute(
            select(Message).where(
                Message.id.in_(source_message_ids),
                Message.conversation_id == conversation.id,
                Message.role == "user",
            ).order_by(Message.created_at.asc(), Message.id.asc()).with_for_update()
            .execution_options(populate_existing=True)
        )).scalars().all())
        if len(rows) != len(source_message_ids):
            raise ValueError("Voice sources are missing or out of scope")
        by_id = {message.id: message for message in rows}
        # This order comes only from Browser's committed-input ledger, never
        # from provider arguments or client requests. ASR may persist out of order.
        sources = [by_id[source_id] for source_id in source_message_ids]
        for source in sources:
            _validate_source_author(source, conversation, user_id=user_id, public=public_channel)
        request_text = "\n".join(source.content for source in sources if source.content)
        if not request_text.strip() or request_text != text:
            raise ValueError("Voice request does not match its original source messages")
        # The newest persisted row anchors response history and cancellation,
        # independently of the authoritative capture order of request_text.
        anchor = rows[-1]
        existing = _work_meta(anchor)
        if existing:
            if (
                existing.get("source_message_ids") == list(source_message_ids)
                and existing.get("request_text") == request_text
                and existing.get("scope_id") == scope_id
                and existing.get("pending_action_id") == pending_action_id
                and existing.get("presented_actions", []) == [item.to_record() for item in presented_actions]
                and existing.get("id")
                and all(
                    (source.meta or {}).get("voice_work_source_id") == existing["id"]
                    and not _work_meta(source)
                    for source in sources if source is not anchor
                )
            ):
                return VoiceWorkReceipt(
                    existing["id"], anchor.id, request_text, scope_id=scope_id,
                    pending_action_id=pending_action_id, presented_actions=presented_actions,
                )
            raise ValueError("Voice source already belongs to different work")
        if any(_work_meta(source) or (source.meta or {}).get("voice_work_source_id") for source in sources):
            raise ValueError("Voice source already belongs to different work")
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
        **({"pending_action_id": pending_action_id} if pending_action_id else {}),
        **({"presented_actions": [item.to_record() for item in presented_actions]} if presented_actions else {}),
    }
    if anchor is not None:
        message = anchor
        work.update(request_text=text, source_message_ids=list(source_message_ids))
        message.meta = {**(message.meta or {}), VOICE_WORK_META_KEY: work}
        for source in sources:
            if source is not anchor:
                source.meta = {**(source.meta or {}), "voice_work_source_id": work_id}
    elif public_channel:
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
        pending_action_id=pending_action_id,
        presented_actions=presented_actions,
    )


async def claim_voice_work(
    db: AsyncSession,
    receipt: VoiceWorkReceipt,
    *,
    conversation_id: str,
    expected_author_user_id: str | None = None,
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
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if message is None:
        raise LookupError("Voice work receipt not found")
    if expected_author_user_id is not None and (
        not isinstance(expected_author_user_id, str)
        or not expected_author_user_id.strip()
        or expected_author_user_id != expected_author_user_id.strip()
        or (message.meta or {}).get("author_user_id") != expected_author_user_id
    ):
        raise VoiceWorkNotPendingError("Voice work belongs to a different author")
    work = _work_meta(message)
    if (
        work.get("id") != receipt.id
        or work.get("state") != VoiceWorkState.PENDING.value
        or work.get("pending_action_id") != receipt.pending_action_id
        or work.get("presented_actions", []) != [item.to_record() for item in receipt.presented_actions]
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
        request=_request_text(message) or receipt.text,
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
    expected_author_user_id: str | None = None,
) -> list[VoiceWorkReceipt]:
    """Return pending work and fail closed for work interrupted mid-run."""

    recovery_query = select(Message).where(
        Message.conversation_id == conversation_id,
        Message.role == "user",
        Message.meta[VOICE_WORK_META_KEY]["state"].astext.in_(_ACTIVE_STATES),
    )
    if expected_author_user_id is not None:
        if (
            not isinstance(expected_author_user_id, str)
            or not expected_author_user_id.strip()
            or expected_author_user_id != expected_author_user_id.strip()
        ):
            raise ValueError("Invalid voice work author")
        # Filter before the limit and terminal-state cleanup: a new caller
        # must neither execute nor interrupt another member's queued work.
        # JSONB containment preserves the author field's string type.
        recovery_query = recovery_query.where(
            Message.meta.contains({"author_user_id": expected_author_user_id})
        )
    rows = (
        await db.execute(
            recovery_query.order_by(Message.created_at.asc(), Message.id.asc())
            .limit(VOICE_WORK_RECOVERY_LIMIT)
            .with_for_update()
            .execution_options(populate_existing=True)
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
            try:
                records = work.get("presented_actions", [])
                if not isinstance(records, list) or len(records) > 8:
                    raise ValueError("Invalid voice approval presentation context")
                presented_actions = tuple(VoicePendingAction.from_record(item) for item in records)
                target_id = work.get("pending_action_id")
                if target_id is not None and (
                    not isinstance(target_id, str) or not target_id.strip() or len(target_id) > 128
                ):
                    raise ValueError("Invalid voice pending action ID")
            except (KeyError, TypeError, ValueError):
                _set_work_state(
                    message, VoiceWorkState.INTERRUPTED,
                    error="The queued voice approval context is invalid. Please answer its current Chat card again.",
                )
                changed = True
                continue
            pending.append(
                VoiceWorkReceipt(
                    id=work_id,
                    message_id=message.id,
                    text=_request_text(message),
                    recovered=True,
                    scope_id=str(work.get("scope_id") or "").strip() or None,
                    pending_action_id=target_id,
                    presented_actions=presented_actions,
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
    expected_author_user_id: str | None = None,
) -> Message:
    """Validate the internal origin receipt before suppressing a duplicate."""

    work = _work_meta(message) if message is not None else {}
    if (
        message is None
        or message.conversation_id != conversation_id
        or message.role != "user"
        or _request_text(message).strip() != content.strip()
        or not str(work.get("id") or "").strip()
        or work.get("state") != VoiceWorkState.RUNNING.value
        or expected_author_user_id is not None and (
            not isinstance(expected_author_user_id, str)
            or not expected_author_user_id.strip()
            or expected_author_user_id != expected_author_user_id.strip()
            or (message.meta or {}).get("author_user_id") != expected_author_user_id
        )
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
