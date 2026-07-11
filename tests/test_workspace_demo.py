"""Workspace demo/sandbox scenarios."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select


async def _register(client: AsyncClient, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_leasing_sandbox_demo_seeds_full_workspace_runtime(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.channel import ChannelConfig
    from packages.core.models.document import Channel, Document, DocumentGroup
    from packages.core.models.goal import Goal
    from packages.core.models.runtime_learning import AgentLearningCandidate, RuntimeEvidence
    from packages.core.models.task import Message, Task
    from packages.core.models.workspace import AgentSubscription, Workspace, WorkspaceActivity, WorkspaceWorkBatch

    headers = await _register(client, "leasing_demo")

    create = await client.post(
        "/api/v1/workspaces/sandbox",
        headers=headers,
        json={"kind": "leasing"},
    )

    assert create.status_code == 201
    ids = create.json()
    workspace_id = ids["workspace_id"]

    workspace = (await db_session.execute(select(Workspace).where(Workspace.id == workspace_id))).scalar_one()
    entity_id = workspace.entity_id
    assert workspace.kind == "leasing"
    assert workspace.settings["sandbox"] is True
    assert workspace.heartbeat_enabled is True
    assert workspace.monthly_budget_usd is not None
    assert workspace.operating_model["evaluation"]["loop"].startswith("After every active work batch")
    assert len(workspace.operating_model["goals"]) == 5

    subscriptions = (
        (await db_session.execute(select(AgentSubscription).where(AgentSubscription.workspace_id == workspace_id)))
        .scalars()
        .all()
    )
    service_keys = {sub.service_key for sub in subscriptions}
    assert {
        "lead_intake",
        "unit_recommendation",
        "tour_scheduling",
        "followup_drafting",
        "pipeline_tracking",
    } <= service_keys

    goals = (await db_session.execute(select(Goal).where(Goal.workspace_id == workspace_id))).scalars().all()
    assert len(goals) >= 5
    assert all(goal.current_value is not None for goal in goals)
    assert {"behind", "at_risk", "on_track"} <= {goal.pace_status for goal in goals}

    repair = await client.post(
        f"/api/v1/workspaces/{workspace_id}/operation/repair",
        headers=headers,
    )
    assert repair.status_code == 200
    assert repair.json()["goals"]["created"] == 0
    assert repair.json()["channels"]["created_configs"] == 0
    assert repair.json()["channels"]["created_bindings"] == 0
    assert repair.json()["channels"]["updated_configs"] == 0
    assert repair.json()["channels"]["updated_bindings"] == 0
    db_session.expire_all()
    repaired_goals = (await db_session.execute(select(Goal).where(Goal.workspace_id == workspace_id))).scalars().all()
    assert len(repaired_goals) == len(goals)

    groups = (
        (await db_session.execute(select(DocumentGroup).where(DocumentGroup.workspace_id == workspace_id)))
        .scalars()
        .all()
    )
    knowledge_groups = [group for group in groups if not (group.settings or {}).get("workspace_file_bucket")]
    workspace_file_buckets = [group for group in groups if (group.settings or {}).get("workspace_file_bucket")]
    group_names = {group.name for group in groups}
    assert {
        "Unit Inventory & Availability",
        "Property FAQ & Policies",
        "Leasing Playbook & Message Templates",
    } <= group_names
    assert all((group.settings or {}).get("kind") == "knowledge_net" for group in knowledge_groups)
    assert len(workspace_file_buckets) == 1

    artifact_docs = (
        (
            await db_session.execute(
                select(Document).where(
                    Document.entity_id == entity_id,
                    Document.metadata_["origin"]["workspace_id"].astext == workspace_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {"Daily Lead Audit Summary.md", "Maya Chen Follow-up Draft.md"} <= {doc.name for doc in artifact_docs}
    from packages.core.services.document_service import get_document_content

    audit_doc = next(doc for doc in artifact_docs if doc.name == "Daily Lead Audit Summary.md")
    audit_content = await get_document_content(db_session, audit_doc.id, entity_id)
    assert audit_content is not None
    assert "12 qualified leads" in audit_content
    assert "Unit 4C" in audit_content

    channel_configs = (
        (await db_session.execute(select(ChannelConfig).where(ChannelConfig.workspace_id == workspace_id)))
        .scalars()
        .all()
    )
    assert {cfg.channel_type for cfg in channel_configs} >= {"webchat", "internal_chat"}
    assert any((cfg.config or {}).get("public_token") for cfg in channel_configs)

    bindings = (await db_session.execute(select(Channel).where(Channel.workspace_id == workspace_id))).scalars().all()
    assert {binding.type for binding in bindings} >= {"webchat", "internal_chat"}
    assert all(binding.agent_subscription_id for binding in bindings)

    tasks = (await db_session.execute(select(Task).where(Task.workspace_id == workspace_id))).scalars().all()
    assert {"completed", "in_progress", "pending"} <= {task.status for task in tasks}
    assert any(task.owner_service_key == "followup_drafting" for task in tasks)
    assert any((task.actual_output or {}).get("files") for task in tasks if task.status in {"completed", "in_progress"})

    batch = (
        await db_session.execute(select(WorkspaceWorkBatch).where(WorkspaceWorkBatch.workspace_id == workspace_id))
    ).scalar_one()
    assert batch.status == "active"
    assert set(batch.task_ids) == {task.id for task in tasks}
    assert batch.details["trigger_strategist_when_all_complete"] is True
    assert {task.details.get("workspace_work_batch_id") for task in tasks} == {batch.id}

    evidence = (
        (await db_session.execute(select(RuntimeEvidence).where(RuntimeEvidence.workspace_id == workspace_id)))
        .scalars()
        .all()
    )
    assert {"strategist_review", "task_run", "chat_run"} <= {ev.evidence_type for ev in evidence}
    assert any(ev.status == "blocked" for ev in evidence)

    candidates = (
        (
            await db_session.execute(
                select(AgentLearningCandidate).where(AgentLearningCandidate.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    assert {"memory", "tool_experience"} <= {candidate.candidate_type for candidate in candidates}
    assert all(candidate.status == "proposed" for candidate in candidates)

    messages_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages",
        headers=headers,
    )
    assert messages_resp.status_code == 200
    messages = messages_resp.json()
    pending = [msg for msg in messages if (msg.get("pending_action") or {}).get("kind") == "external_message_approval"]
    assert pending

    resolve = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{pending[0]['id']}/resolve",
        headers=headers,
        json={"choice": "approve"},
    )
    assert resolve.status_code == 200
    assert resolve.json()["resolved_at"] is not None
    assert resolve.json()["resolution"]["choice"] == "approve"

    db_session.expire_all()
    resolved_msg = (await db_session.execute(select(Message).where(Message.id == pending[0]["id"]))).scalar_one()
    assert resolved_msg.resolved_at is not None
    activity = (
        await db_session.execute(
            select(WorkspaceActivity).where(
                WorkspaceActivity.workspace_id == workspace_id,
                WorkspaceActivity.event_type == "external_message.approved",
            )
        )
    ).scalar_one()
    assert activity.details["message_id"] == pending[0]["id"]
    assert activity.details["pending_action_kind"] == "external_message_approval"

    documents_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/documents",
        headers=headers,
    )
    assert documents_resp.status_code == 200
    doc_groups = documents_resp.json()
    assert any(group["document_count"] >= 2 for group in doc_groups)
    assert any(group["is_knowledge_net"] for group in doc_groups)
    # The Documents API hides the internal workspace file bucket, while still
    # exposing a read-only generated-artifacts pseudo group for task outputs.
    assert all(not group["settings"].get("workspace_file_bucket") for group in doc_groups)
    artifact_group = next(group for group in doc_groups if group["kind"] == "workspace_artifacts")
    assert artifact_group["is_workspace_file_bucket"] is True
    assert artifact_group["settings"]["generated_artifacts"] is True

    from packages.core.workspace_chat import context as chat_context

    artifact_search = await chat_context.workspace_search(
        db_session,
        workspace_id,
        entity_id,
        category="artifacts",
        query="Maya",
    )
    assert "## Workspace Artifacts" in artifact_search
    assert "Maya Chen Follow-up Draft.md" in artifact_search
    assert "document_id=" in artifact_search
    assert "task_id=" in artifact_search

    goal_search = await chat_context.workspace_search(
        db_session,
        workspace_id,
        entity_id,
        category="goals",
        query="tour",
    )
    assert "Lift lead-to-tour conversion to 40%" in goal_search
    assert "28 / 40" in goal_search

    learning_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/learning-candidates",
        headers=headers,
    )
    assert learning_resp.status_code == 200
    learning_candidates = learning_resp.json()
    assert len(learning_candidates) >= 2
    memory_candidate = next(candidate for candidate in learning_candidates if candidate["candidate_type"] == "memory")
    accepted = await client.post(
        f"/api/v1/workspaces/{workspace_id}/learning-candidates/{memory_candidate['id']}/resolve",
        headers=headers,
        json={"status": "accepted", "note": "Good demo rule."},
    )
    assert accepted.status_code == 200
    queued = await client.post(
        f"/api/v1/workspaces/{workspace_id}/learning-candidates/{memory_candidate['id']}/apply",
        headers=headers,
    )
    assert queued.status_code == 200
    assert queued.json()["status"] == "accepted"
    assert queued.json()["resolution"]["apply_status"] in {"queued", "failed"}

    all_learning = await client.get(
        f"/api/v1/workspaces/{workspace_id}/learning-candidates?status=",
        headers=headers,
    )
    assert all_learning.status_code == 200
    assert any(
        candidate["id"] == memory_candidate["id"] and candidate["status"] == "accepted"
        for candidate in all_learning.json()
    )


@pytest.mark.asyncio
async def test_social_sandbox_demo_seeds_visible_approval_card(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.channel import ChannelConfig, MessageLog
    from packages.core.models.document import Channel
    from packages.core.models.goal import GoalTaskLink
    from packages.core.models.runtime_learning import RuntimeEvidence
    from packages.core.models.task import Message
    from packages.core.models.workspace import WorkspaceActivity

    headers = await _register(client, "social_demo")

    create = await client.post(
        "/api/v1/workspaces/sandbox",
        headers=headers,
        json={"kind": "social_media"},
    )
    assert create.status_code == 201
    workspace_id = create.json()["workspace_id"]
    task_id = create.json()["task_id"]

    channel_config = (
        await db_session.execute(select(ChannelConfig).where(ChannelConfig.workspace_id == workspace_id))
    ).scalar_one()
    assert channel_config.channel_type == "twitter_x"
    channel_config_id = channel_config.id
    channel_binding = (
        await db_session.execute(select(Channel).where(Channel.workspace_id == workspace_id))
    ).scalar_one()
    assert channel_binding.agent_subscription_id == create.json()["subscription_id"]

    goal_link = (
        await db_session.execute(
            select(GoalTaskLink).where(
                GoalTaskLink.task_id == task_id,
            )
        )
    ).scalar_one()
    assert goal_link.contribution == "direct"

    messages_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/chat/messages",
        headers=headers,
    )
    assert messages_resp.status_code == 200
    pending = [
        msg
        for msg in messages_resp.json()
        if (msg.get("pending_action") or {}).get("action_key") == "social_post.publish"
    ]
    assert pending
    assert pending[0]["pending_action"]["channel_config_id"] == channel_config_id

    resolve = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{pending[0]['id']}/resolve",
        headers=headers,
        json={"choice": "approve"},
    )
    assert resolve.status_code == 200
    assert resolve.json()["resolved_at"] is not None

    db_session.expire_all()
    approved_message = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == pending[0]["conversation_id"],
                Message.meta["approved_external_message"].as_boolean().is_(True),
            )
        )
    ).scalar_one()
    assert "boring follow-through" in approved_message.content
    message_log = (
        await db_session.execute(
            select(MessageLog).where(
                MessageLog.channel_config_id == channel_config_id,
                MessageLog.direction == "outbound",
            )
        )
    ).scalar_one()
    assert message_log.status == "queued"

    decision_evidence = (
        await db_session.execute(
            select(RuntimeEvidence).where(
                RuntimeEvidence.workspace_id == workspace_id,
                RuntimeEvidence.evidence_type == "external_message_decision",
            )
        )
    ).scalar_one()
    assert decision_evidence.details["pending_action_kind"] == "external_message_approval"
    assert decision_evidence.details["channel_type"] == "twitter_x"
    approval_activity = (
        await db_session.execute(
            select(WorkspaceActivity).where(
                WorkspaceActivity.workspace_id == workspace_id,
                WorkspaceActivity.event_type == "external_message.approved",
            )
        )
    ).scalar_one()
    assert approval_activity.details["choice"] == "approve"


@pytest.mark.asyncio
async def test_solo_founder_content_engine_can_finish_a_work_wave(db_session, monkeypatch):
    """One-person-company demo: content goals should progress as a complete work wave."""
    from datetime import datetime, timezone
    from decimal import Decimal
    from types import SimpleNamespace

    from sqlalchemy import select

    from packages.core.governance import WorkspacePolicy, check_step_policy, update_policy
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import (
        Document,
        DocumentGroup,
        DocumentGroupMember,
        VectorStatus,
    )
    from packages.core.models.goal import Goal, GoalTaskLink
    from packages.core.models.runtime_learning import AgentLearningCandidate, RuntimeEvidence
    from packages.core.models.task import Task
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace, WorkspaceWorkBatch
    from packages.core.services.document_metadata import merge_document_metadata
    from packages.core.services.runtime_learning import record_task_execution_evidence
    from packages.core.services.task_service import update_task
    from packages.core.tasks import ai_tasks
    from packages.core.workspace_chat import context as chat_context

    now = datetime.now(timezone.utc)
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    calls: list[dict] = []

    class _FakeStrategistTask:
        @staticmethod
        def apply_async(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return None

    monkeypatch.setattr(ai_tasks, "run_strategist_review", _FakeStrategistTask)

    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="X Account - AI Tech Founder",
        description="One-person company content engine demo.",
        kind="founder_content_engine",
        category="growth",
        primary_work=(
            "Plan weekly founder content, draft X and LinkedIn posts, require "
            "founder approval before publishing, and learn from engagement."
        ),
        heartbeat_enabled=True,
        heartbeat_cadence="weekly",
        monthly_budget_usd=Decimal("25.000000"),
        auto_pause_on_budget=True,
        operating_model={
            "services": [
                {"key": "content_strategy", "name": "Content Strategist"},
                {"key": "draft_generation", "name": "Draft Writer"},
                {"key": "analytics_review", "name": "Analytics Reviewer"},
            ],
            "goals": [
                {"metric_key": "weekly_posts", "target": 5},
                {"metric_key": "engagement_rate", "target": 6},
            ],
            "rules": [
                {
                    "rule_key": "founder_review_before_publish",
                    "description": "Public X/LinkedIn posts must be approved by the founder before publishing.",
                    "severity": "high",
                    "action_patterns": ["social_post.publish", "external_message.send"],
                }
            ],
            "knowledge": {
                "nets": ["Founder Voice & Positioning", "Market POV Library"],
            },
            "evaluation": {
                "loop": "After every active work batch completes, compare task outputs, goal movement, artifacts, approvals, and runtime evidence before creating the next batch.",
                "trigger_strategist_when_all_complete": True,
            },
        },
        settings={"demo": "solo_founder_content_engine"},
    )
    db_session.add(workspace)

    await update_policy(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
        policy=WorkspacePolicy(
            hitl_required_actions=["social_post.publish", "external_message.send"],
            auto_approve_actions=["generate_file", "workspace_search", "analytics.report"],
            budget_caps_per_kind={"action": 1200},
        ),
        changed_by="demo-test",
        change_summary="Founder must approve public publishing.",
    )

    subscriptions: dict[str, tuple[str, str]] = {}
    for service_key, name, tools in [
        ("content_strategy", "Founder Content Strategist", ["workspace_search", "generate_file"]),
        ("draft_generation", "Founder Draft Writer", ["workspace_search", "generate_file", "social.schedule_draft"]),
        ("analytics_review", "Founder Analytics Analyst", ["workspace_search", "analytics.report"]),
    ]:
        agent_id = generate_ulid()
        subscription_id = generate_ulid()
        subscriptions[service_key] = (agent_id, subscription_id)
        db_session.add(
            Agent(
                id=agent_id,
                entity_id=entity_id,
                name=name,
                system_prompt=f"Own the {service_key} lane for a solo founder workspace.",
                config={"tools": tools},
                source="custom",
                status="active",
            )
        )
        db_session.add(
            AgentSubscription(
                id=subscription_id,
                entity_id=entity_id,
                agent_id=agent_id,
                workspace_id=workspace_id,
                name=name,
                service_key=service_key,
                config={"tool_bindings": tools},
                status="active",
            )
        )

    goal_ids: list[str] = []
    for title, metric_key, current, target, pace in [
        ("Publish 5 high-signal founder posts this week", "weekly_posts", "2.0000", "5.0000", "behind"),
        ("Lift average engagement rate to 6%", "engagement_rate", "4.2000", "6.0000", "at_risk"),
    ]:
        goal_id = generate_ulid()
        goal_ids.append(goal_id)
        db_session.add(
            Goal(
                id=goal_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                title=title,
                metric_key=metric_key,
                baseline_value=Decimal("0.0000"),
                current_value=Decimal(current),
                target_value=Decimal(target),
                current_value_updated_at=now,
                pace_status=pace,
                measurement_cadence="daily",
                measurement_source={"provider": "manual_demo", "action": "update_metric"},
                priority=1,
            )
        )

    voice_group_id = generate_ulid()
    market_group_id = generate_ulid()
    workspace_files_group_id = generate_ulid()
    for group_id, name, settings in [
        (
            voice_group_id,
            "Founder Voice & Positioning",
            {
                "kind": "knowledge_net",
                "purpose": "Durable voice, positioning, and banned phrasing for founder posts.",
                "linked_service_keys": ["content_strategy", "draft_generation"],
            },
        ),
        (
            market_group_id,
            "Market POV Library",
            {
                "kind": "knowledge_net",
                "purpose": "Reusable takes, customer pain points, and market observations.",
                "linked_service_keys": ["content_strategy"],
            },
        ),
        (
            workspace_files_group_id,
            "Workspace Files",
            {"workspace_file_bucket": True, "kind": "workspace_files"},
        ),
    ]:
        db_session.add(
            DocumentGroup(
                id=group_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                name=name,
                settings=settings,
            )
        )

    for group_id, name, content_text in [
        (voice_group_id, "Founder Voice Rules.md", "Use specific builder lessons, avoid generic AI hype."),
        (voice_group_id, "Approved Post Examples.md", "Short hook, concrete pain, one useful lesson."),
        (market_group_id, "Customer Pain Points.md", "Solo operators need automation without losing control."),
    ]:
        doc_id = generate_ulid()
        db_session.add(
            Document(
                id=doc_id,
                entity_id=entity_id,
                name=name,
                fs_path=f"knowledge/{workspace_id}/{name}",
                file_type="md",
                mime_type="text/markdown",
                vector_status=VectorStatus.READY,
                source="manual",
                metadata_={"content_text": content_text},
            )
        )
        db_session.add(DocumentGroupMember(document_id=doc_id, group_id=group_id))

    batch_id = generate_ulid()
    task_specs = [
        {
            "title": "Build this week's founder content calendar",
            "description": "Turn goals and voice docs into a five-post weekly plan.",
            "service": "content_strategy",
            "tools": ["workspace_search", "generate_file"],
            "artifact_name": "Weekly Founder Content Calendar.md",
            "fs_path": f"workspace/{workspace_id}/artifacts/weekly-founder-content-calendar.md",
            "summary": "Generated a five-post content calendar.",
        },
        {
            "title": "Draft X and LinkedIn posts from the calendar",
            "description": "Use the calendar artifact and approved examples to draft platform-specific posts.",
            "service": "draft_generation",
            "tools": ["workspace_search", "generate_file", "social.schedule_draft"],
            "artifact_name": "Founder Launch Post Drafts.md",
            "fs_path": f"workspace/{workspace_id}/artifacts/founder-launch-post-drafts.md",
            "summary": "Drafted X and LinkedIn posts but did not publish.",
        },
        {
            "title": "Review engagement and propose next experiments",
            "description": "Summarize early engagement signals and recommend the next content experiments.",
            "service": "analytics_review",
            "tools": ["workspace_search", "analytics.report"],
            "artifact_name": None,
            "fs_path": None,
            "summary": "Identified two experiments for the next wave.",
        },
    ]
    task_ids: list[str] = []
    task_artifacts: dict[str, dict] = {}
    for spec in task_specs:
        task_id = generate_ulid()
        task_ids.append(task_id)
        agent_id, subscription_id = subscriptions[spec["service"]]
        runtime_context = {
            "knowledge_query": "founder voice rules approved post examples market pain points",
            "required_refs": [f"knowledge_net:{voice_group_id}", f"knowledge_net:{market_group_id}"],
            "rules": [
                {
                    "rule_key": "founder_review_before_publish",
                    "description": "Draft freely, but do not publish externally before founder approval.",
                    "rule_type": "approval_required",
                    "action_patterns": ["social_post.publish", "external_message.send"],
                }
            ],
        }
        db_session.add(
            Task(
                id=task_id,
                entity_id=entity_id,
                workspace_id=workspace_id,
                title=spec["title"],
                description=spec["description"],
                status="in_progress",
                priority=2,
                task_type="ai_generated",
                agent_id=agent_id,
                owner_service_key=spec["service"],
                owner_subscription_id=subscription_id,
                delegate_service_keys=[spec["service"]],
                details={
                    "workspace_work_batch_id": batch_id,
                    "runtime_context": runtime_context,
                    "demo": "solo_founder_content_engine",
                },
                expected_output={
                    "type": "object",
                    "required": ["summary"],
                    "properties": {"files": {"type": "array"}},
                },
                started_at=now,
            )
        )
        db_session.add(
            GoalTaskLink(
                goal_id=goal_ids[0],
                task_id=task_id,
                contribution="direct",
                estimated_impact=Decimal("1.0000"),
            )
        )
        if spec["artifact_name"]:
            artifact_id = generate_ulid()
            artifact_ref = {
                "document_id": artifact_id,
                "name": spec["artifact_name"],
                "fs_path": spec["fs_path"],
                "file_type": "md",
                "artifact_role": "draft",
            }
            task_artifacts[task_id] = artifact_ref
            db_session.add(
                Document(
                    id=artifact_id,
                    entity_id=entity_id,
                    name=spec["artifact_name"],
                    fs_path=spec["fs_path"],
                    file_type="md",
                    mime_type="text/markdown",
                    vector_status=VectorStatus.READY,
                    source="agent",
                    metadata_=merge_document_metadata(
                        origin={
                            "workspace_id": workspace_id,
                            "task_id": task_id,
                            "agent_id": agent_id,
                            "tool_name": "generate_file",
                        },
                        artifact={"role": "draft", "storage_scope": "artifact"},
                        extra={
                            "content_text": f"{spec['summary']} References founder voice and weekly_posts goal.",
                            "demo": "solo_founder_content_engine",
                        },
                    ),
                )
            )
            db_session.add(DocumentGroupMember(document_id=artifact_id, group_id=workspace_files_group_id))

    db_session.add(
        WorkspaceWorkBatch(
            id=batch_id,
            workspace_id=workspace_id,
            entity_id=entity_id,
            source_kind="demo_solo_founder",
            summary="Solo founder content engine weekly wave.",
            status="active",
            task_ids=task_ids,
            details={
                "trigger_strategist_when_all_complete": True,
                "goal_ids": goal_ids,
                "demo": "solo_founder_content_engine",
            },
        )
    )
    await db_session.commit()

    publish_decision = await check_step_policy(
        db_session,
        workspace_id=workspace_id,
        kind="action",
        action_key="social_post.publish",
        risk_level="medium",
        task_id=task_ids[1],
    )
    assert publish_decision.allowed is False
    assert publish_decision.pause_for_hitl is True
    assert publish_decision.matched_rule == "social_post.publish"

    draft_decision = await check_step_policy(
        db_session,
        workspace_id=workspace_id,
        kind="action",
        action_key="generate_file",
        risk_level="low",
        task_id=task_ids[1],
    )
    assert draft_decision.allowed is True

    for index, (task_id, spec) in enumerate(zip(task_ids, task_specs, strict=True)):
        files = [task_artifacts[task_id]] if task_id in task_artifacts else []
        actual_output = {
            "summary": spec["summary"],
            "files": files,
            "steps": [
                {
                    "key": "search_context",
                    "status": "done",
                    "result_summary": "Loaded workspace goals, voice docs, prior artifacts, and rules.",
                },
                {
                    "key": "produce_output",
                    "status": "done",
                    "result_summary": spec["summary"],
                    "files": files,
                },
            ],
        }
        updated = await update_task(
            db_session,
            task_id,
            entity_id,
            status="completed",
            actual_output=actual_output,
        )
        assert updated is not None
        await record_task_execution_evidence(
            db_session,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            plan_id=f"plan_{task_id}",
            task_status="completed",
            plan_status="completed",
            task_title=spec["title"],
            task_description=spec["description"],
            owner_service_key=spec["service"],
            delegate_service_keys=[spec["service"]],
            agent_id=subscriptions[spec["service"]][0],
            steps=[
                SimpleNamespace(
                    id=f"{task_id}_search",
                    step_key="search_context",
                    kind="action",
                    step_status="done",
                    provider="manor",
                    action_key="workspace_search",
                    service_key=spec["service"],
                    resolved_agent_id=subscriptions[spec["service"]][0],
                    result={"text": "Loaded relevant workspace context."},
                    evidence_refs=[],
                    cost={"usd": 0.001},
                    error=None,
                ),
                SimpleNamespace(
                    id=f"{task_id}_produce",
                    step_key="produce_output",
                    kind="action",
                    step_status="done",
                    provider="manor",
                    action_key=spec["tools"][-1],
                    service_key=spec["service"],
                    resolved_agent_id=subscriptions[spec["service"]][0],
                    result={"text": spec["summary"], "files": files},
                    evidence_refs=[],
                    cost={"usd": 0.01},
                    error=None,
                ),
            ],
            actual_output=actual_output,
            cost_tracking={"usd": 0.011},
            started_at=now,
            completed_at=now,
            source="solo_founder_demo_test",
        )
        await db_session.commit()

        batch = (
            await db_session.execute(select(WorkspaceWorkBatch).where(WorkspaceWorkBatch.id == batch_id))
        ).scalar_one()
        if index < len(task_ids) - 1:
            assert batch.status == "active"
            assert calls == []
        else:
            assert batch.status == "completed"
            assert batch.completed_at is not None
            assert len(calls) == 1
            assert calls[0]["kwargs"]["args"] == [workspace_id, f"work_batch_completed:{batch_id}"]

    artifact_search = await chat_context.workspace_search(
        db_session,
        workspace_id,
        entity_id,
        category="artifacts",
        query="launch",
    )
    assert "## Workspace Artifacts" in artifact_search
    assert "Founder Launch Post Drafts.md" in artifact_search
    assert f"task_id={task_ids[1]}" in artifact_search
    assert "tool=generate_file" in artifact_search

    goal_search = await chat_context.workspace_search(
        db_session,
        workspace_id,
        entity_id,
        category="goals",
        query="weekly",
    )
    assert "Publish 5 high-signal founder posts this week" in goal_search
    assert "2 / 5" in goal_search

    knowledge_search = await chat_context.workspace_search(
        db_session,
        workspace_id,
        entity_id,
        category="knowledge",
        query="voice",
    )
    assert "Founder Voice & Positioning" in knowledge_search
    assert "Founder Voice Rules.md" in knowledge_search
    assert "Workspace Files" not in knowledge_search

    rules_search = await chat_context.workspace_search(
        db_session,
        workspace_id,
        entity_id,
        category="rules",
        query="publish",
    )
    assert "Public X/LinkedIn posts must be approved" in rules_search
    assert "Approval required: social_post.publish, external_message.send" in rules_search

    runtime_search = await chat_context.workspace_search(
        db_session,
        workspace_id,
        entity_id,
        category="runtime",
        query="tool pattern worked",
    )
    assert "## Learning Candidates" in runtime_search
    assert "Tool pattern worked" in runtime_search

    evidence = (
        (await db_session.execute(select(RuntimeEvidence).where(RuntimeEvidence.workspace_id == workspace_id)))
        .scalars()
        .all()
    )
    candidates = (
        (
            await db_session.execute(
                select(AgentLearningCandidate).where(AgentLearningCandidate.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    assert len([ev for ev in evidence if ev.evidence_type == "task_run"]) == 3
    assert any(candidate.candidate_type == "tool_experience" for candidate in candidates)
