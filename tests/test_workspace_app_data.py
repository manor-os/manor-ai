from datetime import UTC, datetime, timedelta

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity, User
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.models.workspace_stat import WorkspaceStat, WorkspaceStatObservation
from packages.core.models.workflow import WorkflowBinding, WorkflowProject
from packages.core.models.workflow import WorkflowRun
from packages.core.services.workspace_app_data import (
    AppDataError, _launch_label, _normalized_order_value, _page, _present_configured_field, _present_items, _project_rollup,
    _projects, _source_rows, _validate_component_context, _verified_launch_projects, component_contexts, component_data,
)
from packages.core.stats.service import stat_freshness_status


@pytest.mark.asyncio
async def test_course_plan_rollup_is_scoped_filtered_and_keyset_sorted(db_session, monkeypatch):
    """A selected course owns its full reminder rollup, not the visible page."""
    entity = Entity(name="Course rollup")
    user = User(entity_id="pending", email="course-rollup@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Courses", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    course = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course", schema_version=1,
                             project_key="course-a", current_stage="active", state={"title": "A"}, created_by=user.id)
    other = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course", schema_version=1,
                            project_key="course-b", current_stage="inactive", state={"title": "B"}, created_by=user.id)
    db_session.add_all([course, other])
    await db_session.flush()
    reminders = []
    # Reminders 0 and 1 are due at the same instant, so the keyset order falls
    # back to the id. ULIDs minted in one millisecond need not sort in creation
    # order; assign ids in index order so that tie is deterministic.
    reminder_ids = sorted(generate_ulid() for _ in range(23))
    for index in range(23):
        stage = "published" if index == 22 else "ready" if index % 2 else "scheduled"
        due = ("2026-11-03T09:00:00+00:00" if index == 0 else "2026-11-03T17:00:00+08:00" if index == 1
               else "2026-11-24 09:00:00+00:00" if index == 20 else None if index == 21 else f"2026-11-{4 + index:02d}T09:00:00+00:00")
        reminders.append(WorkflowProject(id=reminder_ids[index], entity_id=entity.id, workspace_id=workspace.id, project_type="course_reminder",
            schema_version=1, project_key=f"a-{index:02d}", current_stage=stage,
            state={"course_id": course.id, "due_at": due, "title": f"A {index}"}, created_by=user.id))
    reminders.append(WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course_reminder",
        schema_version=1, project_key="other", current_stage="ready", state={"course_id": other.id, "due_at": "2026-01-01T09:00:00+00:00", "title": "Other"}, created_by=user.id))
    invalid_datetimes = [
        "2026-11-24T24:00:00+00:00",
        "2026-11-24T09:00:60+00:00",
        "2026-11-24T09:00:00+23:00",
        "2026-11-24T09:00:00.1234567+00:00",
    ]
    reminders.extend(
        WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course_reminder",
            schema_version=1, project_key=f"invalid-{index}", current_stage="scheduled",
            state={"course_id": course.id, "due_at": due, "title": f"Invalid {index}"}, created_by=user.id)
        for index, due in enumerate(invalid_datetimes)
    )
    db_session.add_all(reminders)
    await db_session.commit()
    workspace.operating_model = {"app_view": {"data_sources": {
        "courses": {"type": "workflow_projects", "project_type": "course", "schema_versions": [1], "fields": [["title"]],
                    "producer_workflows": ["workflow_definition:course"], "launch_workflows": [], "stages": ["active"]},
        "reminders": {"type": "related_projects", "project_type": "course_reminder", "schema_versions": [1], "parent": "courses",
                      "parent_state_path": ["course_id"], "fields": [["due_at"], ["title"]], "stages": ["scheduled", "ready"],
                      "order": {"field": ["due_at"], "type": "datetime", "direction": "asc"},
                      "producer_workflows": ["workflow_definition:reminder"]},
    }, "components": [{"type": "data_view", "schema_version": 1, "key": "course_plan", "context": {"kind": "project", "source": "courses"},
        "blocks": [{"key": "rollup", "type": "project_rollup", "source": "reminders", "count_by_stage": True,
                    "first": {"stages": ["scheduled", "ready"]}}, {"key": "total", "type": "project_rollup", "source": "reminders",
                    "first": {"stages": ["scheduled", "ready"]}}, {"key": "items", "type": "progress", "source": "reminders", "fields": [
                        {"label": "Due", "field": ["due_at"], "format": "date"},
                        {"label": "Title", "field": ["title"], "hide_when_empty": True},
                    ], "limit": 20}]}]}}
    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"course", "reminder"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": [
            {"slug": "course", "steps": [{"config": {"project_type": "course", "state_schema": {"properties": {"title": {"type": "string"}}}}}]},
            {"slug": "reminder", "steps": [{"config": {"project_type": "course_reminder", "state_schema": {"properties": {"course_id": {"type": "string"}, "due_at": {"type": "string"}, "title": {"type": "string"}}}}}]},
        ]}
    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)
    # A parent source's stage filter applies to all of its children. The list
    # and SQL rollup must agree when there is no selected parent context.
    sources = workspace.operating_model["app_view"]["data_sources"]
    workspace_rows = await _source_rows(db_session, workspace, user, sources,
                                        "reminders", {"kind": "workspace", "id": workspace.id}, {})
    assert {row["id"] for row in workspace_rows} == {
        reminder.id for reminder in reminders
        if reminder.state.get("course_id") == course.id and reminder.current_stage in {"scheduled", "ready"}
    }
    workspace_rollup = await _project_rollup(db_session, workspace, user, sources, "reminders", sources["reminders"],
                                              {"kind": "workspace", "id": workspace.id})
    assert workspace_rollup["total"] == 26
    assert workspace_rollup["counts_by_stage"] == {"scheduled": 15, "ready": 11}

    result = await component_data(db_session, workspace, user=user, component_key="course_plan", context_kind="project", context_id=course.id)
    rollup = next(block for block in result["blocks"] if block["key"] == "rollup")["items"][0]
    assert rollup["total"] == 26
    assert rollup["counts_by_stage"] == {"scheduled": 15, "ready": 11}
    assert rollup["next"]["fields"] == [{
        "label": "Due At", "format": "date", "value": "2026-11-03T09:00:00+00:00",
    }]
    total = next(block for block in result["blocks"] if block["key"] == "total")["items"][0]
    assert "counts_by_stage" not in total
    assert total["next"]["fields"] == rollup["next"]["fields"]
    items = next(block for block in result["blocks"] if block["key"] == "items")
    assert len(items["items"]) == 20 and items["next_cursor"]
    assert [item["fields"][0] for item in items["items"][:2]] == [
        {"label": "Due", "format": "date", "value": "2026-11-03T09:00:00+00:00"},
        {"label": "Due", "format": "date", "value": "2026-11-03T17:00:00+08:00"},
    ]
    second = await component_data(db_session, workspace, user=user, component_key="course_plan", context_kind="project", context_id=course.id, block_key="items", cursor=items["next_cursor"])
    assert len(second["blocks"][0]["items"]) == 6
    assert {item["fields"][0]["value"] for item in second["blocks"][0]["items"]} == {
        "2026-11-24 09:00:00+00:00", None, *invalid_datetimes,
    }

    # Python accepts the space-separated value, but the portable order contract
    # treats it as null so each one-item cursor page still advances exactly once.
    page = await component_data(db_session, workspace, user=user, component_key="course_plan", context_kind="project", context_id=course.id, block_key="items", limit=1)
    seen = set()
    while page["blocks"][0]["items"]:
        item = page["blocks"][0]["items"][0]
        assert item["id"] not in seen
        seen.add(item["id"])
        cursor = page["blocks"][0]["next_cursor"]
        if not cursor:
            break
        page = await component_data(db_session, workspace, user=user, component_key="course_plan", context_kind="project", context_id=course.id, block_key="items", cursor=cursor, limit=1)
    assert len(seen) == 26
    assert all(_normalized_order_value(value) is None for value in invalid_datetimes)


@pytest.mark.asyncio
async def test_calendar_contexts_batch_project_summaries_and_progress_dates(db_session, monkeypatch):
    """A declared calendar adds summaries for one context page without changing ordinary projects."""
    entity = Entity(name="Calendar contexts")
    user = User(entity_id="pending", email="calendar-contexts@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Courses", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    other_workspace = Workspace(entity_id=entity.id, name="Other courses", operating_model={})
    other_entity = Entity(name="Other calendar contexts")
    db_session.add_all([other_workspace, other_entity])
    await db_session.flush()
    other_entity_workspace = Workspace(entity_id=other_entity.id, name="Other entity courses", operating_model={})
    db_session.add(other_entity_workspace)
    await db_session.flush()
    first = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course", schema_version=1,
                            project_key="first", current_stage="active", created_by=user.id,
                            state={"title": "First course", "starts_at": "2026-11-01T09:00:00+00:00"})
    second = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course", schema_version=1,
                             project_key="second", current_stage="active", created_by=user.id,
                             state={"title": "Second course", "starts_at": "2026-11-02T09:00:00+00:00"})
    db_session.add_all([first, second])
    await db_session.flush()
    db_session.add_all([
        WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course_reminder", schema_version=1,
                        project_key="first-scheduled", current_stage="scheduled", created_by=user.id,
                        state={"course_id": first.id, "schedule": {"due_at": "2026-11-03T09:00:00+00:00"}, "title": "Prepare"}),
        WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course_reminder", schema_version=1,
                        project_key="first-ready", current_stage="ready", created_by=user.id,
                        state={"course_id": first.id, "schedule": {"due_at": "2026-11-04T09:00:00+00:00"}, "title": "Send"}),
        WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course_reminder", schema_version=1,
                        project_key="second-published", current_stage="published", created_by=user.id,
                        state={"course_id": second.id, "schedule": {"due_at": "2026-11-05T09:00:00+00:00"}, "title": "Done"}),
        # The aggregation query must not count a child that merely references
        # the selected parent ID from another Workspace or tenant.
        WorkflowProject(entity_id=entity.id, workspace_id=other_workspace.id, project_type="course_reminder", schema_version=1,
                        project_key="foreign-workspace", current_stage="scheduled", created_by=user.id,
                        state={"course_id": first.id, "schedule": {"due_at": "2026-11-02T09:00:00+00:00"}, "title": "Foreign workspace"}),
        WorkflowProject(entity_id=other_entity.id, workspace_id=other_entity_workspace.id, project_type="course_reminder", schema_version=1,
                        project_key="foreign-entity", current_stage="scheduled", created_by=user.id,
                        state={"course_id": first.id, "schedule": {"due_at": "2026-11-01T09:00:00+00:00"}, "title": "Foreign entity"}),
    ])
    workspace.operating_model = {"app_view": {"data_sources": {
        "courses": {"type": "workflow_projects", "project_type": "course", "schema_versions": [1],
                    "producer_workflows": ["workflow_definition:course"], "launch_workflows": [],
                    "fields": [["title"], ["starts_at"]], "label_fields": [["title"]]},
        "reminders": {"type": "related_projects", "project_type": "course_reminder", "schema_versions": [1],
                      "producer_workflows": ["workflow_definition:reminder"], "parent": "courses",
                      "parent_state_path": ["course_id"], "fields": [["schedule", "due_at"], ["title"]],
                      "label_fields": [["title"]],
                      "order": {"field": ["schedule", "due_at"], "type": "datetime", "direction": "asc"}},
    }, "components": [{"type": "data_view", "schema_version": 1, "key": "course_calendar",
        "context": {"kind": "project", "source": "courses"}, "blocks": [
            {"key": "calendar", "type": "progress", "source": "reminders", "limit": 100, "fields": [
                    {"label": "Due", "field": ["schedule", "due_at"], "format": "date", "hide_when_empty": False},
            ]},
            {"key": "rollup", "type": "project_rollup", "source": "reminders", "count_by_stage": True,
             "first": {"stages": ["scheduled", "ready"]}},
            {"key": "context", "type": "summary", "source": "courses", "fields": [
                {"label": "Starts", "field": ["starts_at"], "format": "date", "hide_when_empty": False},
            ]},
        ], "display": {"layout": "stack", "groups": [{"key": "all", "title": "All", "blocks": ["calendar", "rollup", "context"]}],
            "hidden": False, "hidden_blocks": [], "calendar": {
                "block": "calendar", "date_field": ["schedule", "due_at"], "rollup_block": "rollup", "context_block": "context",
                "attention_stages": ["blocked"], "collection_title": "Courses",
            }}}]}}
    await db_session.commit()

    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"course", "reminder"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": [
            {"slug": "course", "steps": [{"config": {"project_type": "course", "state_schema": {"properties": {
                "title": {"type": "string"}, "starts_at": {"type": "string"},
            }}}}]},
            {"slug": "reminder", "steps": [{"config": {"project_type": "course_reminder", "state_schema": {"properties": {
                    "course_id": {"type": "string"}, "schedule": {"type": "object", "properties": {"due_at": {"type": "string"}}}, "title": {"type": "string"},
            }}}}]},
        ]}
    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)

    contexts = await component_contexts(db_session, workspace, user=user, component_key="course_calendar")
    summaries = {item["id"]: item["summary"] for item in contexts["items"]}
    first_summary = summaries[first.id]
    assert first_summary["fields"] == [{"label": "Starts", "format": "date", "value": "2026-11-01T09:00:00+00:00"}]
    assert first_summary["total"] == 2 and first_summary["counts_by_stage"] == {"scheduled": 1, "ready": 1}
    assert first_summary["next"] == {
        "id": first_summary["next"]["id"], "label": "Prepare", "status": "scheduled",
        "fields": [{"label": "Due", "format": "date", "value": "2026-11-03T09:00:00+00:00"}],
        "detail": "course_reminder", "updated_at": None, "execution": None,
        "calendar_at": "2026-11-03T09:00:00+00:00",
    }
    assert summaries[second.id]["total"] == 1
    assert summaries[second.id]["counts_by_stage"] == {"published": 1}
    assert "next" not in summaries[second.id]

    calendar = await component_data(db_session, workspace, user=user, component_key="course_calendar",
                                    context_kind="project", context_id=first.id, block_key="calendar")
    assert [item["calendar_at"] for item in calendar["blocks"][0]["items"]] == [
        "2026-11-03T09:00:00+00:00", "2026-11-04T09:00:00+00:00",
    ]


@pytest.mark.asyncio
async def test_project_source_is_scoped_to_selected_workspace_and_context(db_session):
    """A component source must not read a same-type project from another Workspace."""
    entity = Entity(name="App data entity")
    user = User(entity_id="pending", email="app-data@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Primary", operating_model={})
    other = Workspace(entity_id=entity.id, name="Other", operating_model={})
    db_session.add_all([workspace, other])
    await db_session.flush()
    selected = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="client_delivery",
                               schema_version=1, project_key="selected", current_stage="ready",
                               state={"status": "ready", "client": {"name": "Oak Studio"}, "edition": "Autumn"}, created_by=user.id)
    foreign = WorkflowProject(entity_id=entity.id, workspace_id=other.id, project_type="client_delivery",
                              schema_version=1, project_key="foreign", current_stage="ready",
                              state={"status": "ready"}, created_by=user.id)
    failed_run = WorkflowRun(entity_id=entity.id, workspace_id=workspace.id, workflow_id="delivery-flow",
                             status="failed", current_step_id="publish", error="Upload rejected")
    db_session.add_all([selected, foreign, failed_run])
    await db_session.flush()
    selected.last_run_id = failed_run.id
    await db_session.commit()
    source = {"type": "workflow_projects", "project_type": "client_delivery", "schema_versions": [1],
              "producer_workflows": ["workflow_definition:test"], "launch_workflows": [], "fields": [["status"]]}
    rows = await _source_rows(db_session, workspace, user, {"deliveries": source}, "deliveries",
                              {"kind": "project", "id": selected.id}, {})
    assert [row["id"] for row in rows] == [selected.id]
    assert rows[0]["fields"] == {"status": "ready"}
    assert rows[0]["execution"] == {
        "run_id": failed_run.id, "status": "failed", "current_step_id": "publish", "error": "Upload rejected",
        "href": f"/flows/{failed_run.workflow_id}/runs/{failed_run.id}",
    }
    source["fields"] = [["client", "name"], ["edition"]]
    source["label_fields"] = [["client", "name"], ["edition"]]
    rows = await _source_rows(db_session, workspace, user, {"deliveries": source}, "deliveries",
                             {"kind": "project", "id": selected.id}, {})
    assert rows[0]["label"] == "Oak Studio · Autumn"
    assert rows[0]["created_at"] == selected.created_at.isoformat()
    selected.state = {"edition": "  "}
    rows = await _source_rows(db_session, workspace, user, {"deliveries": source}, "deliveries",
                             {"kind": "project", "id": selected.id}, {})
    assert rows[0]["label"] == "Client Delivery"


@pytest.mark.asyncio
async def test_inspection_records_bind_exact_readable_media_without_publication_metrics(db_session, monkeypatch):
    entity = Entity(name="Inspection app")
    owner = User(entity_id="pending", email="inspection-owner@test.com", password_hash="x", role="owner")
    user = User(entity_id="pending", email="inspection-viewer@test.com", password_hash="x", role="member")
    db_session.add_all([entity, owner, user])
    await db_session.flush()
    owner.entity_id = user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Site inspections", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(WorkspaceStaff(workspace_id=workspace.id, user_id=user.id, role="viewer", status="active"))
    photo = Document(entity_id=entity.id, name="Inspection.jpg", mime_type="image/jpeg", file_type="jpg",
                     visibility="workspace", owner_id=owner.id)
    private = Document(entity_id=entity.id, name="Private.jpg", mime_type="image/jpeg", file_type="jpg",
                       visibility="private", owner_id=owner.id)
    group = DocumentGroup(entity_id=entity.id, name="Evidence", workspace_id=workspace.id)
    db_session.add_all([photo, private, group])
    await db_session.flush()
    db_session.add(DocumentGroupMember(group_id=group.id, document_id=photo.id))
    projects = [WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="inspection", schema_version=1,
        project_key=f"inspection-{index}", current_stage="reviewing", created_by=owner.id,
        state={"title": title, "photo": doc_id, "outcome": "pass", "receipt": {"schema_version": "inspection/v1", "status": "reviewed"}})
        for index, (title, doc_id) in enumerate([("Site A", photo.id), ("Site B", private.id)])]
    empty = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="inspection", schema_version=1,
        project_key="not-inspected", state={"title": "Site C"}, created_by=owner.id)
    db_session.add_all([*projects, empty])
    workspace.operating_model = {"app_view": {"data_sources": {
        "inspections": {"type": "workflow_projects", "project_type": "inspection", "fields": [["title"], ["outcome"]],
                        "label_fields": [["title"]], "producer_workflows": ["workflow_definition:inspect"]},
        "checks": {"type": "project_records", "parent": "inspections", "record_path": ["receipt"], "record_schema": "inspection/v1"},
        "photos": {"type": "project_documents", "parent": "inspections", "document_path": ["photo"]},
    }, "components": [{"type": "data_view", "key": "inspections", "context": {"kind": "project", "source": "inspections"},
        "visibility": {"block": "checks", "when": "has_records"},
        "blocks": [{"key": "checks", "type": "records", "source": "checks", "record_display": {
            "title": {"source": "inspections", "field": ["title"]},
            "fields": [{"label": "Outcome", "source": "inspections", "field": ["outcome"], "labels": {"pass": "Passed"}}],
            "media": [{"source": "photos", "label": "Inspection photo"}],
        }}]}]}}
    await db_session.commit()
    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"inspect"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": []}
    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)
    async def data(project):
        return await component_data(db_session, workspace, user=user, component_key="inspections", context_kind="project", context_id=project.id)
    contexts = await component_contexts(db_session, workspace, user=user, component_key="inspections", limit=1)
    assert len(contexts["items"]) == 1 and contexts["next_cursor"]
    next_page = await component_contexts(db_session, workspace, user=user, component_key="inspections", limit=1, cursor=contexts["next_cursor"])
    assert {item["label"] for page in [contexts, next_page] for item in page["items"]} == {"Site A", "Site B"}
    assert (await data(empty))["visible"] is False
    item = (await data(projects[0]))["blocks"][0]["items"][0]
    assert item["title"] == "Site A" and item["status"] == "reviewed"
    assert item["fields"] == [{"label": "Outcome", "value": "Passed"}]
    assert item["media"] == [{"document_id": photo.id, "title": photo.name, "mime_type": photo.mime_type,
        "file_size": photo.file_size, "updated_at": (photo.updated_at or photo.created_at).isoformat(), "viewer_url": f"/viewer/{photo.id}", "label": "Inspection photo"}]
    assert not {"metrics", "platform", "published_at", "tracking_status"}.intersection(item)
    assert "media" not in (await data(projects[1]))["blocks"][0]["items"][0]
    other_workspace = Workspace(entity_id=entity.id, name="Other site", operating_model={})
    db_session.add(other_workspace)
    await db_session.flush()
    other_group = DocumentGroup(entity_id=entity.id, name="Other evidence", workspace_id=other_workspace.id)
    db_session.add(other_group)
    await db_session.flush()
    private.visibility = "workspace"
    db_session.add(DocumentGroupMember(group_id=other_group.id, document_id=private.id))
    await db_session.flush()
    assert "media" not in (await data(projects[1]))["blocks"][0]["items"][0]
    # Explicitly including the exact file in this workspace permits its preview.
    db_session.add(DocumentGroupMember(group_id=group.id, document_id=private.id))
    await db_session.flush()
    assert (await data(projects[1]))["blocks"][0]["items"][0]["media"][0]["document_id"] == private.id
    photo.is_trashed = True
    await db_session.flush()
    assert "media" not in (await data(projects[0]))["blocks"][0]["items"][0]
    photo.is_trashed = False
    photo.entity_id = "another-entity"
    await db_session.flush()
    assert "media" not in (await data(projects[0]))["blocks"][0]["items"][0]
    # Ordinary project rows use the same presentation without inventing a receipt.
    workspace.operating_model["app_view"]["components"][0]["blocks"][0]["source"] = "inspections"
    assert (await data(empty))["blocks"][0]["items"][0]["title"] == "Site C"


def test_launch_context_label_uses_workflow_metadata_not_internal_run_id():
    run = WorkflowRun(id="01M4RG00000000000000000000", status="running", definition_snapshot={"name": "Campaign launch"})
    label = _launch_label(run, "Publish campaign")
    assert label.startswith("Publish campaign · ")
    assert label.endswith(" · running")
    assert run.id not in label


@pytest.mark.parametrize(("published_url", "expected_href"), [
    ("https://www.youtube.com/watch?v=video", "https://www.youtube.com/watch?v=video"),
    ("javascript:alert(1)", None),
    ("/viewer/not-a-public-post", None),
])
def test_record_links_are_limited_to_public_http_urls(published_url, expected_href):
    item = _present_items({"type": "records", "fields": []}, [{
        "id": "receipt", "schema": "publication-receipt/v1", "record": {"published_url": published_url},
    }])[0]
    assert item["href"] == expected_href


@pytest.mark.asyncio
async def test_project_records_expose_only_the_parent_recent_execution(db_session):
    entity = Entity(name="Record execution entity")
    user = User(entity_id="pending", email="record-execution@test.com", password_hash="x", role="owner")
    other_entity = Entity(name="Other record execution entity")
    db_session.add_all([entity, user, other_entity])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Record execution", operating_model={})
    other_workspace = Workspace(entity_id=other_entity.id, name="Other record execution", operating_model={})
    db_session.add_all([workspace, other_workspace])
    await db_session.flush()
    project = WorkflowProject(
        entity_id=entity.id, workspace_id=workspace.id, project_type="delivery", schema_version=1,
        project_key="delivery", created_by=user.id,
        state={"receipt": {"schema_version": "receipt/v1", "title": "Delivered work"}},
    )
    related_run = WorkflowRun(
        entity_id=entity.id, workspace_id=workspace.id, workflow_id="delivery-flow",
        status="completed", current_step_id="done",
    )
    foreign_run = WorkflowRun(
        entity_id=other_entity.id, workspace_id=other_workspace.id, workflow_id="foreign-flow",
        status="completed", current_step_id="done",
    )
    db_session.add_all([project, related_run, foreign_run])
    await db_session.flush()
    project.last_run_id = foreign_run.id
    await db_session.commit()
    sources = {
        "deliveries": {"type": "workflow_projects", "project_type": "delivery", "schema_versions": [1],
                       "producer_workflows": ["workflow_definition:delivery"], "launch_workflows": [], "fields": []},
        "receipts": {"type": "project_records", "parent": "deliveries", "record_path": ["receipt"],
                     "record_schema": "receipt/v1", "producer_workflows": [], "fields": []},
    }
    context = {"kind": "project", "id": project.id}
    rows = await _source_rows(db_session, workspace, user, sources, "receipts", context, {})
    assert rows[0]["execution"] is None

    project.last_run_id = related_run.id
    await db_session.commit()
    rows = await _source_rows(db_session, workspace, user, sources, "receipts", context, {})
    assert rows[0]["execution"] == {
        "run_id": related_run.id, "status": "completed", "current_step_id": "done", "error": None,
        "href": f"/flows/{related_run.workflow_id}/runs/{related_run.id}",
    }
    assert _present_items({"type": "records", "fields": []}, rows)[0]["execution"] == rows[0]["execution"]


@pytest.mark.asyncio
async def test_a_plain_list_a_project_keeps_is_listed_whole(db_session):
    """A series' plan lists every episode, made or not; a versioned record source still drops strays."""
    entity = Entity(name="Plan list entity")
    user = User(entity_id="pending", email="plan-list@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Plan list", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    plan = [{"code": "E01", "title": "Farming", "synopsis": "Why farm?", "status": "made"},
            {"code": "E02", "title": "Cities", "synopsis": "Who feeds a potter?"}, "not a record"]
    series = WorkflowProject(
        entity_id=entity.id, workspace_id=workspace.id, project_type="series", schema_version=1,
        project_key="series", created_by=user.id, state={"episodes": plan},
    )
    db_session.add(series)
    await db_session.commit()
    sources = {
        "series": {"type": "workflow_projects", "project_type": "series", "schema_versions": [1],
                   "producer_workflows": ["workflow_definition:series"], "launch_workflows": [], "fields": []},
        "plan": {"type": "project_records", "parent": "series", "record_path": ["episodes"],
                 "producer_workflows": [], "fields": []},
        "versioned": {"type": "project_records", "parent": "series", "record_path": ["episodes"],
                      "record_schema": "episode/v1", "producer_workflows": [], "fields": []},
    }
    context = {"kind": "project", "id": series.id}
    rows = await _source_rows(db_session, workspace, user, sources, "plan", context, {})
    items = _present_items({"type": "records", "fields": [["synopsis"]]}, rows)
    assert [(item["title"], item["status"]) for item in items] == [("E01 · Farming", "made"), ("E02 · Cities", None)]
    assert items[1]["fields"] == [{"label": "synopsis", "value": "Who feeds a potter?"}]
    # The series' own last run did not make its plan items.
    assert all(item["execution"] is None for item in items)
    assert await _source_rows(db_session, workspace, user, sources, "versioned", context, {}) == []


@pytest.mark.asyncio
async def test_project_source_can_pin_execution_to_a_declared_state_run_id(db_session):
    """A later status-check run must not replace an App's substantive execution link."""
    entity = Entity(name="Pinned execution entity")
    user = User(entity_id="pending", email="pinned-execution@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Pinned execution", operating_model={})
    other_workspace = Workspace(entity_id=entity.id, name="Other pinned execution", operating_model={})
    db_session.add_all([workspace, other_workspace])
    await db_session.flush()
    publishing_run = WorkflowRun(
        entity_id=entity.id, workspace_id=workspace.id, workflow_id="publish-flow",
        status="failed", current_step_id="upload", error="Provider unavailable", lineage_is_legacy=False,
    )
    verification_run = WorkflowRun(
        entity_id=entity.id, workspace_id=workspace.id, workflow_id="verify-flow",
        status="completed", current_step_id="check",
    )
    foreign_run = WorkflowRun(
        entity_id=entity.id, workspace_id=other_workspace.id, workflow_id="foreign-flow",
        status="completed", current_step_id="done",
    )
    db_session.add_all([publishing_run, verification_run, foreign_run])
    await db_session.flush()
    publishing_run.lineage_root_run_id = publishing_run.id
    publishing_run.attempt_number = 1
    publication_retry = WorkflowRun(
        entity_id=entity.id, workspace_id=workspace.id, workflow_id="publish-flow",
        status="completed", current_step_id="complete", retry_of_run_id=publishing_run.id,
        lineage_root_run_id=publishing_run.id, lineage_is_legacy=False, attempt_number=2,
    )
    db_session.add(publication_retry)
    await db_session.flush()
    project = WorkflowProject(
        entity_id=entity.id, workspace_id=workspace.id, project_type="delivery", schema_version=1,
        project_key="delivery", created_by=user.id,
        state={"publishing": {"run_id": publishing_run.id}}, last_run_id=verification_run.id,
    )
    db_session.add(project)
    await db_session.commit()
    sources = {
        "deliveries": {"type": "workflow_projects", "project_type": "delivery", "schema_versions": [1],
                       "producer_workflows": ["workflow_definition:delivery"], "launch_workflows": [], "fields": [],
                       "execution_run_id_path": ["publishing", "run_id"]},
    }
    rows = await _source_rows(db_session, workspace, user, sources, "deliveries", {"kind": "project", "id": project.id}, {})
    assert rows[0]["execution"] == {
        "run_id": publication_retry.id, "status": "completed", "current_step_id": "complete", "error": None,
        "href": f"/flows/{publication_retry.workflow_id}/runs/{publication_retry.id}",
    }

    # Without the explicit business-run path, retain the established exact
    # last_run_id projection rather than following a retry family.
    unconfigured_sources = {
        "deliveries": {key: value for key, value in sources["deliveries"].items()
                       if key != "execution_run_id_path"},
    }
    rows = await _source_rows(
        db_session, workspace, user, unconfigured_sources, "deliveries",
        {"kind": "project", "id": project.id}, {},
    )
    assert rows[0]["execution"] == {
        "run_id": verification_run.id, "status": "completed", "current_step_id": "check", "error": None,
        "href": f"/flows/{verification_run.workflow_id}/runs/{verification_run.id}",
    }

    project.state = {"publishing": {"run_id": foreign_run.id}}
    await db_session.commit()
    rows = await _source_rows(db_session, workspace, user, sources, "deliveries", {"kind": "project", "id": project.id}, {})
    assert rows[0]["execution"] is None


def test_provider_field_shows_logo_key_label_and_first_available_account():
    platform = {"label": "Platform", "field": ["channel", "platform"], "format": "provider", "hide_when_empty": True,
                "labels": {"tiktok": "TikTok"}, "secondary": [["channel", "account_label"], ["channel", "creator_username"]]}
    items = _present_items({"type": "progress", "fields": [platform]}, [
        {"id": "a", "fields": {"channel.platform": "TikTok", "channel.account_label": "", "channel.creator_username": "jrmusic"}},
        {"id": "b", "fields": {"channel.platform": None, "channel.account_label": "Unused"}},
    ])

    assert items[0]["fields"] == [
        {"label": "Platform", "format": "provider", "value": "TikTok", "provider": "tiktok", "secondary": "jrmusic"},
    ]
    assert items[1]["fields"] == []


def test_metric_links_are_limited_to_public_http_urls():
    item = _present_items({"type": "metrics", "fields": []}, [{
        "id": "metric", "key": "views", "public_url": "javascript:alert(1)",
    }])[0]
    assert item["href"] is None


def test_metric_latest_unavailable_does_not_display_old_current_value():
    item = _present_items({"type": "metrics", "fields": []}, [{
        "id": "metric", "key": "views", "current_value": 0,
        "comparison": {"latest_value": None, "delta_reason": "unavailable"},
    }])[0]
    assert item["value"] is None
    cached = _present_items({"type": "metrics", "fields": []}, [{
        "id": "metric", "key": "views", "current_value": 0,
    }])[0]
    assert cached["value"] == 0


def test_metric_presentation_retains_its_collector_metric_key():
    item = _present_items({"type": "metrics", "fields": []}, [{
        "id": "metric", "key": "youtube.views", "metric_key": "views", "current_value": 12,
    }])[0]

    assert item["metric_key"] == "views"


@pytest.mark.asyncio
async def test_course_video_records_attach_only_their_own_metrics_and_deduplicate_before_paging(db_session, monkeypatch):
    entity = Entity(name="Course publication records")
    user = User(entity_id="pending", email="course-publications@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Course videos", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    course = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course", schema_version=1,
                             project_key="course", state={"title": "Course A"}, created_by=user.id)
    db_session.add(course)
    await db_session.flush()
    # This source pages by id. ULIDs created in the same millisecond are not
    # insertion-ordered, so explicitly order both the empty page and receipts.
    lesson_ids = iter(sorted(generate_ulid() for _ in range(106)))
    def lesson(key, title, url, external_id):
        return WorkflowProject(id=next(lesson_ids), entity_id=entity.id, workspace_id=workspace.id, project_type="lesson", schema_version=1,
            project_key=key, state={"course_id": course.id, "video_title": title, "publication": {
                "schema_version": "publication-receipt/v1", "verification_status": "verified", "platform": "youtube", "published_url": url,
                "external_id": external_id, "published_at": "2026-10-12T00:00:00Z", "account": "course-channel",
            }}, created_by=user.id)
    # The record source must advance through an empty parent page before it
    # reaches these verified lesson videos.
    db_session.add_all([WorkflowProject(id=next(lesson_ids), entity_id=entity.id, workspace_id=workspace.id, project_type="lesson", schema_version=1,
        project_key=f"empty-{index:03d}", state={"course_id": course.id, "video_title": "Not published"}, created_by=user.id)
        for index in range(101)])
    first = lesson("lesson-1", "First lesson", "https://www.youtube.com/watch?v=abcdEFGhijk", "abcdEFGhijk")
    duplicate = lesson("lesson-2", "Repeated receipt", "https://www.youtube.com/watch?v=abcdEFGhijk", "abcdEFGhijk")
    first.state["publication"]["payload_hash"] = "a" * 64
    duplicate.state["publication"]["payload_hash"] = "b" * 64
    second = lesson("lesson-3", "Second lesson", "https://www.youtube.com/watch?v=zyxWVUt9876", "zyxWVUt9876")
    untracked = lesson("lesson-4", "Untracked lesson", "https://www.youtube.com/watch?v=untracked01", "untracked01")
    unverified = lesson("lesson-unverified", "Unverified lesson", "https://www.youtube.com/watch?v=unverified1", "unverified1")
    unverified.state["publication"]["verification_status"] = "unverified"
    db_session.add_all([first, duplicate, second, untracked, unverified])
    await db_session.flush()
    subject = {"kind": "workflow_project", "id": course.id}
    first_views = WorkspaceStat(entity_id=entity.id, workspace_id=workspace.id, key="first.views", name="Views", current_value=0,
                      collector_type="public_post", collector_config={"metric_key": "views", "subject": subject,
                          "platform": "youtube", "public_url": "https://www.youtube.com/watch?v=abcdEFGhijk", "account_id": "course-channel"})
    second_views = WorkspaceStat(entity_id=entity.id, workspace_id=workspace.id, key="second.views", name="Views", current_value=3,
                      collector_type="public_post", collector_config={"metric_key": "views", "subject": subject,
                          "platform": "youtube", "public_url": "https://www.youtube.com/watch?v=zyxWVUt9876", "account_id": "course-channel"})
    second_likes = WorkspaceStat(entity_id=entity.id, workspace_id=workspace.id, key="second.likes", name="Likes", current_value=1,
                      collector_type="public_post", collector_config={"metric_key": "likes", "subject": subject,
                          "platform": "youtube", "public_url": "https://www.youtube.com/watch?v=zyxWVUt9876", "account_id": "course-channel"})
    db_session.add_all([first_views, second_views, second_likes])
    await db_session.flush()
    db_session.add(WorkspaceStatObservation(
        entity_id=entity.id, workspace_id=workspace.id, stat_id=first_views.id, value=0,
        observed_at=datetime.now(UTC), source="test", idempotency_key="first-views-zero",
    ))
    workspace.operating_model = {"app_view": {"data_sources": {
        "courses": {"type": "workflow_projects", "project_type": "course", "schema_versions": [1], "fields": [["title"]], "producer_workflows": ["workflow_definition:course"], "launch_workflows": []},
        "lessons": {"type": "related_projects", "project_type": "lesson", "schema_versions": [1], "parent": "courses", "parent_state_path": ["course_id"], "fields": [["video_title"], ["publication", "platform"], ["channel_title"], ["publication", "account"]], "producer_workflows": ["workflow_definition:lesson"]},
        "publications": {"type": "project_records", "parent": "lessons", "record_path": ["publication"], "record_schema": "publication-receipt/v1", "producer_workflows": [], "fields": []},
        "results": {"type": "publication_stats", "parent": "publications", "subject": "courses", "metric_keys": ["views", "likes"], "producer_workflows": [], "fields": []},
    }, "components": [{"type": "data_view", "schema_version": 1, "key": "course", "context": {"kind": "project", "source": "courses"}, "blocks": [{
        "key": "published", "type": "records", "source": "publications", "metrics_source": "results", "limit": 1,
        "record_display": {"title": {"source": "lessons", "field": ["video_title"]}, "fields": [
            {"label": "Course", "source": "courses", "field": ["title"]},
            {"label": "Channel", "source": "lessons", "field": ["publication", "platform"], "format": "provider",
             "labels": {"youtube": "YouTube"}, "secondary": [["channel_title"], ["publication", "account"]]},
        ]},
    }]}]}}
    await db_session.commit()
    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"course", "lesson"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": []}
    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)
    first_page = await component_data(db_session, workspace, user=user, component_key="course", context_kind="project", context_id=course.id)
    item = first_page["blocks"][0]["items"][0]
    assert item["title"] == "First lesson"
    # The provider keeps its raw key for the logo; the first non-empty
    # declared fallback names the account.
    assert item["fields"] == [
        {"label": "Course", "value": "Course A"},
        {"label": "Channel", "format": "provider", "value": "YouTube", "provider": "youtube", "secondary": "course-channel"},
    ]
    assert item["tracking_status"] == "partial"
    assert "material identity" in item["conflict"]
    assert item["published_at"] == "2026-10-12T00:00:00Z"
    assert item["metrics"][0]["metric_key"] == "views"
    assert item["metrics"][0]["value"] == 0
    second_page = await component_data(db_session, workspace, user=user, component_key="course", context_kind="project", context_id=course.id,
                                       block_key="published", cursor=first_page["blocks"][0]["next_cursor"])
    item = second_page["blocks"][0]["items"][0]
    assert item["title"] == "Second lesson"
    assert item["tracking_status"] == "configured"
    assert {metric["metric_key"] for metric in item["metrics"]} == {"views", "likes"}
    third_page = await component_data(db_session, workspace, user=user, component_key="course", context_kind="project", context_id=course.id,
                                      block_key="published", cursor=second_page["blocks"][0]["next_cursor"])
    item = third_page["blocks"][0]["items"][0]
    assert item["title"] == "Untracked lesson"
    assert {metric["metric_key"] for metric in item["metrics"]} == {"views", "likes"}
    assert all(metric["value"] is None and metric["delta_reason"] == "no_observation" for metric in item["metrics"])
    assert item["tracking_status"] == "pending"
    assert third_page["blocks"][0]["next_cursor"] is None

    # Course lessons can collect different metrics. Configured-only display
    # omits an inapplicable metric, while an untracked post still stays pending.
    publication_block = workspace.operating_model["app_view"]["components"][0]["blocks"][0]
    publication_block["metrics_display"] = "configured"
    configured_page = await component_data(db_session, workspace, user=user, component_key="course", context_kind="project", context_id=course.id)
    configured_item = configured_page["blocks"][0]["items"][0]
    assert [metric["metric_key"] for metric in configured_item["metrics"]] == ["views"]
    assert configured_item["metrics"][0]["value"] == 0
    assert configured_item["tracking_status"] == "configured"
    configured_pending = await component_data(db_session, workspace, user=user, component_key="course", context_kind="project", context_id=course.id,
                                             block_key="published", cursor=second_page["blocks"][0]["next_cursor"])
    assert configured_pending["blocks"][0]["items"][0]["metrics"] == []
    assert configured_pending["blocks"][0]["items"][0]["tracking_status"] == "pending"
    publication_block.pop("metrics_display")

    # Tracking-only sections qualify both the picker and its records before
    # pagination. Cached values, conflicted receipts and unknowns are not proof.
    workspace.operating_model["app_view"]["components"][0]["visibility"] = {
        "block": "published", "when": "has_observations",
    }
    newer_course = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course", schema_version=1,
        project_key="not-published", state={"title": "Unpublished course"}, created_by=user.id)
    observation = WorkspaceStatObservation(entity_id=entity.id, workspace_id=workspace.id, stat_id=second_views.id,
        value=None, observed_at=datetime.now(UTC) - timedelta(minutes=1), source="test", idempotency_key="second-views")
    db_session.add_all([newer_course, observation])
    await db_session.flush()
    async def contexts():
        return await component_contexts(db_session, workspace, user=user, component_key="course", limit=1)
    async def data(project_id=course.id):
        return await component_data(db_session, workspace, user=user, component_key="course", context_kind="project", context_id=project_id)
    assert (await contexts())["items"] == []
    assert (await data())["visible"] is False
    observation.value = 0
    await db_session.flush()
    picker = await contexts()
    assert [item["id"] for item in picker["items"]] == [course.id]
    assert picker["next_cursor"] is None
    visible = await data()
    assert visible["visible"] is True
    assert [item["title"] for item in visible["blocks"][0]["items"]] == ["Second lesson"]
    assert visible["blocks"][0]["items"][0]["metrics"][0]["value"] == 0
    assert visible["blocks"][0]["next_cursor"] is None
    assert (await data(newer_course.id))["visible"] is False

    # A later collection failure must not erase a previously tracked entry.
    second_views.last_collection_status = "error"
    second_views.last_collection_error = "Platform temporarily unavailable"
    db_session.add(WorkspaceStatObservation(entity_id=entity.id, workspace_id=workspace.id, stat_id=second_views.id,
        value=None, observed_at=datetime.now(UTC), source="test", idempotency_key="second-views-unavailable"))
    await db_session.flush()
    visible = await data()
    assert visible["visible"] is True
    assert visible["blocks"][0]["items"][0]["metrics"][0]["collection_error"] == "Platform temporarily unavailable"

    # Receipt visibility starts at the verified publication, so a configured
    # collector without samples and an entirely absent collector stay visible.
    workspace.operating_model["app_view"]["components"][0]["visibility"] = {
        "block": "published", "when": "has_records",
    }
    workspace.operating_model["app_view"]["components"][0]["blocks"][0]["limit"] = 100
    picker = await contexts()
    assert [item["id"] for item in picker["items"]] == [course.id]
    visible = await data()
    records = {item["title"]: item for item in visible["blocks"][0]["items"]}
    assert records["Untracked lesson"]["tracking_status"] == "pending"
    assert all(metric["status"] == "no_data" for metric in records["Untracked lesson"]["metrics"])
    assert next(metric for metric in records["Second lesson"]["metrics"] if metric["metric_key"] == "views")["collection_error"] == "Platform temporarily unavailable"

    workspace.operating_model["app_view"]["components"][0]["visibility"]["when"] = "has_observations"
    for target, field, invalid in [
        (second_views, "status", "paused"),
        (second_views, "revision", 2),
        (second_views, "workspace_id", "another-workspace"),
        (observation, "entity_id", "another-entity"),
        (observation, "observed_at", datetime.now(UTC) + timedelta(days=1)),
        (second_views, "collector_config", {**second_views.collector_config, "account_id": "another-channel"}),
        (second_views, "collector_config", {**second_views.collector_config, "subject": {"kind": "workflow_project", "id": newer_course.id}}),
    ]:
        original = getattr(target, field)
        setattr(target, field, invalid)
        await db_session.flush()
        assert (await contexts())["items"] == [], field
        setattr(target, field, original)
        await db_session.flush()


@pytest.mark.asyncio
async def test_publication_stats_never_cross_match_subject_and_receipt_account_sets(db_session):
    entity = Entity(name="Publication subject isolation")
    user = User(entity_id="pending", email="publication-subjects@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Subject isolation", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    courses = [WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course", schema_version=1,
                               project_key=f"course-{index}", state={}, created_by=user.id) for index in range(2)]
    db_session.add_all(courses)
    await db_session.flush()
    url = "https://www.youtube.com/watch?v=subject0001"
    lessons = [WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="lesson", schema_version=1,
        project_key=f"lesson-{index}", state={"course_id": course.id, "publication": {
            "schema_version": "publication-receipt/v1", "verification_status": "verified", "platform": "youtube",
            "published_url": url, "external_id": "subject0001", "account": f"channel-{index}",
        }}, created_by=user.id) for index, course in enumerate(courses)]
    db_session.add_all(lessons)
    await db_session.flush()
    db_session.add(WorkspaceStat(entity_id=entity.id, workspace_id=workspace.id, key="wrong-account", name="Views",
        collector_type="public_post", collector_config={"metric_key": "views", "subject": {"kind": "workflow_project", "id": courses[0].id},
            "platform": "youtube", "public_url": url, "account_id": "channel-1"}))
    await db_session.commit()
    sources = {
        "courses": {"type": "workflow_projects", "project_type": "course", "schema_versions": [1], "producer_workflows": [], "launch_workflows": [], "fields": []},
        "lessons": {"type": "related_projects", "project_type": "lesson", "schema_versions": [1], "parent": "courses", "parent_state_path": ["course_id"], "producer_workflows": [], "fields": []},
        "publications": {"type": "project_records", "parent": "lessons", "record_path": ["publication"], "record_schema": "publication-receipt/v1", "producer_workflows": [], "fields": []},
        "results": {"type": "publication_stats", "parent": "publications", "subject": "courses", "metric_keys": ["views"], "producer_workflows": [], "fields": []},
    }
    rows = await _source_rows(db_session, workspace, user, sources, "results", {"kind": "workspace", "id": workspace.id}, {})
    assert rows == []


@pytest.mark.asyncio
async def test_publication_stats_read_declared_manual_assessments_bound_to_the_receipt(db_session):
    """Evidence-backed manual Stats appear only when the source declares them."""
    entity = Entity(name="Manual publication metrics")
    user = User(entity_id="pending", email="manual-publication-metrics@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Course clips", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    course = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course", schema_version=1,
                             project_key="course", state={}, created_by=user.id)
    db_session.add(course)
    await db_session.flush()
    url = "https://www.youtube.com/watch?v=manual00001"
    db_session.add(WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="lesson", schema_version=1,
        project_key="lesson", state={"course_id": course.id, "publication": {
            "schema_version": "publication-receipt/v1", "verification_status": "verified", "platform": "youtube",
            "published_url": url, "external_id": "manual00001", "account": "course-channel",
        }}, created_by=user.id))
    subject = {"kind": "workflow_project", "id": course.id}
    views = WorkspaceStat(entity_id=entity.id, workspace_id=workspace.id, key="lesson.views", name="Views", current_value=42,
        collector_type="manual", collector_config={"metric_key": "views", "subject": subject, "source": "visible_page",
            "platform": "youtube", "public_url": url, "account_id": "course-channel"})
    other_account = WorkspaceStat(entity_id=entity.id, workspace_id=workspace.id, key="other.views", name="Views",
        collector_type="manual", collector_config={"metric_key": "views", "subject": subject, "source": "visible_page",
            "platform": "youtube", "public_url": url, "account_id": "another-channel"})
    db_session.add_all([views, other_account])
    await db_session.commit()
    results = {"type": "publication_stats", "parent": "publications", "subject": "courses", "metric_keys": ["views"],
               "producer_workflows": [], "fields": []}
    sources = {
        "courses": {"type": "workflow_projects", "project_type": "course", "schema_versions": [1], "producer_workflows": [], "launch_workflows": [], "fields": []},
        "lessons": {"type": "related_projects", "project_type": "lesson", "schema_versions": [1], "parent": "courses", "parent_state_path": ["course_id"], "producer_workflows": [], "fields": []},
        "publications": {"type": "project_records", "parent": "lessons", "record_path": ["publication"], "record_schema": "publication-receipt/v1", "producer_workflows": [], "fields": []},
        "results": results,
    }
    context = {"kind": "workspace", "id": workspace.id}
    # The default remains the automatic public-post collector.
    assert await _source_rows(db_session, workspace, user, sources, "results", context, {}) == []
    results["collector_types"] = ["manual"]
    rows = await _source_rows(db_session, workspace, user, sources, "results", context, {})
    # Receipt identity still applies: the other account's Stat is excluded.
    assert [(row["key"], row["current_value"]) for row in rows] == [("lesson.views", 42.0)]


def test_stat_freshness_uses_collection_error_and_limit():
    now = datetime(2026, 10, 13, tzinfo=UTC)
    stale = WorkspaceStat(key="views", name="Views", entity_id="entity", workspace_id="workspace",
                          collector_type="public_post", status="active", freshness_limit_seconds=60,
                          current_value_updated_at=now - timedelta(seconds=61))
    failed = WorkspaceStat(key="likes", name="Likes", entity_id="entity", workspace_id="workspace",
                           collector_type="public_post", status="active", last_collection_status="error")
    assert stat_freshness_status(stale, now=now) == "stale"
    assert stat_freshness_status(failed, now=now) == "collection_error"


@pytest.mark.asyncio
async def test_owned_automation_prefers_its_binding_name_when_job_name_is_identifier(db_session):
    entity = Entity(name="Automation label entity")
    user = User(entity_id="pending", email="automation-label@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Automation label", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    project = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="campaign",
                              schema_version=1, project_key="campaign", state={}, created_by=user.id)
    binding = WorkflowBinding(entity_id=entity.id, workspace_id=workspace.id, workflow_id="campaign-flow",
                              name="Collect weekly results")
    db_session.add_all([project, binding])
    await db_session.flush()
    job = ScheduledJob(entity_id=entity.id, workspace_id=workspace.id, owner_project_id=project.id,
                       job_id="collect-results", name="collect-results", execution_target={"binding_id": binding.id})
    custom = ScheduledJob(entity_id=entity.id, workspace_id=workspace.id, owner_project_id=project.id,
                          job_id="publish-results", name="Publish the approved report", execution_target={"binding_id": binding.id})
    slug = ScheduledJob(entity_id=entity.id, workspace_id=workspace.id, owner_project_id=project.id,
                        job_id="collect-results:campaign-1", name="collect-weekly-results",
                        execution_target={"workflow_id": binding.workflow_id})
    disabled = ScheduledJob(entity_id=entity.id, workspace_id=workspace.id, owner_project_id=project.id,
                            job_id="disabled-results", name="Disabled results", enabled=False, last_status="completed",
                            stop_after=datetime.now(UTC) + timedelta(days=1))
    ended = ScheduledJob(entity_id=entity.id, workspace_id=workspace.id, owner_project_id=project.id,
                         job_id="ended-results", name="Ended results", enabled=False, last_status="completed",
                         stop_after=datetime.now(UTC) - timedelta(days=1))
    db_session.add_all([job, custom, slug, disabled, ended])
    await db_session.commit()
    sources = {
        "campaigns": {"type": "workflow_projects", "project_type": "campaign", "schema_versions": [1],
                      "producer_workflows": ["workflow_definition:campaign-flow"], "launch_workflows": [], "fields": []},
        "jobs": {"type": "owned_automations", "parent": "campaigns", "producer_workflows": [], "fields": []},
    }

    rows = await _source_rows(db_session, workspace, user, sources, "jobs", {"kind": "project", "id": project.id}, {})

    labels = {row["job_id"]: row["label"] for row in rows}
    assert labels == {
        "collect-results": "Collect weekly results",
        "collect-results:campaign-1": "Collect weekly results",
        "disabled-results": "Disabled results",
        "ended-results": "Ended results",
        "publish-results": "Publish the approved report",
    }
    by_job = {row["job_id"]: row for row in rows}
    assert by_job["disabled-results"]["state"] == "disabled"
    assert by_job["ended-results"]["state"] == "ended"
    presented = {item["id"]: item for item in _present_items({"type": "progress", "fields": []}, rows)}
    assert presented[disabled.id]["status"] == "disabled"
    assert presented[ended.id]["status"] == "ended"


@pytest.mark.asyncio
async def test_project_context_pages_into_unmaterialized_launches(db_session, monkeypatch):
    entity = Entity(name="Context page entity")
    user = User(entity_id="pending", email="context-page@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Context page", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    binding = WorkflowBinding(entity_id=entity.id, workspace_id=workspace.id, workflow_id="definition", name="Launch")
    db_session.add(binding)
    await db_session.flush()
    projects = [WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="campaign", schema_version=1,
                                project_key=f"campaign-{index}", state={}, created_by=user.id) for index in range(2)]
    db_session.add_all(projects)
    await db_session.flush()
    materialized = WorkflowRun(entity_id=entity.id, workspace_id=workspace.id, workflow_id="definition", binding_id=binding.id,
        status="completed", definition_snapshot={"steps": [{"id": "create", "type": "workflow_project", "config": {"operation": "create", "project_type": "campaign", "schema_version": 1}}]},
        step_results={"create": {"status": "completed", "output": {"project_id": projects[0].id, "project_type": "campaign", "schema_version": 1}}})
    pending = WorkflowRun(entity_id=entity.id, workspace_id=workspace.id, workflow_id="definition", binding_id=binding.id, status="running")
    db_session.add_all([materialized, pending])
    workspace.operating_model = {"app_view": {"data_sources": {"campaigns": {"type": "workflow_projects", "project_type": "campaign", "schema_versions": [1], "producer_workflows": ["workflow_definition:definition"], "launch_workflows": [{"workflow": f"workflow:{binding.id}", "project_steps": ["create"]}], "fields": []}}, "components": [{"type": "data_view", "schema_version": 1, "key": "campaigns", "context": {"kind": "project", "source": "campaigns"}, "blocks": [{"key": "progress", "type": "progress", "source": "campaigns", "limit": 10}]}]}}
    await db_session.commit()
    async def catalog(*_args, **_kwargs): return {"workflow_definitions": {"definition"}, "workflow_bindings": {binding.id}, "stat_keys": set(), "workflows": []}
    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)
    first = await component_contexts(db_session, workspace, user=user, component_key="campaigns", limit=2)
    assert [item["kind"] for item in first["items"]] == ["project", "project"]
    assert first["next_cursor"].startswith("p:")
    second = await component_contexts(db_session, workspace, user=user, component_key="campaigns", cursor=first["next_cursor"], limit=2)
    assert [item["id"] for item in second["items"]] == [pending.id]
    assert second["next_cursor"] is None
    combined = await component_contexts(db_session, workspace, user=user, component_key="campaigns", limit=10)
    assert {item["id"] for item in combined["items"]} == {projects[0].id, projects[1].id, pending.id}
    assert len({item["id"] for item in combined["items"]}) == len(combined["items"])
    assert combined["next_cursor"] is None

    # A projects-only Page must not treat unsuccessful launch attempts as projects.
    db_session.add_all([
        WorkflowRun(entity_id=entity.id, workspace_id=workspace.id, workflow_id="definition",
                    binding_id=binding.id, status=status) for status in ("failed", "cancelled")
    ])
    source = workspace.operating_model["app_view"]["data_sources"]["campaigns"]
    source.update(launch_workflows=[], fields=[["title"]], label_fields=[["title"]])
    for project in projects:
        project.state = {"title": "Autumn catalog"}
    await db_session.flush()
    actual = await component_contexts(db_session, workspace, user=user, component_key="campaigns", limit=10)
    assert {item["id"] for item in actual["items"]} == {project.id for project in projects}
    assert {item["label"] for item in actual["items"]} == {"Autumn catalog"}
    assert all(item["kind"] == "project" and item["created_at"] for item in actual["items"])
    assert actual["next_cursor"] is None


@pytest.mark.asyncio
async def test_component_data_continues_after_an_empty_record_parent_page(db_session, monkeypatch):
    entity = Entity(name="Empty record page entity")
    user = User(entity_id="pending", email="empty-record-page@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Empty record page", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    campaign = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="campaign", schema_version=1, project_key="root", state={}, created_by=user.id)
    db_session.add(campaign)
    await db_session.flush()
    slots = [WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="slot", schema_version=1, project_key=f"slot-{i:03d}", state={"parent_project_id": campaign.id, "due_at": "2026-11-03T09:00:00+00:00"}, created_by=user.id) for i in range(101)]
    slots.append(WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="slot", schema_version=1, project_key="slot-valid", state={"parent_project_id": campaign.id, "receipt": {"schema_version": "receipt/v1", "title": "Later receipt"}}, created_by=user.id))
    db_session.add_all(slots)
    workspace.operating_model = {"app_view": {"data_sources": {"campaigns": {"type": "workflow_projects", "project_type": "campaign", "schema_versions": [1], "producer_workflows": ["workflow_definition:campaign"], "launch_workflows": [], "fields": []}, "slots": {"type": "related_projects", "project_type": "slot", "schema_versions": [1], "producer_workflows": ["workflow_definition:slot"], "parent": "campaigns", "parent_state_path": ["parent_project_id"], "fields": [["due_at"]], "order": {"field": ["due_at"], "type": "datetime", "direction": "asc"}}, "receipts": {"type": "project_records", "parent": "slots", "record_path": ["receipt"], "record_schema": "receipt/v1", "producer_workflows": [], "fields": []}}, "components": [{"type": "data_view", "schema_version": 1, "key": "campaign", "context": {"kind": "project", "source": "campaigns"}, "blocks": [{"key": "receipts", "type": "records", "source": "receipts", "limit": 100}]}]}}
    await db_session.commit()
    async def catalog(*_args, **_kwargs): return {"workflow_definitions": {"campaign", "slot"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": []}
    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)
    first = await component_data(db_session, workspace, user=user, component_key="campaign", context_kind="project", context_id=campaign.id)
    assert first["blocks"][0]["items"] == []
    assert first["blocks"][0]["next_cursor"]
    second = await component_data(db_session, workspace, user=user, component_key="campaign", context_kind="project", context_id=campaign.id, block_key="receipts", cursor=first["blocks"][0]["next_cursor"])
    assert [item["title"] for item in second["blocks"][0]["items"]] == ["Later receipt"]

    sources = workspace.operating_model["app_view"]["data_sources"]
    sources["documents"] = {"type": "project_documents", "parent": "slots", "document_path": ["artifact", "document_id"],
                            "document_role": "artifact", "producer_workflows": [], "fields": []}
    document_rows = await _source_rows(db_session, workspace, user, sources, "documents",
                                       {"kind": "project", "id": campaign.id}, {}, page_limit=101)
    assert document_rows == []
    assert document_rows.next_cursor
    assert await _source_rows(db_session, workspace, user, sources, "documents",
                              {"kind": "project", "id": campaign.id}, {},
                              after_id=document_rows.next_cursor, page_limit=101) == []


@pytest.mark.asyncio
async def test_component_data_publication_stats_scans_past_unmatched_receipt_batch(db_session, monkeypatch):
    entity = Entity(name="manor_app_data_verify")
    user = User(entity_id="pending", email="app-data-stats-verify@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Publication stat scan", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    campaign = WorkflowProject(
        entity_id=entity.id, workspace_id=workspace.id, project_type="campaign", schema_version=1,
        project_key="release", state={"publication": {
            "schema_version": "publication-receipt/v1", "verification_status": "verified", "platform": "youtube",
            "published_url": "https://www.youtube.com/watch?v=matching001", "account": "channel-a",
        }}, created_by=user.id,
    )
    db_session.add(campaign)
    await db_session.flush()
    subject = {"kind": "workflow_project", "id": campaign.id}
    db_session.add_all([
        WorkspaceStat(
            entity_id=entity.id, workspace_id=workspace.id, key=f"views.unmatched.{index:03d}",
            name=f"Unmatched views {index}", collector_type="public_post", collector_config={
                "metric_key": "views", "subject": subject, "platform": "youtube",
                "public_url": f"https://www.youtube.com/watch?v=bad{index:08d}", "account_id": "channel-a",
            },
        )
        for index in range(101)
    ])
    await db_session.flush()
    matching = WorkspaceStat(
        entity_id=entity.id, workspace_id=workspace.id, key="views.matching", name="Matching views",
        collector_type="public_post", collector_config={
            "metric_key": "views", "subject": subject, "platform": "youtube",
            "public_url": "https://www.youtube.com/watch?v=matching001", "account_id": "channel-a",
        },
    )
    db_session.add(matching)
    workspace.operating_model = {"app_view": {"data_sources": {
        "campaigns": {"type": "workflow_projects", "project_type": "campaign", "schema_versions": [1],
                      "producer_workflows": ["workflow_definition:campaign"], "launch_workflows": [], "fields": []},
        "publications": {"type": "project_records", "parent": "campaigns", "record_path": ["publication"],
                         "record_schema": "publication-receipt/v1", "producer_workflows": [], "fields": []},
        "results": {"type": "publication_stats", "parent": "publications", "subject": "campaigns",
                    "metric_keys": ["views"], "producer_workflows": [], "fields": []},
    }, "components": [{"type": "data_view", "schema_version": 1, "key": "campaign", "context": {
        "kind": "project", "source": "campaigns"}, "blocks": [{
            "key": "results", "type": "metrics", "source": "results", "limit": 1,
        }]}]}}
    await db_session.commit()

    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"campaign"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": []}

    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)
    response = await component_data(
        db_session, workspace, user=user, component_key="campaign", context_kind="project", context_id=campaign.id,
    )
    block = response["blocks"][0]
    assert [item["id"] for item in block["items"]] == [matching.id]
    assert block["next_cursor"] is None


@pytest.mark.asyncio
async def test_component_data_document_acl_filters_a_page_and_continues_to_workspace_document(db_session, monkeypatch):
    entity = Entity(name="Document ACL page entity")
    owner = User(entity_id="pending", email="document-owner@test.com", password_hash="x", role="owner")
    user = User(entity_id="pending", email="document-member@test.com", password_hash="x", role="member")
    db_session.add_all([entity, owner, user])
    await db_session.flush()
    owner.entity_id = entity.id
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Document ACL page", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    db_session.add(WorkspaceStaff(workspace_id=workspace.id, user_id=user.id, role="viewer", status="active"))
    private = Document(entity_id=entity.id, name="private-first.md", file_type="md", source="upload",
                       visibility="private", owner_id=owner.id)
    private_second = Document(entity_id=entity.id, name="private-second.md", file_type="md", source="upload",
                              visibility="private", owner_id=owner.id)
    readable = Document(entity_id=entity.id, name="workspace-visible.md", file_type="md", source="upload",
                        visibility="workspace", owner_id=owner.id)
    db_session.add_all([private, private_second, readable])
    await db_session.flush()
    group = DocumentGroup(entity_id=entity.id, name="Workspace documents", workspace_id=workspace.id)
    db_session.add(group)
    await db_session.flush()
    db_session.add(DocumentGroupMember(group_id=group.id, document_id=readable.id))
    projects = [
        WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="campaign", schema_version=1,
                        project_key="private-first", state={"artifact": {"document_id": private.id}}, created_by=owner.id),
        WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="campaign", schema_version=1,
                        project_key="private-second", state={"artifact": {"document_id": private_second.id}}, created_by=owner.id),
        WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="campaign", schema_version=1,
                        project_key="workspace-visible", state={"artifact": {"document_id": readable.id}}, created_by=owner.id),
    ]
    db_session.add_all(projects)
    workspace.operating_model = {"app_view": {"data_sources": {
        "campaigns": {"type": "workflow_projects", "project_type": "campaign", "schema_versions": [1],
                      "producer_workflows": ["workflow_definition:campaign"], "launch_workflows": [], "fields": []},
        "documents": {"type": "project_documents", "parent": "campaigns", "document_path": ["artifact", "document_id"],
                      "document_role": "artifact", "producer_workflows": [], "fields": []},
    }, "components": [{"type": "data_view", "schema_version": 1, "key": "workspace_documents", "context": {
        "kind": "workspace"}, "blocks": [{"key": "documents", "type": "documents", "source": "documents", "limit": 1}]}]}}
    await db_session.commit()

    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"campaign"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": []}

    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)
    first = await component_data(
        db_session, workspace, user=user, component_key="workspace_documents", context_kind="workspace", context_id=workspace.id,
    )
    assert first["blocks"][0]["items"] == []
    assert first["blocks"][0]["next_cursor"]
    second = await component_data(
        db_session, workspace, user=user, component_key="workspace_documents", context_kind="workspace", context_id=workspace.id,
        block_key="documents", cursor=first["blocks"][0]["next_cursor"],
    )
    assert [(item["document_id"], item["title"]) for item in second["blocks"][0]["items"]] == [
        (readable.id, "workspace-visible.md"),
    ]
    assert private.id not in {item["document_id"] for item in second["blocks"][0]["items"]}
    assert private_second.id not in {item["document_id"] for item in second["blocks"][0]["items"]}
    assert second["blocks"][0]["next_cursor"] is None


@pytest.mark.asyncio
async def test_metric_and_automation_sources_use_their_own_keyset_cursors(db_session):
    entity = Entity(name="App source pagination entity")
    user = User(entity_id="pending", email="app-source-pagination@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="App source pagination", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    root = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="campaign",
                           schema_version=1, project_key="root", state={}, created_by=user.id)
    db_session.add(root)
    await db_session.flush()
    db_session.add_all([WorkspaceStat(entity_id=entity.id, workspace_id=workspace.id, key=f"metric.{index:03d}",
                                      name=f"Metric {index}", collector_type="manual") for index in range(102)])
    db_session.add_all([ScheduledJob(entity_id=entity.id, workspace_id=workspace.id, owner_project_id=root.id,
                                    job_id=f"job-{index:03d}", name=f"Job {index}") for index in range(102)])
    await db_session.commit()
    sources = {
        "campaigns": {"type": "workflow_projects", "project_type": "campaign", "schema_versions": [1],
                      "producer_workflows": ["workflow_definition:campaign"], "launch_workflows": [], "fields": []},
        "metrics": {"type": "workspace_stats", "stat_keys": [f"metric.{index:03d}" for index in range(102)],
                    "producer_workflows": [], "fields": []},
        "jobs": {"type": "owned_automations", "parent": "campaigns", "producer_workflows": [], "fields": []},
    }
    context = {"kind": "project", "id": root.id}
    for source_name in ("metrics", "jobs"):
        first = await _source_rows(db_session, workspace, user, sources, source_name, context, {}, page_limit=101)
        page, cursor = _page(first, None, 100)
        second = await _source_rows(db_session, workspace, user, sources, source_name, context, {}, after_id=cursor, page_limit=101)
        assert len(page) == 100
        assert len(second) == 2
        assert {item["id"] for item in page}.isdisjoint({item["id"] for item in second})


@pytest.mark.asyncio
async def test_component_context_is_validated_before_fixed_metric_blocks(db_session):
    entity = Entity(name="App context guard entity")
    user = User(entity_id="pending", email="app-context-guard@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="App context guard", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    source = {"type": "workflow_projects", "project_type": "campaign", "schema_versions": [1],
              "producer_workflows": ["workflow_definition:campaign"], "launch_workflows": [], "fields": []}
    with pytest.raises(AppDataError, match="selected project"):
        await _validate_component_context(db_session, workspace, user, {"campaigns": source},
                                          {"context": {"kind": "project", "source": "campaigns"}},
                                          "project", "01M4INVALIDPROJECTID00000000")


@pytest.mark.asyncio
async def test_launch_projection_rejects_forged_or_non_create_project_output(db_session):
    entity = Entity(name="Launch projection entity")
    user = User(entity_id="pending", email="launch-projection@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Launch Workspace", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    project = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="client_delivery",
                              schema_version=1, project_key="real", state={}, created_by=user.id)
    db_session.add(project)
    await db_session.flush()
    run = WorkflowRun(entity_id=entity.id, workspace_id=workspace.id, workflow_id="flow-1", binding_id="binding-1",
                      status="completed", definition_snapshot={"steps": [{"id": "create", "type": "workflow_project", "config": {
                          "operation": "create", "project_type": "client_delivery", "schema_version": 1,
                      }}]}, step_results={"create": {"status": "completed", "output": {
                          "project_id": project.id, "project_type": "other_type", "schema_version": 1,
                      }}})
    db_session.add(run)
    await db_session.commit()
    source = {"type": "workflow_projects", "project_type": "client_delivery", "schema_versions": [1],
              "producer_workflows": ["workflow_definition:test"],
              "launch_workflows": [{"workflow": "workflow:binding-1", "project_steps": ["create"]}], "fields": []}
    assert await _verified_launch_projects(db_session, workspace, user, source, run.id) == []


@pytest.mark.asyncio
async def test_project_source_uses_database_cursor_beyond_first_hundred_rows(db_session):
    entity = Entity(name="Cursor entity")
    user = User(entity_id="pending", email="cursor-app-data@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Cursor Workspace", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    projects = [WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="client_delivery",
                                schema_version=1, project_key=f"delivery-{index:03d}", state={}, created_by=user.id)
                for index in range(102)]
    db_session.add_all(projects)
    await db_session.commit()
    source = {"project_type": "client_delivery", "schema_versions": [1]}
    first = await _projects(db_session, entity_id=entity.id, workspace_id=workspace.id, source=source, limit=101)
    second = await _projects(db_session, entity_id=entity.id, workspace_id=workspace.id, source=source,
                             after_id=first[-1].id, limit=101)
    assert len(first) == 101
    assert len(second) == 1
    assert second[0].id not in {row.id for row in first}
    newest = await _projects(db_session, entity_id=entity.id, workspace_id=workspace.id, source=source,
                             limit=2, descending=True)
    older = await _projects(db_session, entity_id=entity.id, workspace_id=workspace.id, source=source,
                            after_id=newest[-1].id, limit=2, descending=True)
    assert newest[0].id > newest[1].id
    assert older and older[0].id < newest[-1].id


@pytest.mark.asyncio
async def test_derived_record_source_cursor_reaches_second_child_page(db_session):
    entity = Entity(name="Derived cursor entity")
    user = User(entity_id="pending", email="derived-cursor@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Derived Cursor", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    root = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="campaign",
                           schema_version=1, project_key="root", state={}, created_by=user.id)
    db_session.add(root)
    await db_session.flush()
    db_session.add_all([WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="slot",
                                         schema_version=1, project_key=f"slot-{index:03d}",
                                         state={"parent_project_id": root.id, "receipt": {"schema_version": "receipt/v1", "published_url": f"https://example.com/{index}"}},
                                         created_by=user.id) for index in range(102)])
    await db_session.commit()
    sources = {
        "campaigns": {"type": "workflow_projects", "project_type": "campaign", "schema_versions": [1],
                      "producer_workflows": ["workflow_definition:campaign"], "launch_workflows": [], "fields": []},
        "slots": {"type": "related_projects", "project_type": "slot", "schema_versions": [1],
                  "producer_workflows": ["workflow_definition:slot"], "parent": "campaigns",
                  "parent_state_path": ["parent_project_id"], "fields": []},
        "receipts": {"type": "project_records", "parent": "slots", "record_path": ["receipt"],
                     "record_schema": "receipt/v1", "producer_workflows": [], "fields": []},
    }
    context = {"kind": "project", "id": root.id}
    first_rows = await _source_rows(db_session, workspace, user, sources, "receipts", context, {}, page_limit=101)
    first, cursor = _page(first_rows, None, 100)
    second_rows = await _source_rows(db_session, workspace, user, sources, "receipts", context, {}, after_id=cursor, page_limit=101)
    assert len(first) == 100
    assert cursor
    assert len(second_rows) == 2
    assert {row["id"] for row in second_rows}.isdisjoint({row["id"] for row in first})


@pytest.mark.asyncio
async def test_nested_related_source_cursor_reaches_children_of_later_parents(db_session):
    """A leaf cursor must not be applied as a cursor for the parent relation."""
    entity = Entity(name="Nested relation entity")
    user = User(entity_id="pending", email="nested-relation@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Nested relation", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    campaign = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="campaign",
                               schema_version=1, project_key="root", state={}, created_by=user.id)
    db_session.add(campaign)
    await db_session.flush()
    slots = [WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="slot",
                             schema_version=1, project_key=f"slot-{index:03d}",
                             state={"parent_project_id": campaign.id}, created_by=user.id)
             for index in range(102)]
    db_session.add_all(slots)
    await db_session.flush()
    db_session.add_all([WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="video",
                                         schema_version=1, project_key=f"video-{index:03d}",
                                         state={"parent_project_id": slot.id,
                                                "receipt": {"schema_version": "receipt/v1", "published_url": f"https://example.com/{index}"}},
                                         created_by=user.id)
                        for index, slot in enumerate(slots)])
    await db_session.commit()
    sources = {
        "campaigns": {"type": "workflow_projects", "project_type": "campaign", "schema_versions": [1],
                      "producer_workflows": ["workflow_definition:campaign"], "launch_workflows": [], "fields": []},
        "slots": {"type": "related_projects", "project_type": "slot", "schema_versions": [1],
                  "producer_workflows": ["workflow_definition:slot"], "parent": "campaigns",
                  "parent_state_path": ["parent_project_id"], "fields": []},
        "videos": {"type": "related_projects", "project_type": "video", "schema_versions": [1],
                   "producer_workflows": ["workflow_definition:video"], "parent": "slots",
                   "parent_state_path": ["parent_project_id"], "fields": []},
        "receipts": {"type": "project_records", "parent": "videos", "record_path": ["receipt"],
                     "record_schema": "receipt/v1", "producer_workflows": [], "fields": []},
    }
    context = {"kind": "project", "id": campaign.id}
    first_rows = await _source_rows(db_session, workspace, user, sources, "receipts", context, {}, page_limit=101)
    first, cursor = _page(first_rows, None, 100)
    second_rows = await _source_rows(db_session, workspace, user, sources, "receipts", context, {}, after_id=cursor, page_limit=101)
    assert len(first) == 100
    assert len(second_rows) == 2
    assert {row["id"] for row in second_rows}.isdisjoint({row["id"] for row in first})
    child_context = {"kind": "project", "id": slots[0].id, "source": "slots"}
    child_rows = await _source_rows(db_session, workspace, user, sources, "videos", child_context, {})
    assert len(child_rows) == 1
    with pytest.raises(AppDataError):
        await _source_rows(db_session, workspace, user, sources, "campaigns", child_context, {})


@pytest.mark.asyncio
async def test_project_summary_projects_only_declared_launch_context_and_safe_links(db_session, monkeypatch):
    entity = Entity(name="Product launch summary")
    user = User(entity_id="pending", email="launch-summary@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Launches", operating_model={})
    other = Workspace(entity_id=entity.id, name="Other launches", operating_model={})
    db_session.add_all([workspace, other])
    await db_session.flush()
    launch = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="product_launch", schema_version=1,
        project_key="spring", state={"launch": {"status": "planned", "date": "2026-11-03T09:00:00Z"},
                                      "portal_links": [{"label": "Customer portal", "url": "https://example.com/portal"},
                                                       {"label": "Unsafe", "url": "javascript:alert(1)"}],
                                      "tracking": {"ends_at": "2026-11-17T09:00:00Z"}}, created_by=user.id)
    foreign = WorkflowProject(entity_id=entity.id, workspace_id=other.id, project_type="product_launch", schema_version=1,
        project_key="foreign", state={"launch": {"status": "released"}}, created_by=user.id)
    db_session.add_all([launch, foreign])
    workspace.operating_model = {"app_view": {"data_sources": {
        "launches": {"type": "workflow_projects", "project_type": "product_launch", "schema_versions": [1],
                     "producer_workflows": ["workflow_definition:product_launch"], "launch_workflows": [],
                     "fields": [["launch", "status"], ["launch", "date"], ["portal_links"], ["tracking", "ends_at"]]},
    }, "components": [{"type": "data_view", "schema_version": 1, "key": "launch_context",
                          "context": {"kind": "project", "source": "launches"}, "blocks": [{
        "key": "summary", "type": "summary", "source": "launches", "title": "Launch context", "fields": [
            {"label": "Launch status", "field": ["launch", "status"], "format": "status", "labels": {"planned": "Planned"}},
            {"label": "Launch date", "field": ["launch", "date"], "format": "date", "hide_when_empty": True},
            {"label": "Customer portals", "field": ["portal_links"], "format": "link_list", "hide_when_empty": True},
            {"label": "Tracking through", "field": ["tracking", "ends_at"], "format": "date"},
        ],
    }]}]}}
    await db_session.commit()
    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"product_launch"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": [{
            "slug": "product_launch", "steps": [{"type": "workflow_project", "config": {"project_type": "product_launch", "state_schema": {"type": "object", "properties": {
                "launch": {"type": "object", "properties": {"status": {"type": "string"}, "date": {"type": "string"}}},
                "portal_links": {"type": "array"}, "tracking": {"type": "object", "properties": {"ends_at": {"type": "string"}}},
            }}}}],
        }]}
    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)

    result = await component_data(db_session, workspace, user=user, component_key="launch_context", context_kind="project", context_id=launch.id)
    fields = result["blocks"][0]["items"][0]["fields"]
    assert fields == [
        {"label": "Launch status", "format": "status", "value": "Planned"},
        {"label": "Launch date", "format": "date", "value": "2026-11-03T09:00:00Z"},
        {"label": "Customer portals", "format": "link_list", "links": [{"label": "Customer portal", "href": "https://example.com/portal"}], "value": None},
        {"label": "Tracking through", "format": "date", "value": "2026-11-17T09:00:00Z"},
    ]
    with pytest.raises(AppDataError, match="not available"):
        await component_data(db_session, workspace, user=user, component_key="launch_context", context_kind="project", context_id=foreign.id)

    launch.state["launch"]["date"] = None
    launch.state["portal_links"] = []
    await db_session.flush()
    fields = (await component_data(db_session, workspace, user=user, component_key="launch_context", context_kind="project", context_id=launch.id))["blocks"][0]["items"][0]["fields"]
    assert [field["label"] for field in fields] == ["Launch status", "Tracking through"]


def test_document_field_opens_only_a_bare_document_id_in_the_viewer():
    """A document field links to the viewer for a stored Document ID; anything else is hidden instead of linked."""
    configured = {"label": "Video", "field": ["production", "video_document_id"], "format": "document", "hide_when_empty": True}
    assert _present_configured_field(configured, "01M631K8Z23VBSP41MY78AJ6AW") == {
        "label": "Video", "format": "document", "value": None, "href": "/viewer/01M631K8Z23VBSP41MY78AJ6AW",
    }
    for value in (None, "", "../admin", "https://example.com/video.mp4", "01M631 K8", "x" * 65):
        assert _present_configured_field(configured, value) is None


@pytest.mark.asyncio
async def test_workspace_progress_lists_projects_with_labeled_fields(db_session, monkeypatch):
    """Each project row in a Workspace-level list shows labeled, formatted fields and skips empty ones."""
    entity = Entity(name="Workspace progress fields")
    user = User(entity_id="pending", email="workspace-progress@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Tracked work", operating_model={})
    db_session.add(workspace)
    await db_session.flush()

    def project(project_type, key, state):
        return WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type=project_type, schema_version=1,
                               project_key=key, current_stage="active", state=state, created_by=user.id)
    creators = [project("creator", "ada", {"name": "Ada", "profile_url": "https://example.com/ada", "priority": "high",
                                           "last_collected_at": "2026-10-20T09:00:00Z"}),
                project("creator", "bo", {"name": "Bo", "profile_url": "", "priority": "normal", "last_collected_at": None})]
    series = project("series", "pilot", {"title": "Pilot"})
    db_session.add_all([*creators, series])
    await db_session.flush()
    episodes = [project("episode", "ep-1", {"series_id": series.id, "title": "Ep 1", "air_date": "2026-11-03T09:00:00Z"}),
                project("episode", "ep-2", {"series_id": series.id, "title": "Ep 2"})]
    db_session.add_all(episodes)
    workspace.operating_model = {"app_view": {"data_sources": {
        "creators": {"type": "workflow_projects", "project_type": "creator", "label_fields": [["name"]],
                     "fields": [["name"], ["profile_url"], ["priority"], ["last_collected_at"]],
                     "producer_workflows": ["workflow_definition:creator"]},
        "series": {"type": "workflow_projects", "project_type": "series", "fields": [["title"]],
                   "producer_workflows": ["workflow_definition:series"]},
        "episodes": {"type": "related_projects", "project_type": "episode", "parent": "series", "parent_state_path": ["series_id"],
                     "label_fields": [["title"]], "fields": [["title"], ["air_date"]], "producer_workflows": ["workflow_definition:episode"]},
    }, "components": [
        {"type": "data_view", "key": "tracked", "context": {"kind": "workspace"}, "blocks": [{
            "key": "creators", "type": "progress", "source": "creators", "fields": [
                {"label": "Profile", "field": ["profile_url"], "format": "link", "hide_when_empty": True},
                {"label": "Priority", "field": ["priority"], "labels": {"high": "High", "normal": "Normal"}, "hide_when_empty": True},
                {"label": "Last collected", "field": ["last_collected_at"], "format": "date", "hide_when_empty": True},
            ]}]},
        {"type": "data_view", "key": "schedule", "context": {"kind": "workspace"}, "blocks": [{
            "key": "episodes", "type": "progress", "source": "episodes", "fields": [
                {"label": "Airs", "field": ["air_date"], "format": "date", "hide_when_empty": True},
            ]}]},
    ]}}
    await db_session.commit()

    async def catalog(*_args, **_kwargs):
        def flow(slug, project_type, properties):
            return {"slug": slug, "steps": [{"type": "workflow_project", "config": {"project_type": project_type, "state_schema": {
                "type": "object", "properties": {name: {"type": "string"} for name in properties}}}}]}
        return {"workflow_definitions": {"creator", "series", "episode"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": [
            flow("creator", "creator", ["name", "profile_url", "priority", "last_collected_at"]),
            flow("series", "series", ["title"]), flow("episode", "episode", ["series_id", "title", "air_date"]),
        ]}
    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)

    tracked = await component_data(db_session, workspace, user=user, component_key="tracked", context_kind="workspace", context_id=workspace.id)
    assert {item["label"]: item["fields"] for item in tracked["blocks"][0]["items"]} == {
        "Ada": [{"label": "Profile", "format": "link", "value": "https://example.com/ada", "href": "https://example.com/ada"},
                {"label": "Priority", "format": "text", "value": "High"},
                {"label": "Last collected", "format": "date", "value": "2026-10-20T09:00:00Z"}],
        "Bo": [{"label": "Priority", "format": "text", "value": "Normal"}],
    }
    schedule = await component_data(db_session, workspace, user=user, component_key="schedule", context_kind="workspace", context_id=workspace.id)
    assert {item["label"]: item["fields"] for item in schedule["blocks"][0]["items"]} == {
        "Ep 1": [{"label": "Airs", "format": "date", "value": "2026-11-03T09:00:00Z"}],
        "Ep 2": [],
    }


@pytest.mark.asyncio
async def test_a_project_shows_its_folder_tree_and_the_voices_its_children_inherit(db_session, monkeypatch):
    entity = Entity(name="Project tree entity")
    owner = User(entity_id="pending", email="project-tree-owner@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, owner])
    await db_session.flush()
    owner.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Podcast tree", operating_model={}, artifact_folder_id="folder1")
    db_session.add(workspace)
    await db_session.flush()
    voice = {"version": 1, "provider": "google", "model": "google/tts", "voice": "Puck", "voice_instructions": ""}
    series = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="series", schema_version=1,
                             created_by=owner.id, state={"title": "Ghost lantern", "project_root": "projects/ghost",
                                                         "references": {"voices": {"narrator": voice, "guest": {}},
                                                                        "images": {"series-key-art": {
                                                                            "version": 1, "kind": "image", "model": "img",
                                                                            "fs_path": "Workspaces/_by_id/folder1/projects/ghost/references/series-key-art.png"}}}})
    db_session.add(series)
    await db_session.flush()
    episode = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="episode", schema_version=1,
                              created_by=owner.id, state={"parent_project_id": series.id, "title": "E01",
                                                          "project_root": "projects/ghost/e01-lantern"})
    db_session.add(episode)
    home = "Workspaces/_by_id/folder1"
    for path in ("projects/ghost/references/series-key-art.png",
                 "projects/ghost/e01-lantern/v1/script.md", "projects/ghost/e01-lantern/v1/episode.mp3",
                 "projects/ghost-2/other.md", "workflow/run1/reviews/receipt.json"):
        db_session.add(Document(entity_id=entity.id, name=path.rsplit("/", 1)[1], file_type="md", source="generated",
                                visibility="workspace", owner_id=owner.id, fs_path=f"{home}/{path}"))
    workspace.operating_model = {"app_view": {"data_sources": {
        "parents": {"type": "workflow_projects", "project_type": "series", "producer_workflows": ["workflow_definition:make"],
                    "fields": [["title"]], "label_fields": [["title"]]},
        "children": {"type": "related_projects", "parent": "parents", "project_type": "episode",
                     "producer_workflows": ["workflow_definition:make"], "fields": [["title"]]},
        "files": {"type": "project_files", "parent": "parents"},
        "voices": {"type": "project_references", "parent": "parents"},
        "visuals": {"type": "project_references", "parent": "parents", "reference_kind": "images"},
        "episode_voices": {"type": "project_references", "parent": "children"},
    }, "components": [
        {"type": "data_view", "schema_version": 1, "key": "series", "context": {"kind": "project", "source": "parents"},
         "blocks": [{"key": "files", "type": "documents", "source": "files"},
                    {"key": "voices", "type": "references", "source": "voices"},
                    {"key": "visuals", "type": "references", "source": "visuals"}]},
        {"type": "data_view", "schema_version": 1, "key": "episode", "context": {"kind": "project", "source": "children"},
         "blocks": [{"key": "voices", "type": "references", "source": "episode_voices"}]},
    ]}}
    await db_session.commit()

    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"make"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": []}

    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)
    shown = await component_data(db_session, workspace, user=owner, component_key="series",
                                 context_kind="project", context_id=series.id)
    files, voices = shown["blocks"][0]["items"], shown["blocks"][1]["items"]
    # Only the project's own folder, its children's included, as the Workspace files show it.
    assert [(item["title"], item["folder"]) for item in files] == [
        ("episode.mp3", "e01-lantern/v1"), ("script.md", "e01-lantern/v1"), ("series-key-art.png", "references")]
    # A named image shows the readable Document it is.
    visual = shown["blocks"][2]["items"][0]
    assert visual["role"] == "series-key-art" and visual["media"][0]["title"] == "series-key-art.png"
    # A declared slot not pinned yet is not a voice.
    assert [(item["role"], item["reference"]["voice"], item["inherited"]) for item in voices] == [("narrator", "Puck", False)]

    inherited = await component_data(db_session, workspace, user=owner, component_key="episode",
                                     context_kind="project", context_id=episode.id)
    item = inherited["blocks"][0]["items"][0]
    assert item["inherited"] and item["holder_project_id"] == series.id and item["holder_label"] == "Ghost lantern"


@pytest.mark.asyncio
async def test_each_episode_shows_as_one_pack_of_its_results(db_session, monkeypatch):
    entity = Entity(name="Pack entity")
    owner = User(entity_id="pending", email="pack-owner@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, owner])
    await db_session.flush()
    owner.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Packs", operating_model={}, artifact_folder_id="folder2")
    db_session.add(workspace)
    await db_session.flush()
    series = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="series", schema_version=1,
                             created_by=owner.id, state={"title": "Ghost", "project_root": "projects/ghost"})
    db_session.add(series)
    await db_session.flush()
    episode = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="episode", schema_version=1,
                              created_by=owner.id, current_stage="made",
                              state={"parent_project_id": series.id, "code": "E01", "title": "Lantern",
                                     "project_root": "projects/ghost/e01-lantern"})
    db_session.add(episode)
    # An episode still being made has no file yet; its latest run stopped.
    stalled = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="episode", schema_version=1,
                              created_by=owner.id, current_stage="in_production",
                              state={"parent_project_id": series.id, "code": "E02", "title": "Tomb",
                                     "project_root": "projects/ghost/e02-tomb"})
    failed_run = WorkflowRun(entity_id=entity.id, workspace_id=workspace.id, workflow_id="make-flow",
                             binding_id="make-binding", status="failed", current_step_id="render")
    db_session.add_all([stalled, failed_run])
    await db_session.flush()
    stalled.last_run_id = failed_run.id
    # Retrying the stopped run is a new attempt in its lineage; the card follows it.
    retry = WorkflowRun(entity_id=entity.id, workspace_id=workspace.id, workflow_id="make-flow", binding_id="make-binding",
                        status="running", current_step_id="render", lineage_root_run_id=failed_run.id,
                        retry_of_run_id=failed_run.id, attempt_number=2)
    db_session.add(retry)
    home = "Workspaces/_by_id/folder2"
    for path, role in (("projects/ghost/e01-lantern/v1/cover.png", "final"),
                       ("projects/ghost/e01-lantern/v1/episode.mp3", "final"),
                       ("projects/ghost/e01-lantern/v2/brief.json", "final"),
                       ("projects/ghost/e01-lantern/v1/narration.mp3", "intermediate"),
                       ("projects/ghost/e01-lantern/v1/brief.json", "intermediate"),
                       ("projects/ghost/references/series-key-art.png", "final"),
                       # A production's working files sort before its results; they never fill the page.
                       *((f"projects/ghost/e01-lantern/frames/f-{index:02}.png", "intermediate") for index in range(6))):
        db_session.add(Document(entity_id=entity.id, name=path.rsplit("/", 1)[1], file_type="x", source="generated",
                                visibility="workspace", owner_id=owner.id, fs_path=f"{home}/{path}",
                                metadata_={"artifact": {"role": role, **({"delivered": True} if "/v1/" in path else {})}}))
    workspace.operating_model = {"app_view": {"data_sources": {
        "parents": {"type": "workflow_projects", "project_type": "series", "producer_workflows": ["workflow_definition:make"],
                    "fields": [["title"]]},
        "results": {"type": "project_files", "parent": "parents", "results_only": True},
    }, "components": [
        {"type": "data_view", "schema_version": 1, "key": "series", "context": {"kind": "project", "source": "parents"},
         "blocks": [{"key": "episodes", "type": "packs", "source": "results", "limit": 5}]},
    ]}}
    await db_session.commit()

    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"make"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": []}

    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)
    shown = await component_data(db_session, workspace, user=owner, component_key="series",
                                 context_kind="project", context_id=series.id)

    items = shown["blocks"][0]["items"]
    files = [item for item in items if item["document_id"]]
    # Only results, each in its episode's pack; the series' own references are in no pack.
    # A newer version still being made says so: only delivered files are results.
    assert {(item["title"], item["delivered"]) for item in files} == {
        ("cover.png", True), ("episode.mp3", True), ("brief.json", False)}
    assert {(item["pack"]["label"], item["pack"]["stage"]) for item in files} == {("E01 · Lantern", "made")}
    # Every episode is a card from its start, carrying how its latest run is going.
    cards = {item["pack"]["label"]: item["pack"] for item in items if not item["document_id"]}
    assert set(cards) == {"E01 · Lantern", "E02 · Tomb"}
    assert cards["E02 · Tomb"]["run"] == {"id": retry.id, "workflow_id": "make-flow", "binding_id": "make-binding",
                                          "status": "running", "needs_you": False}
    assert "run" not in cards["E01 · Lantern"]


@pytest.mark.asyncio
async def test_episodes_follow_the_plan_and_a_remade_one_shows_its_current_take(db_session, monkeypatch):
    entity = Entity(name="Plan pack entity")
    owner = User(entity_id="pending", email="plan-pack-owner@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, owner])
    await db_session.flush()
    owner.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Plan packs", operating_model={}, artifact_folder_id="folder-plan")
    db_session.add(workspace)
    await db_session.flush()
    series = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="series", schema_version=1,
                             created_by=owner.id, state={"title": "Ghost", "project_root": "projects/ghost"})
    db_session.add(series)
    await db_session.flush()
    take = "projects/ghost/e02-tomb/takes/run-2"
    remade = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="episode", schema_version=1,
                             created_by=owner.id, current_stage="made",
                             state={"parent_project_id": series.id, "code": "E02", "title": "Tomb",
                                    "project_root": "projects/ghost/e02-tomb", "production_root": take})
    first = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="episode", schema_version=1,
                            created_by=owner.id, current_stage="made",
                            state={"parent_project_id": series.id, "code": "E01", "title": "Lantern",
                                   "project_root": "projects/ghost/e01-lantern"})
    db_session.add_all([remade, first])
    await db_session.flush()
    series.state = {**series.state, "episodes": [
        {"code": "E01", "title": "Lantern", "synopsis": "A lantern.", "status": "made", "project_id": first.id},
        {"code": "E02", "title": "Tomb", "synopsis": "A tomb.", "status": "made", "project_id": remade.id},
        {"code": "E03", "title": "Bell", "synopsis": "A bell."}]}
    home = "Workspaces/_by_id/folder-plan"
    for path in ("projects/ghost/e01-lantern/final/episode.mp4", "projects/ghost/e01-lantern/takes/run-9/final/episode.mp4",
                 "projects/ghost/e02-tomb/final/episode.mp4", f"{take}/final/episode.mp4", f"{take}/thumbnail.png"):
        db_session.add(Document(entity_id=entity.id, name=path.rsplit("/", 1)[1], file_type="x", source="generated",
                                visibility="workspace", owner_id=owner.id, fs_path=f"{home}/{path}"))
    workspace.operating_model = {"app_view": {"data_sources": {
        "parents": {"type": "workflow_projects", "project_type": "series", "producer_workflows": ["workflow_definition:make"],
                    "fields": [["title"]]},
        "plan": {"type": "project_records", "parent": "parents", "record_path": ["episodes"]},
        "children": {"type": "related_projects", "parent": "parents", "project_type": "episode",
                     "producer_workflows": ["workflow_definition:make"], "fields": [["title"]]},
        "results": {"type": "project_files", "parent": "parents", "results_only": True},
    }, "components": [
        {"type": "data_view", "schema_version": 1, "key": "series", "context": {"kind": "project", "source": "parents"},
         "blocks": [{"key": "episodes", "type": "packs", "source": "results", "plan": "plan",
                     "item_action": {"action": "action:remake", "input": "episode_id", "label": "Remake"}},
                    {"key": "progress", "type": "project_rollup", "source": "children", "plan": "plan",
                     "count_by_stage": True, "completion": {"stages": ["made"], "label": "Ready"}}]},
    ]}}
    await db_session.commit()

    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"make"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": []}

    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)
    blocks = (await component_data(db_session, workspace, user=owner, component_key="series",
                                   context_kind="project", context_id=series.id))["blocks"]
    items = blocks[0]["items"]
    # Progress counts the whole plan: two of three are made.
    assert (blocks[1]["items"][0]["total"], blocks[1]["items"][0]["counts_by_stage"]) == (3, {"made": 2})
    cards = [item["pack"] for item in items if not item["document_id"]]
    # The plan's order; a planned episode is a card before it starts, carrying
    # what its own action opens with: its series and code.
    assert [(card["label"], card["stage"], card.get("synopsis")) for card in cards] == [
        ("E01 · Lantern", "made", "A lantern."), ("E02 · Tomb", "made", "A tomb."), ("E03 · Bell", "planned", "A bell.")]
    assert (cards[2]["parent_id"], cards[2]["code"]) == (series.id, "E03")
    # A remade episode shows its current take; an episode never remade shows no take.
    assert sorted((item["pack"]["code"], item["folder"], item["pack"]["version"]) for item in items if item["document_id"]) == [
        ("E01", "e01-lantern/final", "final"),
        ("E02", "e02-tomb/takes/run-2", ""), ("E02", "e02-tomb/takes/run-2/final", "final")]


@pytest.mark.asyncio
async def test_series_are_cards_showing_their_key_visual_and_how_many_episodes_are_done(db_session, monkeypatch):
    from packages.core.services.workspace_app_view import normalize_app_view_data_contract

    entity = Entity(name="Cards entity")
    owner = User(entity_id="pending", email="cards-owner@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, owner])
    await db_session.flush()
    owner.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Cards", operating_model={}, artifact_folder_id="folder3")
    db_session.add(workspace)
    await db_session.flush()
    art = "Workspaces/_by_id/folder3/projects/ghost/references/key.png"
    series = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="series", schema_version=1,
                             created_by=owner.id, state={"title": "Ghost", "project_root": "projects/ghost",
                                                         "references": {"images": {"key": {"version": 1, "fs_path": art}}}})
    db_session.add(series)
    await db_session.flush()
    for stage in ("made", "made", "in_production"):
        db_session.add(WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="episode",
                                       schema_version=1, created_by=owner.id, current_stage=stage,
                                       state={"parent_project_id": series.id}))
    db_session.add(Document(entity_id=entity.id, name="key.png", file_type="png", source="generated",
                            visibility="workspace", owner_id=owner.id, fs_path=art))
    sources = {
        "parents": {"type": "workflow_projects", "project_type": "series", "producer_workflows": ["workflow_definition:make"],
                    "fields": [["title"]], "label_fields": [["title"]]},
        "children": {"type": "related_projects", "parent": "parents", "project_type": "episode",
                     "producer_workflows": ["workflow_definition:make"], "fields": []},
        "visuals": {"type": "project_references", "parent": "parents", "reference_kind": "images"},
    }
    blocks = [{"key": "visuals", "type": "references", "source": "visuals"},
              {"key": "rollup", "type": "project_rollup", "source": "children", "count_by_stage": True,
               "completion": {"stages": ["made"], "label": "Made"}}]
    component = {"type": "data_view", "schema_version": 1, "key": "series", "blocks": blocks,
                 "context": {"kind": "project", "source": "parents", "display": "cards", "cover": "visuals", "progress": "rollup"}}
    workspace.operating_model = {"app_view": {"data_sources": sources, "components": [component]}}
    await db_session.commit()
    # A cover must be an image reference block; progress a rollup with completion.
    with pytest.raises(ValueError):
        normalize_app_view_data_contract(sources, [{**component, "context": {**component["context"], "cover": "rollup"}}])

    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"make"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": []}

    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)
    listed = await component_contexts(db_session, workspace, user=owner, component_key="series")

    card = listed["items"][0]
    assert card["label"] == "Ghost" and card["cover"]["title"] == "key.png"
    assert card["progress"] == {"done": 2, "total": 3, "label": "Made"}


SONG_STEPS = [
    {"id": "start", "type": "trigger", "config": {"run_inputs": [{"key": "style", "type": "string"}]}},
    {"id": "plan_song", "type": "llm", "config": {"output_var": "song_plan", "output_schema": {
        "type": "object", "properties": {"title": {"type": "string"}, "sections": {"type": "array"}}}}},
    {"id": "compose", "type": "agent", "config": {"output_var": "song", "output_schema": {
        "type": "object", "properties": {"document_id": {"type": "string"}, "summary": {"type": "string"}}}}},
]
SONG_VIEW = {"data_sources": {"songs": {
    "type": "workflow_results", "producer_workflows": ["workflow_definition:create-song"],
    "label_fields": [["song_plan", "title"]], "fields": [["song_plan", "title"], ["style"]],
    "media": [{"field": ["song", "document_id"]}]}},
    "components": [{"type": "data_view", "key": "songs", "title": "Songs", "context": {"kind": "workspace"}, "blocks": [
        {"key": "songs", "type": "gallery", "source": "songs",
         "fields": [{"label": "Style", "field": ["style"], "hide_when_empty": True}]}]}]}


@pytest.mark.asyncio
async def test_a_gallery_shows_the_latest_version_of_each_result_a_flow_made(db_session, monkeypatch):
    """One card per result: its newest version, a run still at work, never a failed attempt or a nested run."""
    entity = Entity(name="Song shelf")
    user = User(entity_id="pending", email="song-shelf@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Music", operating_model={"app_view": SONG_VIEW})
    db_session.add(workspace)
    await db_session.flush()

    def doc(name, **extra):
        return Document(entity_id=entity.id, name=name, mime_type="audio/mpeg", file_type="mp3",
                        visibility="workspace", owner_id=user.id, **extra)
    v1, v2, kept, gone = doc("hold-v1.mp3"), doc("hold-v2.mp3"), doc("rain.mp3"), doc("old.mp3", is_trashed=True)
    db_session.add_all([v1, v2, kept, gone])
    await db_session.flush()
    clock = datetime(2026, 10, 29, 9, tzinfo=UTC)

    def run(status, minutes, title=None, document=None, root=None, **extra):
        variables = {"style": "synth-pop", **({"song_plan": {"title": title}} if title else {}),
                     **({"song": {"document_id": document.id}} if document else {})}
        return WorkflowRun(entity_id=entity.id, workspace_id=workspace.id, workflow_id="create-song", status=status,
                           variables=variables, lineage_root_run_id=root, created_at=clock + timedelta(minutes=minutes),
                           **extra)
    hold = run("completed", 0, "Hold the Line", v1)
    db_session.add(hold)
    await db_session.flush()
    rain = run("completed", 2, "Rainy Platform", kept)
    db_session.add(rain)
    await db_session.flush()
    db_session.add_all([
        run("completed", 5, "Hold the Line", v2, root=hold.id),       # its second version
        run("failed", 6, "Rainy Platform", root=rain.id),            # a failed redo keeps the version before it
        run("running", 7, "Night Drive", definition_snapshot={"nodes": [  # still being made
            {"id": "start", "type": "trigger", "targets": ["plan_song"]},
            {"id": "plan_song", "type": "llm", "targets": ["compose"]}, {"id": "compose", "type": "agent"}]},
            current_step_id="compose", step_results={"plan_song": {"status": "completed"}}),
        run("failed", 8, "Never Made"),                              # failed with nothing made
        run("completed", 9, "Old", gone),                            # its only file was deleted
        run("completed", 10, "Inner", kept, trigger_data={"parent_run_id": hold.id}),  # a nested run
    ])
    await db_session.commit()

    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"create-song"}, "workflow_bindings": set(), "stat_keys": set(),
                "workflows": [{"slug": "create-song", "steps": SONG_STEPS}]}
    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)

    data = await component_data(db_session, workspace, user=user, component_key="songs",
                                context_kind="workspace", context_id=workspace.id)
    cards = [(item["title"], item["status"], [media["title"] for media in item["media"]]) for item in data["blocks"][0]["items"]]
    # Ordered by each result's latest activity.
    # "Old" finished but its only file was deleted: nothing to show, so no card.
    assert cards == [
        ("Night Drive", "running", []),
        ("Rainy Platform", "completed", ["rain.mp3"]),
        ("Hold the Line", "completed", ["hold-v2.mp3"]),
    ]
    assert data["blocks"][0]["items"][2]["fields"] == [{"label": "Style", "format": "text", "value": "synth-pop"}]
    # Only the result still being made says how far it is; a finished one has nothing to count.
    night, *finished = data["blocks"][0]["items"]
    assert 1 <= night["progress_percent"] <= 99
    assert all("progress_percent" not in item for item in finished)


@pytest.mark.parametrize("change, message", [
    ({"fields": [["song_plan", "title"], ["song", "lyrics"]]}, "does not produce"),  # not in compose's output schema
    ({"label_fields": [["mood"]], "fields": [["mood"]]}, "does not produce"),  # no step records it
    ({"producer_workflows": []}, "require producer_workflows"),
])
def test_a_results_view_reads_only_what_its_flow_produces(change, message):
    from packages.core.services.workspace_app_view import (
        normalize_app_view_data_contract, validate_app_view_data_contract,
    )

    sources = {"songs": {**SONG_VIEW["data_sources"]["songs"], **change}}
    with pytest.raises(ValueError, match=message):
        normalized, components = normalize_app_view_data_contract(sources, SONG_VIEW["components"])
        validate_app_view_data_contract(normalized, components, catalog={
            "workflow_definitions": {"create-song"}, "workflows": [{"slug": "create-song", "steps": SONG_STEPS}]})
    # A gallery reads results, and results are read by a gallery only.
    wrong = [{**SONG_VIEW["components"][0], "blocks": [{"key": "songs", "type": "records", "source": "songs"}]}]
    with pytest.raises(ValueError, match="gallery"):
        normalize_app_view_data_contract(SONG_VIEW["data_sources"], wrong)


def test_a_results_view_may_read_the_runs_final_result():
    """``__result`` (what the run ended with) is a readable root, unlike other underscore names."""
    from packages.core.services.workspace_app_view import (
        normalize_app_view_data_contract, validate_app_view_data_contract,
    )

    sources = {"videos": {"type": "workflow_results", "producer_workflows": ["workflow_definition:create-song"],
                          "fields": [["__result", "summary"]], "media": [{"field": ["__result", "state", "artifact"]}]}}
    blocks = [{"key": "videos", "type": "gallery", "source": "videos",
               "fields": [{"label": "Summary", "field": ["__result", "summary"]}]}]
    normalized, components = normalize_app_view_data_contract(
        sources, [{"type": "data_view", "key": "videos", "context": {"kind": "workspace"}, "blocks": blocks}])
    validate_app_view_data_contract(normalized, components, catalog={
        "workflow_definitions": {"create-song"}, "workflows": [{"slug": "create-song", "steps": SONG_STEPS}]})
    with pytest.raises(ValueError):
        normalize_app_view_data_contract({"videos": {**sources["videos"], "fields": [["_launch_key"]]}}, [])


@pytest.mark.asyncio
async def test_a_project_being_prepared_shows_until_its_run_holds_the_project(db_session, monkeypatch):
    """Before a Flow's create step there is no project; the run stands in for it, with its step and progress."""
    entity = Entity(name="Preparing entity")
    user = User(entity_id="pending", email="preparing@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Preparing", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    clock = datetime.now(UTC)
    nodes = [{"id": "start", "type": "trigger", "name": "Start", "targets": ["plan"]},
             {"id": "plan", "type": "agent", "name": "Write the campaign plan", "targets": ["create"]},
             {"id": "create", "type": "workflow_project", "name": "Create campaign project"}]

    def run(status, minutes, **extra):
        return WorkflowRun(entity_id=entity.id, workspace_id=workspace.id, workflow_id="campaign-flow", status=status,
                           created_at=clock + timedelta(minutes=minutes), **extra)
    preparing = run("running", 3, current_step_id="plan", definition_snapshot={"nodes": nodes},
                    step_results={"start": {"status": "completed"}})
    holding = run("paused", 2, current_step_id="create", variables={})
    done = run("completed", 1)
    nested = run("running", 4, trigger_data={"parent_run_id": "outer"})
    db_session.add_all([preparing, holding, done, nested])
    await db_session.flush()
    made = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="campaign", schema_version=1,
                           project_key="made", state={}, created_by=user.id, created_at=clock + timedelta(minutes=2, seconds=30))
    db_session.add(made)
    await db_session.flush()
    holding.variables = {"campaign": {"project_id": made.id}}  # its create step has run
    workspace.operating_model = {"app_view": {"data_sources": {"campaigns": {
        "type": "workflow_projects", "project_type": "campaign", "schema_versions": [1],
        "producer_workflows": ["workflow_definition:campaign-flow"], "launch_workflows": [], "fields": []}},
        "components": [{"type": "data_view", "schema_version": 1, "key": "campaigns",
                        "context": {"kind": "project", "source": "campaigns"},
                        "blocks": [{"key": "progress", "type": "progress", "source": "campaigns", "limit": 10}]}]}}
    await db_session.commit()

    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"campaign-flow"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": []}
    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)

    contexts = await component_contexts(db_session, workspace, user=user, component_key="campaigns")
    # The finished run, the one already holding its project, and a nested run are not being prepared.
    (being_made,) = contexts["making"]
    assert (being_made["run_id"], being_made["step"], being_made["status"]) == (preparing.id, "Write the campaign plan", "running")
    assert 1 <= being_made["progress_percent"] <= 99
    assert [item["id"] for item in contexts["items"]] == [made.id]


@pytest.mark.asyncio
async def test_course_card_and_open_course_count_reminders_by_their_runs_alike(db_session, monkeypatch):
    """A reminder whose run waits on a person reads the same on the course card and inside the course."""
    entity = Entity(name="Reminder execution counts")
    user = User(entity_id="pending", email="reminder-execution-counts@test.com", password_hash="x", role="owner")
    db_session.add_all([entity, user])
    await db_session.flush()
    user.entity_id = entity.id
    workspace = Workspace(entity_id=entity.id, name="Courses", operating_model={})
    db_session.add(workspace)
    await db_session.flush()
    course = WorkflowProject(entity_id=entity.id, workspace_id=workspace.id, project_type="course", schema_version=1,
                             project_key="course", current_stage="active", created_by=user.id,
                             state={"title": "Spring course", "starts_at": "2026-11-01T09:00:00+00:00"})
    db_session.add(course)
    await db_session.flush()

    def run(status: str) -> WorkflowRun:
        return WorkflowRun(entity_id=entity.id, workspace_id=workspace.id, workflow_id="reminder-flow",
                           status=status, current_step_id="step")

    waiting, working, failed, later_check = run("paused"), run("running"), run("failed"), run("failed")
    db_session.add_all([waiting, working, failed, later_check])
    await db_session.flush()

    def reminder(key: str, stage: str, last_run: WorkflowRun | None, day: int) -> WorkflowProject:
        return WorkflowProject(
            entity_id=entity.id, workspace_id=workspace.id, project_type="course_reminder", schema_version=1,
            project_key=key, current_stage=stage, created_by=user.id, last_run_id=last_run.id if last_run else None,
            state={"course_id": course.id, "schedule": {"due_at": f"2026-11-0{day}T09:00:00+00:00"}, "title": key},
        )

    db_session.add_all([
        reminder("waits-for-teacher", "leased", waiting, 2),
        reminder("being-written", "leased", working, 3),
        reminder("last-attempt-failed", "scheduled", failed, 4),
        # A sent reminder stays sent even when a later check of it failed.
        reminder("sent", "published", later_check, 5),
    ])
    workspace.operating_model = {"app_view": {"data_sources": {
        "courses": {"type": "workflow_projects", "project_type": "course", "schema_versions": [1],
                    "producer_workflows": ["workflow_definition:course"], "launch_workflows": [],
                    "fields": [["title"], ["starts_at"]], "label_fields": [["title"]]},
        "reminders": {"type": "related_projects", "project_type": "course_reminder", "schema_versions": [1],
                      "producer_workflows": ["workflow_definition:reminder"], "parent": "courses",
                      "parent_state_path": ["course_id"], "fields": [["schedule", "due_at"], ["title"]],
                      "label_fields": [["title"]],
                      "order": {"field": ["schedule", "due_at"], "type": "datetime", "direction": "asc"}},
    }, "components": [{"type": "data_view", "schema_version": 1, "key": "course_calendar",
        "context": {"kind": "project", "source": "courses"}, "blocks": [
            {"key": "calendar", "type": "progress", "source": "reminders", "limit": 100, "fields": [
                {"label": "Due", "field": ["schedule", "due_at"], "format": "date", "hide_when_empty": False},
            ]},
            {"key": "rollup", "type": "project_rollup", "source": "reminders", "count_by_stage": True,
             "completion": {"stages": ["published"], "label": "Sent"}},
            {"key": "context", "type": "summary", "source": "courses", "fields": [
                {"label": "Starts", "field": ["starts_at"], "format": "date", "hide_when_empty": False},
            ]},
        ], "display": {"layout": "stack", "groups": [{"key": "all", "title": "All", "blocks": ["calendar", "rollup", "context"]}],
            "hidden": False, "hidden_blocks": [], "calendar": {
                "block": "calendar", "date_field": ["schedule", "due_at"], "rollup_block": "rollup", "context_block": "context",
                "attention_stages": ["blocked"], "collection_title": "Courses",
            }}}]}}
    await db_session.commit()

    async def catalog(*_args, **_kwargs):
        return {"workflow_definitions": {"course", "reminder"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": [
            {"slug": "course", "steps": [{"config": {"project_type": "course", "state_schema": {"properties": {
                "title": {"type": "string"}, "starts_at": {"type": "string"},
            }}}}]},
            {"slug": "reminder", "steps": [{"config": {"project_type": "course_reminder", "state_schema": {"properties": {
                "course_id": {"type": "string"}, "schedule": {"type": "object", "properties": {"due_at": {"type": "string"}}},
                "title": {"type": "string"},
            }}}}]},
        ]}
    monkeypatch.setattr("packages.core.services.workspace_app_data.load_workspace_app_data_catalog", catalog)

    expected = {"paused": 1, "leased": 1, "failed": 1, "published": 1}
    contexts = await component_contexts(db_session, workspace, user=user, component_key="course_calendar")
    card = contexts["items"][0]["summary"]
    assert (card["total"], card["counts_by_stage"], card["counts_include_execution"]) == (4, expected, True)

    inside = await component_data(db_session, workspace, user=user, component_key="course_calendar",
                                  context_kind="project", context_id=course.id, block_key="rollup")
    rollup = inside["blocks"][0]["items"][0]
    assert (rollup["total"], rollup["counts_by_stage"], rollup["counts_include_execution"]) == (4, expected, True)
