"""Every built-in tool schema must be accepted by every provider we route to.

Anthropic rejects a tool whose ``input_schema`` uses ``anyOf``/``oneOf``/``allOf``
at the top level (HTTP 400 ``input_schema does not support oneOf, allOf, or anyOf
at the top level``). Because OpenRouter forwards the whole tool list on every
call, a single offending tool takes down every Anthropic-routed turn that loads
it. This guard scans the real registry instead of one hand-picked schema so a
new tool cannot reintroduce the class of failure.
"""

from __future__ import annotations

from packages.core.ai.tool_pool import ToolPool

_TOP_LEVEL_COMBINATORS = frozenset({"anyOf", "oneOf", "allOf"})


def _top_level_combinators(schema: dict) -> set[str]:
    parameters = (schema.get("function") or {}).get("parameters") or {}
    return set(parameters) & _TOP_LEVEL_COMBINATORS


def test_detector_sees_top_level_combinators():
    bad = {
        "type": "function",
        "function": {
            "name": "bad",
            "parameters": {
                "type": "object",
                "properties": {},
                "anyOf": [{"required": ["a"]}, {"required": ["b"]}],
            },
        },
    }
    assert _top_level_combinators(bad) == {"anyOf"}
    # Nested combinators inside a property are allowed by Anthropic.
    nested = {
        "type": "function",
        "function": {
            "name": "ok",
            "parameters": {
                "type": "object",
                "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
            },
        },
    }
    assert _top_level_combinators(nested) == set()


def test_no_builtin_tool_schema_uses_top_level_combinators():
    pool = ToolPool()
    pool.initialize()
    schemas = pool.registered_tool_schemas(include_undiscoverable=True)
    assert len(schemas) > 50, "registry scan did not load the built-in tools"
    offenders = {name: sorted(found) for name, schema in schemas if (found := _top_level_combinators(schema))}
    assert offenders == {}, f"tool schemas with top-level anyOf/oneOf/allOf are rejected by Anthropic: {offenders}"

