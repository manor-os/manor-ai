"""M7/M8 — proposal_items bookkeeping + HitlRequest wiring (v1 slice).

On the strategist_review_v2 path each persisted proposed Task also gets a
``proposal_items`` row, and the cohort's approval is resolved ONCE through
the unified approval core:

* standing grant (policy auto_approve_actions) → auto-approve, items
  ``approved`` with the work-batch execution root
* no grant → items stay ``proposed`` behind one pending HitlRequest;
  the existing chat-card approve/reject mirrors decisions onto items and
  settles the request (grant+consume / deny)
* legacy boolean ``strategist.auto_approve_proposals`` still auto-approves
* flag off → zero proposal rows (legacy path untouched)
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from packages.core.ledger import event_types as et
from packages.core.ledger import record_event
from packages.core.models.base import generate_ulid
from packages.core.models.feature_flag import FeatureFlag
from packages.core.models.goal import Goal
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
from packages.core.models.task import Task
from packages.core.models.user import User, UserMembership
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.models.workspace_event import WorkspaceEvent
from packages.core.proposals.service import decide_items
from packages.core.services import feature_flags as feature_flags_service
from packages.core.strategist import service as strategist_service
from packages.core.tasks import ai_tasks
from packages.core.tasks.ai_tasks import _execute_strategist_review_cycle

FLAG_KEY = "strategist_review_v2"

_seq = 0


async def _seed_workspace(db, *, settings: dict | None = None) -> Workspace:
    entity_id = generate_ulid()
    actor_id = entity_id
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Proposal Items WS",
        status="active",
        settings=settings or {},
    )
    goal = Goal(
        entity_id=entity_id,
        workspace_id=workspace.id,
        title="Grow followers",
        metric_key="follower_count",
        target_value=1000,
        status="active",
    )
    agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Ops Agent",
        status="active",
    )
    subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace.id,
        agent_id=agent.id,
        service_key="ops",
        status="active",
    )
    db.add_all([
        User(
            id=actor_id,
            entity_id=entity_id,
            email=f"{actor_id}@example.com",
            password_hash="test-only",
            role="owner",
            status="active",
        ),
        UserMembership(
            user_id=actor_id,
            entity_id=entity_id,
            role="owner",
            status="active",
        ),
        workspace,
        goal,
        agent,
        subscription,
    ])
    await db.commit()
    return workspace


async def _emit(db, workspace: Workspace) -> None:
    global _seq
    _seq += 1
    event = await record_event(
        db,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        event_type=et.EXECUTION_COMPLETED,
        source_kind="task",
        source_id=f"task_{_seq}",
        idempotency_key=f"proposal-items:{workspace.id}:{_seq}",
    )
    assert event is not None
    await db.commit()
    await asyncio.sleep(0.002)


async def _set_flag(db, enabled: bool) -> None:
    flag = (await db.execute(
        select(FeatureFlag).where(FeatureFlag.key == FLAG_KEY)
    )).scalar_one_or_none()
    if flag is None:
        db.add(FeatureFlag(key=FLAG_KEY, description="test", default_enabled=enabled))
    else:
        flag.default_enabled = enabled
    await db.commit()
    feature_flags_service._bump_cache()


_TWO_TASKS = {
    "summary": "Draft docs, then publish.",
    "tasks": [
        {
            "task_key": "draft_docs",
            "title": "Draft source docs",
            "description": "Write the source docs for this cycle.",
            "owner_service_key": "ops",
            "priority": 3,
            "correlation_key": "draft_docs_weekly",
            "basis": {
                # "goal" is a real briefing domain; "bogus_domain" is not —
                # the v1 validator strips it and keeps the valid ref.
                "report_refs": ["goal", "bogus_domain"],
                "evidence_refs": ["ev_1"],
            },
            "deliverables": [{
                "name": "docs",
                "kind": "value",
                "shape": "TextResult",
                "acceptance": "docs drafted",
                "usage": "input for publish",
            }],
        },
        {
            "task_key": "publish_docs",
            "title": "Publish the docs",
            "description": "Publish once drafting completes.",
            "owner_service_key": "ops",
            "priority": 3,
            "depends_on_task_keys": ["draft_docs"],
            "basis": {"report_refs": ["execution"], "evidence_refs": []},
            "deliverables": [{
                "name": "published",
                "kind": "value",
                "shape": "TextResult",
                "acceptance": "docs published",
                "usage": "operator review",
            }],
        },
    ],
}


def _fake_completion(payload: dict = _TWO_TASKS):
    async def fake(system_prompt, user_prompt, **kwargs):
        return SimpleNamespace(content=json.dumps(payload))
    return fake


async def _run_v2_review(
    db,
    monkeypatch,
    workspace: Workspace,
    *,
    flag: bool = True,
    payload: dict = _TWO_TASKS,
) -> dict:
    await _emit(db, workspace)
    await _set_flag(db, flag)
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _fake_completion(payload),
    )
    # Keep the review off real chat + Celery surfaces.
    async def _noop_post(*args, **kwargs):
        return None
    monkeypatch.setattr(strategist_service, "_post_proposal_chat", _noop_post)
    monkeypatch.setattr(ai_tasks.plan_and_run_task, "delay", lambda task_id: None)
    return await _execute_strategist_review_cycle(db, workspace.id, "scheduled")


async def _items(db, review_id: str) -> list[ProposalItemRecord]:
    return list((await db.execute(
        select(ProposalItemRecord)
        .join(ProposalRecord, ProposalItemRecord.proposal_id == ProposalRecord.id)
        .where(ProposalRecord.review_id == review_id)
        .order_by(ProposalItemRecord.item_key.asc())
    )).scalars().all())


async def _tasks(db, workspace: Workspace) -> dict[str, Task]:
    rows = (await db.execute(
        select(Task).where(Task.workspace_id == workspace.id)
    )).scalars().all()
    return {t.details.get("strategist_task_key"): t for t in rows}


async def _proposal_request(db, proposal_id: str) -> HitlRequest | None:
    return (await db.execute(
        select(HitlRequest).where(
            HitlRequest.dedup_key == f"proposal:{proposal_id}",
        ).order_by(HitlRequest.created_at.desc()).limit(1)
    )).scalar_one_or_none()


def test_workflow_run_proposal_contract_is_first_class() -> None:
    from packages.core.proposals.constants import ACTION_KEY_BY_KIND, ITEM_KINDS
    from packages.core.proposals.schema import PAYLOAD_MODEL_BY_KIND, WorkflowRunPayload
    from packages.core.strategist.proposal import Proposal

    proposal = Proposal.model_validate({
        "review_id": "review-flow",
        "summary": "Create the approved product video.",
        "tasks": [],
        "workflow_runs": [{
            "run_key": "create_product_video",
            "workflow_ref": {
                "blueprint_slug": "product-video-studio-v1",
                "workflow_slug": "create-product-video-v1",
            },
            "inputs": {"product_name": "Manor"},
            "source_brief": "Create a concise Manor product video.",
            "rationale": "The installed Flow already owns this production pipeline.",
        }],
    })

    assert "workflow_run" in ITEM_KINDS
    assert ACTION_KEY_BY_KIND["workflow_run"] == "workspace.proposal.workflow_run"
    assert PAYLOAD_MODEL_BY_KIND["workflow_run"] is WorkflowRunPayload
    assert proposal.workflow_runs[0].run_key == "create_product_video"


async def test_workflow_run_proposal_item_persists_with_wr_prefix(db_session) -> None:
    from packages.core.proposals.service import create_workflow_run_items
    from packages.core.strategist.proposal import ProposedWorkflowRun

    workspace = await _seed_workspace(db_session)
    record = ProposalRecord(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        review_id=generate_ulid(),
        summary="Run an installed Flow",
        status="open",
    )
    db_session.add(record)
    await db_session.flush()
    proposed = ProposedWorkflowRun.model_validate({
        "run_key": "create_product_video",
        "workflow_ref": {
            "blueprint_slug": "product-video-studio-v1",
            "workflow_slug": "create-product-video-v1",
        },
        "inputs": {"product_name": "Manor"},
        "source_brief": "Create a Manor product video.",
        "rationale": "Use the installed production Flow.",
    })

    items = await create_workflow_run_items(
        db_session,
        record=record,
        proposed_runs=[proposed],
    )

    assert len(items) == 1
    assert items[0].item_key == "wr_create_product_video"
    assert items[0].kind == "workflow_run"
    assert items[0].status == "proposed"
    assert items[0].action_key == "workspace.proposal.workflow_run"
    assert items[0].risk_level == "medium"


async def test_public_workflow_run_proposal_uses_declared_external_risk(db_session) -> None:
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
    from packages.core.proposals.service import create_workflow_run_items
    from packages.core.strategist.proposal import ProposedWorkflowRun

    workspace = await _seed_workspace(db_session)
    workspace.settings = {
        **dict(workspace.settings or {}),
        "_blueprint": {"blueprint_slug": "video-studio-v1"},
    }
    workflow = WorkflowDefinition(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        created_by=workspace.entity_id,
        name="publish-video-v1",
        variables={},
        steps=[
            {"id": "start", "type": "trigger", "config": {}},
            {"id": "upload_youtube_video", "type": "agent", "config": {}},
            {"id": "set_youtube_visibility", "type": "agent", "config": {}},
        ],
        status="active",
        is_active=True,
    )
    db_session.add(workflow)
    await db_session.flush()
    db_session.add(WorkflowBinding(
        entity_id=workspace.entity_id,
        workflow_id=workflow.id,
        workspace_id=workspace.id,
        name="Publish video",
        trigger_type="manual",
        enabled=True,
        status="active",
        config={
            "workspace_blueprint_workflow_slug": "publish-video-v1",
            "chat_entrypoint": {
                "enabled": True,
                "title": "Publish video",
                "run_inputs": [{
                    "key": "youtube_visibility",
                    "type": "string",
                    "required": True,
                    "target": "request.youtube_visibility",
                }],
            },
            "proposal_authorization": {
                "kind": "youtube_publication_v1",
                "action_key": "workspace.proposal.workflow_run.external",
                "when": {"input_key": "youtube_visibility", "equals": "public"},
                "destination": "studio.youtube.com",
                "upload_step_id": "upload_youtube_video",
                "publish_step_id": "set_youtube_visibility",
                "ttl_seconds": 86400,
            },
        },
    ))
    record = ProposalRecord(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        review_id=generate_ulid(),
        summary="Publish a video",
        status="open",
    )
    db_session.add(record)
    await db_session.flush()

    items = await create_workflow_run_items(
        db_session,
        record=record,
        proposed_runs=[ProposedWorkflowRun.model_validate({
            "run_key": "publish_video",
            "workflow_ref": {
                "blueprint_slug": "video-studio-v1",
                "workflow_slug": "publish-video-v1",
            },
            "inputs": {"youtube_visibility": "public"},
            "source_brief": "Publish today's verified video.",
            "rationale": "Run the installed Flow.",
        })],
    )

    assert items[0].risk_level == "high"
    assert items[0].action_key == "workspace.proposal.workflow_run.external"
    assert items[0].payload["_proposal_authorization_binding"]["revision"] == 1


async def test_approved_workflow_run_item_dispatches_once_through_shared_launcher(
    db_session,
    monkeypatch,
) -> None:
    from unittest.mock import AsyncMock

    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
    from packages.core.proposals.service import create_workflow_run_items
    from packages.core.services.auth_service import hash_password
    from packages.core.services.proposal_workflow_runs import (
        ProposalWorkflowRunConflict,
        dispatch_workflow_run_item,
    )
    from packages.core.strategist.proposal import ProposedWorkflowRun

    workspace = await _seed_workspace(db_session)
    user = User(
        entity_id=workspace.entity_id,
        email="proposal-flow-owner@test.com",
        display_name="Proposal Flow Owner",
        password_hash=hash_password("pass123"),
        role="owner",
        status="active",
    )
    db_session.add(user)
    await db_session.flush()
    workspace.settings = {
        **dict(workspace.settings or {}),
        "created_by_user_id": user.id,
        "_blueprint": {"blueprint_slug": "product-video-studio-v1"},
    }
    workflow = WorkflowDefinition(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        created_by=user.id,
        name="Create product video",
        variables={"request": {}},
        steps=[{
            "id": "start",
            "type": "trigger",
            "config": {
                "run_inputs": [{
                    "key": "product_name",
                    "type": "string",
                    "required": True,
                    "target": "request.product_name",
                }],
            },
        }],
        status="active",
        is_active=True,
    )
    db_session.add(workflow)
    await db_session.flush()
    binding = WorkflowBinding(
        entity_id=workspace.entity_id,
        workflow_id=workflow.id,
        workspace_id=workspace.id,
        name="Create product video",
        trigger_type="manual",
        enabled=True,
        status="active",
        config={
            "source": "blueprint",
            "workspace_blueprint_workflow_slug": "create-product-video-v1",
            "chat_entrypoint": {"enabled": True, "title": "Create product video"},
        },
    )
    db_session.add(binding)
    record = ProposalRecord(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        review_id=generate_ulid(),
        summary="Run an installed Flow",
        status="open",
    )
    db_session.add(record)
    await db_session.flush()
    items = await create_workflow_run_items(
        db_session,
        record=record,
        proposed_runs=[ProposedWorkflowRun.model_validate({
            "run_key": "create_product_video",
            "workflow_ref": {
                "blueprint_slug": "product-video-studio-v1",
                "workflow_slug": "create-product-video-v1",
            },
            "inputs": {"product_name": "Manor"},
            "source_brief": "Create a Manor product video.",
            "rationale": "Use the installed production Flow.",
        })],
    )
    item = items[0]
    item.status = "approved"
    await db_session.commit()

    from packages.core.models.workflow import WorkflowRun

    launched_run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=workflow.id,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        binding_id=binding.id,
        trigger_source="proposal",
        status="paused",
        variables={},
        step_results={},
        trigger_data={},
        definition_snapshot={},
        execution_trace=[],
        started_by=user.id,
        lineage_root_run_id=None,
    )
    async def _launch(*_args, **_kwargs):
        db_session.add(launched_run)
        await db_session.flush()
        return SimpleNamespace(run=launched_run, created=True)

    launch = AsyncMock(side_effect=_launch)
    monkeypatch.setattr(
        "packages.core.services.workspace_flow_launcher.launch_workspace_flow",
        launch,
    )
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "packages.core.services.workspace_flow_launcher.enqueue_workspace_flow_launch",
        enqueue,
    )

    first = await dispatch_workflow_run_item(
        db_session,
        item_id=item.id,
        actor_id=user.id,
    )
    second = await dispatch_workflow_run_item(
        db_session,
        item_id=item.id,
        actor_id=user.id,
    )

    assert first.id == launched_run.id
    assert second.id == launched_run.id
    assert launch.await_count == 1
    assert launch.await_args.kwargs["starter_policy"] == "only_missing"
    assert launch.await_args.kwargs["transaction_owner"] == "caller"
    assert launch.await_args.kwargs["proposal_context"]["proposal_item_id"] == item.id
    await db_session.refresh(item)
    assert item.status == "executing"
    assert item.execution_root_id == launched_run.id
    enqueue.assert_awaited_once()

    # Exactly-once is per proposal item, while the binding-level lock and
    # final live-run check stop a different item from racing a second lineage.
    duplicate = (await create_workflow_run_items(
        db_session,
        record=record,
        proposed_runs=[ProposedWorkflowRun.model_validate({
            "run_key": "duplicate_product_video",
            "workflow_ref": {
                "blueprint_slug": "product-video-studio-v1",
                "workflow_slug": "create-product-video-v1",
            },
            "inputs": {"product_name": "Manor"},
            "source_brief": "Duplicate the Manor product video.",
            "rationale": "Exercise the final dispatch guard.",
        })],
    ))[0]
    duplicate.status = "approved"
    await db_session.flush()

    import pytest

    with pytest.raises(ProposalWorkflowRunConflict) as exc_info:
        await dispatch_workflow_run_item(
            db_session,
            item_id=duplicate.id,
            actor_id=user.id,
        )
    assert exc_info.value.run_id == launched_run.id
    assert launch.await_count == 1


async def test_workflow_run_item_lifecycle_tracks_retry_lineage(db_session) -> None:
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.proposal_workflow_runs import (
        sync_proposal_workflow_run_item,
    )

    workspace = await _seed_workspace(db_session)
    record = ProposalRecord(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        review_id=generate_ulid(),
        summary="Run an installed Flow",
        status="resolved",
    )
    db_session.add(record)
    await db_session.flush()
    item = ProposalItemRecord(
        proposal_id=record.id,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        item_key="wr_create_product_video",
        kind="workflow_run",
        payload={"run_key": "create_product_video"},
        risk_level="medium",
        action_key="workspace.proposal.workflow_run",
        status="executing",
    )
    db_session.add(item)
    await db_session.flush()
    root = WorkflowRun(
        id=generate_ulid(),
        workflow_id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        trigger_source="proposal",
        status="failed",
        current_step_id="collect_assets",
        variables={},
        step_results={},
        trigger_data={"_proposal_context": {"proposal_item_id": item.id}},
        definition_snapshot={},
        execution_trace=[],
    )
    db_session.add(root)
    await db_session.flush()
    item.execution_root_id = root.id

    await sync_proposal_workflow_run_item(db_session, root)
    assert item.status == "executing"
    assert item.finished_at is None
    assert item.decision["latest_workflow_run_id"] == root.id

    retry = WorkflowRun(
        id=generate_ulid(),
        workflow_id=root.workflow_id,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        trigger_source="proposal",
        retry_of_run_id=root.id,
        lineage_root_run_id=root.id,
        attempt_number=2,
        status="completed",
        variables={"project": {"state": {"business_outcome": "accepted"}}},
        step_results={},
        trigger_data={"_proposal_context": {"proposal_item_id": item.id}},
        definition_snapshot={},
        execution_trace=[],
    )
    db_session.add(retry)
    await db_session.flush()

    await sync_proposal_workflow_run_item(db_session, retry)
    assert item.status == "succeeded"
    assert item.execution_root_id == root.id
    assert item.finished_at is not None
    assert item.decision["latest_workflow_run_id"] == retry.id

    await sync_proposal_workflow_run_item(db_session, root)
    assert item.status == "succeeded"
    assert item.decision["latest_workflow_run_id"] == retry.id


async def test_workflow_run_item_lifecycle_distinguishes_actionable_and_terminal_outcomes(
    db_session,
) -> None:
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.proposal_workflow_runs import (
        sync_proposal_workflow_run_item,
    )

    workspace = await _seed_workspace(db_session)
    record = ProposalRecord(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        review_id=generate_ulid(),
        summary="Run an installed Flow",
        status="resolved",
    )
    db_session.add(record)
    await db_session.flush()

    cases = [
        ("completed", "needs_input", "collect_assets", "executing"),
        ("completed", "revision_required", "render_video", "executing"),
        ("cancelled", "in_progress", None, "cancelled"),
        ("failed", "in_progress", None, "failed"),
    ]
    for index, (run_status, business_outcome, step_id, expected_status) in enumerate(cases):
        item = ProposalItemRecord(
            proposal_id=record.id,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            item_key=f"wr_case_{index}",
            kind="workflow_run",
            payload={"run_key": f"case_{index}"},
            risk_level="medium",
            action_key="workspace.proposal.workflow_run",
            status="executing",
        )
        db_session.add(item)
        await db_session.flush()
        run = WorkflowRun(
            id=generate_ulid(),
            workflow_id=generate_ulid(),
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            trigger_source="proposal",
            status=run_status,
            current_step_id=step_id,
            variables={"project": {"state": {"business_outcome": business_outcome}}},
            step_results={},
            trigger_data={"_proposal_context": {"proposal_item_id": item.id}},
            definition_snapshot={},
            execution_trace=[],
        )
        db_session.add(run)
        await db_session.flush()
        item.execution_root_id = run.id

        await sync_proposal_workflow_run_item(db_session, run)

        assert item.status == expected_status
        assert (item.finished_at is not None) == (
            expected_status in {"succeeded", "failed", "cancelled"}
        )


async def test_workflow_projection_syncs_proposal_item_without_chat_activity(
    db_session,
) -> None:
    from packages.core.models.workflow import WorkflowRun
    from packages.core.services.workflow_chat_projection import (
        project_workflow_run_status,
    )

    workspace = await _seed_workspace(db_session)
    record = ProposalRecord(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        review_id=generate_ulid(),
        summary="Run an installed Flow",
        status="resolved",
    )
    db_session.add(record)
    await db_session.flush()
    item = ProposalItemRecord(
        proposal_id=record.id,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        item_key="wr_projection",
        kind="workflow_run",
        payload={"run_key": "projection"},
        risk_level="medium",
        action_key="workspace.proposal.workflow_run",
        status="executing",
    )
    db_session.add(item)
    await db_session.flush()
    run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        trigger_source="proposal",
        status="completed",
        variables={},
        step_results={},
        trigger_data={"_proposal_context": {"proposal_item_id": item.id}},
        definition_snapshot={},
        execution_trace=[],
    )
    db_session.add(run)
    await db_session.flush()
    item.execution_root_id = run.id

    await project_workflow_run_status(db_session, run=run)

    assert item.status == "succeeded"
    assert item.finished_at is not None


async def test_strategist_persists_installed_flow_run_behind_item_approval(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition

    workspace = await _seed_workspace(db_session)
    workspace.settings = {
        **dict(workspace.settings or {}),
        "_blueprint": {
            "blueprint_slug": "product-video-studio-v1",
            "live_setup_requirements": [],
        },
    }
    workflow = WorkflowDefinition(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        name="Create product video",
        variables={"request": {}},
        steps=[{
            "id": "start",
            "type": "trigger",
            "config": {
                "run_inputs": [{"key": "product_name", "type": "string"}],
            },
        }],
        status="active",
        is_active=True,
    )
    db_session.add(workflow)
    await db_session.flush()
    db_session.add(WorkflowBinding(
        entity_id=workspace.entity_id,
        workflow_id=workflow.id,
        workspace_id=workspace.id,
        name="Create product video",
        trigger_type="manual",
        enabled=True,
        status="active",
        config={
            "source": "blueprint",
            "workspace_blueprint_workflow_slug": "create-product-video-v1",
            "chat_entrypoint": {"enabled": True, "title": "Create product video"},
        },
    ))
    await db_session.commit()
    await _emit(db_session, workspace)
    await _set_flag(db_session, True)
    monkeypatch.setattr(
        "packages.core.strategist.prompt.runtime_execute_strategist_completion",
        _fake_completion({
            "summary": "Use the installed video production Flow.",
            "tasks": [],
            "workflow_runs": [{
                "run_key": "create_product_video",
                "workflow_ref": {
                    "blueprint_slug": "product-video-studio-v1",
                    "workflow_slug": "create-product-video-v1",
                },
                "inputs": {"product_name": "Manor"},
                "source_brief": "Create a concise Manor product video.",
                "rationale": "The installed Flow owns the complete production pipeline.",
            }],
        }),
    )
    monkeypatch.setattr(strategist_service, "_post_proposal_chat", AsyncMock())

    result = await _execute_strategist_review_cycle(
        db_session,
        workspace.id,
        "scheduled",
    )

    items = await _items(db_session, result["review_id"])
    assert len(items) == 1
    assert items[0].kind == "workflow_run"
    assert items[0].status == "proposed"
    assert items[0].approval_request_id
    request = await db_session.get(HitlRequest, items[0].approval_request_id)
    assert request.action_key == "workspace.proposal.workflow_run"
    assert result["pending_items"][0]["kind"] == "workflow_run"


# ── persistence + validator ────────────────────────────────────────────

async def test_v2_review_persists_proposal_record_and_items(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    result = await _run_v2_review(db_session, monkeypatch, workspace)

    assert not result.get("skipped")
    assert result["task_count"] == 2
    assert result["approval_outcome"] == "needs_human"

    record = (await db_session.execute(
        select(ProposalRecord).where(ProposalRecord.review_id == result["review_id"])
    )).scalar_one()
    assert record.workspace_id == workspace.id
    assert record.status == "open"
    assert record.summary == "Draft docs, then publish."
    assert result["proposal_id"] == record.id

    items = await _items(db_session, result["review_id"])
    assert [i.item_key for i in items] == ["draft_docs", "publish_docs"]
    by_key = {i.item_key: i for i in items}
    tasks = await _tasks(db_session, workspace)

    for key, item in by_key.items():
        assert item.kind == "task"
        assert item.action_key == "workspace.proposal.task"
        assert item.risk_level == "low"
        assert item.status == "proposed"
        assert item.payload["task_id"] == tasks[key].id

    # Validator stripped the unknown report ref, kept the valid domain ref.
    assert by_key["draft_docs"].basis["report_refs"] == ["goal"]
    assert by_key["draft_docs"].basis["evidence_refs"] == ["ev_1"]
    assert by_key["draft_docs"].correlation_key == "draft_docs_weekly"
    assert by_key["publish_docs"].basis["report_refs"] == ["execution"]
    assert by_key["publish_docs"].depends_on_item_keys == ["draft_docs"]
    assert result.get("validation_notes")
    assert "bogus_domain" in result["validation_notes"][0]


async def test_external_proposal_task_is_high_risk_and_approval_mints_single_use_scope(
    db_session,
    monkeypatch,
):
    external_payload = {
        "summary": "Create and publicly publish one verified video.",
        "tasks": [
            {
                "task_key": "render_video",
                "title": "Render video",
                "owner_service_key": "ops",
                "required_capabilities": ["skill.invoke", "file.write"],
                "deliverables": [{
                    "name": "verified_mp4",
                    "kind": "file",
                    "shape": "ArtifactResult",
                    "acceptance": "A verified MP4 exists.",
                    "usage": "Input to publishing.",
                }],
            },
            {
                "task_key": "publish_youtube_public",
                "title": "Publish video to YouTube",
                "owner_service_key": "ops",
                "depends_on_task_keys": ["render_video"],
                "required_capabilities": ["skill.invoke", "external.social"],
                "external_action": {
                    "provider": "youtube",
                    "action": "publish_video",
                    "destination": "studio.youtube.com",
                    "visibility": "public",
                    "intended_channel": "paired_chrome_signed_in_channel",
                    "predecessor_task_key": "render_video",
                    "max_executions": 1,
                    "expires_in_hours": 24,
                },
                "deliverables": [{
                    "name": "publication_receipt",
                    "kind": "value",
                    "shape": "PublishResult",
                    "acceptance": "A Public URL and receipt exist.",
                    "usage": "Input to daily metrics.",
                }],
            },
        ],
    }
    workspace = await _seed_workspace(db_session)
    result = await _run_v2_review(
        db_session,
        monkeypatch,
        workspace,
        payload=external_payload,
    )

    items = await _items(db_session, result["review_id"])
    by_key = {item.item_key: item for item in items}
    assert by_key["render_video"].action_key == "workspace.proposal.task"
    assert by_key["render_video"].risk_level == "low"
    assert by_key["publish_youtube_public"].action_key == "workspace.proposal.task.external"
    assert by_key["publish_youtube_public"].risk_level == "high"

    request = await _proposal_request(db_session, result["proposal_id"])
    assert request is not None
    assert request.action_key == "workspace.proposal.task.external"
    assert request.risk_level == "high"

    actor_id = workspace.entity_id
    await strategist_service.approve_proposal(
        db_session,
        entity_id=workspace.entity_id,
        review_id=result["review_id"],
        actor_id=actor_id,
    )
    await db_session.commit()

    tasks = await _tasks(db_session, workspace)
    authorization = tasks["publish_youtube_public"].details[
        "proposal_external_authorization"
    ]
    assert authorization["workspace_id"] == workspace.id
    assert authorization["review_id"] == result["review_id"]
    assert authorization["proposal_id"] == result["proposal_id"]
    assert authorization["proposal_item_id"] == by_key["publish_youtube_public"].id
    assert authorization["task_id"] == tasks["publish_youtube_public"].id
    assert authorization["predecessor_task_id"] == tasks["render_video"].id
    assert authorization["provider"] == "youtube"
    assert authorization["destination"] == "studio.youtube.com"
    assert authorization["visibility"] == "public"
    assert authorization["max_executions"] == 1
    assert authorization["consumed_at"] is None
    assert authorization["approved_by"] == actor_id


async def test_legacy_auto_approve_does_not_start_external_proposal_task(
    db_session,
    monkeypatch,
):
    external_payload = {
        "summary": "Render and publish one video.",
        "tasks": [
            {
                "task_key": "render_video",
                "title": "Render video",
                "owner_service_key": "ops",
                "deliverables": [{
                    "name": "verified_mp4",
                    "kind": "file",
                    "shape": "ArtifactResult",
                    "acceptance": "A verified MP4 exists.",
                    "usage": "Input to publishing.",
                }],
            },
            {
                "task_key": "publish_youtube_public",
                "title": "Publish video to YouTube",
                "owner_service_key": "ops",
                "depends_on_task_keys": ["render_video"],
                "required_capabilities": ["skill.invoke", "external.social"],
                "external_action": {
                    "provider": "youtube",
                    "action": "publish_video",
                    "destination": "studio.youtube.com",
                    "visibility": "public",
                    "intended_channel": "paired_chrome_signed_in_channel",
                    "predecessor_task_key": "render_video",
                },
                "deliverables": [{
                    "name": "publication_receipt",
                    "kind": "value",
                    "shape": "PublishResult",
                    "acceptance": "A Public URL and receipt exist.",
                    "usage": "Input to daily metrics.",
                }],
            },
        ],
    }
    workspace = await _seed_workspace(
        db_session,
        settings={"strategist": {"auto_approve_proposals": True}},
    )

    result = await _run_v2_review(
        db_session,
        monkeypatch,
        workspace,
        flag=False,
        payload=external_payload,
    )

    assert "approval_outcome" not in result
    assert result["auto_approved"] is False
    tasks = await _tasks(db_session, workspace)
    assert {task.status for task in tasks.values()} == {"proposed"}
    assert "proposal_external_authorization" not in tasks[
        "publish_youtube_public"
    ].details


async def test_legacy_user_approval_mints_review_bound_external_scope(
    db_session,
):
    workspace = await _seed_workspace(db_session)
    review_id = "rv_legacy_external_user_approval"
    render_task = Task(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        title="Render video",
        status="proposed",
        owner_service_key="ops",
        details={
            "strategist_review_id": review_id,
            "strategist_task_key": "render_video",
            "depends_on_task_keys": [],
            "depends_on_task_ids": [],
        },
    )
    publish_task = Task(
        id=generate_ulid(),
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        title="Publish video to YouTube",
        status="proposed",
        owner_service_key="ops",
        details={
            "strategist_review_id": review_id,
            "strategist_task_key": "publish_youtube_public",
            "depends_on_task_keys": ["render_video"],
            "depends_on_task_ids": [render_task.id],
            "external_action": {
                "provider": "youtube",
                "action": "publish_video",
                "destination": "studio.youtube.com",
                "visibility": "public",
                "intended_channel": "paired_chrome_signed_in_channel",
                "predecessor_task_key": "render_video",
                "max_executions": 1,
                "expires_in_hours": 24,
            },
        },
    )
    db_session.add_all([render_task, publish_task])
    await db_session.commit()

    actor_id = workspace.entity_id
    moved = await strategist_service.approve_proposal(
        db_session,
        entity_id=workspace.entity_id,
        review_id=review_id,
        actor_id=actor_id,
    )
    await db_session.commit()

    assert set(moved) == {render_task.id, publish_task.id}
    await db_session.refresh(publish_task)
    authorization = publish_task.details["proposal_external_authorization"]
    assert authorization["review_id"] == review_id
    assert authorization["proposal_id"] == f"legacy:{review_id}"
    assert authorization["proposal_item_id"] == f"legacy:{publish_task.id}"
    assert authorization["task_id"] == publish_task.id
    assert authorization["predecessor_task_id"] == render_task.id
    assert authorization["approved_by"] == actor_id


# ── standing grant → auto-approve ─────────────────────────────────────

async def test_standing_grant_auto_approves_cohort(db_session, monkeypatch):
    from packages.core.governance.service import add_auto_approve_action

    workspace = await _seed_workspace(db_session)
    await add_auto_approve_action(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        action_key="workspace.proposal.task",
        changed_by="operator",
    )
    await db_session.commit()

    result = await _run_v2_review(db_session, monkeypatch, workspace)

    assert result["approval_outcome"] == "allow"
    assert result["auto_approved"] is True
    assert set(result["approved_task_ids"]) == set(result["task_ids"])

    tasks = await _tasks(db_session, workspace)
    assert tasks["draft_docs"].status == "in_progress"
    # Downstream task waits on its dependency (approved-but-not-started).
    assert tasks["publish_docs"].status == "pending"
    batch_id = tasks["draft_docs"].details.get("workspace_work_batch_id")
    assert batch_id

    items = await _items(db_session, result["review_id"])
    assert {i.status for i in items} == {"approved"}
    assert {i.execution_root_id for i in items} == {batch_id}
    assert all(i.decision["decided_by"] == "system" for i in items)
    assert all(i.decided_at is not None for i in items)

    record = await db_session.get(ProposalRecord, result["proposal_id"])
    assert record.status == "resolved"

    # Standing grant means no request needed to be minted; if one was, it
    # must not be left open.
    req = await _proposal_request(db_session, result["proposal_id"])
    assert req is None or req.status == "consumed"

    events = list((await db_session.execute(
        select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == workspace.id,
            WorkspaceEvent.event_type == et.PROPOSAL_ITEM_APPROVED,
        )
    )).scalars().all())
    assert len(events) == 2


# ── no grant → pending request, then chat-card approve ────────────────

async def test_no_grant_leaves_items_proposed_then_card_approve(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    result = await _run_v2_review(db_session, monkeypatch, workspace)

    assert result["approval_outcome"] == "needs_human"
    req = await _proposal_request(db_session, result["proposal_id"])
    assert req is not None
    assert req.status == "pending"
    assert req.action_key == "workspace.proposal.task"
    assert req.resource_kind == "proposal"
    assert req.resource_id == result["proposal_id"]
    assert req.origin_kind == "operation"
    assert result["approval_request_id"] == req.id

    items = await _items(db_session, result["review_id"])
    assert {i.status for i in items} == {"proposed"}
    assert {i.approval_request_id for i in items} == {req.id}
    assert result["auto_approved"] is False

    tasks = await _tasks(db_session, workspace)
    assert {t.status for t in tasks.values()} == {"proposed"}

    # Operator clicks approve on the existing chat card.
    user_id = workspace.entity_id
    moved = await strategist_service.approve_proposal(
        db_session,
        entity_id=workspace.entity_id,
        review_id=result["review_id"],
        actor_id=user_id,
    )
    await db_session.commit()
    assert set(moved) == set(result["task_ids"])

    items = await _items(db_session, result["review_id"])
    assert {i.status for i in items} == {"approved"}
    assert all(i.decision["decided_by"] == user_id for i in items)
    assert all(i.execution_root_id for i in items)

    await db_session.refresh(req)
    assert req.status == "consumed"
    assert req.decided_by_user_id == user_id

    record = await db_session.get(ProposalRecord, result["proposal_id"])
    assert record.status == "resolved"


# ── reject mirrors decision + denies the request ──────────────────────

async def test_reject_records_reason_and_denies_request(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    result = await _run_v2_review(db_session, monkeypatch, workspace)
    req = await _proposal_request(db_session, result["proposal_id"])
    assert req is not None and req.status == "pending"

    user_id = workspace.entity_id
    cancelled = await strategist_service.reject_proposal(
        db_session,
        entity_id=workspace.entity_id,
        review_id=result["review_id"],
        reason="Wrong direction this cycle",
        actor_id=user_id,
    )
    await db_session.commit()
    assert set(cancelled) == set(result["task_ids"])

    items = await _items(db_session, result["review_id"])
    assert {i.status for i in items} == {"rejected"}
    for item in items:
        assert item.decision["reason_code"] == "OTHER"
        assert item.decision["comment"] == "Wrong direction this cycle"
        assert item.decision["decided_by"] == user_id
        assert item.execution_root_id is None

    await db_session.refresh(req)
    assert req.status == "denied"

    record = await db_session.get(ProposalRecord, result["proposal_id"])
    assert record.status == "resolved"

    tasks = await _tasks(db_session, workspace)
    assert {t.status for t in tasks.values()} == {"cancelled"}


# ── legacy boolean auto-approve still works on v2 ─────────────────────

async def test_legacy_boolean_auto_approve_still_works_on_v2(db_session, monkeypatch):
    workspace = await _seed_workspace(
        db_session,
        settings={"strategist": {"auto_approve_proposals": True}},
    )
    result = await _run_v2_review(db_session, monkeypatch, workspace)

    # No standing grant → the resolver wants a human, but the legacy
    # workspace boolean still auto-approves for compat...
    assert result["approval_outcome"] == "needs_human"
    assert result["auto_approved"] is True

    items = await _items(db_session, result["review_id"])
    assert {i.status for i in items} == {"approved"}

    # ...and the minted request is settled, not orphaned.
    req = await _proposal_request(db_session, result["proposal_id"])
    assert req is not None
    assert req.status == "consumed"

    tasks = await _tasks(db_session, workspace)
    assert tasks["draft_docs"].status == "in_progress"
    assert tasks["publish_docs"].status == "pending"


# ── reason_code vocabulary is enforced ────────────────────────────────

async def test_decide_items_rejects_unknown_reason_code(db_session):
    import pytest

    with pytest.raises(ValueError, match="reason_code"):
        await decide_items(
            db_session,
            review_id="rv_bogus",
            task_ids=["t1"],
            decision="rejected",
            reason_code="NOT_A_CODE",
        )
    with pytest.raises(ValueError, match="decision"):
        await decide_items(
            db_session,
            review_id="rv_bogus",
            task_ids=["t1"],
            decision="maybe",
        )


# ── flag off → legacy path untouched ──────────────────────────────────

async def test_flag_off_creates_no_proposal_rows(db_session, monkeypatch):
    workspace = await _seed_workspace(db_session)
    result = await _run_v2_review(db_session, monkeypatch, workspace, flag=False)

    assert not result.get("skipped")
    assert result["task_count"] == 2
    assert result["review_id"].startswith("rv_")
    assert "approval_outcome" not in result
    assert "proposal_id" not in result

    records = list((await db_session.execute(
        select(ProposalRecord).where(ProposalRecord.workspace_id == workspace.id)
    )).scalars().all())
    assert records == []
    item_rows = list((await db_session.execute(
        select(ProposalItemRecord).where(ProposalItemRecord.workspace_id == workspace.id)
    )).scalars().all())
    assert item_rows == []

    reqs = list((await db_session.execute(
        select(HitlRequest).where(HitlRequest.workspace_id == workspace.id)
    )).scalars().all())
    assert reqs == []

    tasks = await _tasks(db_session, workspace)
    assert {t.status for t in tasks.values()} == {"proposed"}
