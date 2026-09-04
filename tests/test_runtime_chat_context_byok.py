from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_runtime_chat_context_resolves_active_tenant_llm_route(monkeypatch):
    from packages.core.ai.runtime.prompt_adapter import ChatContext
    from packages.core.services import runtime_chat_context as module

    db_marker = object()
    seen: dict[str, dict] = {}

    async def fake_resolve_workspace_runtime(*_args, **kwargs):
        return SimpleNamespace(
            workspace_id=kwargs.get("workspace_id"),
            tool_profile=None,
            is_master=False,
            task_id=None,
            thread_ref_kind=None,
            thread_ref_id=None,
            bound_tool_names=[],
            mcp_allowed_names=set(),
            extra_context=None,
        )

    async def fake_assemble_prompt(_db, *, request, **_kwargs):
        ctx = ChatContext(
            db=_db,
            entity_id=request.entity_id,
            user_id=request.user_id,
            workspace_id=request.workspace_id,
            conversation_id=request.conversation_id,
        )
        ctx.entity = SimpleNamespace(
            id=request.entity_id,
            plan_id="plan_pro",
            settings={},
        )
        ctx.user = SimpleNamespace(
            id=request.user_id,
            role="member",
            preferences={
                "models": {"primary": "anthropic/claude-sonnet-4.6"},
            },
        )
        return SimpleNamespace(
            context=ctx,
            tool_schemas=[],
            prompt="system prompt",
        )

    async def fake_resolve_model(role, *, user_id=None, entity_id=None, db=None):
        seen["model"] = {
            "role": role,
            "user_id": user_id,
            "entity_id": entity_id,
            "db": db,
        }
        return "openai/gpt-5.5"

    async def fake_resolve_metadata(role, *, user_id=None, entity_id=None, db=None):
        seen["metadata"] = {
            "role": role,
            "user_id": user_id,
            "entity_id": entity_id,
            "db": db,
        }
        return {
            "llm_api_key": "sk-test-owner-key-1234567890",
            "llm_base_url": "https://apitokengate.com/v1",
        }

    monkeypatch.setattr(
        "packages.core.services.workspace_runtime.resolve_workspace_runtime",
        fake_resolve_workspace_runtime,
    )
    monkeypatch.setattr(module, "runtime_assemble_prompt_for_turn", fake_assemble_prompt)

    async def fake_auto_skill_forced_tool_calls(*_args, **_kwargs):
        return []

    monkeypatch.setattr(module, "runtime_auto_skill_forced_tool_calls", fake_auto_skill_forced_tool_calls)
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_model_for_user",
        fake_resolve_model,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        fake_resolve_metadata,
    )

    _prompt, _tools, _history, ctx = await module.resolve_runtime_chat_context(
        db_marker,
        "hello",
        entity_id="ent_active",
        user_id="member_user",
        workspace_id="ws_active",
        conversation_id=None,
    )

    assert getattr(ctx, "model", None) == "openai/gpt-5.5"
    assert getattr(ctx, "llm_metadata", None) == {
        "llm_api_key": "sk-test-owner-key-1234567890",
        "llm_base_url": "https://apitokengate.com/v1",
        "_resolved_model": "openai/gpt-5.5",
    }
    assert seen == {
        "model": {
            "role": "primary",
            "user_id": "member_user",
            "entity_id": "ent_active",
            "db": db_marker,
        },
        "metadata": {
            "role": "primary",
            "user_id": "member_user",
            "entity_id": "ent_active",
            "db": db_marker,
        },
    }


@pytest.mark.asyncio
async def test_workspace_ledger_tool_scope_keeps_the_tenant_byok_route(monkeypatch):
    from packages.core.ai.runtime.prompt_adapter import ChatContext
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.services import runtime_chat_context as module

    ledger_tools = {
        "read_finance_ledger",
        "query_ledger",
        "visualize_workspace_ledgers",
    }

    async def fake_resolve_workspace_runtime(*_args, **kwargs):
        assert kwargs["workspace_id"] == "ws_ledger"
        assert kwargs["agent_id"] == "agent_finance"
        return SimpleNamespace(
            workspace_id="ws_ledger",
            tool_profile="workspace_agent",
            is_master=False,
            task_id=None,
            thread_ref_kind=None,
            thread_ref_id=None,
            bound_tool_names=ledger_tools,
            mcp_allowed_names=set(),
            extra_context=None,
        )

    async def fake_assemble_prompt(_db, *, request, **kwargs):
        assert kwargs["bound_tool_names"] == ledger_tools
        assert kwargs["visible_tool_names"] == (
            "read_content_ledger",
            "read_finance_ledger",
            "read_recruiting_ledger",
            "read_relationship_ledger",
            "query_ledger",
            "visualize_workspace_ledgers",
        )
        assert request.metadata["turn_execution_plan"]["tool_catalog_mode"] == (
            "ledger_query"
        )
        ctx = ChatContext(
            db=_db,
            entity_id=request.entity_id,
            user_id=request.user_id,
            agent_id=request.agent_id,
            workspace_id=request.workspace_id,
        )
        ctx.agent = SimpleNamespace(config={"model_mode": "inherit"})
        return SimpleNamespace(
            context=ctx,
            tool_schemas=[],
            prompt="system prompt",
        )

    async def fake_resolve_model(role, **kwargs):
        assert role == "primary"
        assert kwargs["user_id"] == "user_owner"
        assert kwargs["entity_id"] == "entity_owner"
        return "deepseek/deepseek-chat"

    async def fake_resolve_metadata(role, **kwargs):
        assert role == "primary"
        assert kwargs["user_id"] == "user_owner"
        assert kwargs["entity_id"] == "entity_owner"
        return {
            "llm_api_key": "sk-test-native-key-1234567890",
            "llm_base_url": "https://api.deepseek.com/v1",
        }

    async def fake_auto_skill_forced_tool_calls(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "packages.core.services.workspace_runtime.resolve_workspace_runtime",
        fake_resolve_workspace_runtime,
    )
    monkeypatch.setattr(module, "runtime_assemble_prompt_for_turn", fake_assemble_prompt)
    monkeypatch.setattr(
        module,
        "runtime_auto_skill_forced_tool_calls",
        fake_auto_skill_forced_tool_calls,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_model_for_user",
        fake_resolve_model,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        fake_resolve_metadata,
    )

    _prompt, _tools, _history, ctx = await module.resolve_runtime_chat_context(
        object(),
        "花了多少钱了",
        entity_id="entity_owner",
        user_id="user_owner",
        agent_id="agent_finance",
        workspace_id="ws_ledger",
        runtime_surface=ChatSurface.WORKSPACE_CHAT,
    )

    assert ctx.model == "deepseek/deepseek-chat"
    assert ctx.llm_metadata == {
        "llm_api_key": "sk-test-native-key-1234567890",
        "llm_base_url": "https://api.deepseek.com/v1",
        "_resolved_model": "deepseek/deepseek-chat",
    }


@pytest.mark.asyncio
async def test_runtime_chat_context_prefers_fixed_agent_config(monkeypatch):
    from packages.core.ai.runtime.prompt_adapter import ChatContext
    from packages.core.services import runtime_chat_context as module

    async def fake_resolve_workspace_runtime(*_args, **kwargs):
        return SimpleNamespace(
            workspace_id=kwargs.get("workspace_id"),
            tool_profile=None,
            is_master=False,
            task_id=None,
            thread_ref_kind=None,
            thread_ref_id=None,
            bound_tool_names=[],
            mcp_allowed_names=set(),
            extra_context=None,
        )

    async def fake_assemble_prompt(_db, *, request, **_kwargs):
        ctx = ChatContext(
            db=_db,
            entity_id=request.entity_id,
            user_id=request.user_id,
            agent_id=request.agent_id,
        )
        ctx.agent = SimpleNamespace(
            config={
                "model_mode": "fixed",
                "model": "openai/gpt-5.6-terra",
                "temperature": 0.15,
                "max_tokens": 6144,
            }
        )
        return SimpleNamespace(
            context=ctx,
            tool_schemas=[],
            prompt="system prompt",
        )

    async def fake_resolve_model(*_args, **_kwargs):
        return "anthropic/claude-sonnet-4.6"

    async def fake_resolve_metadata(*_args, **_kwargs):
        return {"llm_api_key": "sk-test-owner-key-1234567890"}

    async def fake_auto_skill_forced_tool_calls(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "packages.core.services.workspace_runtime.resolve_workspace_runtime",
        fake_resolve_workspace_runtime,
    )
    monkeypatch.setattr(module, "runtime_assemble_prompt_for_turn", fake_assemble_prompt)
    monkeypatch.setattr(
        module,
        "runtime_auto_skill_forced_tool_calls",
        fake_auto_skill_forced_tool_calls,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_model_for_user",
        fake_resolve_model,
    )
    monkeypatch.setattr(
        "packages.core.services.model_resolver.resolve_llm_metadata_for_user",
        fake_resolve_metadata,
    )

    _prompt, _tools, _history, ctx = await module.resolve_runtime_chat_context(
        object(),
        "hello",
        entity_id="ent_active",
        user_id="member_user",
        agent_id="agent_fixed",
    )

    assert ctx.model == "openai/gpt-5.6-terra"
    assert ctx.temperature == 0.15
    assert ctx.max_tokens == 6144
    assert ctx.llm_metadata == {
        "llm_api_key": "sk-test-owner-key-1234567890",
        "_resolved_model": "openai/gpt-5.6-terra",
    }


@pytest.mark.asyncio
async def test_runtime_chat_context_adds_turn_scoped_extra_tools(monkeypatch):
    from packages.core.ai.runtime.prompt_adapter import ChatContext
    from packages.core.services import runtime_chat_context as module

    db_marker = object()
    captured: dict = {}
    dashboard_schema = {
        "type": "function",
        "function": {
            "name": "dashboard_submit_module",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    async def fake_resolve_workspace_runtime(*_args, **kwargs):
        return SimpleNamespace(
            workspace_id=kwargs.get("workspace_id"),
            tool_profile=None,
            is_master=False,
            task_id=None,
            thread_ref_kind=None,
            thread_ref_id=None,
            bound_tool_names=[],
            mcp_allowed_names=set(),
            extra_context=None,
        )

    async def fake_assemble_prompt(_db, *, request, **kwargs):
        captured.update(kwargs)
        ctx = ChatContext(
            db=_db,
            entity_id=request.entity_id,
            user_id=request.user_id,
        )
        return SimpleNamespace(
            context=ctx,
            tool_schemas=[dashboard_schema],
            prompt="system prompt",
        )

    monkeypatch.setattr(
        "packages.core.services.workspace_runtime.resolve_workspace_runtime",
        fake_resolve_workspace_runtime,
    )
    monkeypatch.setattr(module, "runtime_assemble_prompt_for_turn", fake_assemble_prompt)
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_registry.runtime_tool_schemas_for_names",
        lambda names: [dashboard_schema] if names == ["dashboard_submit_module"] else [],
    )
    async def fake_auto_skill_forced_tool_calls(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        module,
        "runtime_auto_skill_forced_tool_calls",
        fake_auto_skill_forced_tool_calls,
    )

    _prompt, tools, _history, _ctx = await module.resolve_runtime_chat_context(
        db_marker,
        "Build my dashboard",
        entity_id="ent_dashboard",
        user_id="user_dashboard",
        runtime_metadata={"extra_tool_names": ["dashboard_submit_module"]},
    )

    assert tools == [dashboard_schema]
    assert captured["extra_tool_schemas"] == [dashboard_schema]
    assert captured["extra_allowed_tool_names"] == {"dashboard_submit_module"}
