"""A sandbox the runner reclaimed must not stay bound to its conversation.

Incident (staging, 2026-10-04): a pptx sandbox sat idle past the runner's
idle TTL (300 s) and was pruned; 13 minutes later the model reused the old
sandbox_id from its history. The conversation context (7-day TTL) and the
warm registry still named the container, so the call was routed to the runner
and failed with a bare ``Sandbox not found`` that told the model nothing about
how to continue. These tests pin the recovery contract: the vanished binding
is dropped, the model gets a ``sandbox_expired`` error naming the way back
(re-open the skill), and re-opening allocates a fresh container.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.test_sandbox_session_service import (  # noqa: F401 - fixtures
    FakeCache,
    ScriptedSandboxClient,
    _bundle,
    fake_cache,
    local_sandbox_settings,
)

CONVERSATION = "01M3RHSDAH4WCT68JGXPE3EJ19"
ENTITY = "ent_1"
USER = "user_1"
REGISTRY_KEY = f"sandbox:owner:{ENTITY}:{USER}:conversation:{CONVERSATION}"


class ExecScriptedClient(ScriptedSandboxClient):
    """Scripted client whose containers can vanish between calls."""

    gone: set[str] = set()

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.gone = set()

    async def status(self, sandbox_id):
        if sandbox_id in type(self).gone:
            from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

            type(self).calls.append({"handler": "status", "sandbox_id": sandbox_id})
            raise SandboxNotFoundError(f"Sandbox not found: {sandbox_id}", status_code=404)
        return await super().status(sandbox_id)

    async def exec(self, *, sandbox_id, command, timeout, execution_id=None):
        from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

        type(self).calls.append({"handler": "exec", "sandbox_id": sandbox_id, "command": command})
        if sandbox_id in type(self).gone:
            raise SandboxNotFoundError(f"Sandbox not found: {sandbox_id}", status_code=404)
        return SimpleNamespace(stdout="ok", stderr="", exit_code=0)


@pytest.fixture
def exec_client(monkeypatch):
    import packages.core.services.sandbox_sdk as sandbox_sdk

    ExecScriptedClient.reset()
    monkeypatch.setattr(sandbox_sdk, "SandboxClient", ExecScriptedClient)
    return ExecScriptedClient


async def _bind_warm_pptx_sandbox(fake_cache, sandbox_id: str = "998b86cf3c11") -> None:
    from packages.core.ai.runtime import runtime_init_sandbox_context

    await runtime_init_sandbox_context(
        CONVERSATION,
        sandbox_id,
        "skill_pptx",
        entity_id=ENTITY,
        user_id=USER,
        agent_id="",
    )
    fake_cache.store[REGISTRY_KEY] = {
        "sandbox_id": sandbox_id,
        "base_url": "http://sandbox-service",
        "created_at": 1.0,
        "last_acquired_at": 1.0,
        "loaded_skills": ["pptx"],
        "current_skill": "pptx",
    }
    fake_cache.store[f"sandbox:warm:{sandbox_id}"] = REGISTRY_KEY


async def _exec(sandbox_id: str, command: str = "python3 scripts/project_manager.py init deck") -> dict | str:
    from packages.core.ai.tools import sandbox_tools

    result = await sandbox_tools._sandbox_exec(
        entity_id=ENTITY,
        user_id=USER,
        sandbox_id=sandbox_id,
        command=command,
        conversation_id=CONVERSATION,
    )
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return result


@pytest.mark.asyncio
async def test_exec_on_reclaimed_sandbox_reports_expired_and_drops_the_binding(
    fake_cache, exec_client, local_sandbox_settings
) -> None:
    await _bind_warm_pptx_sandbox(fake_cache)
    exec_client.gone.add("998b86cf3c11")

    payload = await _exec("998b86cf3c11")

    assert payload["ok"] is False
    assert payload["error"]["code"] == "sandbox_expired"
    message = payload["error"]["message"]
    assert "998b86cf3c11" in message
    assert "invoke_skill" in message and "pptx" in message
    # The stale binding is gone everywhere it could be found again.
    ctx = fake_cache.store[f"sandbox:ctx:{CONVERSATION}"]
    assert "sandbox_id" not in ctx
    assert ctx["expired_sandbox_id"] == "998b86cf3c11"
    assert REGISTRY_KEY not in fake_cache.store
    assert "sandbox:warm:998b86cf3c11" not in fake_cache.store


@pytest.mark.asyncio
async def test_retrying_the_expired_id_repeats_the_expiry_without_calling_the_runner(
    fake_cache, exec_client, local_sandbox_settings
) -> None:
    await _bind_warm_pptx_sandbox(fake_cache)
    exec_client.gone.add("998b86cf3c11")
    await _exec("998b86cf3c11")
    exec_client.calls.clear()

    payload = await _exec("998b86cf3c11")

    assert payload["error"]["code"] == "sandbox_expired"
    assert "invoke_skill" in payload["error"]["message"]
    assert not [c for c in exec_client.calls if c["handler"] in {"exec", "status"}]


@pytest.mark.asyncio
async def test_not_found_for_a_live_sandbox_keeps_the_binding(
    fake_cache, exec_client, local_sandbox_settings, monkeypatch
) -> None:
    """A 404 about something inside a live sandbox is not an expiry."""

    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError

    await _bind_warm_pptx_sandbox(fake_cache)

    async def exec_404(self, **_kwargs):
        raise SandboxNotFoundError("Execution not found: x", status_code=404)

    monkeypatch.setattr(ExecScriptedClient, "exec", exec_404)

    payload = await _exec("998b86cf3c11")

    assert payload["error"]["code"] == "sandbox_execution_failed"
    assert fake_cache.store[f"sandbox:ctx:{CONVERSATION}"]["sandbox_id"] == "998b86cf3c11"
    assert REGISTRY_KEY in fake_cache.store


@pytest.mark.asyncio
async def test_reopening_after_expiry_allocates_a_fresh_sandbox_that_exec_accepts(
    fake_cache, exec_client, local_sandbox_settings
) -> None:
    """Replay of the incident: expiry, then the skill is re-opened and work resumes."""

    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxOwner,
        acquire_sandbox_session,
    )

    await _bind_warm_pptx_sandbox(fake_cache)
    exec_client.gone.add("998b86cf3c11")
    assert (await _exec("998b86cf3c11"))["error"]["code"] == "sandbox_expired"

    exec_client.created_ids = ["sb_fresh"]
    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id=ENTITY, user_id=USER),
        bundle=_bundle("pptx"),
        binding=SandboxBinding(conversation_id=CONVERSATION, agent_id="", skill_id="skill_pptx"),
    )

    assert session.sandbox_id == "sb_fresh"
    assert session.reused is False
    ctx = fake_cache.store[f"sandbox:ctx:{CONVERSATION}"]
    assert ctx["sandbox_id"] == "sb_fresh"
    assert "expired_sandbox_id" not in ctx
    assert await _exec("sb_fresh") == "ok\n[exit_code: 0]"
    # The old id stays refused: it belongs to nobody now.
    assert (await _exec("998b86cf3c11"))["error"]["code"] == "sandbox_access_denied"


@pytest.mark.asyncio
async def test_unroutable_sandbox_counts_as_expired(fake_cache, exec_client, local_sandbox_settings, monkeypatch) -> None:
    """External-runner mode: a released instance row with no warm record has no route at all."""

    from packages.core.ai.tools import sandbox_tools
    from packages.core.services.sandbox_queue_service import SandboxRouteNotFoundError

    await _bind_warm_pptx_sandbox(fake_cache)

    async def no_route(sandbox_id):
        raise SandboxRouteNotFoundError(f"no active runner route for sandbox {sandbox_id}")

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", no_route)

    payload = await _exec("998b86cf3c11")

    assert payload["error"]["code"] == "sandbox_expired"
    assert "sandbox_id" not in fake_cache.store[f"sandbox:ctx:{CONVERSATION}"]


@pytest.mark.asyncio
async def test_destroying_an_expired_sandbox_is_a_benign_no_op(
    fake_cache, exec_client, local_sandbox_settings
) -> None:
    from packages.core.ai.tools import sandbox_tools

    await _bind_warm_pptx_sandbox(fake_cache)
    exec_client.gone.add("998b86cf3c11")
    await _exec("998b86cf3c11")

    result = await sandbox_tools._sandbox_destroy(
        entity_id=ENTITY,
        user_id=USER,
        sandbox_id="998b86cf3c11",
        conversation_id=CONVERSATION,
    )

    assert "already" in result and "998b86cf3c11" in result


def test_every_runner_facing_action_maps_a_vanished_sandbox_to_expiry() -> None:
    """Guard the class: each sandbox action that talks to a sandbox's runner
    must route its failures through the vanished-sandbox check, or a reclaimed
    container surfaces as a generic failure again. Derived from the action
    enum so a new action is covered without editing this test."""

    import inspect

    from packages.core.ai.runtime.composite_tools import SandboxToolAction
    from packages.core.ai.tools import sandbox_tools

    runner_facing = {}
    for action in SandboxToolAction:
        handler = getattr(sandbox_tools, f"_sandbox_{action.value}")
        source = inspect.getsource(handler)
        if "_get_client_for_sandbox(" in source:
            runner_facing[action.value] = source
    # Sanity: the derivation still finds the actions this incident hit.
    assert {"exec", "status", "read_file", "write_file", "save_result"} <= set(runner_facing)
    missing = sorted(name for name, source in runner_facing.items() if "_vanished_sandbox_error(" not in source)
    assert missing == []
