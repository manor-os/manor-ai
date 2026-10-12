"""Private continuation state for a Workspace service Agent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.agent_delegation_session import AgentContextArchive, AgentDelegationSession
from packages.core.ai.runtime.context_window import classify_context_messages, context_digest
from packages.core.models.base import generate_ulid


class DelegationSessionBusy(Exception):
    pass


class DelegationSessionInterrupted(Exception):
    pass


# Full answers of an Agent's latest delegated runs in one chat, readable by
# run_id after the parent saw a shortened copy.
DELEGATION_RESULTS_KEPT = 20
DELEGATION_RESULT_STORED_CHARS = 400_000


async def find_delegation_result(
    db: AsyncSession, *, entity_id: str, workspace_id: str, conversation_id: str,
    user_id: str, run_id: str,
) -> tuple[AgentDelegationSession, dict[str, Any]] | None:
    """A stored run result, only within the chat and user that ran it."""
    sessions = (await db.execute(select(AgentDelegationSession).where(
        AgentDelegationSession.entity_id == entity_id,
        AgentDelegationSession.workspace_id == workspace_id,
        AgentDelegationSession.conversation_id == conversation_id,
        AgentDelegationSession.user_id == user_id,
    ))).scalars().all()
    for session in sessions:
        for item in (session.context_state or {}).get("results") or []:
            if isinstance(item, dict) and item.get("run_id") == run_id:
                return session, item
    return None


async def claim_delegation_session(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    conversation_id: str,
    user_id: str,
    subscription: Any,
    session_id: str | None = None,
    continue_interrupted: bool = False,
) -> tuple[AgentDelegationSession, str, list[dict[str, Any]]]:
    """Claim one exact child identity, leaving completed turns available to resume."""

    candidate_id = generate_ulid()
    await db.execute(
        insert(AgentDelegationSession)
        .values(
            id=candidate_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            user_id=user_id,
            agent_subscription_id=subscription.id,
            agent_id=subscription.agent_id,
            service_key=subscription.service_key,
            messages=[],
            context_usage={},
            context_state={},
            turn_count=0,
            status="idle",
        )
        .on_conflict_do_nothing(constraint="uq_agent_delegation_session_scope")
    )
    session = (
        await db.execute(
            select(AgentDelegationSession)
            .where(
                AgentDelegationSession.conversation_id == conversation_id,
                AgentDelegationSession.user_id == user_id,
                AgentDelegationSession.agent_subscription_id == subscription.id,
            )
            .with_for_update()
        )
    ).scalar_one()
    if (
        session.entity_id != entity_id
        or session.workspace_id != workspace_id
        or session.agent_id != subscription.agent_id
        or session.service_key != subscription.service_key
        or (session_id and session.id != session_id)
    ):
        raise ValueError("Delegation session no longer matches the requested service scope.")

    now = datetime.now(timezone.utc)
    if session.status == "running":
        if session.active_until and session.active_until > now:
            raise DelegationSessionBusy("This service Agent is still working in this conversation.")
        session.status = "interrupted"
        session.active_run_id = None
        session.active_until = None
    if session.status == "interrupted" and not continue_interrupted:
        raise DelegationSessionInterrupted(
            "The previous delegated turn ended without a verified receipt. "
            "Check its effects before continuing; then pass continue_interrupted=true."
        )

    prior_messages = [dict(message) for message in session.messages if isinstance(message, dict)]
    run_id = generate_ulid()
    session.status = "running"
    session.active_run_id = run_id
    session.active_until = now + timedelta(hours=2)
    await db.commit()
    return session, run_id, prior_messages


async def finish_delegation_session(
    db: AsyncSession,
    *,
    session_id: str,
    run_id: str,
    messages: list[dict[str, Any]] | None,
    context_usage: dict[str, Any] | None,
    context_sources: list[dict[str, Any]] | None = None,
    run_result: dict[str, Any] | None = None,
) -> None:
    """Close the run; ``run_result`` keeps its full answer readable by run_id."""
    session = (
        await db.execute(
            select(AgentDelegationSession)
            .where(AgentDelegationSession.id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (session is None or session.status != "running" or session.active_run_id != run_id
        or not session.active_until or session.active_until <= datetime.now(timezone.utc)):
        raise RuntimeError("Delegated session is no longer active.")
    if messages is not None:
        # The system prompt and tool catalog are rebuilt under current bindings
        # on every turn. They are never replayed as a grant from old state.
        session.messages = [
            dict(message) for message in messages
            if isinstance(message, dict) and message.get("role") not in {"system", "developer"}
        ]
        session.turn_count += 1
        session.context_usage = dict(context_usage or {})
        session.context_state = {
            **(session.context_state or {}), "version": 1,
            "sources": list(context_sources or []),
            "history": classify_context_messages(session.messages),
        }
    if run_result is not None:
        content = str(run_result.get("content") or "")
        kept = [
            item for item in (session.context_state or {}).get("results") or []
            if isinstance(item, dict) and item.get("run_id") != run_id
        ][-(DELEGATION_RESULTS_KEPT - 1):]
        session.context_state = {
            **(session.context_state or {}),
            "results": [*kept, {
                **run_result,
                "run_id": run_id,
                "content": content[:DELEGATION_RESULT_STORED_CHARS],
                "total_chars": len(content),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }],
        }
    # A run whose last action may or may not have happened parks the session
    # exactly like an interrupted turn: the next delegation must explicitly
    # continue after checking the effect, never replay it silently.
    session.status = (
        "interrupted"
        if isinstance(run_result, dict) and run_result.get("do_not_replay") is True
        else "idle"
    )
    session.active_run_id = None
    session.active_until = None
    await db.commit()


async def archive_delegation_context(
    db: AsyncSession, *, session_id: str, run_id: str, messages: list[dict],
) -> str:
    session = (await db.execute(select(AgentDelegationSession).where(
        AgentDelegationSession.id == session_id,
        AgentDelegationSession.status == "running",
        AgentDelegationSession.active_run_id == run_id,
        AgentDelegationSession.active_until > datetime.now(timezone.utc),
    ).with_for_update())).scalar_one_or_none()
    if session is None:
        raise RuntimeError("Delegated session is no longer active.")
    history = [dict(m) for m in messages if m.get("role") not in {"system", "developer"}]
    digest = context_digest(history)
    previous = (await db.execute(select(AgentContextArchive.id).where(
        AgentContextArchive.session_id == session_id, AgentContextArchive.digest == digest,
    ))).scalar_one_or_none()
    if previous:
        return previous
    archive_id = generate_ulid()
    db.add(AgentContextArchive(
        id=archive_id, session_id=session_id, run_id=run_id, digest=digest,
        messages=history, categories=classify_context_messages(history, storage="archive.messages"),
    ))
    state = dict(session.context_state or {})
    state.update(version=1, latest_archive_id=archive_id, archive_count=int(state.get("archive_count", 0)) + 1)
    session.context_state = state
    await db.commit()
    return archive_id


async def read_delegation_context_archive(
    db: AsyncSession, *, archive_id: str, session_id: str, entity_id: str,
    workspace_id: str, conversation_id: str, user_id: str, agent_subscription_id: str,
) -> AgentContextArchive | None:
    """An archive is data only; reading it never restores historical tool grants."""
    return (await db.execute(select(AgentContextArchive).join(
        AgentDelegationSession, AgentDelegationSession.id == AgentContextArchive.session_id,
    ).where(
        AgentContextArchive.id == archive_id, AgentDelegationSession.id == session_id,
        AgentDelegationSession.entity_id == entity_id, AgentDelegationSession.workspace_id == workspace_id,
        AgentDelegationSession.conversation_id == conversation_id, AgentDelegationSession.user_id == user_id,
        AgentDelegationSession.agent_subscription_id == agent_subscription_id,
    ))).scalar_one_or_none()


async def renew_delegation_session(db: AsyncSession, *, session_id: str, run_id: str) -> None:
    result = await db.execute(
        update(AgentDelegationSession)
        .where(
            AgentDelegationSession.id == session_id,
            AgentDelegationSession.status == "running",
            AgentDelegationSession.active_run_id == run_id,
            AgentDelegationSession.active_until > datetime.now(timezone.utc),
        )
        .values(active_until=datetime.now(timezone.utc) + timedelta(hours=2))
    )
    if result.rowcount != 1:
        raise RuntimeError("Delegated session is no longer active.")
    await db.commit()


async def interrupt_delegation_session(
    db: AsyncSession, *, session_id: str, run_id: str,
) -> None:
    session = (
        await db.execute(
            select(AgentDelegationSession)
            .where(AgentDelegationSession.id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (session is None or session.status != "running" or session.active_run_id != run_id
        or not session.active_until or session.active_until <= datetime.now(timezone.utc)):
        return
    session.status = "interrupted"
    session.active_run_id = None
    session.active_until = None
    await db.commit()


async def checkpoint_delegation_context(db: AsyncSession, *, session_id: str, run_id: str, messages: list[dict]) -> None:
    session = (await db.execute(select(AgentDelegationSession).where(
        AgentDelegationSession.id == session_id,
        AgentDelegationSession.active_run_id == run_id,
        AgentDelegationSession.active_until > datetime.now(timezone.utc),
        AgentDelegationSession.status == "running",
    ).with_for_update())).scalar_one_or_none()
    if session is None:
        raise RuntimeError("Delegated session is no longer active.")
    session.messages = [dict(m) for m in messages if m.get("role") not in {"system", "developer"}]
    await db.commit()
