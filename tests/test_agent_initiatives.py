"""End-to-end boundaries for owner-scoped proactive Agent Initiatives."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from packages.core.contracts.agent_initiative import (
    InitiativeDecision,
    QuietHours,
    TriggerSpec,
)
from packages.core.models.base import generate_ulid
from packages.core.models.agent_initiative import (
    AgentInitiative,
    AgentInitiativeRun,
    AgentInitiativeState,
)
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.task import Conversation
from packages.core.models.workspace import AgentSubscription


pytestmark = pytest.mark.oss_regression


async def _auth(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _seed_legacy_workspace_initiative(
    db_session,
    *,
    subscription_id: str,
    entity_id: str,
    workspace_id: str,
    owner_user_id: str,
    conversation_id: str,
    name: str,
) -> AgentInitiative:
    """Reproduce a pre-boundary Workspace Initiative without the create API."""
    from packages.core.services.agent_initiative_service import _sync_driver

    subscription = await db_session.get(AgentSubscription, subscription_id)
    assert subscription is not None
    initiative = AgentInitiative(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_subscription_id=subscription_id,
        owner_user_id=owner_user_id,
        name=name,
        instruction="Check the Workspace status.",
        trigger_kind="interval",
        trigger_source="schedule",
        trigger_config={"kind": "interval", "every_seconds": 3600, "timezone": "UTC"},
        condition_config={},
        decision_policy={},
        delivery_policy={
            "surface": {"kind": "workspace_chat", "conversation_id": conversation_id},
        },
        interruption_policy={},
        status="active",
        revision=1,
    )
    db_session.add(initiative)
    db_session.add(AgentInitiativeState(initiative_id=initiative.id, version=1))
    await db_session.flush()
    await _sync_driver(db_session, initiative, subscription)
    await db_session.commit()
    return initiative


def test_agent_initiative_contracts_are_strict_and_structured() -> None:
    with pytest.raises(ValidationError):
        TriggerSpec.model_validate({
            "kind": "interval",
            "every_seconds": 30,
            "timezone": "UTC",
        })
    with pytest.raises(ValidationError):
        QuietHours.model_validate({
            "mode": "custom",
            "start": "22:00",
            "end": "07:00",
        })
    with pytest.raises(ValidationError):
        InitiativeDecision.model_validate({
            "decision": "silent",
            "message": "This must never be delivered",
            "reason_code": "no_change",
        })

    from packages.core.schemas.agent_initiative import AgentInitiativePatch

    with pytest.raises(ValidationError):
        AgentInitiativePatch.model_validate({"name": None})
    with pytest.raises(ValidationError):
        InitiativeDecision.model_validate({
            "decision": "notify",
            "reason_code": "new_item",
            "unexpected_recipient": "other-user",
        })


@pytest.mark.asyncio
async def test_personal_manor_chat_reuses_its_bound_subscription(
    client: AsyncClient,
    db_session,
) -> None:
    from packages.core.constants.agents import MANOR_AGENT_ID
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services.agent_subscription_service import (
        ensure_personal_subscription,
    )
    from packages.core.services.runtime_chat_context import (
        resolve_runtime_chat_context,
    )

    headers = await _auth(client, "initiative_manor_owner")
    me_response = await client.get("/api/v1/auth/me", headers=headers)
    assert me_response.status_code == 200, me_response.text
    me = me_response.json()

    conversation = Conversation(
        entity_id=me["entity_id"],
        user_id=me["id"],
        agent_id=None,
        title="Manor AI",
        channel="web",
        scope="channel",
        status="active",
    )
    db_session.add(conversation)
    subscription = await ensure_personal_subscription(
        db_session,
        entity_id=me["entity_id"],
        agent_id=MANOR_AGENT_ID,
        owner_user_id=me["id"],
        name="Personal Manor AI",
    )
    subscription.custom_prompt = "Keep this owner's proactive preferences private."
    conversation.agent_subscription_id = subscription.id
    await db_session.commit()

    _prompt, _tools, _history, runtime_context = await resolve_runtime_chat_context(
        db_session,
        "Continue our conversation.",
        entity_id=me["entity_id"],
        user_id=me["id"],
        conversation_id=conversation.id,
        disable_tools=True,
    )

    assert runtime_context.agent_id is None
    assert runtime_context.runtime_envelope.surface is ChatSurface.GLOBAL_OWNER_CHAT
    assert runtime_context.runtime_envelope.profile is RuntimeProfile.OWNER_COPILOT
    assert (
        runtime_context.runtime_envelope.metadata["agent_subscription_id"]
        == subscription.id
    )
    assert "Keep this owner's proactive preferences private." in (
        runtime_context.extra_context or ""
    )

    # Scheduled/manual Initiative admission must accept the same exact binding
    # without changing a Manor main Chat into a custom Agent DM.
    from types import SimpleNamespace

    from packages.core.services.agent_initiative_decision import (
        _exact_runtime_authority,
    )

    resolved_subscription, resolved_owner, resolved_conversation = (
        await _exact_runtime_authority(
            db_session,
            initiative=SimpleNamespace(
                agent_subscription_id=subscription.id,
                owner_user_id=me["id"],
                entity_id=me["entity_id"],
                workspace_id=None,
            ),
            subscription_id=subscription.id,
            owner_user_id=me["id"],
            agent_id=MANOR_AGENT_ID,
            conversation_id=conversation.id,
        )
    )
    assert resolved_subscription.id == subscription.id
    assert resolved_owner.id == me["id"]
    assert resolved_conversation.id == conversation.id
    assert resolved_conversation.agent_id is None


@pytest.mark.asyncio
async def test_personal_agent_initiative_crud_and_driver_boundary(
    client: AsyncClient,
    db_session,
    monkeypatch,
) -> None:
    headers = await _auth(client, "initiative_owner")
    me_response = await client.get("/api/v1/auth/me", headers=headers)
    assert me_response.status_code == 200, me_response.text
    me = me_response.json()

    agent_response = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "Personal News Scout",
            "system_prompt": "Track relevant news and explain material changes.",
        },
    )
    assert agent_response.status_code == 201, agent_response.text
    agent_id = agent_response.json()["id"]

    subscription_response = await client.post(
        "/api/v1/agents/subscriptions",
        headers=headers,
        json={"agent_id": agent_id},
    )
    assert subscription_response.status_code == 201, subscription_response.text
    subscription = subscription_response.json()
    assert subscription["owner_user_id"] == me["id"]
    assert subscription["scope_kind"] == "personal"
    assert subscription["can_manage"] is True

    conversation = Conversation(
        entity_id=me["entity_id"],
        user_id=me["id"],
        agent_id=agent_id,
        title="News scout",
        channel="web",
        scope="channel",
        status="active",
    )
    db_session.add(conversation)
    await db_session.commit()
    conversation_id = conversation.id

    created = await client.post(
        f"/api/v1/agent-subscriptions/{subscription['id']}/initiatives",
        headers=headers,
        json={
            "name": "Daily important AI news",
            "instruction": "Search for important AI product and policy news.",
            "trigger": {
                "kind": "interval",
                "every_seconds": 3600,
                "timezone": "UTC",
            },
            "condition": {
                "semantic_condition": "Only notify for a material new development."
            },
            "decision_policy": {
                "mode": "on_condition",
                "cooldown_seconds": 7200,
                "max_notifications_per_day": 4,
            },
            "delivery_policy": {
                "surface": {
                    "kind": "conversation",
                    "conversation_id": conversation_id,
                },
                "notification": {
                    "enabled": True,
                    "recipient": "owner",
                    "channels": ["inapp"],
                    "content_mode": "summary_and_link",
                },
            },
            "interruption_policy": {
                "quiet_hours": {
                    "mode": "custom",
                    "start": "22:00",
                    "end": "07:00",
                    "timezone": "UTC",
                },
                "during_quiet_hours": "suppress_external",
            },
        },
    )
    assert created.status_code == 201, created.text
    initiative = created.json()
    assert initiative["status"] == "active"
    assert initiative["revision"] == 1
    assert initiative["agent_subscription_id"] == subscription["id"]
    assert initiative["delivery_policy"]["surface"]["conversation_id"] == conversation_id

    conversation_initiatives = await client.get(
        f"/api/v1/agent-initiatives/conversation/{conversation_id}",
        headers=headers,
    )
    assert conversation_initiatives.status_code == 200, conversation_initiatives.text
    conversation_payload = conversation_initiatives.json()
    assert conversation_payload["conversation"] == {
        "id": conversation_id,
        "title": "News scout",
        "workspace_id": None,
        "scope": "channel",
    }
    assert [item["id"] for item in conversation_payload["items"]] == [
        initiative["id"]
    ]

    conversations = await client.get("/api/v1/chat/conversations", headers=headers)
    assert conversations.status_code == 200, conversations.text
    destination = next(item for item in conversations.json() if item["id"] == conversation_id)
    assert destination["scope"] == "channel"
    assert destination["agent_subscription_id"] == subscription["id"]

    # Follow-up turns in the same personal Agent DM must resolve the exact
    # deployment, even when the caller does not repeat an agent id.
    subscription_row = await db_session.get(AgentSubscription, subscription["id"])
    assert subscription_row is not None
    subscription_row.custom_prompt = "Use the owner's private AI-news watchlist."
    await db_session.commit()
    from packages.core.services.runtime_chat_context import (
        resolve_runtime_chat_context,
    )
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface

    _prompt, _tools, _history, runtime_context = await resolve_runtime_chat_context(
        db_session,
        "What did you find?",
        entity_id=me["entity_id"],
        user_id=me["id"],
        conversation_id=conversation_id,
        disable_tools=True,
    )
    assert runtime_context.agent_id == agent_id
    assert runtime_context.runtime_envelope.surface is ChatSurface.AGENT_DM
    assert runtime_context.runtime_envelope.profile is RuntimeProfile.AGENT_DELEGATE
    assert (
        runtime_context.runtime_envelope.metadata["agent_subscription_id"]
        == subscription["id"]
    )
    assert "Use the owner's private AI-news watchlist." in (
        runtime_context.extra_context or ""
    )

    db_session.expire_all()
    row = await db_session.get(AgentInitiative, initiative["id"])
    assert row is not None
    driver = (
        await db_session.execute(
            select(ScheduledJob).where(
                ScheduledJob.agent_initiative_id == initiative["id"]
            )
        )
    ).scalar_one()
    assert driver.execution_type == "agent_initiative"
    assert driver.execution_target["managed_by"] == "agent_initiative"
    assert driver.execution_target["subscription_id"] == subscription["id"]
    assert driver.conversation_id == conversation_id
    assert driver.payload_message is None
    driver_id = driver.id

    # The ScheduledJob is a private clock driver, not a second product object.
    generic_list = await client.get("/api/v1/jobs", headers=headers)
    assert generic_list.status_code == 200, generic_list.text
    assert driver_id not in {item["id"] for item in generic_list.json()["items"]}
    generic_detail = await client.get(f"/api/v1/jobs/{driver_id}", headers=headers)
    assert generic_detail.status_code == 404

    updated = await client.patch(
        f"/api/v1/agent-initiatives/{initiative['id']}",
        headers=headers,
        json={
            "expected_revision": 1,
            "patch": {
                "name": "Hourly material AI news",
                "trigger": {
                    "kind": "cron",
                    "cron": "0 * * * *",
                    "timezone": "UTC",
                },
            },
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    assert updated.json()["trigger"]["kind"] == "cron"

    stale = await client.patch(
        f"/api/v1/agent-initiatives/{initiative['id']}",
        headers=headers,
        json={
            "expected_revision": 1,
            "patch": {"name": "Stale overwrite"},
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == (
        "agent_initiative.error.initiative_revision_conflict"
    )

    muted = await client.post(
        f"/api/v1/agent-initiatives/{initiative['id']}/mute",
        headers=headers,
        json={"expected_revision": 2},
    )
    assert muted.status_code == 200, muted.text
    assert muted.json()["status"] == "muted"

    paused = await client.post(
        f"/api/v1/agent-initiatives/{initiative['id']}/pause",
        headers=headers,
        json={"expected_revision": 3},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"
    db_session.expire_all()
    driver = await db_session.get(ScheduledJob, driver_id)
    assert driver is not None and driver.enabled is False
    assert driver.next_run_at is None

    from packages.core.tasks import ai_tasks, scheduler_tasks

    published_dispatches: list[dict] = []
    monkeypatch.setattr(
        scheduler_tasks,
        "_publish_scheduled_dispatch",
        lambda dispatch, **_kwargs: published_dispatches.append(dispatch),
    )
    unconfirmed_run = await client.post(
        f"/api/v1/agent-initiatives/{initiative['id']}/run",
        headers=headers,
        json={"idempotency_key": "paused-check"},
    )
    assert unconfirmed_run.status_code == 409
    confirmed_run = await client.post(
        f"/api/v1/agent-initiatives/{initiative['id']}/run",
        headers=headers,
        json={"idempotency_key": "paused-check", "confirm_cost": True},
    )
    assert confirmed_run.status_code == 202, confirmed_run.text
    scheduled_run_id = confirmed_run.json()["scheduled_run_id"]
    assert scheduled_run_id
    assert confirmed_run.json()["idempotency_key"] == "paused-check"

    # Consume the exact durable parent handoff that the run-now API published.
    # This creates the real scheduled child Task without letting a test worker
    # race the assertions below.
    assert len(published_dispatches) == 1
    parent_dispatch = published_dispatches.pop()
    assert parent_dispatch["kind"] == "scheduler_parent"
    await scheduler_tasks._async_dispatch_single(
        driver_id,
        parent_dispatch["args"][1],
        manual=True,
        occurrence_key="manual:paused-check",
        allow_disabled_agent_initiative=True,
    )
    assert len(published_dispatches) == 1
    child_dispatch = published_dispatches.pop()
    assert child_dispatch["kind"] == "agent_task"
    task_id = child_dispatch["args"][0]
    from packages.core.models.scheduler import ScheduledJobRun
    from packages.core.services.scheduled_run_lifecycle import (
        MANUAL_AGENT_INITIATIVE_ADMISSION_KEY,
        MANUAL_AGENT_INITIATIVE_ADMISSION_VERSION,
    )

    db_session.expire_all()
    manual_run = await db_session.get(ScheduledJobRun, scheduled_run_id)
    assert manual_run is not None
    assert manual_run.result[MANUAL_AGENT_INITIATIVE_ADMISSION_KEY] == {
        "version": MANUAL_AGENT_INITIATIVE_ADMISSION_VERSION,
        "initiative_id": initiative["id"],
    }
    assert manual_run.result["explicit_trigger"]["detail"] == (
        "owner_confirmed_paused"
    )

    runner_calls: list[str] = []
    completed_result = {
        "task_id": task_id,
        "status": "completed",
        "initiative_decision": {
            "version": "1",
            "decision": "silent",
            "reason_code": "manual_check_complete",
        },
    }

    async def run_claimed_task(
        _session_factory,
        claimed_task_id,
        _agent_id,
        *,
        runtime_hitl_delivery_identity=None,
    ):
        assert runtime_hitl_delivery_identity is None
        runner_calls.append(claimed_task_id)
        return True, completed_result

    monkeypatch.setattr(
        ai_tasks,
        "_run_agent_task_with_execution_claim",
        run_claimed_task,
    )
    from packages.core.database import create_worker_session

    claimed, result = await ai_tasks._run_scheduled_agent_task_if_open(
        create_worker_session(),
        task_id,
        agent_id,
        scheduled_run_id=scheduled_run_id,
        scheduled_job_id=f"initiative:{initiative['id']}",
    )
    assert claimed is True
    assert result == completed_result
    assert runner_calls == [task_id]
    await ai_tasks._update_job_run_status_async(
        create_worker_session(),
        task_id,
        completed_result,
        scheduled_run_id=scheduled_run_id,
        scheduled_job_id=f"initiative:{initiative['id']}",
    )

    db_session.expire_all()
    driver = await db_session.get(ScheduledJob, driver_id)
    row = await db_session.get(AgentInitiative, initiative["id"])
    manual_run = await db_session.get(ScheduledJobRun, scheduled_run_id)
    manual_initiative_run = (
        await db_session.execute(
            select(AgentInitiativeRun).where(
                AgentInitiativeRun.scheduled_run_id == scheduled_run_id
            )
        )
    ).scalar_one()
    assert row is not None and row.status == "paused"
    assert driver is not None and driver.enabled is False
    assert driver.last_run_at is None
    assert driver.next_run_at is None
    assert manual_run is not None and manual_run.status == "success"
    assert manual_initiative_run.status == "succeeded"
    assert manual_initiative_run.final_decision == "silent"
    assert manual_initiative_run.completed_at is not None

    # A timer occurrence has no owner-confirmed manual-run marker. Even if a
    # stale broker delivery already contains a pristine child, the same paused
    # driver must suppress it before TaskRunner and cancel its exact lineage.
    from packages.core.services.task_service import create_task

    timer_occurrence_key = f"scheduled:v2:{generate_ulid()}"
    timer_run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=driver.job_id,
        idempotency_key=timer_occurrence_key,
        status="running",
        trigger_type="cron",
        started_at=datetime.now(timezone.utc),
    )
    timer_task = await create_task(
        db_session,
        me["entity_id"],
        title="[Auto] Suppressed paused Initiative timer",
        task_type="ai_generated",
        details={
            "scheduled_job_id": driver.job_id,
            "scheduled_run_id": timer_run.id,
        },
    )
    timer_run.result = {
        "dispatch": {
            "kind": "agent_task",
            "args": [timer_task.id, agent_id],
            "kwargs": {
                "scheduled_run_id": timer_run.id,
                "scheduled_job_id": driver.job_id,
            },
        },
        "dispatch_status": "published",
        "dispatch_ledger": {"revision": driver.revision},
    }
    timer_run_id = timer_run.id
    timer_task_id = timer_task.id
    timer_initiative_run = AgentInitiativeRun(
        id=generate_ulid(),
        initiative_id=initiative["id"],
        scheduled_run_id=timer_run_id,
        task_id=timer_task_id,
        occurrence_key=timer_occurrence_key,
        trigger_type="cron",
        status="admitted",
    )
    timer_initiative_run_id = timer_initiative_run.id
    db_session.add_all([timer_run, timer_initiative_run])
    await db_session.commit()

    timer_claimed, timer_result = await ai_tasks._run_scheduled_agent_task_if_open(
        create_worker_session(),
        timer_task_id,
        agent_id,
        scheduled_run_id=timer_run_id,
        scheduled_job_id=driver.job_id,
    )
    assert timer_claimed is False
    assert timer_result["reason"] == "scheduled_job_disabled"
    assert runner_calls == [task_id]
    db_session.expire_all()
    assert (
        await db_session.get(ScheduledJobRun, timer_run_id)
    ).status == "cancelled"
    assert (
        await db_session.get(type(timer_task), timer_task_id)
    ).status == "cancelled"
    timer_initiative_run = await db_session.get(
        AgentInitiativeRun,
        timer_initiative_run_id,
    )
    assert timer_initiative_run.status == "failed"
    assert timer_initiative_run.reason_code == "initiative_dispatch_failed"
    assert timer_initiative_run.error == "scheduled_job_disabled"
    assert timer_initiative_run.completed_at is not None

    deleted = await client.request(
        "DELETE",
        f"/api/v1/agent-initiatives/{initiative['id']}",
        headers=headers,
        json={"expected_revision": 4},
    )
    assert deleted.status_code == 204, deleted.text
    missing = await client.get(
        f"/api/v1/agent-initiatives/{initiative['id']}", headers=headers
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_personal_initiative_rejects_a_different_delivery_agent(
    client: AsyncClient,
    db_session,
) -> None:
    headers = await _auth(client, "initiative_exact_destination")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    first = await client.post(
        "/api/v1/agents", headers=headers, json={"name": "First Agent"}
    )
    second = await client.post(
        "/api/v1/agents", headers=headers, json={"name": "Second Agent"}
    )
    subscription = await client.post(
        "/api/v1/agents/subscriptions",
        headers=headers,
        json={"agent_id": first.json()["id"]},
    )
    conversation = Conversation(
        entity_id=me["entity_id"],
        user_id=me["id"],
        agent_id=second.json()["id"],
        channel="web",
        scope="channel",
        status="active",
    )
    db_session.add(conversation)
    await db_session.commit()

    response = await client.post(
        f"/api/v1/agent-subscriptions/{subscription.json()['id']}/initiatives",
        headers=headers,
        json={
            "name": "Wrong destination",
            "instruction": "Check something.",
            "trigger": {
                "kind": "interval",
                "every_seconds": 3600,
                "timezone": "UTC",
            },
            "delivery_policy": {
                "surface": {
                    "kind": "conversation",
                    "conversation_id": conversation.id,
                }
            },
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "agent_initiative.error.initiative_conversation_invalid"
    )


@pytest.mark.asyncio
async def test_workspace_chat_rejects_creation_but_lists_exact_legacy_initiative(
    client: AsyncClient,
    db_session,
    monkeypatch,
) -> None:
    headers = await _auth(client, "initiative_workspace_chat_list")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Initiative Workspace"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]
    agent_response = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Workspace Scout", "workspace_id": workspace_id},
    )
    assert agent_response.status_code == 201, agent_response.text
    subscription_response = await client.post(
        "/api/v1/agents/subscriptions",
        headers=headers,
        json={
            "agent_id": agent_response.json()["id"],
            "workspace_id": workspace_id,
        },
    )
    assert subscription_response.status_code == 201, subscription_response.text

    target = Conversation(
        entity_id=me["entity_id"],
        workspace_id=workspace_id,
        title="Workspace main",
        channel="workspace",
        scope="workspace_main",
        status="active",
    )
    other = Conversation(
        entity_id=me["entity_id"],
        workspace_id=workspace_id,
        title="Other thread",
        channel="workspace",
        scope="workspace_thread",
        status="active",
    )
    db_session.add_all([target, other])
    await db_session.commit()

    created = await client.post(
        f"/api/v1/agent-subscriptions/{subscription_response.json()['id']}/initiatives",
        headers=headers,
        json={
            "name": "Workspace updates",
            "instruction": "Check the Workspace status.",
            "trigger": {
                "kind": "interval",
                "every_seconds": 3600,
                "timezone": "UTC",
            },
            "delivery_policy": {
                "surface": {
                    "kind": "workspace_chat",
                    "conversation_id": target.id,
                }
            },
        },
    )
    assert created.status_code == 409, created.text
    assert created.json()["detail"]["code"] == (
        "agent_initiative.error.initiative_private_chat_only"
    )
    assert list((await db_session.execute(select(AgentInitiative).where(
        AgentInitiative.agent_subscription_id == subscription_response.json()["id"],
    ))).scalars().all()) == []
    assert list((await db_session.execute(select(ScheduledJob).where(
        ScheduledJob.workspace_id == workspace_id,
        ScheduledJob.agent_initiative_id.isnot(None),
    ))).scalars().all()) == []

    legacy = await _seed_legacy_workspace_initiative(
        db_session,
        subscription_id=subscription_response.json()["id"],
        entity_id=me["entity_id"],
        workspace_id=workspace_id,
        owner_user_id=me["id"],
        conversation_id=target.id,
        name="Legacy Workspace updates",
    )

    exact = await client.get(
        f"/api/v1/agent-initiatives/conversation/{target.id}",
        headers=headers,
    )
    assert exact.status_code == 200, exact.text
    assert exact.json()["conversation"]["scope"] == "workspace_main"
    assert [item["id"] for item in exact.json()["items"]] == [legacy.id]

    empty = await client.get(
        f"/api/v1/agent-initiatives/conversation/{other.id}",
        headers=headers,
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["items"] == []

    # Pausing autonomy disables the hidden clock, not this active Initiative's
    # explicit Run-now action. No separate Initiative-pause confirmation applies.
    from packages.core.tasks import scheduler_tasks
    from packages.core.models.scheduler import ScheduledJobRun

    published = []
    monkeypatch.setattr(scheduler_tasks, "_publish_scheduled_dispatch", published.append)
    paused = await client.post(f"/api/v1/workspaces/{workspace_id}/pause", headers=headers)
    assert paused.status_code == 200, paused.text
    driver = (await db_session.execute(select(ScheduledJob).where(
        ScheduledJob.agent_initiative_id == legacy.id,
    ))).scalar_one()
    clock = driver.last_run_at
    run_now = await client.post(
        f"/api/v1/agent-initiatives/{legacy.id}/run", headers=headers,
        json={"idempotency_key": "workspace-paused-manual"},
    )
    assert run_now.status_code == 202, run_now.text
    assert len(published) == 1
    receipt = await db_session.get(ScheduledJobRun, run_now.json()["scheduled_run_id"])
    assert receipt.result["manual_request"]["user_id"] == me["id"]
    await db_session.refresh(driver)
    assert driver.enabled is False
    assert driver.last_run_at == clock


@pytest.mark.asyncio
async def test_workspace_delete_requires_explicit_initiative_resume(
    client: AsyncClient,
    db_session,
) -> None:
    headers = await _auth(client, "initiative_workspace_restore")
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Restorable Initiative Workspace"},
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace_id = workspace_response.json()["id"]

    agent_response = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Restorable Scout", "workspace_id": workspace_id},
    )
    assert agent_response.status_code == 201, agent_response.text
    subscription_response = await client.post(
        "/api/v1/agents/subscriptions",
        headers=headers,
        json={
            "agent_id": agent_response.json()["id"],
            "workspace_id": workspace_id,
        },
    )
    assert subscription_response.status_code == 201, subscription_response.text

    conversation = Conversation(
        entity_id=me["entity_id"],
        workspace_id=workspace_id,
        title="Restorable Workspace main",
        channel="workspace",
        scope="workspace_main",
        status="active",
    )
    db_session.add(conversation)
    await db_session.commit()

    legacy = await _seed_legacy_workspace_initiative(
        db_session,
        subscription_id=subscription_response.json()["id"],
        entity_id=me["entity_id"],
        workspace_id=workspace_id,
        owner_user_id=me["id"],
        conversation_id=conversation.id,
        name="Restorable Workspace updates",
    )
    initiative_id = legacy.id
    original_driver = (await db_session.execute(
        select(ScheduledJob).where(
            ScheduledJob.agent_initiative_id == initiative_id,
        )
    )).scalar_one()
    original_driver_id = original_driver.id

    deleted = await client.delete(
        f"/api/v1/workspaces/{workspace_id}",
        headers=headers,
    )
    assert deleted.status_code == 204, deleted.text
    db_session.expire_all()
    after_delete = await db_session.get(AgentInitiative, initiative_id)
    assert after_delete is not None
    assert after_delete.status == "needs_setup"
    assert after_delete.revision == 2
    assert after_delete.updated_at is not None
    assert await db_session.get(ScheduledJob, original_driver_id) is None

    restored = await client.post(
        f"/api/v1/workspaces/{workspace_id}/restore",
        headers=headers,
    )
    assert restored.status_code == 200, restored.text
    db_session.expire_all()
    after_restore = await db_session.get(AgentInitiative, initiative_id)
    assert after_restore is not None
    assert after_restore.status == "needs_setup"
    assert after_restore.revision == 2
    assert list((await db_session.execute(
        select(ScheduledJob).where(
            ScheduledJob.agent_initiative_id == initiative_id,
        )
    )).scalars().all()) == []

    resumed = await client.post(
        f"/api/v1/agent-initiatives/{initiative_id}/resume",
        headers=headers,
        json={"expected_revision": 2},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "active"
    assert resumed.json()["revision"] == 3

    db_session.expire_all()
    resumed_drivers = list((await db_session.execute(
        select(ScheduledJob).where(
            ScheduledJob.agent_initiative_id == initiative_id,
        )
    )).scalars().all())
    assert len(resumed_drivers) == 1
    assert resumed_drivers[0].id != original_driver_id
    assert resumed_drivers[0].enabled is True
    assert resumed_drivers[0].next_run_at is not None
