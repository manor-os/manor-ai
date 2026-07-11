from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from packages.core.dispatcher.service import Dispatcher
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Task
from packages.core.models.worker import SubscriptionWorker, WorkLease, Worker
from packages.core.models.workspace import Agent, AgentSubscription


@pytest.mark.asyncio
async def test_dispatcher_requires_connected_chrome_extension_before_leasing(db_session) -> None:
    entity_id = generate_ulid()
    plan_id = generate_ulid()
    worker_display_name = "Local worker"
    worker = Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind="custom_http",
        display_name=worker_display_name,
        capabilities={
            "supported_kinds": ["action"],
            "supported_providers": ["chrome"],
            "max_risk_level": "high",
            "browser": {"browser_use_available": True},
        },
        monthly_spent_usd=Decimal("0"),
        auto_pause_on_budget=True,
        status="active",
    )
    step = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan_id,
        entity_id=entity_id,
        step_key="chrome_observe",
        kind="action",
        provider="chrome",
        action_key="observe",
        params={"command": "observe"},
        depends_on=[],
        step_status="pending",
        risk_level="low",
        attempt_count=0,
        max_attempts=1,
    )
    db_session.add_all(
        [
            worker,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                status="running",
                execution_mode="execute",
                plan_dag={"source": "local_worker"},
                dispatcher_state={"source": "local_worker"},
            ),
            step,
        ]
    )
    await db_session.flush()

    assert await Dispatcher().checkout_steps_for_worker(db_session, worker, max_n=1) == []
    assert step.step_status == "pending"

    worker.capabilities = {
        **worker.capabilities,
        "browser": {
            "native_host_connected": True,
            "extension_connected": True,
        },
    }
    leases = await Dispatcher().checkout_steps_for_worker(db_session, worker, max_n=1)

    assert len(leases) == 1
    _, leased_step = leases[0]
    assert leased_step.id == step.id
    assert leased_step.step_status == "running"


@pytest.mark.asyncio
async def test_dispatcher_accepts_browser_mcp_for_connected_legacy_chrome_worker(db_session) -> None:
    entity_id = generate_ulid()
    plan_id = generate_ulid()
    worker_display_name = "Local worker"
    worker = Worker(
        id=generate_ulid(),
        entity_id=entity_id,
        kind="custom_http",
        display_name=worker_display_name,
        capabilities={
            "supported_kinds": ["action"],
            "supported_providers": ["chrome"],
            "max_risk_level": "high",
            "browser": {
                "native_host_connected": True,
                "extension_connected": True,
            },
        },
        monthly_spent_usd=Decimal("0"),
        auto_pause_on_budget=True,
        status="active",
    )
    step = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan_id,
        entity_id=entity_id,
        step_key="browser_mcp_status",
        kind="action",
        provider="browser_mcp",
        action_key="tools/call",
        params={
            "method": "tools/call",
            "params": {"name": "mcp__chrome__status", "arguments": {}},
        },
        depends_on=[],
        step_status="pending",
        risk_level="medium",
        attempt_count=0,
        max_attempts=1,
    )
    db_session.add_all(
        [
            worker,
            ExecutionPlan(
                id=plan_id,
                entity_id=entity_id,
                status="running",
                execution_mode="execute",
                plan_dag={"source": "local_worker"},
                dispatcher_state={"source": "local_worker"},
            ),
            step,
        ]
    )
    await db_session.flush()

    leases = await Dispatcher().checkout_steps_for_worker(db_session, worker, max_n=1)

    assert len(leases) == 1
    _, leased_step = leases[0]
    assert leased_step.id == step.id
    assert leased_step.step_status == "running"


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
async def test_dispatcher_coerces_worker_output_before_validation(client) -> None:
    import packages.core.database as dbmod

    entity_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    lease_id = generate_ulid()
    worker_id = generate_ulid()
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
        assert lease.status == "completed"
        assert lease.result == step.result


@pytest.mark.asyncio
async def test_internal_worker_heartbeats_during_long_lease(client, monkeypatch) -> None:
    import packages.core.database as dbmod
    from packages.core.workers import internal

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
        await asyncio.sleep(0.05)
        return {"result": {"text": "done"}, "cost": {"usd": 0}}

    monkeypatch.setattr(internal, "LEASE_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(internal, "LEASE_HEARTBEAT_EXTEND_SECONDS", 60)
    monkeypatch.setattr(internal, "_execute_by_kind", slow_handler)

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
