"""Regression coverage for one-call structured Runtime query contracts."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.user import User, UserMembership
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun
from packages.core.models.workspace import Agent, AgentSubscription, WorkspaceStaff


class _CountingSession:
    def __init__(self, session) -> None:
        self._session = session
        self.execute_count = 0

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def execute(self, statement, *args, **kwargs):
        self.execute_count += 1
        return await self._session.execute(statement, *args, **kwargs)


@pytest.mark.asyncio
async def test_scheduled_job_query_combines_plural_and_range_filters_before_pagination(
    db_session,
    monkeypatch,
) -> None:
    from packages.core.ai.runtime.manor_actions import (
        runtime_manor_list_scheduled_jobs,
    )
    from packages.core.services.scheduler_service import list_scheduled_jobs

    entity_id = generate_ulid()
    other_entity_id = generate_ulid()
    workspace_a = generate_ulid()
    workspace_b = generate_ulid()
    agent_a = generate_ulid()
    agent_b = generate_ulid()

    def job(
        *,
        job_id: str,
        entity: str = entity_id,
        workspace_id: str,
        agent_id: str,
        schedule_kind: str,
        enabled: bool,
        errors: int,
        created_day: int,
    ) -> ScheduledJob:
        return ScheduledJob(
            id=generate_ulid(),
            job_id=job_id,
            entity_id=entity,
            workspace_id=workspace_id,
            agent_id=agent_id,
            name=f"Quarterly query {job_id}",
            job_type="cron",
            schedule_kind=schedule_kind,
            enabled=enabled,
            last_status="error",
            consecutive_errors=errors,
            created_at=datetime(2026, 8, created_day, tzinfo=timezone.utc),
            updated_at=datetime(2026, 8, created_day, 12, tzinfo=timezone.utc),
            last_run_at=datetime(2026, 8, created_day, 13, tzinfo=timezone.utc),
            next_run_at=datetime(2026, 9, created_day, tzinfo=timezone.utc),
        )

    db_session.add_all([
        job(
            job_id="query-paused",
            workspace_id=workspace_a,
            agent_id=agent_a,
            schedule_kind="every",
            enabled=False,
            errors=2,
            created_day=2,
        ),
        job(
            job_id="query-attention",
            workspace_id=workspace_b,
            agent_id=agent_b,
            schedule_kind="once",
            enabled=True,
            errors=3,
            created_day=3,
        ),
        job(
            job_id="query-outside-range",
            workspace_id=workspace_a,
            agent_id=agent_a,
            schedule_kind="cron",
            enabled=True,
            errors=0,
            created_day=9,
        ),
        job(
            job_id="query-other-entity",
            entity=other_entity_id,
            workspace_id=workspace_a,
            agent_id=agent_a,
            schedule_kind="every",
            enabled=False,
            errors=2,
            created_day=2,
        ),
    ])
    await db_session.flush()

    rows, total = await list_scheduled_jobs(
        db_session,
        entity_id,
        search="quarterly query",
        statuses=["paused", "attention"],
        workspace_ids=[workspace_a, workspace_b],
        agent_ids=[agent_a, agent_b],
        schedule_kinds=["every", "once"],
        last_statuses=["error"],
        consecutive_errors_min=1,
        consecutive_errors_max=3,
        created_after="2026-08-01T00:00:00Z",
        created_before="2026-08-04T00:00:00Z",
        last_run_after="2026-08-02T00:00:00Z",
        last_run_before="2026-08-03T23:59:59Z",
        limit=1,
        offset=1,
    )

    assert total == 2
    assert [row.job_id for row in rows] == ["query-paused"]

    counted_session = _CountingSession(db_session)

    async def readable_workspaces(*_args, **_kwargs):
        return {workspace_a, workspace_b}

    monkeypatch.setattr(
        "packages.core.services.workspace_access.readable_workspace_ids_for_user",
        readable_workspaces,
    )

    payload = json.loads(await runtime_manor_list_scheduled_jobs(
        counted_session,
        entity_id=entity_id,
        user_id=generate_ulid(),
        params={
            "query": "quarterly query",
            "statuses": ["paused", "attention"],
            "workspace_ids": [workspace_a, workspace_b],
            "agent_ids": [agent_a, agent_b],
            "schedule_kinds": ["every", "once"],
            "last_statuses": ["error"],
            "consecutive_errors_min": 1,
            "consecutive_errors_max": 3,
            "created_after": "2026-08-01T00:00:00Z",
            "created_before": "2026-08-04T00:00:00Z",
            "limit": 1,
            "offset": 1,
        },
    ))
    assert payload["ok"] is True
    assert payload["total"] == 2
    assert payload["count"] == 1
    assert payload["automations"][0]["job_id"] == "query-paused"
    assert payload["has_more"] is False
    assert counted_session.execute_count == 2

    with pytest.raises(ValueError, match="created_after"):
        await list_scheduled_jobs(
            db_session,
            entity_id,
            created_after="2026-09-01T00:00:00Z",
            created_before="2026-08-01T00:00:00Z",
        )


@pytest.mark.asyncio
async def test_agent_query_combines_filters_and_returns_exact_pagination(
    db_session,
) -> None:
    from packages.core.ai.runtime.agent_provisioning import (
        runtime_query_entity_agents_action,
    )

    entity_id = generate_ulid()
    other_entity_id = generate_ulid()
    workspace_a = generate_ulid()
    workspace_b = generate_ulid()
    owner_a = generate_ulid()
    owner_b = generate_ulid()

    def agent(
        *,
        name: str,
        entity: str = entity_id,
        workspace_id: str,
        owner_id: str,
        category: str,
        status: str,
        created_day: int,
    ) -> Agent:
        return Agent(
            id=generate_ulid(),
            entity_id=entity,
            workspace_id=workspace_id,
            owner_user_id=owner_id,
            name=name,
            description="Structured query Agent",
            category=category,
            source="custom",
            status=status,
            is_template=False,
            is_public=False,
            created_at=datetime(2026, 8, created_day, tzinfo=timezone.utc),
            updated_at=datetime(2026, 8, created_day, 12, tzinfo=timezone.utc),
        )

    agent_a = agent(
        name="Agent Alpha",
        workspace_id=workspace_a,
        owner_id=owner_a,
        category="Ops",
        status="active",
        created_day=3,
    )
    agent_b = agent(
        name="Agent Beta",
        workspace_id=workspace_b,
        owner_id=owner_b,
        category="Growth",
        status="paused",
        created_day=2,
    )
    outside = agent(
        name="Agent Outside",
        workspace_id=workspace_a,
        owner_id=owner_a,
        category="Ops",
        status="active",
        created_day=9,
    )
    foreign = agent(
        name="Agent Foreign",
        entity=other_entity_id,
        workspace_id=workspace_a,
        owner_id=owner_a,
        category="Ops",
        status="active",
        created_day=2,
    )
    db_session.add_all([agent_a, agent_b, outside, foreign])
    db_session.add_all([
        AgentSubscription(
            id=generate_ulid(),
            entity_id=entity_id,
            agent_id=agent_a.id,
            workspace_id=workspace_a,
            status="active",
        ),
        AgentSubscription(
            id=generate_ulid(),
            entity_id=entity_id,
            agent_id=agent_b.id,
            workspace_id=workspace_b,
            status="active",
        ),
        AgentSubscription(
            id=generate_ulid(),
            entity_id=entity_id,
            agent_id=agent_b.id,
            workspace_id=workspace_a,
            status="active",
        ),
    ])
    await db_session.flush()

    counted_session = _CountingSession(db_session)

    payload = json.loads(await runtime_query_entity_agents_action(
        entity_id=entity_id,
        query="structured query",
        statuses=["active", "paused"],
        categories=["ops", "growth"],
        sources=["custom"],
        workspace_ids=[workspace_a, workspace_b],
        owner_user_ids=[owner_a, owner_b],
        created_after="2026-08-01T00:00:00Z",
        created_before="2026-08-04T00:00:00Z",
        deployment_count_min=1,
        deployment_count_max=2,
        readable_workspace_ids={workspace_a, workspace_b},
        limit=1,
        offset=1,
        db=counted_session,
    ))

    assert payload["ok"] is True
    assert payload["total"] == 2
    assert payload["count"] == 1
    assert payload["offset"] == 1
    assert payload["has_more"] is False
    assert payload["agents"][0]["id"] == agent_b.id
    assert payload["agents"][0]["deployment_count"] == 2
    assert counted_session.execute_count == 2

    scoped = json.loads(await runtime_query_entity_agents_action(
        entity_id=entity_id,
        require_schedulable_scope=True,
        schedulable_workspace_id=workspace_a,
        db=db_session,
    ))
    assert [row["id"] for row in scoped["agents"]] == [agent_a.id]
    assert scoped["agents"][0]["deployment_count"] == 1

    invalid = json.loads(await runtime_query_entity_agents_action(
        entity_id=entity_id,
        created_after="2026-09-01T00:00:00Z",
        created_before="2026-08-01T00:00:00Z",
    ))
    assert invalid["error"] == "invalid_agent_filters"


@pytest.mark.asyncio
async def test_query_actions_reject_invalid_direct_inputs_before_database_access() -> None:
    from packages.core.ai.runtime.agent_provisioning import (
        runtime_query_entity_agents_action,
    )
    from packages.core.ai.runtime.scheduling import (
        runtime_create_scheduled_job_action,
        runtime_query_scheduled_jobs_action,
    )

    agent_error = json.loads(await runtime_query_entity_agents_action(
        entity_id=generate_ulid(),
        statuses="active",  # type: ignore[arg-type]
    ))
    automation_error = json.loads(await runtime_query_scheduled_jobs_action(
        entity_id=generate_ulid(),
        runtime_workspace_id=generate_ulid(),
        limit="not-an-int",  # type: ignore[arg-type]
    ))
    automation_plural_error = json.loads(await runtime_query_scheduled_jobs_action(
        entity_id=generate_ulid(),
        runtime_workspace_id=generate_ulid(),
        statuses="enabled",  # type: ignore[arg-type]
    ))
    automation_kind_error = json.loads(await runtime_query_scheduled_jobs_action(
        entity_id=generate_ulid(),
        runtime_workspace_id=generate_ulid(),
        schedule_kinds=["sometimes"],
    ))
    automation_create_error = json.loads(await runtime_create_scheduled_job_action(
        entity_id=generate_ulid(),
        name="invalid",
        schedule_kind="once",
        payload_message="should not reach the database",
    ))
    automation_missing_cron_error = json.loads(
        await runtime_create_scheduled_job_action(
            entity_id=generate_ulid(),
            name="invalid cron",
            schedule_kind="cron",
            payload_message="should not reach the database",
        )
    )
    automation_interval_error = json.loads(
        await runtime_create_scheduled_job_action(
            entity_id=generate_ulid(),
            name="invalid interval",
            schedule_kind="every",
            every_seconds=0,
            payload_message="should not reach the database",
        )
    )
    automation_run_at_error = json.loads(
        await runtime_create_scheduled_job_action(
            entity_id=generate_ulid(),
            name="invalid run at",
            schedule_kind="at",
            run_at="not-an-iso-datetime",
            payload_message="should not reach the database",
        )
    )

    assert agent_error["error"] == "invalid_agent_filters"
    assert automation_error["error"] == "invalid_automation_filters"
    assert automation_plural_error["error"] == "invalid_automation_filters"
    assert automation_kind_error["error"] == "invalid_automation_filters"
    assert automation_create_error["error"] == "invalid_automation_schedule"
    assert automation_missing_cron_error["error"] == "invalid_automation_schedule"
    assert automation_interval_error["error"] == "invalid_automation_schedule"
    assert automation_run_at_error["error"] == "invalid_automation_schedule"


@pytest.mark.asyncio
async def test_runtime_automation_agent_selection_validates_scope_and_falls_back(
) -> None:
    from packages.core.ai.runtime.scheduling import (
        _resolve_runtime_scheduled_job_agent_id,
    )

    class QueryResult:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return self

        def all(self):
            return self.values

    class FakeDb:
        def __init__(self, values):
            self.values = list(values)

        async def execute(self, _statement):
            return QueryResult(self.values.pop(0))

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    caller_agent_id = generate_ulid()
    selected_agent_id = generate_ulid()
    caller_subscription_id = generate_ulid()
    selected_subscription_id = generate_ulid()
    caller_subscription = SimpleNamespace(
        id=caller_subscription_id,
        agent_id=caller_agent_id,
    )
    selected_subscription = SimpleNamespace(
        id=selected_subscription_id,
        agent_id=selected_agent_id,
    )
    other_caller_subscription = SimpleNamespace(
        id=generate_ulid(),
        agent_id=caller_agent_id,
    )
    db = FakeDb([
        [caller_agent_id, selected_agent_id],
        [caller_subscription, selected_subscription],
    ])

    selected = await _resolve_runtime_scheduled_job_agent_id(
        db,
        entity_id=entity_id,
        requested_agent_id=selected_agent_id,
        caller_agent_id=caller_agent_id,
        caller_agent_subscription_id=caller_subscription_id,
        workspace_id=workspace_id,
        user_id=generate_ulid(),
    )
    assert selected.agent_id == selected_agent_id
    assert selected.agent_subscription_id == selected_subscription_id
    assert selected.source == "requested"

    db = FakeDb([
        [caller_agent_id],
        [caller_subscription, other_caller_subscription],
    ])
    omitted_custom = await _resolve_runtime_scheduled_job_agent_id(
        db,
        entity_id=entity_id,
        caller_agent_id=caller_agent_id,
        caller_agent_subscription_id=caller_subscription_id,
        workspace_id=workspace_id,
    )
    assert omitted_custom.agent_id == caller_agent_id
    assert omitted_custom.agent_subscription_id == caller_subscription_id
    assert omitted_custom.source == "caller"

    db = FakeDb([[caller_agent_id], [caller_subscription]])
    fallback = await _resolve_runtime_scheduled_job_agent_id(
        db,
        entity_id=entity_id,
        requested_agent_id=generate_ulid(),
        caller_agent_id=caller_agent_id,
        caller_agent_subscription_id=caller_subscription_id,
        workspace_id=workspace_id,
        user_id=generate_ulid(),
    )
    assert fallback.agent_id == caller_agent_id
    assert fallback.agent_subscription_id == caller_subscription_id
    assert fallback.source == "caller_fallback"

    db = FakeDb([
        [caller_agent_id, selected_agent_id],
        [
            caller_subscription,
            selected_subscription,
            SimpleNamespace(id=generate_ulid(), agent_id=selected_agent_id),
        ],
    ])
    ambiguous = await _resolve_runtime_scheduled_job_agent_id(
        db,
        entity_id=entity_id,
        requested_agent_id=selected_agent_id,
        caller_agent_id=caller_agent_id,
        caller_agent_subscription_id=caller_subscription_id,
        workspace_id=workspace_id,
    )
    assert ambiguous.agent_id == caller_agent_id
    assert ambiguous.agent_subscription_id == caller_subscription_id
    assert ambiguous.source == "caller_fallback"

    db = FakeDb([[caller_agent_id], [caller_subscription]])
    denied_master = await _resolve_runtime_scheduled_job_agent_id(
        db,
        entity_id=entity_id,
        requested_agent_id="master",
        caller_agent_id=caller_agent_id,
        caller_agent_subscription_id=caller_subscription_id,
        workspace_id=workspace_id,
    )
    assert denied_master.agent_id == caller_agent_id
    assert denied_master.agent_subscription_id == caller_subscription_id
    assert denied_master.source == "caller_fallback"

    db = FakeDb([])
    omitted = await _resolve_runtime_scheduled_job_agent_id(
        db,
        entity_id=entity_id,
        caller_agent_id="",
        workspace_id=workspace_id,
    )
    assert omitted.agent_id == "manor-master"
    assert omitted.agent_subscription_id is None
    assert omitted.source == "caller"

    db = FakeDb([])
    master = await _resolve_runtime_scheduled_job_agent_id(
        db,
        entity_id=entity_id,
        requested_agent_id="master",
        caller_agent_id="manor-master",
        workspace_id=workspace_id,
    )
    assert master.agent_id == "manor-master"
    assert master.agent_subscription_id is None
    assert master.source == "requested"
    assert db.values == []


@pytest.mark.asyncio
async def test_runtime_automation_agent_selection_uses_active_scoped_deployment(
    db_session,
) -> None:
    from packages.core.ai.runtime.agent_provisioning import (
        runtime_query_entity_agents_action,
    )
    from packages.core.ai.runtime.scheduling import (
        _resolve_runtime_scheduled_job_agent_id,
    )

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    caller_agent_id = generate_ulid()
    caller_subscription_id = generate_ulid()
    selected_agent_id = generate_ulid()
    wrong_scope_agent_id = generate_ulid()
    unowned_agent_id = generate_ulid()
    unowned_subscription_id = generate_ulid()
    db_session.add_all([
        Agent(
            id=caller_agent_id,
            entity_id=entity_id,
            name="Calling Agent",
            status="active",
        ),
        Agent(
            id=selected_agent_id,
            entity_id=entity_id,
            name="Selected Agent",
            status="active",
        ),
        Agent(
            id=wrong_scope_agent_id,
            entity_id=entity_id,
            name="Other Workspace Agent",
            status="active",
        ),
        Agent(
            id=unowned_agent_id,
            entity_id=entity_id,
            name="Unowned Agent",
            status="active",
        ),
        AgentSubscription(
            id=caller_subscription_id,
            entity_id=entity_id,
            agent_id=caller_agent_id,
            workspace_id=workspace_id,
            status="active",
        ),
        AgentSubscription(
            id=generate_ulid(),
            entity_id=entity_id,
            agent_id=selected_agent_id,
            workspace_id=workspace_id,
            status="active",
        ),
        AgentSubscription(
            id=unowned_subscription_id,
            entity_id=entity_id,
            agent_id=unowned_agent_id,
            workspace_id=None,
            owner_user_id=None,
            status="active",
        ),
        AgentSubscription(
            id=generate_ulid(),
            entity_id=entity_id,
            agent_id=wrong_scope_agent_id,
            workspace_id=generate_ulid(),
            status="active",
        ),
    ])
    await db_session.flush()

    selected = await _resolve_runtime_scheduled_job_agent_id(
        db_session,
        entity_id=entity_id,
        requested_agent_id=selected_agent_id,
        caller_agent_id=caller_agent_id,
        caller_agent_subscription_id=caller_subscription_id,
        workspace_id=workspace_id,
    )
    wrong_scope = await _resolve_runtime_scheduled_job_agent_id(
        db_session,
        entity_id=entity_id,
        requested_agent_id=wrong_scope_agent_id,
        caller_agent_id=caller_agent_id,
        caller_agent_subscription_id=caller_subscription_id,
        workspace_id=workspace_id,
    )
    unowned = await _resolve_runtime_scheduled_job_agent_id(
        db_session,
        entity_id=entity_id,
        requested_agent_id=unowned_agent_id,
        caller_agent_id="manor-master",
        workspace_id=None,
        user_id=None,
    )
    missing_scope = json.loads(await runtime_query_entity_agents_action(
        entity_id=entity_id,
        require_schedulable_scope=True,
        db=db_session,
    ))

    assert selected.agent_id == selected_agent_id
    assert selected.agent_subscription_id is not None
    assert selected.source == "requested"
    assert wrong_scope.agent_id == caller_agent_id
    assert wrong_scope.agent_subscription_id == caller_subscription_id
    assert wrong_scope.source == "caller_fallback"
    assert unowned.agent_id == "manor-master"
    assert unowned.agent_subscription_id is None
    assert unowned.source == "caller_fallback"
    assert missing_scope["agents"] == []
    assert missing_scope["total"] == 0


def test_scheduled_agent_subscription_identity_is_not_blueprint_portable() -> None:
    from packages.core.blueprints.exporter import _without_nonportable_references

    assert _without_nonportable_references({
        "deliverable": "report",
        "agent_subscription_id": generate_ulid(),
    }) == {"deliverable": "report"}


def test_structured_query_schemas_expose_one_call_filter_contracts() -> None:
    from packages.core.ai.tools.agent_provisioning_tools import (
        QUERY_ENTITY_AGENTS_SCHEMA,
    )
    from packages.core.ai.tools.cron_tools import get_tools as get_cron_tools
    from packages.core.ai.tools.document_tools import (
        LIST_DOCUMENTS_SCHEMA,
        SEARCH_DOCUMENTS_SCHEMA,
    )
    from packages.core.ai.tools.ledger_query_tools import QUERY_LEDGER_SCHEMA
    from packages.core.ai.tools.notification_tools import FIND_TEAM_MEMBERS_SCHEMA
    from packages.core.ai.tools.task_tools import SEARCH_TASKS_SCHEMA
    from packages.core.ai.tools.workflow_tools import (
        LIST_DEFINITIONS_SCHEMA,
        LIST_RUNS_SCHEMA,
    )

    query_jobs = next(
        schema
        for schema, _handler in get_cron_tools()
        if schema["function"]["name"] == "query_scheduled_jobs"
    )
    create_job = next(
        schema
        for schema, _handler in get_cron_tools()
        if schema["function"]["name"] == "create_scheduled_job"
    )
    create_kinds = set(
        create_job["function"]["parameters"]["properties"]["schedule_kind"]["enum"]
    )
    schedule_kind_schema = query_jobs["function"]["parameters"]["properties"][
        "schedule_kinds"
    ]
    query_kinds = set(schedule_kind_schema["items"]["enum"])
    assert create_kinds == {"cron", "every", "at"}
    assert create_kinds <= query_kinds
    assert {"interval", "once"} <= query_kinds
    expected = {
        "search_tasks": {"statuses", "priority_min", "created_after", "offset"},
        "query_scheduled_jobs": {
            "statuses",
            "workspace_ids",
            "consecutive_errors_min",
            "last_run_after",
            "offset",
        },
        "query_entity_agents": {
            "statuses",
            "categories",
            "deployment_count_min",
            "created_after",
            "offset",
        },
        "query_ledger": {"filters", "date_from", "date_to", "cursor"},
        "search_documents": {
            "workspace_ids",
            "file_types",
            "file_size_min",
            "created_after",
            "offset",
        },
        "list_documents": {
            "folder_ids",
            "sources",
            "vector_statuses",
            "updated_before",
            "offset",
        },
        "list_workflow_definitions": {
            "statuses",
            "categories",
            "workspace_ids",
            "version_min",
            "created_after",
            "offset",
        },
        "list_workflow_runs": {
            "workflow_ids",
            "statuses",
            "workspace_ids",
            "trigger_sources",
            "attempt_number_min",
            "started_after",
            "offset",
        },
        "find_team_members": {
            "workspace_ids",
            "roles",
            "created_after",
            "last_login_before",
            "offset",
        },
    }
    schemas = (
        SEARCH_TASKS_SCHEMA,
        query_jobs,
        QUERY_ENTITY_AGENTS_SCHEMA,
        QUERY_LEDGER_SCHEMA,
        SEARCH_DOCUMENTS_SCHEMA,
        LIST_DOCUMENTS_SCHEMA,
        LIST_DEFINITIONS_SCHEMA,
        LIST_RUNS_SCHEMA,
        FIND_TEAM_MEMBERS_SCHEMA,
    )
    for schema in schemas:
        function = schema["function"]
        properties = set(function["parameters"]["properties"])
        assert expected[function["name"]] <= properties


def test_document_runtime_omits_unspecified_plural_filters() -> None:
    from packages.core.ai.runtime.document_actions import (
        _runtime_document_query_filters,
    )

    filters = _runtime_document_query_filters({}, workspace_id=None)

    assert filters["workspace_ids"] is None
    assert filters["folder_ids"] is None
    assert filters["file_types"] is None
    assert filters["sources"] is None
    assert filters["vector_statuses"] is None


@pytest.mark.asyncio
async def test_document_query_combines_plural_and_range_filters_before_pagination(
    db_session,
) -> None:
    from packages.core.services.document_service import list_documents

    entity_id = generate_ulid()
    other_entity_id = generate_ulid()
    workspace_a = generate_ulid()
    workspace_b = generate_ulid()
    folder_id = generate_ulid()

    def document(
        *,
        name: str,
        entity: str = entity_id,
        workspace_id: str,
        file_type: str,
        source: str,
        vector_status: str,
        size: int,
        created_day: int,
        folder: str | None,
    ) -> Document:
        return Document(
            id=generate_ulid(),
            entity_id=entity,
            name=name,
            file_type=file_type,
            source=source,
            vector_status=vector_status,
            file_size=size,
            folder_id=folder,
            visibility="workspace",
            metadata_={"origin": {"workspace_id": workspace_id}},
            created_at=datetime(2026, 8, created_day, tzinfo=timezone.utc),
            updated_at=datetime(2026, 8, created_day, 12, tzinfo=timezone.utc),
            is_trashed=False,
        )

    db_session.add_all([
        document(
            name="Quarterly report.pdf",
            workspace_id=workspace_a,
            file_type="pdf",
            source="upload",
            vector_status="ready",
            size=1_000,
            created_day=2,
            folder=None,
        ),
        document(
            name="Quarterly export.csv",
            workspace_id=workspace_b,
            file_type="csv",
            source="agent",
            vector_status="indexed",
            size=2_000,
            created_day=3,
            folder=folder_id,
        ),
        document(
            name="Quarterly oversized.pdf",
            workspace_id=workspace_a,
            file_type="pdf",
            source="upload",
            vector_status="ready",
            size=20_000,
            created_day=3,
            folder=None,
        ),
        document(
            name="Quarterly foreign.pdf",
            entity=other_entity_id,
            workspace_id=workspace_a,
            file_type="pdf",
            source="upload",
            vector_status="ready",
            size=1_000,
            created_day=2,
            folder=None,
        ),
    ])
    await db_session.flush()

    rows, total = await list_documents(
        db_session,
        entity_id,
        name_search="quarterly",
        workspace_ids=[workspace_a, workspace_b],
        folder_ids={"root", folder_id},
        file_types=["pdf", "csv"],
        sources=["upload", "agent"],
        vector_statuses=["ready", "indexed"],
        file_size_min=500,
        file_size_max=2_500,
        created_after="2026-08-01T00:00:00Z",
        created_before="2026-08-04T00:00:00Z",
        limit=1,
        offset=1,
    )

    assert total == 2
    assert [row.name for row in rows] == ["Quarterly report.pdf"]

    with pytest.raises(ValueError, match="file_size_min"):
        await list_documents(
            db_session,
            entity_id,
            file_size_min=10,
            file_size_max=1,
        )


@pytest.mark.asyncio
async def test_workflow_queries_apply_plural_and_range_filters_before_pagination(
    db_session,
) -> None:
    from packages.core.services.workflow_service import query_runs, query_workflows

    entity_id = generate_ulid()
    other_entity_id = generate_ulid()
    workspace_a = generate_ulid()
    workspace_b = generate_ulid()
    creator_a = generate_ulid()
    creator_b = generate_ulid()

    def workflow(
        *,
        name: str,
        entity: str = entity_id,
        workspace_id: str,
        creator_id: str,
        category: str,
        status: str,
        version: int,
        created_day: int,
    ) -> WorkflowDefinition:
        return WorkflowDefinition(
            id=generate_ulid(),
            entity_id=entity,
            workspace_id=workspace_id,
            created_by=creator_id,
            name=name,
            description="Quarterly finance operation",
            category=category,
            status=status,
            is_active=True,
            version=version,
            steps=[],
            variables={},
            tags=[],
            created_at=datetime(2026, 8, created_day, tzinfo=timezone.utc),
            updated_at=datetime(2026, 8, created_day, 12, tzinfo=timezone.utc),
        )

    definition_a = workflow(
        name="Quarterly close",
        workspace_id=workspace_a,
        creator_id=creator_a,
        category="finance",
        status="active",
        version=2,
        created_day=2,
    )
    definition_b = workflow(
        name="Quarterly audit",
        workspace_id=workspace_b,
        creator_id=creator_b,
        category="operations",
        status="draft",
        version=3,
        created_day=3,
    )
    db_session.add_all([
        definition_a,
        definition_b,
        workflow(
            name="Quarterly excluded",
            workspace_id=workspace_a,
            creator_id=creator_a,
            category="finance",
            status="active",
            version=9,
            created_day=4,
        ),
        workflow(
            name="Quarterly foreign",
            entity=other_entity_id,
            workspace_id=workspace_a,
            creator_id=creator_a,
            category="finance",
            status="active",
            version=2,
            created_day=2,
        ),
    ])
    await db_session.flush()

    counted = _CountingSession(db_session)
    definitions, definition_total = await query_workflows(
        counted,
        entity_id,
        query="quarterly",
        statuses=["active", "draft"],
        categories=["finance", "operations"],
        workspace_ids=[workspace_a, workspace_b],
        created_by_ids=[creator_a, creator_b],
        created_after="2026-08-01T00:00:00Z",
        created_before="2026-08-04T00:00:00Z",
        version_min=2,
        version_max=3,
        limit=1,
        offset=1,
    )

    assert counted.execute_count == 2
    assert definition_total == 2
    assert [row.name for row in definitions] == ["Quarterly close"]

    def run(
        *,
        definition: WorkflowDefinition,
        workspace_id: str,
        binding_id: str,
        status: str,
        trigger_source: str,
        starter_id: str,
        attempt: int,
        created_day: int,
    ) -> WorkflowRun:
        stamp = datetime(2026, 8, created_day, tzinfo=timezone.utc)
        return WorkflowRun(
            id=generate_ulid(),
            entity_id=entity_id,
            workflow_id=definition.id,
            workspace_id=workspace_id,
            binding_id=binding_id,
            status=status,
            trigger_source=trigger_source,
            started_by=starter_id,
            attempt_number=attempt,
            variables={},
            step_results={},
            trigger_data={},
            definition_snapshot={},
            execution_snapshot={},
            execution_trace=[],
            created_at=stamp,
            updated_at=stamp,
            started_at=stamp,
            completed_at=stamp.replace(hour=1),
        )

    binding_a = generate_ulid()
    binding_b = generate_ulid()
    db_session.add_all([
        run(
            definition=definition_a,
            workspace_id=workspace_a,
            binding_id=binding_a,
            status="completed",
            trigger_source="manual",
            starter_id=creator_a,
            attempt=1,
            created_day=2,
        ),
        run(
            definition=definition_b,
            workspace_id=workspace_b,
            binding_id=binding_b,
            status="failed",
            trigger_source="schedule",
            starter_id=creator_b,
            attempt=2,
            created_day=3,
        ),
        run(
            definition=definition_a,
            workspace_id=workspace_a,
            binding_id=binding_a,
            status="completed",
            trigger_source="manual",
            starter_id=creator_a,
            attempt=5,
            created_day=4,
        ),
    ])
    await db_session.flush()

    counted = _CountingSession(db_session)
    runs, run_total = await query_runs(
        counted,
        entity_id,
        workflow_ids=[definition_a.id, definition_b.id],
        workspace_ids=[workspace_a, workspace_b],
        binding_ids=[binding_a, binding_b],
        statuses=["completed", "failed"],
        trigger_sources=["manual", "schedule"],
        started_by_ids=[creator_a, creator_b],
        attempt_number_min=1,
        attempt_number_max=2,
        started_after="2026-08-01T00:00:00Z",
        started_before="2026-08-04T00:00:00Z",
        limit=1,
        offset=1,
    )

    assert counted.execute_count == 2
    assert run_total == 2
    assert [row.workflow_id for row in runs] == [definition_a.id]

    with pytest.raises(ValueError, match="attempt_number_min"):
        await query_runs(
            db_session,
            entity_id,
            attempt_number_min=2,
            attempt_number_max=1,
        )


@pytest.mark.asyncio
async def test_team_member_query_combines_plural_and_range_filters(
    db_session,
    monkeypatch,
) -> None:
    from packages.core import database
    from packages.core.ai.runtime.notification_actions import (
        runtime_find_team_members_action,
    )

    entity_id = generate_ulid()
    other_entity_id = generate_ulid()
    workspace_a = generate_ulid()
    workspace_b = generate_ulid()

    def user(
        *,
        email: str,
        name: str,
        entity: str,
        role: str,
        created_day: int,
    ) -> User:
        stamp = datetime(2026, 8, created_day, tzinfo=timezone.utc)
        return User(
            id=generate_ulid(),
            entity_id=entity,
            email=email,
            display_name=name,
            password_hash="test",
            status="active",
            role=role,
            created_at=stamp,
            updated_at=stamp,
            last_login_at=stamp.replace(hour=12),
        )

    direct = user(
        email=f"alpha-{generate_ulid()}@example.com",
        name="Alpha Member",
        entity=entity_id,
        role="owner",
        created_day=2,
    )
    invited = user(
        email=f"beta-{generate_ulid()}@example.com",
        name="Beta Member",
        entity=other_entity_id,
        role="viewer",
        created_day=3,
    )
    excluded = user(
        email=f"late-{generate_ulid()}@example.com",
        name="Late Member",
        entity=entity_id,
        role="owner",
        created_day=5,
    )
    db_session.add_all([direct, invited, excluded])
    await db_session.flush()
    db_session.add_all([
        UserMembership(
            id=generate_ulid(),
            user_id=invited.id,
            entity_id=entity_id,
            role="member",
            status="active",
        ),
        WorkspaceStaff(
            id=generate_ulid(),
            workspace_id=workspace_a,
            user_id=direct.id,
            status="active",
        ),
        WorkspaceStaff(
            id=generate_ulid(),
            workspace_id=workspace_b,
            user_id=invited.id,
            status="active",
        ),
        WorkspaceStaff(
            id=generate_ulid(),
            workspace_id=workspace_a,
            user_id=excluded.id,
            status="active",
        ),
    ])
    await db_session.flush()

    counted = _CountingSession(db_session)

    @asynccontextmanager
    async def session_context():
        yield counted

    monkeypatch.setattr(database, "async_session", session_context)
    payload = json.loads(await runtime_find_team_members_action(
        entity_id=entity_id,
        params={
            "query": "member",
            "workspace_ids": [workspace_a, workspace_b],
            "roles": ["owner", "member"],
            "created_after": "2026-08-01T00:00:00Z",
            "created_before": "2026-08-04T00:00:00Z",
            "last_login_after": "2026-08-01T00:00:00Z",
            "last_login_before": "2026-08-04T23:59:59Z",
            "limit": 1,
            "offset": 1,
        },
    ))

    assert counted.execute_count == 2
    assert payload["ok"] is True
    assert payload["total"] == 2
    assert payload["count"] == 1
    assert payload["members"][0]["display_name"] == "Beta Member"
    assert payload["members"][0]["role"] == "member"
    assert payload["has_more"] is False

    invalid = json.loads(await runtime_find_team_members_action(
        entity_id=entity_id,
        params={
            "created_after": "2026-08-04T00:00:00Z",
            "created_before": "2026-08-01T00:00:00Z",
        },
    ))
    assert invalid["code"] == "invalid_team_member_filters"
