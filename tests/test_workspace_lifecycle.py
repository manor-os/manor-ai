"""E2E tests: workspace soft-delete / restore / purge lifecycle."""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient


async def _register(
    client: AsyncClient,
    username: str = "lifecycle_user",
) -> tuple[str, dict]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Lifecycle Corp",
        },
    )
    token = resp.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_delete_is_soft_default(client: AsyncClient):
    """DELETE /workspaces/{id} should soft-delete (not hard-delete) — the
    workspace must still be findable in /workspaces/trash/list."""
    _, headers = await _register(client, "lifecycle_soft")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Doomed"},
    )
    ws_id = create.json()["id"]

    delete = await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert delete.status_code == 204

    # Default list should not see it
    listed = await client.get("/api/v1/workspaces", headers=headers)
    assert listed.status_code == 200
    assert all(w["id"] != ws_id for w in listed.json())

    # Trash should
    trash = await client.get("/api/v1/workspaces/trash/list", headers=headers)
    assert trash.status_code == 200
    trashed_ids = [w["id"] for w in trash.json()]
    assert ws_id in trashed_ids
    assert trash.json()[0]["deleted_at"] is not None


@pytest.mark.asyncio
async def test_deleted_workspace_chat_is_not_accessible(client: AsyncClient):
    _, headers = await _register(client, "lifecycle_chat_deleted")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Muted"},
    )
    ws_id = create.json()["id"]

    delete = await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert delete.status_code == 204

    listed = await client.get(f"/api/v1/workspaces/{ws_id}/chat/messages", headers=headers)
    posted = await client.post(
        f"/api/v1/workspaces/{ws_id}/chat/messages",
        headers=headers,
        json={"body": "should not revive deleted workspace"},
    )

    assert listed.status_code == 404
    assert posted.status_code == 404


@pytest.mark.asyncio
async def test_paused_workspace_still_accepts_direct_user_chat(client: AsyncClient):
    _, headers = await _register(client, "lifecycle_chat_paused")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Paused but conversational"},
    )
    ws_id = create.json()["id"]

    pause = await client.post(f"/api/v1/workspaces/{ws_id}/pause", headers=headers)
    assert pause.status_code == 200

    streamed = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        data={
            "message": "Summarize my current plan without starting new work.",
            "workspace_context": "true",
            "workspace_id": ws_id,
        },
        timeout=15.0,
    )

    assert streamed.status_code == 200
    messages = (
        await client.get(
            f"/api/v1/workspaces/{ws_id}/chat/messages",
            headers=headers,
        )
    ).json()
    assert any(
        message["author_kind"] == "user"
        and "Summarize my current plan" in message["body"]
        for message in messages
    )


@pytest.mark.asyncio
async def test_deleted_workspace_wiki_index_fails_closed_for_workspace_and_knowledge_net(
    client: AsyncClient,
    db_session,
    tmp_path,
):
    """Knowledge graph scope must not expose a soft-deleted Workspace's net."""
    from packages.core.config import get_settings
    from packages.core.models.document import DocumentGroup

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    old_mode = settings.DEPLOYMENT_MODE
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    settings.DEPLOYMENT_MODE = "oss"
    try:
        _, headers = await _register(client, "lifecycle_deleted_wiki")
        created = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Deleted Knowledge Workspace"},
        )
        assert created.status_code == 201
        workspace_id = created.json()["id"]
        group = DocumentGroup(
            entity_id=created.json()["entity_id"],
            workspace_id=workspace_id,
            name="Deleted Workspace Knowledge",
        )
        db_session.add(group)
        await db_session.commit()

        deleted = await client.delete(f"/api/v1/workspaces/{workspace_id}", headers=headers)
        assert deleted.status_code == 204

        workspace_scope = await client.get(
            "/api/v1/fs/wiki-index",
            headers=headers,
            params={"workspace_id": workspace_id},
        )
        net_scope = await client.get(
            "/api/v1/fs/wiki-index",
            headers=headers,
            params={"group_id": group.id},
        )

        assert workspace_scope.status_code == 404
        assert net_scope.status_code == 404
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled
        settings.DEPLOYMENT_MODE = old_mode


@pytest.mark.asyncio
async def test_deleted_workspace_hides_provenance_documents_without_hiding_entity_documents(
    client: AsyncClient,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """A soft-deleted Workspace must also hide its durable artifact projection."""
    from apps.api.routers import documents as documents_router
    from packages.core.config import get_settings
    from packages.core.models.document import Document

    settings = get_settings()
    monkeypatch.setattr(documents_router, "settings", settings)
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    old_mode = settings.DEPLOYMENT_MODE
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    settings.DEPLOYMENT_MODE = "oss"
    try:
        _, headers = await _register(client, "lifecycle_deleted_document")
        create = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "Deleted document provenance"},
        )
        assert create.status_code == 201
        workspace_id = create.json()["id"]

        artifact = await client.post(
            "/api/v1/documents/upload?visibility=entity",
            headers=headers,
            files={"file": ("workspace-artifact.md", b"workspace artifact", "text/markdown")},
        )
        entity_document = await client.post(
            "/api/v1/documents/upload?visibility=entity",
            headers=headers,
            files={"file": ("entity-document.md", b"entity document", "text/markdown")},
        )
        assert artifact.status_code == 201, artifact.text
        assert entity_document.status_code == 201, entity_document.text
        artifact_id = artifact.json()["id"]
        entity_document_id = entity_document.json()["id"]

        document = await db_session.get(Document, artifact_id)
        assert document is not None
        document.metadata_ = {"origin": {"workspace_id": workspace_id}}
        await db_session.commit()

        for doc_id in (artifact_id, entity_document_id):
            assert (await client.get(f"/api/v1/documents/{doc_id}", headers=headers)).status_code == 200
            assert (await client.get(f"/api/v1/documents/{doc_id}/content", headers=headers)).status_code == 200
            assert (await client.get(f"/api/v1/documents/{doc_id}/download", headers=headers)).status_code == 200

        delete = await client.delete(f"/api/v1/workspaces/{workspace_id}", headers=headers)
        assert delete.status_code == 204

        for suffix in ("", "/content", "/download"):
            denied = await client.get(f"/api/v1/documents/{artifact_id}{suffix}", headers=headers)
            assert denied.status_code == 404, denied.text
            allowed = await client.get(f"/api/v1/documents/{entity_document_id}{suffix}", headers=headers)
            assert allowed.status_code == 200, allowed.text

        listed = await client.get("/api/v1/documents", headers=headers)
        assert listed.status_code == 200
        listed_ids = {item["id"] for item in listed.json()["items"]}
        assert artifact_id not in listed_ids
        assert entity_document_id in listed_ids
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled
        settings.DEPLOYMENT_MODE = old_mode


@pytest.mark.asyncio
async def test_restore_brings_workspace_back(client: AsyncClient):
    _, headers = await _register(client, "lifecycle_restore")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Saved"},
    )
    ws_id = create.json()["id"]

    await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)

    restore = await client.post(
        f"/api/v1/workspaces/{ws_id}/restore",
        headers=headers,
    )
    assert restore.status_code == 200
    assert restore.json()["id"] == ws_id
    assert restore.json()["deleted_at"] is None

    # Now visible in default list, gone from trash
    listed = await client.get("/api/v1/workspaces", headers=headers)
    assert any(w["id"] == ws_id for w in listed.json())
    trash = await client.get("/api/v1/workspaces/trash/list", headers=headers)
    assert all(w["id"] != ws_id for w in trash.json())


@pytest.mark.asyncio
async def test_restore_requeues_only_workspace_deleted_embedding_rows(
    client: AsyncClient,
    db_session,
    monkeypatch,
):
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, VectorStatus
    from packages.core.tasks.ai_tasks import process_document_embeddings

    _, headers = await _register(client, "lifecycle_restore_embeddings")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Restore indexing"},
    )
    assert create.status_code == 201, create.text
    workspace = create.json()
    document = Document(
        id=generate_ulid(),
        entity_id=workspace["entity_id"],
        name="blocked-until-restore.md",
        folder_id=workspace["artifact_folder_id"],
        file_type="md",
        mime_type="text/markdown",
        vector_status=VectorStatus.PENDING,
        metadata_={
            "indexing": {
                "step": "blocked",
                "blocked_reason": "workspace_deleted",
                "blocked_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    db_session.add(document)
    await db_session.commit()

    delete = await client.delete(
        f"/api/v1/workspaces/{workspace['id']}",
        headers=headers,
    )
    assert delete.status_code == 204, delete.text
    dispatched: list[str] = []
    monkeypatch.setattr(
        process_document_embeddings,
        "delay",
        lambda document_id: dispatched.append(document_id),
    )

    restore = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/restore",
        headers=headers,
    )

    assert restore.status_code == 200, restore.text
    await db_session.refresh(document)
    assert document.vector_status == VectorStatus.PENDING
    assert document.metadata_["indexing"]["step"] == "queued"
    assert "blocked_reason" not in document.metadata_["indexing"]
    assert "blocked_at" not in document.metadata_["indexing"]
    assert dispatched == [document.id]


@pytest.mark.asyncio
async def test_delete_restore_syncs_runtime_schedules(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.services.scheduler_service import claim_job_run

    _, headers = await _register(client, "lifecycle_runtime")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": "Runtime Restored",
            "heartbeat_enabled": True,
            "heartbeat_cadence": "daily",
        },
    )
    ws_id = create.json()["id"]
    runtime_job_id = f"sr:{ws_id}"
    occurrence_key = "restore-occurrence"

    before_delete = (
        (await db_session.execute(select(ScheduledJob.job_id).where(ScheduledJob.workspace_id == ws_id)))
        .scalars()
        .all()
    )
    run, claimed = await claim_job_run(
        db_session,
        runtime_job_id,
        "completed",
        idempotency_key=occurrence_key,
    )
    run_id = run.id
    await db_session.commit()
    delete = await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
    db_session.expire_all()
    after_delete = (
        (await db_session.execute(select(ScheduledJob).where(ScheduledJob.workspace_id == ws_id))).scalars().all()
    )
    deleted_run = await db_session.get(ScheduledJobRun, run_id)
    restore = await client.post(f"/api/v1/workspaces/{ws_id}/restore", headers=headers)
    db_session.expire_all()
    after_restore = (
        (await db_session.execute(select(ScheduledJob.job_id).where(ScheduledJob.workspace_id == ws_id)))
        .scalars()
        .all()
    )
    restored_run, restored_claimed = await claim_job_run(
        db_session,
        runtime_job_id,
        "completed",
        idempotency_key=occurrence_key,
    )

    assert {f"sr:{ws_id}", f"oe:{ws_id}", f"cie:{ws_id}"} <= set(before_delete)
    assert claimed is True
    assert delete.status_code == 204
    assert after_delete == []
    assert deleted_run is None
    assert restore.status_code == 200
    assert {f"sr:{ws_id}", f"oe:{ws_id}", f"cie:{ws_id}"} <= set(after_restore)
    assert restored_claimed is True
    assert restored_run.id != run_id


@pytest.mark.asyncio
async def test_pause_pauses_workspace_automations(client: AsyncClient, db_session):
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workflow import WorkflowBinding

    _, headers = await _register(client, "lifecycle_pause_automations")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Pause Automations"},
    )
    ws_id = create.json()["id"]
    entity_id = create.json()["entity_id"]

    job_pk = generate_ulid()
    unrelated_job_pk = generate_ulid()
    automation_binding_id = generate_ulid()
    manual_binding_id = generate_ulid()
    job = ScheduledJob(
        id=job_pk,
        job_id=f"custom:{ws_id}",
        entity_id=entity_id,
        workspace_id=ws_id,
        name="Workspace automation",
        schedule_kind="every",
        every_seconds=60,
        enabled=True,
    )
    unrelated_job = ScheduledJob(
        id=unrelated_job_pk,
        job_id=f"global:{ws_id}",
        entity_id=entity_id,
        workspace_id=None,
        name="Entity automation",
        schedule_kind="every",
        every_seconds=60,
        enabled=True,
    )
    automation_binding = WorkflowBinding(
        id=automation_binding_id,
        entity_id=entity_id,
        workflow_id=generate_ulid(),
        workspace_id=ws_id,
        name="Workspace event automation",
        trigger_type="workspace_event",
        enabled=True,
        status="active",
    )
    manual_binding = WorkflowBinding(
        id=manual_binding_id,
        entity_id=entity_id,
        workflow_id=generate_ulid(),
        workspace_id=ws_id,
        name="Attached flow",
        trigger_type="manual",
        enabled=True,
        status="active",
    )
    db_session.add_all([job, unrelated_job, automation_binding, manual_binding])
    await db_session.commit()

    before_ids = set((await db_session.execute(
        select(ScheduledJob.id).where(ScheduledJob.workspace_id == ws_id)
    )).scalars().all())
    pause = await client.post(f"/api/v1/workspaces/{ws_id}/pause", headers=headers)
    assert pause.status_code == 200

    db_session.expire_all()
    workspace_jobs = list((await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.workspace_id == ws_id)
    )).scalars().all())
    refreshed_automation_binding = await db_session.get(WorkflowBinding, automation_binding_id)
    refreshed_manual_binding = await db_session.get(WorkflowBinding, manual_binding_id)
    refreshed_unrelated_job = await db_session.get(ScheduledJob, unrelated_job_pk)

    assert {row.id for row in workspace_jobs} == before_ids
    assert workspace_jobs and all(row.enabled is False for row in workspace_jobs)
    assert refreshed_automation_binding is not None
    assert refreshed_automation_binding.enabled is False
    assert refreshed_automation_binding.status == "paused"
    assert refreshed_manual_binding is not None
    assert refreshed_manual_binding.enabled is True
    assert refreshed_manual_binding.status == "active"
    assert refreshed_unrelated_job is not None
    assert refreshed_unrelated_job.enabled is True


@pytest.mark.asyncio
async def test_delete_removes_workspace_automations(client: AsyncClient, db_session):
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workflow import WorkflowBinding

    _, headers = await _register(client, "lifecycle_delete_automations")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Delete Automations"},
    )
    ws_id = create.json()["id"]
    entity_id = create.json()["entity_id"]

    job_pk = generate_ulid()
    unrelated_job_pk = generate_ulid()
    automation_binding_id = generate_ulid()
    job = ScheduledJob(
        id=job_pk,
        job_id=f"delete-custom:{ws_id}",
        entity_id=entity_id,
        workspace_id=ws_id,
        name="Delete with workspace",
        schedule_kind="every",
        every_seconds=60,
        enabled=True,
    )
    unrelated_job = ScheduledJob(
        id=unrelated_job_pk,
        job_id=f"keep-global:{ws_id}",
        entity_id=entity_id,
        workspace_id=None,
        name="Keep entity automation",
        schedule_kind="every",
        every_seconds=60,
        enabled=True,
    )
    automation_binding = WorkflowBinding(
        id=automation_binding_id,
        entity_id=entity_id,
        workflow_id=generate_ulid(),
        workspace_id=ws_id,
        name="Delete event automation",
        trigger_type="workspace_event",
        enabled=True,
        status="active",
    )
    db_session.add_all([job, unrelated_job, automation_binding])
    await db_session.commit()

    delete = await client.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert delete.status_code == 204

    db_session.expire_all()
    workspace_jobs = list((await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.workspace_id == ws_id)
    )).scalars().all())
    deleted_binding = await db_session.get(WorkflowBinding, automation_binding_id)
    kept_job = await db_session.get(ScheduledJob, unrelated_job_pk)

    assert workspace_jobs == []
    assert deleted_binding is None
    assert kept_job is not None
    assert kept_job.enabled is True


@pytest.mark.asyncio
async def test_restore_404_when_not_in_trash(client: AsyncClient):
    """POST /restore on a workspace that isn't in the trash returns 404."""
    _, headers = await _register(client, "lifecycle_404")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Fresh"},
    )
    ws_id = create.json()["id"]

    restore = await client.post(
        f"/api/v1/workspaces/{ws_id}/restore",
        headers=headers,
    )
    assert restore.status_code == 404


@pytest.mark.asyncio
async def test_grace_days_endpoint(client: AsyncClient):
    """The UI hits this to render the "X days left" copy."""
    _, headers = await _register(client, "lifecycle_grace")
    resp = await client.get(
        "/api/v1/workspaces/trash/grace-days",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["grace_days"], int)
    assert body["grace_days"] > 0


@pytest.mark.asyncio
async def test_purge_only_after_grace_window(
    client: AsyncClient,
    db_session,
):
    """``list_workspaces_due_for_purge`` should respect the cutoff —
    workspaces deleted recently are NOT yet eligible, ones deleted
    long ago ARE."""
    from packages.core.services.entity_service import (
        list_workspaces_due_for_purge,
        soft_delete_workspace,
    )

    _, headers = await _register(client, "lifecycle_purge")
    create_recent = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Recent"},
    )
    create_old = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Old"},
    )
    recent_id = create_recent.json()["id"]
    old_id = create_old.json()["id"]
    entity_id = create_recent.json()["entity_id"]

    await soft_delete_workspace(db_session, recent_id, entity_id)
    await soft_delete_workspace(db_session, old_id, entity_id)

    # Backdate the "old" workspace's deleted_at past the grace window.
    from sqlalchemy import update as sa_update
    from packages.core.models.workspace import Workspace

    await db_session.execute(
        sa_update(Workspace)
        .where(Workspace.id == old_id)
        .values(deleted_at=datetime.now(timezone.utc) - timedelta(days=45))
    )
    await db_session.flush()

    due = await list_workspaces_due_for_purge(db_session, grace_days=30)
    due_ids = {ws.id for ws in due}
    assert old_id in due_ids, "30+ day old soft-delete should be purgeable"
    assert recent_id not in due_ids, "Just-deleted workspace should NOT be purgeable yet"


@pytest.mark.asyncio
async def test_purge_workspace_cascades(client: AsyncClient, db_session):
    """``purge_workspace`` (the hard delete) should remove the row and
    all workspace-scoped tasks/conversations/etc."""
    from packages.core.services.entity_service import (
        purge_workspace,
        soft_delete_workspace,
    )
    from packages.core.models.billing import (
        CreditReservation,
        CreditUsageAllocation,
        CreditUsageLog,
    )
    from packages.core.models.automation_revision import AutomationRevision
    from packages.core.models.base import generate_ulid
    from packages.core.models.consolidation_report import ConsolidationReport
    from packages.core.models.experiment import Experiment
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
    from packages.core.models.notification import (
        Notification,
        NotificationDelivery,
        NotificationOutboxEvent,
    )
    from packages.core.models.channel_pairing import ChannelPairingCode
    from packages.core.models.custom_field import CustomFieldDefinition
    from packages.core.models.participant import (
        HumanCommitment,
        HumanContribution,
        ParticipantProfile,
    )
    from packages.core.models.permission import (
        PendingStatus,
        ResourceGrantPending,
        ResourceType,
    )
    from packages.core.models.scheduler import ScheduledJob, ScheduledJobRun
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.models.goal import Goal, GoalMeasurement, GoalTaskLink
    from packages.core.models.chat_feedback import ChatMessageFeedback
    from packages.core.models.task import Conversation, Message, Task
    from packages.core.models.workflow import (
        WorkflowActionGrant,
        WorkflowBinding,
        WorkflowProject,
        WorkflowRun,
    )
    from packages.core.models.workspace import (
        Agent,
        Workspace,
        WorkspaceOperationDraft,
        WorkspaceWorkBatch,
    )
    from packages.core.models.workspace_stat import (
        WorkspaceStat,
        WorkspaceStatObservation,
    )
    from packages.core.models.runtime_learning import (
        AgentLearningCandidate,
        RuntimeEvidence,
        RuntimeEventLog,
    )
    from packages.core.models.runtime_run import (
        RuntimeOutboxEvent,
        RuntimeRun,
        SandboxInstance,
        SandboxReservation,
    )
    from packages.core.models.proposal import ProposalItemRecord, ProposalRecord
    from packages.core.models.review_run import ReviewRun
    from packages.core.models.usage import TokenUsageLog, ToolCallLog
    from packages.core.models.workspace_event import WorkspaceEvent
    from packages.core.services.marketplace_resource_links import (
        RELATIONSHIP_INSTALLED_COMPONENT,
        RESOURCE_AGENT,
        RESOURCE_WORKSPACE_BLUEPRINT,
        SCOPE_WORKSPACE,
        record_marketplace_resource_link,
    )
    from sqlalchemy import select

    _, headers = await _register(client, "lifecycle_cascade")
    create = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Cascade"},
    )
    ws_id = create.json()["id"]
    entity_id = create.json()["entity_id"]
    preserved_workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Stat preservation workspace"},
    )
    assert preserved_workspace.status_code == 201, preserved_workspace.text
    preserved_workspace_id = preserved_workspace.json()["id"]
    current_user = (await client.get("/api/v1/auth/me", headers=headers)).json()

    # Create a Task scoped to this workspace
    task = Task(
        entity_id=entity_id,
        workspace_id=ws_id,
        title="Pre-purge task",
        status="pending",
    )
    preserved_task = Task(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        title="Preserved task",
        status="pending",
    )
    scheduled_job = ScheduledJob(
        entity_id=entity_id,
        workspace_id=ws_id,
        job_id=f"purge:{generate_ulid()}",
        name="Purge job",
    )
    preserved_scheduled_job = ScheduledJob(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        job_id=f"preserved:{generate_ulid()}",
        name="Preserved job",
    )
    conversation = Conversation(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=ws_id,
        user_id=current_user["id"],
        channel="web",
        scope="channel",
    )
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation.id,
        role="assistant",
        content="Workspace preview that must be purged",
        author_kind="agent",
        message_kind="text",
    )
    feedback = ChatMessageFeedback(
        entity_id=entity_id,
        user_id=current_user["id"],
        conversation_id=conversation.id,
        message_id=message.id,
        target_kind="response",
        target_id=message.id,
        rating="up",
        content_preview="Workspace preview that must be purged",
        mutation_sequence=1,
    )
    feedback_evidence = RuntimeEvidence(
        entity_id=entity_id,
        workspace_id=ws_id,
        user_id=current_user["id"],
        conversation_id=conversation.id,
        message_id=message.id,
        evidence_type="task_completion_feedback",
        source="workspace_chat",
        status="succeeded",
        summary="Workspace feedback evidence that must be purged",
        details={"rating": "up"},
        metrics={"helpful": 1},
    )
    binding_id = generate_ulid()
    binding = WorkflowBinding(
        id=binding_id,
        entity_id=entity_id,
        workflow_id=generate_ulid(),
        workspace_id=ws_id,
        name="Pre-purge flow attachment",
        trigger_type="manual",
        enabled=True,
        status="active",
    )
    workflow_run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=binding.workflow_id,
        entity_id=entity_id,
        workspace_id=ws_id,
        status="completed",
    )
    preserved_workflow_run = WorkflowRun(
        id=generate_ulid(),
        workflow_id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        status="completed",
    )
    workflow_project = WorkflowProject(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=ws_id,
        project_type="purge_project",
        state={},
        last_run_id=workflow_run.id,
        created_by="lifecycle-user",
    )
    preserved_workflow_project = WorkflowProject(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        project_type="preserved_project",
        state={},
        last_run_id=preserved_workflow_run.id,
        created_by="lifecycle-user",
    )
    now = datetime.now(timezone.utc)
    workflow_action_grant = WorkflowActionGrant(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=ws_id,
        workflow_run_id=workflow_run.id,
        project_id=workflow_project.id,
        grant_type="publish",
        scope={},
        granted_by="lifecycle-user",
        granted_at=now,
        expires_at=now + timedelta(hours=1),
    )
    preserved_workflow_action_grant = WorkflowActionGrant(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        workflow_run_id=preserved_workflow_run.id,
        project_id=preserved_workflow_project.id,
        grant_type="publish",
        scope={},
        granted_by="lifecycle-user",
        granted_at=now,
        expires_at=now + timedelta(hours=1),
    )
    agent = Agent(
        entity_id=entity_id,
        workspace_id=ws_id,
        name="Blueprint Agent",
        slug="blueprint-agent",
        system_prompt="Operate this Workspace.",
        status="active",
    )
    skill = Skill(
        entity_id=entity_id,
        workspace_id=ws_id,
        name="Blueprint Skill",
        slug="blueprint-skill",
        system_prompt="Perform the Workspace procedure.",
        status="active",
    )
    shared_skill = Skill(
        entity_id=entity_id,
        workspace_id=None,
        name="Shared Skill",
        slug="shared-skill",
        system_prompt="Remain entity-scoped.",
        status="active",
    )
    workspace_profile = ParticipantProfile(
        entity_id=entity_id,
        user_id=current_user["id"],
        workspace_id=ws_id,
        roles=["workspace_owner"],
    )
    preserved_profile = ParticipantProfile(
        entity_id=entity_id,
        user_id=current_user["id"],
        workspace_id=preserved_workspace_id,
        roles=["workspace_viewer"],
    )
    entity_profile = ParticipantProfile(
        entity_id=entity_id,
        user_id=current_user["id"],
        workspace_id=None,
        roles=["entity_admin"],
    )
    workspace_commitment = HumanCommitment(
        entity_id=entity_id,
        workspace_id=ws_id,
        request_kind="review",
        source_kind="execution_step",
        source_id=f"step:{ws_id}",
        participant_id=current_user["id"],
    )
    preserved_commitment = HumanCommitment(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        request_kind="review",
        source_kind="execution_step",
        source_id=f"step:{preserved_workspace_id}",
        participant_id=current_user["id"],
    )
    workspace_contribution = HumanContribution(
        entity_id=entity_id,
        workspace_id=ws_id,
        participant_id=current_user["id"],
        kind="choice",
        target_kind="task",
        target_id=f"task:{ws_id}",
        diff_summary={"fields": ["status"]},
    )
    preserved_contribution = HumanContribution(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        participant_id=current_user["id"],
        kind="choice",
        target_kind="task",
        target_id=f"task:{preserved_workspace_id}",
        diff_summary={"fields": ["status"]},
    )
    workspace_custom_field = CustomFieldDefinition(
        entity_id=entity_id,
        workspace_id=ws_id,
        name="purge_priority",
        display_name="Purge priority",
        field_type="number",
        target="task",
    )
    preserved_custom_field = CustomFieldDefinition(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        name="preserved_priority",
        display_name="Preserved priority",
        field_type="number",
        target="task",
    )
    entity_custom_field = CustomFieldDefinition(
        entity_id=entity_id,
        workspace_id=None,
        name="entity_priority",
        display_name="Entity priority",
        field_type="number",
        target="task",
    )
    notification = Notification(
        entity_id=entity_id,
        user_id="purge-notification-user",
        workspace_id=ws_id,
        type="system",
        title="Workspace notification",
        dispatch_status="pending",
    )
    pairing_code = ChannelPairingCode(
        code="PURGE2",
        entity_id=entity_id,
        user_id=current_user["id"],
        workspace_id=ws_id,
        channel_type="telegram",
        expires_at=now + timedelta(hours=1),
    )
    preserved_pairing_code = ChannelPairingCode(
        code="KEEP22",
        entity_id=entity_id,
        user_id=current_user["id"],
        workspace_id=preserved_workspace_id,
        channel_type="telegram",
        expires_at=now + timedelta(hours=1),
    )
    legacy_notification = Notification(
        entity_id=entity_id,
        user_id="purge-notification-user",
        type="system",
        title="Legacy workspace notification",
        meta={"workspace_id": ws_id},
        dispatch_status="pending",
    )
    unrelated_notification = Notification(
        entity_id=entity_id,
        user_id="purge-notification-user",
        type="system",
        title="Entity notification",
    )
    workspace_reservation = CreditReservation(
        entity_id=entity_id,
        workspace_id=ws_id,
        amount_credits=7,
        source_kind="lifecycle_purge",
        source_id=f"reservation:{ws_id}",
    )
    preserved_reservation = CreditReservation(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        amount_credits=7,
        source_kind="lifecycle_purge",
        source_id=f"reservation:{preserved_workspace_id}",
    )
    workspace_usage = CreditUsageLog(
        entity_id=entity_id,
        workspace_id=ws_id,
        operation_id=f"usage:{ws_id}",
    )
    preserved_usage = CreditUsageLog(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        operation_id=f"usage:{preserved_workspace_id}",
    )
    workspace_token_usage = TokenUsageLog(
        entity_id=entity_id,
        workspace_id=ws_id,
        source="lifecycle_purge",
    )
    preserved_token_usage = TokenUsageLog(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        source="lifecycle_purge",
    )
    workspace_tool_log = ToolCallLog(
        entity_id=entity_id,
        workspace_id=ws_id,
        tool_name="lifecycle_purge",
        source="lifecycle_purge",
    )
    preserved_tool_log = ToolCallLog(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        tool_name="lifecycle_purge",
        source="lifecycle_purge",
    )
    workspace_runtime_event = RuntimeEventLog(
        entity_id=entity_id,
        workspace_id=ws_id,
        surface="workspace",
        profile="lifecycle_purge",
        principal_kind="system",
        event_type="purge_audit",
    )
    preserved_runtime_event = RuntimeEventLog(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        surface="workspace",
        profile="lifecycle_purge",
        principal_kind="system",
        event_type="purge_audit",
    )
    workspace_review_run = ReviewRun(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=ws_id,
        trigger_kind="human_requested",
        status="succeeded",
    )
    preserved_review_run = ReviewRun(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        trigger_kind="human_requested",
        status="succeeded",
    )
    workspace_proposal = ProposalRecord(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=ws_id,
        review_id=workspace_review_run.id,
        summary="Purge this proposal",
    )
    preserved_proposal = ProposalRecord(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        review_id=preserved_review_run.id,
        summary="Preserve this proposal",
    )
    workspace_proposal_item = ProposalItemRecord(
        entity_id=entity_id,
        workspace_id=ws_id,
        proposal_id=workspace_proposal.id,
        item_key="purge_item",
        kind="task",
        action_key="workspace.proposal.task",
    )
    preserved_proposal_item = ProposalItemRecord(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        proposal_id=preserved_proposal.id,
        item_key="preserved_item",
        kind="task",
        action_key="workspace.proposal.task",
    )
    workspace_hitl = HitlRequest(
        entity_id=entity_id,
        workspace_id=ws_id,
        origin_kind="step",
        dedup_key=f"purge-hitl:{ws_id}",
    )
    preserved_hitl = HitlRequest(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        origin_kind="step",
        dedup_key=f"purge-hitl:{preserved_workspace_id}",
    )
    workspace_automation_revision = AutomationRevision(
        entity_id=entity_id,
        workspace_id=ws_id,
        target_kind="scheduled_job",
        target_id=generate_ulid(),
        revision=1,
    )
    preserved_automation_revision = AutomationRevision(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        target_kind="scheduled_job",
        target_id=generate_ulid(),
        revision=1,
    )
    workspace_experiment = Experiment(
        entity_id=entity_id,
        workspace_id=ws_id,
        hypothesis="Purge this experiment",
    )
    preserved_experiment = Experiment(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        hypothesis="Preserve this experiment",
    )
    workspace_consolidation = ConsolidationReport(
        entity_id=entity_id,
        workspace_id=ws_id,
        review_id=workspace_review_run.id,
        domain="execution",
        status="complete",
        analyzer_version="lifecycle_purge",
        input_hash="a" * 64,
    )
    preserved_consolidation = ConsolidationReport(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        review_id=preserved_review_run.id,
        domain="execution",
        status="complete",
        analyzer_version="lifecycle_purge",
        input_hash="b" * 64,
    )
    workspace_runtime_run_id = generate_ulid()
    preserved_runtime_run_id = generate_ulid()
    workspace_sandbox_reservation_id = generate_ulid()
    preserved_sandbox_reservation_id = generate_ulid()
    workspace_sandbox_id = f"sandbox-{ws_id}"
    preserved_sandbox_id = f"sandbox-{preserved_workspace_id}"
    workspace_runtime_run = RuntimeRun(
        id=workspace_runtime_run_id,
        root_run_id=workspace_runtime_run_id,
        conversation_id=conversation.id,
        entity_id=entity_id,
        user_id=current_user["id"],
        workspace_id=ws_id,
        status="completed",
        execution_payload={},
    )
    preserved_runtime_run = RuntimeRun(
        id=preserved_runtime_run_id,
        root_run_id=preserved_runtime_run_id,
        conversation_id=generate_ulid(),
        entity_id=entity_id,
        user_id=current_user["id"],
        workspace_id=preserved_workspace_id,
        status="completed",
        execution_payload={},
    )
    workspace_sandbox_reservation = SandboxReservation(
        id=workspace_sandbox_reservation_id,
        runtime_run_id=workspace_runtime_run_id,
        root_run_id=workspace_runtime_run_id,
        tool_call_id=f"tool:{workspace_runtime_run_id}",
        entity_id=entity_id,
        user_id=current_user["id"],
        deadline_at=now + timedelta(hours=1),
        status="consumed",
    )
    preserved_sandbox_reservation = SandboxReservation(
        id=preserved_sandbox_reservation_id,
        runtime_run_id=preserved_runtime_run_id,
        root_run_id=preserved_runtime_run_id,
        tool_call_id=f"tool:{preserved_runtime_run_id}",
        entity_id=entity_id,
        user_id=current_user["id"],
        deadline_at=now + timedelta(hours=1),
        status="consumed",
    )
    workspace_sandbox_instance = SandboxInstance(
        sandbox_id=workspace_sandbox_id,
        reservation_id=workspace_sandbox_reservation_id,
        runtime_run_id=workspace_runtime_run_id,
        root_run_id=workspace_runtime_run_id,
        runner_id="lifecycle-runner",
    )
    preserved_sandbox_instance = SandboxInstance(
        sandbox_id=preserved_sandbox_id,
        reservation_id=preserved_sandbox_reservation_id,
        runtime_run_id=preserved_runtime_run_id,
        root_run_id=preserved_runtime_run_id,
        runner_id="lifecycle-runner",
    )
    workspace_runtime_outbox = RuntimeOutboxEvent(
        event_type="lifecycle.purge",
        aggregate_id=workspace_runtime_run_id,
        dedupe_key=f"lifecycle:{workspace_runtime_run_id}",
        payload={},
    )
    preserved_runtime_outbox = RuntimeOutboxEvent(
        event_type="lifecycle.purge",
        aggregate_id=preserved_runtime_run_id,
        dedupe_key=f"lifecycle:{preserved_runtime_run_id}",
        payload={},
    )
    workspace_operation_draft = WorkspaceOperationDraft(
        entity_id=entity_id,
        workspace_id=ws_id,
        created_by_user_id=current_user["id"],
        current_state={"source": "purge"},
    )
    preserved_operation_draft = WorkspaceOperationDraft(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        created_by_user_id=current_user["id"],
        current_state={"source": "preserve"},
    )
    workspace_work_batch = WorkspaceWorkBatch(
        entity_id=entity_id,
        workspace_id=ws_id,
        created_by_user_id=current_user["id"],
        summary="Purge work batch",
    )
    preserved_work_batch = WorkspaceWorkBatch(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        created_by_user_id=current_user["id"],
        summary="Preserved work batch",
    )
    db_session.add_all([
        task,
        preserved_task,
        scheduled_job,
        preserved_scheduled_job,
        conversation,
        message,
        binding,
        workflow_run,
        preserved_workflow_run,
        workflow_project,
        preserved_workflow_project,
        workflow_action_grant,
        preserved_workflow_action_grant,
        agent,
        skill,
        shared_skill,
        workspace_profile,
        preserved_profile,
        entity_profile,
        workspace_commitment,
        preserved_commitment,
        workspace_contribution,
        preserved_contribution,
        workspace_custom_field,
        preserved_custom_field,
        entity_custom_field,
        notification,
        pairing_code,
        preserved_pairing_code,
        legacy_notification,
        unrelated_notification,
        workspace_reservation,
        preserved_reservation,
        workspace_usage,
        preserved_usage,
        workspace_token_usage,
        preserved_token_usage,
        workspace_tool_log,
        preserved_tool_log,
        workspace_runtime_event,
        preserved_runtime_event,
        workspace_review_run,
        preserved_review_run,
        workspace_proposal,
        preserved_proposal,
        workspace_proposal_item,
        preserved_proposal_item,
        workspace_hitl,
        preserved_hitl,
        workspace_automation_revision,
        preserved_automation_revision,
        workspace_experiment,
        preserved_experiment,
        workspace_consolidation,
        preserved_consolidation,
        workspace_runtime_run,
        preserved_runtime_run,
        workspace_sandbox_reservation,
        preserved_sandbox_reservation,
        workspace_sandbox_instance,
        preserved_sandbox_instance,
        workspace_runtime_outbox,
        preserved_runtime_outbox,
        workspace_operation_draft,
        preserved_operation_draft,
        workspace_work_batch,
        preserved_work_batch,
    ])
    workspace_stat = WorkspaceStat(
        entity_id=entity_id,
        workspace_id=ws_id,
        key="purge_stat",
        name="Purge stat",
        collector_type="manual",
        collector_config={},
    )
    preserved_stat = WorkspaceStat(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        key="preserved_stat",
        name="Preserved stat",
        collector_type="manual",
        collector_config={},
    )
    goal = Goal(
        entity_id=entity_id,
        workspace_id=ws_id,
        title="Purge goal",
        metric_key="purge_metric",
        target_value=1,
        status="active",
    )
    preserved_goal = Goal(
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        title="Preserved goal",
        metric_key="preserved_metric",
        target_value=1,
        status="active",
    )
    entity_goal = Goal(
        entity_id=entity_id,
        workspace_id=None,
        title="Entity goal",
        metric_key="entity_metric",
        target_value=1,
        status="active",
    )
    db_session.add_all([
        workspace_stat,
        preserved_stat,
        goal,
        preserved_goal,
        entity_goal,
    ])
    await db_session.flush()
    workspace_operation_draft_id = workspace_operation_draft.id
    preserved_operation_draft_id = preserved_operation_draft.id
    workspace_work_batch_id = workspace_work_batch.id
    preserved_work_batch_id = preserved_work_batch.id
    workspace_usage_allocation = CreditUsageAllocation(
        entity_id=entity_id,
        usage_log_id=workspace_usage.id,
        grant_id=generate_ulid(),
        grant_kind="workspace_test",
        bucket="workspace_test",
        amount_credits=7,
        priority=1,
    )
    preserved_usage_allocation = CreditUsageAllocation(
        entity_id=entity_id,
        usage_log_id=preserved_usage.id,
        grant_id=generate_ulid(),
        grant_kind="workspace_test",
        bucket="workspace_test",
        amount_credits=7,
        priority=1,
    )
    db_session.add_all([workspace_usage_allocation, preserved_usage_allocation])
    workspace_event = WorkspaceEvent(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=ws_id,
        event_type="task.created",
        source_kind="task",
        source_id=task.id,
        actor_kind="system",
        idempotency_key="purge-workspace-event",
        occurred_at=now,
    )
    preserved_workspace_event = WorkspaceEvent(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        event_type="task.created",
        source_kind="task",
        source_id=preserved_task.id,
        actor_kind="system",
        idempotency_key="purge-preserved-workspace-event",
        occurred_at=now,
    )
    runtime_evidence = RuntimeEvidence(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=ws_id,
        task_id=task.id,
        evidence_type="task_run",
        source="test",
        status="succeeded",
        summary="Purge workspace evidence",
    )
    preserved_runtime_evidence = RuntimeEvidence(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        task_id=preserved_task.id,
        evidence_type="task_run",
        source="test",
        status="succeeded",
        summary="Preserved workspace evidence",
    )
    learning_candidate = AgentLearningCandidate(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=ws_id,
        agent_id=agent.id,
        candidate_type="rule",
        scope="workspace",
        title="Purge workspace candidate",
        summary="Derived from purged evidence",
        evidence_ids=[runtime_evidence.id],
    )
    preserved_learning_candidate = AgentLearningCandidate(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=preserved_workspace_id,
        candidate_type="rule",
        scope="workspace",
        title="Preserved workspace candidate",
        summary="Derived from preserved evidence",
        evidence_ids=[preserved_runtime_evidence.id],
    )
    db_session.add_all([
        workspace_event,
        preserved_workspace_event,
        runtime_evidence,
        preserved_runtime_evidence,
        learning_candidate,
        preserved_learning_candidate,
    ])
    workspace_stat_observation = WorkspaceStatObservation(
        stat_id=workspace_stat.id,
        entity_id=entity_id,
        workspace_id=ws_id,
        value=1,
        observed_at=datetime.now(timezone.utc),
        source="manual",
        idempotency_key="purge-stat-observation",
    )
    db_session.add(workspace_stat_observation)
    goal_measurement = GoalMeasurement(
        goal_id=goal.id,
        measured_at=datetime.now(timezone.utc),
        value=1,
        source="manual",
    )
    preserved_goal_measurement = GoalMeasurement(
        goal_id=preserved_goal.id,
        measured_at=datetime.now(timezone.utc),
        value=1,
        source="manual",
    )
    goal_task_link = GoalTaskLink(
        goal_id=goal.id,
        task_id=task.id,
        contribution="direct",
    )
    preserved_goal_task_link = GoalTaskLink(
        goal_id=preserved_goal.id,
        task_id=preserved_task.id,
        contribution="direct",
    )
    entity_goal_task_link = GoalTaskLink(
        goal_id=entity_goal.id,
        task_id=task.id,
        contribution="indirect",
    )
    db_session.add_all([
        goal_measurement,
        preserved_goal_measurement,
        goal_task_link,
        preserved_goal_task_link,
        entity_goal_task_link,
    ])
    scheduled_job_run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=scheduled_job.job_id,
        status="completed",
    )
    preserved_scheduled_job_run = ScheduledJobRun(
        id=generate_ulid(),
        job_id=preserved_scheduled_job.job_id,
        status="completed",
    )
    db_session.add_all([scheduled_job_run, preserved_scheduled_job_run])
    db_session.add_all([feedback, feedback_evidence])
    await db_session.flush()
    notification_outbox = NotificationOutboxEvent(
        notification_id=notification.id,
        payload={},
        available_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    legacy_outbox = NotificationOutboxEvent(
        notification_id=legacy_notification.id,
        payload={},
        available_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    notification_delivery = NotificationDelivery(
        notification_id=notification.id,
        entity_id=entity_id,
        user_id="purge-notification-user",
        channel_contact_id="purge-channel-contact",
        channel_type="telegram",
        status="sent",
    )
    db_session.add_all([
        notification_outbox,
        legacy_outbox,
        notification_delivery,
    ])
    skill_binding = AgentSkillBinding(
        agent_id=agent.id,
        skill_id=skill.id,
        status="active",
    )
    pending_workspace_grant = ResourceGrantPending(
        entity_id=entity_id,
        resource_type=ResourceType.WORKSPACE,
        resource_id=ws_id,
        requester_user_id=current_user["id"],
        requested_capabilities=["view"],
        status=PendingStatus.PENDING,
    )
    pending_agent_grant = ResourceGrantPending(
        entity_id=entity_id,
        resource_type=ResourceType.AGENT,
        resource_id=agent.id,
        requester_user_id=current_user["id"],
        requested_capabilities=["use"],
        status=PendingStatus.PENDING,
    )
    pending_preserved_workspace_grant = ResourceGrantPending(
        entity_id=entity_id,
        resource_type=ResourceType.WORKSPACE,
        resource_id=preserved_workspace_id,
        requester_user_id=current_user["id"],
        requested_capabilities=["view"],
        status=PendingStatus.PENDING,
    )
    db_session.add(skill_binding)
    db_session.add_all([
        pending_workspace_grant,
        pending_agent_grant,
        pending_preserved_workspace_grant,
    ])
    marketplace_link = await record_marketplace_resource_link(
        db_session,
        entity_id=entity_id,
        marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
        marketplace_resource_id="builtin:purge-regression",
        relationship=RELATIONSHIP_INSTALLED_COMPONENT,
        scope_type=SCOPE_WORKSPACE,
        scope_id=ws_id,
        local_resource_type=RESOURCE_AGENT,
        local_resource_id=agent.id,
        component_key="blueprint-agent-component",
    )
    agent_id = agent.id
    skill_id = skill.id
    shared_skill_id = shared_skill.id
    skill_binding_id = skill_binding.id
    marketplace_link_id = marketplace_link.id
    notification_id = notification.id
    legacy_notification_id = legacy_notification.id
    unrelated_notification_id = unrelated_notification.id
    notification_outbox_id = notification_outbox.id
    legacy_outbox_id = legacy_outbox.id
    notification_delivery_id = notification_delivery.id
    workspace_reservation_id = workspace_reservation.id
    preserved_reservation_id = preserved_reservation.id
    workspace_usage_id = workspace_usage.id
    preserved_usage_id = preserved_usage.id
    workspace_usage_allocation_id = workspace_usage_allocation.id
    preserved_usage_allocation_id = preserved_usage_allocation.id
    workspace_token_usage_id = workspace_token_usage.id
    preserved_token_usage_id = preserved_token_usage.id
    workspace_tool_log_id = workspace_tool_log.id
    preserved_tool_log_id = preserved_tool_log.id
    workspace_runtime_event_id = workspace_runtime_event.id
    preserved_runtime_event_id = preserved_runtime_event.id
    workspace_review_run_id = workspace_review_run.id
    preserved_review_run_id = preserved_review_run.id
    workspace_proposal_id = workspace_proposal.id
    preserved_proposal_id = preserved_proposal.id
    workspace_proposal_item_id = workspace_proposal_item.id
    preserved_proposal_item_id = preserved_proposal_item.id
    workspace_hitl_id = workspace_hitl.id
    preserved_hitl_id = preserved_hitl.id
    workspace_automation_revision_id = workspace_automation_revision.id
    preserved_automation_revision_id = preserved_automation_revision.id
    workspace_experiment_id = workspace_experiment.id
    preserved_experiment_id = preserved_experiment.id
    workspace_consolidation_id = workspace_consolidation.id
    preserved_consolidation_id = preserved_consolidation.id
    workspace_stat_id = workspace_stat.id
    preserved_stat_id = preserved_stat.id
    workspace_stat_observation_id = workspace_stat_observation.id
    goal_id = goal.id
    preserved_goal_id = preserved_goal.id
    entity_goal_id = entity_goal.id
    goal_measurement_at = goal_measurement.measured_at
    preserved_goal_measurement_at = preserved_goal_measurement.measured_at
    goal_task_link_id = (goal_task_link.goal_id, goal_task_link.task_id)
    preserved_goal_task_link_id = (
        preserved_goal_task_link.goal_id,
        preserved_goal_task_link.task_id,
    )
    entity_goal_task_link_id = (
        entity_goal_task_link.goal_id,
        entity_goal_task_link.task_id,
    )
    workspace_event_id = workspace_event.id
    preserved_workspace_event_id = preserved_workspace_event.id
    runtime_evidence_id = runtime_evidence.id
    preserved_runtime_evidence_id = preserved_runtime_evidence.id
    learning_candidate_id = learning_candidate.id
    preserved_learning_candidate_id = preserved_learning_candidate.id
    scheduled_job_run_id = scheduled_job_run.id
    preserved_scheduled_job_run_id = preserved_scheduled_job_run.id
    workflow_run_id = workflow_run.id
    preserved_workflow_run_id = preserved_workflow_run.id
    workflow_project_id = workflow_project.id
    preserved_workflow_project_id = preserved_workflow_project.id
    workflow_action_grant_id = workflow_action_grant.id
    preserved_workflow_action_grant_id = preserved_workflow_action_grant.id
    workspace_profile_id = workspace_profile.id
    preserved_profile_id = preserved_profile.id
    entity_profile_id = entity_profile.id
    workspace_commitment_id = workspace_commitment.id
    preserved_commitment_id = preserved_commitment.id
    workspace_contribution_id = workspace_contribution.id
    preserved_contribution_id = preserved_contribution.id
    workspace_custom_field_id = workspace_custom_field.id
    preserved_custom_field_id = preserved_custom_field.id
    entity_custom_field_id = entity_custom_field.id
    pairing_code_value = pairing_code.code
    preserved_pairing_code_value = preserved_pairing_code.code
    pending_workspace_grant_id = pending_workspace_grant.id
    pending_agent_grant_id = pending_agent_grant.id
    pending_preserved_workspace_grant_id = pending_preserved_workspace_grant.id
    feedback_id = feedback.id
    feedback_evidence_id = feedback_evidence.id
    await db_session.commit()

    await soft_delete_workspace(db_session, ws_id, entity_id)
    db_session.expire_all()
    assert await db_session.get(ScheduledJobRun, scheduled_job_run_id) is None
    assert await db_session.get(ScheduledJobRun, preserved_scheduled_job_run_id) is not None
    assert await db_session.get(WorkflowProject, workflow_project_id) is not None
    assert await db_session.get(WorkflowActionGrant, workflow_action_grant_id) is None
    assert await db_session.get(
        WorkflowActionGrant,
        preserved_workflow_action_grant_id,
    ) is not None
    assert await db_session.get(WorkspaceEvent, workspace_event_id) is not None
    assert await db_session.get(RuntimeEvidence, runtime_evidence_id) is not None
    assert await db_session.get(AgentLearningCandidate, learning_candidate_id) is not None
    purged = await purge_workspace(db_session, ws_id)
    assert purged is True

    assert await db_session.get(ParticipantProfile, workspace_profile_id) is None
    assert await db_session.get(ParticipantProfile, preserved_profile_id) is not None
    assert await db_session.get(ParticipantProfile, entity_profile_id) is not None
    assert await db_session.get(HumanCommitment, workspace_commitment_id) is None
    assert await db_session.get(HumanCommitment, preserved_commitment_id) is not None
    assert await db_session.get(HumanContribution, workspace_contribution_id) is None
    assert await db_session.get(HumanContribution, preserved_contribution_id) is not None
    assert await db_session.get(CustomFieldDefinition, workspace_custom_field_id) is None
    assert await db_session.get(CustomFieldDefinition, preserved_custom_field_id) is not None
    assert await db_session.get(CustomFieldDefinition, entity_custom_field_id) is not None
    assert await db_session.get(ChannelPairingCode, pairing_code_value) is None
    assert await db_session.get(ChannelPairingCode, preserved_pairing_code_value) is not None
    assert await db_session.get(ResourceGrantPending, pending_workspace_grant_id) is None
    assert await db_session.get(ResourceGrantPending, pending_agent_grant_id) is None
    assert await db_session.get(
        ResourceGrantPending,
        pending_preserved_workspace_grant_id,
    ) is not None

    # Workspace row gone
    ws_check = await db_session.execute(select(Workspace).where(Workspace.id == ws_id))
    assert ws_check.scalar_one_or_none() is None

    # Task gone
    task_check = await db_session.execute(select(Task).where(Task.workspace_id == ws_id))
    assert task_check.scalar_one_or_none() is None
    assert await db_session.get(ChatMessageFeedback, feedback_id) is None
    assert await db_session.get(RuntimeEvidence, feedback_evidence_id) is None

    # Workspace workflow deployments must not dangle after the hard purge.
    assert await db_session.get(WorkflowBinding, binding_id) is None
    assert await db_session.get(Agent, agent_id) is None
    assert await db_session.get(Skill, skill_id) is None
    assert await db_session.get(AgentSkillBinding, skill_binding_id) is None
    assert await db_session.get(MarketplaceResourceLink, marketplace_link_id) is None
    assert await db_session.get(Skill, shared_skill_id) is not None
    db_session.expire_all()
    assert await db_session.get(Notification, notification_id) is None
    assert await db_session.get(Notification, legacy_notification_id) is None
    assert await db_session.get(NotificationOutboxEvent, notification_outbox_id) is None
    assert await db_session.get(NotificationOutboxEvent, legacy_outbox_id) is None
    assert await db_session.get(NotificationDelivery, notification_delivery_id) is None
    assert await db_session.get(Notification, unrelated_notification_id) is not None
    purged_runtime_rows = {
        "credit_reservation": await db_session.get(
            CreditReservation, workspace_reservation_id
        ),
        "credit_usage": await db_session.get(CreditUsageLog, workspace_usage_id),
        "credit_usage_allocation": await db_session.get(
            CreditUsageAllocation, workspace_usage_allocation_id
        ),
        "token_usage": await db_session.get(TokenUsageLog, workspace_token_usage_id),
        "tool_call_log": await db_session.get(ToolCallLog, workspace_tool_log_id),
        "runtime_event": await db_session.get(RuntimeEventLog, workspace_runtime_event_id),
        "review_run": await db_session.get(ReviewRun, workspace_review_run_id),
        "proposal": await db_session.get(ProposalRecord, workspace_proposal_id),
        "proposal_item": await db_session.get(
            ProposalItemRecord, workspace_proposal_item_id
        ),
        "hitl": await db_session.get(HitlRequest, workspace_hitl_id),
        "automation_revision": await db_session.get(
            AutomationRevision, workspace_automation_revision_id
        ),
        "experiment": await db_session.get(Experiment, workspace_experiment_id),
        "consolidation_report": await db_session.get(
            ConsolidationReport, workspace_consolidation_id
        ),
    }
    assert not {
        name: row.id for name, row in purged_runtime_rows.items() if row is not None
    }
    preserved_runtime_rows = {
        "credit_reservation": await db_session.get(
            CreditReservation, preserved_reservation_id
        ),
        "credit_usage": await db_session.get(CreditUsageLog, preserved_usage_id),
        "credit_usage_allocation": await db_session.get(
            CreditUsageAllocation, preserved_usage_allocation_id
        ),
        "token_usage": await db_session.get(TokenUsageLog, preserved_token_usage_id),
        "tool_call_log": await db_session.get(ToolCallLog, preserved_tool_log_id),
        "runtime_event": await db_session.get(RuntimeEventLog, preserved_runtime_event_id),
        "review_run": await db_session.get(ReviewRun, preserved_review_run_id),
        "proposal": await db_session.get(ProposalRecord, preserved_proposal_id),
        "proposal_item": await db_session.get(
            ProposalItemRecord, preserved_proposal_item_id
        ),
        "hitl": await db_session.get(HitlRequest, preserved_hitl_id),
        "automation_revision": await db_session.get(
            AutomationRevision, preserved_automation_revision_id
        ),
        "experiment": await db_session.get(Experiment, preserved_experiment_id),
        "consolidation_report": await db_session.get(
            ConsolidationReport, preserved_consolidation_id
        ),
    }
    assert all(row is not None for row in preserved_runtime_rows.values())
    assert await db_session.get(WorkspaceEvent, workspace_event_id) is None
    assert await db_session.get(RuntimeEvidence, runtime_evidence_id) is None
    assert await db_session.get(WorkspaceEvent, preserved_workspace_event_id) is not None
    assert await db_session.get(RuntimeEvidence, preserved_runtime_evidence_id) is not None
    assert await db_session.get(AgentLearningCandidate, learning_candidate_id) is None
    assert await db_session.get(
        AgentLearningCandidate,
        preserved_learning_candidate_id,
    ) is not None
    assert await db_session.get(WorkspaceStat, workspace_stat_id) is None
    assert await db_session.get(WorkspaceStatObservation, workspace_stat_observation_id) is None
    assert await db_session.get(WorkspaceStat, preserved_stat_id) is not None
    assert await db_session.get(Goal, goal_id) is None
    assert await db_session.get(GoalMeasurement, (goal_id, goal_measurement_at)) is None
    assert await db_session.get(GoalTaskLink, goal_task_link_id) is None
    assert await db_session.get(Goal, preserved_goal_id) is not None
    assert await db_session.get(
        GoalMeasurement,
        (preserved_goal_id, preserved_goal_measurement_at),
    ) is not None
    assert await db_session.get(GoalTaskLink, preserved_goal_task_link_id) is not None
    assert await db_session.get(Goal, entity_goal_id) is not None
    assert await db_session.get(GoalTaskLink, entity_goal_task_link_id) is None
    assert await db_session.get(ScheduledJobRun, scheduled_job_run_id) is None
    assert await db_session.get(ScheduledJobRun, preserved_scheduled_job_run_id) is not None

    # Runtime execution and Workspace operation staging must not outlive a
    # hard-purged Workspace. Their counterparts in the preserved Workspace
    # prove that cleanup remains scoped rather than entity-wide.
    runtime_cleanup_leaks = []
    if await db_session.get(RuntimeRun, workspace_runtime_run_id) is not None:
        runtime_cleanup_leaks.append("RuntimeRun")
    if await db_session.get(
        SandboxReservation, workspace_sandbox_reservation_id
    ) is not None:
        runtime_cleanup_leaks.append("SandboxReservation")
    if await db_session.get(SandboxInstance, workspace_sandbox_id) is not None:
        runtime_cleanup_leaks.append("SandboxInstance")
    runtime_outbox_check = await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.aggregate_id == workspace_runtime_run_id
        )
    )
    if runtime_outbox_check.scalar_one_or_none() is not None:
        runtime_cleanup_leaks.append("RuntimeOutboxEvent")
    preserved_runtime_outbox_check = await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.aggregate_id == preserved_runtime_run_id
        )
    )
    assert preserved_runtime_outbox_check.scalar_one_or_none() is not None
    if await db_session.get(WorkspaceOperationDraft, workspace_operation_draft_id) is not None:
        runtime_cleanup_leaks.append("WorkspaceOperationDraft")
    if await db_session.get(WorkspaceWorkBatch, workspace_work_batch_id) is not None:
        runtime_cleanup_leaks.append("WorkspaceWorkBatch")
    assert not runtime_cleanup_leaks, runtime_cleanup_leaks
    assert await db_session.get(RuntimeRun, preserved_runtime_run_id) is not None
    assert await db_session.get(
        SandboxReservation, preserved_sandbox_reservation_id
    ) is not None
    assert await db_session.get(SandboxInstance, preserved_sandbox_id) is not None
    assert await db_session.get(WorkspaceOperationDraft, preserved_operation_draft_id) is not None
    assert await db_session.get(WorkspaceWorkBatch, preserved_work_batch_id) is not None
    assert await db_session.get(WorkflowRun, workflow_run_id) is None
    assert await db_session.get(WorkflowRun, preserved_workflow_run_id) is not None
    assert await db_session.get(WorkflowProject, workflow_project_id) is None
    assert await db_session.get(WorkflowProject, preserved_workflow_project_id) is not None
    assert await db_session.get(WorkflowActionGrant, workflow_action_grant_id) is None
    assert await db_session.get(
        WorkflowActionGrant,
        preserved_workflow_action_grant_id,
    ) is not None


@pytest.mark.asyncio
async def test_purge_workspace_preserves_resources_reused_elsewhere(
    client: AsyncClient,
    db_session,
):
    from packages.core.models.document import Channel
    from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
    from packages.core.models.permission import (
        Capability,
        GrantStatus,
        ResourceGrant,
        ResourceType,
        SubjectType,
    )
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.skill import AgentSkillBinding, Skill
    from packages.core.models.task import Conversation, Task
    from packages.core.models.task_template import TaskTemplate
    from packages.core.models.user import User
    from packages.core.models.workflow import (
        WorkflowBinding,
        WorkflowDefinition,
        WorkflowTemplateInstallation,
    )
    from packages.core.models.workspace import (
        Agent,
        AgentSubscription,
        Workspace,
        WorkspaceStaff,
    )
    from packages.core.services.entity_service import (
        purge_workspace,
        soft_delete_workspace,
    )
    from packages.core.services.marketplace_resource_links import (
        RELATIONSHIP_INSTALLED_COMPONENT,
        RESOURCE_WORKSPACE_BLUEPRINT,
        SCOPE_RESOURCE,
        SCOPE_WORKSPACE,
        record_marketplace_resource_link,
    )
    from packages.core.services.resource_access import (
        ResourceDescriptor,
        user_can_access_resource,
    )

    _, headers = await _register(client, "lifecycle_shared_resources")
    home_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Resource home"},
    )
    home_id = home_response.json()["id"]
    entity_id = home_response.json()["entity_id"]
    consumer_workspace = Workspace(
        entity_id=entity_id,
        name="Resource consumer",
        settings={},
    )
    unrelated_workspace = Workspace(
        entity_id=entity_id,
        name="Unrelated resource consumer",
        settings={},
    )
    consumer_user = User(
        entity_id=entity_id,
        email="lifecycle-resource-consumer@test.com",
        display_name="Resource consumer",
        password_hash="not-used",
        role="member",
        status="active",
    )
    unrelated_user = User(
        entity_id=entity_id,
        email="lifecycle-resource-unrelated@test.com",
        display_name="Unrelated resource consumer",
        password_hash="not-used",
        role="member",
        status="active",
    )
    db_session.add_all([
        consumer_workspace,
        unrelated_workspace,
        consumer_user,
        unrelated_user,
    ])
    await db_session.flush()
    consumer_id = consumer_workspace.id
    unrelated_id = unrelated_workspace.id
    consumer_user_id = consumer_user.id
    unrelated_user_id = unrelated_user.id
    db_session.add_all([
        WorkspaceStaff(
            workspace_id=consumer_id,
            user_id=consumer_user.id,
            role="viewer",
            added_at=datetime.now(timezone.utc),
            status="active",
        ),
        WorkspaceStaff(
            workspace_id=unrelated_id,
            user_id=unrelated_user.id,
            role="viewer",
            added_at=datetime.now(timezone.utc),
            status="active",
        ),
    ])

    shared_agent = Agent(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Shared Agent",
        slug="shared-agent",
        system_prompt="Serve more than one Workspace.",
        status="active",
    )
    exclusive_agent = Agent(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Exclusive Agent",
        slug="exclusive-agent",
        system_prompt="Serve only the home Workspace.",
        status="active",
    )
    task_agent = Agent(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Entity Task Agent",
        slug="entity-task-agent",
        system_prompt="Remain assigned to an entity Task.",
        status="active",
    )
    conversation_agent = Agent(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Direct Conversation Agent",
        slug="direct-conversation-agent",
        system_prompt="Remain attached to a direct conversation.",
        status="active",
    )
    channel_agent = Agent(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Legacy Channel Agent",
        slug="legacy-channel-agent",
        system_prompt="Remain attached to a legacy Channel binding.",
        status="active",
    )
    scheduled_agent = Agent(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Entity Scheduled Agent",
        slug="entity-scheduled-agent",
        system_prompt="Remain attached to an entity ScheduledJob.",
        status="active",
    )
    template_agent = Agent(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Entity Template Agent",
        slug="entity-template-agent",
        system_prompt="Remain available to an entity Task template.",
        status="active",
    )
    workflow_agent = Agent(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Workflow Dependency Agent",
        slug="workflow-dependency-agent",
        system_prompt="Remain available to a reused Workflow.",
        status="active",
    )
    shared_skill = Skill(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Shared Skill",
        slug="shared-skill-from-home",
        system_prompt="Remain available to the shared Agent.",
        status="active",
    )
    exclusive_skill = Skill(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Exclusive Skill",
        slug="exclusive-skill-from-home",
        system_prompt="Serve only the exclusive Agent.",
        status="active",
    )
    public_skill = Skill(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Public Skill",
        slug="public-skill-from-home",
        system_prompt="Remain available without an Agent binding.",
        is_public=True,
        status="active",
    )
    linked_skill = Skill(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Linked Skill",
        slug="linked-skill-from-home",
        system_prompt="Remain available through an external install mapping.",
        status="active",
    )
    operating_skill = Skill(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Operating Model Skill",
        slug="operating-model-skill-from-home",
        system_prompt="Remain bound by exact ID in another Workspace.",
        status="active",
    )
    scheduled_skill = Skill(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Entity Scheduled Skill",
        slug="entity-scheduled-skill-from-home",
        system_prompt="Remain referenced by an entity ScheduledJob.",
        status="active",
    )
    provenance_only_skill = Skill(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Provenance-only Skill",
        slug="provenance-only-skill-from-home",
        system_prompt="A source link alone is not a deployment.",
        status="active",
    )
    workflow_skill = Skill(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="Workflow Dependency Skill",
        slug="workflow-dependency-skill-from-home",
        system_prompt="Remain available to a reused Workflow Agent.",
        status="active",
    )
    shared_workflow = WorkflowDefinition(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="shared-workflow",
        steps=[],
        status="active",
    )
    exclusive_workflow = WorkflowDefinition(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="exclusive-workflow",
        steps=[],
        status="active",
    )
    scheduled_workflow = WorkflowDefinition(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="scheduled-workflow",
        steps=[],
        status="active",
    )
    nested_workflow = WorkflowDefinition(
        entity_id=entity_id,
        workspace_id=home_id,
        visibility="workspace",
        name="nested-workflow",
        steps=[],
        status="active",
    )
    db_session.add_all([
        shared_agent,
        exclusive_agent,
        task_agent,
        conversation_agent,
        channel_agent,
        scheduled_agent,
        template_agent,
        workflow_agent,
        shared_skill,
        exclusive_skill,
        public_skill,
        linked_skill,
        operating_skill,
        scheduled_skill,
        provenance_only_skill,
        workflow_skill,
        shared_workflow,
        exclusive_workflow,
        scheduled_workflow,
        nested_workflow,
    ])
    await db_session.flush()

    scheduled_workflow.steps = [
        {"id": "start", "type": "trigger", "config": {}, "next": ["agent"]},
        {
            "id": "agent",
            "type": "agent",
            "config": {
                "agent_id": workflow_agent.id,
                "skill": workflow_skill.id,
            },
            "next": ["nested"],
        },
        {
            "id": "nested",
            "type": "subworkflow",
            "config": {"workflow_id": nested_workflow.id},
            "next": [],
        },
    ]

    consumer_workspace.operating_model = {
        "skill_bindings": [{"skill_id": operating_skill.id, "enabled": True}],
    }

    direct_task = Task(
        entity_id=entity_id,
        workspace_id=None,
        title="Entity task using a Workspace-home Agent",
        status="pending",
        agent_id=task_agent.id,
        creator_id=consumer_user.id,
        owner_id=consumer_user.id,
    )
    direct_conversation = Conversation(
        entity_id=entity_id,
        user_id=consumer_user.id,
        agent_id=conversation_agent.id,
        workspace_id=None,
        title="Direct Agent conversation",
    )
    legacy_channel = Channel(
        entity_id=entity_id,
        user_id=consumer_user.id,
        workspace_id=consumer_id,
        type="telegram",
        name="Legacy direct Agent channel",
        config={},
        agent_id=channel_agent.id,
        status="active",
    )
    entity_scheduled_job = ScheduledJob(
        job_id=f"entity-agent:{scheduled_agent.id}",
        entity_id=entity_id,
        workspace_id=None,
        user_id=consumer_user.id,
        name="Entity Agent schedule",
        agent_id=scheduled_agent.id,
        enabled=True,
    )
    entity_skill_job = ScheduledJob(
        job_id=f"entity-skill:{scheduled_skill.id}",
        entity_id=entity_id,
        workspace_id=None,
        user_id=consumer_user.id,
        name="Entity Skill schedule",
        execution_type="skill",
        execution_target={"config": {"skill_id": scheduled_skill.id}},
        enabled=True,
    )
    consumer_workflow_job = ScheduledJob(
        job_id=f"workspace-workflow:{scheduled_workflow.id}",
        entity_id=entity_id,
        workspace_id=consumer_id,
        user_id=consumer_user.id,
        name="Consumer Workflow schedule",
        execution_type="workflow",
        execution_target={"runtime": {"workflow_id": scheduled_workflow.id}},
        enabled=True,
    )
    entity_task_template = TaskTemplate(
        entity_id=entity_id,
        name="Entity template using a Workspace-home Agent",
        title_template="Run the entity template",
        default_agent_id=template_agent.id,
        status="active",
    )
    db_session.add_all([
        direct_task,
        direct_conversation,
        legacy_channel,
        entity_scheduled_job,
        entity_skill_job,
        consumer_workflow_job,
        entity_task_template,
    ])

    home_shared_subscription = AgentSubscription(
        entity_id=entity_id,
        agent_id=shared_agent.id,
        workspace_id=home_id,
        status="active",
    )
    consumer_subscription = AgentSubscription(
        entity_id=entity_id,
        agent_id=shared_agent.id,
        workspace_id=consumer_id,
        status="active",
    )
    exclusive_subscription = AgentSubscription(
        entity_id=entity_id,
        agent_id=exclusive_agent.id,
        workspace_id=home_id,
        status="active",
    )
    shared_skill_binding = AgentSkillBinding(
        agent_id=shared_agent.id,
        skill_id=shared_skill.id,
        status="active",
    )
    exclusive_skill_binding = AgentSkillBinding(
        agent_id=exclusive_agent.id,
        skill_id=exclusive_skill.id,
        status="active",
    )
    workflow_skill_binding = AgentSkillBinding(
        agent_id=workflow_agent.id,
        skill_id=workflow_skill.id,
        status="active",
    )
    home_shared_workflow_binding = WorkflowBinding(
        entity_id=entity_id,
        workflow_id=shared_workflow.id,
        workspace_id=home_id,
        trigger_type="manual",
        status="active",
    )
    consumer_workflow_binding = WorkflowBinding(
        entity_id=entity_id,
        workflow_id=shared_workflow.id,
        workspace_id=consumer_id,
        trigger_type="manual",
        status="active",
    )
    exclusive_workflow_binding = WorkflowBinding(
        entity_id=entity_id,
        workflow_id=exclusive_workflow.id,
        workspace_id=home_id,
        trigger_type="manual",
        status="active",
    )
    shared_installation = WorkflowTemplateInstallation(
        entity_id=entity_id,
        template_id="blueprint:shared-lifecycle",
        component_key="shared-workflow",
        workflow_id=shared_workflow.id,
        installed_version="1.0.0",
    )
    exclusive_installation = WorkflowTemplateInstallation(
        entity_id=entity_id,
        template_id="blueprint:exclusive-lifecycle",
        component_key="exclusive-workflow",
        workflow_id=exclusive_workflow.id,
        installed_version="1.0.0",
    )
    db_session.add_all([
        home_shared_subscription,
        consumer_subscription,
        exclusive_subscription,
        shared_skill_binding,
        exclusive_skill_binding,
        workflow_skill_binding,
        home_shared_workflow_binding,
        consumer_workflow_binding,
        exclusive_workflow_binding,
        shared_installation,
        exclusive_installation,
    ])
    await db_session.flush()

    resource_rows = {
        "shared-agent": ("agent", shared_agent.id),
        "exclusive-agent": ("agent", exclusive_agent.id),
        "shared-skill": ("skill", shared_skill.id),
        "exclusive-skill": ("skill", exclusive_skill.id),
        "public-skill": ("skill", public_skill.id),
        "linked-skill": ("skill", linked_skill.id),
        "operating-skill": ("skill", operating_skill.id),
        "scheduled-skill": ("skill", scheduled_skill.id),
        "provenance-only-skill": ("skill", provenance_only_skill.id),
        "shared-workflow": ("workflow", shared_workflow.id),
        "exclusive-workflow": ("workflow", exclusive_workflow.id),
        "scheduled-workflow": ("workflow", scheduled_workflow.id),
        "template-agent": ("agent", template_agent.id),
        "workflow-agent": ("agent", workflow_agent.id),
        "workflow-skill": ("skill", workflow_skill.id),
        "nested-workflow": ("workflow", nested_workflow.id),
    }
    marketplace_links: dict[str, MarketplaceResourceLink] = {}
    for component_key, (resource_type, resource_id) in resource_rows.items():
        marketplace_links[component_key] = await record_marketplace_resource_link(
            db_session,
            entity_id=entity_id,
            marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
            marketplace_resource_id="builtin:shared-resource-purge",
            relationship=RELATIONSHIP_INSTALLED_COMPONENT,
            scope_type=SCOPE_WORKSPACE,
            scope_id=home_id,
            local_resource_type=resource_type,
            local_resource_id=resource_id,
            component_key=component_key,
        )
    marketplace_links["consumer-linked-skill"] = (
        await record_marketplace_resource_link(
            db_session,
            entity_id=entity_id,
            marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
            marketplace_resource_id="builtin:shared-resource-purge",
            relationship=RELATIONSHIP_INSTALLED_COMPONENT,
            scope_type=SCOPE_WORKSPACE,
            scope_id=consumer_id,
            local_resource_type="skill",
            local_resource_id=linked_skill.id,
            component_key="linked-skill",
        )
    )
    marketplace_links["provenance-only-resource"] = (
        await record_marketplace_resource_link(
            db_session,
            entity_id=entity_id,
            marketplace_resource_type=RESOURCE_WORKSPACE_BLUEPRINT,
            marketplace_resource_id="builtin:shared-resource-purge",
            relationship=RELATIONSHIP_INSTALLED_COMPONENT,
            scope_type=SCOPE_RESOURCE,
            scope_id=provenance_only_skill.id,
            local_resource_type="skill",
            local_resource_id=provenance_only_skill.id,
            component_key="provenance-only-skill",
        )
    )

    exclusive_resource_grant = ResourceGrant(
        entity_id=entity_id,
        resource_type=ResourceType.SKILL,
        resource_id=exclusive_skill.id,
        subject_type=SubjectType.USER,
        subject_id=consumer_user.id,
        capabilities=[Capability.VIEW],
        granted_at=datetime.now(timezone.utc),
        status=GrantStatus.ACTIVE,
    )
    deleted_workspace_role_grant = ResourceGrant(
        entity_id=entity_id,
        resource_type=ResourceType.AGENT,
        resource_id=shared_agent.id,
        subject_type=SubjectType.WORKSPACE_ROLE,
        subject_id=f"{home_id}:viewer",
        capabilities=[Capability.VIEW],
        granted_at=datetime.now(timezone.utc),
        status=GrantStatus.ACTIVE,
    )
    db_session.add_all([exclusive_resource_grant, deleted_workspace_role_grant])
    await db_session.flush()
    marketplace_link_ids = {
        component_key: link.id for component_key, link in marketplace_links.items()
    }

    ids = {
        "shared_agent": shared_agent.id,
        "exclusive_agent": exclusive_agent.id,
        "task_agent": task_agent.id,
        "conversation_agent": conversation_agent.id,
        "channel_agent": channel_agent.id,
        "scheduled_agent": scheduled_agent.id,
        "template_agent": template_agent.id,
        "workflow_agent": workflow_agent.id,
        "direct_task": direct_task.id,
        "direct_conversation": direct_conversation.id,
        "legacy_channel": legacy_channel.id,
        "entity_scheduled_job": entity_scheduled_job.id,
        "entity_skill_job": entity_skill_job.id,
        "consumer_workflow_job": consumer_workflow_job.id,
        "entity_task_template": entity_task_template.id,
        "shared_skill": shared_skill.id,
        "exclusive_skill": exclusive_skill.id,
        "public_skill": public_skill.id,
        "linked_skill": linked_skill.id,
        "operating_skill": operating_skill.id,
        "scheduled_skill": scheduled_skill.id,
        "provenance_only_skill": provenance_only_skill.id,
        "workflow_skill": workflow_skill.id,
        "shared_workflow": shared_workflow.id,
        "exclusive_workflow": exclusive_workflow.id,
        "scheduled_workflow": scheduled_workflow.id,
        "nested_workflow": nested_workflow.id,
        "home_shared_subscription": home_shared_subscription.id,
        "consumer_subscription": consumer_subscription.id,
        "exclusive_subscription": exclusive_subscription.id,
        "shared_skill_binding": shared_skill_binding.id,
        "exclusive_skill_binding": exclusive_skill_binding.id,
        "workflow_skill_binding": workflow_skill_binding.id,
        "home_shared_workflow_binding": home_shared_workflow_binding.id,
        "consumer_workflow_binding": consumer_workflow_binding.id,
        "exclusive_workflow_binding": exclusive_workflow_binding.id,
        "shared_installation": shared_installation.id,
        "exclusive_installation": exclusive_installation.id,
        "exclusive_resource_grant": exclusive_resource_grant.id,
        "deleted_workspace_role_grant": deleted_workspace_role_grant.id,
    }
    await db_session.commit()

    await soft_delete_workspace(db_session, home_id, entity_id)
    assert await purge_workspace(db_session, home_id) is True
    db_session.expire_all()

    assert await db_session.get(Workspace, home_id) is None
    assert await db_session.get(Workspace, consumer_id) is not None
    assert await db_session.get(Workspace, unrelated_id) is not None

    retained_agent = await db_session.get(Agent, ids["shared_agent"])
    retained_skill = await db_session.get(Skill, ids["shared_skill"])
    retained_public_skill = await db_session.get(Skill, ids["public_skill"])
    retained_linked_skill = await db_session.get(Skill, ids["linked_skill"])
    retained_operating_skill = await db_session.get(Skill, ids["operating_skill"])
    retained_scheduled_skill = await db_session.get(Skill, ids["scheduled_skill"])
    retained_workflow = await db_session.get(
        WorkflowDefinition, ids["shared_workflow"]
    )
    retained_scheduled_workflow = await db_session.get(
        WorkflowDefinition, ids["scheduled_workflow"]
    )
    retained_template_agent = await db_session.get(Agent, ids["template_agent"])
    retained_workflow_agent = await db_session.get(Agent, ids["workflow_agent"])
    retained_workflow_skill = await db_session.get(Skill, ids["workflow_skill"])
    retained_nested_workflow = await db_session.get(
        WorkflowDefinition, ids["nested_workflow"]
    )
    assert retained_agent is not None
    assert retained_agent.workspace_id is None
    assert retained_agent.visibility == "private"
    assert retained_skill is not None
    assert retained_skill.workspace_id is None
    assert retained_skill.visibility == "private"
    assert retained_public_skill is not None
    assert retained_public_skill.workspace_id is None
    assert retained_public_skill.visibility == "entity"
    assert retained_linked_skill is not None
    assert retained_linked_skill.workspace_id is None
    assert retained_linked_skill.visibility == "private"
    assert retained_operating_skill is not None
    assert retained_operating_skill.workspace_id is None
    assert retained_operating_skill.visibility == "private"
    assert retained_scheduled_skill is not None
    assert retained_scheduled_skill.workspace_id is None
    assert retained_scheduled_skill.visibility == "private"
    assert retained_workflow is not None
    assert retained_workflow.workspace_id is None
    assert retained_workflow.visibility == "private"
    assert retained_scheduled_workflow is not None
    assert retained_scheduled_workflow.workspace_id is None
    assert retained_scheduled_workflow.visibility == "private"
    assert retained_template_agent is not None
    assert retained_template_agent.workspace_id is None
    assert retained_template_agent.visibility == "entity"
    assert retained_workflow_agent is not None
    assert retained_workflow_agent.workspace_id is None
    assert retained_workflow_agent.visibility == "private"
    assert retained_workflow_skill is not None
    assert retained_workflow_skill.workspace_id is None
    assert retained_workflow_skill.visibility == "private"
    assert retained_nested_workflow is not None
    assert retained_nested_workflow.workspace_id is None
    assert retained_nested_workflow.visibility == "private"

    retained_direct_agents = [
        await db_session.get(Agent, ids[key])
        for key in (
            "task_agent",
            "conversation_agent",
            "channel_agent",
            "scheduled_agent",
        )
    ]
    assert all(agent is not None for agent in retained_direct_agents)
    assert all(agent.workspace_id is None for agent in retained_direct_agents)
    assert all(agent.visibility == "private" for agent in retained_direct_agents)
    assert await db_session.get(Task, ids["direct_task"]) is not None
    assert await db_session.get(Conversation, ids["direct_conversation"]) is not None
    assert await db_session.get(Channel, ids["legacy_channel"]) is not None
    assert await db_session.get(ScheduledJob, ids["entity_scheduled_job"]) is not None
    assert await db_session.get(ScheduledJob, ids["entity_skill_job"]) is not None
    assert await db_session.get(ScheduledJob, ids["consumer_workflow_job"]) is not None
    assert await db_session.get(TaskTemplate, ids["entity_task_template"]) is not None

    assert await db_session.get(Agent, ids["exclusive_agent"]) is None
    assert await db_session.get(Skill, ids["exclusive_skill"]) is None
    assert await db_session.get(WorkflowDefinition, ids["exclusive_workflow"]) is None
    assert await db_session.get(Skill, ids["provenance_only_skill"]) is None
    assert await db_session.get(
        ResourceGrant, ids["exclusive_resource_grant"]
    ) is None
    assert await db_session.get(
        ResourceGrant, ids["deleted_workspace_role_grant"]
    ) is None
    assert await db_session.get(AgentSubscription, ids["home_shared_subscription"]) is None
    assert await db_session.get(AgentSubscription, ids["exclusive_subscription"]) is None
    assert await db_session.get(AgentSubscription, ids["consumer_subscription"]) is not None
    assert await db_session.get(AgentSkillBinding, ids["shared_skill_binding"]) is not None
    assert await db_session.get(AgentSkillBinding, ids["exclusive_skill_binding"]) is None
    assert await db_session.get(
        AgentSkillBinding, ids["workflow_skill_binding"]
    ) is not None
    assert await db_session.get(WorkflowBinding, ids["home_shared_workflow_binding"]) is None
    assert await db_session.get(WorkflowBinding, ids["exclusive_workflow_binding"]) is None
    assert await db_session.get(WorkflowBinding, ids["consumer_workflow_binding"]) is not None
    assert await db_session.get(
        WorkflowTemplateInstallation, ids["shared_installation"]
    ) is not None
    assert await db_session.get(
        WorkflowTemplateInstallation, ids["exclusive_installation"]
    ) is None

    for component_key in (
        "shared-agent",
        "shared-skill",
        "public-skill",
        "linked-skill",
        "operating-skill",
        "scheduled-skill",
        "shared-workflow",
        "scheduled-workflow",
        "template-agent",
        "workflow-agent",
        "workflow-skill",
        "nested-workflow",
    ):
        retained_link = await db_session.get(
            MarketplaceResourceLink, marketplace_link_ids[component_key]
        )
        assert retained_link is not None
        assert retained_link.scope_type == SCOPE_RESOURCE
        assert retained_link.scope_id == resource_rows[component_key][1]
    consumer_link = await db_session.get(
        MarketplaceResourceLink,
        marketplace_link_ids["consumer-linked-skill"],
    )
    assert consumer_link is not None
    assert consumer_link.scope_type == SCOPE_WORKSPACE
    assert consumer_link.scope_id == consumer_id
    for component_key in (
        "exclusive-agent",
        "exclusive-skill",
        "exclusive-workflow",
        "provenance-only-skill",
        "provenance-only-resource",
    ):
        assert await db_session.get(
            MarketplaceResourceLink, marketplace_link_ids[component_key]
        ) is None

    async def can_view(resource, resource_type: str, user_id: str) -> bool:
        return await user_can_access_resource(
            db_session,
            descriptor=ResourceDescriptor.from_row(resource, resource_type),
            entity_id=entity_id,
            user_id=user_id,
            role="member",
            capability=Capability.VIEW,
        )

    for label, resource, resource_type in (
        ("shared agent", retained_agent, ResourceType.AGENT),
        ("shared skill", retained_skill, ResourceType.SKILL),
        ("linked skill", retained_linked_skill, ResourceType.SKILL),
        ("operating skill", retained_operating_skill, ResourceType.SKILL),
        ("scheduled skill", retained_scheduled_skill, ResourceType.SKILL),
        ("shared workflow", retained_workflow, ResourceType.WORKFLOW),
        (
            "scheduled workflow",
            retained_scheduled_workflow,
            ResourceType.WORKFLOW,
        ),
        ("workflow agent", retained_workflow_agent, ResourceType.AGENT),
        ("workflow skill", retained_workflow_skill, ResourceType.SKILL),
        (
            "nested workflow",
            retained_nested_workflow,
            ResourceType.WORKFLOW,
        ),
    ):
        assert await can_view(resource, resource_type, consumer_user_id) is True, label
        assert await can_view(resource, resource_type, unrelated_user_id) is False, label
    for direct_agent in retained_direct_agents:
        assert await can_view(
            direct_agent, ResourceType.AGENT, consumer_user_id
        ) is True, direct_agent.name
        assert await can_view(
            direct_agent, ResourceType.AGENT, unrelated_user_id
        ) is False, direct_agent.name
    assert await can_view(
        retained_template_agent,
        ResourceType.AGENT,
        consumer_user_id,
    ) is True
    assert await can_view(
        retained_template_agent,
        ResourceType.AGENT,
        unrelated_user_id,
    ) is True
    assert await can_view(
        retained_public_skill,
        ResourceType.SKILL,
        unrelated_user_id,
    ) is True
