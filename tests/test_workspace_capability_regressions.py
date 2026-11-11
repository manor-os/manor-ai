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

from packages.core.ai.runtime.tool_context import RUNTIME_TOOL_CONTEXT_KEYS
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
        **dict.fromkeys(RUNTIME_TOOL_CONTEXT_KEYS, "from-runtime"),
    }
    result = json.loads(await executor("ws_request_custom_agent", {**design, **legacy}))
    assert result["ok"] is False
    handler.assert_not_awaited()
    # Internal Runtime provenance must not make a valid design invalid.
    assert json.loads(await executor("ws_request_custom_agent", design))["ok"] is True
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_custom_agent_design_reaches_its_tool_through_a_real_architect_loop(monkeypatch):
    """The loop adds its own run identity to every call; none of it may fail the design."""
    import importlib

    from packages.core.ai.runtime import billing, completions, runtime_execute_workspace_architect_loop
    from packages.core.ai.runtime.workspace_drafts import runtime_workspace_architect_tool_executor

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    design = {
        "draft_id": "draft", "service_key": "care", "agent_name": "Customer Care",
        "system_prompt": "Answer guests about the menu, hours and policies, and hand over what needs a person. " * 2,
    }
    replies = iter([("", [{"id": "call_1", "name": "ws_request_custom_agent", "arguments": design}]),
                    ("done", None)])
    seen_results = []

    async def completion(messages, tools=None, **_):
        seen_results.extend(m["content"] for m in messages if m.get("role") == "tool")
        text, calls = next(replies)
        return text, calls, {"prompt": 1, "completion": 1, "total": 2}

    async def text(_messages, **_):
        return "done", {"prompt": 1, "completion": 1, "total": 2}

    async def route(**_):
        return None, {}, None

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", completion)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_text_completion", text)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_final_completion", text)
    monkeypatch.setattr(completions, "runtime_resolve_text_completion_route", route)
    monkeypatch.setattr(billing, "runtime_ensure_billing_context", lambda *_a, **_k: None)
    handler = AsyncMock(return_value='{"ok": true}')
    monkeypatch.setitem(workspace_arch_tools.HANDLERS, "ws_request_custom_agent", handler)

    await runtime_execute_workspace_architect_loop(
        runtime_envelope=None, system_prompt="s", user_message="u",
        tools=[workspace_arch_tools.REQUEST_CUSTOM_AGENT_SCHEMA],
        entity_id="entity", agent_id=None, max_rounds=3,
        tool_executor=runtime_workspace_architect_tool_executor(
            AsyncMock(), draft_id="draft", entity_id="entity", user_id="user",
        ),
    )

    handler.assert_awaited_once()
    assert handler.await_args.kwargs["agent_name"] == "Customer Care"
    assert handler.await_args.kwargs["_performance_run_id_from_context"]
    assert seen_results == ['{"ok": true}']


@pytest.mark.asyncio
async def test_a_crashed_architect_tool_keeps_its_cause_from_the_model(monkeypatch):
    from packages.core.ai.runtime.workspace_drafts import runtime_workspace_architect_tool_executor

    handler = AsyncMock(side_effect=RuntimeError("connection to 10.0.0.5 refused"))
    monkeypatch.setitem(workspace_arch_tools.HANDLERS, "ws_get_draft", handler)
    executor = runtime_workspace_architect_tool_executor(
        AsyncMock(), draft_id="draft", entity_id="entity", user_id="user",
    )

    result = await executor("ws_get_draft", {})

    assert json.loads(result)["ok"] is False
    assert "10.0.0.5" not in result


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
    # The user's message stays for the turn to be run again; the stale turn records no reply.
    assert not any(message.get("role") == "assistant" for message in draft.messages)


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


@pytest.mark.asyncio
async def test_capability_search_reuses_the_stored_plan_for_an_unchanged_request(
    db_session, monkeypatch,
):
    """Regression (2026-11-01): a later Architect turn searched an unchanged
    service again and paid for a second full catalog match."""
    draft, catalog = await _seed_draft(db_session, monkeypatch)
    match = AsyncMock(return_value=catalog.resolve(["tool:test_0"]))
    monkeypatch.setattr(agent_generator, "match_agent_capabilities", match)

    async def search(**kwargs):
        result = json.loads(await workspace_arch_tools._search_capabilities(
            db_session, draft_id=draft.id, entity_id=draft.entity_id,
            user_id=draft.user_id, service_key="email", **kwargs,
        ))
        await db_session.commit()
        return result

    first = await search(intent="Read the inbox")
    again = await search(intent="Read the inbox")

    assert match.await_count == 1
    assert (first["plan_reused"], again["plan_reused"]) == (False, True)
    assert again["agent_capability_plan"] == first["agent_capability_plan"]

    match.return_value = catalog.resolve(["tool:test_1"])
    changed = await search(intent="Read and label the inbox")

    assert match.await_count == 2
    assert changed["plan_reused"] is False
    await db_session.refresh(draft)
    [stored] = draft.fields["agent_capability_plans"]
    assert stored["capability_ids"] == ["tool:test_1"]


@pytest.mark.asyncio
async def test_concurrent_architect_turns_finish_when_the_capability_provider_stalls(
    db_session, monkeypatch,
):
    """Regression (2026-10-27): three Create Workspace drafts started together
    stopped inside ws_search_capabilities. Capability selection had no overall
    deadline, so turns waiting on an unresponsive provider never ended and
    kept their chat stream slots. Each turn must get the selection error back
    as a tool result, finish, and release its claim on the draft."""
    import packages.core.ai.agentic_loop as loop_module
    from packages.core.ai.runtime.workspace_drafts import (
        runtime_workspace_architect_tool_executor,
    )
    from packages.core.services import workspace_draft_service

    monkeypatch.setattr(
        AgentCapabilityCatalogFactory, "create", AsyncMock(return_value=_catalog()),
    )
    monkeypatch.setattr(workspace_arch_tools, "_list_nango_aggregator", AsyncMock(return_value=None))
    provider_calls = 0

    async def unresponsive_provider(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        await asyncio.Event().wait()

    monkeypatch.setattr(agent_generator, "runtime_execute_text_completion", unresponsive_provider)
    monkeypatch.delenv("AGENT_CAPABILITY_MATCH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(agent_generator, "_CAPABILITY_MATCH_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(loop_module, "_preflight_credit_check", AsyncMock())
    monkeypatch.setattr(
        loop_module, "runtime_execute_agentic_final_completion",
        AsyncMock(return_value=("Capability matching is unavailable right now.", {})),
    )
    monkeypatch.setattr(workspace_draft_service, "_refresh_missing_from_lint", AsyncMock())
    search_results: dict[str, dict] = {}

    async def architect(db, *, draft, turn, **_kwargs):
        executor = runtime_workspace_architect_tool_executor(
            db, draft_id=draft.id, entity_id=draft.entity_id, user_id=draft.user_id, turn=turn,
        )

        async def recording_executor(name, args):
            result = await executor(name, args)
            search_results[draft.id] = json.loads(result)
            return result

        result = await loop_module.agentic_loop(
            system_prompt="system", user_message="build", tools=[], max_rounds=1,
            tool_executor=recording_executor,
            forced_tool_calls=[
                {"name": "ws_search_capabilities", "arguments": {"service_key": "email"}},
            ],
        )
        return result.content

    monkeypatch.setattr(workspace_draft_service, "_architect_turn", architect)
    entity_id, user_id = generate_ulid(), generate_ulid()
    drafts = [
        WorkspaceDraft(
            entity_id=entity_id, user_id=user_id, status="active",
            fields={"services": [{"service_key": "email", "name": "Email"}]},
            messages=[], missing=[], ready=False,
        )
        for _ in range(3)
    ]
    db_session.add_all(drafts)
    await db_session.commit()
    draft_ids = [draft.id for draft in drafts]

    async def turn(draft_id):
        async with AsyncSession(db_session.bind, expire_on_commit=False) as db:
            reply, _ = await workspace_draft_service.process_draft_message(
                db, draft_id=draft_id, entity_id=entity_id,
                user_id=user_id, user_message="Answer customer email",
            )
            await db.commit()
            return reply

    replies = await asyncio.wait_for(
        asyncio.gather(*(turn(draft_id) for draft_id in draft_ids)), timeout=15,
    )

    assert replies == ["Capability matching is unavailable right now."] * 3
    assert provider_calls == 3
    for draft_id in draft_ids:
        assert search_results[draft_id]["ok"] is False
        assert "did not finish within" in search_results[draft_id]["error"]
    async with AsyncSession(db_session.bind) as other:
        rows = (await other.execute(
            select(WorkspaceDraft).where(WorkspaceDraft.id.in_(draft_ids))
        )).scalars().all()
    for row in rows:
        assert "_architect_turn" not in row.fields
        assert not row.fields.get("agent_capability_plans")
        assert row.messages[-1]["role"] == "assistant"
