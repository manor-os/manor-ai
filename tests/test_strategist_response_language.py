from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.parametrize("user_id", ["requesting-user", "schedule-owner", None])
async def test_proposal_and_repair_preserve_actor_through_runtime(monkeypatch, user_id):
    from packages.core.ai.runtime import strategist as runtime
    from packages.core.strategist.context import StrategistContext
    from packages.core.strategist.prompt import generate_proposal

    completion = AsyncMock(side_effect=[
        SimpleNamespace(content="invalid proposal"),
        SimpleNamespace(content=json.dumps({"summary": "Review complete", "tasks": []})),
    ])
    monkeypatch.setattr(runtime, "runtime_execute_text_completion", completion)
    context = StrategistContext(
        workspace=SimpleNamespace(id="workspace", entity_id="entity", name="Operations"),
    )

    proposal = await generate_proposal(context, review_id="review", user_id=user_id)

    assert proposal.review_id == "review"
    assert completion.await_count == 2
    for call in completion.await_args_list:
        assert call.kwargs["user_id"] == user_id
        assert call.kwargs["entity_id"] == "entity"
        assert call.kwargs["workspace_id"] == "workspace"
        assert call.kwargs["source"] == "strategist"


async def test_review_orchestrator_keeps_requester_through_legacy_cycle(monkeypatch):
    from packages.core.strategist import ReviewTrigger, ReviewTriggerKind
    from packages.core.tasks.ai_tasks import _execute_strategist_review_cycle

    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalar_one_or_none=lambda: None,
    )))
    review = AsyncMock(return_value={"workspace_id": "workspace", "task_count": 0})
    monkeypatch.setattr("packages.core.strategist.run_review", review)

    await _execute_strategist_review_cycle(
        db,
        "workspace",
        ReviewTrigger(kind=ReviewTriggerKind.HUMAN_REQUESTED),
        user_id="requester",
    )

    assert review.await_args.kwargs["user_id"] == "requester"


@pytest.mark.parametrize(
    ("owner", "manual_user", "expected"),
    [("job-owner", None, "job-owner"), ("job-owner", "requester", "requester"), (None, None, None)],
)
async def test_scheduler_dispatch_uses_exact_requester_or_job_owner(
    monkeypatch, owner, manual_user, expected,
):
    from packages.core.tasks import scheduler_tasks

    workspace = SimpleNamespace(
        id="workspace", entity_id="entity", status="active", deleted_at=None,
        settings={}, heartbeat_enabled=True, kind="operations",
    )
    db = SimpleNamespace(
        flush=AsyncMock(),
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: workspace)),
    )
    run = SimpleNamespace(id="run", result={})
    job = SimpleNamespace(
        id="job-db", job_id="job", user_id=owner, entity_id="entity",
        workspace_id="workspace", execution_target={"workspace_id": "workspace"},
        execution_type="strategist_review", schedule_kind="every", revision=1,
        name="Review", payload_message="Review this workspace", execution_script=None,
        manor_task_id=None,
    )
    monkeypatch.setattr(
        "packages.core.services.scheduler_service.claim_job_run",
        AsyncMock(return_value=(run, True)),
    )
    monkeypatch.setattr(
        "packages.core.services.scheduled_run_lifecycle.manual_scheduled_request_user_id",
        lambda *_args, **_kwargs: manual_user,
    )
    monkeypatch.setattr(scheduler_tasks, "_manual_scheduled_request_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(scheduler_tasks, "_preflight_scheduled_job_credits", AsyncMock())
    monkeypatch.setattr("packages.core.schedule_clock.refresh_scheduled_job_next_run_at", MagicMock())
    monkeypatch.setattr(
        "packages.core.experiments.effective_dispatch_config",
        AsyncMock(return_value=(job.execution_target, None, None)),
    )
    monkeypatch.setattr(
        "packages.core.services.workspace_readiness.evaluate_workspace_blocking_setup",
        AsyncMock(return_value=None),
    )

    dispatch, run_id = await scheduler_tasks._dispatch_job(
        db, job, datetime.now(timezone.utc),
        manual=manual_user is not None, occurrence_key="test-occurrence",
    )

    assert run_id == "run"
    assert dispatch["kwargs"]["user_id"] == expected
    assert dispatch["kwargs"]["trigger_kind"] == ("human_requested" if manual_user else "scheduled")


@pytest.mark.parametrize("owner", ["creator", None])
async def test_new_strategist_schedule_records_only_explicit_owner(monkeypatch, owner):
    from packages.core.strategist.scheduling import install_strategist_schedule

    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalar_one_or_none=lambda: None,
    )))
    persist = AsyncMock()
    monkeypatch.setattr("packages.core.services.product_growth.persist_scheduled_job", persist)

    job = await install_strategist_schedule(
        db,
        SimpleNamespace(id="workspace", entity_id="entity", name="Operations"),
        cadence="daily",
        user_id=owner,
    )

    assert job.user_id == owner
    assert persist.await_args.args[1] is job


async def test_schedule_refresh_does_not_replace_the_existing_owner(monkeypatch):
    from packages.core.strategist.scheduling import install_strategist_schedule

    existing = SimpleNamespace(user_id="original-owner")
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalar_one_or_none=lambda: existing,
    )))
    update = AsyncMock(return_value=SimpleNamespace(job=existing))
    monkeypatch.setattr("packages.core.services.scheduler_service.ScheduledJobMutationFactory.apply", update)

    await install_strategist_schedule(
        db,
        SimpleNamespace(id="workspace", entity_id="entity", name="Operations"),
        cadence="weekly",
        user_id="other-editor",
    )

    assert "user_id" not in update.await_args.args[2]
    assert existing.user_id == "original-owner"


@pytest.mark.parametrize(
    ("trigger_kind", "user_id", "creator_id", "uses_creator_language"),
    [
        ("scheduled", None, "saved-creator", True),
        ("scheduled", "job-owner", "saved-creator", False),
        ("human_requested", "requester", "saved-creator", False),
        ("human_requested", None, "saved-creator", False),
        ("scheduled", None, None, False),
    ],
)
async def test_legacy_schedule_creator_fallback_changes_only_language(
    monkeypatch, trigger_kind, user_id, creator_id, uses_creator_language,
):
    from packages.core.ai.runtime.response_language import (
        current_response_language_messages,
        resolve_runtime_response_language,
    )
    from packages.core.strategist import orchestrator, ReviewTrigger, ReviewTriggerKind

    workspace = SimpleNamespace(
        id="workspace", entity_id="entity", status="active", deleted_at=None,
        settings={"created_by_user_id": creator_id},
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalar_one_or_none=lambda: workspace,
    )))
    locale = AsyncMock(return_value="ja")
    monkeypatch.setattr("packages.core.ai.response_language.resolve_response_locale", locale)
    monkeypatch.setattr("packages.core.services.feature_flags.is_enabled", AsyncMock(return_value=False))
    messages = [{"role": "system", "content": "Review this workspace."}]

    async def review_cycle(*_args, **kwargs):
        assert kwargs["user_id"] == user_id
        rendered = current_response_language_messages(messages)
        if uses_creator_language:
            assert rendered[:-1] == messages
            assert rendered[-1]["role"] == "system"
            assert "must be Japanese" in rendered[-1]["content"]
            inherited = await resolve_runtime_response_language(entity_id="entity", user_id=None)
            assert inherited.locale == "ja"
            assert inherited.user_id is None
        else:
            assert rendered == messages
        return {"workspace_id": "workspace", "task_count": 0}

    monkeypatch.setattr(orchestrator, "_run_strategist_review_cycle", review_cycle)
    await orchestrator.run_strategist_review_cycle(
        db, "workspace", ReviewTrigger(kind=ReviewTriggerKind(trigger_kind)), user_id=user_id,
    )

    if uses_creator_language:
        locale.assert_awaited_once_with("saved-creator", db=db)
    else:
        locale.assert_not_awaited()
    assert workspace.settings == {"created_by_user_id": creator_id}
    assert current_response_language_messages(messages) == messages


@pytest.mark.parametrize("batch_actor", ["batch-owner", None])
async def test_work_batch_completion_dispatch_keeps_batch_actor(monkeypatch, batch_actor):
    from packages.core.services.workspace_operation_service import check_work_batch_completion
    from packages.core.tasks.ai_tasks import run_strategist_review

    task = SimpleNamespace(
        id="task-1", entity_id="entity", workspace_id="workspace", status="completed",
        creator_id="different-task-author", details={"workspace_work_batch_id": "batch"},
    )
    batch = SimpleNamespace(
        id="batch", status="active", task_ids=["task-1", "task-2"], details={},
        summary="Completed wave", created_by_user_id=batch_actor,
    )
    batch_result = SimpleNamespace(scalar_one_or_none=lambda: batch)
    task_result = SimpleNamespace(all=lambda: [("task-1", "completed"), ("task-2", "completed")])
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=[batch_result, task_result, batch_result]),
        flush=AsyncMock(),
    )
    monkeypatch.setattr(
        "packages.core.services.task_dependencies.release_or_block_dependents", AsyncMock(),
    )
    activity = AsyncMock()
    monkeypatch.setattr("packages.core.services.workspace_service.record_activity", activity)
    dispatch = MagicMock()
    monkeypatch.setattr(run_strategist_review, "apply_async", dispatch)

    assert await check_work_batch_completion(db, task) is batch

    dispatch.assert_called_once_with(
        args=["workspace"],
        kwargs={
            "trigger_kind": "event", "trigger_detail": "work batch completed: batch",
            "user_id": batch_actor,
        },
        countdown=1,
    )
    assert activity.await_args.kwargs["user_id"] == batch_actor
    assert batch.status == "completed"
    assert "strategist_triggered_at" in batch.details
    # Repeated task terminal receipts do not create another review.
    assert await check_work_batch_completion(db, task) is None
    assert dispatch.call_count == 1
