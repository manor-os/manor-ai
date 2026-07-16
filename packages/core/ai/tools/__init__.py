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
        system_tools,
        bash_tool,
        file_tools,
        generate_file_tool,
        extended_tools,
        media_tools,
        goal_tools,
        manor_tool,
        code_tool,
        browser_tools,
        agent_file_tools,
        sandbox_file_tools,
        workspace_arch_tools,
        workspace_agent_tools,
        agent_provisioning_tools,
        notification_tools,
        dashboard_tools,
    )

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

    for module in [
        document_tools,
        rag_tools,
        web_tools,
        weather_tools,
        system_tools,
        bash_tool,
        file_tools,
        generate_file_tool,
        extended_tools,
        media_tools,
        goal_tools,
        manor_tool,
        code_tool,
        browser_tools,
        agent_file_tools,
        sandbox_file_tools,
        workspace_arch_tools,
        workspace_agent_tools,
        agent_provisioning_tools,
        notification_tools,
        dashboard_tools,
    ]:
        for schema, handler in module.get_tools():
            pool.register(schema["function"]["name"], schema, handler)

    # Workspace context search — lets agents query workspace state
    from . import workspace_context_tool
    for schema, handler in workspace_context_tool.get_tools():
        pool.register(schema["function"]["name"], schema, handler)

    # Sandbox tools — only registered when SANDBOX_SERVICE_URL is configured.
    # sandbox_exec / sandbox_destroy are always-loaded when present so the LLM
    # can drive them interactively after invoke_skill returns a sandbox_id.
    try:
        from . import sandbox_tools as _sandbox_mod
        for schema, handler in _sandbox_mod.get_tools():
            pool.register(schema["function"]["name"], schema, handler)
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
