from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select


async def _register(client: AsyncClient, username: str = "learnuser") -> tuple[str, dict]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": "Learning Corp",
        },
    )
    token = resp.json()["access_token"]
    return token, {"Authorization": f"Bearer {token}"}


def test_runtime_evidence_response_normalizes_legacy_failed_plan_success():
    from apps.api.routers.workspaces import _runtime_evidence_response

    row = SimpleNamespace(
        id="ev_legacy",
        workspace_id="ws_legacy",
        agent_id=None,
        user_id=None,
        conversation_id=None,
        message_id=None,
        task_id="task_legacy",
        trace_id="plan_legacy",
        evidence_type="task_run",
        source="plan_executor",
        status="succeeded",
        summary="Task completed: Prepare social draft pack (OutputSchemaError)",
        details={"task_status": "completed", "plan_status": "failed"},
        metrics={"failed_steps": 2, "done_steps": 0, "artifact_count": 0},
        created_at=None,
    )

    response = _runtime_evidence_response(row)
    assert response.status == "failed"
    assert response.summary.startswith("Task failed:")


def test_runtime_evidence_response_normalizes_legacy_completed_plan_blocked():
    from apps.api.routers.workspaces import _runtime_evidence_response

    row = SimpleNamespace(
        id="ev_completed_blocked",
        workspace_id="ws_completed_blocked",
        agent_id=None,
        user_id=None,
        conversation_id=None,
        message_id=None,
        task_id="task_completed_blocked",
        trace_id="plan_completed_blocked",
        evidence_type="task_run",
        source="plan_executor",
        status="blocked",
        summary="Task waiting_on_customer: Prepare social draft pack",
        details={
            "task_title": "Prepare social draft pack",
            "task_status": "waiting_on_customer",
            "plan_status": "completed",
            "steps": [{"status": "done", "result_excerpt": '{"files":[{"name":"draft-pack.md"}]}'}],
        },
        metrics={"failed_steps": 0, "blocked_steps": 0, "done_steps": 3, "artifact_count": 0},
        created_at=None,
    )

    response = _runtime_evidence_response(row)
    assert response.status == "succeeded"
    assert response.summary == "Task completed: Prepare social draft pack"


@pytest.mark.asyncio
async def test_runtime_evidence_stores_long_external_reference_ids_in_details(client: AsyncClient):
    _, headers = await _register(client, username="externalids")
    ws_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "External ID WS"})
    assert ws_resp.status_code == 201
    workspace_id = ws_resp.json()["id"]

    import packages.core.database as db_module
    from packages.core.models.user import User
    from packages.core.services.runtime_learning import record_runtime_evidence

    long_conversation_id = "external-conversation-id-that-is-not-a-ulid"
    long_message_id = "external-message-id-that-is-not-a-ulid"
    long_trace_id = "trace-" + ("x" * 100)

    async with db_module.async_session() as db:
        user = (await db.execute(select(User).where(User.email == "externalids@test.com"))).scalar_one()
        row = await record_runtime_evidence(
            db,
            entity_id=user.entity_id,
            workspace_id=workspace_id,
            user_id=user.id,
            conversation_id=long_conversation_id,
            message_id=long_message_id,
            trace_id=long_trace_id,
            evidence_type="chat_run",
            source="qa",
            status="succeeded",
            summary="External ids should not break evidence writes.",
        )
        assert row.conversation_id is None
        assert row.message_id is None
        assert row.trace_id is not None
        assert row.trace_id.startswith("sha256:")
        assert len(row.trace_id) <= 64
        refs = row.details["external_reference_ids"]
        assert refs["conversation_id"] == long_conversation_id
        assert refs["message_id"] == long_message_id
        assert refs["trace_id"] == long_trace_id
        await db.commit()


@pytest.mark.asyncio
async def test_workspace_runtime_learning_can_be_disabled(client: AsyncClient):
    _, headers = await _register(client, username="workspacelearnoff")
    ws_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Learning Off WS"})
    assert ws_resp.status_code == 201
    workspace_id = ws_resp.json()["id"]

    update_resp = await client.put(
        f"/api/v1/workspaces/{workspace_id}",
        headers=headers,
        json={"settings": {"runtime_learning": {"enabled": False}}},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["settings"]["runtime_learning"]["enabled"] is False

    import packages.core.database as db_module
    from packages.core.models.runtime_learning import AgentLearningCandidate, RuntimeEvidence
    from packages.core.models.user import User
    from packages.core.services.runtime_learning import record_chat_run_evidence

    async with db_module.async_session() as db:
        user = (await db.execute(select(User).where(User.email == "workspacelearnoff@test.com"))).scalar_one()
        evidence, candidates = await record_chat_run_evidence(
            db,
            entity_id=user.entity_id,
            user_id=user.id,
            workspace_id=workspace_id,
            conversation_id="conv_learning_off",
            message_id="msg_learning_off",
            trace_id="trace_learning_off",
            user_message="以后发 post 必须先给我审核批准。",
            assistant_content="我会先准备草稿。",
            tool_calls_made=["workspace_search", "generate_file"],
            rounds=1,
            stop_reason="completed",
        )
        assert evidence is None
        assert candidates == []
        evidence_rows = (
            (await db.execute(select(RuntimeEvidence).where(RuntimeEvidence.workspace_id == workspace_id)))
            .scalars()
            .all()
        )
        candidate_rows = (
            (
                await db.execute(
                    select(AgentLearningCandidate).where(AgentLearningCandidate.workspace_id == workspace_id)
                )
            )
            .scalars()
            .all()
        )
        assert evidence_rows == []
        assert candidate_rows == []


@pytest.mark.asyncio
async def test_agent_runtime_learning_can_be_disabled(client: AsyncClient):
    _, headers = await _register(client, username="agentlearnoff")
    ws_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Agent Learning Off WS"})
    assert ws_resp.status_code == 201
    workspace_id = ws_resp.json()["id"]

    agent_resp = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "Fixed Behavior Agent",
            "config": {"runtime_learning": {"enabled": False}},
        },
    )
    assert agent_resp.status_code == 201
    agent_id = agent_resp.json()["id"]
    assert agent_resp.json()["config"]["runtime_learning"]["enabled"] is False

    import packages.core.database as db_module
    from packages.core.models.runtime_learning import AgentLearningCandidate, RuntimeEvidence
    from packages.core.models.user import User
    from packages.core.services.runtime_learning import record_chat_run_evidence

    async with db_module.async_session() as db:
        user = (await db.execute(select(User).where(User.email == "agentlearnoff@test.com"))).scalar_one()
        evidence, candidates = await record_chat_run_evidence(
            db,
            entity_id=user.entity_id,
            user_id=user.id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            conversation_id="conv_agent_learning_off",
            message_id="msg_agent_learning_off",
            trace_id="trace_agent_learning_off",
            user_message="以后你是这个 workspace 的 lease consultant，负责先总结客户需求再推荐房源。",
            assistant_content="我会按这个身份处理租房咨询。",
            tool_calls_made=["workspace_search"],
            rounds=1,
            stop_reason="completed",
        )
        assert evidence is None
        assert candidates == []
        evidence_rows = (
            (await db.execute(select(RuntimeEvidence).where(RuntimeEvidence.agent_id == agent_id))).scalars().all()
        )
        candidate_rows = (
            (await db.execute(select(AgentLearningCandidate).where(AgentLearningCandidate.agent_id == agent_id)))
            .scalars()
            .all()
        )
        assert evidence_rows == []
        assert candidate_rows == []


@pytest.mark.asyncio
async def test_workspace_runtime_learning_endpoints(client: AsyncClient, tmp_path, monkeypatch):
    from packages.core.config import get_settings

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        _, headers = await _register(client)
        ws_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Learning WS"})
        assert ws_resp.status_code == 201
        workspace_id = ws_resp.json()["id"]

        import packages.core.database as db_module
        from packages.core.models.user import User
        from packages.core.services.runtime_learning import record_chat_run_evidence

        async with db_module.async_session() as db:
            user = (await db.execute(select(User).where(User.email == "learnuser@test.com"))).scalar_one()
            entity_id = user.entity_id
            user_id = user.id
            evidence, candidates = await record_chat_run_evidence(
                db,
                entity_id=user.entity_id,
                user_id=user.id,
                workspace_id=workspace_id,
                conversation_id="conv_123",
                message_id="msg_123",
                trace_id="trace_123",
                user_message="以后发 post 必须先给我审核批准。",
                assistant_content="我会先准备草稿。",
                tool_calls_made=["workspace_search", "generate_file"],
                tool_results=[{"name": "generate_file", "result": "draft created"}],
                usage={"total_tokens": 42, "model": "test-model"},
                rounds=1,
                stop_reason="completed",
            )
            assert evidence is not None
            assert candidates
            candidate_id = candidates[0].id
            await db.commit()

        evidence_resp = await client.get(f"/api/v1/workspaces/{workspace_id}/runtime/evidence", headers=headers)
        assert evidence_resp.status_code == 200
        evidence_rows = evidence_resp.json()
        assert evidence_rows[0]["trace_id"] == "trace_123"
        assert evidence_rows[0]["metrics"]["total_tokens"] == 42

        candidates_resp = await client.get(f"/api/v1/workspaces/{workspace_id}/learning-candidates", headers=headers)
        assert candidates_resp.status_code == 200
        candidate_rows = candidates_resp.json()
        assert any(row["id"] == candidate_id for row in candidate_rows)

        early_apply_resp = await client.post(
            f"/api/v1/workspaces/{workspace_id}/learning-candidates/{candidate_id}/apply",
            headers=headers,
        )
        assert early_apply_resp.status_code == 409

        resolve_resp = await client.post(
            f"/api/v1/workspaces/{workspace_id}/learning-candidates/{candidate_id}/resolve",
            headers=headers,
            json={"status": "accepted", "note": "durable"},
        )
        assert resolve_resp.status_code == 200
        assert resolve_resp.json()["status"] == "accepted"
        assert resolve_resp.json()["resolution"]["note"] == "durable"

        from packages.core.tasks import ai_tasks

        enqueued: list[dict] = []

        def _fake_apply_async(*, args=None, kwargs=None, countdown=None):
            enqueued.append({"args": args, "kwargs": kwargs, "countdown": countdown})

        monkeypatch.setattr(ai_tasks.apply_learning_candidate_async, "apply_async", _fake_apply_async)

        apply_resp = await client.post(
            f"/api/v1/workspaces/{workspace_id}/learning-candidates/{candidate_id}/apply",
            headers=headers,
        )
        assert apply_resp.status_code == 200
        queued = apply_resp.json()
        assert queued["status"] == "accepted"
        assert queued["applied_at"] is None
        assert queued["resolution"]["apply_status"] == "queued"
        assert enqueued == [
            {
                "args": [entity_id, candidate_id],
                "kwargs": {"workspace_id": workspace_id, "user_id": user_id},
                "countdown": 1,
            }
        ]

        from packages.core.services.runtime_learning import apply_queued_learning_candidate

        async with db_module.async_session() as db:
            from packages.core.models.runtime_learning import AgentLearningCandidate

            candidate = (
                await db.execute(select(AgentLearningCandidate).where(AgentLearningCandidate.id == candidate_id))
            ).scalar_one()
            candidate.resolution = {
                **(candidate.resolution or {}),
                "apply_status": "failed",
                "apply_error": "enqueue failed: redis unavailable",
                "apply_failed_at": "2026-05-17T00:00:00+00:00",
            }
            await db.flush()
            applied = await apply_queued_learning_candidate(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                user_id=user_id,
                candidate_id=candidate_id,
            )
            assert applied is not None
            assert applied.status == "applied"
            assert applied.applied_at is not None
            assert applied.resolution["applied_result"]["kind"] == "workspace_memory"
            assert "apply_error" not in applied.resolution
            assert "apply_failed_at" not in applied.resolution
            await db.commit()

        apply_evidence_resp = await client.get(
            f"/api/v1/workspaces/{workspace_id}/runtime/evidence?evidence_type=learning_apply",
            headers=headers,
        )
        assert apply_evidence_resp.status_code == 200
        assert apply_evidence_resp.json()[0]["details"]["candidate_id"] == candidate_id

        from packages.core.models.memory import AgentMemory
        from packages.core.memory.canonical import read_workspace_memory_file
        from packages.core.memory.service import record_memory

        async with db_module.async_session() as db:
            memory = (
                await db.execute(select(AgentMemory).where(AgentMemory.source == f"learning_candidate:{candidate_id}"))
            ).scalar_one()
            assert "审核" in memory.content
            assert memory.workspace_id == workspace_id
            rules = read_workspace_memory_file(memory.entity_id, workspace_id, "RULES.md") or ""
            assert "审核" in rules

            from packages.core.ai.runtime.prompt_adapter import (
                ChatContext,
                build_default_prompt_builder,
            )

            prompt = await build_default_prompt_builder().build(
                ChatContext(
                    entity_id=memory.entity_id,
                    workspace_id=workspace_id,
                    workspace=SimpleNamespace(name="Learning WS", kind="project"),
                    mode="full",
                )
            )
            assert "Workspace Operating Memory" in prompt
            assert "审核" in prompt

            await record_memory(
                db,
                entity_id=memory.entity_id,
                workspace_id=workspace_id,
                scope="learning",
                title="Batch similar leasing follow-ups",
                body="Follow-ups perform better when grouped by client urgency.",
                tags=["calibration", "auto"],
                source="test:record_memory",
                confidence=0.75,
            )
            await db.commit()
            learnings = read_workspace_memory_file(memory.entity_id, workspace_id, "LEARNINGS.md") or ""
            assert "Batch similar leasing follow-ups" in learnings
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_auto_apply_learning_candidate_waits_for_worker(client: AsyncClient, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        _, headers = await _register(client, username="autolearnuser")
        ws_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Auto Learning WS"})
        assert ws_resp.status_code == 201
        workspace_id = ws_resp.json()["id"]

        import packages.core.database as db_module
        from packages.core.models.user import User
        from packages.core.services.agent_files import effective_agent_id
        from packages.core.memory.canonical import read_workspace_agent_memory_file
        from packages.core.services.runtime_learning import (
            apply_queued_learning_candidate,
            queued_learning_candidate_ids,
            record_chat_run_evidence,
        )

        async with db_module.async_session() as db:
            user = (await db.execute(select(User).where(User.email == "autolearnuser@test.com"))).scalar_one()
            _evidence, candidates = await record_chat_run_evidence(
                db,
                entity_id=user.entity_id,
                user_id=user.id,
                workspace_id=workspace_id,
                conversation_id="conv_auto",
                message_id="msg_auto",
                trace_id="trace_auto",
                user_message="以后你是这个 workspace 的 lease consultant，负责先总结客户需求再推荐房源。",
                assistant_content="我会按这个身份处理租房咨询。",
                tool_calls_made=[],
                usage={"total_tokens": 24, "model": "test-model"},
                rounds=1,
                stop_reason="completed",
            )
            profile = next(c for c in candidates if c.candidate_type == "agent_profile_patch")
            assert profile.status == "accepted"
            assert profile.applied_at is None
            assert profile.resolution["apply_status"] == "queued"
            assert queued_learning_candidate_ids(candidates) == [profile.id]
            candidate_id = profile.id
            entity_id = user.entity_id
            user_id = user.id
            await db.commit()

        agent_key = effective_agent_id(None)
        before = read_workspace_agent_memory_file(entity_id, workspace_id, agent_key, "AGENT.md") or ""
        assert "lease consultant" not in before

        async with db_module.async_session() as db:
            applied = await apply_queued_learning_candidate(
                db,
                entity_id=entity_id,
                workspace_id=workspace_id,
                user_id=user_id,
                candidate_id=candidate_id,
            )
            assert applied is not None
            assert applied.status == "applied"
            await db.commit()

        after = read_workspace_agent_memory_file(entity_id, workspace_id, agent_key, "AGENT.md") or ""
        assert "lease consultant" in after
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_task_execution_evidence_records_task_and_step_learning(client: AsyncClient, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        _, headers = await _register(client, "tasklearnuser")
        ws_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Task Learning WS"})
        assert ws_resp.status_code == 201
        workspace_id = ws_resp.json()["id"]

        import packages.core.database as db_module
        from packages.core.models.user import User
        from packages.core.services.runtime_learning import record_task_execution_evidence

        async with db_module.async_session() as db:
            user = (await db.execute(select(User).where(User.email == "tasklearnuser@test.com"))).scalar_one()
            task_evidence, step_evidence, candidates = await record_task_execution_evidence(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                task_id="task_exec_learning",
                plan_id="plan_exec_learning",
                task_status="completed",
                plan_status="completed",
                task_title="Prepare leasing follow-up email",
                task_description="Draft a tour follow-up email for a renter.",
                owner_service_key="leasing",
                delegate_service_keys=[],
                steps=[
                    SimpleNamespace(
                        id="step_email",
                        step_key="draft_email",
                        kind="action",
                        step_status="done",
                        provider="gmail",
                        action_key="draft",
                        service_key="leasing",
                        resolved_agent_id=None,
                        result={"text": "Drafted the tour follow-up email."},
                        evidence_refs=[],
                        cost={"usd": 0.01},
                        error=None,
                    ),
                    SimpleNamespace(
                        id="step_polish",
                        step_key="polish_copy",
                        kind="llm",
                        step_status="done",
                        provider=None,
                        action_key=None,
                        service_key="leasing",
                        resolved_agent_id=None,
                        result={"text": "Polished the email tone."},
                        evidence_refs=[],
                        cost={"usd": 0.02, "llm_tokens_input": 120, "llm_tokens_output": 80},
                        error=None,
                    ),
                ],
                actual_output={
                    "steps": [
                        {"status": "done", "result_summary": "Drafted the tour follow-up email."},
                        {"status": "done", "result_summary": "Polished the email tone."},
                    ]
                },
                cost_tracking={"usd": 0.03},
            )
            assert task_evidence is not None
            assert task_evidence.evidence_type == "task_run"
            assert len(step_evidence) == 2
            assert any(c.candidate_type == "tool_experience" for c in candidates)
            await db.commit()

        task_evidence_resp = await client.get(
            f"/api/v1/workspaces/{workspace_id}/runtime/evidence?evidence_type=task_run",
            headers=headers,
        )
        assert task_evidence_resp.status_code == 200
        assert task_evidence_resp.json()[0]["details"]["tool_calls_made"] == ["gmail.draft", "llm"]

        step_evidence_resp = await client.get(
            f"/api/v1/workspaces/{workspace_id}/runtime/evidence?evidence_type=tool_summary",
            headers=headers,
        )
        assert step_evidence_resp.status_code == 200
        assert len(step_evidence_resp.json()) == 2

        candidates_resp = await client.get(
            f"/api/v1/workspaces/{workspace_id}/learning-candidates?candidate_type=tool_experience",
            headers=headers,
        )
        assert candidates_resp.status_code == 200
        assert candidates_resp.json()
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_task_execution_evidence_does_not_mark_failed_plan_as_success(client: AsyncClient, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        _, headers = await _register(client, "taskfaillearnuser")
        ws_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Task Failed Learning WS"})
        assert ws_resp.status_code == 201
        workspace_id = ws_resp.json()["id"]

        import packages.core.database as db_module
        from packages.core.models.user import User
        from packages.core.services.runtime_learning import record_task_execution_evidence

        async with db_module.async_session() as db:
            user = (await db.execute(select(User).where(User.email == "taskfaillearnuser@test.com"))).scalar_one()
            task_evidence, step_evidence, _ = await record_task_execution_evidence(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                task_id="task_exec_failed_learning",
                plan_id="plan_exec_failed_learning",
                task_status="completed",
                plan_status="failed",
                task_title="Prepare social draft pack",
                task_description="Generate a Markdown draft pack from predecessor outputs.",
                owner_service_key="social_ops",
                delegate_service_keys=["xhs_research", "x_account_ops"],
                steps=[
                    SimpleNamespace(
                        id="step_xhs",
                        step_key="xhs_draft_notes",
                        kind="subagent",
                        step_status="failed",
                        provider=None,
                        action_key=None,
                        service_key="xhs_research",
                        resolved_agent_id=None,
                        result=None,
                        evidence_refs=[],
                        cost={},
                        error={"type": "OutputSchemaError", "message": "missing required fields"},
                    ),
                    SimpleNamespace(
                        id="step_pack",
                        step_key="assemble_draft_pack",
                        kind="subagent",
                        step_status="skipped",
                        provider=None,
                        action_key=None,
                        service_key="x_account_ops",
                        resolved_agent_id=None,
                        result=None,
                        evidence_refs=[],
                        cost={},
                        error=None,
                    ),
                ],
                actual_output={
                    "plan_status": "failed",
                    "steps": [
                        {"status": "failed", "error": {"type": "OutputSchemaError"}},
                        {"status": "skipped"},
                    ],
                    "files": None,
                },
                cost_tracking={},
            )
            assert task_evidence is not None
            assert task_evidence.status == "failed"
            assert task_evidence.details["task_status"] == "completed"
            assert task_evidence.details["plan_status"] == "failed"
            assert task_evidence.metrics["failed_steps"] == 2
            assert all(row.status == "failed" for row in step_evidence)
            await db.commit()
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.mark.asyncio
async def test_task_execution_evidence_marks_completed_plan_with_artifact_as_success(client: AsyncClient, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        _, headers = await _register(client, "taskcompletedblockeduser")
        ws_resp = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Task Completed Evidence WS"})
        assert ws_resp.status_code == 201
        workspace_id = ws_resp.json()["id"]

        import packages.core.database as db_module
        from packages.core.models.user import User
        from packages.core.models.base import generate_ulid
        from packages.core.services.runtime_learning import record_task_execution_evidence

        async with db_module.async_session() as db:
            user = (
                await db.execute(select(User).where(User.email == "taskcompletedblockeduser@test.com"))
            ).scalar_one()
            task_id = generate_ulid()
            plan_id = generate_ulid()
            task_evidence, step_evidence, _ = await record_task_execution_evidence(
                db,
                entity_id=user.entity_id,
                workspace_id=workspace_id,
                task_id=task_id,
                plan_id=plan_id,
                task_status="waiting_on_customer",
                plan_status="completed",
                task_title="Prepare social draft pack",
                task_description="Generate a Markdown draft pack from predecessor outputs.",
                owner_service_key="social_ops",
                delegate_service_keys=["xhs_research", "x_account_ops"],
                steps=[
                    SimpleNamespace(
                        id="step_pack",
                        step_key="assemble_draft_pack",
                        kind="subagent",
                        step_status="done",
                        provider=None,
                        action_key=None,
                        service_key="x_account_ops",
                        resolved_agent_id=None,
                        result={"summary": "Draft pack assembled."},
                        evidence_refs=[],
                        cost={},
                        error=None,
                    ),
                ],
                actual_output={
                    "plan_status": "completed",
                    "files": [{"name": "draft-pack.md", "fs_path": "workspace/social_ops/draft-pack.md"}],
                },
                cost_tracking={},
            )
            assert task_evidence is not None
            assert task_evidence.status == "succeeded"
            assert task_evidence.summary.startswith("Task completed:")
            assert task_evidence.metrics["artifact_count"] == 1
            assert all(row.status == "succeeded" for row in step_evidence)
            await db.commit()

        evidence_resp = await client.get(
            f"/api/v1/workspaces/{workspace_id}/runtime/evidence?evidence_type=task_run",
            headers=headers,
        )
        assert evidence_resp.status_code == 200
        assert evidence_resp.json()[0]["status"] == "succeeded"
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled
