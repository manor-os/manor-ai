"""Resolve the human requester for current and legacy Task rows."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import or_, select

from packages.core.constants.agents import (
    MANOR_AGENT_NAME,
    is_legacy_agent_author_placeholder,
    is_master_agent,
)


@dataclass(frozen=True)
class TaskRequesterResolution:
    user_id: str | None
    legacy_agent_creator: bool = False
    author_agent_id: str | None = None


class TaskRequesterIdentityError(RuntimeError):
    """A Task has no User that is currently allowed to execute it."""

    def __init__(self, task_id: str | None, reason: str) -> None:
        self.task_id = task_id
        self.reason = reason
        task_label = task_id or "unknown"
        super().__init__(f"Task {task_label} requester identity is invalid: {reason}")


def _value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None and key in mapping:
        return mapping[key]
    return getattr(row, key, None)


def _text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _is_public_customer_task(task: Any) -> bool:
    details = _value(task, "details")
    customer_context = details.get("customer_context") if isinstance(details, dict) else None
    return (
        isinstance(customer_context, dict)
        and customer_context.get("source") == "public_customer_chat"
    )


async def _has_trusted_system_task_provenance(db: Any, task: Any) -> bool:
    """Verify creator-less Task provenance against durable scoped records."""
    if _is_public_customer_task(task):
        return False
    details = _value(task, "details")
    if not isinstance(details, dict):
        return False
    task_id = _text(_value(task, "id"))
    entity_id = _text(_value(task, "entity_id"))
    workspace_id = _text(_value(task, "workspace_id"))
    if not task_id or not entity_id:
        return False

    review_id = _text(details.get("strategist_review_id"))
    if review_id and workspace_id:
        from packages.core.constants.proposal import ProposalItemKind
        from packages.core.models.proposal import ProposalItemRecord, ProposalRecord

        proposal_item_id = (await db.execute(
            select(ProposalItemRecord.id)
            .join(
                ProposalRecord,
                ProposalItemRecord.proposal_id == ProposalRecord.id,
            )
            .where(
                ProposalRecord.review_id == review_id,
                ProposalRecord.entity_id == entity_id,
                ProposalRecord.workspace_id == workspace_id,
                ProposalItemRecord.entity_id == entity_id,
                ProposalItemRecord.workspace_id == workspace_id,
                ProposalItemRecord.kind == ProposalItemKind.TASK,
                ProposalItemRecord.payload["task_id"].astext == task_id,
            )
            .limit(1)
        )).scalar_one_or_none()
        if proposal_item_id:
            return True

        # Legacy Strategist reviews predate Proposal/ProposalItem rows. Once
        # approved, however, their exact task cohort is durably linked through
        # a scoped WorkspaceWorkBatch. Use that receipt as compatibility proof
        # instead of trusting the task's self-reported review marker.
        work_batch_id = _text(details.get("workspace_work_batch_id"))
        if work_batch_id:
            from packages.core.models.workspace import WorkspaceWorkBatch

            work_batch = (await db.execute(
                select(WorkspaceWorkBatch).where(
                    WorkspaceWorkBatch.id == work_batch_id,
                    WorkspaceWorkBatch.entity_id == entity_id,
                    WorkspaceWorkBatch.workspace_id == workspace_id,
                    WorkspaceWorkBatch.source_kind == "strategist_proposal",
                )
            )).scalar_one_or_none()
            batch_details = _value(work_batch, "details")
            batch_task_ids = _value(work_batch, "task_ids")
            if (
                work_batch is not None
                and isinstance(batch_details, dict)
                and _text(batch_details.get("strategist_review_id")) == review_id
                and isinstance(batch_task_ids, list)
                and task_id in {_text(value) for value in batch_task_ids}
            ):
                return True

    scheduled_job_id = _text(details.get("scheduled_job_id"))
    scheduled_run_id = _text(details.get("scheduled_run_id"))
    if scheduled_job_id and scheduled_run_id:
        from packages.core.constants.task import TaskLogType
        from packages.core.constants.task_actors import (
            TASK_ACTOR_META_KEY,
            TaskActor,
        )
        from packages.core.models.task import TaskLog

        creation_receipt_query = select(TaskLog.id).where(
            TaskLog.task_id == task_id,
            TaskLog.log_type == TaskLogType.CREATE,
            TaskLog.meta[TASK_ACTOR_META_KEY].astext == TaskActor.SYSTEM.value,
            TaskLog.meta["entity_id"].astext == entity_id,
            TaskLog.meta["scheduled_job_id"].astext == scheduled_job_id,
            TaskLog.meta["scheduled_run_id"].astext == scheduled_run_id,
        )
        if workspace_id:
            creation_receipt_query = creation_receipt_query.where(
                TaskLog.meta["workspace_id"].astext == workspace_id,
            )
        else:
            creation_receipt_query = creation_receipt_query.where(
                TaskLog.meta["workspace_id"].astext.is_(None),
            )
        creation_receipt_id = (await db.execute(
            creation_receipt_query.limit(1)
        )).scalar_one_or_none()
        if creation_receipt_id:
            return True

    if scheduled_job_id:
        from packages.core.models.scheduler import ScheduledJob

        scheduled_query = select(ScheduledJob.id).where(
            ScheduledJob.entity_id == entity_id,
            ScheduledJob.manor_task_id == task_id,
            or_(
                ScheduledJob.id == scheduled_job_id,
                ScheduledJob.job_id == scheduled_job_id,
            ),
        )
        if workspace_id:
            scheduled_query = scheduled_query.where(
                ScheduledJob.workspace_id == workspace_id,
            )
        else:
            scheduled_query = scheduled_query.where(
                ScheduledJob.workspace_id.is_(None),
            )
        if (await db.execute(scheduled_query.limit(1))).scalar_one_or_none():
            return True

    if details.get("seeded_by") == "sandbox_demo" and workspace_id:
        from packages.core.models.workspace import Workspace
        from packages.core.workspaces.sandbox import is_sandbox_workspace

        workspace = (await db.execute(
            select(Workspace).where(
                Workspace.id == workspace_id,
                Workspace.entity_id == entity_id,
                Workspace.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if workspace is not None and is_sandbox_workspace(workspace):
            return True

    return False


async def resolve_task_requesters(
    db: Any,
    tasks: Iterable[Any],
) -> dict[str, TaskRequesterResolution]:
    """Resolve Task requester Users without trusting mutable runtime context.

    Current Agent authorship comes from the immutable CREATE TaskLog. Older
    Workspace Agent task creation stored the Agent id in ``Task.creator_id``;
    those rows are recovered from the matching ``workspace_agent.task_created``
    activity. ``runtime_context.captured_from`` is deliberately ignored because
    it records the latest requirements source and changes when another user
    edits the Task.
    """

    task_rows = [task for task in tasks if _text(_value(task, "id"))]
    if not task_rows:
        return {}

    from packages.core.constants.task import TaskLogType
    from packages.core.models.task import TaskLog
    from packages.core.models.workspace import Agent, WorkspaceActivity

    task_ids = {
        task_id
        for task in task_rows
        if (task_id := _text(_value(task, "id")))
    }
    create_log_rows = (await db.execute(
        select(
            TaskLog.task_id.label("task_id"),
            TaskLog.meta.label("meta"),
        )
        .where(
            TaskLog.task_id.in_(task_ids),
            TaskLog.log_type == TaskLogType.CREATE,
        )
        .order_by(TaskLog.created_at.asc(), TaskLog.id.asc())
    )).all()
    create_log_agent_ids: dict[str, str | None] = {}
    for log_row in create_log_rows:
        task_id = _text(_value(log_row, "task_id"))
        if not task_id or task_id in create_log_agent_ids:
            continue
        metadata = _value(log_row, "meta")
        create_log_agent_ids[task_id] = (
            _text(metadata.get("agent_id")) if isinstance(metadata, dict) else None
        )

    creator_ids = {
        creator_id
        for task in task_rows
        if (creator_id := _text(_value(task, "creator_id")))
    }
    stored_agent_entities: dict[str, str | None] = {}
    if creator_ids:
        agent_rows = await db.execute(
            select(Agent.id, Agent.entity_id).where(Agent.id.in_(creator_ids))
        )
        stored_agent_entities = {
            str(agent_id): str(agent_entity_id) if agent_entity_id else None
            for agent_id, agent_entity_id in agent_rows.all()
        }

    resolutions: dict[str, TaskRequesterResolution] = {}
    for task in task_rows:
        task_id = _text(_value(task, "id"))
        creator_id = _text(_value(task, "creator_id"))
        entity_id = _text(_value(task, "entity_id"))
        assert task_id is not None

        agent_entity_id = stored_agent_entities.get(creator_id or "")
        legacy_agent_creator = bool(
            creator_id
            and (
                is_master_agent(creator_id)
                or creator_id == MANOR_AGENT_NAME
                or is_legacy_agent_author_placeholder(creator_id)
                or (
                    creator_id in stored_agent_entities
                    and (agent_entity_id is None or agent_entity_id == entity_id)
                )
            )
        )
        if legacy_agent_creator:
            author_agent_id = create_log_agent_ids.get(task_id) or (
                creator_id
                if creator_id in stored_agent_entities or is_master_agent(creator_id)
                else None
            )
            resolutions[task_id] = TaskRequesterResolution(
                user_id=None,
                legacy_agent_creator=True,
                author_agent_id=author_agent_id,
            )
            continue

        resolutions[task_id] = TaskRequesterResolution(
            user_id=creator_id,
            author_agent_id=create_log_agent_ids.get(task_id),
        )

    activity_tasks = {
        _text(_value(task, "id")): task
        for task in task_rows
        if _text(_value(task, "workspace_id"))
        and _text(_value(task, "entity_id"))
    }
    activity_tasks = {
        task_id: task for task_id, task in activity_tasks.items() if task_id
    }
    if not activity_tasks:
        return resolutions

    activity_workspace_ids = {
        _text(_value(task, "workspace_id")) for task in activity_tasks.values()
    }
    activity_entity_ids = {
        _text(_value(task, "entity_id")) for task in activity_tasks.values()
    }
    activity_rows = (await db.execute(
        select(
            WorkspaceActivity.details["task_id"].astext.label("task_id"),
            WorkspaceActivity.workspace_id,
            WorkspaceActivity.entity_id,
            WorkspaceActivity.user_id,
            WorkspaceActivity.agent_id,
        )
        .where(
            WorkspaceActivity.event_type == "workspace_agent.task_created",
            WorkspaceActivity.workspace_id.in_(activity_workspace_ids),
            WorkspaceActivity.entity_id.in_(activity_entity_ids),
            WorkspaceActivity.details["task_id"].astext.in_(activity_tasks),
        )
    )).all()

    activities_by_task: dict[str, list[Any]] = {}
    for activity in activity_rows:
        task_id = _text(_value(activity, "task_id"))
        task = activity_tasks.get(task_id or "")
        if not task or not task_id:
            continue
        if _text(_value(activity, "entity_id")) != _text(_value(task, "entity_id")):
            continue
        task_workspace_id = _text(_value(task, "workspace_id"))
        if task_workspace_id and _text(_value(activity, "workspace_id")) != task_workspace_id:
            continue
        activities_by_task.setdefault(task_id, []).append(activity)

    for task_id, task in activity_tasks.items():
        activities = activities_by_task.get(task_id, [])
        if not activities:
            continue
        resolution = resolutions[task_id]
        creator_id = _text(_value(task, "creator_id"))
        user_ids = {
            user_id
            for activity in activities
            if (user_id := _text(_value(activity, "user_id")))
        }
        activity_agent_ids = [
            _text(_value(activity, "agent_id")) for activity in activities
        ]
        agent_ids = {agent_id for agent_id in activity_agent_ids if agent_id}
        has_missing_agent = any(agent_id is None for agent_id in activity_agent_ids)

        if creator_id in stored_agent_entities:
            agent_evidence_matches = (
                not has_missing_agent and agent_ids == {creator_id}
            )
        elif is_master_agent(creator_id):
            agent_evidence_matches = (
                not has_missing_agent
                and bool(agent_ids)
                and all(is_master_agent(agent_id) for agent_id in agent_ids)
            )
        elif resolution.legacy_agent_creator:
            agent_evidence_matches = not has_missing_agent and len(agent_ids) == 1
        else:
            agent_evidence_matches = False

        author_agent_id = resolution.author_agent_id
        if author_agent_id is None and len(agent_ids) == 1 and not has_missing_agent:
            activity_author_matches_requester = (
                resolution.user_id is not None
                and user_ids == {resolution.user_id}
            )
            if resolution.legacy_agent_creator or activity_author_matches_requester:
                author_agent_id = next(iter(agent_ids))

        user_id = resolution.user_id
        if (
            resolution.legacy_agent_creator
            and not _is_public_customer_task(task)
            and agent_evidence_matches
            and len(user_ids) == 1
        ):
            user_id = next(iter(user_ids))
        resolutions[task_id] = TaskRequesterResolution(
            user_id=user_id,
            legacy_agent_creator=resolution.legacy_agent_creator,
            author_agent_id=author_agent_id,
        )

    return resolutions


async def resolve_task_requester(
    db: Any,
    task: Any,
) -> TaskRequesterResolution:
    task_id = _text(_value(task, "id"))
    if not task_id:
        return TaskRequesterResolution(user_id=_text(_value(task, "creator_id")))
    return (await resolve_task_requesters(db, [task]))[task_id]


async def resolve_task_execution_user_id(db: Any, task: Any) -> str | None:
    """Return a currently executable User for a Task.

    Historical attribution and execution authority are deliberately separate:
    an inactive former member remains the displayed requester, but cannot own
    new model, billing, or tool calls.
    """

    resolution = await resolve_task_requester(db, task)
    task_id = _text(_value(task, "id"))
    entity_id = _text(_value(task, "entity_id"))
    if not entity_id:
        raise TaskRequesterIdentityError(task_id, "the Task has no entity scope")

    from packages.core.models.staff import Staff
    from packages.core.models.user import User, UserMembership

    execution_user_id = resolution.user_id
    if not execution_user_id:
        if resolution.legacy_agent_creator:
            raise TaskRequesterIdentityError(
                task_id,
                "no trusted workspace_agent.task_created activity identifies a User",
            )
        fallback_ids = [
            candidate
            for key in ("owner_id", "assignee_id", "user_id")
            if (candidate := _text(_value(task, key)))
        ]
        if fallback_ids:
            known_user_ids = set(
                (
                    await db.execute(
                        select(User.id).where(User.id.in_(fallback_ids))
                    )
                ).scalars()
            )
            execution_user_id = next(
                (candidate for candidate in fallback_ids if candidate in known_user_ids),
                None,
            )
        if execution_user_id is None:
            if await _has_trusted_system_task_provenance(db, task):
                return None
            raise TaskRequesterIdentityError(
                task_id,
                "no trusted User or explicit system provenance owns the Task",
            )

    executable_user = (
        await db.execute(
            select(User).where(
                User.id == execution_user_id,
                User.status == "active",
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if executable_user is None:
        raise TaskRequesterIdentityError(
            task_id,
            "the requester is inactive or no longer belongs to the Task entity",
        )

    memberships = list(
        (
            await db.execute(
                select(UserMembership).where(
                    UserMembership.user_id == execution_user_id,
                    UserMembership.entity_id == entity_id,
                )
            )
        ).scalars()
    )
    staff_rows = list(
        (
            await db.execute(
                select(Staff).where(
                    Staff.user_id == execution_user_id,
                    Staff.entity_id == entity_id,
                )
            )
        ).scalars()
    )
    active_memberships = [
        membership
        for membership in memberships
        if membership.status == "active" and membership.deleted_at is None
    ]
    active_staff = [
        staff
        for staff in staff_rows
        if staff.status == "active" and staff.deleted_at is None
    ]

    explicit_membership_is_valid = not memberships or len(active_memberships) == 1
    explicit_staff_is_valid = not staff_rows or len(active_staff) == 1
    has_explicit_entity_link = bool(memberships or staff_rows)
    legacy_primary_entity_is_valid = (
        not has_explicit_entity_link and executable_user.entity_id == entity_id
    )
    if not (
        explicit_membership_is_valid
        and explicit_staff_is_valid
        and (has_explicit_entity_link or legacy_primary_entity_is_valid)
    ):
        raise TaskRequesterIdentityError(
            task_id,
            "the requester is inactive or no longer belongs to the Task entity",
        )
    return execution_user_id
