"""Task search filters execute together in SQL before pagination."""

from datetime import datetime, timezone

import pytest

from packages.core.models.base import generate_ulid
from packages.core.models.task import Task
from packages.core.services.task_service import list_tasks


pytestmark = pytest.mark.oss_regression


def _dt(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_list_tasks_combines_plural_filters_ranges_and_pagination(db_session):
    entity_id = generate_ulid()
    workspace_a, workspace_b = generate_ulid(), generate_ulid()
    category_a, category_b = generate_ulid(), generate_ulid()
    assignee_a, assignee_b, assignee_other = (
        generate_ulid(),
        generate_ulid(),
        generate_ulid(),
    )

    matching_a = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_a,
        category_id=category_a,
        title="OAuth Review Alpha",
        description="First matching task",
        status="completed",
        priority=3,
        assignee_id=assignee_a,
        task_type="general",
        details={},
        created_at=_dt(1),
        updated_at=_dt(2),
        completed_at=_dt(3),
        deadline=_dt(4),
    )
    matching_b = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace_b,
        category_id=category_b,
        title="Second matching task",
        description="OAuth review beta",
        status="in_progress",
        priority=4,
        assignee_id=assignee_b,
        task_type="inspection",
        details={},
        created_at=_dt(2),
        updated_at=_dt(3),
        deadline=_dt(5),
    )
    excluded = [
        Task(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace_a,
            category_id=category_a,
            title="OAuth review priority outside range",
            status="completed",
            priority=5,
            assignee_id=assignee_a,
            task_type="general",
            details={},
            created_at=_dt(2),
            updated_at=_dt(3),
            deadline=_dt(4),
        ),
        Task(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace_a,
            category_id=category_a,
            title="OAuth review wrong status",
            status="failed",
            priority=3,
            assignee_id=assignee_a,
            task_type="general",
            details={},
            created_at=_dt(2),
            updated_at=_dt(3),
            deadline=_dt(4),
        ),
        Task(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace_a,
            category_id=category_a,
            title="OAuth review wrong assignee",
            status="completed",
            priority=3,
            assignee_id=assignee_other,
            task_type="general",
            details={},
            created_at=_dt(2),
            updated_at=_dt(3),
            deadline=_dt(4),
        ),
        Task(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=workspace_a,
            category_id=category_a,
            title="OAuth review outside created range",
            status="completed",
            priority=3,
            assignee_id=assignee_a,
            task_type="general",
            details={},
            created_at=_dt(6),
            updated_at=_dt(6),
            deadline=_dt(4),
        ),
    ]
    db_session.add_all([matching_a, matching_b, *excluded])
    await db_session.commit()

    filters = {
        "query": "oauth review",
        "status": "completed",
        "statuses": ["in_progress"],
        "workspace_id": workspace_a,
        "workspace_ids": [workspace_b],
        "category_id": category_a,
        "category_ids": [category_b],
        "assignee_id": assignee_a,
        "assignee_ids": [assignee_b],
        "task_type": "general",
        "task_types": ["inspection"],
        "priority": 3,
        "priorities": [4, 5],
        "priority_min": 2,
        "priority_max": 4,
        "created_after": _dt(1),
        "created_before": _dt(2),
        "updated_after": _dt(2),
        "updated_before": _dt(3),
        "deadline_after": _dt(4),
        "deadline_before": _dt(5),
        "limit": 1,
    }

    first_page, first_total = await list_tasks(db_session, entity_id, offset=0, **filters)
    second_page, second_total = await list_tasks(db_session, entity_id, offset=1, **filters)

    assert first_total == second_total == 2
    assert [task.id for task in first_page] == [matching_b.id]
    assert [task.id for task in second_page] == [matching_a.id]


@pytest.mark.asyncio
async def test_list_tasks_keeps_legacy_single_filters_and_inclusive_datetime_range(db_session):
    entity_id = generate_ulid()
    completed_at = _dt(2, 12)
    matching = Task(
        id=generate_ulid(),
        entity_id=entity_id,
        title="Exact boundary task",
        status="completed",
        priority=4,
        assignee_id=generate_ulid(),
        completed_at=completed_at,
        details={},
    )
    db_session.add(matching)
    await db_session.commit()

    tasks, total = await list_tasks(
        db_session,
        entity_id,
        status="completed",
        priority=4,
        assignee_id=matching.assignee_id,
        completed_after=completed_at,
        completed_before=completed_at,
    )

    assert total == 1
    assert [task.id for task in tasks] == [matching.id]


@pytest.mark.asyncio
async def test_list_tasks_rejects_inverted_ranges(db_session):
    entity_id = generate_ulid()

    with pytest.raises(ValueError, match="priority_min"):
        await list_tasks(db_session, entity_id, priority_min=5, priority_max=2)

    with pytest.raises(ValueError, match="created_after"):
        await list_tasks(
            db_session,
            entity_id,
            created_after=_dt(2),
            created_before=_dt(1),
        )
