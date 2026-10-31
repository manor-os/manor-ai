"""Generic workflow action grant service tests (create/validate/revoke/TTL).

Chrome/local-browser-worker enforcement tests that build on these grants live
in ``tests/test_workflow_action_grants_chrome.py`` instead — that file (and
the ``packages.core.ai.mcp.chrome`` module it exercises) is excluded from the
OSS export, so this file stays free of that import and ships in OSS.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from tests.approval_settings_test_utils import (
    action_rule,
    initialize_test_approval_owners,
    set_workspace_test_approval,
)

from packages.core.services.workflow_action_grant_service import (
    WorkflowActionGrantDenied,
    consume_proposal_youtube_publication_grant,
    consume_workflow_wait_youtube_publication_grant,
    create_proposal_workflow_action_grant,
    create_workflow_action_grant,
    create_workflow_execution_youtube_publication_grant,
    create_workflow_wait_youtube_publication_grant,
    consume_workflow_execution_youtube_publication_grant,
    record_workflow_wait_youtube_upload_preparation,
    record_workflow_execution_youtube_upload_preparation,
    revoke_workflow_action_grant,
    validate_proposal_youtube_publication_grant,
    validate_workflow_wait_youtube_publication_grant,
    validate_workflow_wait_youtube_publication_step,
    validate_workflow_wait_youtube_upload_preparation,
    validate_workflow_execution_youtube_publication_grant,
    validate_publication_workflow_browser_action,
    validate_workflow_execution_youtube_upload_preparation,
    validate_workflow_action_grant,
    workflow_wait_youtube_publication_authorization,
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
    label = "Save" if kind == "save" else "Publish"
    return {
        **common,
        "confirmation_mode": "always_action_time",
        "policy_category": "representational_communication",
        "retry_tool": "mcp__chrome__click_element",
        "retry_arguments": {"label": label},
        "target_label": label,
    }


async def _create_workflow_wait_publication_grant(
    db_session,
    *,
    visibility: str = "public",
    tmp_path,
    monkeypatch,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document
    from packages.core.models.user import User, UserMembership
    from packages.core.models.workflow import (
        WorkflowDefinition,
        WorkflowProject,
        WorkflowRun,
    )
    from packages.core.models.workspace import Workspace
    from packages.core.services.workflow_run_trace import (
        build_execution_snapshot,
        workflow_definition_fingerprint,
    )

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    user_id = generate_ulid()
    digest = "a" * 64
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Stickman publication runtime test",
        operating_model={},
    )
    user = User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    )
    document = Document(
        entity_id=entity_id,
        name="stickman-final.mp4",
        fs_path="Knowledge/Stickman/videos/stickman-final.mp4",
        file_type="mp4",
        mime_type="video/mp4",
        vector_status="ready",
        source="agent",
        metadata_={"sha256": digest},
        created_by=user_id,
    )
    project = WorkflowProject(
        entity_id=entity_id,
        workspace_id=workspace_id,
        project_type="stickman_video",
        project_key="stickman-video-001",
        current_stage="video_ready",
        state={},
        created_by=user_id,
    )
    root_workflow = WorkflowDefinition(
        entity_id=entity_id,
        workspace_id=workspace_id,
        created_by=user_id,
        name="Stickman publish wrapper",
        variables={},
        steps=[
            {"id": "start", "type": "trigger", "next": ["approve_video"]},
            {
                "id": "approve_video",
                "type": "wait",
                "config": {"wait_type": "approval"},
                "next": ["publish"],
            },
            {"id": "publish", "type": "subworkflow", "config": {}},
        ],
        status="active",
        is_active=True,
    )
    executor_workflow = WorkflowDefinition(
        entity_id=entity_id,
        workspace_id=workspace_id,
        created_by=user_id,
        name="Stickman YouTube executor",
        variables={},
        steps=[
            {"id": "start", "type": "trigger", "next": ["upload_youtube_video"]},
            {"id": "upload_youtube_video", "type": "agent", "next": ["set_youtube_visibility"]},
            {"id": "set_youtube_visibility", "type": "agent", "next": []},
        ],
        status="active",
        is_active=True,
    )
    db_session.add_all(
        [
            workspace,
            user,
            UserMembership(user_id=user_id, entity_id=entity_id, role="owner", status="active"),
            document,
            project,
            root_workflow,
            executor_workflow,
        ]
    )
    await db_session.flush()
    await initialize_test_approval_owners(db_session, workspace, user_id=user_id)
    artifact = {
        "workflow_project_id": project.id,
        "document_id": document.id,
        "fs_path": document.fs_path,
        "filename": "stickman-final.mp4",
        "sha256": digest,
    }
    publication = {
        "title": "Why Your Brain Loves Tiny Wins",
        "description": "A verified Stickman explainer.",
        "audience": "not_made_for_kids",
        "visibility": visibility,
    }
    project.state = {
        "artifact": {key: value for key, value in artifact.items() if key != "workflow_project_id"},
        "publication": {
            **publication,
            "review_status": "approved",
            "revision_notes": "",
        },
    }
    artifact_path = tmp_path / document.fs_path
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"stickman-final-video")
    document.metadata_ = {"sha256": hashlib.sha256(b"stickman-final-video").hexdigest()}
    artifact["sha256"] = document.metadata_["sha256"]
    project.state["artifact"]["sha256"] = document.metadata_["sha256"]
    monkeypatch.setattr(
        "packages.core.services.entity_fs.get_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    monkeypatch.setattr(
        "packages.core.services.entity_fs.canonical_entity_root",
        lambda _entity_id: str(tmp_path),
    )
    root_snapshot = build_execution_snapshot(root_workflow)
    root_run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=root_workflow.id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        status="running",
        current_step_id="publish",
        variables={},
        step_results={
            "approve_video": {
                "status": "completed",
                "decision": "approve",
                "approved": True,
                "approved_by": user_id,
                "approved_at": datetime.now(timezone.utc).isoformat(),
            }
        },
        trigger_data={},
        execution_snapshot=root_snapshot,
        execution_trace=[],
        started_by=user_id,
        lineage_root_run_id=None,
        lineage_is_legacy=False,
        attempt_number=1,
    )
    root_run.lineage_root_run_id = root_run.id
    executor_snapshot = build_execution_snapshot(executor_workflow)
    executor_run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=executor_workflow.id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        status="running",
        current_step_id="upload_youtube_video",
        variables={},
        step_results={},
        trigger_data={
            "parent_run_id": root_run.id,
            "parent_step_id": "publish",
            "_workflow_definition_fingerprint": workflow_definition_fingerprint(executor_workflow),
            "_workflow_runtime_context": {},
        },
        execution_snapshot=executor_snapshot,
        execution_trace=[],
        started_by=user_id,
        lineage_root_run_id=None,
        lineage_is_legacy=False,
        attempt_number=1,
    )
    executor_run.lineage_root_run_id = executor_run.id
    db_session.add_all([root_run, executor_run])
    await db_session.flush()
    declaration = {
        "kind": "youtube_publication_v1",
        "source": "workflow_wait",
        "approved_wait_step_id": "approve_video",
        "destination": "studio.youtube.com",
        "upload_step_id": "upload_youtube_video",
        "publish_step_id": "set_youtube_visibility",
        "artifact": artifact,
        "publication": publication,
        "ttl_seconds": 3600,
    }
    grant = await create_workflow_wait_youtube_publication_grant(
        db_session,
        root_run=root_run,
        executor_run=executor_run,
        executor_workflow=executor_workflow,
        declaration=declaration,
    )
    await db_session.commit()
    return {
        "grant": grant,
        "root_run": root_run,
        "executor_run": executor_run,
        "executor_workflow": executor_workflow,
        "document": document,
        "project": project,
        "declaration": declaration,
        "user_id": user_id,
    }


async def _record_workflow_wait_upload_preparation(
    db_session,
    context,
    local_path,
):
    executor_run = context["executor_run"]
    declaration = context["declaration"]
    await record_workflow_wait_youtube_upload_preparation(
        db_session,
        grant_id=context["grant"].id,
        entity_id=executor_run.entity_id,
        user_id=context["user_id"],
        workspace_id=executor_run.workspace_id,
        workflow_run_id=executor_run.id,
        workflow_lineage_root_run_id=executor_run.id,
        workflow_step_id=declaration["upload_step_id"],
        source=declaration["artifact"]["fs_path"],
        local_path=local_path,
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_workflow_wait_publication_grant_binds_root_child_and_artifact(
    db_session,
    tmp_path,
    monkeypatch,
):
    context = await _create_workflow_wait_publication_grant(
        db_session,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    grant = context["grant"]
    root_run = context["root_run"]
    executor_run = context["executor_run"]
    declaration = context["declaration"]
    prepared_path = f"/workspace/Knowledge/{declaration['artifact']['filename']}"
    await _record_workflow_wait_upload_preparation(
        db_session,
        context,
        prepared_path,
    )
    validated, request_kind = await validate_workflow_wait_youtube_publication_grant(
        db_session,
        grant_id=grant.id,
        entity_id=executor_run.entity_id,
        user_id=context["user_id"],
        workspace_id=executor_run.workspace_id,
        workflow_run_id=executor_run.id,
        workflow_lineage_root_run_id=executor_run.id,
        workflow_step_id=declaration["upload_step_id"],
        provider_request={
            **_youtube_publication_request("upload"),
            "retry_arguments": {
                "files": [f"/workspace/Knowledge/{declaration['artifact']['filename']}"],
            },
        },
    )

    assert validated.id == grant.id
    assert request_kind == "upload_transfer"

    source_arguments = {
        "source": (f"/api/v1/fs/{executor_run.entity_id}/{declaration['artifact']['fs_path']}"),
    }
    assert grant.scope["source"] == "workflow_wait"
    assert grant.scope["approval_root_run_id"] == root_run.id
    assert grant.scope["executor_run_id"] == executor_run.id
    assert executor_run.trigger_data["_workflow_runtime_context"]["workflow_action_grant_id"] == grant.id

    validated, request_kind = await validate_workflow_wait_youtube_publication_grant(
        db_session,
        grant_id=grant.id,
        entity_id=executor_run.entity_id,
        user_id=context["user_id"],
        workspace_id=executor_run.workspace_id,
        workflow_run_id=executor_run.id,
        workflow_lineage_root_run_id=executor_run.id,
        workflow_step_id=declaration["upload_step_id"],
        provider_request={
            **_youtube_publication_request("upload"),
            "retry_arguments": {"document_id": declaration["artifact"]["document_id"]},
        },
    )
    assert validated.id == grant.id
    assert request_kind == "upload_transfer"

    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_wait_youtube_publication_grant(
            db_session,
            grant_id=grant.id,
            entity_id=executor_run.entity_id,
            user_id=context["user_id"],
            workspace_id=executor_run.workspace_id,
            workflow_run_id=executor_run.id,
            workflow_lineage_root_run_id=executor_run.id,
            workflow_step_id=declaration["upload_step_id"],
            provider_request={
                **_youtube_publication_request("upload"),
                "retry_arguments": {
                    "document_id": declaration["artifact"]["document_id"],
                    "files": [prepared_path],
                },
            },
        )

    for rejected_source_arguments in (
        {"source": f"/api/v1/fs/foreign/{declaration['artifact']['fs_path']}"},
        {"source": "Knowledge/Stickman/videos/other.mp4"},
        {"source": "http://["},
        {"source": "https://example.test/approved.mp4"},
        {
            "source": source_arguments["source"],
            "document_id": declaration["artifact"]["document_id"],
        },
        {
            "source": source_arguments["source"],
            "sources": [source_arguments["source"]],
        },
        {"sources": [source_arguments["source"], source_arguments["source"]]},
        {"sources": [f"/api/v1/fs/foreign/{declaration['artifact']['fs_path']}"]},
    ):
        with pytest.raises(WorkflowActionGrantDenied):
            await validate_workflow_wait_youtube_publication_grant(
                db_session,
                grant_id=grant.id,
                entity_id=executor_run.entity_id,
                user_id=context["user_id"],
                workspace_id=executor_run.workspace_id,
                workflow_run_id=executor_run.id,
                workflow_lineage_root_run_id=executor_run.id,
                workflow_step_id=declaration["upload_step_id"],
                provider_request={
                    **_youtube_publication_request("upload"),
                    "retry_arguments": rejected_source_arguments,
                },
            )

    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_wait_youtube_publication_grant(
            db_session,
            grant_id=grant.id,
            entity_id=executor_run.entity_id,
            user_id=context["user_id"],
            workspace_id=executor_run.workspace_id,
            workflow_run_id=executor_run.id,
            workflow_lineage_root_run_id=executor_run.id,
            workflow_step_id=declaration["upload_step_id"],
            provider_request={
                **_youtube_publication_request("upload"),
                "retry_arguments": {"files": ["/workspace/Knowledge/other.mp4"]},
            },
        )

    artifact_path = tmp_path / declaration["artifact"]["fs_path"]
    artifact_path.write_bytes(b"changed-video")
    await db_session.commit()
    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_wait_youtube_publication_grant(
            db_session,
            grant_id=grant.id,
            entity_id=executor_run.entity_id,
            user_id=context["user_id"],
            workspace_id=executor_run.workspace_id,
            workflow_run_id=executor_run.id,
            workflow_lineage_root_run_id=executor_run.id,
            workflow_step_id=declaration["upload_step_id"],
            provider_request={
                **_youtube_publication_request("upload"),
                "retry_arguments": {
                    "files": [prepared_path],
                },
            },
        )
    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_wait_youtube_publication_grant(
            db_session,
            grant_id=grant.id,
            entity_id=executor_run.entity_id,
            user_id=context["user_id"],
            workspace_id=executor_run.workspace_id,
            workflow_run_id=executor_run.id,
            workflow_lineage_root_run_id=executor_run.id,
            workflow_step_id=declaration["upload_step_id"],
            provider_request={
                **_youtube_publication_request("upload"),
                "retry_arguments": source_arguments,
            },
        )


@pytest.mark.asyncio
async def test_workflow_wait_publication_grant_allows_canonical_executor_retry(
    db_session,
    tmp_path,
    monkeypatch,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.workflow import WorkflowRun

    context = await _create_workflow_wait_publication_grant(
        db_session,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    grant = context["grant"]
    root_run = context["root_run"]
    executor_run = context["executor_run"]
    declaration = context["declaration"]

    executor_run.status = "failed"
    executor_run.current_step_id = "youtube_visibility_blocked"
    root_run.status = "failed"
    root_run.current_step_id = "publish"
    root_run.step_results = {
        **root_run.step_results,
        "publish": {
            "status": "failed",
            "subrun_id": executor_run.id,
            "error": "subworkflow failed",
        },
    }
    retry_run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=executor_run.workflow_id,
        entity_id=executor_run.entity_id,
        workspace_id=executor_run.workspace_id,
        status="running",
        current_step_id=declaration["publish_step_id"],
        retry_of_run_id=executor_run.id,
        retry_from_step_id=declaration["publish_step_id"],
        lineage_root_run_id=executor_run.id,
        lineage_is_legacy=False,
        attempt_number=2,
        variables={},
        step_results={},
        trigger_data={
            "_workflow_definition_fingerprint": (executor_run.trigger_data["_workflow_definition_fingerprint"]),
            "_workflow_runtime_context": {
                "workflow_action_grant_id": grant.id,
            },
        },
        execution_snapshot=executor_run.execution_snapshot,
        execution_trace=[],
        started_by=executor_run.started_by,
    )
    db_session.add(retry_run)
    await db_session.commit()

    validated = await validate_workflow_wait_youtube_publication_step(
        db_session,
        grant_id=grant.id,
        entity_id=retry_run.entity_id,
        user_id=context["user_id"],
        workspace_id=retry_run.workspace_id,
        workflow_run_id=retry_run.id,
        workflow_lineage_root_run_id=executor_run.id,
        workflow_step_id=declaration["publish_step_id"],
        browser_action="click_element",
        browser_arguments={"label": "Publish"},
    )

    assert validated.id == grant.id


@pytest.mark.asyncio
async def test_workflow_wait_publication_grant_accepts_exact_paths_upload_alias(
    db_session,
    tmp_path,
    monkeypatch,
):
    context = await _create_workflow_wait_publication_grant(
        db_session,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    grant = context["grant"]
    executor_run = context["executor_run"]
    declaration = context["declaration"]
    upload_arguments = {"paths": [f"/workspace/Knowledge/{declaration['artifact']['filename']}"]}
    await _record_workflow_wait_upload_preparation(
        db_session,
        context,
        upload_arguments["paths"][0],
    )
    scope = {
        "entity_id": executor_run.entity_id,
        "user_id": context["user_id"],
        "workspace_id": executor_run.workspace_id,
        "workflow_run_id": executor_run.id,
        "workflow_lineage_root_run_id": executor_run.id,
        "workflow_step_id": declaration["upload_step_id"],
    }

    dispatched = await validate_workflow_wait_youtube_publication_step(
        db_session,
        grant_id=grant.id,
        browser_action="upload",
        browser_arguments=upload_arguments,
        **scope,
    )
    confirmed, request_kind = await validate_workflow_wait_youtube_publication_grant(
        db_session,
        grant_id=grant.id,
        provider_request={
            **_youtube_publication_request("upload"),
            "retry_arguments": upload_arguments,
        },
        **scope,
    )

    assert dispatched.id == grant.id
    assert confirmed.id == grant.id
    assert request_kind == "upload_transfer"


@pytest.mark.asyncio
async def test_workflow_wait_upload_requires_artifact_bound_preparation_receipt(
    db_session,
    tmp_path,
    monkeypatch,
):
    context = await _create_workflow_wait_publication_grant(
        db_session,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    grant = context["grant"]
    executor_run = context["executor_run"]
    artifact = context["declaration"]["artifact"]
    scope = {
        "grant_id": grant.id,
        "entity_id": executor_run.entity_id,
        "user_id": context["user_id"],
        "workspace_id": executor_run.workspace_id,
        "workflow_run_id": executor_run.id,
        "workflow_lineage_root_run_id": executor_run.id,
        "workflow_step_id": context["declaration"]["upload_step_id"],
    }
    prepared_path = f"/managed/uploads/{artifact['filename']}"

    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_wait_youtube_publication_step(
            db_session,
            browser_action="upload",
            browser_arguments={"files": [prepared_path]},
            **scope,
        )

    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_wait_youtube_upload_preparation(
            db_session,
            source="Knowledge/Stickman/videos/other.mp4",
            **scope,
        )

    approved_artifact = await validate_workflow_wait_youtube_upload_preparation(
        db_session,
        source=artifact["fs_path"],
        **scope,
    )
    assert approved_artifact == artifact

    await record_workflow_wait_youtube_upload_preparation(
        db_session,
        source=artifact["fs_path"],
        local_path=prepared_path,
        **scope,
    )
    await db_session.commit()

    same_grant = await create_workflow_wait_youtube_publication_grant(
        db_session,
        root_run=context["root_run"],
        executor_run=executor_run,
        executor_workflow=context["executor_workflow"],
        declaration=context["declaration"],
    )
    assert same_grant.id == grant.id

    await validate_workflow_wait_youtube_publication_step(
        db_session,
        browser_action="upload",
        browser_arguments={"files": [prepared_path]},
        **scope,
    )
    for rejected_path in (
        f"/different/location/{artifact['filename']}",
        prepared_path.replace(artifact["filename"], "other.mp4"),
    ):
        with pytest.raises(WorkflowActionGrantDenied):
            await validate_workflow_wait_youtube_publication_step(
                db_session,
                browser_action="upload",
                browser_arguments={"files": [rejected_path]},
                **scope,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("current_stage", "publishing"),
        ("review_status", "pending"),
        ("title", "A different approved title"),
        ("description", "A different approved description."),
        ("audience", "made_for_kids"),
        ("visibility", "private"),
    ],
)
async def test_workflow_wait_publication_grant_rejects_changed_project_snapshot(
    db_session,
    tmp_path,
    monkeypatch,
    changed_field,
    changed_value,
):
    context = await _create_workflow_wait_publication_grant(
        db_session,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    project = context["project"]
    if changed_field == "current_stage":
        project.current_stage = changed_value
    else:
        project.state = {
            **project.state,
            "publication": {
                **project.state["publication"],
                changed_field: changed_value,
            },
        }
    await db_session.commit()

    with pytest.raises(WorkflowActionGrantDenied):
        await create_workflow_wait_youtube_publication_grant(
            db_session,
            root_run=context["root_run"],
            executor_run=context["executor_run"],
            executor_workflow=context["executor_workflow"],
            declaration=context["declaration"],
        )

    declaration = context["declaration"]
    executor_run = context["executor_run"]
    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_wait_youtube_publication_grant(
            db_session,
            grant_id=context["grant"].id,
            entity_id=executor_run.entity_id,
            user_id=context["user_id"],
            workspace_id=executor_run.workspace_id,
            workflow_run_id=executor_run.id,
            workflow_lineage_root_run_id=executor_run.id,
            workflow_step_id=declaration["upload_step_id"],
            provider_request={
                **_youtube_publication_request("upload"),
                "retry_arguments": {
                    "files": [f"/workspace/Knowledge/{declaration['artifact']['filename']}"],
                },
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("visibility", "allowed_final", "denied_final"),
    [
        ("public", "publish", "save"),
        ("unlisted", "save", "publish"),
        ("private", "save", "publish"),
    ],
)
async def test_workflow_wait_publication_grant_consumes_only_exact_final_action(
    db_session,
    tmp_path,
    monkeypatch,
    visibility,
    allowed_final,
    denied_final,
):
    context = await _create_workflow_wait_publication_grant(
        db_session,
        visibility=visibility,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    grant = context["grant"]
    executor_run = context["executor_run"]
    declaration = context["declaration"]
    executor_run.current_step_id = declaration["publish_step_id"]
    await db_session.commit()
    kwargs = {
        "grant_id": grant.id,
        "entity_id": executor_run.entity_id,
        "user_id": context["user_id"],
        "workspace_id": executor_run.workspace_id,
        "workflow_run_id": executor_run.id,
        "workflow_lineage_root_run_id": executor_run.id,
        "workflow_step_id": declaration["publish_step_id"],
    }

    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_wait_youtube_publication_grant(
            db_session,
            **kwargs,
            provider_request=_youtube_publication_request(denied_final),
        )

    consumed = await consume_workflow_wait_youtube_publication_grant(
        db_session,
        **kwargs,
        provider_request=_youtube_publication_request(allowed_final),
    )
    await db_session.commit()
    assert consumed.consumed_by_run_id == executor_run.id
    with pytest.raises(WorkflowActionGrantDenied):
        await consume_workflow_wait_youtube_publication_grant(
            db_session,
            **kwargs,
            provider_request=_youtube_publication_request(allowed_final),
        )


@pytest.mark.asyncio
async def test_workflow_wait_runtime_unwraps_mcp_error_and_consumes_private_save(
    db_session,
    monkeypatch,
    tmp_path,
):
    from packages.core import database
    from packages.core.ai.runtime import (
        AIRuntimeRequest,
        ChatSurface,
        RuntimeResolver,
        tool_registry,
    )

    context = await _create_workflow_wait_publication_grant(
        db_session,
        visibility="private",
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    grant = context["grant"]
    executor_run = context["executor_run"]
    declaration = context["declaration"]
    executor_run.current_step_id = declaration["publish_step_id"]
    await db_session.commit()

    class SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(database, "async_session", lambda: SessionContext())
    request = _youtube_publication_request("save")
    approval_required = json.dumps(
        {
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": request["provider_approval_id"],
            "confirmation_mode": request["confirmation_mode"],
            "policy_category": request["policy_category"],
            "target_label": request["target_label"],
            "url": request["url"],
            "retry_action": {
                "name": request["retry_tool"],
                "arguments": request["retry_arguments"],
            },
        }
    )

    blocked = json.dumps(
        {
            "server": "chrome",
            "tool": "click_element",
            "error": "tool_error",
            "detail": approval_required,
        }
    )
    calls: list[tuple[str, dict]] = []

    class Registry:
        async def execute(self, name: str, args: dict, **_kwargs) -> str:
            calls.append((name, dict(args)))
            if len(calls) == 1:
                return blocked
            if name == "mcp__chrome__confirm_action":
                return json.dumps(
                    {
                        "ok": True,
                        "status": "approved",
                        "approvalId": args["approvalId"],
                        "approvalToken": "single-use-token",
                    }
                )
            return json.dumps(
                {
                    "ok": True,
                    "status": "saved",
                    "post_action_page_state": {"state_verified": True},
                }
            )

    monkeypatch.setattr(
        tool_registry,
        "runtime_ensure_tool_registry_initialized",
        lambda: Registry(),
    )
    runtime_envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.WORKFLOW_AGENT_STEP,
            entity_id=executor_run.entity_id,
            user_id=context["user_id"],
            workspace_id=executor_run.workspace_id,
        ),
        allowed_tool_names={"mcp__chrome__click_element"},
    )
    result = await tool_registry.runtime_execute_tool(
        "mcp__chrome__click_element",
        {
            "label": "Save",
            "_workflow_run_id_from_context": executor_run.id,
            "_workflow_lineage_root_run_id_from_context": executor_run.id,
            "_workflow_action_grant_id_from_context": grant.id,
            "_workflow_step_id_from_context": declaration["publish_step_id"],
            "_runtime_run_id_from_context": f"workflow:{executor_run.id}",
            "_runtime_tool_call_id_from_context": f"node:{declaration['publish_step_id']}",
        },
        entity_id=executor_run.entity_id,
        user_id=context["user_id"],
        workspace_id=executor_run.workspace_id,
        runtime_envelope=runtime_envelope,
    )

    assert json.loads(result)["status"] == "saved"
    assert [name for name, _args in calls] == [
        "mcp__chrome__click_element",
        "mcp__chrome__confirm_action",
        "mcp__chrome__click_element",
    ]
    retry_arguments = calls[-1][1]
    assert retry_arguments["label"] == "Save"
    assert retry_arguments["approvalToken"] == "single-use-token"
    assert retry_arguments["_workflow_run_id_from_context"] == executor_run.id
    assert retry_arguments["_workflow_lineage_root_run_id_from_context"] == executor_run.id
    assert retry_arguments["_workflow_action_grant_id_from_context"] == grant.id
    assert retry_arguments["_workflow_step_id_from_context"] == declaration["publish_step_id"]
    await db_session.refresh(grant)
    assert grant.consumed_at is not None


async def _create_proposal_publication_grant(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
    from packages.core.models.user import User, UserMembership
    from packages.core.models.workspace import Workspace
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition, WorkflowRun

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    user_id = generate_ulid()
    workspace = Workspace(
        id=workspace_id, entity_id=entity_id, name="Proposal publication approval scope", status="active"
    )
    db_session.add_all(
        [
            workspace,
            User(
                id=user_id,
                entity_id=entity_id,
                email=f"{user_id}@example.test",
                password_hash="fixture",
                role="owner",
                status="active",
            ),
            UserMembership(user_id=user_id, entity_id=entity_id, role="owner", status="active"),
        ]
    )
    await db_session.flush()
    await initialize_test_approval_owners(db_session, workspace, user_id=user_id)
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
    grant, item, run, binding, declaration, user_id = await _create_proposal_publication_grant(db_session)

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
    grant, _item, run, _binding, declaration, user_id = await _create_proposal_publication_grant(db_session)
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
    from packages.core.ai.runtime import ChatSurface
    from packages.core.ai.runtime.approval_service import (
        runtime_auto_confirm_provider_approval,
    )

    grant, _item, run, _binding, declaration, user_id = await _create_proposal_publication_grant(db_session)

    class SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(database, "async_session", lambda: SessionContext())
    url = "https://studio.youtube.com/channel/demo/videos/upload"
    blocked = json.dumps(
        {
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
        }
    )
    calls: list[str] = []

    async def execute(name: str, args: dict) -> str:
        calls.append(name)
        if name == "mcp__chrome__confirm_action":
            return json.dumps(
                {
                    "ok": True,
                    "status": "approved",
                    "approvalId": args["approvalId"],
                    "approvalToken": "single-use-token",
                }
            )
        return json.dumps(
            {
                "ok": True,
                "status": "published",
                "post_action_page_state": {"state_verified": True},
            }
        )

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
        runtime_surface=ChatSurface.WORKFLOW_AGENT_STEP,
        workflow_step_id=declaration["upload_step_id"],
    )
    assert mismatched == blocked
    assert calls == []

    direct_chat = await runtime_auto_confirm_provider_approval(
        **common,
        runtime_surface=ChatSurface.WORKSPACE_CHAT,
        workflow_step_id=declaration["publish_step_id"],
    )
    assert direct_chat == blocked
    assert calls == []
    await db_session.refresh(grant)
    assert grant.consumed_at is None

    # A historical Workflow grant satisfies only its declared publication.
    # A current explicit policy ask or deny still stops it before provider I/O.
    for effect in ("ask", "deny"):
        # A rejected provider grant rolls back its session and expires run.
        # The immutable call scope remains valid without implicit ORM I/O.
        await set_workspace_test_approval(
            db_session,
            entity_id=common["entity_id"],
            workspace_id=common["workspace_id"],
            actor_id=user_id,
            user_rules=[action_rule("chrome.action", effect=effect)],
        )
        await db_session.commit()
        assert (
            await runtime_auto_confirm_provider_approval(
                **common,
                runtime_surface=ChatSurface.WORKFLOW_AGENT_STEP,
                workflow_step_id=declaration["publish_step_id"],
            )
            == blocked
        )
        assert calls == []
        await db_session.refresh(grant)
        assert grant.consumed_at is None
    await set_workspace_test_approval(
        db_session, entity_id=common["entity_id"], workspace_id=common["workspace_id"], actor_id=user_id, user_rules=[]
    )
    await db_session.commit()

    published = await runtime_auto_confirm_provider_approval(
        **common,
        runtime_surface=ChatSurface.WORKFLOW_AGENT_STEP,
        workflow_step_id=declaration["publish_step_id"],
    )
    assert json.loads(published)["status"] == "published"
    assert calls == [
        "mcp__chrome__confirm_action",
        "mcp__chrome__click_element",
    ]
    await db_session.refresh(grant)
    assert grant.consumed_at is not None


async def _create_workflow_execution_publication_grant(
    db_session,
    *,
    tmp_path,
    monkeypatch,
    with_post_publish_asset: bool = False,
):
    from packages.core.models.document import Document
    from packages.core.services.workflow_run_trace import (
        build_execution_snapshot,
        workflow_definition_fingerprint,
    )

    context = await _create_workflow_wait_publication_grant(
        db_session,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    await db_session.delete(context["grant"])
    executor = context["executor_workflow"]
    run = context["executor_run"]
    declaration = {
        "kind": "youtube_publication_v1",
        "source": "workflow_execution",
        "destination": "studio.youtube.com",
        "channel_id": "UCverified",
        "channel_observation_step_id": "preflight",
        "lease_path": "publication_lease",
        "upload_step_id": "upload_youtube_video",
        "publish_step_id": "set_youtube_visibility",
        "artifact": context["declaration"]["artifact"],
        "publication": context["declaration"]["publication"],
        "ttl_seconds": 3600,
    }
    project = context["project"]
    if with_post_publish_asset:
        poster_bytes = b"product-preview-poster"
        poster = Document(
            entity_id=run.entity_id,
            name="product-preview-poster.png",
            fs_path="Knowledge/Stickman/images/product-preview-poster.png",
            file_type="png",
            mime_type="image/png",
            vector_status="ready",
            source="agent",
            metadata_={"sha256": hashlib.sha256(poster_bytes).hexdigest()},
            created_by=context["user_id"],
        )
        db_session.add(poster)
        await db_session.flush()
        poster_path = tmp_path / poster.fs_path
        poster_path.parent.mkdir(parents=True, exist_ok=True)
        poster_path.write_bytes(poster_bytes)
        declaration["post_publish_asset"] = {
            "role": "thumbnail",
            "workflow_project_id": project.id,
            "document_id": poster.id,
            "fs_path": poster.fs_path,
            "filename": poster.name,
            "sha256": poster.metadata_["sha256"],
            "state_path": "production.poster",
            "target_video_state_path": "publication.external_id",
        }
        declaration["post_publish_asset_step_id"] = "apply_thumbnail"
    executor.steps = [
        {"id": "start", "type": "trigger", "next": ["preflight"]},
        {
            "id": "preflight",
            "type": "tool",
            "config": {"tool": "check_youtube_publication_setup"},
            "next": ["grant"],
        },
        {
            "id": "grant",
            "type": "workflow_action_grant",
            "config": {"operation": "create", "declaration": declaration},
            "next": ["upload_youtube_video"],
        },
        {"id": "upload_youtube_video", "type": "agent", "next": ["set_youtube_visibility"]},
        {
            "id": "set_youtube_visibility",
            "type": "agent",
            "next": ["apply_thumbnail"] if with_post_publish_asset else [],
        },
        *([{"id": "apply_thumbnail", "type": "agent", "next": []}] if with_post_publish_asset else []),
    ]
    run.current_step_id = "grant"
    project.current_stage = "leased"
    project.state = {
        "artifact": {key: value for key, value in declaration["artifact"].items() if key != "workflow_project_id"},
        "publication": declaration["publication"],
        "publication_lease": {
            "run_id": run.id,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
            "due_path": "due_at",
            "pending_stage": "ready",
        },
        **(
            {"production": {"poster": {
                key: declaration["post_publish_asset"][key]
                for key in ("document_id", "fs_path", "filename", "sha256")
            }}}
            if with_post_publish_asset
            else {}
        ),
    }
    run.step_results = {
        "preflight": {
            "status": "completed",
            "output": {
                "ready": True,
                "studio_authenticated": True,
                "channel": declaration["channel_id"],
            },
        }
    }
    run.execution_snapshot = build_execution_snapshot(executor)
    run.trigger_data = {
        "_workflow_definition_fingerprint": workflow_definition_fingerprint(executor),
        "_workflow_runtime_context": {},
    }
    await db_session.flush()
    grant = await create_workflow_execution_youtube_publication_grant(
        db_session,
        run=run,
        grant_step_id="grant",
        declaration=declaration,
    )
    run.trigger_data = {
        **run.trigger_data,
        "_workflow_runtime_context": {"workflow_action_grant_id": grant.id},
    }
    await db_session.commit()
    return context | {"grant": grant, "declaration": declaration}


def test_execution_publication_grant_normalizes_a_declared_post_publish_asset():
    """An attachment is scoped to one agent step and one durable image."""
    declaration = {
        "kind": "youtube_publication_v1",
        "source": "workflow_execution",
        "destination": "studio.youtube.com",
        "channel_id": "UCverified",
        "channel_observation_step_id": "preflight",
        "lease_path": "publication_lease",
        "upload_step_id": "upload_video",
        "publish_step_id": "publish_video",
        "post_publish_asset_step_id": "apply_thumbnail",
        "artifact": {
            "workflow_project_id": "project-1",
            "document_id": "video-1",
            "fs_path": "videos/promo.mp4",
            "filename": "promo.mp4",
            "sha256": "a" * 64,
        },
        "post_publish_asset": {
            "role": "thumbnail",
            "workflow_project_id": "project-1",
            "document_id": "image-1",
            "fs_path": "images/cover.png",
            "filename": "cover.png",
            "sha256": "b" * 64,
            "state_path": "production.poster",
            "target_video_state_path": "publication.external_id",
        },
        "publication": {
            "title": "Product preview",
            "description": "A product preview.",
            "audience": "not_made_for_kids",
            "visibility": "public",
        },
        "ttl_seconds": 3600,
    }
    workflow_steps = [
        {"id": step_id, "type": "agent"} for step_id in ("upload_video", "publish_video", "apply_thumbnail")
    ]

    normalized = workflow_wait_youtube_publication_authorization(
        declaration,
        workflow_steps=workflow_steps,
    )

    assert normalized is not None
    assert normalized["post_publish_asset_step_id"] == "apply_thumbnail"
    assert normalized["post_publish_asset"]["document_id"] == "image-1"
    assert (
        workflow_wait_youtube_publication_authorization(
            {**declaration, "post_publish_asset_step_id": "publish_video"},
            workflow_steps=workflow_steps,
        )
        is None
    )
    assert (
        workflow_wait_youtube_publication_authorization(
            {**declaration, "post_publish_asset": {**declaration["post_publish_asset"], "filename": "cover.mp4"}},
            workflow_steps=workflow_steps,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", [None, "updated_definition", "prior_grant", "missing_parent", "changed_snapshot", "invalid_snapshot"]
)
async def test_execution_retry_can_create_only_its_first_publication_grant(db_session, tmp_path, monkeypatch, fault):
    from copy import deepcopy
    from packages.core.models.base import generate_ulid
    from packages.core.models.workflow import WorkflowRun

    context = await _create_workflow_execution_publication_grant(
        db_session,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    parent = context["executor_run"]
    if fault != "prior_grant":
        await db_session.delete(context["grant"])
    parent.status = "failed"
    retry = WorkflowRun(
        id=generate_ulid(),
        entity_id=parent.entity_id,
        workspace_id=parent.workspace_id,
        workflow_id=parent.workflow_id,
        started_by=parent.started_by,
        status="running",
        current_step_id="grant",
        lineage_root_run_id=parent.id,
        lineage_is_legacy=False,
        retry_of_run_id="missing" if fault == "missing_parent" else parent.id,
        attempt_number=2,
        execution_snapshot=deepcopy(parent.execution_snapshot),
        step_results=deepcopy(parent.step_results),
        trigger_data={"_workflow_runtime_context": {}},
    )
    db_session.add(retry)
    project = context["project"]
    project.state = {**project.state, "publication_lease": {**project.state["publication_lease"], "run_id": retry.id}}
    if fault == "changed_snapshot":
        parent.execution_snapshot = {**parent.execution_snapshot, "fingerprint": "changed"}
    elif fault == "invalid_snapshot":
        parent.execution_snapshot = {**parent.execution_snapshot, "fingerprint": "invalid"}
        retry.execution_snapshot = deepcopy(parent.execution_snapshot)
    elif fault == "updated_definition":
        context["executor_workflow"].steps = []
        context["executor_workflow"].version += 1
    await db_session.flush()
    if fault not in {None, "updated_definition", "prior_grant"}:
        with pytest.raises(WorkflowActionGrantDenied):
            await create_workflow_execution_youtube_publication_grant(
                db_session,
                run=retry,
                grant_step_id="grant",
                declaration=context["declaration"],
            )
        return
    grant = await create_workflow_execution_youtube_publication_grant(
        db_session,
        run=retry,
        grant_step_id="grant",
        declaration=context["declaration"],
    )
    assert grant.workflow_run_id == (parent.id if fault == "prior_grant" else retry.id)
    if fault == "prior_grant":
        assert grant.id == context["grant"].id
    assert grant.workflow_lineage_root_run_id == parent.id
    retry.trigger_data = {"_workflow_runtime_context": {"workflow_action_grant_id": grant.id}}
    scope = dict(
        grant_id=grant.id,
        entity_id=retry.entity_id,
        user_id=retry.started_by,
        workspace_id=retry.workspace_id,
        workflow_run_id=retry.id,
        workflow_lineage_root_run_id=parent.id,
        workflow_step_id="upload_youtube_video",
    )
    assert (
        await validate_workflow_execution_youtube_upload_preparation(
            db_session,
            source=context["declaration"]["artifact"]["fs_path"],
            **scope,
        )
        == context["declaration"]["artifact"]
    )
    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_execution_youtube_upload_preparation(
            db_session,
            source=context["declaration"]["artifact"]["fs_path"],
            **{**scope, "workflow_run_id": parent.id},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "active_parent",
        "changed_snapshot",
        "wrong_actor",
        "unrelated",
        "consumed",
        "revoked",
        "expired",
        "lease_owner",
    ],
)
async def test_execution_retry_reuses_only_its_unconsumed_publication_grant(
    db_session,
    tmp_path,
    monkeypatch,
    fault,
):
    from copy import deepcopy
    from packages.core.models.base import generate_ulid
    from packages.core.models.workflow import WorkflowRun

    context = await _create_workflow_execution_publication_grant(
        db_session,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    parent, grant = context["executor_run"], context["grant"]
    parent.status = "running" if fault == "active_parent" else "failed"
    retry = WorkflowRun(
        id=generate_ulid(),
        entity_id=parent.entity_id,
        workspace_id=parent.workspace_id,
        workflow_id=parent.workflow_id,
        started_by=parent.started_by,
        status="running",
        current_step_id="upload_youtube_video",
        lineage_root_run_id=parent.id,
        lineage_is_legacy=False,
        retry_of_run_id="missing" if fault == "unrelated" else parent.id,
        attempt_number=2,
        execution_snapshot=deepcopy(parent.execution_snapshot),
        step_results=deepcopy(parent.step_results),
        trigger_data=deepcopy(parent.trigger_data),
    )
    db_session.add(retry)
    project = context["project"]
    retry.trigger_data["_workflow_runtime_context"]["workflow_project_id"] = project.id
    project.state = {
        **project.state,
        "publication_lease": {
            **project.state["publication_lease"],
            "run_id": parent.id if fault == "lease_owner" else retry.id,
        },
    }
    if fault == "changed_snapshot":
        parent.execution_snapshot = {**parent.execution_snapshot, "fingerprint": "changed"}
    elif fault == "wrong_actor":
        parent.started_by = "another-user"
    elif fault in {"consumed", "revoked"}:
        setattr(grant, fault + "_at", datetime.now(timezone.utc))
    elif fault == "expired":
        grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.flush()
    scope = dict(
        grant_id=grant.id,
        entity_id=retry.entity_id,
        user_id=retry.started_by,
        workspace_id=retry.workspace_id,
        workflow_run_id=retry.id,
        workflow_lineage_root_run_id=parent.id,
        workflow_step_id="upload_youtube_video",
    )
    source = context["declaration"]["artifact"]["fs_path"]
    project_get_step = {
        "id": "project_after_grant",
        "type": "workflow_project",
        "config": {
            "operation": "get",
            "project_id": project.id,
            "project_type": project.project_type,
            "state_schema": {"type": "object"},
        },
    }
    from packages.core.ai.workflow_runner import WorkflowRunner

    if fault:
        with pytest.raises(WorkflowActionGrantDenied):
            await create_workflow_execution_youtube_publication_grant(
                db_session,
                run=retry,
                grant_step_id="grant",
                declaration=context["declaration"],
            )
        with pytest.raises(WorkflowActionGrantDenied):
            await validate_workflow_execution_youtube_upload_preparation(db_session, source=source, **scope)
        result = await WorkflowRunner()._execute_step_safe(project_get_step, retry, db_session)
        assert result["status"] == "completed"
        assert "workflow_action_grant_id" not in retry.trigger_data["_workflow_runtime_context"]
        return
    expires_at = grant.expires_at
    reused = await create_workflow_execution_youtube_publication_grant(
        db_session,
        run=retry,
        grant_step_id="grant",
        declaration=context["declaration"],
    )
    assert reused.id == grant.id
    assert reused.expires_at == expires_at
    result = await WorkflowRunner()._execute_step_safe(project_get_step, retry, db_session)
    assert result["status"] == "completed"
    assert retry.trigger_data["_workflow_runtime_context"]["workflow_action_grant_id"] == grant.id
    changed_declaration = {**context["declaration"], "ttl_seconds": context["declaration"]["ttl_seconds"] + 1}
    with pytest.raises(WorkflowActionGrantDenied):
        await create_workflow_execution_youtube_publication_grant(
            db_session,
            run=retry,
            grant_step_id="grant",
            declaration=changed_declaration,
        )
    assert (
        await validate_workflow_execution_youtube_upload_preparation(
            db_session,
            source=source,
            **scope,
        )
        == context["declaration"]["artifact"]
    )
    assert (
        await validate_publication_workflow_browser_action(
            db_session,
            browser_action="fill_or_select",
            browser_arguments={"tabId": 42, "ref": "search-drafts", "value": "technical test"},
            **scope,
        )
        is True
    )
    assert grant.workflow_run_id == parent.id
    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_execution_youtube_upload_preparation(
            db_session,
            source=source,
            **{**scope, "workflow_run_id": parent.id},
        )
    # Reuse never reissues or extends the grant, and consumption is shared.
    request = {
        **_youtube_publication_request("publish"),
        "url": "https://studio.youtube.com/channel/UCverified/videos/upload",
    }
    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_execution_youtube_publication_grant(
            db_session,
            provider_request=request,
            **scope,
        )
    publish_scope = {**scope, "workflow_step_id": "set_youtube_visibility"}
    consumed = await consume_workflow_execution_youtube_publication_grant(
        db_session,
        provider_request=request,
        **publish_scope,
    )
    assert consumed.id == grant.id
    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_execution_youtube_upload_preparation(db_session, source=source, **scope)


@pytest.mark.asyncio
async def test_workflow_execution_publication_grant_binds_native_observation_artifact_and_channel(
    db_session,
    tmp_path,
    monkeypatch,
):
    context = await _create_workflow_execution_publication_grant(
        db_session,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    grant = context["grant"]
    run = context["executor_run"]
    declaration = context["declaration"]
    scope = {
        "grant_id": grant.id,
        "entity_id": run.entity_id,
        "user_id": context["user_id"],
        "workspace_id": run.workspace_id,
        "workflow_run_id": run.id,
        "workflow_lineage_root_run_id": run.id,
        "workflow_step_id": declaration["upload_step_id"],
    }
    artifact = await validate_workflow_execution_youtube_upload_preparation(
        db_session,
        source=declaration["artifact"]["fs_path"],
        **scope,
    )
    assert artifact == declaration["artifact"]
    prepared_path = f"/managed/uploads/{declaration['artifact']['filename']}"
    await record_workflow_execution_youtube_upload_preparation(
        db_session,
        source=declaration["artifact"]["fs_path"],
        local_path=prepared_path,
        **scope,
    )
    await db_session.commit()
    assert (
        await validate_publication_workflow_browser_action(
            db_session,
            browser_action="upload",
            browser_arguments={"files": [prepared_path]},
            **scope,
        )
        is True
    )

    for suffix in ("", "/", "/videos/upload?test=1"):
        validated, request_kind = await validate_workflow_execution_youtube_publication_grant(
            db_session,
            provider_request={
                **_youtube_publication_request("upload"),
                "url": f"https://studio.youtube.com/channel/UCverified{suffix}",
                "retry_arguments": {"files": [prepared_path]},
            },
            **scope,
        )
        assert validated.id == grant.id
        assert request_kind == "upload_transfer"

    for url in (
        "https://studio.youtube.com/channel/UCother/videos/upload",
        "https://studio.youtube.com/channel/UCverified-other/videos/upload",
        "http://studio.youtube.com/channel/UCverified/",
    ):
        with pytest.raises(WorkflowActionGrantDenied):
            await validate_workflow_execution_youtube_publication_grant(
                db_session,
                provider_request={
                    **_youtube_publication_request("upload"),
                    "url": url,
                    "retry_arguments": {"files": [prepared_path]},
                },
                **scope,
            )

    publish_scope = {**scope, "workflow_step_id": declaration["publish_step_id"]}
    request = {
        **_youtube_publication_request("publish"),
        "url": "https://studio.youtube.com/channel/UCverified",
    }
    consumed = await consume_workflow_execution_youtube_publication_grant(
        db_session,
        provider_request=request,
        **publish_scope,
    )
    assert consumed.id == grant.id
    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_execution_youtube_publication_grant(
            db_session,
            provider_request=request,
            **publish_scope,
        )


@pytest.mark.asyncio
async def test_execution_post_publish_asset_is_limited_to_verified_video_and_image(
    db_session,
    tmp_path,
    monkeypatch,
):
    context = await _create_workflow_execution_publication_grant(
        db_session,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        with_post_publish_asset=True,
    )
    grant = context["grant"]
    run = context["executor_run"]
    declaration = context["declaration"]
    base_scope = {
        "grant_id": grant.id,
        "entity_id": run.entity_id,
        "user_id": context["user_id"],
        "workspace_id": run.workspace_id,
        "workflow_run_id": run.id,
        "workflow_lineage_root_run_id": run.id,
    }
    video_scope = {**base_scope, "workflow_step_id": declaration["publish_step_id"]}
    await consume_workflow_execution_youtube_publication_grant(
        db_session,
        provider_request={
            **_youtube_publication_request("publish"),
            "url": "https://studio.youtube.com/channel/UCverified/videos/upload",
        },
        **video_scope,
    )
    project = context["project"]
    project.state = {
        **project.state,
        "publication": {"external_id": "a1b2c3D4e5F"},
    }
    await db_session.commit()

    asset_scope = {**base_scope, "workflow_step_id": declaration["post_publish_asset_step_id"]}
    upload = {
        **_youtube_publication_request("upload"),
        "url": "https://studio.youtube.com/video/a1b2c3D4e5F/edit",
        "retry_arguments": {"document_id": declaration["post_publish_asset"]["document_id"]},
    }
    _validated, kind = await validate_workflow_execution_youtube_publication_grant(
        db_session,
        provider_request=upload,
        **asset_scope,
    )
    assert kind == "post_publish_asset_upload"
    _validated, kind = await validate_workflow_execution_youtube_publication_grant(
        db_session,
        provider_request={
            **_youtube_publication_request("save"),
            "url": "https://studio.youtube.com/video/a1b2c3D4e5F/edit",
        },
        **asset_scope,
    )
    assert kind == "post_publish_asset_save"

    for invalid_request in (
        {**upload, "retry_arguments": {"document_id": declaration["artifact"]["document_id"]}},
        {**upload, "url": "https://studio.youtube.com/video/z9y8x7W6v5U/edit"},
    ):
        with pytest.raises(WorkflowActionGrantDenied):
            await validate_workflow_execution_youtube_publication_grant(
                db_session,
                provider_request=invalid_request,
                **asset_scope,
            )
    with pytest.raises(WorkflowActionGrantDenied):
        await validate_workflow_execution_youtube_publication_grant(
            db_session,
            provider_request=upload,
            **{**base_scope, "workflow_step_id": declaration["upload_step_id"]},
        )
    with pytest.raises(WorkflowActionGrantDenied):
        await validate_publication_workflow_browser_action(
            db_session,
            browser_action="fill_or_select",
            browser_arguments={"tabId": 1, "ref": "title", "value": "Changed"},
            **asset_scope,
        )

    # The provider path receives the same normalized request, including the
    # exact Studio URL, rather than treating a thumbnail save as video publish.
    from packages.core.ai.runtime.provider_approvals import normalize_provider_approval
    provider_request = normalize_provider_approval(
        "mcp__chrome__click_element",
        {"tabId": 1, "ref": "save"},
        json.dumps({
            "status": "approval_required", "approval_required": True, "provider": "chrome",
            "approvalId": "asset-save", "confirmation_mode": "always_action_time",
            "policy_category": "representational_communication", "target_label": "Save",
            "url": "https://studio.youtube.com/video/a1b2c3D4e5F/edit",
        }),
    )
    _validated, kind = await validate_workflow_execution_youtube_publication_grant(
        db_session,
        provider_request=provider_request,
        **asset_scope,
    )
    assert kind == "post_publish_asset_save"


@pytest.mark.asyncio
async def test_project_patch_keeps_consumed_video_grant_for_declared_post_asset(
    db_session, tmp_path, monkeypatch,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.ai.runtime.workflow_tools import runtime_workflow_run_context

    context = await _create_workflow_execution_publication_grant(
        db_session, tmp_path=tmp_path, monkeypatch=monkeypatch, with_post_publish_asset=True,
    )
    grant, run, project, declaration = (
        context["grant"], context["executor_run"], context["project"], context["declaration"]
    )
    scope = {
        "grant_id": grant.id, "entity_id": run.entity_id, "user_id": context["user_id"],
        "workspace_id": run.workspace_id, "workflow_run_id": run.id,
        "workflow_lineage_root_run_id": run.id, "workflow_step_id": declaration["publish_step_id"],
    }
    await consume_workflow_execution_youtube_publication_grant(
        db_session,
        provider_request={**_youtube_publication_request("publish"), "url": "https://studio.youtube.com/channel/UCverified/videos/upload"},
        **scope,
    )
    run.trigger_data["_workflow_runtime_context"]["workflow_project_id"] = project.id
    revision = project.revision
    result = await WorkflowRunner()._execute_workflow_project_step(
        {"id": "persist_receipt", "type": "workflow_project", "config": {
            "operation": "patch", "project_id": project.id, "project_type": project.project_type,
            "expected_revision": revision, "patch": {"publication": {"external_id": "a1b2c3D4e5F"}},
            "state_schema": {"type": "object"},
        }},
        {}, run, db_session,
    )
    assert result["status"] == "completed"
    assert runtime_workflow_run_context(run)["workflow_action_grant_id"] == grant.id
    asset_scope = {**scope, "workflow_step_id": declaration["post_publish_asset_step_id"]}
    assert await validate_publication_workflow_browser_action(
        db_session, browser_action="upload",
        browser_arguments={"document_id": declaration["post_publish_asset"]["document_id"]},
        **asset_scope,
    ) is True
    with pytest.raises(WorkflowActionGrantDenied):
        await validate_publication_workflow_browser_action(
            db_session, browser_action="upload",
            browser_arguments={"document_id": declaration["artifact"]["document_id"]},
            **asset_scope,
        )




@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["missing_observation", "wrong_observation_node", "wrong_actor", "stopped", "expired_lease"],
)
async def test_workflow_execution_publication_grant_rechecks_live_run_and_observation(
    db_session,
    tmp_path,
    monkeypatch,
    change,
):
    context = await _create_workflow_execution_publication_grant(
        db_session,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    grant = context["grant"]
    run = context["executor_run"]
    declaration = context["declaration"]
    if change == "missing_observation":
        run.step_results["preflight"]["output"]["channel"] = "UCother"
    elif change == "wrong_observation_node":
        for step in run.execution_snapshot["steps"]:
            if step.get("id") == "preflight":
                step["config"] = {"tool": "other_tool"}
    elif change == "wrong_actor":
        run.started_by = "another-user"
    elif change == "expired_lease":
        context["project"].state["publication_lease"]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
    else:
        run.status = "cancelled"
    await db_session.commit()

    with pytest.raises(WorkflowActionGrantDenied):
        await validate_publication_workflow_browser_action(
            db_session,
            grant_id=grant.id,
            entity_id=run.entity_id,
            user_id=context["user_id"],
            workspace_id=run.workspace_id,
            workflow_run_id=run.id,
            workflow_lineage_root_run_id=run.id,
            workflow_step_id=declaration["upload_step_id"],
            browser_action="click_element",
            browser_arguments={},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_supplies_retry_arguments", [False, True])
@pytest.mark.parametrize(
    "use_source,post_asset",
    [
        (False, False),
        (False, True),
    ],
)
async def test_workflow_publication_provider_confirmation_uses_internal_runtime_scope(
    db_session,
    tmp_path,
    monkeypatch,
    provider_supplies_retry_arguments,
    use_source,
    post_asset,
):
    from packages.core import database
    from packages.core.ai.runtime import (
        AIRuntimeRequest,
        ChatSurface,
        RuntimeResolver,
    )
    from packages.core.ai.runtime.tool_availability import (
        RuntimeMCPCallPreflightResult,
    )
    from packages.core.ai.runtime.tool_registry import runtime_execute_tool
    from packages.core.ai.tool_pool import ToolPool

    context = (
        await _create_workflow_execution_publication_grant(
            db_session, tmp_path=tmp_path, monkeypatch=monkeypatch, with_post_publish_asset=True,
        )
        if post_asset
        else await _create_workflow_wait_publication_grant(
            db_session, tmp_path=tmp_path, monkeypatch=monkeypatch,
        )
    )
    grant = context["grant"]
    executor_run = context["executor_run"]
    declaration = context["declaration"]
    user_id = context["user_id"]
    if post_asset:
        await consume_workflow_execution_youtube_publication_grant(
            db_session,
            provider_request={**_youtube_publication_request("publish"), "url": "https://studio.youtube.com/channel/UCverified/videos/upload"},
            grant_id=grant.id, entity_id=executor_run.entity_id, user_id=user_id,
            workspace_id=executor_run.workspace_id, workflow_run_id=executor_run.id,
            workflow_lineage_root_run_id=executor_run.id, workflow_step_id=declaration["publish_step_id"],
        )
        context["project"].state = {**context["project"].state, "publication": {"external_id": "a1b2c3D4e5F"}}
        await db_session.commit()
    # The fixture authorizes these two concrete Runtime effects separately;
    # the old publication grant is not consent for all Chrome actions.
    await set_workspace_test_approval(
        db_session,
        entity_id=executor_run.entity_id,
        workspace_id=executor_run.workspace_id,
        actor_id=user_id,
        user_rules=[action_rule("chrome.browser.interact"), action_rule("chrome.file_upload.confirm")],
    )
    if not post_asset:
        await _record_workflow_wait_upload_preparation(db_session, context, context["document"].fs_path)

    class SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_exc):
            return False

    async def allow_mcp_call(*_args, **_kwargs):
        return RuntimeMCPCallPreflightResult()

    monkeypatch.setattr(database, "async_session", lambda: SessionContext())
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_execution.runtime_preflight_mcp_call",
        allow_mcp_call,
    )

    upload_tool = "mcp__chrome__upload"
    confirm_tool = "mcp__chrome__confirm_action"
    approval_id = "workflow-upload-approval"
    calls: list[tuple[str, dict]] = []
    upload_arguments = (
        {"document_id": declaration["post_publish_asset"]["document_id"]}
        if post_asset
        else
        {
            "source": (f"/api/v1/fs/{executor_run.entity_id}/{context['document'].fs_path}"),
        }
        if use_source
        else {"files": [context["document"].fs_path]}
    )

    async def upload_handler(**kwargs):
        calls.append((upload_tool, dict(kwargs)))
        if post_asset:
            assert kwargs.get("document_id") == declaration["post_publish_asset"]["document_id"]
        if kwargs.get("approvalToken"):
            return json.dumps(
                {
                    "ok": True,
                    "status": "uploaded",
                    "action_executed": True,
                }
            )
        provider_result = {
            "ok": False,
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": approval_id,
            "confirmation_mode": "preapproval_allowed",
            "policy_category": "file_upload",
            "target_label": "Upload videos",
            "target_role": "input",
            "url": "https://studio.youtube.com/video/a1b2c3D4e5F/edit" if post_asset else "https://studio.youtube.com/channel/demo/videos/upload",
        }
        if provider_supplies_retry_arguments:
            provider_result["retry_action"] = {
                "name": upload_tool,
                "arguments": {"files": ["provider-rewritten.mp4"]},
            }
        return json.dumps(
            {
                "server": "chrome",
                "tool": "upload",
                "error": "tool_error",
                "detail": json.dumps(provider_result),
            }
        )

    async def confirm_handler(**kwargs):
        calls.append((confirm_tool, dict(kwargs)))
        return json.dumps(
            {
                "ok": True,
                "status": "approved",
                "approvalId": kwargs["approvalId"],
                "approvalToken": "single-use-upload-token",
            }
        )

    pool = ToolPool()
    pool.register(
        upload_tool,
        {
            "type": "function",
            "function": {
                "name": upload_tool,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "files": {"type": "array", "items": {"type": "string"}},
                        "source": {"type": "string"},
                        "document_id": {"type": "string"},
                        "approvalToken": {"type": "string"},
                    },
                },
            },
        },
        upload_handler,
    )
    pool.register(
        confirm_tool,
        {
            "type": "function",
            "function": {
                "name": confirm_tool,
                "parameters": {
                    "type": "object",
                    "properties": {"approvalId": {"type": "string"}},
                    "required": ["approvalId"],
                },
            },
        },
        confirm_handler,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_registry.runtime_ensure_tool_registry_initialized",
        lambda: pool,
    )

    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.WORKFLOW_AGENT_STEP,
            entity_id=executor_run.entity_id,
            user_id=user_id,
            workspace_id=executor_run.workspace_id,
        ),
        tool_schemas=[pool.get_schema(upload_tool)],
        allowed_tool_names={upload_tool},
    )
    result = await runtime_execute_tool(
        upload_tool,
        {
            **upload_arguments,
            "_workflow_run_id_from_context": executor_run.id,
            "_workflow_lineage_root_run_id_from_context": (executor_run.lineage_root_run_id),
            "_workflow_action_grant_id_from_context": grant.id,
            "_workflow_step_id_from_context": declaration["post_publish_asset_step_id"] if post_asset else declaration["upload_step_id"],
            "_runtime_run_id_from_context": f"workflow:{executor_run.id}",
            "_runtime_tool_call_id_from_context": f"node:{declaration['post_publish_asset_step_id'] if post_asset else declaration['upload_step_id']}",
        },
        entity_id=executor_run.entity_id,
        user_id=user_id,
        workspace_id=executor_run.workspace_id,
        allowed_tool_names={upload_tool},
        runtime_envelope=envelope,
    )

    assert json.loads(result)["status"] == "uploaded"
    assert [name for name, _arguments in calls] == [
        upload_tool,
        confirm_tool,
        upload_tool,
    ]
    retry_arguments = calls[-1][1]
    confirmation_arguments = calls[1][1]
    assert confirmation_arguments["_workflow_run_id_from_context"] == executor_run.id
    assert confirmation_arguments["_workflow_lineage_root_run_id_from_context"] == executor_run.lineage_root_run_id
    assert confirmation_arguments["_workflow_action_grant_id_from_context"] == grant.id
    expected_step = declaration["post_publish_asset_step_id"] if post_asset else declaration["upload_step_id"]
    assert confirmation_arguments["_workflow_step_id_from_context"] == expected_step
    assert retry_arguments["_workflow_action_grant_id_from_context"] == grant.id
    assert retry_arguments["_workflow_step_id_from_context"] == expected_step
    if post_asset:
        assert retry_arguments["document_id"] == upload_arguments["document_id"]
        assert "files" not in retry_arguments
    if use_source:
        assert calls[0][1]["source"] == upload_arguments["source"]
        assert retry_arguments["source"] == upload_arguments["source"]
        assert "files" not in retry_arguments
    assert confirm_tool not in envelope.allowed_tool_names
    await db_session.refresh(grant)
    assert grant.consumed_at is None
