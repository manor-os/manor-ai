"""Generic workflow action grant service tests (create/validate/revoke/TTL).

Chrome/local-browser-worker enforcement tests that build on these grants live
in ``tests/test_workflow_action_grants_chrome.py`` instead — that file (and
the ``packages.core.ai.mcp.chrome`` module it exercises) is excluded from the
OSS export, so this file stays free of that import and ships in OSS.
"""
from datetime import datetime, timedelta, timezone
import json

import pytest

from packages.core.services.workflow_action_grant_service import (
    WorkflowActionGrantDenied,
    consume_proposal_youtube_publication_grant,
    create_proposal_workflow_action_grant,
    create_workflow_action_grant,
    revoke_workflow_action_grant,
    validate_proposal_youtube_publication_grant,
    validate_workflow_action_grant,
)


async def _create_grant(db_session):
    grant = await create_workflow_action_grant(
        db_session,
        entity_id="ent1",
        workspace_id="ws1",
        workflow_run_id="run1",
        project_id="proj1",
        grant_type="browser_capture",
        scope={
            "approved_plan_version": 3,
            "scene_ids": ["scene-1"],
            "allowed_actions": ["start_tab_recording", "click_element"],
        },
        granted_by="user1",
        ttl_seconds=3600,
    )
    await db_session.commit()
    return grant


@pytest.mark.asyncio
async def test_validate_browser_capture_grant_requires_matching_scope(db_session):
    grant = await _create_grant(db_session)

    validated = await validate_workflow_action_grant(
        db_session,
        grant_id=grant.id,
        entity_id="ent1",
        user_id="user1",
        workspace_id="ws1",
        project_id="proj1",
        grant_type="browser_capture",
        plan_version=3,
        scene_id="scene-1",
        action="start_tab_recording",
    )

    assert validated.id == grant.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entity_id", "ent2"),
        ("user_id", "user2"),
        ("workspace_id", "ws2"),
        ("project_id", "proj2"),
        ("grant_type", "external_publish"),
        ("plan_version", 4),
        ("scene_id", "scene-2"),
        ("action", "fill_or_select"),
    ],
)
async def test_validate_workflow_action_grant_rejects_scope_mismatch(
    db_session,
    field,
    value,
):
    grant = await _create_grant(db_session)
    request = {
        "grant_id": grant.id,
        "entity_id": "ent1",
        "user_id": "user1",
        "workspace_id": "ws1",
        "project_id": "proj1",
        "grant_type": "browser_capture",
        "plan_version": 3,
        "scene_id": "scene-1",
        "action": "start_tab_recording",
    }
    request[field] = value

    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_action_grant(db_session, **request)


@pytest.mark.asyncio
async def test_validate_workflow_action_grant_rejects_expired_grant(db_session):
    grant = await _create_grant(db_session)
    grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_action_grant(
            db_session,
            grant_id=grant.id,
            entity_id="ent1",
            user_id="user1",
            workspace_id="ws1",
            project_id="proj1",
            grant_type="browser_capture",
            plan_version=3,
            scene_id="scene-1",
            action="start_tab_recording",
        )


@pytest.mark.asyncio
async def test_revoke_workflow_action_grant_is_scoped_and_blocks_validation(db_session):
    grant = await _create_grant(db_session)

    with pytest.raises(WorkflowActionGrantDenied):
        await revoke_workflow_action_grant(
            db_session,
            grant_id=grant.id,
            entity_id="ent2",
            workspace_id="ws1",
        )

    revoked = await revoke_workflow_action_grant(
        db_session,
        grant_id=grant.id,
        entity_id="ent1",
        workspace_id="ws1",
    )
    await db_session.commit()

    assert revoked.revoked_at is not None
    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_action_grant(
            db_session,
            grant_id=grant.id,
            entity_id="ent1",
            user_id="user1",
            workspace_id="ws1",
            project_id="proj1",
            grant_type="browser_capture",
            plan_version=3,
            scene_id="scene-1",
            action="start_tab_recording",
        )


@pytest.mark.asyncio
async def test_workflow_action_grant_ttl_is_clamped_to_one_day(db_session):
    before = datetime.now(timezone.utc)
    grant = await create_workflow_action_grant(
        db_session,
        entity_id="ent1",
        workspace_id="ws1",
        workflow_run_id="run1",
        project_id="proj1",
        grant_type="browser_capture",
        scope={"approved_plan_version": 1, "scene_ids": [], "allowed_actions": []},
        granted_by="user1",
        ttl_seconds=7 * 86400,
    )
    after = datetime.now(timezone.utc)

    assert grant.expires_at > before + timedelta(hours=23, minutes=59)
    assert grant.expires_at <= after + timedelta(days=1)


def _youtube_publication_request(kind: str) -> dict:
    common = {
        "provider": "chrome",
        "provider_approval_id": f"approval-{kind}",
        "url": "https://studio.youtube.com/channel/demo/videos/upload",
    }
    if kind == "upload":
        return {
            **common,
            "confirmation_mode": "preapproval_allowed",
            "policy_category": "file_upload",
            "retry_tool": "mcp__chrome__upload",
            "retry_arguments": {"files": ["/prepared/video.mp4"]},
        }
    return {
        **common,
        "confirmation_mode": "always_action_time",
        "policy_category": "representational_communication",
        "retry_tool": "mcp__chrome__click_element",
        "retry_arguments": {"label": "Publish"},
        "target_label": "Publish",
    }


async def _create_proposal_publication_grant(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition, WorkflowRun

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    user_id = generate_ulid()
    declaration = {
        "kind": "youtube_publication_v1",
        "action_key": "workspace.proposal.workflow_run.external",
        "when": {"input_key": "youtube_visibility", "equals": "public"},
        "destination": "studio.youtube.com",
        "upload_step_id": "upload_youtube_video",
        "publish_step_id": "set_youtube_visibility",
        "ttl_seconds": 3600,
    }
    workflow = WorkflowDefinition(
        entity_id=entity_id,
        workspace_id=workspace_id,
        created_by=user_id,
        name="proposal-publication",
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
    binding = WorkflowBinding(
        entity_id=entity_id,
        workflow_id=workflow.id,
        workspace_id=workspace_id,
        trigger_type="manual",
        enabled=True,
        status="active",
        revision=4,
        config={"proposal_authorization": declaration},
    )
    record = ProposalRecord(
        entity_id=entity_id,
        workspace_id=workspace_id,
        review_id=generate_ulid(),
        summary="Publish one verified video",
        status="resolved",
    )
    db_session.add_all([binding, record])
    await db_session.flush()
    item = ProposalItemRecord(
        proposal_id=record.id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        item_key="wr_publish_video",
        kind="workflow_run",
        payload={"inputs": {"youtube_visibility": "public"}},
        risk_level="high",
        action_key="workspace.proposal.workflow_run.external",
        status="approved",
    )
    db_session.add(item)
    await db_session.flush()
    item.payload = {
        **dict(item.payload or {}),
        "_proposal_authorization_binding": {
            "binding_id": binding.id,
            "workflow_id": workflow.id,
            "revision": binding.revision,
            "declaration": declaration,
        },
    }
    run_id = generate_ulid()
    run = WorkflowRun(
        id=run_id,
        workflow_id=workflow.id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        binding_id=binding.id,
        trigger_source="proposal",
        status="running",
        variables={},
        step_results={},
        trigger_data={"_proposal_context": {"proposal_item_id": item.id}},
        definition_snapshot={},
        execution_trace=[],
        started_by=user_id,
        lineage_root_run_id=run_id,
        lineage_is_legacy=False,
        attempt_number=1,
    )
    db_session.add(run)
    await db_session.flush()
    item.execution_root_id = run.id
    grant = await create_proposal_workflow_action_grant(
        db_session,
        item=item,
        run=run,
        binding=binding,
        declaration=declaration,
        granted_by=user_id,
    )
    item.status = "executing"
    await db_session.commit()
    return grant, item, run, binding, declaration, user_id


@pytest.mark.asyncio
async def test_proposal_publication_grant_binds_exact_lineage_step_and_binding(db_session):
    grant, item, run, binding, declaration, user_id = (
        await _create_proposal_publication_grant(db_session)
    )

    validated, request_kind = await validate_proposal_youtube_publication_grant(
        db_session,
        grant_id=grant.id,
        entity_id=run.entity_id,
        user_id=user_id,
        workspace_id=run.workspace_id,
        workflow_run_id=run.id,
        workflow_lineage_root_run_id=run.id,
        workflow_step_id=declaration["upload_step_id"],
        provider_request=_youtube_publication_request("upload"),
    )

    assert validated.id == grant.id
    assert request_kind == "upload_transfer"
    assert validated.proposal_item_id == item.id
    assert validated.workflow_lineage_root_run_id == run.id

    for field, value in (
        ("workflow_lineage_root_run_id", "foreign-lineage"),
        ("workflow_step_id", declaration["publish_step_id"]),
    ):
        kwargs = {
            "grant_id": grant.id,
            "entity_id": run.entity_id,
            "user_id": user_id,
            "workspace_id": run.workspace_id,
            "workflow_run_id": run.id,
            "workflow_lineage_root_run_id": run.id,
            "workflow_step_id": declaration["upload_step_id"],
            "provider_request": _youtube_publication_request("upload"),
        }
        kwargs[field] = value
        with pytest.raises(WorkflowActionGrantDenied):
            await validate_proposal_youtube_publication_grant(db_session, **kwargs)

    binding.revision += 1
    await db_session.commit()
    with pytest.raises(WorkflowActionGrantDenied):
        await validate_proposal_youtube_publication_grant(
            db_session,
            grant_id=grant.id,
            entity_id=run.entity_id,
            user_id=user_id,
            workspace_id=run.workspace_id,
            workflow_run_id=run.id,
            workflow_lineage_root_run_id=run.id,
            workflow_step_id=declaration["upload_step_id"],
            provider_request=_youtube_publication_request("upload"),
        )


@pytest.mark.asyncio
async def test_proposal_publication_grant_final_publish_is_single_use(db_session):
    grant, _item, run, _binding, declaration, user_id = (
        await _create_proposal_publication_grant(db_session)
    )
    kwargs = {
        "grant_id": grant.id,
        "entity_id": run.entity_id,
        "user_id": user_id,
        "workspace_id": run.workspace_id,
        "workflow_run_id": run.id,
        "workflow_lineage_root_run_id": run.id,
        "workflow_step_id": declaration["publish_step_id"],
        "provider_request": _youtube_publication_request("publish"),
    }

    consumed = await consume_proposal_youtube_publication_grant(
        db_session,
        **kwargs,
    )
    await db_session.commit()

    assert consumed.consumed_at is not None
    assert consumed.consumed_by_run_id == run.id
    with pytest.raises(WorkflowActionGrantDenied):
        await consume_proposal_youtube_publication_grant(db_session, **kwargs)


@pytest.mark.asyncio
async def test_proposal_publication_runtime_confirms_only_declared_publish_step(
    db_session,
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.runtime.approval_service import (
        runtime_auto_confirm_provider_approval,
    )

    grant, _item, run, _binding, declaration, user_id = (
        await _create_proposal_publication_grant(db_session)
    )

    class SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(database, "async_session", lambda: SessionContext())
    url = "https://studio.youtube.com/channel/demo/videos/upload"
    blocked = json.dumps({
        "status": "approval_required",
        "approval_required": True,
        "provider": "chrome",
        "approvalId": "publish-approval",
        "confirmation_mode": "always_action_time",
        "policy_category": "representational_communication",
        "target_label": "Publish",
        "target_role": "button",
        "url": url,
        "retry_action": {
            "name": "mcp__chrome__click_element",
            "arguments": {"url": url, "label": "Publish", "role": "button"},
        },
    })
    calls: list[str] = []

    async def execute(name: str, args: dict) -> str:
        calls.append(name)
        if name == "mcp__chrome__confirm_action":
            return json.dumps({
                "ok": True,
                "status": "approved",
                "approvalId": args["approvalId"],
                "approvalToken": "single-use-token",
            })
        return json.dumps({
            "ok": True,
            "status": "published",
            "post_action_page_state": {"state_verified": True},
        })

    common = {
        "tool_name": "mcp__chrome__click_element",
        "arguments": {"label": "Publish"},
        "result": blocked,
        "execute": execute,
        "entity_id": run.entity_id,
        "user_id": user_id,
        "workspace_id": run.workspace_id,
        "workflow_run_id": run.id,
        "workflow_lineage_root_run_id": run.id,
        "workflow_action_grant_id": grant.id,
    }
    mismatched = await runtime_auto_confirm_provider_approval(
        **common,
        workflow_step_id=declaration["upload_step_id"],
    )
    assert mismatched == blocked
    assert calls == []

    published = await runtime_auto_confirm_provider_approval(
        **common,
        workflow_step_id=declaration["publish_step_id"],
    )
    assert json.loads(published)["status"] == "published"
    assert calls == [
        "mcp__chrome__confirm_action",
        "mcp__chrome__click_element",
    ]
    await db_session.refresh(grant)
    assert grant.consumed_at is not None
