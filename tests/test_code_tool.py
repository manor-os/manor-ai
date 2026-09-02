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


@pytest.mark.asyncio
async def test_runtime_code_filesystem_actions_require_sandbox(monkeypatch):
    from packages.core.ai.tools import code_tool

    def forbidden(*_args, **_kwargs):
        raise AssertionError("runtime code action reached the API filesystem")

    monkeypatch.setattr(code_tool, "_get_cwd", forbidden)
    result = json.loads(await _code_handler(
        entity_id="test-entity",
        action="refactor_extract",
        params={
            "file": "../../../etc/hosts",
            "start_line": 1,
            "end_line": 1,
        },
        _user_id_from_context="user-1",
    ))

    assert result["error"] == "code_action_requires_sandbox"


def test_code_child_path_cannot_escape_cwd(tmp_path):
    from packages.core.ai.tools.code_tool import CodePathFactory

    with pytest.raises(ValueError, match="inside the coding workspace"):
        CodePathFactory.resolve(str(tmp_path), "../../../etc/hosts")
