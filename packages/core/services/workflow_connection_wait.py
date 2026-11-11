"""A Workflow step whose integration is not connected waits for it.

The step's Agent cannot reach an integration nobody has connected, so the step
ends without the action it owed. Failing the run would leave a Retry for the
person to find after they connect. Instead the step pauses for that
connection through the normal re-execute resume:

- the Workspace chat gets the same connect card the Workspace asks with;
- the run continues from this step once the account works, when the card is
  answered, when someone continues it from the run, or on the next recheck.

Nothing here is specific to a provider: the need comes from the Workspace's
own connection requirements for the step's Agent.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime.outcome import attempt_forbids_replay
from packages.core.models.base import generate_ulid
from packages.core.services.provider_keys import canonical_provider_key

logger = logging.getLogger(__name__)

CONNECTION_WAIT_TYPE = "connection"

#: Seconds between readiness rechecks while a run waits, by recheck count.
#: A connect card or the run's own Continue resumes it at once; this only
#: covers an account connected somewhere else (the Integrations page).
CONNECTION_RECHECK_SECONDS = (120, 300, 900, 1800)


def is_connection_wait(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and result.get("status") == "paused"
        and result.get("wait_type") == CONNECTION_WAIT_TYPE
        and bool(result.get("connection_provider"))
    )


def _evidence(result: dict) -> str:
    from packages.core.services.workspace_connection_status import workflow_step_result_evidence

    return workflow_step_result_evidence(result)


def _arm_recheck(run: Any, rechecks: int) -> datetime:
    now = datetime.now(timezone.utc)
    due = now + timedelta(seconds=CONNECTION_RECHECK_SECONDS[min(rechecks, len(CONNECTION_RECHECK_SECONDS) - 1)])
    run.continuation_token = generate_ulid()
    run.continuation_due_at = due
    run.continuation_next_attempt_at = due
    return due


def _waiting_result(run: Any, *, need: dict, prior: dict, rechecks: int, asked: bool) -> dict:
    due = _arm_recheck(run, rechecks)
    label = need["label"]
    return {
        "status": "paused",
        "resume_strategy": "reexecute",
        "wait_type": CONNECTION_WAIT_TYPE,
        "connection_provider": need["provider"],
        "connection_label": label,
        "prompt": f"Waiting for {label}. This step continues once {label} is connected.",
        # The attempt that found the gap stays as recovery evidence for the rerun.
        "output": prior.get("output"),
        "prior_error": prior.get("error"),
        "waiting_since": prior.get("waiting_since") or datetime.now(timezone.utc).isoformat(),
        "connection_rechecks": rechecks,
        "connection_asked": asked,
        "resume_at": due.isoformat(),
        "auto_resume_scheduled": True,
    }


async def _step_need(db: AsyncSession, *, run: Any, step_id: str, evidence: str) -> dict | None:
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workspace_connection_status import workflow_step_connection_need

    # Only a run's own steps wait; a stage's inner operations keep its contract.
    user_id = str(getattr(run, "started_by", None) or "").strip()
    if not isinstance(run, WorkflowRun) or not run.workspace_id or not user_id or not hasattr(db, "begin_nested"):
        return None
    try:
        async with db.begin_nested():
            return await workflow_step_connection_need(
                db, run=run, step_id=step_id, user_id=user_id, evidence=evidence,
            )
    except Exception:  # noqa: BLE001 — never turn a step outcome into a crash
        logger.warning("Workflow connection need unavailable for run %s", getattr(run, "id", None), exc_info=True)
        return None


async def connection_wait_for_failed_step(db: AsyncSession, *, run: Any, step: dict, result: Any) -> dict | None:
    """The wait that replaces a failed step's result when its integration is missing."""
    if not isinstance(result, dict) or result.get("status") != "failed" or attempt_forbids_replay(result):
        return None
    need = await _step_need(db, run=run, step_id=str(step.get("id") or ""), evidence=_evidence(result))
    if need is None:
        return None
    asked = await ask_to_connect_for_run(db, run=run, need=need)
    return _waiting_result(run, need=need, prior=result, rechecks=0, asked=asked)


async def recheck_connection_wait(db: AsyncSession, *, run: Any, step: dict, previous: Any) -> dict | None:
    """Still waiting: the re-armed wait. Connected now: ``None``, so the step runs."""
    if not is_connection_wait(previous):
        return None
    need = await _step_need(db, run=run, step_id=str(step.get("id") or ""), evidence=_evidence(previous))
    if need is None:
        return None
    same = canonical_provider_key(need["provider"]) == canonical_provider_key(previous["connection_provider"])
    asked = bool(previous.get("connection_asked")) if same else await ask_to_connect_for_run(db, run=run, need=need)
    return _waiting_result(
        run, need=need, prior={**previous, "error": previous.get("prior_error")},
        rechecks=int(previous.get("connection_rechecks") or 0) + 1, asked=asked,
    )


async def _open_connect_card(db: AsyncSession, *, entity_id: str, workspace_id: str, provider: str) -> bool:
    from packages.core.models.task import Conversation, Message
    from packages.core.services.workspace_connection_status import CONNECTION_REQUEST_KEY

    keys = (await db.execute(
        select(Message.pending_action["provider_key"].as_string())
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.entity_id == entity_id,
            Conversation.workspace_id == workspace_id,
            Message.resolved_at.is_(None),
            Message.pending_action[CONNECTION_REQUEST_KEY].as_boolean().is_(True),
        )
    )).scalars().all()
    return provider in {canonical_provider_key(key) for key in keys if key}


async def ask_to_connect_for_run(db: AsyncSession, *, run: Any, need: dict) -> bool:
    """Put the connect card in the run's Workspace chat, unless one is already open.

    Answering it (or any open card for the same integration) continues every
    run of this Workspace waiting for it. The caller commits.
    """
    from packages.core.models.user import User
    from packages.core.services.workflow_chat_projection import workflow_run_display_name
    from packages.core.services.workspace_connection_status import _connect_body_zh, _connect_question
    from packages.core.workspace_chat.service import post_message

    provider = canonical_provider_key(need["provider"])
    try:
        async with db.begin_nested():
            if await _open_connect_card(db, entity_id=run.entity_id, workspace_id=run.workspace_id, provider=provider):
                return True
            waiting = [workflow_run_display_name(run)]
            card = _connect_question(provider=need["provider"], label=need["label"], waiting=waiting)
            card["waiting_workflow_run_id"] = str(run.id)
            asker = await db.get(User, run.started_by)
            chinese = str(getattr(asker, "locale", "") or "").lower().startswith("zh")
            await post_message(
                db,
                entity_id=run.entity_id,
                workspace_id=run.workspace_id,
                body=_connect_body_zh(need["label"], waiting) if chinese else card["description"],
                message_kind="hitl_request",
                author_kind="system",
                pending_action=card,
            )
        return True
    except Exception:  # noqa: BLE001 — the run still waits; the App lists the connection
        logger.warning("Workflow connect card not posted for run %s", getattr(run, "id", None), exc_info=True)
        return False


async def resume_runs_waiting_for_connection(
    db: AsyncSession, *, entity_id: str, workspace_id: str, provider: str, user_id: str,
) -> list[str]:
    """Continue each run of the Workspace waiting for ``provider``.

    Each resumes from its blocked step through the normal resume, which
    rechecks the connection before running anything.
    """
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.models.workflow import WorkflowRun

    provider = canonical_provider_key(provider)
    runs = (await db.execute(
        select(WorkflowRun).where(
            WorkflowRun.entity_id == entity_id,
            WorkflowRun.workspace_id == workspace_id,
            WorkflowRun.status == "paused",
        )
    )).scalars().all()
    waiting = [
        run.id for run in runs
        if is_connection_wait(result := (run.step_results or {}).get(run.current_step_id))
        and canonical_provider_key(result["connection_provider"]) == provider
    ]
    resumed = []
    for run_id in waiting:
        outcome = await WorkflowRunner.resume(run_id, entity_id=entity_id, resumed_by=user_id, execute=False)
        if outcome == "resumed":
            resumed.append(run_id)
    return resumed
