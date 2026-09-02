import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from packages.core.database import async_session
from packages.core.models.workspace import Workspace
from packages.core.services import workspace_access
from packages.core.services.workspace_access import (
    WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE,
    WORKSPACE_ACCESS_MODE_KEY,
    WORKSPACE_ACCESS_MODE_MEMBERS_ONLY,
    settings_with_default_workspace_access,
    lock_workspace_recipient_authorization,
    workspace_access_mode,
)


def test_workspace_access_mode_missing_settings_defaults_to_members_only():
    assert workspace_access_mode(SimpleNamespace(settings=None)) == WORKSPACE_ACCESS_MODE_MEMBERS_ONLY
    assert workspace_access_mode(SimpleNamespace(settings={})) == WORKSPACE_ACCESS_MODE_MEMBERS_ONLY
    assert (
        workspace_access_mode(SimpleNamespace(settings={WORKSPACE_ACCESS_MODE_KEY: "unexpected"}))
        == WORKSPACE_ACCESS_MODE_MEMBERS_ONLY
    )


def test_workspace_access_mode_preserves_explicit_entity_visible():
    workspace = SimpleNamespace(settings={WORKSPACE_ACCESS_MODE_KEY: WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE})

    assert workspace_access_mode(workspace) == WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE


def test_workspace_access_settings_helper_sets_secure_default():
    assert settings_with_default_workspace_access({})[WORKSPACE_ACCESS_MODE_KEY] == WORKSPACE_ACCESS_MODE_MEMBERS_ONLY
    assert (
        settings_with_default_workspace_access({WORKSPACE_ACCESS_MODE_KEY: WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE})[
            WORKSPACE_ACCESS_MODE_KEY
        ]
        == WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE
    )


@pytest.mark.asyncio
async def test_recipient_authorization_locks_every_acl_input_for_share():
    class RecordingSession:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)

    db = RecordingSession()

    await lock_workspace_recipient_authorization(
        db,
        workspace_id="workspace-1",
        entity_id="entity-1",
        user_id="user-1",
    )

    sql = [
        str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        for statement in db.statements
    ]
    assert len(sql) == 5
    assert all("FOR SHARE" in statement for statement in sql)
    assert [
        "staff_roles" in sql[0],
        "staff" in sql[1],
        "user_memberships" in sql[2],
        "users" in sql[3],
        "workspace_staff" in sql[4],
    ] == [True, True, True, True, True]


@pytest.mark.asyncio
async def test_post_commit_dispatch_merges_fresh_access_mode(
    db_session,
    monkeypatch,
):
    from packages.core.services.workspace_setup_service import (
        dispatch_workspace_post_commit,
    )

    workspace = Workspace(
        id="workspace-settings-race",
        entity_id="entity-settings-race",
        name="Settings race",
        status="active",
        heartbeat_enabled=False,
        settings={
            WORKSPACE_ACCESS_MODE_KEY: WORKSPACE_ACCESS_MODE_MEMBERS_ONLY,
            "provisioning": {"post_commit_dispatch": "pending"},
        },
    )
    db_session.add(workspace)
    await db_session.commit()

    final_merge_waiting = asyncio.Event()
    allow_final_merge = asyncio.Event()
    original_lock = workspace_access.lock_workspace_access_boundary

    async def delayed_final_lock(*args, **kwargs):
        final_merge_waiting.set()
        await allow_final_merge.wait()
        return await original_lock(*args, **kwargs)

    monkeypatch.setattr(
        workspace_access,
        "lock_workspace_access_boundary",
        delayed_final_lock,
    )

    dispatch = asyncio.create_task(
        dispatch_workspace_post_commit(
            db_session,
            workspace_id=workspace.id,
            entity_id=workspace.entity_id,
        )
    )
    await asyncio.wait_for(final_merge_waiting.wait(), timeout=5)

    async with async_session() as concurrent_db:
        concurrent_workspace = (
            await concurrent_db.execute(
                select(Workspace)
                .where(Workspace.id == workspace.id)
                .with_for_update()
            )
        ).scalar_one()
        settings = dict(concurrent_workspace.settings or {})
        settings[WORKSPACE_ACCESS_MODE_KEY] = WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE
        concurrent_workspace.settings = settings
        await concurrent_db.commit()

    allow_final_merge.set()
    await dispatch
    await db_session.commit()
    await db_session.refresh(workspace)

    assert workspace.settings[WORKSPACE_ACCESS_MODE_KEY] == WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE
    assert workspace.settings["provisioning"]["post_commit_dispatch"] == "dispatched"


@pytest.mark.asyncio
async def test_chat_bookmark_merges_into_locked_workspace_settings(monkeypatch):
    from datetime import datetime, timezone

    from packages.core.memory import chat_extractor

    stale_workspace = SimpleNamespace(
        id="workspace-bookmark",
        entity_id="entity-bookmark",
        settings={WORKSPACE_ACCESS_MODE_KEY: WORKSPACE_ACCESS_MODE_MEMBERS_ONLY},
    )
    locked_workspace = SimpleNamespace(
        id=stale_workspace.id,
        entity_id=stale_workspace.entity_id,
        settings={WORKSPACE_ACCESS_MODE_KEY: WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE},
    )
    db = object()

    async def return_locked_workspace(session, *, workspace_id, entity_id):
        assert session is db
        assert workspace_id == stale_workspace.id
        assert entity_id == stale_workspace.entity_id
        return locked_workspace

    monkeypatch.setattr(
        workspace_access,
        "lock_workspace_access_boundary",
        return_locked_workspace,
    )
    bookmark = datetime(2026, 8, 29, tzinfo=timezone.utc)

    await chat_extractor._write_bookmark(db, stale_workspace, bookmark)

    assert (
        locked_workspace.settings[WORKSPACE_ACCESS_MODE_KEY]
        == WORKSPACE_ACCESS_MODE_ENTITY_VISIBLE
    )
    assert locked_workspace.settings[chat_extractor.LAST_EXTRACT_KEY] == bookmark.isoformat()


@pytest.mark.asyncio
async def test_soft_deleted_workspaces_leave_no_membership_goal_or_plan_visibility(
    db_session,
):
    from datetime import datetime, timezone
    from decimal import Decimal

    from apps.api.routers.plans import list_plans
    from packages.core.goals.service import list_goals
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.goal import Goal
    from packages.core.models.user import User
    from packages.core.models.workspace import WorkspaceStaff

    entity_id = generate_ulid()
    owner = User(
        entity_id=entity_id,
        email="deleted-workspace-owner@test.com",
        display_name="Deleted Workspace Owner",
        password_hash="not-used",
        role="owner",
        status="active",
    )
    member = User(
        entity_id=entity_id,
        email="deleted-workspace-member@test.com",
        display_name="Deleted Workspace Member",
        password_hash="not-used",
        role="member",
        status="active",
    )
    active_workspace = Workspace(
        entity_id=entity_id,
        name="Active Workspace",
        settings={WORKSPACE_ACCESS_MODE_KEY: WORKSPACE_ACCESS_MODE_MEMBERS_ONLY},
    )
    deleted_workspace = Workspace(
        entity_id=entity_id,
        name="Deleted Workspace",
        settings={WORKSPACE_ACCESS_MODE_KEY: WORKSPACE_ACCESS_MODE_MEMBERS_ONLY},
        deleted_at=datetime.now(timezone.utc),
    )
    foreign_workspace = Workspace(
        entity_id=generate_ulid(),
        name="Foreign Workspace",
        settings={WORKSPACE_ACCESS_MODE_KEY: WORKSPACE_ACCESS_MODE_MEMBERS_ONLY},
    )
    db_session.add_all([
        owner,
        member,
        active_workspace,
        deleted_workspace,
        foreign_workspace,
    ])
    await db_session.flush()
    db_session.add_all([
        WorkspaceStaff(
            workspace_id=deleted_workspace.id,
            user_id=member.id,
            role="viewer",
            status="active",
        ),
        WorkspaceStaff(
            workspace_id=foreign_workspace.id,
            user_id=member.id,
            role="viewer",
            status="active",
        ),
        Goal(
            entity_id=entity_id,
            workspace_id=active_workspace.id,
            title="Active Goal",
            goal_key="active_goal",
            metric_key="active_metric",
            target_value=Decimal("1"),
        ),
        Goal(
            entity_id=entity_id,
            workspace_id=deleted_workspace.id,
            title="Deleted Goal",
            goal_key="deleted_goal",
            metric_key="deleted_metric",
            target_value=Decimal("1"),
        ),
        Goal(
            entity_id=entity_id,
            workspace_id=None,
            title="Entity Goal",
            goal_key="entity_goal",
            metric_key="entity_metric",
            target_value=Decimal("1"),
        ),
        ExecutionPlan(
            entity_id=entity_id,
            workspace_id=active_workspace.id,
            plan_dag={},
        ),
        ExecutionPlan(
            entity_id=entity_id,
            workspace_id=deleted_workspace.id,
            plan_dag={},
        ),
        ExecutionPlan(
            entity_id=entity_id,
            workspace_id=None,
            plan_dag={},
        ),
    ])
    await db_session.commit()

    readable = await workspace_access.readable_workspace_ids_for_user(
        db_session,
        entity_id=entity_id,
        user_id=member.id,
        role=member.role,
    )
    assert readable == set()

    goals = await list_goals(
        db_session,
        entity_id,
        readable_workspace_ids=None,
    )
    assert {goal.title for goal in goals} == {"Active Goal", "Entity Goal"}

    plans = await list_plans(
        workspace_id=None,
        task_id=None,
        status=None,
        limit=50,
        user=owner,
        db=db_session,
    )
    assert {plan.workspace_id for plan in plans} == {active_workspace.id, None}
