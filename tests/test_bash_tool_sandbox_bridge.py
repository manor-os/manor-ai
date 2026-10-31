"""bash shares the generic sandbox's admission and never creates one-shot containers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def no_entity_fs(monkeypatch):
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", False)


@pytest.mark.asyncio
async def test_bash_admits_the_generic_sandbox_and_runs_there(monkeypatch, tmp_path, no_entity_fs) -> None:
    from packages.core.ai.runtime import sandbox as runtime_sandbox
    from packages.core.ai.tools import bash_tool, sandbox_tools
    from packages.core.services.sandbox_session_service import (
        GENERIC_SANDBOX_REUSE_SCOPE,
        SandboxSession,
    )
    from tests.bash_sandbox_fakes import LocalSandbox

    admitted: list[dict] = []
    local = LocalSandbox(base=tmp_path / "sandbox")
    local.scratch.mkdir(parents=True)

    async def acquire(*, owner, bundle, binding, **_kwargs):
        admitted.append({"owner": owner, "bundle": bundle, "binding": binding})
        return SandboxSession(sandbox_id="sbx-generic", base_url="http://runner", created_at=0, last_acquired_at=0)

    async def no_binding(*_args):
        return None

    async def client_for(sandbox_id):
        assert sandbox_id == "sbx-generic"
        return local

    def forbidden(*_args, **_kwargs):
        raise AssertionError("bash must never build its own SandboxClient or one-shot sandbox")

    monkeypatch.setattr(sandbox_tools, "acquire_sandbox_session", acquire)
    monkeypatch.setattr(sandbox_tools, "_sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", client_for)
    monkeypatch.setattr(runtime_sandbox, "runtime_agent_sandbox", no_binding)
    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", forbidden)

    payload = json.loads(await bash_tool._bash(
        "entity_1",
        command="python3 -c 'print(6*7)'",
        _user_id_from_context="user_1",
        _agent_id_from_context="agent_1",
        conversation_id="conv_1",
    ))

    assert payload["exit_code"] == 0 and payload["stdout"] == "42\n", payload
    [admission] = admitted
    assert admission["bundle"].reuse_scope == GENERIC_SANDBOX_REUSE_SCOPE
    assert admission["bundle"].skill_name == sandbox_tools.generic_sandbox_bundle().skill_name
    assert (admission["owner"].entity_id, admission["owner"].user_id) == ("entity_1", "user_1")
    assert (admission["binding"].conversation_id, admission["binding"].agent_id) == ("conv_1", "agent_1")
    # The user's shared sandbox: the command queues for a slot fairly in its
    # conversation's work area, with no Skill view or Skill credentials.
    [user_command] = [call for call in local.calls if "print(6*7)" in call["command"]]
    assert user_command["queue_timeout"] > 0 and user_command["fair_key"] == "conv_1"
    assert user_command["skill"] == ""


@pytest.mark.asyncio
async def test_bash_reuses_the_bound_generic_sandbox(monkeypatch, tmp_path, no_entity_fs) -> None:
    from packages.core.ai.runtime import sandbox as runtime_sandbox
    from packages.core.ai.tools import bash_tool, sandbox_tools
    from tests.bash_sandbox_fakes import LocalSandbox

    local = LocalSandbox(base=tmp_path / "sandbox")
    local.scratch.mkdir(parents=True)
    scopes = []

    async def acquire(**_kwargs):
        raise AssertionError("a bound sandbox is reused, not admitted again")

    async def client_for(sandbox_id):
        assert sandbox_id == "sbx-bound"
        return local

    monkeypatch.setattr(sandbox_tools, "acquire_sandbox_session", acquire)
    monkeypatch.setattr(sandbox_tools, "_sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", client_for)

    async def owned(scope, agent_id, kind):
        scopes.append((scope, agent_id, kind))
        return "sbx-bound", {"sandbox_id": "sbx-bound"}

    monkeypatch.setattr(runtime_sandbox, "runtime_agent_sandbox", owned)
    payload = json.loads(await bash_tool._bash(
        "entity_1", command="echo reuse", _user_id_from_context="user_1",
        _agent_id_from_context="agent_1", conversation_id="conv_1",
    ))
    assert payload["stdout"] == "reuse\n", payload
    [(scope, agent_id, kind)] = scopes
    # The user's own scope (one general sandbox across their conversations).
    assert scope.shared and (scope.entity_id, scope.user_id, scope.binding_key) == ("entity_1", "user_1", "conv_1")
    assert (agent_id, kind) == ("agent_1", runtime_sandbox.RuntimeSandboxKind.GENERIC)


@pytest.mark.asyncio
async def test_durable_admission_parks_the_run(monkeypatch, no_entity_fs) -> None:
    from packages.core.ai.runtime import sandbox as runtime_sandbox
    from packages.core.ai.tools import bash_tool, sandbox_tools

    suspension = SimpleNamespace(kind="runtime_tool_suspension")

    async def acquire(**_kwargs):
        return suspension

    async def no_binding(*_args):
        return None

    monkeypatch.setattr(sandbox_tools, "acquire_generic_sandbox", acquire)
    monkeypatch.setattr(sandbox_tools, "_sandbox_available", lambda: True)
    monkeypatch.setattr(runtime_sandbox, "runtime_agent_sandbox", no_binding)
    result = await bash_tool._bash(
        "entity_1", command="ls", _user_id_from_context="user_1", conversation_id="conv_1",
    )
    assert result is suspension, "the Runtime suspension is returned so the run parks and resumes"


@pytest.mark.asyncio
async def test_bash_without_a_binding_scope_is_refused(monkeypatch, no_entity_fs) -> None:
    from packages.core.ai.tools import bash_tool, sandbox_tools

    monkeypatch.setattr(sandbox_tools, "_sandbox_available", lambda: True)
    result = json.loads(await bash_tool._bash("entity_1", command="ls", _user_id_from_context="user_1"))
    assert result["error"] == "sandbox_scope_unavailable"
    assert result["command_executed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["   ", "sudo ls", "shutdown now"])
async def test_bash_refusals_before_anything_ran_are_settled_by_the_handler(command) -> None:
    from packages.core.ai.runtime.receipts import runtime_tool_effect_state
    from packages.core.ai.tools import bash_tool

    result = await bash_tool._bash("entity_1", command=command)
    assert json.loads(result)["command_executed"] is False
    assert runtime_tool_effect_state("bash", {"command": command}, result).value == "none"
    # The same text as plain JSON proves nothing.
    assert runtime_tool_effect_state("bash", {"command": command}, str(result)).value == "unconfirmed"


@pytest.mark.asyncio
async def test_a_failed_command_that_could_not_reach_manor_is_settled_by_the_handler(
    monkeypatch, tmp_path, no_entity_fs,
) -> None:
    """Without an entity filesystem nothing a command does can reach Manor, so
    its failure is host-proved no effect (with one, see test_bash_in_sandbox)."""
    from packages.core.ai.runtime.receipts import runtime_tool_effect_state
    from packages.core.ai.tools import bash_tool
    from tests.bash_sandbox_fakes import install

    install(monkeypatch, tmp_path)
    command = "python3 -c \"import wave; wave.open('s01.wav')\""
    result = await bash_tool._bash(
        "entity_1", command=command, _user_id_from_context="user_1",
        _agent_id_from_context="agent_1", conversation_id="conv_1",
    )
    assert json.loads(result)["exit_code"] != 0, result
    assert runtime_tool_effect_state("bash", {"command": command}, result).value == "none"
