"""Guards for how code reaches an existing sandbox.

Two holes were closed together and are guarded here as classes:

* ``save_sandbox_file`` / ``list_sandbox_files`` (retired) were agent-visible
  tools that read or exec'd any model-supplied ``sandbox_id`` with no
  ownership check and no PPTX/DOCX quality gate. ``sandbox action='save_result'``
  and ``action='exec'`` cover them. No agent-visible tool may take a sandbox
  id without the per-call binding check.
* They, and image delivery, reached the sandbox through
  ``SANDBOX_SERVICE_URL``. On external runners a container lives on its own
  runner, so an existing sandbox must be reached through its route
  (``runtime_sandbox_client`` / ``_get_client_for_sandbox``). Only code that
  creates the container may talk to ``SANDBOX_SERVICE_URL`` directly.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest
from tests.source_inventory import production_python_files

ROOT = Path(__file__).resolve().parents[1]


# ── 1. no agent-visible tool bypasses the sandbox binding check ─────────


@pytest.fixture
def full_tool_pool(monkeypatch):
    from packages.core.ai.tool_pool import ToolPool
    from packages.core.ai.tools import register_all_tools
    from packages.core.config import get_settings

    # Sandbox tools register only when a sandbox service is configured.
    monkeypatch.setattr(get_settings(), "SANDBOX_SERVICE_URL", "http://sandbox-service")
    monkeypatch.setenv("SANDBOX_SERVICE_URL", "http://sandbox-service")
    pool = ToolPool()
    register_all_tools(pool)
    return pool


_ROUTED_CLIENT_MARKERS = ("_get_client_for_sandbox(", "runtime_sandbox_client(")


def _source(handler) -> str:
    try:
        return inspect.getsource(inspect.unwrap(handler))
    except (OSError, TypeError):
        return ""


def test_no_agent_visible_tool_bypasses_the_sandbox_access_check(full_tool_pool) -> None:
    from packages.core.ai.runtime.composite_tools import SandboxToolAction
    from packages.core.ai.tools import sandbox_tools

    # The composite gateway dispatches to one handler per action; derive them
    # from the action enum so a new action is covered automatically.
    action_handlers = {
        action: getattr(sandbox_tools, f"_sandbox_{action.value}") for action in SandboxToolAction
    }
    checked = {
        handler
        for action, handler in action_handlers.items()
        if action is not SandboxToolAction.CREATE
    }
    for handler in checked:
        assert "_sandbox_instance_access_error(" in _source(handler), handler.__name__

    sandbox_taking = []
    offenders = []
    for name in full_tool_pool.registered_tool_names(include_undiscoverable=True):
        entry = full_tool_pool.get(name)
        handler = entry["handler"]
        source = _source(handler)
        takes_sandbox_id = "sandbox_id" in json.dumps(entry["schema"])
        reaches_existing_sandbox = any(marker in source for marker in _ROUTED_CLIENT_MARKERS)
        if not (takes_sandbox_id or reaches_existing_sandbox):
            continue
        sandbox_taking.append(name)
        governed = (
            handler is sandbox_tools._sandbox_handler
            or handler is sandbox_tools._sandbox_create
            or handler in checked
        )
        if not governed:
            offenders.append(f"{name} -> {getattr(handler, '__module__', '?')}.{getattr(handler, '__qualname__', handler)}")

    # Sanity: the derivation still finds the sandbox gateway itself.
    assert "sandbox" in sandbox_taking
    assert offenders == []


def test_retired_sandbox_file_tools_are_gone_from_every_agent_surface(full_tool_pool) -> None:
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.capability_search_metadata import capability_search_metadata
    from packages.core.ai.runtime.tool_execution import RUNTIME_ENVELOPE_AWARE_TOOLS

    retired = ("save_sandbox_file", "list_sandbox_files")
    registered = set(full_tool_pool.registered_tool_names(include_undiscoverable=True))
    metadata = capability_search_metadata()
    for name in retired:
        assert name not in registered
        assert f"tool:{name}" not in metadata
        assert name not in RUNTIME_ENVELOPE_AWARE_TOOLS
    assert not (ROOT / "packages/core/ai/tools/sandbox_file_tools.py").exists()
    # Nothing classifies them as an available read action any more.
    source = (ROOT / "packages/core/ai/runtime/approval_classifier.py").read_text(encoding="utf-8")
    for name in retired:
        assert f'"{name}"' not in source
    assert classify_runtime_tool("sandbox", {"action": "exec", "params": {"command": "ls"}}) is not None


# ── 2. existing sandboxes are reached through their runner route ────────

# Functions that build a sandbox client from SANDBOX_SERVICE_URL without
# creating a container in the same function. Each is allowed for the stated
# reason; any new one fails the guard below.
_SERVICE_URL_CLIENT_ALLOWLIST = {
    "packages/core/ai/tools/sandbox_tools.py::_get_client": (
        "the local-coordination route: in local mode SANDBOX_SERVICE_URL hosts every container; "
        "callers are restricted by test_sandbox_tools_service_client_is_only_the_local_route"
    ),
}
_CLIENT_BUILDERS = ("SandboxClient", "_get_client", "_new_client", "sandbox_runner_client")
# create_builtin_sandbox: create_from_builtin with the baked-Skill choice (sandbox_baked_skills).
_CREATE_METHODS = ("create_from_files", "create_from_builtin", "create_builtin_sandbox")


def _production_python_files() -> list[str]:
    return [path for path in production_python_files(ROOT) if "/sandbox_sdk/" not in path]


def _reads_service_url(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr == "SANDBOX_SERVICE_URL":
            return True
        if isinstance(child, ast.Name) and child.id in {"SANDBOX_SERVICE_URL", "_get_sandbox_service_url"}:
            return True
        if isinstance(child, ast.Call):
            for arg in child.args:
                if isinstance(arg, ast.Constant) and arg.value == "SANDBOX_SERVICE_URL":
                    return True
    return False


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_existing_sandboxes_are_never_reached_through_the_service_url() -> None:
    found = set()
    offenders = []
    for path in _production_python_files():
        source = (ROOT / path).read_text(encoding="utf-8")
        if "SANDBOX_SERVICE_URL" not in source and "_get_sandbox_service_url" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = _called_names(node)
            if not (calls & set(_CLIENT_BUILDERS)) or not _reads_service_url(node):
                continue
            if calls & set(_CREATE_METHODS):
                continue  # creates the container it then talks to
            key = f"{path}::{node.name}"
            found.add(key)
            if key not in _SERVICE_URL_CLIENT_ALLOWLIST:
                offenders.append(key)
    assert offenders == []
    # A stale allowlist entry hides nothing; keep it exact.
    assert found == set(_SERVICE_URL_CLIENT_ALLOWLIST)


def test_sandbox_tools_service_client_is_only_the_local_route() -> None:
    """``sandbox_tools._get_client`` targets SANDBOX_SERVICE_URL; only the
    coordination-aware router and the local shutdown sweep may use it."""

    module_path = "packages/core/ai/tools/sandbox_tools.py"
    tree = ast.parse((ROOT / module_path).read_text(encoding="utf-8"))
    callers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and "_get_client" in _called_names(node)
    }
    assert callers == {"_get_client_for_sandbox", "destroy_all_sandboxes"}
    for path in _production_python_files():
        if path == module_path:
            continue
        source = (ROOT / path).read_text(encoding="utf-8")
        assert "sandbox_tools import _get_client\n" not in source, path
        assert "sandbox_tools._get_client(" not in source, path


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["EDIT_SKILL", "EDIT_RUNTIME"])
async def test_video_skill_sandbox_probe_follows_the_sandbox_route(monkeypatch, which) -> None:
    """The video-edit Skill sandbox and the Video Edit session container were
    both admitted by the session service, so the chat router probes them on
    their own runner, not on SANDBOX_SERVICE_URL."""

    from types import SimpleNamespace

    from apps.api.routers import chat
    from packages.core.ai.tools import sandbox_tools

    routed: list[str] = []

    expected = chat._VideoSandboxSkill[which]

    class RoutedClient:
        async def status(self, sandbox_id):
            return SimpleNamespace(status="ready", skill_name=expected.value)

        async def close(self):
            return None

    async def client_for_sandbox(sandbox_id):
        routed.append(sandbox_id)
        return RoutedClient()

    def forbid_service_url_client(*_args, **_kwargs):
        raise AssertionError("a video sandbox must not be probed through SANDBOX_SERVICE_URL")

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", client_for_sandbox)
    monkeypatch.setattr(sandbox_tools, "_get_client", forbid_service_url_client)

    assert await chat._live_video_sandbox_matches_skill("sb_video", expected)
    assert routed == ["sb_video"]
    assert not hasattr(chat, "_video_edit_session_service_client")
