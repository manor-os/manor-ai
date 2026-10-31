"""Tools a Runtime host injects for one loop instead of the ToolPool registry.

Every host-armed handler (and every host-owned schema a loop adds) must be
listed here, so the effect-contract guard enumerates it alongside the
registry. A host that arms an unlisted tool fails loudly in tests
(``MANOR_STRICT_EFFECT_CONTRACT``) and logs in production: a new host tool can
never silently escape classification.
"""
from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

logger = logging.getLogger(__name__)

HOST_INJECTED_TOOLS: Mapping[str, frozenset[str]] = MappingProxyType({
    "task_agent_loop": frozenset({
        "update_task_plan",
        "complete_task",
        "wait_until",
        # Registered tool whose handler the loop overrides to park instead of block.
        "wait_media_jobs",
        "request_input",
        "read_context_archive",
    }),
    "chat": frozenset({
        "read_context_archive",
        "request_input",
        "send_channel_attachment",
    }),
})
ALL_HOST_INJECTED_TOOLS: frozenset[str] = frozenset().union(*HOST_INJECTED_TOOLS.values())


class UnregisteredHostToolError(RuntimeError):
    """A host armed a tool that the effect-contract guard does not enumerate."""


def runtime_check_host_tools(host: str | None, names: Iterable[str]) -> None:
    """``names`` must be listed for ``host`` (any host when ``None``)."""
    from packages.core.governance.action_effects import strict_effect_contract

    allowed = HOST_INJECTED_TOOLS.get(host, frozenset()) if host else ALL_HOST_INJECTED_TOOLS
    unlisted = sorted({str(name) for name in names} - allowed)
    if not unlisted:
        return
    message = (
        f"Host {host or 'runtime'!r} armed unlisted tool(s) {unlisted}; add them to "
        "packages/core/ai/runtime/host_tools.py HOST_INJECTED_TOOLS"
    )
    if strict_effect_contract():
        raise UnregisteredHostToolError(message)
    logger.error(message, extra={"event": "runtime.host_tool_unlisted", "host": host, "tools": unlisted})


def _task_loop_schema(name: str) -> dict[str, Any]:
    from packages.core.workers import task_agent_loop

    return {"update_task_plan": task_agent_loop.UPDATE_TASK_PLAN_TOOL,
            "complete_task": task_agent_loop.COMPLETE_TASK_TOOL}[name]


def _wait_until_schema() -> dict[str, Any]:
    from packages.core.workers.task_agent_loop_waits import WAIT_UNTIL_TOOL

    return WAIT_UNTIL_TOOL


def _registered_schema(name: str) -> Callable[[], dict[str, Any] | None]:
    def load() -> dict[str, Any] | None:
        from packages.core.ai.runtime.tool_registry import runtime_tool_schema

        return runtime_tool_schema(name)
    return load




def _context_archive_schema() -> dict[str, Any]:
    from packages.core.services.runtime_context_archive import CONTEXT_ARCHIVE_TOOL

    return CONTEXT_ARCHIVE_TOOL


def _request_input_schema() -> dict[str, Any]:
    from packages.core.ai.runtime.human_input import runtime_request_input_tool_schema

    return runtime_request_input_tool_schema()


def _channel_attachment_schema() -> dict[str, Any]:
    from packages.core.ai.runtime.channel_tools import runtime_channel_attachment_tool_schema

    return runtime_channel_attachment_tool_schema()


_SCHEMAS: Mapping[str, Callable[[], dict[str, Any] | None]] = MappingProxyType({
    "update_task_plan": lambda: _task_loop_schema("update_task_plan"),
    "complete_task": lambda: _task_loop_schema("complete_task"),
    "wait_until": _wait_until_schema,
    "wait_media_jobs": _registered_schema("wait_media_jobs"),
    "request_input": _request_input_schema,
    "read_context_archive": _context_archive_schema,
    "send_channel_attachment": _channel_attachment_schema,
})


def host_injected_tool_schema(name: str) -> dict[str, Any] | None:
    """The schema the host offers for one injected tool (for the guard)."""
    loader = _SCHEMAS.get(name)
    return loader() if loader is not None else None


__all__ = [
    "ALL_HOST_INJECTED_TOOLS",
    "HOST_INJECTED_TOOLS",
    "UnregisteredHostToolError",
    "host_injected_tool_schema",
    "runtime_check_host_tools",
]
