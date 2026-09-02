"""Exclusive execution rights for long-running runtime and planning work.

These runtimes can perform irreversible external actions. Celery may deliver
the same message more than once, and API callers can also race inline work,
so checking the run status is not enough. One renewable PostgreSQL row lease is
the authoritative fence for every execution; it does not pin a database
connection for the duration of long-running provider work.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import AsyncIterator

from sqlalchemy import text

from packages.core.constants.execution import (
    ExecutionClaimKind,
    SCHEDULED_JOB_SKILL_GENERATION_RECHECK_SECONDS,
    SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS,
)
from packages.core.models.base import generate_ulid


WORKFLOW_RUN_EXECUTION_LEASE_TTL_SECONDS = 660
AGENT_TASK_EXECUTION_LEASE_TTL_SECONDS = 1860
AGENT_TASK_EXECUTION_RECHECK_SECONDS = (
    AGENT_TASK_EXECUTION_LEASE_TTL_SECONDS + 5
)
TASK_PLAN_EXECUTION_LEASE_TTL_SECONDS = 1110
TASK_PLAN_EXECUTION_RECHECK_SECONDS = TASK_PLAN_EXECUTION_LEASE_TTL_SECONDS + 5
SCHEDULED_RUN_EXECUTION_LEASE_TTL_SECONDS = (
    SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS - 5
)
SCHEDULED_JOB_SKILL_GENERATION_LEASE_TTL_SECONDS = (
    SCHEDULED_JOB_SKILL_GENERATION_RECHECK_SECONDS - 5
)
_WORKFLOW_RUN_EXECUTION_HEARTBEAT_SECONDS = 30
_WORKFLOW_RUN_EXECUTION_RENEWAL_RETRY_SECONDS = 2
_WORKFLOW_RUN_EXECUTION_RENEWAL_TIMEOUT_SECONDS = 10
_WORKFLOW_RUN_EXECUTION_TRANSIENT_FAILURE_LIMIT = 3
logger = logging.getLogger(__name__)

CLAIM_GRANTED = "granted"
CLAIM_HELD = "claim_held_by_live_execution"


class WorkflowRunExecutionClaimLost(RuntimeError):
    """The current runner can no longer prove exclusive execution rights."""


class AgentTaskExecutionClaimLost(RuntimeError):
    """The current Task runner can no longer prove exclusive execution rights."""


class TaskPlanExecutionClaimLost(RuntimeError):
    """The current Planner can no longer prove exclusive planning rights."""


class ScheduledRunExecutionClaimLost(RuntimeError):
    """The scheduled occurrence can no longer prove exclusive execution rights."""


class ScheduledJobSkillGenerationClaimLost(RuntimeError):
    """The scheduled Skill generator lost its revision-scoped claim."""


@dataclass(frozen=True)
class ExecutionClaimPolicy:
    kind: ExecutionClaimKind
    lease_ttl_seconds: int
    resource_label: str
    lost_error_type: type[RuntimeError]


class ExecutionClaimPolicyFactory:
    """Resolve one shared claim policy from the runtime kind enum."""

    @staticmethod
    def create(kind: ExecutionClaimKind) -> ExecutionClaimPolicy:
        return {
            ExecutionClaimKind.WORKFLOW_RUN: ExecutionClaimPolicy(
                kind=kind,
                lease_ttl_seconds=WORKFLOW_RUN_EXECUTION_LEASE_TTL_SECONDS,
                resource_label="Workflow run",
                lost_error_type=WorkflowRunExecutionClaimLost,
            ),
            ExecutionClaimKind.AGENT_TASK: ExecutionClaimPolicy(
                kind=kind,
                lease_ttl_seconds=AGENT_TASK_EXECUTION_LEASE_TTL_SECONDS,
                resource_label="Agent task",
                lost_error_type=AgentTaskExecutionClaimLost,
            ),
            ExecutionClaimKind.TASK_PLAN: ExecutionClaimPolicy(
                kind=kind,
                lease_ttl_seconds=TASK_PLAN_EXECUTION_LEASE_TTL_SECONDS,
                resource_label="Task planning",
                lost_error_type=TaskPlanExecutionClaimLost,
            ),
            ExecutionClaimKind.SCHEDULED_RUN: ExecutionClaimPolicy(
                kind=kind,
                lease_ttl_seconds=SCHEDULED_RUN_EXECUTION_LEASE_TTL_SECONDS,
                resource_label="Scheduled run",
                lost_error_type=ScheduledRunExecutionClaimLost,
            ),
            ExecutionClaimKind.SCHEDULED_JOB_SKILL: ExecutionClaimPolicy(
                kind=kind,
                lease_ttl_seconds=(
                    SCHEDULED_JOB_SKILL_GENERATION_LEASE_TTL_SECONDS
                ),
                resource_label="Scheduled Job Skill generation",
                lost_error_type=ScheduledJobSkillGenerationClaimLost,
            ),
        }[kind]


@dataclass(frozen=True)
class WorkflowRunExecutionClaim:
    run_id: str
    token: str
    granted: bool
    reason: str
    lost: asyncio.Event
    terminal_committed: asyncio.Event
    lost_error_type: type[RuntimeError] = WorkflowRunExecutionClaimLost
    resource_label: str = "Execution"
    claim_key: str = ""

    def __bool__(self) -> bool:
        return self.granted

    def raise_if_lost(self) -> None:
        """Fence a durable commit or external-effect boundary."""
        if self.lost.is_set() and not self.terminal_committed.is_set():
            raise self.lost_error_type(
                f"{self.resource_label} {self.run_id} lost its execution claim"
            )

    def mark_terminal_committed(self) -> None:
        """Close the fenced business boundary after its terminal commit."""

        self.terminal_committed.set()

    async def fence_for_commit(self, db) -> None:
        """Lock and validate this token inside the business transaction."""

        self.raise_if_lost()
        if not self.claim_key:
            # Test/legacy claims created without a durable key retain the
            # in-memory guard. Every production renewable claim has a key.
            return
        current = (await db.execute(
            _FENCE_POSTGRES_CLAIM,
            {
                "claim_key": self.claim_key,
                "claim_token": self.token,
            },
        )).scalar_one_or_none()
        if current is None:
            self.lost.set()
            self.raise_if_lost()


async def commit_fenced_execution_boundary(
    commit: Callable[[], Awaitable[None]],
    *,
    before_commit: Callable[[], None] | None = None,
    after_commit: Callable[[], None] | None = None,
    execution_claim: WorkflowRunExecutionClaim | None = None,
    session=None,
) -> None:
    """Commit one claimed boundary without an ambiguous cancellation window.

    Claim heartbeats deliberately keep cancelling an owner after its lease is
    lost.  A PostgreSQL COMMIT can nevertheless have reached the server before
    that cancellation is delivered to the awaiting coroutine.  Run the commit
    in its own task and shield it until its result is known; only then close the
    in-memory claim fence.  Repeated cancellation remains poisoned everywhere
    except this exact durability boundary.
    """

    if before_commit is not None:
        before_commit()
    if execution_claim is not None:
        if session is None:
            raise ValueError("execution claim fencing requires its business session")
        await execution_claim.fence_for_commit(session)
    commit_task = asyncio.ensure_future(commit())
    owner_task = asyncio.current_task()
    while not commit_task.done():
        try:
            await asyncio.shield(commit_task)
        except asyncio.CancelledError:
            # The heartbeat may cancel repeatedly until after_commit closes the
            # fence.  Keep resolving only this already-started COMMIT.
            if owner_task is not None:
                owner_task.uncancel()
            continue
    commit_task.result()
    if after_commit is not None:
        after_commit()


def _claim(
    run_id: str,
    token: str,
    *,
    granted: bool,
    reason: str,
    lost: asyncio.Event | None = None,
    terminal_committed: asyncio.Event | None = None,
    lost_error_type: type[RuntimeError] = WorkflowRunExecutionClaimLost,
    resource_label: str = "Execution",
    claim_key: str = "",
) -> WorkflowRunExecutionClaim:
    return WorkflowRunExecutionClaim(
        run_id=run_id,
        token=token,
        granted=granted,
        reason=reason,
        lost=lost or asyncio.Event(),
        terminal_committed=terminal_committed or asyncio.Event(),
        lost_error_type=lost_error_type,
        resource_label=resource_label,
        claim_key=claim_key,
    )


_ACQUIRE_POSTGRES_CLAIM = text("""
    INSERT INTO runtime_execution_claims (
        claim_key,
        claim_token,
        expires_at,
        created_at,
        updated_at
    )
    VALUES (
        :claim_key,
        :claim_token,
        CURRENT_TIMESTAMP
            + CAST(:ttl_seconds AS DOUBLE PRECISION) * INTERVAL '1 second',
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    )
    ON CONFLICT (claim_key) DO UPDATE
    SET
        claim_token = EXCLUDED.claim_token,
        expires_at = EXCLUDED.expires_at,
        updated_at = CURRENT_TIMESTAMP
    WHERE runtime_execution_claims.expires_at <= CURRENT_TIMESTAMP
       OR runtime_execution_claims.claim_token = EXCLUDED.claim_token
    RETURNING claim_token
""")

_EXTEND_POSTGRES_CLAIM = text("""
    UPDATE runtime_execution_claims
    SET
        expires_at = CURRENT_TIMESTAMP
            + CAST(:ttl_seconds AS DOUBLE PRECISION) * INTERVAL '1 second',
        updated_at = CURRENT_TIMESTAMP
    WHERE claim_key = :claim_key
      AND claim_token = :claim_token
      AND expires_at > CURRENT_TIMESTAMP
    RETURNING claim_token
""")

_RELEASE_POSTGRES_CLAIM = text("""
    DELETE FROM runtime_execution_claims
    WHERE claim_key = :claim_key
      AND claim_token = :claim_token
    RETURNING claim_token
""")

_FENCE_POSTGRES_CLAIM = text("""
    SELECT claim_token
    FROM runtime_execution_claims
    WHERE claim_key = :claim_key
      AND claim_token = :claim_token
      AND expires_at > CURRENT_TIMESTAMP
    FOR UPDATE
""")

_LIVE_POSTGRES_CLAIM = text("""
    SELECT claim_token
    FROM runtime_execution_claims
    WHERE claim_key = :claim_key
      AND expires_at > CURRENT_TIMESTAMP
    FOR UPDATE
""")


async def scheduled_run_has_live_execution_claim(db, run_id: str) -> bool:
    """Lock and report the authoritative execution lease for one occurrence."""

    claim_key = f"{ExecutionClaimKind.SCHEDULED_RUN.value}:{run_id}"
    current = (await db.execute(
        _LIVE_POSTGRES_CLAIM,
        {"claim_key": claim_key},
    )).scalar_one_or_none()
    return current is not None


async def _run_claim_statement(statement, parameters: dict) -> bool:
    """Execute one short claim transaction on a disposable worker session."""
    from packages.core.database import create_worker_session

    async with create_worker_session()() as db:
        result = (await db.execute(statement, parameters)).scalar_one_or_none()
        await db.commit()
        return result is not None


async def _acquire_postgres_execution_claim(
    claim_key: str,
    token: str,
    *,
    lease_ttl_seconds: int,
) -> bool:
    return await _run_claim_statement(
        _ACQUIRE_POSTGRES_CLAIM,
        {
            "claim_key": claim_key,
            "claim_token": token,
            "ttl_seconds": lease_ttl_seconds,
        },
    )


async def _extend_postgres_execution_claim(
    claim_key: str,
    token: str,
    *,
    lease_ttl_seconds: int,
) -> bool:
    return await _run_claim_statement(
        _EXTEND_POSTGRES_CLAIM,
        {
            "claim_key": claim_key,
            "claim_token": token,
            "ttl_seconds": lease_ttl_seconds,
        },
    )


async def _release_postgres_execution_claim(
    claim_key: str,
    token: str,
) -> bool:
    return await _run_claim_statement(
        _RELEASE_POSTGRES_CLAIM,
        {"claim_key": claim_key, "claim_token": token},
    )


@asynccontextmanager
async def _postgres_execution_claim(
    resource_id: str,
    token: str,
    *,
    claim_key: str,
    lease_ttl_seconds: int,
) -> AsyncIterator[WorkflowRunExecutionClaim]:
    """Own one durable PostgreSQL lease without holding its DB connection."""
    granted = await _acquire_postgres_execution_claim(
        claim_key,
        token,
        lease_ttl_seconds=lease_ttl_seconds,
    )
    try:
        yield _claim(
            resource_id,
            token,
            granted=granted,
            reason=CLAIM_GRANTED if granted else CLAIM_HELD,
            claim_key=claim_key,
        )
    finally:
        if granted:
            try:
                await _release_postgres_execution_claim(claim_key, token)
            except Exception:
                # Expiry remains the crash-recovery boundary if cleanup is
                # unavailable; never hide a completed business result.
                logger.warning(
                    "Failed to release execution claim %s",
                    claim_key,
                    exc_info=True,
                )


@asynccontextmanager
async def _renewable_execution_claim(
    resource_id: str,
    *,
    kind: ExecutionClaimKind,
) -> AsyncIterator[WorkflowRunExecutionClaim]:
    policy = ExecutionClaimPolicyFactory.create(kind)
    token = generate_ulid()
    claim_key = f"{policy.kind.value}:{resource_id}"
    async with _postgres_execution_claim(
        resource_id,
        token,
        claim_key=claim_key,
        lease_ttl_seconds=policy.lease_ttl_seconds,
    ) as postgres_claim:
        if not postgres_claim:
            yield postgres_claim
            return

        lost = asyncio.Event()
        terminal_committed = asyncio.Event()
        owner_task = asyncio.current_task()

        async def _heartbeat() -> None:
            delay_seconds = _WORKFLOW_RUN_EXECUTION_HEARTBEAT_SECONDS
            transient_failures = 0
            while True:
                await asyncio.sleep(delay_seconds)
                if terminal_committed.is_set():
                    return
                try:
                    extended = await asyncio.wait_for(
                        _extend_postgres_execution_claim(
                            claim_key,
                            token,
                            lease_ttl_seconds=policy.lease_ttl_seconds,
                        ),
                        timeout=_WORKFLOW_RUN_EXECUTION_RENEWAL_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    extended = None
                except Exception:
                    logger.warning(
                        "%s %s lease renewal failed",
                        policy.resource_label,
                        resource_id,
                        exc_info=True,
                    )
                    extended = None
                if terminal_committed.is_set():
                    return
                if extended is True:
                    transient_failures = 0
                    delay_seconds = _WORKFLOW_RUN_EXECUTION_HEARTBEAT_SECONDS
                    continue
                if extended is None:
                    transient_failures += 1
                    if (
                        transient_failures
                        < _WORKFLOW_RUN_EXECUTION_TRANSIENT_FAILURE_LIMIT
                    ):
                        logger.warning(
                            "%s %s lease renewal unavailable; retrying "
                            "within the current fencing TTL (%d/%d)",
                            policy.resource_label,
                            resource_id,
                            transient_failures,
                            _WORKFLOW_RUN_EXECUTION_TRANSIENT_FAILURE_LIMIT,
                        )
                        delay_seconds = (
                            _WORKFLOW_RUN_EXECUTION_RENEWAL_RETRY_SECONDS
                        )
                        continue
                if terminal_committed.is_set():
                    return
                lost.set()
                # Cancellation is cooperative and provider wrappers sometimes
                # suppress one CancelledError. Keep the owner poisoned until
                # the claim context exits so it cannot cross a later await and
                # commit under an expired fencing token.
                while (
                    not terminal_committed.is_set()
                    and owner_task is not None
                    and not owner_task.done()
                ):
                    owner_task.cancel()
                    await asyncio.sleep(0)
                return

        heartbeat = asyncio.create_task(
            _heartbeat(),
            name=(
                f"execution-claim-heartbeat:"
                f"{policy.resource_label}:{resource_id}"
            ),
        )
        try:
            try:
                claim = _claim(
                    resource_id,
                    token,
                    granted=True,
                    reason=CLAIM_GRANTED,
                    lost=lost,
                    terminal_committed=terminal_committed,
                    lost_error_type=policy.lost_error_type,
                    resource_label=policy.resource_label,
                    claim_key=claim_key,
                )
                yield claim
            except asyncio.CancelledError as exc:
                if lost.is_set():
                    raise policy.lost_error_type(
                        f"{policy.resource_label} {resource_id} lost its execution claim"
                    ) from exc
                raise
            claim.raise_if_lost()
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat


@asynccontextmanager
async def workflow_run_execution_claim(
    run_id: str,
) -> AsyncIterator[WorkflowRunExecutionClaim]:
    """Yield one renewable, exclusive execution claim for ``run_id``."""

    async with _renewable_execution_claim(
        run_id,
        kind=ExecutionClaimKind.WORKFLOW_RUN,
    ) as claim:
        yield claim


@asynccontextmanager
async def agent_task_execution_claim(
    task_id: str,
) -> AsyncIterator[WorkflowRunExecutionClaim]:
    """Yield one renewable, exclusive execution claim for an Agent Task."""

    async with _renewable_execution_claim(
        task_id,
        kind=ExecutionClaimKind.AGENT_TASK,
    ) as claim:
        yield claim


@asynccontextmanager
async def task_plan_execution_claim(
    task_id: str,
) -> AsyncIterator[WorkflowRunExecutionClaim]:
    """Yield one renewable claim for billable planning of a Task."""

    async with _renewable_execution_claim(
        task_id,
        kind=ExecutionClaimKind.TASK_PLAN,
    ) as claim:
        yield claim


@asynccontextmanager
async def scheduled_run_execution_claim(
    run_id: str,
) -> AsyncIterator[WorkflowRunExecutionClaim]:
    """Yield one renewable claim for a scheduled occurrence's child work."""

    async with _renewable_execution_claim(
        run_id,
        kind=ExecutionClaimKind.SCHEDULED_RUN,
    ) as claim:
        yield claim


@asynccontextmanager
async def scheduled_job_skill_generation_claim(
    job_id: str,
    revision: int,
) -> AsyncIterator[WorkflowRunExecutionClaim]:
    """Yield one renewable claim for a Job's exact prompt revision."""

    resource_id = f"{job_id}:{int(revision)}"
    async with _renewable_execution_claim(
        resource_id,
        kind=ExecutionClaimKind.SCHEDULED_JOB_SKILL,
    ) as claim:
        yield claim


__all__ = [
    "AGENT_TASK_EXECUTION_LEASE_TTL_SECONDS",
    "AGENT_TASK_EXECUTION_RECHECK_SECONDS",
    "AgentTaskExecutionClaimLost",
    "CLAIM_GRANTED",
    "CLAIM_HELD",
    "ExecutionClaimPolicy",
    "ExecutionClaimPolicyFactory",
    "WORKFLOW_RUN_EXECUTION_LEASE_TTL_SECONDS",
    "SCHEDULED_RUN_EXECUTION_LEASE_TTL_SECONDS",
    "SCHEDULED_RUN_EXECUTION_RECHECK_SECONDS",
    "SCHEDULED_JOB_SKILL_GENERATION_LEASE_TTL_SECONDS",
    "TASK_PLAN_EXECUTION_LEASE_TTL_SECONDS",
    "TASK_PLAN_EXECUTION_RECHECK_SECONDS",
    "ScheduledJobSkillGenerationClaimLost",
    "ScheduledRunExecutionClaimLost",
    "TaskPlanExecutionClaimLost",
    "WorkflowRunExecutionClaim",
    "WorkflowRunExecutionClaimLost",
    "agent_task_execution_claim",
    "commit_fenced_execution_boundary",
    "scheduled_run_execution_claim",
    "scheduled_run_has_live_execution_claim",
    "scheduled_job_skill_generation_claim",
    "task_plan_execution_claim",
    "workflow_run_execution_claim",
]
