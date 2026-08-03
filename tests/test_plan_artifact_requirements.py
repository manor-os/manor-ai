from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.plans import executor


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
    assert {"artifact_url", "download_url", "file_path"} <= sources
    assert any(ref.get("fs_path") == "Workspaces/Demo/reports/final.pdf" for ref in refs)


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

    assert executor._dedupe_task_artifact_refs(refs) == [refs[0]]


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
    from packages.core.plans.executor import PlanExecutor

    dispatched: list[str] = []
    monkeypatch.setattr(
        "packages.core.tasks.ai_tasks.plan_and_run_task.delay",
        lambda task_id: dispatched.append(task_id),
    )

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
    assert dispatched == [task_id]


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


# ── replan context: completed steps carry reusable handles ────────────


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
