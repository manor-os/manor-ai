"""Typed routing for a Task's first execution and subsequent attempts.

``TaskType`` describes business meaning and remains extensible.  This module is
the closed runtime factory: executable Workspace Tasks use the coordinated Plan
runtime, interactive and approval Tasks retain their dedicated surfaces, and
standalone Agent Tasks keep the legacy runner only for compatibility.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.agents import MANOR_AGENT_ID, is_master_agent
from packages.core.constants.task import TaskRuntimeKind, TaskStatus, TaskType
from packages.core.models.base import generate_ulid
from packages.core.models.runtime_run import RuntimeOutboxEvent
from packages.core.models.task import Task
from packages.core.models.workspace import AgentSubscription

logger = logging.getLogger(__name__)
_ENQUEUE_KEY = "task_execution_after_commit"
TASK_EXECUTION_OUTBOX_EVENT = "runtime.dispatch_task_execution"
_TASK_EXECUTION_INTENT_VERSION = 1


_LEGACY_MODE_BY_RUNTIME = {
    TaskRuntimeKind.COORDINATED_PLAN: "plan_new",
    TaskRuntimeKind.PLAN_RESUME: "plan",
    TaskRuntimeKind.INTERACTIVE_SESSION: "interactive_session",
    TaskRuntimeKind.HUMAN_DECISION: "human_decision",
    TaskRuntimeKind.MANUAL: "manual",
    TaskRuntimeKind.LEGACY_AGENT: "agent",
}


@dataclass(frozen=True)
class TaskPlanServiceBinding:
    """Immutable execution identity for one Workspace service binding."""

    subscription_id: str
    service_key: str
    agent_id: str


@dataclass(frozen=True)
class CoordinatorRuntimeSelectionIdentity:
    """Canonical identity of one factory-owned coordinator selection."""

    root: TaskPlanServiceBinding
    bindings: tuple[TaskPlanServiceBinding, ...]


@dataclass(frozen=True)
class CoordinatorRuntimeSelection:
    """Factory-owned service scope and unique root for a coordinated Plan.

    The root is selected while the Task runtime is classified.  Planner,
    persistence, and materialization consume this value instead of choosing an
    owner again from a source DAG or mutable request field.
    """

    root: TaskPlanServiceBinding
    bindings: tuple[TaskPlanServiceBinding, ...]

    @classmethod
    def from_subscriptions(
        cls,
        subscriptions: list[AgentSubscription],
    ) -> CoordinatorRuntimeSelection | None:
        """Freeze the exact ordered factory rows into a detached selection."""

        if not subscriptions:
            return None
        bindings = tuple(
            TaskPlanServiceBinding(
                subscription_id=str(subscription.id),
                service_key=str(subscription.service_key),
                agent_id=str(subscription.agent_id),
            )
            for subscription in subscriptions
        )
        return cls(root=bindings[0], bindings=bindings)

    def __post_init__(self) -> None:
        if not self.bindings or self.root not in self.bindings:
            raise ValueError("coordinator root must belong to its service scope")
        service_keys = [binding.service_key for binding in self.bindings]
        subscription_ids = [binding.subscription_id for binding in self.bindings]
        if any(
            not binding.subscription_id
            or not binding.service_key
            or not binding.agent_id
            for binding in self.bindings
        ):
            raise ValueError("coordinator service bindings must be complete")
        if len(service_keys) != len(set(service_keys)):
            raise ValueError("coordinator service scope must be unambiguous")
        if len(subscription_ids) != len(set(subscription_ids)):
            raise ValueError("coordinator subscriptions must be unique")

    @property
    def service_keys(self) -> frozenset[str]:
        return frozenset(binding.service_key for binding in self.bindings)

    @property
    def subscription_ids(self) -> frozenset[str]:
        return frozenset(binding.subscription_id for binding in self.bindings)

    def stable_identity(self) -> CoordinatorRuntimeSelectionIdentity:
        """Return an order-independent identity safe across transaction reloads."""

        return CoordinatorRuntimeSelectionIdentity(
            root=self.root,
            bindings=tuple(
                sorted(
                    self.bindings,
                    key=lambda binding: (
                        binding.service_key,
                        binding.subscription_id,
                        binding.agent_id,
                    ),
                )
            ),
        )

    def stable_fingerprint(self) -> str:
        """Hash the exact root and service-to-subscription/Agent bindings."""

        identity = self.stable_identity()
        payload = {
            "version": 1,
            "root": {
                "subscription_id": identity.root.subscription_id,
                "service_key": identity.root.service_key,
                "agent_id": identity.root.agent_id,
            },
            "bindings": [
                {
                    "subscription_id": binding.subscription_id,
                    "service_key": binding.service_key,
                    "agent_id": binding.agent_id,
                }
                for binding in identity.bindings
            ],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def require_current(
        self,
        current: CoordinatorRuntimeSelection | None,
    ) -> CoordinatorRuntimeSelection:
        """Fail closed unless ``current`` is this exact immutable selection."""

        if current is None or self.stable_identity() != current.stable_identity():
            raise CoordinatorSelectionDriftError(expected=self, current=current)
        return self

    def binding_for_service(self, service_key: str | None) -> TaskPlanServiceBinding | None:
        normalized = str(service_key or "").strip()
        return next(
            (
                binding
                for binding in self.bindings
                if binding.service_key == normalized
            ),
            None,
        )


class CoordinatorSelectionDriftError(ValueError):
    """The Workspace service selection changed while a Plan was generated."""

    def __init__(
        self,
        *,
        expected: CoordinatorRuntimeSelection,
        current: CoordinatorRuntimeSelection | None,
    ) -> None:
        self.expected_fingerprint = expected.stable_fingerprint()
        self.current_fingerprint = (
            current.stable_fingerprint() if current is not None else None
        )
        current_label = self.current_fingerprint or "unavailable"
        super().__init__(
            "coordinator selection changed while planning; retry planning "
            f"(expected={self.expected_fingerprint}, current={current_label})"
        )


@dataclass(frozen=True)
class TaskExecutionDispatch:
    """One closed runtime decision for a Task.

    ``mode`` preserves the public/legacy vocabulary used by retry responses and
    logs. New control flow must branch on ``runtime_kind`` instead of those
    string values.
    """

    runtime_kind: TaskRuntimeKind
    task_id: str
    target_id: str | None = None
    coordinator: CoordinatorRuntimeSelection | None = None

    def __post_init__(self) -> None:
        if (
            self.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN
            and self.coordinator is None
        ):
            raise ValueError(
                "coordinated Plan dispatch requires a factory-owned selection"
            )
        if (
            self.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN
            and self.coordinator is not None
        ):
            raise ValueError(
                "only coordinated Plan dispatch may carry a coordinator selection"
            )

    @property
    def mode(self) -> str:
        return _LEGACY_MODE_BY_RUNTIME[self.runtime_kind]

    @property
    def value(self) -> str | None:
        """Compatibility name for the old tuple's second element."""

        return self.target_id

    @property
    def is_dispatchable(self) -> bool:
        return self.runtime_kind in {
            TaskRuntimeKind.COORDINATED_PLAN,
            TaskRuntimeKind.PLAN_RESUME,
            TaskRuntimeKind.LEGACY_AGENT,
        }


def plan_resume_dispatch(*, task_id: str, plan_id: str) -> TaskExecutionDispatch:
    """Build the typed continuation used by existing Plan retry paths."""

    return TaskExecutionDispatch(
        runtime_kind=TaskRuntimeKind.PLAN_RESUME,
        task_id=task_id,
        target_id=plan_id,
    )


def _clean_service_keys(values: object) -> set[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {
        str(value).strip()
        for value in values
        if str(value or "").strip()
    }


def _unique_service_subscription(
    subscriptions: list[AgentSubscription],
    service_key: str,
    *,
    agent_id: str | None = None,
) -> AgentSubscription | None:
    matches = [
        subscription
        for subscription in subscriptions
        if subscription.service_key == service_key
        and (agent_id is None or subscription.agent_id == agent_id)
    ]
    return matches[0] if len(matches) == 1 else None


async def task_plan_subscriptions(
    db: AsyncSession,
    task: Task,
    *,
    lock_for_update: bool = False,
) -> list[AgentSubscription]:
    """Resolve one unambiguous persisted service scope for Task planning.

    A direct ``agent_id`` assignment is not permission to use every Workspace
    service. Without an explicit owner it must identify exactly one deployed
    service subscription. Explicit delegates may add other uniquely deployed
    services, while the owner remains pinned to the assigned Agent.
    """
    if not task.workspace_id:
        return []
    query = select(AgentSubscription).where(
        AgentSubscription.entity_id == task.entity_id,
        AgentSubscription.workspace_id == task.workspace_id,
        AgentSubscription.status == "active",
    ).order_by(AgentSubscription.id.asc()).execution_options(populate_existing=True)
    if lock_for_update:
        # Persistence locks Task first, then active subscriptions by stable id.
        # The locks are released before provider I/O and are held only through
        # the short final Plan/Step commit boundary.
        query = query.with_for_update()
    subscriptions = list((await db.execute(query)).scalars().all())
    service_subscriptions = [
        subscription for subscription in subscriptions if subscription.service_key
    ]
    delegate_keys = _clean_service_keys(task.delegate_service_keys)
    assigned_agent_id = (
        str(task.agent_id).strip()
        if task.agent_id and not is_master_agent(task.agent_id, task.agent_type)
        else None
    )

    owner: AgentSubscription | None = None
    if task.owner_subscription_id:
        owner = next((s for s in subscriptions if s.id == task.owner_subscription_id), None)
        if (
            owner is None
            or not owner.service_key
            or (
                task.owner_service_key
                and owner.service_key != task.owner_service_key
            )
            or (assigned_agent_id and owner.agent_id != assigned_agent_id)
        ):
            return []
    elif task.owner_service_key:
        owner = _unique_service_subscription(
            service_subscriptions,
            str(task.owner_service_key),
            agent_id=assigned_agent_id,
        )
        if owner is None:
            return []
    elif assigned_agent_id:
        assigned = [
            subscription
            for subscription in service_subscriptions
            if subscription.agent_id == assigned_agent_id
        ]
        # An Agent can serve several Workspace roles. A bare Agent assignment
        # cannot choose among those deployments by query order or recency.
        if len(assigned) != 1:
            return []
        owner = assigned[0]

    selected: list[AgentSubscription] = []
    selected_keys: set[str] = set()
    if owner is not None:
        selected.append(owner)
        selected_keys.add(str(owner.service_key))

    requested_keys = set(delegate_keys)
    if task.owner_service_key:
        requested_keys.add(str(task.owner_service_key))
    if not requested_keys and owner is None:
        requested_keys = {
            str(subscription.service_key)
            for subscription in service_subscriptions
        }

    for service_key in sorted(requested_keys - selected_keys):
        subscription = _unique_service_subscription(
            service_subscriptions,
            service_key,
        )
        if subscription is None:
            return []
        selected.append(subscription)
        selected_keys.add(service_key)

    return selected


async def resolve_task_execution(
    db: AsyncSession,
    task: Task,
    *,
    lock_subscriptions: bool = False,
) -> TaskExecutionDispatch:
    """Classify a Task into exactly one runtime surface.

    Custom ``task_type`` slugs deliberately fall through to ordinary background
    execution; only the two canonical types with distinct product semantics are
    special-cased.
    """
    task_id = str(task.id)
    task_type = str(getattr(task, "task_type", TaskType.GENERAL.value))
    if task_type == TaskType.INTERACTIVE.value:
        return TaskExecutionDispatch(TaskRuntimeKind.INTERACTIVE_SESSION, task.id)
    if task_type == TaskType.APPROVAL.value:
        return TaskExecutionDispatch(TaskRuntimeKind.HUMAN_DECISION, task.id)
    if getattr(task, "workspace_id", None):
        subscriptions = await task_plan_subscriptions(
            db,
            task,
            lock_for_update=lock_subscriptions,
        )
        if subscriptions:
            coordinator = CoordinatorRuntimeSelection.from_subscriptions(
                subscriptions
            )
            assert coordinator is not None
            return TaskExecutionDispatch(
                TaskRuntimeKind.COORDINATED_PLAN,
                task_id,
                task_id,
                coordinator=coordinator,
            )
        return TaskExecutionDispatch(TaskRuntimeKind.MANUAL, task_id)
    agent_id = getattr(task, "agent_id", None)
    agent_type = getattr(task, "agent_type", None)
    if agent_id or is_master_agent(agent_id, agent_type):
        return TaskExecutionDispatch(
            TaskRuntimeKind.LEGACY_AGENT,
            task_id,
            agent_id or MANOR_AGENT_ID,
        )
    return TaskExecutionDispatch(TaskRuntimeKind.MANUAL, task_id)


def dispatch_task_execution(task_id: str, dispatch: TaskExecutionDispatch) -> bool:
    """Publish only standalone legacy work after the caller commits.

    Coordinated Workspace starts are exclusively admitted from their durable
    Task intent; a direct V2 Planner enqueue would race the Task-level owner.
    """
    from packages.core.tasks.ai_tasks import run_agent_task

    if task_id != dispatch.task_id:
        raise ValueError("Task execution dispatch does not belong to this Task")
    if dispatch.runtime_kind is TaskRuntimeKind.PLAN_RESUME:
        raise ValueError(
            "Plan resume requires a transactionally staged Plan-cycle wake"
        )
    elif dispatch.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN:
        raise ValueError(
            "Coordinated Task start requires a transactionally staged Task execution intent"
        )
    elif dispatch.runtime_kind is TaskRuntimeKind.LEGACY_AGENT:
        if not dispatch.target_id:
            raise ValueError("Legacy Agent dispatch requires an Agent id")
        run_agent_task.delay(dispatch.task_id, dispatch.target_id)
    else:
        raise ValueError(
            f"Task runtime {dispatch.runtime_kind.value!r} is not dispatchable"
        )
    return True


def task_execution_intent_generation(task: Task) -> str:
    """Return the immutable generation for one requested Task start.

    ``updated_at`` is intentionally absent: editing a title after delivery
    must not manufacture a new execution.  The lifecycle clock identifies a
    new start/retry occurrence, while the routing fields make a delivery stale
    if its Workspace execution identity changes before a worker consumes it.
    """

    status_changed_at = task.status_changed_at
    intent = {
        "version": _TASK_EXECUTION_INTENT_VERSION,
        "task_id": task.id,
        "entity_id": task.entity_id,
        "workspace_id": task.workspace_id,
        "status": task.status,
        "status_changed_at": (
            status_changed_at.isoformat() if status_changed_at is not None else None
        ),
        "task_type": task.task_type,
        "agent_id": task.agent_id,
        "agent_type": task.agent_type,
        "owner_service_key": task.owner_service_key,
        "owner_subscription_id": task.owner_subscription_id,
        "delegate_service_keys": sorted(_clean_service_keys(task.delegate_service_keys)),
    }
    encoded = json.dumps(
        intent,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def task_execution_intent_dedupe_key(task_id: str, generation: str) -> str:
    return f"task-execution:{task_id}:{generation}"


async def stage_task_execution_intent(
    db: AsyncSession,
    task: Task,
    dispatch: TaskExecutionDispatch | None = None,
) -> str:
    """Stage the current coordinated start in the caller's transaction.

    Re-staging the same logical start atomically re-arms its existing row.
    This matters when several API requests race to recover the same Task: the
    unique intent generation collapses them without relying on process-local
    ``after_commit`` callbacks.
    """

    resolved = dispatch or await resolve_task_execution(db, task)
    if resolved.task_id != task.id:
        raise ValueError("Task execution dispatch does not belong to this Task")
    if resolved.runtime_kind is not TaskRuntimeKind.COORDINATED_PLAN:
        raise ValueError("Only coordinated Workspace Tasks use the durable start outbox")
    if task.status != TaskStatus.IN_PROGRESS.value:
        raise ValueError("A coordinated Task start must be in progress before staging")

    generation = task_execution_intent_generation(task)
    dedupe_key = task_execution_intent_dedupe_key(task.id, generation)
    now = datetime.now(timezone.utc)
    payload = {
        "task_id": task.id,
        "intent_generation": generation,
    }
    statement = pg_insert(RuntimeOutboxEvent).values(
        id=generate_ulid(),
        event_type=TASK_EXECUTION_OUTBOX_EVENT,
        aggregate_id=task.id,
        dedupe_key=dedupe_key,
        payload=payload,
        attempt_count=0,
        available_at=now,
        delivered_at=None,
        last_error=None,
        created_at=now,
    )
    await db.execute(statement.on_conflict_do_update(
        index_elements=[RuntimeOutboxEvent.dedupe_key],
        set_={
            "event_type": TASK_EXECUTION_OUTBOX_EVENT,
            "aggregate_id": task.id,
            "payload": payload,
            "attempt_count": 0,
            "available_at": now,
            "delivered_at": None,
            "last_error": None,
            "created_at": now,
        },
    ))
    return generation


def _dispatch_committed_tasks(session) -> None:
    if session.in_nested_transaction():
        return
    queued = session.info.pop(_ENQUEUE_KEY, [])
    for task_id, dispatch in dict.fromkeys((item[0], item[1]) for item in queued):
        try:
            dispatch_task_execution(task_id, dispatch)
        except Exception:
            logger.exception("Could not dispatch committed Task %s", task_id)


def _discard_rolled_back_tasks(session, previous_transaction) -> None:
    if not session.in_transaction():
        session.info.pop(_ENQUEUE_KEY, None)
        return
    retained = []
    for item in session.info.get(_ENQUEUE_KEY, []):
        owner = item[2]
        while owner is not None and owner is not previous_transaction:
            owner = owner.parent
        if owner is None:
            retained.append(item)
    session.info[_ENQUEUE_KEY] = retained


def queue_task_execution_after_commit(db: AsyncSession, task_id: str, dispatch: TaskExecutionDispatch) -> None:
    """Compatibility publisher for standalone legacy Agent work.

    Coordinated starts must use :func:`stage_task_execution_intent`; accepting
    them here would silently reintroduce a process-local post-commit delivery
    gap beside the durable outbox. Plan continuations likewise use the
    generation-fenced Plan-cycle outbox.
    """
    if not dispatch.is_dispatchable:
        raise ValueError(
            f"Task runtime {dispatch.runtime_kind.value!r} cannot be queued"
        )
    if dispatch.runtime_kind in {
        TaskRuntimeKind.COORDINATED_PLAN,
        TaskRuntimeKind.PLAN_RESUME,
    }:
        raise ValueError(
            "Coordinated starts and Plan resumes require a durable execution intent"
        )
    if dispatch.task_id != task_id:
        raise ValueError("Task execution dispatch does not belong to this Task")
    session = db.sync_session
    owner = session.get_nested_transaction() or session.get_transaction()
    session.info.setdefault(_ENQUEUE_KEY, []).append((task_id, dispatch, owner))
    if not event.contains(session, "after_commit", _dispatch_committed_tasks):
        event.listen(session, "after_commit", _dispatch_committed_tasks)
        event.listen(session, "after_soft_rollback", _discard_rolled_back_tasks)


async def queue_initial_task_execution(db: AsyncSession, task: Task, *, user_id: str) -> bool:
    """API assignment/start follows the same route as Task recovery."""
    dispatch = await resolve_task_execution(db, task)
    if not dispatch.is_dispatchable:
        return False
    if (
        dispatch.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN
        and task.status != TaskStatus.IN_PROGRESS
    ):
        from packages.core.services.task_service import update_task

        updated = await update_task(
            db, task.id, task.entity_id, user_id=user_id, manual_execution=True,
            dispatch_plan=False, status=TaskStatus.IN_PROGRESS.value,
        )
        if updated is not None:
            task = updated
    if dispatch.runtime_kind is TaskRuntimeKind.COORDINATED_PLAN:
        await stage_task_execution_intent(db, task, dispatch)
    else:
        # Standalone legacy Agent execution retains the compatibility publisher.
        queue_task_execution_after_commit(db, task.id, dispatch)
    return True
