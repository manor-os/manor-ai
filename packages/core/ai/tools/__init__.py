"""
AI Tools package — OpenAI function-calling tool implementations.

Each sub-module exports get_tools() -> list[tuple[dict, callable]].
register_all_tools(pool) wires them all into a ToolPool.
"""
from __future__ import annotations


def register_all_tools(pool) -> None:
    """Register every tool module into the given ToolPool."""
    from . import (
        document_tools,
        rag_tools,
        web_tools,
        weather_tools,
        content_ledger_tools,
        finance_ledger_tools,
        recruiting_ledger_tools,
        relationship_ledger_tools,
        ledger_query_tools,
        youtube_tools,
        integration_route_tools,
        stats_tools,
        system_tools,
        bash_tool,
        file_tools,
        generate_file_tool,
        extended_tools,
        media_tools,
        recording_tools,
        creative_studio_tools,
        goal_tools,
        manor_tool,
        code_tool,
        code_project_tool,
        browser_tools,
        agent_file_tools,
        workspace_arch_tools,
        agent_provisioning_tools,
        agent_initiative_tools,
        notification_tools,
        response_surface_tools,
        dashboard_tools,
        workflow_tools,
        site_tools,
        video_edit_tools,
        request_input_tool,
        file_delivery_tools,
        memory_tools,
    )

    # request_input is always-loaded: any agent may need to stop and ask the
    # user mid-run, and the durable loop keys its resume on this exact name.
    _request_input_schema, _request_input_handler = request_input_tool.get_tools()[0]
    pool.register(_request_input_schema["function"]["name"], _request_input_schema, _request_input_handler)

    # invoke_skill is always-loaded since any agent may call skills at runtime.
    from . import skill_tools as _skill_mod
    _invoke_schema, _invoke_handler = _skill_mod.get_tools()[0]  # invoke_skill is first
    pool.register("invoke_skill", _invoke_schema, _invoke_handler)

    # Task + cron CRUD tools. Cron ones get elevated to always-loaded
    # for the master agent via Runtime Harness legacy surface rules — the
    # registration itself is just "available"; the allowlist set decides
    # visibility. Before that elevation, time-delayed user requests
    # ("send X in 10 minutes") silently hallucinated because the agent
    # couldn't see a scheduling tool in its top-level list.
    from . import task_tools, cron_tools
    for module in [task_tools, cron_tools]:
        for schema, handler in module.get_tools():
            pool.register(schema["function"]["name"], schema, handler)
    # Skill CRUD (skip invoke_skill which is already registered above)
    for schema, handler in _skill_mod.get_tools()[1:]:
        pool.register(schema["function"]["name"], schema, handler)

    # rag stays registered/executable for Workflow rag steps, but is not
    # discoverable while embeddings are paused and agents were mis-calling it.
    for schema, handler in rag_tools.get_tools():
        pool.register(
            schema["function"]["name"],
            schema,
            handler,
            discoverable=False,
        )

    for module in [
        document_tools,
        web_tools,
        weather_tools,
        content_ledger_tools,
        finance_ledger_tools,
        recruiting_ledger_tools,
        relationship_ledger_tools,
        ledger_query_tools,
        youtube_tools,
        integration_route_tools,
        stats_tools,
        system_tools,
        bash_tool,
        file_tools,
        generate_file_tool,
        extended_tools,
        media_tools,
        recording_tools,
        creative_studio_tools,
        goal_tools,
        manor_tool,
        code_tool,
        code_project_tool,
        browser_tools,
        agent_file_tools,
        workspace_arch_tools,
        agent_provisioning_tools,
        agent_initiative_tools,
        notification_tools,
        response_surface_tools,
        dashboard_tools,
        workflow_tools,
        site_tools,
        video_edit_tools,
        file_delivery_tools,
        memory_tools,
    ]:
        for schema, handler in module.get_tools():
            name = schema["function"]["name"]
            pool.register(
                name, schema, handler,
                guidance_skill=getattr(module, "TOOL_GUIDANCE", {}).get(name),
            )

    from . import resident_maintenance_tools
    for schema, handler in resident_maintenance_tools.get_tools():
        pool.register(schema["function"]["name"], schema, handler, discoverable=False)

    # Armed only on public webchat turns whose channel serves a live site.
    from . import site_content_tools
    for schema, handler in site_content_tools.get_tools():
        pool.register(schema["function"]["name"], schema, handler, discoverable=False)

    # Internal Workspace runtime operations are exposed through
    # manor(action="workspace"). Keep the historical names executable for
    # persisted task/tool calls, while leaving only the external least-
    # privilege ticket intake entry discoverable.
    from . import workspace_agent_tools
    for schema, handler in workspace_agent_tools.get_tools():
        name = schema["function"]["name"]
        pool.register(
            name,
            schema,
            handler,
            discoverable=name in {"workspace_create_task", "delegate_to_agent"},
        )

    # The aggregate Ledger renderer is now manor workspace action
    # visualize_ledgers. Retain its old execution name for resume safety.
    for schema, handler in ledger_query_tools.get_legacy_tools():
        pool.register(
            schema["function"]["name"],
            schema,
            handler,
            discoverable=False,
        )

    # Workspace context search — lets agents query workspace state
    from . import workspace_context_tool
    for schema, handler in workspace_context_tool.get_tools():
        pool.register(schema["function"]["name"], schema, handler)

    # Sandbox gateway — only registered when SANDBOX_SERVICE_URL is configured.
    # Historical action-specific names remain execution-only aliases.
    try:
        from . import sandbox_tools as _sandbox_mod
        for schema, handler in _sandbox_mod.get_tools():
            pool.register(schema["function"]["name"], schema, handler)
        for schema, handler in _sandbox_mod.get_legacy_tools():
            pool.register(
                schema["function"]["name"],
                schema,
                handler,
                discoverable=False,
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug("Sandbox tools not loaded: %s", e)


    # Built-in MCP tool catalog — 8 seeded servers, ~33 curated tools.
    # All registered as deferred so `search_tools` discovers them on demand
    # (same pattern Claude Code uses for MCP + MCPTool search).
    try:
        from . import mcp_builtin
        for schema, handler in mcp_builtin.get_tools():
            pool.register(
                schema["function"]["name"], schema, handler, deferred=True,
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "Built-in MCP tools not loaded: %s", e,
        )

    # Legacy: external HTTP MCP servers via MCP_SERVERS env var.
    # Kept for compatibility with deployments that run real MCP services.
    try:
        from . import mcp_tools
        for schema, handler in mcp_tools.get_tools():
            pool.register(
                schema["function"]["name"], schema, handler, deferred=True,
            )
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug("External MCP tools not loaded: %s", e)

    # run_sequence is advertised with the normal Chrome Skill surface, but it
    # is owned and executed by Runtime. The low-level Chrome module therefore
    # has no dispatch action for it and can never forward a whole sequence.
    from packages.core.ai.runtime.chrome_sequence import (
        CHROME_RUN_SEQUENCE_TOOL_NAME,
        runtime_chrome_run_sequence_handler,
    )
    sequence_schema = pool.get_schema(CHROME_RUN_SEQUENCE_TOOL_NAME)
    if sequence_schema is not None:
        pool.register(
            CHROME_RUN_SEQUENCE_TOOL_NAME,
            sequence_schema,
            runtime_chrome_run_sequence_handler,
            deferred=True,
        )
