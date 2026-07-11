from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.core.models.base import generate_ulid
from packages.core.models.worker import SubscriptionWorker, Worker
from packages.core.models.workspace import AgentSubscription, Workspace
from packages.core.workers.registry import INTERNAL_WORKER_KIND, ensure_internal_worker


def test_subagent_artifact_path_parser_prefers_save_target():
    from packages.core.workers.internal import (
        _schema_requires_materialized_artifact,
        _schema_requires_fs_path,
        _target_artifact_path_from_prompt,
    )

    prompt = (
        "读取输入文件：`workspace/social_ops/product-shortlist.md`\n"
        "将完整选题计划保存至 `Workspace Artifacts/topic_plan_next_week.md`。"
    )

    assert _target_artifact_path_from_prompt(prompt, "phase_a") == "Workspace Artifacts/topic_plan_next_week.md"
    assert _schema_requires_fs_path(
        {
            "type": "object",
            "required": ["fs_path"],
            "properties": {"fs_path": {"type": "string"}},
        }
    )
    assert _schema_requires_materialized_artifact(
        {
            "type": "object",
            "required": ["summary", "document_url"],
            "properties": {
                "summary": {"type": "string"},
                "document_url": {"type": "string", "description": "URL or fs_path to the saved research document"},
            },
        }
    )


@pytest.mark.asyncio
async def test_subagent_text_artifact_materializes_under_workspace_folder(db_session, monkeypatch):
    from packages.core.workers.internal import _persist_subagent_text_artifact

    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    db_session.add(
        Workspace(
            id=workspace_id,
            entity_id=entity_id,
            name="Social Ops",
        )
    )
    await db_session.commit()

    captured: dict[str, object] = {}

    def fake_write_entity_file_atomic(entity_arg, rel_path, data, **kwargs):
        captured["entity_id"] = entity_arg
        captured["rel_path"] = rel_path
        captured["data"] = data
        return f"/tmp/entity/{rel_path}"

    async def fake_sync_file_to_knowledge(**kwargs):
        captured["sync"] = kwargs
        return SimpleNamespace(document_id="doc_123")

    monkeypatch.setattr("packages.core.services.entity_fs.get_entity_root", lambda _entity_id: "/tmp/entity")
    monkeypatch.setattr(
        "packages.core.services.entity_fs.write_entity_file_atomic",
        fake_write_entity_file_atomic,
    )
    monkeypatch.setattr(
        "packages.core.services.knowledge_sync.sync_file_to_knowledge",
        fake_sync_file_to_knowledge,
    )

    result = await _persist_subagent_text_artifact(
        {
            "entity_id": entity_id,
            "workspace_id": workspace_id,
            "task_id": "task_1",
            "conversation_id": "conversation_1",
            "resolved_agent_id": "agent_1",
            "step_key": "draft_pack",
            "expected_output_schema": {
                "type": "object",
                "required": ["fs_path"],
                "properties": {"fs_path": {"type": "string"}},
            },
        },
        prompt="请生成草稿包并返回 fs_path。",
        content="# Draft pack\n\nReady.",
        result={"summary": "Ready."},
    )

    assert captured["rel_path"] == "Workspaces/Social Ops/artifacts/draft_pack.md"
    assert result["fs_path"] == "Workspaces/Social Ops/artifacts/draft_pack.md"
    assert result["files"][0]["fs_path"] == "Workspaces/Social Ops/artifacts/draft_pack.md"
    assert result["document_id"] == "doc_123"
    assert captured["sync"]["workspace_id"] == workspace_id


def test_plan_artifact_refs_are_deduped_per_step_and_path():
    from packages.core.plans.executor import _artifact_refs_from_result

    refs = _artifact_refs_from_result(
        {
            "fs_path": "Workspace Artifacts/x.md",
            "files": [{"type": "file", "fs_path": "Workspace Artifacts/x.md"}],
        },
        step_key="draft",
    )

    assert refs == [
        {
            "type": "file",
            "step": "draft",
            "source": "fs_path",
            "fs_path": "Workspace Artifacts/x.md",
        }
    ]


def test_plan_artifact_refs_include_generated_file_collections():
    from packages.core.plans.executor import _artifact_refs_from_result

    refs = _artifact_refs_from_result(
        {
            "files": [{"name": "draft-pack.md", "path": "workspace/social_ops/draft-pack.md"}],
            "summary": "Saved a Markdown content pack.",
            "draft_count": 5,
        },
        step_key="assemble_draft_pack",
    )

    assert refs == [
        {
            "type": "file",
            "step": "assemble_draft_pack",
            "source": "path",
            "fs_path": "workspace/social_ops/draft-pack.md",
        }
    ]


def test_plan_artifact_refs_ignore_reference_only_file_collections():
    from packages.core.plans.executor import _artifact_refs_from_result

    refs = _artifact_refs_from_result(
        {
            "context": "upstream inputs",
            "files": [{"name": "source.md", "path": "workspace/source.md"}],
        },
        step_key="load_context",
    )

    assert refs == []


def test_replan_parent_plan_id_is_preserved_from_task_details():
    from packages.core.plans.planner import _replan_parent_plan_id

    task = SimpleNamespace(
        details={
            "_replan_context": {
                "prior_plan_id": "01KRR10PY2AWF27WRQASB686GM",
            }
        }
    )

    assert _replan_parent_plan_id(task) == "01KRR10PY2AWF27WRQASB686GM"
    assert _replan_parent_plan_id(SimpleNamespace(details={})) is None


@pytest.mark.asyncio
async def test_ensure_internal_worker_backfills_subscription_bindings(db_session):
    entity_id = generate_ulid()
    first_sub_id = generate_ulid()
    second_sub_id = generate_ulid()

    db_session.add(
        AgentSubscription(
            id=first_sub_id,
            entity_id=entity_id,
            agent_id=generate_ulid(),
            workspace_id=generate_ulid(),
            service_key="xhs_research",
            status="active",
        )
    )
    await db_session.flush()

    worker = await ensure_internal_worker(db_session, entity_id)
    assert worker.kind == INTERNAL_WORKER_KIND
    assert worker.status == "active"

    first_binding = (
        await db_session.execute(
            select(SubscriptionWorker).where(
                SubscriptionWorker.worker_id == worker.id,
                SubscriptionWorker.subscription_id == first_sub_id,
            )
        )
    ).scalar_one_or_none()
    assert first_binding is not None

    db_session.add(
        AgentSubscription(
            id=second_sub_id,
            entity_id=entity_id,
            agent_id=generate_ulid(),
            workspace_id=generate_ulid(),
            service_key="x_account_ops",
            status="active",
        )
    )
    await db_session.flush()

    same_worker = await ensure_internal_worker(db_session, entity_id)
    assert same_worker.id == worker.id

    rows = list(
        (
            await db_session.execute(
                select(SubscriptionWorker.subscription_id).where(
                    SubscriptionWorker.worker_id == worker.id,
                    SubscriptionWorker.subscription_id.in_([first_sub_id, second_sub_id]),
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(rows) == {first_sub_id, second_sub_id}

    workers = list(
        (
            await db_session.execute(
                select(Worker).where(
                    Worker.entity_id == entity_id,
                    Worker.kind == INTERNAL_WORKER_KIND,
                )
            )
        )
        .scalars()
        .all()
    )
    assert [w.id for w in workers] == [worker.id]
