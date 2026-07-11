from __future__ import annotations

import json

import pytest

from packages.core.ai.tool_pool import ToolPool
from packages.core.ai.runtime.tool_registry import runtime_registered_tool_surface_from_schemas
from packages.core.ai.tools.code_tool import CODE_SCHEMA, _code_handler


def test_master_gets_code_schema_when_registered():
    pool = ToolPool()
    pool.register("code", CODE_SCHEMA, handler=_code_handler)

    surface = runtime_registered_tool_surface_from_schemas(
        pool.registered_tool_schemas(),
        is_master=True,
    )
    schemas = list(surface.prompt_schemas)
    deferred = list(surface.deferred_tool_names)
    schema_names = {schema["function"]["name"] for schema in schemas}

    assert "code" in schema_names
    assert "code" not in deferred


@pytest.mark.asyncio
async def test_code_handler_accepts_direct_action_params():
    result = await _code_handler(
        entity_id="test-entity",
        action="plan_create",
        goal="Ship the fix",
        steps=[{"id": "one", "description": "Patch code tool"}],
    )

    payload = json.loads(result)
    assert payload["plan_created"] is True
    assert payload["goal"] == "Ship the fix"
    assert payload["steps"] == 1
