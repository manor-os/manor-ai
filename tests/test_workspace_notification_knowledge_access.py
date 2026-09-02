"""Workspace access must bound notifications and Knowledge folders together."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import packages.core.database as db_module
from packages.core.models.notification import Notification
from packages.core.models.staff import Staff, StaffRole
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.permissions import Permission
from packages.core.ai.runtime.manor_actions import runtime_manor_list_notifications
from packages.core.services.notify import notify
from packages.core.services.workspace_access import user_readable_workspace_ids
from packages.core.strategist import service as strategist_service
from packages.core.strategist.proposal import Proposal
from tests.test_document_permissions import _auth, _create_entity_user


async def _me(client: AsyncClient, headers: dict[str, str]) -> dict:
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_restricted_staff_cannot_see_workspace_notifications_or_knowledge_folder(
    client: AsyncClient,
    db_session,
) -> None:
    owner_headers = await _auth(client, "workspace_surface_owner")
    owner = await _me(client, owner_headers)
    created = await client.post(
        "/api/v1/workspaces",
        headers=owner_headers,
        json={"name": "Private Launch"},
    )
    assert created.status_code == 201, created.text
    workspace_data = created.json()

    restricted = await _create_entity_user(
        owner["entity_id"],
        "workspace_surface_restricted",
        role="admin",
    )
    role = StaffRole(
        entity_id=owner["entity_id"],
        name="Knowledge only",
        permissions=[Permission.DOCS_READ.value],
        status="active",
    )
    db_session.add(role)
    await db_session.flush()
    db_session.add(Staff(
        entity_id=owner["entity_id"],
        kind="employee",
        name="Restricted staff",
        email="workspace_surface_restricted@test.com",
        user_id=restricted["id"],
        role_id=role.id,
        status="active",
    ))
    db_session.add_all([
        Notification(
            entity_id=owner["entity_id"],
            user_id=restricted["id"],
            type="system",
            title="Entity notice",
        ),
        Notification(
            entity_id=owner["entity_id"],
            user_id=restricted["id"],
            type="proposal",
            title="Historical private proposal",
            meta={"workspace_id": workspace_data["id"]},
        ),
    ])
    await db_session.commit()

    await notify(
        entity_id=owner["entity_id"],
        user_id=restricted["id"],
        type="task_failed",
        title="Private task failed",
        workspace_id=workspace_data["id"],
    )

    workspace = (await db_session.execute(
        select(Workspace).where(Workspace.id == workspace_data["id"])
    )).scalar_one()
    assert await user_readable_workspace_ids(
        db_session,
        entity_id=owner["entity_id"],
        user_id=restricted["id"],
        role="admin",
        workspace_ids={workspace.id},
    ) == set()
    await strategist_service._notify_proposal_users(
        workspace,
        Proposal(review_id="restricted-review", summary="Private work", tasks=[]),
        [],
        items=[],
        auto_approved=False,
        auto_approved_action_key=None,
    )

    notifications = await client.get(
        "/api/v1/notifications",
        headers=restricted["headers"],
    )
    assert notifications.status_code == 200, notifications.text
    assert [item["title"] for item in notifications.json()["items"]] == [
        "Entity notice"
    ]

    runtime_notifications = json.loads(await runtime_manor_list_notifications(
        db_session,
        entity_id=owner["entity_id"],
        user_id=restricted["id"],
    ))
    assert [item["title"] for item in runtime_notifications["notifications"]] == [
        "Entity notice"
    ]

    folder_tree = await client.get(
        "/api/v1/documents/folder-tree",
        headers=restricted["headers"],
    )
    assert folder_tree.status_code == 200, folder_tree.text
    assert workspace_data["artifact_folder_id"] not in {
        folder["id"] for folder in folder_tree.json()
    }

    async with db_module.async_session() as db:
        db.add(WorkspaceStaff(
            workspace_id=workspace_data["id"],
            staff_id=None,
            user_id=restricted["id"],
            role="viewer",
            status="active",
        ))
        await db.commit()

    notifications_after_membership = await client.get(
        "/api/v1/notifications",
        headers=restricted["headers"],
    )
    assert notifications_after_membership.status_code == 200
    assert "Historical private proposal" in {
        item["title"] for item in notifications_after_membership.json()["items"]
    }

    folder_tree_after_membership = await client.get(
        "/api/v1/documents/folder-tree",
        headers=restricted["headers"],
    )
    assert workspace_data["artifact_folder_id"] in {
        folder["id"] for folder in folder_tree_after_membership.json()
    }
