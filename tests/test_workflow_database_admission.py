"""Workflow Skill lookup must yield admission before language and budget reads."""
from __future__ import annotations

import os
from decimal import Decimal

import pytest
from sqlalchemy import event

from packages.core import database
from packages.core.ai import llm_client
from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.database_runtime import DatabaseEngineKind
from packages.core.models.base import generate_ulid
from packages.core.models.skill import Skill
from packages.core.models.user import Entity, User
from packages.core.models.workspace import Workspace
from test_database_runtime import runtime_settings
from test_database_pgbouncer_container import pgbouncer_container  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [
    "direct",
    pytest.param("pgbouncer", marks=[
        pytest.mark.docker,
        pytest.mark.manual,
        pytest.mark.skipif(
            os.getenv("MANOR_PGBOUNCER_CONTAINER_TEST") != "1",
            reason="Set MANOR_PGBOUNCER_CONTAINER_TEST=1 for disposable PgBouncer",
        ),
    ]),
])
@pytest.mark.parametrize("admission_enabled,limit", [(True, 1), (True, 2), (False, 1)])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_workflow_skill_releases_admission_before_workspace_budget(
    db_session, monkeypatch, request, transport, admission_enabled, limit, exhausted,
):
    from conftest import TEST_DATABASE_URL
    from packages.core.ai.runtime import completions
    from packages.core.constants import plans

    entity_id, user_id, workspace_id = (generate_ulid() for _ in range(3))
    slug = f"admission-{generate_ulid().lower()}"
    db_session.add_all([
        Entity(id=entity_id, name="Admission regression"),
        User(id=user_id, entity_id=entity_id, email=f"{user_id}@example.test",
             password_hash="unused", role="owner", status="active", locale="fr"),
        Workspace(id=workspace_id, entity_id=entity_id, name="Admission regression",
                  monthly_budget_usd=Decimal("1"),
                  monthly_spent_usd=Decimal("1") if exhausted else Decimal("0")),
        Skill(id=generate_ulid(), name="Admission regression", slug=slug,
              entity_id=None, status="active", tools=[], config={}, system_prompt="Reply briefly."),
    ])
    await db_session.commit()

    url = TEST_DATABASE_URL
    if transport == "pgbouncer":
        from test_database_pgbouncer_container import _settings

        _, port = request.getfixturevalue("pgbouncer_container")
        url = _settings(port).DATABASE_URL
    settings = runtime_settings(
        url, DATABASE_POOL_MODE="pgbouncer", DATABASE_ADMISSION_LIMIT=limit,
        DATABASE_ADMISSION_ENABLED=admission_enabled,
        DATABASE_MAX_WAITERS=2, DATABASE_ADMISSION_TIMEOUT=0.2,
    )
    engine = database.create_database_engine(settings, kind=DatabaseEngineKind.WORKER)
    factory = database.create_session_factory(engine)
    runtime = engine.sync_engine._manor_database_runtime
    queries, provider_prompts = [], []

    def observe_query(_conn, _cursor, statement, _params, _context, _many):
        queries.append(statement)

    async def route(**kwargs):
        return "test-model", {}, False

    async def completion(messages, **kwargs):
        provider_prompts.append("\n".join(
            message["content"] for message in messages if message["role"] == "system"
        ))
        # The provider is the only execution stub; budget and locale stay real.
        return "done", {}

    event.listen(engine.sync_engine, "after_cursor_execute", observe_query)
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(database, "create_worker_session", lambda: factory)
    monkeypatch.setattr(completions, "runtime_resolve_text_completion_route", route)
    monkeypatch.setattr(
        "packages.core.ai.agentic_loop.runtime_execute_agentic_round_text_completion", completion,
    )
    # Exercise the always-enforced Workspace gate independently of tenant plans.
    monkeypatch.setattr(plans, "ai_credit_limits_enabled", lambda: False)
    billing_token = llm_client._billing_ctx_var.set(None)
    byok_token = llm_client._is_byok_call.set(False)
    try:
        async with factory() as db:
            result = await WorkflowRunner(session_factory=factory)._execute_agent_step(
                {"id": "intake", "type": "agent", "config": {
                    "skill": slug.replace("-", "_"), "input": "Run the admission regression.",
                }},
                {}, entity_id, user_id, {"workspace_id": workspace_id}, db,
            )
            assert not db.in_transaction()
        assert sum("lower(skills.slug)" in sql for sql in queries) == 2
        assert any("users.locale" in sql for sql in queries)
        assert any("FROM workspaces" in sql for sql in queries)
        if exhausted:
            assert result["status"] == "failed", result
            assert "budget" in result["error"].lower(), result
            assert provider_prompts == []
        else:
            assert result["status"] == "completed", result
            assert result["output"] == "done"
            assert len(provider_prompts) == 1
            assert "French (fr)" in provider_prompts[0]
        snapshot = runtime.snapshot()
        assert snapshot["rejected"] == (0 if admission_enabled else None)
        assert snapshot["peak_active_transactions"] == (1 if admission_enabled else None)
        assert snapshot["active_transactions"] == (0 if admission_enabled else None)
        assert snapshot["checked_out"] == 0
    finally:
        llm_client._billing_ctx_var.reset(billing_token)
        llm_client._is_byok_call.reset(byok_token)
        await engine.dispose()
