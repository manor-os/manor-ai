"""Shared chat memory admission from persisted human evidence.

Checkpoints and tool/assistant prose are never extraction inputs. Receipts live
with the original messages, so failed extraction can retry after compaction and
successful empty decisions survive process restarts. No Context Window imports.
"""
from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy import and_, or_, select, text

from packages.core.memory.dedup import existing_memories
from packages.core.memory.manager import run_memory_skill
from packages.core.memory.policy import content_fingerprint
from packages.core.models.task import Conversation, Message
from packages.core.models.runtime_learning import AgentLearningCandidate
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Workspace

logger = logging.getLogger(__name__)
RECEIPTS = "memory_compaction_v1"


class MemoryScopeDenied(PermissionError):
    """Current durable authorization was revoked; stale prompt use must stop."""


async def authorize_memory_scope(db, ctx, *, write=False):
    """Recheck current membership; a cached worker decision grants no access."""
    from packages.core.services.workspace_access import (
        user_can_read_workspace_by_identity, user_can_write_workspace_id,
    )
    user = await db.scalar(select(User).where(
        User.id == ctx.user_id, User.deleted_at.is_(None), User.status == "active",
    ))
    membership = await db.scalar(select(UserMembership).where(
        UserMembership.user_id == ctx.user_id, UserMembership.entity_id == ctx.entity_id,
    ))
    if not user or (membership is not None and (membership.status != "active" or membership.deleted_at)):
        raise MemoryScopeDenied("Memory actor is no longer active")
    if membership is None and user.entity_id != ctx.entity_id:
        raise MemoryScopeDenied("Memory actor is outside the entity")
    role = membership.role if membership else user.role
    conversation = await db.scalar(select(Conversation).where(
        Conversation.id == ctx.conversation_id, Conversation.entity_id == ctx.entity_id,
        Conversation.workspace_id == ctx.workspace_id,
    ))
    if conversation is None:
        raise MemoryScopeDenied("Memory conversation scope is unavailable")
    if not ctx.workspace_id:
        if conversation.user_id != ctx.user_id:
            raise MemoryScopeDenied("Personal memory belongs to another user")
        return None
    workspace = await db.scalar(select(Workspace).where(
        Workspace.id == ctx.workspace_id, Workspace.entity_id == ctx.entity_id,
        Workspace.deleted_at.is_(None),
    ))
    if workspace is None or not await user_can_read_workspace_by_identity(
        db, workspace=workspace, entity_id=ctx.entity_id, user_id=ctx.user_id, role=role,
    ):
        raise MemoryScopeDenied("Workspace memory is no longer readable")
    if write and not await user_can_write_workspace_id(
        db, workspace_id=ctx.workspace_id, entity_id=ctx.entity_id, user_id=ctx.user_id, role=role,
    ):
        return False
    return workspace


def _receipt_key(ctx):
    return hashlib.sha256(json.dumps([ctx.entity_id, ctx.workspace_id, ctx.user_id, ctx.agent_id]).encode()).hexdigest()


def _human_texts(history):
    texts = set()
    for item in history:
        if item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, list):
            content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        if isinstance(content, str) and content.strip() and not content.startswith("[Earlier conversation"):
            texts.add(content)
    return texts


def _mark(message, key, status):
    meta = dict(message.meta or {})
    receipts = dict(meta.get(RECEIPTS) or {})
    receipts[key] = {"status": status, "digest": content_fingerprint(message.content)}
    meta[RECEIPTS] = receipts
    message.meta = meta


async def prepare_compaction_memory(ctx, history, archive_id=None):
    return await prepare_chat_memory(ctx, history, archive_id=archive_id)


async def prepare_chat_memory(ctx, history, *, archive_id=None, source="context_compaction",
                              source_message_ids=None, require_autonomous_runtime=False):
    """Submit reviewable candidates; never auto-apply rules or grant authority."""
    from packages.core.database import async_session
    from packages.core.services.runtime_learning import (
        LearningCandidateDraft, _upsert_learning_candidate, record_runtime_evidence,
        runtime_learning_enabled,
    )
    key = _receipt_key(ctx)
    texts = _human_texts(history)
    scope = dict(entity_id=ctx.entity_id, workspace_id=ctx.workspace_id, agent_id=ctx.agent_id)
    stats = {"messages": 0, "extracted": 0, "pending": 0, "candidate_ids": []}

    async def can_write(db):
        from packages.core.services.workspace_autonomy import workspace_allows_manual_work

        workspace = await authorize_memory_scope(db, ctx, write=True)
        if workspace is False:
            return False
        if workspace is not None and workspace.status != "active" and (
            require_autonomous_runtime or not workspace_allows_manual_work(workspace)
        ):
            return False
        return await runtime_learning_enabled(db, **scope)

    # Workspace messages carry their author in metadata; older 1:1 messages
    # may fall back to the conversation owner. Never borrow another actor.
    authored = or_(
        Message.meta["author_user_id"].astext == ctx.user_id,
        and_(Message.meta["author_user_id"].astext.is_(None), Conversation.user_id == ctx.user_id),
    )
    async with async_session() as db:
        if not await can_write(db):
            return {**stats, "skipped": True}
        # All matched sources are marked pending before the model runs. They
        # remain discoverable even when the next history only has a checkpoint.
        matched = Message.id.in_(source_message_ids) if source_message_ids is not None else Message.content.in_(texts)
        rows = (await db.scalars(select(Message).join(Conversation, Conversation.id == Message.conversation_id).where(
            Message.conversation_id == ctx.conversation_id,
            Message.role == "user", Message.author_kind == "user",
            Message.message_kind == "text", Message.content.is_not(None),
            authored, or_(matched, Message.meta[RECEIPTS][key]["status"].astext == "pending"),
        ).order_by(Message.created_at, Message.id).with_for_update(of=Message))).all()
        pending = []
        size = 0
        for row in rows:
            receipt = ((row.meta or {}).get(RECEIPTS) or {}).get(key, {})
            if receipt == {"status": "done", "digest": content_fingerprint(row.content)}:
                continue
            _mark(row, key, "pending")
            stats["pending"] += 1
            # Whole sources only: an oversized message remains retryable, never
            # silently turns a clipped qualification into a permanent fact.
            item = {"id": row.id, "content": row.content}
            length = len(json.dumps(item, ensure_ascii=False))
            if len(pending) < 80 and size + length <= 60_000:
                pending.append(item)
                size += length
        existing = await existing_memories(
            db, entity_id=ctx.entity_id, workspace_id=ctx.workspace_id,
            user_id=ctx.user_id if not ctx.workspace_id else None,
            agent_id=ctx.agent_id if not ctx.workspace_id else None,
        )
        # Pending proposals also count for duplicate detection, without being
        # presented to the main agent as already-approved facts.
        proposals = (await db.scalars(select(AgentLearningCandidate).where(
            AgentLearningCandidate.entity_id == ctx.entity_id,
            AgentLearningCandidate.workspace_id == ctx.workspace_id,
            AgentLearningCandidate.user_id == ctx.user_id,
            AgentLearningCandidate.agent_id == ctx.agent_id,
            AgentLearningCandidate.candidate_type == "memory",
            AgentLearningCandidate.status.in_(["proposed", "accepted"]),
        ).order_by(AgentLearningCandidate.created_at.desc()).limit(40))).all()
        comparison_size = sum(len(m["content"]) for m in existing)
        for proposal in proposals:
            if comparison_size + len(proposal.summary) <= 12_000:
                existing.append({"id": proposal.id, "content": proposal.summary, "status": proposal.status})
                comparison_size += len(proposal.summary)
        await db.commit()
    if not pending:
        return stats
    decision = await run_memory_skill(
        "extract", {"messages": pending, "source_message_ids": [m["id"] for m in pending], "existing": existing},
        **scope, user_id=ctx.user_id,
    )
    async with async_session() as db:
        if not await can_write(db):
            return {**stats, "skipped": True}
        # Serialize candidate dedup across conversations in the same scope.
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {
            "key": int.from_bytes(bytes.fromhex(key)[:8], "big", signed=True),
        })
        # Lock receipts on admission, not during the worker request. Concurrent
        # compactions can share a cached decision but only one admits each input.
        rows = (await db.scalars(select(Message).join(Conversation, Conversation.id == Message.conversation_id).where(
            Message.conversation_id == ctx.conversation_id,
            Message.id.in_([m["id"] for m in pending]),
            Message.role == "user", Message.author_kind == "user",
            Message.message_kind == "text", authored,
        ).order_by(Message.id).with_for_update(of=Message))).all()
        snapshot = {m["id"]: m["content"] for m in pending}
        eligible = {m.id: m for m in rows if m.content == snapshot[m.id] and
                    ((m.meta or {}).get(RECEIPTS) or {}).get(key, {}).get("status") == "pending"}
        if not eligible:
            return stats
        evidence = await record_runtime_evidence(
            db, **scope, user_id=ctx.user_id, conversation_id=ctx.conversation_id,
            evidence_type="memory_compaction" if source == "context_compaction" else "memory_extraction", source=source,
            summary="Reviewed original human messages for durable memory candidates",
            details={"archive_id": archive_id, "source_message_ids": list(eligible)},
        )
        if evidence is None:
            return stats
        written = 0
        for entry in decision["entries"]:
            source_id = entry["source_message_id"]
            if source_id not in snapshot:
                raise ValueError("Memory evidence is outside the supplied messages")
            if source_id not in eligible:
                continue
            memory_type = entry["scope"] if ctx.workspace_id else {
                "guidance": "instruction", "decision": "context", "learning": "context",
            }.get(entry["scope"], entry["scope"])
            # Recheck active storage at admission, even after a worker cache hit.
            # Concurrent/manual writes cannot create another identical proposal.
            from packages.core.memory.dedup import find_duplicate
            duplicate = await find_duplicate(
                db, entity_id=ctx.entity_id, workspace_id=ctx.workspace_id,
                user_id=ctx.user_id if not ctx.workspace_id else None,
                agent_id=ctx.agent_id if not ctx.workspace_id else None,
                scope=entry["scope"] if ctx.workspace_id else None,
                memory_type=memory_type, content=entry["body"],
            )
            if duplicate:
                continue
            candidate, created = await _upsert_learning_candidate(
                db, LearningCandidateDraft(
                    candidate_type="memory", scope="workspace" if ctx.workspace_id else "user",
                    title=entry["title"], summary=entry["body"], confidence=entry["confidence"],
                    payload={"content": entry["body"], "memory_type": memory_type, "memory_scope": entry["scope"],
                             "source_message_id": source_id, "archive_id": archive_id},
                    dedupe_key=f"compact:{entry['scope']}:{content_fingerprint(entry['body'])}",
                ), **scope, user_id=ctx.user_id, evidence_id=evidence.id,
            )
            written += int(created)
            if candidate:
                stats["candidate_ids"].append(candidate.id)
        for row in eligible.values():
            _mark(row, key, "done")
        await db.commit()
    logger.info("[context-memory] conversation=%s reviewed=%s candidates=%s archive=%s",
                ctx.conversation_id, len(eligible), written, archive_id)
    stats.update(messages=len(eligible), extracted=written, pending=stats["pending"] - len(eligible))
    return stats
