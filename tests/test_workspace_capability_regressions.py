"""Regression coverage for service matching and its Draft turn boundary."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.tools import workspace_arch_tools
from packages.core.models.base import generate_ulid
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services import agent_generator
from packages.core.services.agent_capability_catalog import (
    AgentCapabilityCandidate,
    AgentCapabilityCatalog,
    AgentCapabilityCatalogFactory,
    AgentCapabilityKind,
)


def _catalog() -> AgentCapabilityCatalog:
    return AgentCapabilityCatalog(candidates=tuple(
        AgentCapabilityCandidate(
            catalog_id=f"tool:test_{index}",
            kind=AgentCapabilityKind.TOOL,
            ref=f"test_{index}",
            name=f"Test {index}",
        )
        for index in range(4)
    ))


def test_service_capability_context_honors_persisted_rule_scope():
    rules = [
        {"rule_key": "email", "scope": "email", "description": "Read email only"},
        {"rule_key": "browser", "scope": "browser", "description": "Use Chrome"},
        {"rule_key": "global", "scope": "all", "description": "Require approval"},
        {"rule_key": "unscoped", "description": "No deletion"},
        {"rule_key": "legacy_email", "service_key": "email"},
        {"rule_key": "legacy_browser", "service_key": "browser"},
    ]
    context = workspace_arch_tools._agent_capability_context(
        {"rules": rules}, service={"service_key": "email"},
        service_key="email", intent="",
    )
    assert {rule["rule_key"] for rule in context["rules"]} == {
        "email", "global", "unscoped", "legacy_email",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["scan", "reduction"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_capability_batch_drains_siblings_before_return(monkeypatch, phase, cancel):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    active = 0

    async def completion(*_args, **_kwargs):
        nonlocal calls, active
        calls += 1
        call_number = calls
        active += 1
        entered.set()
        try:
            await release.wait()
            if call_number == 1:
                raise RuntimeError("provider failed")
            # Leave queued siblings enough time to leak if not cancelled.
            await asyncio.sleep(0.01)
            return SimpleNamespace(content='{"capability_ids": []}')
        finally:
            active -= 1

    monkeypatch.setattr(agent_generator, "runtime_execute_text_completion", completion)
    monkeypatch.setattr(agent_generator, "_CAPABILITY_SCAN_MAX_ITEMS", 1)
    monkeypatch.setattr(agent_generator, "_CAPABILITY_REVIEW_MAX_ITEMS", 1)
    if phase == "scan":
        operation = agent_generator._match_agent_capabilities(
            prompt="Read records", spec={}, entity_id="entity",
            capability_catalog=_catalog(),
        )
    else:
        operation = agent_generator._reduce_capability_shortlist(
            shortlist=_catalog().prompt_payload(), request_context="Read records",
            entity_id="entity", semaphore=asyncio.Semaphore(1),
            budget=agent_generator.AgentCapabilitySelectionPolicyFactory.create(
                scan_chunk_count=4,
            ),
        )
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if cancel:
            task.cancel()
        else:
            release.set()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await task
        calls_at_return = calls
        active_at_return = active
        release.set()
        await asyncio.sleep(0.08)
        assert active_at_return == 0
        assert calls == calls_at_return
        assert calls < 4
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [
    {"tool_bindings": ["web_search"]},
    {"business_capabilities": ["web.safe_search"]},
    {"skill_bindings": ["invented"]},
    {"mcp_bindings": ["chrome"]},
    {"capability_ids": ["tool:web_search"]},
])
async def test_architect_runtime_rejects_legacy_binding_arguments(monkeypatch, legacy):
    from packages.core.ai.runtime.workspace_drafts import (
        runtime_workspace_architect_tool_executor,
    )

    handler = AsyncMock(return_value='{"ok": true}')
    monkeypatch.setitem(workspace_arch_tools.HANDLERS, "ws_request_custom_agent", handler)
    executor = runtime_workspace_architect_tool_executor(
        AsyncMock(), draft_id="draft", entity_id="entity", user_id="user",
    )
    design = {
        "service_key": "email", "agent_name": "Email Agent",
        "system_prompt": "Read email and prepare unsent drafts. " * 4,
        "_runtime_run_id_from_context": "run",
        "_runtime_tool_call_id_from_context": "call",
        "_runtime_tool_attempt_from_context": 1,
    }
    result = json.loads(await executor("ws_request_custom_agent", {**design, **legacy}))
    assert result["ok"] is False
    handler.assert_not_awaited()
    # Internal Runtime provenance must not make a valid design invalid.
    assert json.loads(await executor("ws_request_custom_agent", design))["ok"] is True
    handler.assert_awaited_once()


async def _seed_draft(db_session, monkeypatch):
    catalog = _catalog()
    monkeypatch.setattr(
        AgentCapabilityCatalogFactory, "create", AsyncMock(return_value=catalog),
    )
    monkeypatch.setattr(workspace_arch_tools, "_list_nango_aggregator", AsyncMock(return_value=None))
    draft = WorkspaceDraft(
        entity_id=generate_ulid(), user_id=generate_ulid(), status="active",
        fields={"services": [{"service_key": "email", "name": "Email"}]},
        messages=[], missing=[], ready=False,
    )
    db_session.add(draft)
    await db_session.commit()
    return draft, catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["fields", "messages", "status"])
@pytest.mark.parametrize("provider_fails", [False, True])
async def test_concurrent_draft_change_aborts_old_turn_before_later_tools(
    db_session, monkeypatch, change, provider_fails,
):
    import packages.core.ai.agentic_loop as loop_module
    from packages.core.ai.runtime.workspace_drafts import (
        runtime_workspace_architect_tool_executor,
    )
    from packages.core.services import workspace_draft_service

    draft, catalog = await _seed_draft(db_session, monkeypatch)
    draft_id, entity_id, user_id = draft.id, draft.entity_id, draft.user_id

    async def match(**_kwargs):
        assert not db_session.in_transaction()
        async with AsyncSession(db_session.bind, expire_on_commit=False) as other:
            newer = await other.get(WorkspaceDraft, draft_id)
            if change == "fields":
                newer.fields = {**newer.fields, "name": "Newer draft"}
            elif change == "messages":
                newer.messages = [{"role": "user", "content": "Newer turn"}]
            else:
                newer.status = "abandoned"
            await other.commit()
        if provider_fails:
            raise RuntimeError("provider failed")
        return catalog.resolve(["tool:test_0"])

    later_tool = AsyncMock(return_value='{"ok": true}')
    monkeypatch.setattr(agent_generator, "match_agent_capabilities", match)
    monkeypatch.setitem(workspace_arch_tools.HANDLERS, "ws_commit_basics", later_tool)
    final_completion = AsyncMock(return_value=("stale reply", {}))
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_final_completion", final_completion)
    monkeypatch.setattr(loop_module, "_preflight_credit_check", AsyncMock())

    async def architect(db, **_kwargs):
        result = await loop_module.agentic_loop(
            system_prompt="system", user_message="old turn", tools=[], max_rounds=1,
            tool_executor=runtime_workspace_architect_tool_executor(
                db, draft_id=draft_id, entity_id=entity_id, user_id=user_id,
            ),
            forced_tool_calls=[
                {"name": "ws_search_capabilities", "arguments": {"service_key": "email"}},
                {"name": "ws_commit_basics", "arguments": {"name": "Stale overwrite"}},
            ],
        )
        return result.content

    monkeypatch.setattr(workspace_draft_service, "_architect_turn", architect)
    lint = AsyncMock()
    monkeypatch.setattr(workspace_draft_service, "_refresh_missing_from_lint", lint)
    with pytest.raises(ValueError, match="draft changed"):
        await workspace_draft_service.process_draft_message(
            db_session, draft_id=draft_id, entity_id=entity_id,
            user_id=user_id, user_message="old turn",
        )
    later_tool.assert_not_awaited()
    final_completion.assert_not_awaited()
    lint.assert_not_awaited()
    await db_session.refresh(draft)
    assert "agent_capability_plans" not in draft.fields
    assert not any(message.get("content") == "old turn" for message in draft.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails", [False, True])
async def test_capability_search_reacquires_turn_lock_until_caller_finishes(
    db_session, monkeypatch, provider_fails,
):
    draft, catalog = await _seed_draft(db_session, monkeypatch)
    draft_id = draft.id

    async def match(**_kwargs):
        assert not db_session.in_transaction()
        if provider_fails:
            raise RuntimeError("provider failed")
        return catalog.resolve(["tool:test_0"])

    monkeypatch.setattr(agent_generator, "match_agent_capabilities", match)
    call = workspace_arch_tools._search_capabilities(
        db_session, draft_id=draft_id, entity_id=draft.entity_id,
        user_id=draft.user_id, service_key="email",
    )
    if provider_fails:
        with pytest.raises(RuntimeError, match="provider failed"):
            await call
    else:
        assert json.loads(await call)["ok"] is True
    assert db_session.in_transaction()
    async with AsyncSession(db_session.bind) as other:
        with pytest.raises(DBAPIError) as caught:
            await other.execute(
                select(WorkspaceDraft).where(WorkspaceDraft.id == draft_id)
                .with_for_update(nowait=True)
            )
        assert caught.value.orig.sqlstate == "55P03"  # lock_not_available
    await db_session.commit()
    async with AsyncSession(db_session.bind) as other:
        await other.execute(
            select(WorkspaceDraft).where(WorkspaceDraft.id == draft_id)
            .with_for_update(nowait=True)
        )
