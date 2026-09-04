from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from packages.core.constants.task import TaskLogType
from packages.core.dispatcher.service import Dispatcher
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.staff import Staff
from packages.core.models.task import Task, TaskLog
from packages.core.models.user import User, UserMembership
from packages.core.models.worker import SubscriptionWorker, WorkLease, Worker
from packages.core.models.workspace import (
    Agent,
    AgentSubscription,
    Workspace,
    WorkspaceActivity,
)


@pytest.mark.asyncio
async def test_legacy_task_requester_comes_from_creation_event_not_captured_from(
    db_session,
) -> None:
    from packages.core.services.task_requester_identity import (
        TaskRequesterIdentityError,
        resolve_task_execution_user_id,
        resolve_task_requester,
    )
    from packages.core.services.auth_service import hash_password
    from packages.core.ai.runtime.billing import (
        runtime_resolve_task_billable_user_id,
    )

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    agent_id = generate_ulid()
    task_id = generate_ulid()
    original_user_id = generate_ulid()
    latest_editor_id = generate_ulid()
    db_session.add_all([
        Agent(
            id=agent_id,
            entity_id=entity_id,
            name="Legacy task creator",
            config={},
            status="active",
        ),
        User(
            id=original_user_id,
            entity_id=entity_id,
            email="legacy-task-requester@test.com",
            display_name="Legacy Task Requester",
            password_hash=hash_password("pass123"),
            role="member",
            status="active",
        ),
    ])
    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Legacy Agent-created Task",
        creator_id=agent_id,
        owner_id=agent_id,
        details={
            "runtime_context": {
                "captured_from": {"user_id": latest_editor_id},
            },
        },
    )
    db_session.add_all(
        [
            task,
            WorkspaceActivity(
                workspace_id=workspace_id,
                entity_id=entity_id,
                event_type="workspace_agent.task_created",
                summary="Workspace Agent created task",
                details={"task_id": task_id},
                user_id=original_user_id,
                agent_id=agent_id,
            ),
        ]
    )
    await db_session.flush()

    resolution = await resolve_task_requester(db_session, task)
    assert resolution.legacy_agent_creator is True
    assert resolution.user_id == original_user_id
    assert resolution.author_agent_id == agent_id
    assert await resolve_task_execution_user_id(db_session, task) == original_user_id
    assert (
        await runtime_resolve_task_billable_user_id(db_session, task)
        == original_user_id
    )

    unaudited_task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Legacy task without trusted requester evidence",
        creator_id=agent_id,
        details={
            "runtime_context": {
                "captured_from": {"user_id": latest_editor_id},
            },
        },
    )
    db_session.add(unaudited_task)
    await db_session.flush()
    with pytest.raises(TaskRequesterIdentityError):
        await resolve_task_execution_user_id(db_session, unaudited_task)
    with pytest.raises(TaskRequesterIdentityError):
        await runtime_resolve_task_billable_user_id(db_session, unaudited_task)

    normal_task = Task(
        id=generate_ulid(),
        title="Normal User-created Task",
        creator_id=original_user_id,
        entity_id=entity_id,
        details={},
    )
    assert await resolve_task_execution_user_id(db_session, normal_task) == original_user_id

    owner_fallback_task = Task(
        id=generate_ulid(),
        title="Legacy Task with a User owner",
        creator_id=None,
        owner_id=original_user_id,
        entity_id=entity_id,
        details={},
    )
    assert (
        await resolve_task_execution_user_id(db_session, owner_fallback_task)
        == original_user_id
    )

    conflicting_task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Legacy Task with conflicting requester evidence",
        creator_id=agent_id,
        details={},
    )
    mismatched_agent_id = generate_ulid()
    mismatched_agent_task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Legacy Task with mismatched Agent evidence",
        creator_id=agent_id,
        details={},
    )
    db_session.add_all(
        [
            Agent(
                id=mismatched_agent_id,
                entity_id=entity_id,
                name="Different Agent",
                config={},
                status="active",
            ),
            conflicting_task,
            WorkspaceActivity(
                workspace_id=workspace_id,
                entity_id=entity_id,
                event_type="workspace_agent.task_created",
                summary="First conflicting creation event",
                details={"task_id": conflicting_task.id},
                user_id=original_user_id,
                agent_id=agent_id,
            ),
            WorkspaceActivity(
                workspace_id=workspace_id,
                entity_id=entity_id,
                event_type="workspace_agent.task_created",
                summary="Second conflicting creation event",
                details={"task_id": conflicting_task.id},
                user_id=latest_editor_id,
                agent_id=agent_id,
            ),
            mismatched_agent_task,
            WorkspaceActivity(
                workspace_id=workspace_id,
                entity_id=entity_id,
                event_type="workspace_agent.task_created",
                summary="Creation event from a different Agent",
                details={"task_id": mismatched_agent_task.id},
                user_id=original_user_id,
                agent_id=mismatched_agent_id,
            ),
        ]
    )
    await db_session.flush()
    with pytest.raises(TaskRequesterIdentityError):
        await resolve_task_execution_user_id(db_session, conflicting_task)
    with pytest.raises(TaskRequesterIdentityError):
        await resolve_task_execution_user_id(db_session, mismatched_agent_task)

    inactive_membership_user_id = generate_ulid()
    inactive_membership_task = Task(
        id=generate_ulid(),
        title="Task from revoked primary membership",
        creator_id=inactive_membership_user_id,
        entity_id=entity_id,
        details={},
    )
    inactive_membership_id = generate_ulid()
    db_session.add_all(
        [
            User(
                id=inactive_membership_user_id,
                entity_id=entity_id,
                email="revoked-task-membership@test.com",
                display_name="Revoked Task Membership",
                password_hash=hash_password("pass123"),
                role="member",
                status="active",
            ),
            UserMembership(
                id=inactive_membership_id,
                user_id=inactive_membership_user_id,
                entity_id=entity_id,
                role="member",
                status="inactive",
                is_primary=True,
            ),
        ]
    )
    await db_session.flush()
    with pytest.raises(TaskRequesterIdentityError):
        await resolve_task_execution_user_id(db_session, inactive_membership_task)
    with pytest.raises(TaskRequesterIdentityError):
        await runtime_resolve_task_billable_user_id(
            db_session,
            inactive_membership_task,
        )

    inactive_staff_user_id = generate_ulid()
    inactive_staff_id = generate_ulid()
    inactive_staff_task = Task(
        id=generate_ulid(),
        title="Task from inactive Staff assignment",
        creator_id=inactive_staff_user_id,
        entity_id=entity_id,
        details={},
    )
    db_session.add(
        User(
            id=inactive_staff_user_id,
            entity_id=entity_id,
            email="inactive-task-staff@test.com",
            display_name="Inactive Task Staff",
            password_hash=hash_password("pass123"),
            role="member",
            status="active",
        )
    )
    await db_session.flush()
    db_session.add_all(
        [
            Staff(
                id=inactive_staff_id,
                entity_id=entity_id,
                user_id=inactive_staff_user_id,
                name="Inactive Task Staff",
                email="inactive-task-staff@test.com",
                status="inactive",
            ),
            UserMembership(
                id=generate_ulid(),
                user_id=inactive_staff_user_id,
                entity_id=entity_id,
                role="member",
                status="active",
                staff_id=inactive_staff_id,
                is_primary=True,
            ),
        ]
    )
    await db_session.flush()
    with pytest.raises(TaskRequesterIdentityError):
        await resolve_task_execution_user_id(db_session, inactive_staff_task)
    with pytest.raises(TaskRequesterIdentityError):
        await runtime_resolve_task_billable_user_id(db_session, inactive_staff_task)

    dangling_task = Task(
        id=generate_ulid(),
        title="Task with a missing creator User",
        creator_id=generate_ulid(),
        entity_id=entity_id,
        details={},
    )
    with pytest.raises(TaskRequesterIdentityError):
        await resolve_task_execution_user_id(db_session, dangling_task)
    with pytest.raises(TaskRequesterIdentityError):
        await runtime_resolve_task_billable_user_id(db_session, dangling_task)

    system_task = Task(
        id=generate_ulid(),
        title="System Task with an Agent owner",
        creator_id=None,
        owner_id=agent_id,
        entity_id=entity_id,
        details={"scheduled_job_id": "scheduler:test-system-task"},
    )
    system_job = ScheduledJob(
        job_id="scheduler:test-system-task",
        entity_id=entity_id,
        manor_task_id=system_task.id,
    )
    db_session.add_all([system_task, system_job])
    await db_session.flush()
    assert await resolve_task_execution_user_id(db_session, system_task) is None
    assert await runtime_resolve_task_billable_user_id(db_session, system_task) is None

    forged_system_task = Task(
        id=generate_ulid(),
        title="Task with a forged system marker",
        creator_id=None,
        owner_id=agent_id,
        entity_id=entity_id,
        details={"strategist_review_id": generate_ulid()},
    )
    with pytest.raises(TaskRequesterIdentityError):
        await resolve_task_execution_user_id(db_session, forged_system_task)

    unattributed_task = Task(
        id=generate_ulid(),
        title="Task without trusted execution provenance",
        creator_id=None,
        owner_id=agent_id,
        entity_id=entity_id,
        details={},
    )
    with pytest.raises(TaskRequesterIdentityError):
        await resolve_task_execution_user_id(db_session, unattributed_task)

    public_customer_task = Task(
        id=generate_ulid(),
        title="Public customer Task without an internal requester",
        creator_id=None,
        entity_id=entity_id,
        details={"customer_context": {"source": "public_customer_chat"}},
    )
    with pytest.raises(TaskRequesterIdentityError):
        await resolve_task_execution_user_id(db_session, public_customer_task)

    agent = await db_session.get(Agent, agent_id)
    agent.status = "inactive"
    await db_session.flush()
    assert await resolve_task_execution_user_id(db_session, task) == original_user_id

    requester = await db_session.get(User, original_user_id)
    requester.status = "inactive"
    await db_session.flush()
    inactive_resolution = await resolve_task_requester(db_session, task)
    assert inactive_resolution.user_id == original_user_id
    with pytest.raises(TaskRequesterIdentityError):
        await resolve_task_execution_user_id(db_session, task)
    with pytest.raises(TaskRequesterIdentityError):
        await resolve_task_execution_user_id(db_session, normal_task)
    with pytest.raises(TaskRequesterIdentityError):
        await runtime_resolve_task_billable_user_id(db_session, normal_task)

    task.details = {
        **task.details,
        "customer_context": {"source": "public_customer_chat"},
    }
    public_resolution = await resolve_task_requester(db_session, task)
    assert public_resolution.user_id is None
    with pytest.raises(TaskRequesterIdentityError):
        await resolve_task_execution_user_id(db_session, task)


@pytest.mark.asyncio
async def test_task_agent_author_uses_create_log_and_matching_activity_user(
    db_session,
) -> None:
    from packages.core.services.task_requester_identity import resolve_task_requester
    from packages.core.services.auth_service import hash_password

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    creator_id = generate_ulid()
    other_user_id = generate_ulid()
    agent_id = generate_ulid()
    canonical_task_id = generate_ulid()
    mismatched_task_id = generate_ulid()
    matching_task_id = generate_ulid()
    db_session.add_all([
        User(
            id=creator_id,
            entity_id=entity_id,
            email="canonical-task-creator@test.com",
            display_name="Canonical Task Creator",
            password_hash=hash_password("pass123"),
            role="member",
            status="active",
        ),
        Agent(
            id=agent_id,
            entity_id=entity_id,
            name="Canonical Task Agent",
            config={},
            status="active",
        ),
        Task(
            id=canonical_task_id,
            entity_id=entity_id,
            title="Task with canonical Agent author",
            creator_id=creator_id,
        ),
        TaskLog(
            task_id=canonical_task_id,
            log_type=TaskLogType.CREATE,
            content="Task created",
            created_by="Canonical Task Agent",
            meta={"agent_id": agent_id, "agent_name": "Canonical Task Agent"},
        ),
        Task(
            id=mismatched_task_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="User Task with unrelated activity",
            creator_id=creator_id,
        ),
        WorkspaceActivity(
            workspace_id=workspace_id,
            entity_id=entity_id,
            event_type="workspace_agent.task_created",
            summary="Unrelated requester evidence",
            details={"task_id": mismatched_task_id},
            user_id=other_user_id,
            agent_id=agent_id,
        ),
        Task(
            id=matching_task_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Historical User Task with matching Agent activity",
            creator_id=creator_id,
        ),
        WorkspaceActivity(
            workspace_id=workspace_id,
            entity_id=entity_id,
            event_type="workspace_agent.task_created",
            summary="Matching historical author evidence",
            details={"task_id": matching_task_id},
            user_id=creator_id,
            agent_id=agent_id,
        ),
    ])
    await db_session.flush()

    canonical = await resolve_task_requester(
        db_session,
        await db_session.get(Task, canonical_task_id),
    )
    assert canonical.user_id == creator_id
    assert canonical.author_agent_id == agent_id

    mismatched = await resolve_task_requester(
        db_session,
        await db_session.get(Task, mismatched_task_id),
    )
    assert mismatched.user_id == creator_id
    assert mismatched.author_agent_id is None

    matching = await resolve_task_requester(
        db_session,
        await db_session.get(Task, matching_task_id),
    )
    assert matching.user_id == creator_id
    assert matching.author_agent_id == agent_id


@pytest.mark.asyncio
async def test_scheduled_task_creation_receipt_is_valid_system_provenance(
    db_session,
) -> None:
    from packages.core.services.task_requester_identity import (
        resolve_task_execution_user_id,
    )

    entity_id = generate_ulid()
    task = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        title="Older scheduled occurrence",
        creator_id=None,
        details={
            "scheduled_job_id": "scheduler:daily",
            "scheduled_run_id": "run-older-occurrence",
        },
    )
    db_session.add_all([
        task,
        TaskLog(
            task_id=task.id,
            log_type=TaskLogType.CREATE,
            content="Task created by scheduler",
            created_by="system",
            meta={
                "actor_kind": "system",
                "entity_id": entity_id,
                "workspace_id": None,
                "scheduled_job_id": "scheduler:daily",
                "scheduled_run_id": "run-older-occurrence",
            },
        ),
    ])
    await db_session.flush()

    assert await resolve_task_execution_user_id(db_session, task) is None


@pytest.mark.asyncio
async def test_internal_worker_rejects_legacy_task_without_executable_requester(
    client,
    monkeypatch,
) -> None:
    import packages.core.database as dbmod
    from packages.core.workers import internal

    entity_id = generate_ulid()
    agent_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
    handler_calls: list[dict] = []

    async def should_not_execute(snapshot: dict) -> dict:
        handler_calls.append(snapshot)
        return {"result": {"text": "must not run"}, "cost": {"usd": 1}}

    monkeypatch.setattr(internal, "_execute_by_kind", should_not_execute)

    async with dbmod.async_session() as db:
        db.add_all(
            [
                Agent(
                    id=agent_id,
                    entity_id=entity_id,
                    name="Legacy stored creator",
                    config={},
                    status="active",
                ),
                Task(
                    id=task_id,
                    entity_id=entity_id,
                    title="Unaudited legacy Task",
                    creator_id=agent_id,
                ),
                Worker(
                    id=worker_id,
                    entity_id=entity_id,
                    kind="internal",
                    display_name="Internal worker",
                    capabilities={
                        "supported_kinds": ["subagent"],
                        "max_risk_level": "high",
                    },
                    monthly_spent_usd=Decimal("0"),
                    auto_pause_on_budget=True,
                    status="active",
                ),
                ExecutionPlan(
                    id=plan_id,
                    entity_id=entity_id,
                    task_id=task_id,
                    status="running",
                    execution_mode="live",
                    approval_required=False,
                    plan_dag={"steps": []},
                ),
                ExecutionStep(
                    id=step_id,
                    plan_id=plan_id,
                    entity_id=entity_id,
                    step_key="must_not_execute",
                    kind="subagent",
                    params={"prompt": "Do not run"},
                    depends_on=[],
                    step_status="running",
                    attempt_count=1,
                    max_attempts=1,
                    current_lease_id=lease_id,
                ),
                WorkLease(
                    id=lease_id,
                    step_id=step_id,
                    plan_id=plan_id,
                    entity_id=entity_id,
                    worker_id=worker_id,
                    lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
                    status="active",
                ),
            ]
        )
        await db.commit()

    outcome = await internal.execute_lease_inproc(lease_id)

    async with dbmod.async_session() as db:
        lease = await db.get(WorkLease, lease_id)
        step = await db.get(ExecutionStep, step_id)

    assert handler_calls == []
    assert outcome["outcome"] == "failed"
    assert outcome["error"]["type"] == "TaskRequesterIdentityError"
    assert lease.status == "failed"
    assert step.step_status == "failed"
    assert step.error["type"] == "TaskRequesterIdentityError"




@pytest.mark.asyncio
async def test_dispatcher_resolves_step_refs_before_leasing(client) -> None:
    """Worker checkout must receive self-contained params.

    PlanExecutor resolves refs too, but worker heartbeats can race ahead after
    dependencies finish. Dispatcher is the last safe boundary before a payload
    leaves the runtime.
    """
    import packages.core.database as dbmod

    entity_id = generate_ulid()
    plan_id = generate_ulid()
    worker = Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind="internal",
        display_name="Internal worker",
        capabilities={"supported_kinds": ["llm"], "max_risk_level": "high"},
        monthly_spent_usd=Decimal("0"),
        auto_pause_on_budget=True,
        status="active",
    )

    async with dbmod.async_session() as db:
        db.add(worker)
        db.add(
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                status="running",
                execution_mode="live",
                approval_required=False,
                plan_dag={"steps": []},
            )
        )
        db.add(
            ExecutionStep(
                id=generate_ulid(),
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="draft",
                kind="llm",
                params={"prompt": "draft"},
                result={"text": "resolved draft", "metadata": {"score": 0.91}},
                depends_on=[],
                step_status="done",
                attempt_count=1,
                max_attempts=3,
            )
        )
        pending = ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            step_key="finalize",
            kind="llm",
            params={
                "prompt": "Use this upstream output: ${{ steps.draft.result.text }}",
                "native": "${{ steps.draft.result.metadata }}",
            },
            depends_on=["draft"],
            step_status="pending",
            attempt_count=0,
            max_attempts=3,
        )
        db.add(pending)
        await db.flush()

        leases = await Dispatcher().checkout_steps_for_worker(db, worker, max_n=1)

        assert len(leases) == 1
        _, leased_step = leases[0]
        assert leased_step.id == pending.id
        assert leased_step.step_status == "running"
        assert leased_step.params["prompt"] == "Use this upstream output: resolved draft"
        assert leased_step.params["native"] == {"score": 0.91}
        assert "${{" not in str(leased_step.params)


@pytest.mark.asyncio
async def test_dispatcher_hydrates_agent_id_from_workspace_subscription(client) -> None:
    import packages.core.database as dbmod

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    plan_id = generate_ulid()
    agent_id = generate_ulid()
    subscription_id = generate_ulid()
    worker = Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind="internal",
        display_name="Internal worker",
        capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
        monthly_spent_usd=Decimal("0"),
        auto_pause_on_budget=True,
        status="active",
    )

    async with dbmod.async_session() as db:
        db.add(worker)
        db.add(
            Workspace(
                id=workspace_id,
                entity_id=entity_id,
                name="Product Builder Workspace",
                status="active",
            )
        )
        db.add(
            Agent(
                id=agent_id,
                entity_id=entity_id,
                name="Product Builder",
                slug="product-builder",
                status="active",
            )
        )
        db.add(
            AgentSubscription(
                id=subscription_id,
                entity_id=entity_id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                name="Product Builder",
                service_key="product_ship",
                status="active",
            )
        )
        db.add(
            SubscriptionWorker(
                subscription_id=subscription_id,
                worker_id=worker.id,
                priority=100,
                is_preferred=True,
            )
        )
        db.add(
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                status="running",
                execution_mode="live",
                approval_required=False,
                plan_dag={"steps": []},
            )
        )
        pending = ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            service_key="product_ship",
            step_key="generate_component_checklist",
            kind="subagent",
            params={"prompt": "Generate checklist"},
            depends_on=[],
            step_status="pending",
            attempt_count=0,
            max_attempts=3,
        )
        db.add(pending)
        await db.flush()

        leases = await Dispatcher().checkout_steps_for_worker(db, worker, max_n=1)

        assert len(leases) == 1
        lease, leased_step = leases[0]
        assert leased_step.id == pending.id
        assert leased_step.resolved_subscription_id == subscription_id
        assert leased_step.resolved_agent_id == agent_id
        assert lease.subscription_id == subscription_id


@pytest.mark.asyncio
async def test_dispatcher_rejects_action_not_bound_to_resolved_service_agent(client) -> None:
    import packages.core.database as dbmod
    from packages.core.models.mcp import AgentMCPBinding, MCPServer

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    plan_id = generate_ulid()
    agent_id = generate_ulid()
    subscription_id = generate_ulid()
    mcp_server_id = generate_ulid()
    provider_key = f"scope_{generate_ulid()[:12].lower()}"
    worker = Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind="internal",
        display_name="Internal worker",
        capabilities={"supported_kinds": ["action"], "max_risk_level": "high"},
        monthly_spent_usd=Decimal("0"),
        auto_pause_on_budget=True,
        status="active",
    )

    async with dbmod.async_session() as db:
        db.add(worker)
        db.add(
            Workspace(
                id=workspace_id,
                entity_id=entity_id,
                name="Content Publisher Workspace",
                status="active",
            )
        )
        db.add(
            Agent(
                id=agent_id,
                entity_id=entity_id,
                name="Content Publisher",
                slug="content-publisher",
                status="active",
            )
        )
        db.add(
            AgentSubscription(
                id=subscription_id,
                entity_id=entity_id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                name="Content Publisher",
                service_key="content_ops",
                status="active",
            )
        )
        db.add(
            SubscriptionWorker(
                subscription_id=subscription_id,
                worker_id=worker.id,
                priority=100,
                is_preferred=True,
            )
        )
        db.add(
            MCPServer(
                id=mcp_server_id,
                server_key=provider_key,
                name="X",
                transport="builtin",
                auth_type="oauth2",
                tools_cached={
                    "tools": [
                        {"name": "publish_tweet"},
                        {"name": "search_tweets"},
                    ]
                },
                status="active",
            )
        )
        db.add(
            AgentMCPBinding(
                id=generate_ulid(),
                agent_id=agent_id,
                mcp_server_id=mcp_server_id,
                allowed_tools=["publish_tweet"],
                status="active",
            )
        )
        db.add(
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                status="running",
                execution_mode="live",
                approval_required=False,
                plan_dag={"steps": []},
            )
        )
        pending = ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            service_key="content_ops",
            step_key="search_competitors",
            kind="action",
            provider=provider_key,
            action_key="search_tweets",
            params={"query": "launch"},
            depends_on=[],
            step_status="pending",
            attempt_count=0,
            max_attempts=3,
        )
        db.add(pending)
        await db.flush()

        leases = await Dispatcher().checkout_steps_for_worker(db, worker, max_n=1)

        assert leases == []
        assert pending.step_status == "failed"
        assert pending.resolved_subscription_id == subscription_id
        assert pending.resolved_agent_id == agent_id
        assert pending.error["type"] == "ActionBindingDenied"
        assert pending.error["action_key"] == "search_tweets"


@pytest.mark.asyncio
async def test_dispatcher_coerces_worker_output_before_validation(
    client, tmp_path, monkeypatch,
) -> None:
    import packages.core.database as dbmod
    from types import SimpleNamespace

    from packages.core.services import artifact_knowledge

    entity_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
    artifact = tmp_path / "workspace" / "social" / "draft-pack.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("Draft pack", encoding="utf-8")
    monkeypatch.setattr(
        artifact_knowledge,
        "get_entity_root",
        lambda _entity_id: str(tmp_path),
    )

    async def sync_artifact(**_kwargs):
        return SimpleNamespace(synced=True, document_id="doc_draft_pack", reason=None)

    monkeypatch.setattr(artifact_knowledge, "sync_file_to_knowledge", sync_artifact)
    schema = {
        "type": "object",
        "required": ["files", "summary", "draft_count"],
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "path"],
                    "properties": {
                        "name": {"type": "string"},
                        "path": {"type": "string"},
                    },
                },
            },
            "summary": {"type": "string"},
            "draft_count": {"type": "integer"},
        },
    }

    async with dbmod.async_session() as db:
        db.add(
            Worker(
                id=worker_id,
                entity_id=entity_id,
                kind="internal",
                display_name="Internal worker",
                capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
                monthly_spent_usd=Decimal("0"),
                auto_pause_on_budget=True,
                status="active",
            )
        )
        db.add(
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                status="running",
                execution_mode="live",
                approval_required=False,
                plan_dag={"steps": []},
            )
        )
        db.add(
            ExecutionStep(
                id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="draft_pack",
                kind="subagent",
                params={"prompt": "Prepare draft pack"},
                depends_on=[],
                step_status="running",
                attempt_count=1,
                max_attempts=3,
                expected_output_schema=schema,
                current_lease_id=lease_id,
            )
        )
        db.add(
            WorkLease(
                id=lease_id,
                step_id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                worker_id=worker_id,
                lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
                status="active",
            )
        )
        await db.flush()

        await Dispatcher().complete_lease(
            db,
            lease_id,
            result={
                "text": (
                    "Saved final pack to `workspace/social/draft-pack.md`.\n"
                    "Draft 1: X launch\nDraft 2: XHS note\nDraft 3: Reply"
                )
            },
        )
        await db.flush()

        step = await db.get(ExecutionStep, step_id)
        lease = await db.get(WorkLease, lease_id)
        assert step.step_status == "done"
        assert step.result["draft_count"] == 3
        assert step.result["files"][0]["path"] == "workspace/social/draft-pack.md"
        assert step.result["document_id"] == "doc_draft_pack"
        assert lease.status == "completed"
        assert lease.result == step.result


@pytest.mark.asyncio
async def test_internal_worker_heartbeats_during_long_lease(client, monkeypatch) -> None:
    import packages.core.database as dbmod
    from packages.core.workers import internal
    from packages.core.services.auth_service import hash_password

    entity_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
    task_id = generate_ulid()
    creator_id = generate_ulid()
    conversation_id = generate_ulid()
    snapshots: list[dict] = []

    async def slow_handler(_snapshot: dict) -> dict:
        snapshots.append(dict(_snapshot))
        async with asyncio.timeout(2):
            while True:
                async with dbmod.async_session() as db:
                    lease = await db.get(WorkLease, lease_id)
                    if lease is not None and lease.heartbeat_count > 0:
                        break
                await asyncio.sleep(0.01)
        return {"result": {"text": "done"}, "cost": {"usd": 0}}

    monkeypatch.setattr(internal, "LEASE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(internal, "LEASE_HEARTBEAT_EXTEND_SECONDS", 60)
    monkeypatch.setattr(internal, "_execute_by_kind", slow_handler)

    async with dbmod.async_session() as db:
        db.add_all(
            [
                User(
                    id=creator_id,
                    entity_id=entity_id,
                    email="heartbeat-task-creator@test.com",
                    display_name="Heartbeat Task Creator",
                    password_hash=hash_password("pass123"),
                    role="member",
                    status="active",
                ),
                Worker(
                    id=worker_id,
                    entity_id=entity_id,
                    kind="internal",
                    display_name="Internal worker",
                    capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
                    monthly_spent_usd=Decimal("0"),
                    auto_pause_on_budget=True,
                    status="active",
                ),
            ]
        )
        db.add(
            Task(
                id=task_id,
                entity_id=entity_id,
                title="Creator-backed task",
                status="in_progress",
                creator_id=creator_id,
                conversation_id=conversation_id,
            )
        )
        db.add(
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                task_id=task_id,
                status="running",
                execution_mode="live",
                approval_required=False,
                plan_dag={"steps": []},
            )
        )
        db.add(
            ExecutionStep(
                id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                step_key="generate_ux_spec_doc",
                kind="subagent",
                params={"prompt": "Generate UX spec"},
                depends_on=[],
                step_status="running",
                attempt_count=1,
                max_attempts=3,
                current_lease_id=lease_id,
            )
        )
        db.add(
            WorkLease(
                id=lease_id,
                step_id=step_id,
                plan_id=plan_id,
                entity_id=entity_id,
                worker_id=worker_id,
                lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
                status="active",
            )
        )
        await db.commit()

    outcome = await internal.execute_lease_inproc(lease_id)

    async with dbmod.async_session() as db:
        lease = await db.get(WorkLease, lease_id)
        step = await db.get(ExecutionStep, step_id)

    assert outcome == {"lease_id": lease_id, "outcome": "completed"}
    assert snapshots and snapshots[0]["task_id"] == task_id
    assert snapshots[0]["user_id"] == creator_id
    assert snapshots[0]["conversation_id"] == conversation_id
    assert lease.status == "completed"
    assert lease.heartbeat_count > 0
    assert step.step_status == "done"
