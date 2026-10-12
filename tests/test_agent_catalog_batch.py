"""Agent catalog authorization batching regressions."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import event

from apps.api.routers.agents import _visible_agents
from packages.core.models.base import generate_ulid
from packages.core.models.permission import (
    Capability,
    GrantStatus,
    ResourceGrant,
    ResourceType,
    SubjectType,
    Visibility,
)
from packages.core.models.staff import Staff, StaffRole
from packages.core.models.user import User
from packages.core.models.workspace import Agent, Workspace, WorkspaceStaff
from packages.core.services.auth_service import hash_password
from packages.core.services.agent_service import list_agents
from packages.core.services.composer_resources import resource_workspace_names
from packages.core.services.resource_access import (
    ResourceDescriptor,
    user_can_access_resource,
)

pytestmark = pytest.mark.oss_regression


def _capture_sql(db_session):
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", record)
    return statements, record


async def _catalog_query_count(db_session, user):
    db_session.expunge_all()
    statements, listener = _capture_sql(db_session)
    try:
        visible = await _visible_agents(db_session, user)
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", listener)
    return len(statements), visible


async def _seed_entity_agents(db_session, count):
    entity_id = generate_ulid()
    user = User(
        entity_id=entity_id,
        email=f"agent-catalog-{generate_ulid()}@test.com",
        display_name="Catalog member",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    agents = [
        Agent(entity_id=entity_id, name=f"Catalog agent {index}", visibility=Visibility.ENTITY)
        for index in range(count)
    ]
    db_session.add_all([user, *agents])
    await db_session.flush()
    return user, agents


@pytest.mark.asyncio
async def test_visible_agents_matches_single_resource_acl_for_mixed_catalog(db_session):
    """Batching retains ownership, grants, workspace, deletion, and template rules."""
    entity_id = generate_ulid()
    user = User(
        entity_id=entity_id,
        email=f"agent-catalog-acl-{generate_ulid()}@test.com",
        display_name="Stale admin",
        password_hash=hash_password("pass123"),
        role="admin",  # Staff is authoritative and deliberately restricts this login.
        status="active",
    )
    restricted_role = StaffRole(
        entity_id=entity_id, name="Restricted catalog", permissions=[], status="active",
    )
    db_session.add_all([user, restricted_role])
    await db_session.flush()
    staff = Staff(
        entity_id=entity_id,
        kind="employee",
        name=user.display_name,
        email=user.email,
        user_id=user.id,
        role_id=restricted_role.id,
        status="active",
    )
    db_session.add(staff)
    visible_workspace = Workspace(
        entity_id=entity_id, name="Visible workspace", settings={"access_mode": "members_only"},
    )
    removed_workspace = Workspace(
        entity_id=entity_id, name="Removed workspace", settings={"access_mode": "members_only"},
    )
    deleted_workspace = Workspace(
        entity_id=entity_id,
        name="Deleted workspace",
        settings={"access_mode": "members_only"},
        deleted_at=datetime.now(timezone.utc),
    )
    db_session.add_all([visible_workspace, removed_workspace, deleted_workspace])
    await db_session.flush()
    db_session.add_all([
        WorkspaceStaff(workspace_id=visible_workspace.id, user_id=user.id, role="viewer", status="active"),
        WorkspaceStaff(workspace_id=removed_workspace.id, user_id=user.id, role="viewer", status="inactive"),
    ])

    owned = Agent(entity_id=entity_id, name="Owned private", owner_user_id=user.id, visibility=Visibility.PRIVATE)
    granted = Agent(entity_id=entity_id, name="Granted private", visibility=Visibility.PRIVATE)
    revoked = Agent(entity_id=entity_id, name="Revoked private", visibility=Visibility.PRIVATE)
    workspace_visible = Agent(
        entity_id=entity_id, name="Workspace visible", workspace_id=visible_workspace.id,
        visibility=Visibility.WORKSPACE,
    )
    workspace_removed = Agent(
        entity_id=entity_id, name="Workspace removed", workspace_id=removed_workspace.id,
        visibility=Visibility.WORKSPACE,
    )
    workspace_deleted = Agent(
        entity_id=entity_id, name="Workspace deleted", workspace_id=deleted_workspace.id,
        visibility=Visibility.WORKSPACE,
    )
    entity_visible = Agent(entity_id=entity_id, name="Entity visible", visibility=Visibility.ENTITY)
    public = Agent(entity_id=entity_id, name="Same entity public", visibility=Visibility.PUBLIC)
    hidden_private = Agent(entity_id=entity_id, name="Other private", visibility=Visibility.PRIVATE)
    deleted_agent = Agent(
        entity_id=entity_id, name="Deleted agent", visibility=Visibility.ENTITY,
        deleted_at=datetime.now(timezone.utc),
    )
    template = Agent(entity_id=None, name="Platform template", is_template=True, is_public=True)
    foreign = Agent(
        entity_id=generate_ulid(), name="Foreign template", visibility=Visibility.PUBLIC,
        is_template=True, is_public=True,
    )
    candidates = [
        owned, granted, revoked, workspace_visible, workspace_removed,
        workspace_deleted, entity_visible, public, hidden_private, deleted_agent,
        template, foreign,
    ]
    db_session.add_all(candidates)
    await db_session.flush()
    db_session.add_all([
        ResourceGrant(
            entity_id=entity_id, resource_type=ResourceType.AGENT, resource_id=granted.id,
            subject_type=SubjectType.USER, subject_id=user.id, capabilities=[Capability.VIEW],
            granted_at=datetime.now(timezone.utc), status=GrantStatus.ACTIVE,
        ),
        ResourceGrant(
            entity_id=entity_id, resource_type=ResourceType.AGENT, resource_id=revoked.id,
            subject_type=SubjectType.USER, subject_id=user.id, capabilities=[Capability.VIEW],
            granted_at=datetime.now(timezone.utc), status=GrantStatus.REVOKED,
        ),
    ])
    await db_session.flush()

    catalog = await list_agents(db_session, entity_id, include_templates=True)
    individually_visible = []
    for candidate in catalog:
        if not candidate.entity_id or await user_can_access_resource(
            db_session,
            descriptor=ResourceDescriptor.from_row(candidate, ResourceType.AGENT),
            entity_id=user.entity_id,
            user_id=user.id,
            role=user.role,
            capability=Capability.VIEW,
        ):
            individually_visible.append(candidate.id)

    visible = await _visible_agents(db_session, user, include_templates=True)

    assert [agent.id for agent in visible] == individually_visible
    assert {agent.id for agent in visible} == {
        owned.id, granted.id, workspace_visible.id, public.id, template.id,
    }

    # A revoked Staff history remains authoritative over the stale User.role.
    staff.status = "inactive"
    await db_session.flush()
    revoked_staff_visible = await _visible_agents(db_session, user, include_templates=True)
    assert [agent.id for agent in revoked_staff_visible] == individually_visible


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("access_mode", "readable"),
    [
        pytest.param("entity_visible", True, id="canonical"),
        pytest.param(" entity_visible ", True, id="spaces"),
        pytest.param("\t\nentity_visible\r\v\f", True, id="ascii-whitespace"),
        pytest.param("\u00a0entity_visible\u3000", True, id="unicode-whitespace"),
        pytest.param(" members_only ", False, id="members-only"),
        pytest.param("ENTITY_VISIBLE", False, id="invalid-case"),
        pytest.param("entity_ visible", False, id="internal-space"),
        pytest.param("", False, id="empty"),
        pytest.param(None, False, id="null"),
    ],
)
async def test_catalog_workspace_access_matches_single_checks(db_session, access_mode, readable):
    user, _ = await _seed_entity_agents(db_session, 0)
    workspace = Workspace(
        entity_id=user.entity_id, name="Catalog workspace", settings={"access_mode": access_mode},
    )
    db_session.add(workspace)
    await db_session.flush()
    agents = [
        Agent(
            entity_id=user.entity_id, workspace_id=workspace.id, name=f"Scoped {visibility}",
            visibility=visibility,
        )
        for visibility in (Visibility.WORKSPACE, Visibility.ENTITY)
    ]
    db_session.add_all(agents)
    await db_session.flush()

    for agent in agents:
        assert await user_can_access_resource(
            db_session,
            descriptor=ResourceDescriptor.from_row(agent, ResourceType.AGENT),
            entity_id=user.entity_id,
            user_id=user.id,
            role=user.role,
            capability=Capability.VIEW,
        ) is readable

    visible = await _visible_agents(db_session, user)
    names = await resource_workspace_names(db_session, agents, user)
    assert ({agent.id for agent in visible}, names) == (
        {agent.id for agent in agents} if readable else set(),
        {workspace.id: workspace.name} if readable else {},
    )


@pytest.mark.asyncio
async def test_workspace_names_are_batched_without_disclosing_hidden_workspaces(db_session):
    one_entity_id = generate_ulid()
    one_user = User(
        entity_id=one_entity_id, email=f"workspace-name-one-{generate_ulid()}@test.com",
        password_hash=hash_password("pass123"), role="member", status="active",
    )
    one_workspace = Workspace(entity_id=one_entity_id, name="Readable one")
    db_session.add_all([one_user, one_workspace])
    await db_session.flush()
    db_session.add(WorkspaceStaff(
        workspace_id=one_workspace.id, user_id=one_user.id, role="viewer", status="active",
    ))
    await db_session.flush()
    db_session.expunge_all()
    one_statements, one_listener = _capture_sql(db_session)
    try:
        one_names = await resource_workspace_names(
            db_session, [Agent(entity_id=one_entity_id, workspace_id=one_workspace.id, name="One")], one_user,
        )
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", one_listener)
    assert one_names == {one_workspace.id: "Readable one"}

    many_entity_id = generate_ulid()
    many_user = User(
        entity_id=many_entity_id, email=f"workspace-name-many-{generate_ulid()}@test.com",
        password_hash=hash_password("pass123"), role="member", status="active",
    )
    many_workspaces = [Workspace(entity_id=many_entity_id, name=f"Workspace {index}") for index in range(21)]
    db_session.add_all([many_user, *many_workspaces])
    await db_session.flush()
    db_session.add(WorkspaceStaff(
        workspace_id=many_workspaces[0].id, user_id=many_user.id, role="viewer", status="active",
    ))
    await db_session.flush()
    db_session.expunge_all()
    many_statements, many_listener = _capture_sql(db_session)
    try:
        many_names = await resource_workspace_names(
            db_session,
            [Agent(entity_id=many_entity_id, workspace_id=workspace.id, name=workspace.name) for workspace in many_workspaces],
            many_user,
        )
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", many_listener)

    assert many_names == {many_workspaces[0].id: "Workspace 0"}
    assert len(many_statements) <= len(one_statements) + 1, (len(one_statements), len(many_statements))


@pytest.mark.asyncio
async def test_visible_agent_catalog_query_count_is_constant_as_agent_count_grows(db_session):
    one_user, one_agents = await _seed_entity_agents(db_session, 1)
    one_count, one_visible = await _catalog_query_count(db_session, one_user)

    many_user, many_agents = await _seed_entity_agents(db_session, 151)
    many_count, many_visible = await _catalog_query_count(db_session, many_user)

    assert {agent.id for agent in one_visible} == {agent.id for agent in one_agents}
    assert {agent.id for agent in many_visible} == {agent.id for agent in many_agents}
    assert many_count <= one_count + 2, (one_count, many_count)
