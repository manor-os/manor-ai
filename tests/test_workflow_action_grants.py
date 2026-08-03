"""Generic workflow action grant service tests (create/validate/revoke/TTL).

Chrome/local-browser-worker enforcement tests that build on these grants live
in ``tests/test_workflow_action_grants_chrome.py`` instead — that file (and
the ``packages.core.ai.mcp.chrome`` module it exercises) is excluded from the
OSS export, so this file stays free of that import and ships in OSS.
"""
from datetime import datetime, timedelta, timezone

import pytest

from packages.core.services.workflow_action_grant_service import (
    WorkflowActionGrantDenied,
    create_workflow_action_grant,
    revoke_workflow_action_grant,
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
