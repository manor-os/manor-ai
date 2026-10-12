from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.plans import executor


async def _add_workspace_plan_runtime(
    db,
    *,
    entity_id: str,
    workspace_id: str,
    service_key: str = "ops",
):
    """Install the minimal active service required by the Task runtime factory."""
    from packages.core.models.workspace import Agent, AgentSubscription

    agent = Agent(
        entity_id=entity_id,
        name=f"{service_key} agent",
        status="active",
        config={},
    )
    db.add(agent)
    await db.flush()
    subscription = AgentSubscription(
        entity_id=entity_id,
        workspace_id=workspace_id,
        agent_id=agent.id,
        service_key=service_key,
        status="active",
    )
    db.add(subscription)
    await db.flush()
    return subscription


def _task(title: str, *, description: str = "", expected_output=None, details=None):
    return SimpleNamespace(
        title=title,
        description=description,
        expected_output=expected_output,
        details=details or {},
    )


def _step(result, *, status: str = "done", key: str = "draft"):
    return SimpleNamespace(step_status=status, result=result, step_key=key)


def test_file_deliverable_with_text_only_result_is_missing_artifact() -> None:
    task = _task(
        "生成客户可下载的PDF报价单",
        description="输出一个可交付给客户的报价单文件。",
    )
    steps = [_step({"text": "这里是报价单的文字内容。"})]

    issue = executor._missing_artifact_issue(task, steps)

    assert issue is not None
    assert "saved file link or path" in issue


def test_text_report_does_not_require_artifact() -> None:
    task = _task(
        "整理3款方案的文字说明",
        description="只需要输出结构、材料和卖点的文字描述。",
    )
    steps = [_step({"text": "方案A、B、C的文字总结。"})]

    assert executor._missing_artifact_issue(task, steps) is None


def test_explicit_text_only_reference_does_not_require_artifact() -> None:
    task = _task(
        "整理报价单文字说明",
        description="只需要文字方案，不需要文件或附件。",
    )
    steps = [_step({"text": "文字说明。"})]

    assert executor._missing_artifact_issue(task, steps) is None


def test_plain_text_internal_memo_does_not_require_file_artifact() -> None:
    task = _task(
        "Audit Brand Voice and Campaign Playbook knowledge documents",
        description=(
            "Review the two existing knowledge collections and deliver a "
            "concise internal memo (plain text, ~400-600 words)."
        ),
    )
    steps = [_step({"memo_text": "INTERNAL MEMO\n\nBrand voice summary."})]

    assert executor._missing_artifact_issue(task, steps) is None


def test_workspace_video_ideas_do_not_require_video_file_artifact() -> None:
    task = _task(
        "Create three video candidates for this week",
        description=(
            "Deliver an inline workspace_chat packet with hooks, script outlines, "
            "production notes, and recommended titles. No file attachment is required."
        ),
    )
    steps = [_step({"text": "Three video ideas with hooks and script outlines."})]

    assert executor._missing_artifact_issue(task, steps) is None


def test_generic_url_schema_does_not_require_saved_artifact() -> None:
    task = _task(
        "Research competitor examples",
        expected_output={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "notes": {"type": "string"},
            },
        },
    )
    steps = [_step({"text": "Competitor URL: https://example.com"})]

    assert executor._missing_artifact_issue(task, steps) is None


def test_explicit_file_deliverable_still_requires_artifact_with_plain_text_summary() -> None:
    task = _task(
        "Export customer quote",
        description="Create a PDF file and include a plain text summary.",
    )
    steps = [_step({"memo_text": "Summary only, no file path."})]

    issue = executor._missing_artifact_issue(task, steps)

    assert issue is not None
    assert "saved file link or path" in issue


def test_mp4_deliverable_rejects_script_packet_artifact() -> None:
    task = _task(
        "Create and verify today's stickman MP4",
        description="Produce the final video and return daily-stickman-video.mp4.",
        details={"requires_artifact": True, "artifact_type": "video"},
    )
    steps = [
        _step(
            {
                "status": "succeeded",
                "outputs": {
                    "files": [
                        {
                            "name": "stickman-script-packet.md",
                            "path": "workspace/artifacts/stickman-script-packet.md",
                        }
                    ]
                },
            }
        )
    ]

    issue = executor._missing_artifact_issue(task, steps)

    assert issue is not None
    assert ".mp4" in issue


def test_mp4_deliverable_accepts_mp4_artifact() -> None:
    task = _task(
        "Create and verify today's stickman MP4",
        description="Produce the final video and return daily-stickman-video.mp4.",
        details={"requires_artifact": True, "artifact_type": "video"},
    )
    steps = [
        _step(
            {
                "status": "succeeded",
                "outputs": {
                    "files": [
                        {
                            "name": "daily-stickman-video.mp4",
                            "path": "workspace/artifacts/daily-stickman-video.mp4",
                        }
                    ]
                },
            }
        )
    ]

    assert executor._missing_artifact_issue(task, steps) is None


def test_office_file_deliverable_still_requires_artifact() -> None:
    task = _task(
        "Create investor slides",
        description="Create a presentation deck for the weekly update.",
    )
    steps = [_step({"text": "Draft slide content only."})]

    issue = executor._missing_artifact_issue(task, steps)

    assert issue is not None
    assert "saved file link or path" in issue


def test_workspace_missing_artifact_issue_uses_default_workspace_save_policy() -> None:
    task = _task(
        "Create investor slides",
        description="Create a presentation deck for the weekly update.",
    )
    task.workspace_id = "ws_1"
    steps = [_step({"text": "Draft slide content only."})]

    issue = executor._missing_artifact_issue(task, steps)

    assert issue is not None
    assert "workspace's default artifact folder" in issue
    assert "Do not ask the user for a save location" in issue


def test_expected_output_schema_can_require_artifact() -> None:
    task = _task(
        "准备客户选样稿",
        expected_output={
            "type": "object",
            "properties": {
                "image_url": {"type": "string"},
                "notes": {"type": "string"},
            },
        },
    )

    assert executor._task_requires_artifact(task)


def test_explicit_no_write_constraint_overrides_artifact_recovery_inference() -> None:
    task = _task(
        "Close out the baseline artifact recovery diagnosis",
        expected_output={
            "type": "object",
            "properties": {
                "artifact_url": {"type": "string"},
                "summary": {"type": "string"},
            },
        },
        details={
            "runtime_context": {
                "instructions": (
                    "Keep this inspection read-only. Do not create or write files, "
                    "including artifact generation."
                ),
            },
        },
    )
    steps = [_step({"summary": "The diagnosis is complete inline."})]

    assert executor._task_requires_artifact(task) is False
    assert executor._missing_artifact_issue(task, steps) is None


def test_artifact_refs_detect_top_level_and_nested_files() -> None:
    refs = executor._artifact_refs_from_result(
        {
            "image_url": "/api/v1/fs/ent/design.png",
            "files": [{"fs_path": "Designs/sheet.pdf", "type": "pdf"}],
        },
        step_key="render_sheet",
    )

    assert {ref.get("source") for ref in refs} >= {"image_url", "fs_path"}


def test_artifact_refs_detect_common_aliases() -> None:
    refs = executor._artifact_refs_from_result(
        {
            "artifact_url": "/api/v1/fs/entity/reports/final.pdf",
            "download_url": "/api/v1/fs/entity/reports/final.pdf?download=1",
            "file_path": "Workspaces/Demo/reports/final.pdf",
        },
        step_key="compile_report",
    )

    sources = {ref.get("source") for ref in refs}
    assert {"artifact_url", "file_path"} <= sources
    assert "download_url" not in sources
    assert any(ref.get("fs_path") == "Workspaces/Demo/reports/final.pdf" for ref in refs)


def test_artifact_refs_detect_canonical_envelope_output_files() -> None:
    refs = executor._artifact_refs_from_result(
        {
            "status": "succeeded",
            "summary": "Rendered and verified the final MP4.",
            "outputs": {
                "files": [
                    {
                        "name": "daily-stickman.mp4",
                        "path": "Faceless Stickman Studio/final/daily-stickman.mp4",
                    }
                ]
            },
        },
        step_key="create_verified_video",
    )

    assert refs == [
        {
            "type": "file",
            "step": "create_verified_video",
            "source": "path",
            "fs_path": "Faceless Stickman Studio/final/daily-stickman.mp4",
            "open_url": "Faceless Stickman Studio/final/daily-stickman.mp4",
        }
    ]


def test_artifact_refs_detect_envelope_outputs_data_payload() -> None:
    refs = executor._artifact_refs_from_result(
        {
            "status": "partial",
            "summary": "Rendered and verified the final video.",
            "outputs": {
                "data": {
                    "mp4_fs_path": "Workspaces/Demo/final/daily-stickman-video.mp4",
                    "verification_status": "verified",
                    "duration_seconds": 44.29,
                },
            },
        },
        step_key="produce_video",
    )

    assert any(ref.get("fs_path", "").endswith("daily-stickman-video.mp4") for ref in refs)


def test_verified_artifact_allows_partial_envelope_to_continue() -> None:
    task = _task(
        "Produce and upload a verified MP4",
        description="Create the final video file for upload.",
    )
    result = {
        "status": "partial",
        "summary": "Checksum was unavailable, but the MP4 was verified.",
        "outputs": {
            "data": {
                "mp4_fs_path": "Workspaces/Demo/final/daily-stickman-video.mp4",
                "verification_status": "verified",
                "duration_seconds": 44.29,
            },
        },
    }

    assert executor._structured_result_blocker(result, artifact_required=True) is None
    assert executor._missing_artifact_issue(task, [_step(result)]) is None


def test_task_artifact_refs_dedupe_same_file_across_sources() -> None:
    refs = [
        {
            "type": "file",
            "step": "draft",
            "source": "fs_path",
            "fs_path": "workspace/social/draft-pack.md",
        },
        {
            "type": "file",
            "step": "draft",
            "source": "files",
            "name": "draft-pack.md",
            "fs_path": "workspace/social/draft-pack.md",
        },
        {
            "type": "file",
            "step": "publish",
            "source": "fs_path",
            "fs_path": "workspace/social/draft-pack.md",
        },
    ]

    assert executor._dedupe_task_artifact_refs(refs) == [
        {
            **refs[0],
            "name": "draft-pack.md",
            "open_url": "workspace/social/draft-pack.md",
        }
    ]


def test_task_artifact_refs_merge_document_and_filesystem_aliases() -> None:
    refs = [
        {
            "type": "file",
            "fs_path": "Workspaces/Launch/report.md",
        },
        {
            "type": "document",
            "document_id": "doc_report",
            "url": "/api/v1/fs/entity/Workspaces/Launch/report.md",
            "name": "report.md",
        },
    ]

    assert executor._dedupe_task_artifact_refs(refs, entity_id="entity") == [
        {
            "type": "file",
            "fs_path": "Workspaces/Launch/report.md",
            "open_url": "/viewer/doc_report",
            "document_id": "doc_report",
            "url": "/api/v1/fs/entity/Workspaces/Launch/report.md",
            "name": "report.md",
            "viewer_url": "/viewer/doc_report",
            "markdown_link": "[report.md](/viewer/doc_report)",
        }
    ]


def test_artifact_refs_ignore_reference_documents() -> None:
    refs = executor._artifact_refs_from_result(
        {
            "context": "[Document 1] Unit Inventory",
            "source_count": 1,
            "sources": [{"document_id": "doc_1", "name": "Unit Inventory"}],
            "documents": [
                {
                    "id": "doc_1",
                    "name": "Unit Inventory",
                    "fs_path": "Unit Inventory & Availability.md",
                }
            ],
        },
        step_key="knowledge_search",
    )

    assert refs == []


@pytest.mark.asyncio
async def test_missing_artifact_completed_plan_replans_before_hitl(db_session, monkeypatch) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.models.workspace import Workspace
    from packages.core.plans.executor import PlanExecutor

    direct_dispatches: list[str] = []
    monkeypatch.setattr(
        "packages.core.tasks.ai_tasks.plan_and_run_task.delay",
        lambda task_id: direct_dispatches.append(task_id),
    )

    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    workspace = Workspace(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Artifact recovery",
    )
    db_session.add(workspace)
    subscription = await _add_workspace_plan_runtime(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace.id,
    )
    db_session.add(Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace.id,
        title="生成客户可下载的PDF报价单",
        status="in_progress",
        priority=3,
        task_type="general",
        details={},
        owner_service_key=subscription.service_key,
        owner_subscription_id=subscription.id,
    ))
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace.id,
        task_id=task_id,
        status="running",
        plan_dag={},
    )
    step = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan_id,
        entity_id=entity_id,
        step_key="compile_quote",
        kind="subagent",
        step_status="done",
        result={"text": "这里是报价单文字，但没有保存文件。"},
    )
    db_session.add(plan)
    db_session.add(step)
    await db_session.commit()

    replanned = await PlanExecutor._maybe_replan_for_missing_artifact(db_session, plan, [step])
    await db_session.flush()

    assert replanned is True
    assert plan.status == "replanned"
    task = await db_session.get(Task, task_id)
    assert task is not None
    assert task.status == "in_progress"
    assert task.details["_replan_context"]["reason"] == "missing_artifact"
    assert task.details["_replan_context"]["failed_steps"][0]["error"]["type"] == "MissingArtifactEvidence"
    assert task.details["_replan_context"]["artifact_recovery"]["default_action"] == "materialize_saved_workspace_file"
    assert "fs_path" in task.details["_replan_context"]["artifact_recovery"]["required_evidence"]
    assert direct_dispatches == []

    await PlanExecutor._commit_and_dispatch_replan(db_session, task_id, plan_id)
    assert direct_dispatches == []

    from sqlalchemy import select

    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT

    event = (await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == task_id,
            RuntimeOutboxEvent.delivered_at.is_(None),
        )
    )).scalars().one()
    assert event.payload["task_id"] == task_id


@pytest.mark.asyncio
async def test_replan_intent_is_staged_before_commit_without_direct_broker(
    monkeypatch,
) -> None:
    from contextlib import asynccontextmanager

    from packages.core.constants.task import TaskRuntimeKind
    from packages.core.plans.executor import PlanExecutor

    events: list[str] = []

    class CommitRecorder:
        def begin_nested(self):
            @asynccontextmanager
            async def nested_transaction():
                events.append("savepoint")
                yield

            return nested_transaction()

        async def commit(self) -> None:
            events.append("commit")

        async def execute(self, _statement):
            task = SimpleNamespace(id="task_1", status="in_progress")
            return SimpleNamespace(scalar_one_or_none=lambda: task)

    async def noop_cleanup(*_args, **_kwargs) -> None:
        events.append("cleanup")

    async def coordinated_dispatch(_db, _task):
        events.append("resolve")
        return SimpleNamespace(
            runtime_kind=TaskRuntimeKind.COORDINATED_PLAN,
            task_id="task_1",
        )

    async def stage_intent(_db, _task, _dispatch) -> None:
        events.append("stage")

    def direct_broker_forbidden(*_args, **_kwargs):
        pytest.fail("replan admission must not publish directly to the broker")

    monkeypatch.setattr(
        PlanExecutor,
        "_cleanup_terminal_approvals_after_commit",
        noop_cleanup,
    )
    monkeypatch.setattr(
        "packages.core.services.task_execution.resolve_task_execution",
        coordinated_dispatch,
    )
    monkeypatch.setattr(
        "packages.core.services.task_execution.stage_task_execution_intent",
        stage_intent,
    )
    monkeypatch.setattr(
        "packages.core.services.task_execution.dispatch_task_execution",
        direct_broker_forbidden,
    )
    monkeypatch.setattr(
        "packages.core.tasks.ai_tasks.plan_and_run_task.delay",
        direct_broker_forbidden,
    )

    await PlanExecutor._commit_and_dispatch_replan(
        CommitRecorder(),
        "task_1",
        "plan_1",
    )

    assert events == ["savepoint", "resolve", "stage", "commit", "cleanup"]


@pytest.mark.asyncio
async def test_broker_unavailability_does_not_break_durable_replan_admission(
    db_session,
    monkeypatch,
) -> None:
    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.models.task import Task
    from packages.core.models.workspace import Workspace
    from packages.core.plans.executor import PlanExecutor
    from packages.core.services.task_execution import TASK_EXECUTION_OUTBOX_EVENT

    entity_id, workspace_id, task_id, plan_id = (generate_ulid() for _ in range(4))
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Durable replan",
    )
    db_session.add(workspace)
    subscription = await _add_workspace_plan_runtime(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Retry through durable intent",
        status="in_progress",
        priority=3,
        task_type="general",
        details={"_replan_context": {"prior_plan_id": plan_id}},
        owner_service_key=subscription.service_key,
        owner_subscription_id=subscription.id,
    )
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        status="replanned",
        plan_dag={},
    )
    db_session.add_all([task, plan])
    await db_session.commit()

    def broker_unavailable(*_args, **_kwargs):
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        "packages.core.services.task_execution.dispatch_task_execution",
        broker_unavailable,
    )
    monkeypatch.setattr(
        "packages.core.tasks.ai_tasks.plan_and_run_task.delay",
        broker_unavailable,
    )

    assert await PlanExecutor._commit_and_dispatch_replan(
        db_session,
        task_id,
        plan_id,
    ) is True

    db_session.expire_all()
    persisted_task = await db_session.get(Task, task_id)
    assert persisted_task is not None
    assert persisted_task.status == "in_progress"
    event = (await db_session.execute(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.event_type == TASK_EXECUTION_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == task_id,
            RuntimeOutboxEvent.delivered_at.is_(None),
        )
    )).scalars().one()
    assert event.payload["task_id"] == task_id


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["factory", "staging"])
async def test_replan_admission_failure_creates_recovery_card(
    db_session,
    monkeypatch,
    failure_point,
) -> None:
    from packages.core.models.base import generate_ulid
    from sqlalchemy import select

    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.task import Conversation, Message, Task
    from packages.core.models.workspace import Workspace
    from packages.core.plans.executor import PlanExecutor

    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    workspace_id = generate_ulid()
    workspace = Workspace(
        id=workspace_id,
        entity_id=entity_id,
        name="Replan Dispatch Recovery",
    )
    db_session.add(workspace)
    subscription = await _add_workspace_plan_runtime(
        db_session,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    task = Task(
        id=task_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        title="Retry replacement planning",
        status="in_progress",
        priority=3,
        task_type="general",
        details={"_replan_context": {"prior_plan_id": plan_id}},
        owner_service_key=subscription.service_key,
        owner_subscription_id=subscription.id,
    )
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
        status="replanned",
        plan_dag={},
    )
    db_session.add_all([task, plan])
    await db_session.commit()

    async def fail_admission(*_args, **_kwargs) -> None:
        raise RuntimeError(f"deterministic {failure_point} failure")

    monkeypatch.setattr(
        "packages.core.services.task_execution.resolve_task_execution"
        if failure_point == "factory"
        else "packages.core.services.task_execution.stage_task_execution_intent",
        fail_admission,
    )

    dispatched = await PlanExecutor._commit_and_dispatch_replan(
        db_session,
        task_id,
        plan_id,
    )

    assert dispatched is False
    db_session.expire_all()
    task = await db_session.get(Task, task_id)
    plan = await db_session.get(ExecutionPlan, plan_id)
    assert task is not None and task.status == "blocked"
    assert task.details["_replan_context"]["dispatch_status"] == "failed"
    assert task.details["_replan_context"]["dispatch_plan_id"] == plan_id
    assert plan is not None and plan.status == "replanned"
    assert plan.last_error["type"] == "ReplanDispatchFailed"
    recovery = (await db_session.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.workspace_id == workspace_id,
            Message.resolved_at.is_(None),
        )
    )).scalars().all()
    assert recovery == []


@pytest.mark.asyncio
async def test_replan_dispatch_failure_does_not_regress_cached_terminal_task(
    db_session,
    monkeypatch,
) -> None:
    from sqlalchemy import update

    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan
    from packages.core.models.task import Task
    from packages.core.plans.executor import PlanExecutor

    entity_id, task_id, plan_id = (generate_ulid() for _ in range(3))
    task = Task(
        id=task_id,
        entity_id=entity_id,
        title="Terminal task wins the dispatch race",
        status="in_progress",
        priority=3,
        task_type="general",
        details={"_replan_context": {"prior_plan_id": plan_id}},
    )
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        task_id=task_id,
        status="replanned",
        plan_dag={},
    )
    db_session.add_all([task, plan])
    await db_session.commit()

    await db_session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(status="completed")
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    assert task.status == "in_progress"

    async def stage_forbidden(*_args, **_kwargs) -> None:
        pytest.fail("terminal Tasks must not stage a replacement execution intent")

    monkeypatch.setattr(
        "packages.core.services.task_execution.stage_task_execution_intent",
        stage_forbidden,
    )
    dispatched = await PlanExecutor._commit_and_dispatch_replan(
        db_session,
        task_id,
        plan_id,
    )

    assert dispatched is False
    db_session.expire_all()
    task = await db_session.get(Task, task_id)
    plan = await db_session.get(ExecutionPlan, plan_id)
    assert task is not None and task.status == "completed"
    assert "dispatch_status" not in task.details["_replan_context"]
    assert plan is not None and plan.last_error is None


@pytest.mark.asyncio
async def test_replanned_plan_ignores_duplicate_run_cycle(db_session) -> None:
    from contextlib import asynccontextmanager

    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan
    from packages.core.plans.executor import PlanExecutor

    plan_id = generate_ulid()
    db_session.add(ExecutionPlan(
        id=plan_id,
        entity_id=generate_ulid(),
        status="replanned",
        plan_dag={},
    ))
    await db_session.commit()

    @asynccontextmanager
    async def session_factory():
        yield db_session

    result = await PlanExecutor(session_factory=session_factory).run_cycle(plan_id)

    assert result == {
        "plan_id": plan_id,
        "status": "replanned",
        "next_action": "stop",
    }


@pytest.mark.asyncio
async def test_missing_artifact_supervisor_requests_replan_not_human(db_session, monkeypatch) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.plans.executor import PlanExecutor

    async def fail_supervisor_llm(*_args, **_kwargs):
        raise AssertionError("missing artifact gate should not call LLM supervisor")

    monkeypatch.setattr("packages.core.ai.llm_client.chat_completion", fail_supervisor_llm)

    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            title="生成客户可下载的PDF报价单",
            status="in_progress",
            priority=3,
            task_type="general",
            details={},
        )
    )
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        task_id=task_id,
        status="running",
        plan_dag={},
    )
    db_session.add(plan)
    db_session.add(
        ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            step_key="compile_quote",
            kind="subagent",
            step_status="done",
            result={"text": "这里是报价单文字，但没有保存文件。"},
        )
    )
    await db_session.commit()

    decision = await PlanExecutor._supervise_outcome(db_session, plan, "completed")

    from packages.core.constants.supervisor import (
        SupervisorDecisionSource,
        SupervisorVerdict,
    )
    assert decision.verdict is SupervisorVerdict.NEEDS_REPLAN
    # The gate states its own finding — a verdict without evidence is the
    # bug this type exists to prevent.
    assert decision.evidence
    assert decision.source is SupervisorDecisionSource.GATE


@pytest.mark.asyncio
async def test_partial_status_supervisor_requests_replan_not_human(db_session, monkeypatch) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.plans.executor import PlanExecutor

    async def fail_supervisor_llm(*_args, **_kwargs):
        raise AssertionError("partial status gate should not call LLM supervisor")

    monkeypatch.setattr("packages.core.ai.llm_client.chat_completion", fail_supervisor_llm)

    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            title="Score workspace goals from current evidence",
            status="in_progress",
            priority=3,
            task_type="general",
            details={},
        )
    )
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        task_id=task_id,
        status="completed",
        plan_dag={},
    )
    db_session.add(plan)
    db_session.add(
        ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            step_key="persist_goal_scores",
            kind="subagent",
            step_status="done",
            result={"status": "partial", "summary": "persist applied=false"},
        )
    )
    await db_session.commit()

    decision = await PlanExecutor._supervise_outcome(db_session, plan, "completed")

    from packages.core.constants.supervisor import (
        SupervisorDecisionSource,
        SupervisorVerdict,
    )
    assert decision.verdict is SupervisorVerdict.NEEDS_REPLAN
    assert "status=partial" in (decision.evidence or "")
    assert decision.source is SupervisorDecisionSource.GATE


@pytest.mark.asyncio
async def test_structured_hitl_claim_without_request_needs_repair(db_session, monkeypatch) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.plans.executor import PlanExecutor

    async def fail_supervisor_llm(*_args, **_kwargs):
        raise AssertionError("HITL gate should not call LLM supervisor")

    monkeypatch.setattr("packages.core.ai.llm_client.chat_completion", fail_supervisor_llm)

    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            title="Score workspace goals from current evidence",
            status="in_progress",
            priority=3,
            task_type="general",
            details={},
        )
    )
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        task_id=task_id,
        status="completed",
        plan_dag={},
    )
    db_session.add(plan)
    db_session.add(
        ExecutionStep(
            id=generate_ulid(),
            plan_id=plan_id,
            entity_id=entity_id,
            step_key="persist_goal_scores",
            kind="subagent",
            step_status="done",
            result={
                "__hitl__": True,
                "hitl": {"prompt": "Approve persist?", "tool": "sandbox_exec"},
            },
        )
    )
    await db_session.commit()

    decision = await PlanExecutor._supervise_outcome(db_session, plan, "completed")

    from packages.core.constants.supervisor import (
        SupervisorDecisionSource,
        SupervisorVerdict,
    )
    assert decision.verdict is SupervisorVerdict.NEEDS_REPLAN
    assert decision.source is SupervisorDecisionSource.GATE


# ── replan context: completed steps carry reusable handles ────────────


@pytest.mark.asyncio
async def test_model_human_flag_does_not_stop_bounded_replan(
    db_session,
):
    from packages.core.constants.execution import ExecutionPlanStatus
    from packages.core.constants.supervisor import (
        SupervisorDecisionSource,
        SupervisorVerdict,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.plans.executor import PlanExecutor

    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            title="Create missing TikTok algorithm prep files",
            status="in_progress",
            priority=3,
            task_type="general",
            details={},
        )
    )
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        task_id=task_id,
        status=ExecutionPlanStatus.FAILED,
        plan_dag={},
    )
    failure = {
        "reason": "docs.upload is blocked for this run",
        "blockers": ["docs.upload"],
        "retryable": False,
        "requires_human": True,
    }
    step = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan_id,
        entity_id=entity_id,
        step_key="create_tiktok_algorithm_artifacts",
        kind="subagent",
        step_status="failed",
        result={"status": "failed", "summary": failure["reason"], "failure": failure},
        error={"type": "StepResultFailed", "message": failure["reason"], "failure": failure},
    )
    db_session.add_all([plan, step])
    await db_session.commit()

    assert await PlanExecutor._maybe_replan(db_session, plan, [step]) is True

    decision = await PlanExecutor._supervise_outcome(
        db_session,
        plan,
        ExecutionPlanStatus.FAILED,
    )
    assert decision.verdict is SupervisorVerdict.FAILED
    assert decision.source is SupervisorDecisionSource.GATE
    assert "docs.upload" in decision.evidence



def _done_step(result, *, key: str = "draft", kind: str = "subagent", status: str = "done"):
    return SimpleNamespace(step_status=status, result=result, step_key=key, kind=kind)


def test_succeeded_step_context_surfaces_generated_files() -> None:
    steps = [
        _done_step(
            {
                "text": "Script written and saved.",
                "artifact_materialized": True,
                "files": [
                    {"name": "script.md", "fs_path": "workspace/artifacts/script.md"},
                ],
            },
            key="write_script",
        )
    ]

    contexts = executor._succeeded_step_contexts(steps)

    assert len(contexts) == 1
    entry = contexts[0]
    assert entry["step_key"] == "write_script"
    assert entry["kind"] == "subagent"
    # The point of the change: a usable handle, not only a text blurb.
    assert entry["artifacts"] == [
        {
            "type": "file",
            "step": "write_script",
            "source": "fs_path",
            "name": "script.md",
            "fs_path": "workspace/artifacts/script.md",
            "open_url": "workspace/artifacts/script.md",
        }
    ]


def test_succeeded_step_context_marks_done_step_with_no_output() -> None:
    steps = [_done_step(None, key="notify_team", kind="action")]

    contexts = executor._succeeded_step_contexts(steps)

    assert contexts == [
        {"step_key": "notify_team", "kind": "action", "no_output": True}
    ]


def test_succeeded_step_context_surfaces_top_level_document_and_path() -> None:
    steps = [
        _done_step(
            {
                "text": "Deck exported.",
                "document_id": "doc_42",
                "fs_path": "workspace/artifacts/deck.pptx",
            },
            key="export_deck",
        )
    ]

    entry = executor._succeeded_step_contexts(steps)[0]

    assert entry["document_id"] == "doc_42"
    assert entry["fs_path"] == "workspace/artifacts/deck.pptx"


def test_succeeded_step_context_keeps_usable_text_not_a_label() -> None:
    long_text = "x" * 5000
    entry = executor._succeeded_step_contexts([_done_step({"text": long_text})])[0]

    assert len(entry["result_summary"]) == executor._REPLAN_STEP_SUMMARY_CHARS
    assert len(entry["result_summary"]) > 200


def test_succeeded_step_context_skips_non_done_steps() -> None:
    steps = [
        _done_step({"text": "ok"}, key="a"),
        _done_step({"text": "boom"}, key="b", status="failed"),
        _done_step({"text": "later"}, key="c", status="pending"),
    ]

    assert [e["step_key"] for e in executor._succeeded_step_contexts(steps)] == ["a"]


def test_succeeded_step_context_stays_within_prompt_budget() -> None:
    import json

    steps = [
        _done_step(
            {
                "text": "y" * 4000,
                "artifact_materialized": True,
                "files": [
                    {"name": f"asset-{i}-{n}.png", "fs_path": f"workspace/artifacts/asset-{i}-{n}.png"}
                    for n in range(25)
                ],
            },
            key=f"step_{i}",
        )
        for i in range(40)
    ]

    contexts = executor._succeeded_step_contexts(steps)

    assert len(contexts) == executor._REPLAN_MAX_SUCCEEDED_STEPS
    # Tail is kept — downstream steps are what a follow-up plan consumes.
    assert contexts[-1]["step_key"] == "step_39"
    for entry in contexts:
        assert len(entry.get("artifacts", [])) <= executor._REPLAN_MAX_ARTIFACTS_PER_STEP
        assert len(entry.get("result_summary", "")) <= executor._REPLAN_STEP_SUMMARY_CHARS
    summary_chars = sum(len(e.get("result_summary", "")) for e in contexts)
    assert summary_chars <= executor._REPLAN_SUMMARY_TOTAL_CHARS
    # Stated budget for the whole blob that gets dumped into the prompt.
    assert len(json.dumps(contexts, ensure_ascii=False)) < 30000


@pytest.mark.asyncio
async def test_replan_context_carries_artifacts_of_completed_steps(db_session, monkeypatch) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.plans.executor import PlanExecutor

    monkeypatch.setattr(
        "packages.core.tasks.ai_tasks.plan_and_run_task.delay",
        lambda task_id: None,
    )

    entity_id = generate_ulid()
    task_id = generate_ulid()
    plan_id = generate_ulid()
    db_session.add(
        Task(
            id=task_id,
            entity_id=entity_id,
            title="做一条产品短视频",
            status="in_progress",
            priority=3,
            task_type="general",
            details={},
        )
    )
    plan = ExecutionPlan(
        id=plan_id,
        entity_id=entity_id,
        task_id=task_id,
        status="running",
        plan_dag={},
    )
    produced = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan_id,
        entity_id=entity_id,
        step_key="write_script",
        kind="subagent",
        step_status="done",
        result={
            "text": "脚本全文……",
            "artifact_materialized": True,
            "files": [{"name": "script.md", "fs_path": "workspace/artifacts/script.md"}],
        },
    )
    side_effect = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan_id,
        entity_id=entity_id,
        step_key="notify_team",
        kind="action",
        step_status="done",
        result=None,
    )
    failed = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan_id,
        entity_id=entity_id,
        step_key="render_video",
        kind="action",
        step_status="failed",
        error={"type": "ToolError", "message": "renderer timed out"},
    )
    db_session.add_all([plan, produced, side_effect, failed])
    await db_session.commit()

    replanned = await PlanExecutor._maybe_replan(
        db_session, plan, [produced, side_effect, failed], reason="step_failed",
    )
    await db_session.flush()

    assert replanned is True
    task = await db_session.get(Task, task_id)
    assert task is not None
    succeeded = task.details["_replan_context"]["succeeded_steps"]
    by_key = {e["step_key"]: e for e in succeeded}
    assert set(by_key) == {"write_script", "notify_team"}
    assert by_key["write_script"]["artifacts"][0]["fs_path"] == "workspace/artifacts/script.md"
    assert by_key["notify_team"]["no_output"] is True


@pytest.mark.asyncio
async def test_second_replan_preserves_earlier_completed_artifact(db_session, monkeypatch) -> None:
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.task import Task
    from packages.core.plans.executor import PlanExecutor

    monkeypatch.setattr(
        "packages.core.tasks.ai_tasks.plan_and_run_task.delay",
        lambda task_id: None,
    )

    entity_id = generate_ulid()
    task_id = generate_ulid()
    earlier_plan_id = generate_ulid()
    current_plan_id = generate_ulid()
    db_session.add(Task(
        id=task_id,
        entity_id=entity_id,
        title="Verify a release matrix",
        status="in_progress",
        priority=3,
        task_type="general",
        details={"_replan_context": {
            "prior_plan_id": earlier_plan_id,
            "succeeded_steps": [{
                "step_key": "save_matrix",
                "kind": "subagent",
                "artifacts": [{"type": "document", "document_id": "matrix-doc"}],
            }],
        }},
    ))
    db_session.add(ExecutionPlan(
        id=earlier_plan_id,
        entity_id=entity_id,
        task_id=task_id,
        status="replanned",
        plan_dag={},
    ))
    current_plan = ExecutionPlan(
        id=current_plan_id,
        entity_id=entity_id,
        task_id=task_id,
        status="running",
        plan_dag={},
    )
    failed = ExecutionStep(
        id=generate_ulid(),
        plan_id=current_plan_id,
        entity_id=entity_id,
        step_key="return_result",
        kind="subagent",
        step_status="failed",
        error={"type": "StepResultFailed", "message": "output unavailable"},
    )
    db_session.add_all([current_plan, failed])
    await db_session.commit()

    assert await PlanExecutor._maybe_replan(
        db_session, current_plan, [failed], reason="step_failure",
    ) is True
    task = await db_session.get(Task, task_id)
    assert task.details["_replan_context"]["succeeded_steps"] == [{
        "step_key": "save_matrix",
        "kind": "subagent",
        "artifacts": [{"type": "document", "document_id": "matrix-doc"}],
    }]
