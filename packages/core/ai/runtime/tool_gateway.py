"""``call_tool``: one fixed tool through which discovered tools are called.

Tools found by ``search_tools`` (and those restored from earlier turns) used to
be appended to the request's tool list. Providers put tool definitions at the
very front of the prompt, so every discovery invalidated the cached prefix of
the whole conversation. Discovered tools now stay out of that list: their
definitions travel with the turn context, after the history, and the model
calls them as ``call_tool(name=..., arguments={...})``.

The agentic loop unwraps each such call into the real tool call (same call id)
right after the model emits it, so authorization, approvals, evidence, UI and
memory all see the real tool. The registered ``call_tool`` handler only runs
for calls the loop did not unwrap; it reports why, or delegates to the target
through the normal governed execution path.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

CALL_TOOL_NAME = "call_tool"
# Definitions rendered into the turn context; beyond this, least recently
# loaded definitions are listed by name only (they stay callable).
GATEWAY_SCHEMA_CHARS_BUDGET = 48_000


def call_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": CALL_TOOL_NAME,
            "description": (
                "Call a tool listed under 'Tools callable through call_tool' in the turn context "
                "(tools loaded by search_tools). Pass its exact name and arguments that match its "
                "parameters. Tools in your own tool list are called directly, not through this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact name of the tool to call."},
                    "arguments": {
                        "type": "object",
                        "description": "Arguments for that tool, matching its parameters.",
                    },
                },
                "required": ["name", "arguments"],
                # The runtime adds ``rejected_reason`` to calls it will not
                # unwrap so the handler can explain; extra keys are inert.
            },
        },
    }


def runtime_schema_tool_name(schema: Any) -> str:
    if not isinstance(schema, Mapping):
        return ""
    function = schema.get("function") if isinstance(schema.get("function"), Mapping) else schema
    return str(function.get("name") or "").strip()


def runtime_gateway_target(arguments: Any) -> tuple[str, dict[str, Any]] | None:
    """The (name, arguments) a ``call_tool`` call names, or None if malformed."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    if not isinstance(arguments, Mapping):
        return None
    name = arguments.get("name")
    inner = arguments.get("arguments", {})
    if isinstance(inner, str):
        try:
            inner = json.loads(inner) if inner.strip() else {}
        except ValueError:
            return None
    if not isinstance(name, str) or not name.strip() or not isinstance(inner, Mapping):
        return None
    name = name.strip()
    if name == CALL_TOOL_NAME:
        return None
    return name, dict(inner)


def runtime_tool_call_target(name: Any, arguments: Any) -> tuple[str, Any]:
    """The tool a history call really invoked, looking through ``call_tool``."""
    if name == CALL_TOOL_NAME:
        target = runtime_gateway_target(arguments)
        if target is not None:
            return target
    return name, arguments


def runtime_gateway_arguments(name: str, arguments: Any) -> dict[str, Any]:
    """``call_tool`` arguments for a real call (checkpoint declarations)."""
    return {"name": name, "arguments": arguments if isinstance(arguments, Mapping) else {}}


def runtime_unwrap_gateway_call(
    tool_call: dict[str, Any],
    *,
    reachable: set[str],
) -> tuple[dict[str, Any], str | None]:
    """Return the real call for a ``call_tool`` call, or the call plus a rejection.

    ``reachable`` is the set the model may call right now: declared tools and
    loaded gateway tools after every narrowing of this round. A rejected call is
    left as ``call_tool`` with the reason in its arguments; its handler reports
    the reason without executing anything.
    """
    if tool_call.get("name") != CALL_TOOL_NAME:
        return tool_call, None
    target = runtime_gateway_target(tool_call.get("arguments"))
    if target is None:
        reason = (
            "call_tool needs {\"name\": <tool name>, \"arguments\": {...}}; "
            "the name must be a tool loaded by search_tools, not call_tool itself."
        )
    elif target[0] not in reachable:
        reason = (
            f"'{target[0]}' is not loaded for this request. Use search_tools to find and load it, "
            "then call it through call_tool."
        )
    else:
        return {**tool_call, "name": target[0], "arguments": target[1]}, None
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), Mapping) else {}
    return {**tool_call, "arguments": {**arguments, "rejected_reason": reason}}, reason


def runtime_gateway_tools_section(schemas: Mapping[str, dict[str, Any]]) -> str | None:
    """Render loaded gateway tools for the turn context, newest last."""
    if not schemas:
        return None
    entries: list[str] = []
    used = 0
    names_only: list[str] = []
    for name, schema in reversed(list(schemas.items())):
        function = schema.get("function") if isinstance(schema.get("function"), Mapping) else schema
        entry = json.dumps({
            "name": name,
            "description": str(function.get("description") or "").strip(),
            "parameters": function.get("parameters") or {"type": "object"},
        }, ensure_ascii=False, separators=(",", ":"))
        if used + len(entry) > GATEWAY_SCHEMA_CHARS_BUDGET and entries:
            names_only.append(name)
            continue
        entries.append(entry)
        used += len(entry)
    entries.reverse()
    lines = [
        "## Tools callable through call_tool",
        "Loaded by search_tools. Call one as call_tool(name=<name>, arguments={...}) with "
        "arguments that match its parameters.",
        *entries,
    ]
    if names_only:
        lines.append(
            "Also loaded (definitions omitted for size; search_tools again for details): "
            + ", ".join(sorted(names_only))
        )
    return "\n".join(lines)


def runtime_gateway_schemas_by_name(schemas: Iterable[Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for schema in schemas or ():
        name = runtime_schema_tool_name(schema)
        if name and name != CALL_TOOL_NAME:
            out[name] = dict(schema)
    return out


__all__ = [
    "CALL_TOOL_NAME",
    "GATEWAY_SCHEMA_CHARS_BUDGET",
    "call_tool_schema",
    "runtime_gateway_arguments",
    "runtime_gateway_schemas_by_name",
    "runtime_gateway_target",
    "runtime_gateway_tools_section",
    "runtime_schema_tool_name",
    "runtime_tool_call_target",
    "runtime_unwrap_gateway_call",
]
