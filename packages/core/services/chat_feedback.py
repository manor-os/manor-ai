"""Ordered persistence for thumbs feedback on assistant chat messages."""
from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import and_, delete, desc, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.chat_feedback import ChatMessageFeedback
from packages.core.models.task import Conversation, Message
from packages.core.services.conversation_visibility import (
    visible_user_request_predicate,
)


class ChatFeedbackMutationStatus(StrEnum):
    ACCEPTED = "accepted"


class ChatFeedbackRating(StrEnum):
    UP = "up"
    DOWN = "down"


class ChatFeedbackTargetKind(StrEnum):
    RESPONSE = "response"
    TASK_COMPLETION = "task_completion"
    PLAN_COMPLETION = "plan_completion"
    NONE = "none"


class ChatFeedbackEvidenceType(StrEnum):
    TASK_COMPLETION = "task_completion_feedback"
    PLAN_COMPLETION = "plan_completion_feedback"


COMPLETION_FEEDBACK_EVIDENCE_TYPES = tuple(
    evidence_type.value for evidence_type in ChatFeedbackEvidenceType
)


def _completion_feedback_lock_key(identity: str) -> int:
    digest = hashlib.sha256(
        f"chat-completion-feedback\0{identity}".encode()
    ).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


async def lock_completion_feedback_subject(
    db: AsyncSession,
    *,
    task_id: str | None = None,
    plan_id: str | None = None,
) -> None:
    """Serialize completion feedback with deletion of its owning Task."""
    identity = task_id or plan_id
    if not identity:
        return
    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if bind is not None and bind.dialect.name == "postgresql":
        await db.execute(select(func.pg_advisory_xact_lock(
            _completion_feedback_lock_key(identity)
        )))


class ChatFeedbackIntegrityErrorKind(StrEnum):
    TARGET_DELETED = "target_deleted"
    UNEXPECTED = "unexpected"


class ChatFeedbackTargetDeletedError(RuntimeError):
    """The rated message disappeared before feedback could be serialized."""


@dataclass(frozen=True)
class ChatFeedbackTargetDecision:
    eligible: bool
    detail: str


@dataclass(frozen=True)
class ChatFeedbackSubject:
    target_kind: ChatFeedbackTargetKind
    target_id: str
    task_id: str | None = None
    plan_id: str | None = None


class AbstractChatFeedbackTargetPolicy(ABC):
    """Server-authoritative eligibility contract for one feedback kind."""

    @abstractmethod
    def evaluate(
        self,
        message: Message,
        *,
        workspace_scoped: bool,
    ) -> ChatFeedbackTargetDecision:
        raise NotImplementedError


def _feedback_ref_id(message: Message, kind: str) -> str | None:
    refs = message.refs if isinstance(message.refs, list) else []
    return next(
        (
            str(ref.get("id") or "").strip()
            for ref in refs
            if isinstance(ref, dict)
            and str(ref.get("type") or "").strip().lower() == kind
            and str(ref.get("id") or "").strip()
        ),
        None,
    )


def classify_chat_feedback_target_kind(message: Message) -> ChatFeedbackTargetKind:
    """Resolve the declared feedback contract, failing closed on unknown kinds."""
    meta = message.meta if isinstance(message.meta, dict) else {}
    declared_target = meta.get("feedback_target_kind")
    if declared_target is None:
        if message.message_kind == "agent_update" and (
            _feedback_ref_id(message, "task")
            or _feedback_ref_id(message, "plan")
        ):
            return ChatFeedbackTargetKind.NONE
        return ChatFeedbackTargetKind.RESPONSE
    try:
        return ChatFeedbackTargetKind(str(declared_target))
    except ValueError:
        return ChatFeedbackTargetKind.NONE


def is_task_completion_feedback_target(message: Message) -> bool:
    return bool(
        message.role == "assistant"
        and message.author_kind == "agent"
        and message.message_kind == "agent_update"
        and classify_chat_feedback_target_kind(message)
        == ChatFeedbackTargetKind.TASK_COMPLETION
        and (_feedback_ref_id(message, "task") or _feedback_ref_id(message, "plan"))
    )


def is_completion_feedback_target(message: Message) -> bool:
    return classify_chat_feedback_target_kind(message) in {
        ChatFeedbackTargetKind.TASK_COMPLETION,
        ChatFeedbackTargetKind.PLAN_COMPLETION,
    }


class ResponseChatFeedbackTargetPolicy(AbstractChatFeedbackTargetPolicy):
    def evaluate(
        self,
        message: Message,
        *,
        workspace_scoped: bool,
    ) -> ChatFeedbackTargetDecision:
        eligible = bool(
            message.role == "assistant"
            and message.author_kind != "system"
            and classify_chat_feedback_target_kind(message)
            == ChatFeedbackTargetKind.RESPONSE
            and (not workspace_scoped or message.author_kind == "agent")
        )
        return ChatFeedbackTargetDecision(
            eligible=eligible,
            detail=(
                ""
                if eligible
                else "Message is not eligible for response feedback"
            ),
        )


class TaskCompletionChatFeedbackTargetPolicy(AbstractChatFeedbackTargetPolicy):
    def evaluate(
        self,
        message: Message,
        *,
        workspace_scoped: bool,
    ) -> ChatFeedbackTargetDecision:
        eligible = (
            workspace_scoped
            and is_task_completion_feedback_target(message)
        )
        return ChatFeedbackTargetDecision(
            eligible=eligible,
            detail=(
                ""
                if eligible
                else "Message is not eligible for task-completion feedback"
            ),
        )


class PlanCompletionChatFeedbackTargetPolicy(AbstractChatFeedbackTargetPolicy):
    def evaluate(
        self,
        message: Message,
        *,
        workspace_scoped: bool,
    ) -> ChatFeedbackTargetDecision:
        eligible = bool(
            workspace_scoped
            and message.role == "assistant"
            and message.author_kind == "agent"
            and message.message_kind == "agent_update"
            and classify_chat_feedback_target_kind(message)
            == ChatFeedbackTargetKind.PLAN_COMPLETION
            and _feedback_ref_id(message, "plan")
        )
        return ChatFeedbackTargetDecision(
            eligible=eligible,
            detail=(
                ""
                if eligible
                else "Message is not eligible for plan-completion feedback"
            ),
        )


class NonFeedbackTargetPolicy(AbstractChatFeedbackTargetPolicy):
    def evaluate(
        self,
        message: Message,
        *,
        workspace_scoped: bool,
    ) -> ChatFeedbackTargetDecision:
        _ = (message, workspace_scoped)
        return ChatFeedbackTargetDecision(
            eligible=False,
            detail="Message is not eligible for feedback",
        )


class ChatFeedbackTargetPolicyFactory:
    _POLICIES: dict[ChatFeedbackTargetKind, AbstractChatFeedbackTargetPolicy] = {
        ChatFeedbackTargetKind.RESPONSE: ResponseChatFeedbackTargetPolicy(),
        ChatFeedbackTargetKind.TASK_COMPLETION: (
            TaskCompletionChatFeedbackTargetPolicy()
        ),
        ChatFeedbackTargetKind.PLAN_COMPLETION: (
            PlanCompletionChatFeedbackTargetPolicy()
        ),
        ChatFeedbackTargetKind.NONE: NonFeedbackTargetPolicy(),
    }

    @classmethod
    def create(
        cls,
        kind: ChatFeedbackTargetKind,
    ) -> AbstractChatFeedbackTargetPolicy:
        return cls._POLICIES[kind]


_LIFECYCLE_FOREIGN_KEY_CONSTRAINTS = frozenset(
    {
        "fk_chat_feedback_user",
        "fk_chat_feedback_conversation",
        "fk_chat_feedback_message",
    }
)


def classify_chat_feedback_integrity_error(
    error: IntegrityError,
) -> ChatFeedbackIntegrityErrorKind:
    """Classify only named lifecycle foreign-key races as deleted targets."""
    stack: list[BaseException] = [error]
    visited: set[int] = set()
    sqlstates: set[str] = set()
    constraint_names: set[str] = set()
    while stack:
        current = stack.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        sqlstate = getattr(current, "sqlstate", None) or getattr(
            current,
            "pgcode",
            None,
        )
        if sqlstate:
            sqlstates.add(str(sqlstate))
        constraint_name = getattr(current, "constraint_name", None)
        diag = getattr(current, "diag", None)
        if not constraint_name and diag is not None:
            constraint_name = getattr(diag, "constraint_name", None)
        if constraint_name:
            constraint_names.add(str(constraint_name))
        for nested in (
            getattr(current, "orig", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(nested, BaseException):
                stack.append(nested)

    if (
        "23503" in sqlstates
        and constraint_names.intersection(_LIFECYCLE_FOREIGN_KEY_CONSTRAINTS)
    ):
        return ChatFeedbackIntegrityErrorKind.TARGET_DELETED
    return ChatFeedbackIntegrityErrorKind.UNEXPECTED


@dataclass(frozen=True)
class ChatFeedbackMutationResult:
    rating: ChatFeedbackRating
    mutation_sequence: int
    mutation_status: ChatFeedbackMutationStatus
    updated_at: datetime


@dataclass(frozen=True)
class ChatFeedbackSnapshot:
    message_id: str
    rating: ChatFeedbackRating
    mutation_sequence: int
    target_kind: ChatFeedbackTargetKind
    target_id: str
    task_id: str | None
    plan_id: str | None


_MANOR_FINAL_RESPONSE_OPEN_TAG = "<manor-final-response>"
_MANOR_FINAL_RESPONSE_TAG_RE = re.compile(
    r"</?manor-final-response>\s*",
    re.IGNORECASE,
)
_EDITOR_LIVE_PROTOCOL_BLOCK_RE = re.compile(
    r"<manor(?:-|\s+)live(?:-|\s+)(?:patch|edit)(?:\s[^>]*)?>"
    r"[\s\S]*?"
    r"</manor(?:-|\s+)live(?:-|\s+)(?:patch|edit)\s*>",
    re.IGNORECASE,
)
_EDITOR_LIVE_OPEN_BLOCK_RE = re.compile(
    r"<manor(?:-|\s+)live(?:-|\s+)(?:patch|edit)(?:\s[^>]*)?>[\s\S]*$",
    re.IGNORECASE,
)
_EDITOR_LIVE_TAG_RE = re.compile(
    r"</?manor(?:-|\s+)live(?:-|\s+)(?:patch|edit)(?:\s[^>]*)?>",
    re.IGNORECASE,
)
_HIDDEN_EDITOR_LIVE_TAG_PREFIXES = (
    "<manor-live-patch",
    "</manor-live-patch",
    "<manor-live-edit",
    "</manor-live-edit",
    "<manor live patch",
    "</manor live patch",
    "<manor live edit",
    "</manor live edit",
)


def _strip_trailing_editor_live_tag_fragment(text: str) -> str:
    tag_start = text.rfind("<")
    if tag_start < 0:
        return text
    fragment = text[tag_start:].lower()
    if not fragment or ">" in fragment:
        return text
    if not fragment.startswith(("<manor", "</manor")):
        return text
    if any(
        prefix.startswith(fragment) or fragment.startswith(prefix)
        for prefix in _HIDDEN_EDITOR_LIVE_TAG_PREFIXES
    ):
        return text[:tag_start]
    return text


def visible_assistant_text(value: object) -> str:
    """Project persisted assistant text onto the user-visible text contract."""
    source = value if isinstance(value, str) else ""
    without_protocol = _EDITOR_LIVE_PROTOCOL_BLOCK_RE.sub("", source)
    without_open_protocol = _EDITOR_LIVE_OPEN_BLOCK_RE.sub(
        "", without_protocol
    )
    without_protocol_tags = _EDITOR_LIVE_TAG_RE.sub(
        "", without_open_protocol
    )
    visible_source = _strip_trailing_editor_live_tag_fragment(
        without_protocol_tags
    ).strip()
    marker_index = visible_source.lower().rfind(
        _MANOR_FINAL_RESPONSE_OPEN_TAG
    )
    visible_start = marker_index + len(_MANOR_FINAL_RESPONSE_OPEN_TAG)
    visible = (
        visible_source[visible_start:]
        if marker_index >= 0
        else visible_source
    )
    return _MANOR_FINAL_RESPONSE_TAG_RE.sub("", visible).strip()


def build_chat_feedback_content_preview(
    *,
    requested_preview: str | None,
    message_content: str | None,
    assistant_blocks: list[dict] | None,
) -> str | None:
    """Return the visible assistant answer used by feedback review."""

    def bounded(value: object) -> str | None:
        text = visible_assistant_text(value)
        return text[:1000] if text else None

    text_blocks = [
        block
        for block in assistant_blocks or []
        if isinstance(block, dict)
        and block.get("type") == "text"
        and bounded(block.get("text"))
    ]
    final_parts = [
        str(block["text"]).strip()
        for block in text_blocks
        if block.get("phase") == "final"
    ]
    final = bounded("\n\n".join(final_parts))
    compact_final = bounded("".join(final_parts))
    direct = bounded(message_content)
    if final:
        direct_contains_final = bool(
            direct
            and compact_final
            and len(direct) > len(compact_final)
            and (
                direct.endswith(final)
                or direct.endswith(compact_final)
            )
        )
        if direct_contains_final:
            return direct
        return final
    if direct:
        return direct

    structured = bounded(
        "\n\n".join(str(block["text"]).strip() for block in text_blocks)
    )
    return structured or bounded(requested_preview)


async def resolve_chat_feedback_request_preview(
    db: AsyncSession,
    *,
    message: Message,
    requested_preview: str | None,
) -> str | None:
    """Resolve the preceding user request from the message's conversation.

    The persisted conversation is authoritative. Imported conversations may
    supply the client preview when their source request is not in the message
    table.
    """

    meta = message.meta if isinstance(message.meta, dict) else {}
    runtime = meta.get("runtime") if isinstance(meta.get("runtime"), dict) else {}
    runtime_metadata = (
        runtime.get("metadata")
        if isinstance(runtime.get("metadata"), dict)
        else {}
    )
    origin_user_message_id = next(
        (
            str(value).strip()
            for value in (
                meta.get("origin_user_message_id"),
                runtime.get("origin_user_message_id"),
                runtime_metadata.get("origin_user_message_id"),
            )
            if value and str(value).strip()
        ),
        None,
    )
    if origin_user_message_id:
        causal_value = (
            await db.execute(
                select(Message.content).where(
                    Message.id == origin_user_message_id,
                    Message.conversation_id == message.conversation_id,
                    visible_user_request_predicate(
                        Message.role,
                        Message.content,
                    ),
                )
            )
        ).scalar_one_or_none()
        causal_text = (
            causal_value.strip() if isinstance(causal_value, str) else ""
        )
        if causal_text:
            return causal_text[:1000]

    value = (
        await db.execute(
            select(Message.content)
            .where(
                Message.conversation_id == message.conversation_id,
                or_(
                    Message.created_at < message.created_at,
                    and_(
                        Message.created_at == message.created_at,
                        Message.id < message.id,
                    ),
                ),
                visible_user_request_predicate(
                    Message.role,
                    Message.content,
                ),
            )
            .order_by(desc(Message.created_at), desc(Message.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    text = value.strip() if isinstance(value, str) else ""
    if text:
        return text[:1000]

    fallback = requested_preview.strip() if isinstance(requested_preview, str) else ""
    return fallback[:1000] if fallback else None


async def list_chat_message_feedback(
    db: AsyncSession,
    *,
    user_id: str,
    conversation_id: str,
    workspace_id: str | None = None,
) -> list[ChatFeedbackSnapshot]:
    scope_predicate = ChatMessageFeedback.conversation_id == conversation_id
    if workspace_id:
        scope_predicate = or_(
            ChatMessageFeedback.conversation_id == conversation_id,
            and_(
                Conversation.workspace_id == workspace_id,
                ChatMessageFeedback.target_kind.in_(
                    (
                        ChatFeedbackTargetKind.TASK_COMPLETION.value,
                        ChatFeedbackTargetKind.PLAN_COMPLETION.value,
                    )
                ),
            ),
        )
    rows = (
        await db.execute(
            select(
                ChatMessageFeedback.message_id,
                ChatMessageFeedback.rating,
                ChatMessageFeedback.mutation_sequence,
                ChatMessageFeedback.target_kind,
                ChatMessageFeedback.target_id,
                ChatMessageFeedback.task_id,
                ChatMessageFeedback.plan_id,
            )
            .join(
                Conversation,
                Conversation.id == ChatMessageFeedback.conversation_id,
            )
            .where(
                ChatMessageFeedback.user_id == user_id,
                scope_predicate,
            )
            .order_by(ChatMessageFeedback.created_at, ChatMessageFeedback.message_id)
        )
    ).all()
    return [
        ChatFeedbackSnapshot(
            message_id=row.message_id,
            rating=ChatFeedbackRating(row.rating),
            mutation_sequence=row.mutation_sequence,
            target_kind=ChatFeedbackTargetKind(row.target_kind),
            target_id=row.target_id,
            task_id=row.task_id,
            plan_id=row.plan_id,
        )
        for row in rows
    ]


async def delete_task_completion_feedback(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    task_id: str,
) -> None:
    """Remove every completion-feedback projection owned by one Task.

    The caller must acquire ``lock_completion_feedback_subject`` before it
    locks the Task row. That order makes a concurrent rating either finish
    before deletion or revalidate the deleted Task and fail closed.
    """
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.runtime_learning import RuntimeEvidence

    plan_ids = list((await db.execute(
        select(ExecutionPlan.id).where(
            ExecutionPlan.entity_id == entity_id,
            ExecutionPlan.workspace_id == workspace_id,
            ExecutionPlan.task_id == task_id,
        )
    )).scalars().all())
    subject_ids = [task_id, *plan_ids]

    message_ref_predicates = [
        Message.refs.contains([{"type": "task", "id": task_id}]),
        *(
            Message.refs.contains([{"type": "plan", "id": plan_id}])
            for plan_id in plan_ids
        ),
    ]
    messages = (await db.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
            or_(*message_ref_predicates),
        )
        .order_by(Message.id)
        .with_for_update(of=Message)
    )).scalars().all()
    for message in messages:
        meta = dict(message.meta or {})
        if not is_completion_feedback_target(message):
            continue
        meta["feedback_target_kind"] = ChatFeedbackTargetKind.NONE.value
        message.meta = meta

    feedback_lineage = [
        ChatMessageFeedback.task_id == task_id,
        and_(
            ChatMessageFeedback.target_kind.in_(
                (
                    ChatFeedbackTargetKind.TASK_COMPLETION.value,
                    ChatFeedbackTargetKind.PLAN_COMPLETION.value,
                )
            ),
            ChatMessageFeedback.target_id.in_(subject_ids),
        ),
    ]
    if plan_ids:
        feedback_lineage.append(ChatMessageFeedback.plan_id.in_(plan_ids))
    await db.execute(
        delete(ChatMessageFeedback).where(
            ChatMessageFeedback.entity_id == entity_id,
            or_(*feedback_lineage),
        )
    )

    evidence_lineage = [
        RuntimeEvidence.task_id == task_id,
        RuntimeEvidence.details["task_id"].as_string() == task_id,
        RuntimeEvidence.details["target_id"].as_string().in_(subject_ids),
    ]
    if plan_ids:
        evidence_lineage.append(
            RuntimeEvidence.details["plan_id"].as_string().in_(plan_ids)
        )
    await db.execute(
        delete(RuntimeEvidence).where(
            RuntimeEvidence.entity_id == entity_id,
            RuntimeEvidence.workspace_id == workspace_id,
            RuntimeEvidence.evidence_type.in_(
                COMPLETION_FEEDBACK_EVIDENCE_TYPES
            ),
            or_(*evidence_lineage),
        )
    )


async def persist_chat_message_feedback(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    message_id: str,
    rating: ChatFeedbackRating,
    content_preview: str | None,
    request_preview: str | None,
    target_kind: ChatFeedbackTargetKind,
    target_id: str,
    task_id: str | None = None,
    plan_id: str | None = None,
    commit: bool = True,
) -> ChatFeedbackMutationResult:
    """Persist one canonical rating in database receive order."""
    locked_message_id = await db.scalar(
        select(Message.id)
        .where(
            Message.id == message_id,
            Message.conversation_id == conversation_id,
        )
        .with_for_update()
    )
    if locked_message_id is None:
        raise ChatFeedbackTargetDeletedError(message_id)

    mutation_time = func.clock_timestamp()
    metadata = {
        "target_kind": target_kind.value,
        "target_id": target_id,
        "task_id": task_id,
        "plan_id": plan_id,
    }
    feedback_table = ChatMessageFeedback.__table__
    values = {
        "id": generate_ulid(),
        "entity_id": entity_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "target_kind": target_kind.value,
        "target_id": target_id,
        "task_id": task_id,
        "plan_id": plan_id,
        "rating": rating,
        "content_preview": content_preview,
        "request_preview": request_preview,
        "mutation_sequence": 1,
        "metadata": metadata,
        "updated_at": mutation_time,
    }
    returning = (
        ChatMessageFeedback.rating,
        ChatMessageFeedback.mutation_sequence,
        ChatMessageFeedback.updated_at,
    )
    insert_stmt = pg_insert(feedback_table).values(**values)
    update_values = {
        "entity_id": entity_id,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "target_kind": target_kind.value,
        "target_id": target_id,
        "task_id": task_id,
        "plan_id": plan_id,
        "rating": rating,
        "content_preview": content_preview,
        "request_preview": request_preview,
        "metadata": metadata,
        "mutation_sequence": func.coalesce(
            ChatMessageFeedback.mutation_sequence,
            0,
        ) + 1,
        "updated_at": mutation_time,
    }
    row = (
        await db.execute(
            insert_stmt.on_conflict_do_update(
                index_elements=(
                    feedback_table.c.target_kind,
                    feedback_table.c.target_id,
                    feedback_table.c.user_id,
                ),
                set_=update_values,
            ).returning(*returning)
        )
    ).one()

    if commit:
        await db.commit()
    return ChatFeedbackMutationResult(
        rating=ChatFeedbackRating(row.rating),
        mutation_sequence=row.mutation_sequence,
        mutation_status=ChatFeedbackMutationStatus.ACCEPTED,
        updated_at=row.updated_at,
    )
