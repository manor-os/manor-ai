"""A sandbox belongs to the USER x CONVERSATION, shared by every agent in it.

Ownership is (entity, user, root conversation) -- or, for user-less system
work, the workspace keyed by the task. The master agent, sub-agents and
delegates (which run in their parent's conversation) and every Skill they
invoke share the conversation's sandbox; it is created on demand, never
pre-created. The acting agent is recorded for audit and in a per-agent view
(which Skill it drives, whether its commands receive that Skill's
credentials), never in ownership.

What must still separate, and is pinned here as strong guards:

* another user -- also a member of the same workspace conversation -- never
  reaches the sandbox, by binding or by id;
* the same user in another conversation never reaches it either;
* a user-less task only reaches the sandbox of its own task;
* a call without a scope is denied before any runner I/O.

Guard the class: every reader and writer of the binding goes through the
scoped API in ``packages/core/ai/runtime/sandbox.py``.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from tests.source_inventory import production_python_files

from tests.sandbox_exec_fakes import ExecViaStart  # noqa: E402

from tests.test_sandbox_session_service import (  # noqa: F401 - fixtures
    FakeCache,
    _bundle,
    _load_result,
    fake_cache,
    local_sandbox_settings,
)
from tests.test_sandbox_vanished_recovery import exec_client  # noqa: F401 - fixture

ENTITY = "ent_1"
USER = "user_1"
OTHER_USER = "user_2"
CONVERSATION = "01M3RHSDAH4WCT68JGXPE3EJ19"
OTHER_CONVERSATION = "01M3RHSDAH4WCT68JGXPE3EJ20"
ROOT = Path(__file__).resolve().parents[1]


async def _fast_sleep(_seconds: float) -> None:
    await asyncio.sleep(0.002)


async def _open_skill(
    agent_id: str,
    skill: str,
    *,
    conversation: str = CONVERSATION,
    user: str = USER,
):
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxOwner,
        SandboxSession,
        acquire_sandbox_session,
    )

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id=ENTITY, user_id=user),
        bundle=_bundle(skill),
        binding=SandboxBinding(conversation_id=conversation, agent_id=agent_id, skill_id=f"skill_{skill}"),
        sleep=_fast_sleep,
    )
    assert isinstance(session, SandboxSession), session
    return session.sandbox_id


async def _create_generic(agent_id: str, *, user: str = USER, conversation: str = CONVERSATION) -> str:
    from packages.core.ai.tools import sandbox_tools

    result = await sandbox_tools._sandbox_create(
        entity_id=ENTITY,
        user_id=user,
        conversation_id=conversation,
        _agent_id_from_context=agent_id,
    )
    match = re.search(r"sandbox_id: (\S+)", result)
    assert match, result
    return match.group(1)


async def _exec(agent_id: str, sandbox_id: str, *, user: str = USER, conversation: str = CONVERSATION):
    from packages.core.ai.tools import sandbox_tools

    result = await sandbox_tools._sandbox_exec(
        entity_id=ENTITY,
        user_id=user,
        sandbox_id=sandbox_id,
        command="pwd",
        conversation_id=conversation,
        _agent_id_from_context=agent_id,
    )
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return result


def _created(client) -> list[str]:
    return [call["skill_name"] for call in client.calls if call["handler"].startswith("create_from")]


def _placements(client) -> list[tuple[str, dict]]:
    """Where each command ran and with which view (the queue options are the same for all)."""
    queue_options = {"fair_key", "queue_timeout"}
    return [
        (call["sandbox_id"], {key: value for key, value in call["placement"].items() if key not in queue_options})
        for call in client.calls
        if call["handler"] == "exec"
    ]


def _area(agent: str, conversation: str = CONVERSATION) -> dict:
    """The placement of an agent's command that drives no Skill: its own work area."""
    return {"workdir": f"/workspace/c/{conversation}/agents/{agent}", "skill_env": False, "skill": ""}


# ── 1. the parent and its delegates share the conversation's sandbox ─────


@pytest.mark.asyncio
async def test_delegated_sub_agent_shares_the_parents_sandbox(
    fake_cache, exec_client, local_sandbox_settings
) -> None:
    from packages.core.ai.tools import sandbox_tools

    exec_client.created_ids = ["sb_conversation", "sb_unexpected"]
    exec_client.load_results = [_load_result("sb_conversation")]
    parent = await _open_skill("agent-p", "pptx")
    child = await _open_skill("agent-b", "pptx")

    # One container for the conversation, opened on demand by the first agent.
    assert _created(exec_client) == ["pptx"]
    assert parent == child == "sb_conversation"

    # Both agents operate it, either one's commands and the other's.
    assert await _exec("agent-p", parent) == "ok\n[exit_code: 0]"
    assert await _exec("agent-b", parent) == "ok\n[exit_code: 0]"

    # The child releasing it leaves it to the parent (and the conversation).
    released = await sandbox_tools._sandbox_destroy(
        entity_id=ENTITY,
        user_id=USER,
        sandbox_id=child,
        conversation_id=CONVERSATION,
        _agent_id_from_context="agent-b",
    )
    assert "stays warm" in released and "denied" not in released
    assert await _exec("agent-p", parent) == "ok\n[exit_code: 0]"
    # Release dropped only the child's view: it no longer drives the Skill,
    # so its commands run without the Skill's credentials, in its own dir.
    exec_client.calls.clear()
    assert await _exec("agent-b", parent) == "ok\n[exit_code: 0]"
    assert _placements(exec_client) == [("sb_conversation", _area("agent-b"))]


@pytest.mark.asyncio
async def test_parallel_delegates_share_one_sandbox(fake_cache, exec_client, local_sandbox_settings) -> None:
    agents = [f"agent-{index:02d}" for index in range(20)]
    exec_client.created_ids = ["sb_conversation", *(f"sb_extra_{index}" for index in range(20))]
    exec_client.load_results = [_load_result("sb_conversation")] * 19

    opened = await asyncio.gather(*(_open_skill(agent, "docx") for agent in agents))

    # Single flight: one container, every delegate bound to it.
    assert _created(exec_client) == ["docx"]
    assert set(opened) == {"sb_conversation"}
    results = await asyncio.gather(*(_exec(agent, sandbox) for agent, sandbox in zip(agents, opened)))
    assert results == ["ok\n[exit_code: 0]"] * len(agents)

    from packages.core.ai.runtime.sandbox import RuntimeSandboxScope, runtime_load_sandbox_binding

    record = await runtime_load_sandbox_binding(RuntimeSandboxScope(ENTITY, USER, CONVERSATION), "sb_conversation")
    # Audit: who created it, and every agent that used it.
    assert record["created_by_agent"] in agents
    assert sorted(record["agents"]) == agents


# ── 2. what still separates: other users, other conversations ─────────────


@pytest.mark.asyncio
async def test_another_member_of_the_conversation_gets_their_own_sandbox_and_never_reaches_mine(
    fake_cache, exec_client, local_sandbox_settings
) -> None:
    exec_client.created_ids = ["sb_alice", "sb_bob"]
    alice = await _open_skill("agent-p", "pptx")
    bob = await _open_skill("agent-p", "pptx", user=OTHER_USER)

    assert (alice, bob) == ("sb_alice", "sb_bob")
    assert _created(exec_client) == ["pptx", "pptx"]
    assert (await _exec("agent-p", alice, user=OTHER_USER))["error"]["code"] == "sandbox_access_denied"
    assert (await _exec("agent-p", bob))["error"]["code"] == "sandbox_access_denied"
    assert not [call for call in exec_client.calls if call["handler"] == "exec"]


@pytest.mark.asyncio
async def test_the_same_user_in_another_conversation_shares_the_container_in_its_own_area(
    fake_cache, exec_client, local_sandbox_settings
) -> None:
    """One sandbox per USER: another conversation of the same user reaches the
    container (it is the user's), but works in its own area, and only the
    agents that invoked a Skill -- in their own conversation -- see it."""

    exec_client.created_ids = ["sb_user", "sb_unexpected"]
    exec_client.load_results = [_load_result("sb_user")]
    first = await _open_skill("agent-p", "pptx")
    second = await _open_skill("agent-p", "pptx", conversation=OTHER_CONVERSATION)
    assert first == second == "sb_user"
    assert _created(exec_client) == ["pptx"]

    exec_client.calls.clear()
    # Each conversation's agent that invoked the Skill sees it...
    assert await _exec("agent-p", first) == "ok\n[exit_code: 0]"
    assert await _exec("agent-p", first, conversation=OTHER_CONVERSATION) == "ok\n[exit_code: 0]"
    # ...any other agent of either conversation works in its own area.
    assert await _exec("agent-x", first, conversation=OTHER_CONVERSATION) == "ok\n[exit_code: 0]"
    assert _placements(exec_client) == [
        ("sb_user", {"skill": "pptx"}),
        ("sb_user", {"skill": "pptx"}),
        ("sb_user", _area("agent-x", OTHER_CONVERSATION)),
    ]

    from packages.core.ai.runtime.sandbox import RuntimeSandboxScope, runtime_load_sandbox_binding

    record = await runtime_load_sandbox_binding(RuntimeSandboxScope(ENTITY, USER, OTHER_CONVERSATION), "sb_user")
    assert sorted(record["works"]) == sorted([CONVERSATION, OTHER_CONVERSATION])


# ── 3. generic work and Skills share the primary container ───────────────


@pytest.mark.asyncio
async def test_generic_then_skill_share_the_conversation_sandbox(
    fake_cache, exec_client, local_sandbox_settings
) -> None:
    exec_client.created_ids = ["sb_conversation", "sb_unexpected"]
    exec_client.load_results = [_load_result("sb_conversation")]

    generic = await _create_generic("agent-p")
    skill = await _open_skill("agent-p", "pptx")

    assert generic == skill == "sb_conversation"
    assert _created(exec_client) == ["generic-sandbox"]
    # Loading the Skill over the generic placeholder needs no idle wait.
    [load] = [call for call in exec_client.calls if call["handler"] == "load_skill"]
    assert (load["skill_name"], load["idle_threshold"]) == ("pptx", 0)

    exec_client.calls.clear()
    assert await _exec("agent-p", skill) == "ok\n[exit_code: 0]"
    assert await _exec("agent-b", skill) == "ok\n[exit_code: 0]"
    # The agent driving the Skill runs in /skill with its credentials; the
    # other agent in its own directory under the shared /workspace, without.
    assert _placements(exec_client) == [
        ("sb_conversation", {"skill": "pptx"}),
        ("sb_conversation", _area("agent-b")),
    ]


@pytest.mark.asyncio
async def test_generic_create_is_refused_while_the_agent_drives_a_governed_skill(
    fake_cache, exec_client, local_sandbox_settings
) -> None:
    """Governance kept per agent: a blank sandbox must not bypass the Skill
    this agent drives; another agent of the conversation is never blocked."""

    from packages.core.ai.tools import sandbox_tools

    exec_client.created_ids = ["sb_conversation", "sb_unexpected"]
    skill = await _open_skill("agent-p", "pptx")

    refused = await sandbox_tools._sandbox_create(
        entity_id=ENTITY, user_id=USER, conversation_id=CONVERSATION, _agent_id_from_context="agent-p",
    )
    error = json.loads(refused)["error"]
    assert error["code"] == "sandbox_already_active"
    assert f"action='destroy' sandbox_id='{skill}'" in error["message"]

    # Another agent joins the conversation's sandbox (the same container).
    assert await _create_generic("agent-b") == "sb_conversation"

    released = await sandbox_tools._sandbox_destroy(
        entity_id=ENTITY, user_id=USER, sandbox_id=skill,
        conversation_id=CONVERSATION, _agent_id_from_context="agent-p",
    )
    assert "released" in released
    assert await _create_generic("agent-p") == "sb_conversation"
    assert _created(exec_client) == ["pptx"]


# ── 4. a vanished sandbox expires for the whole conversation ────────────


@pytest.mark.asyncio
async def test_vanished_sandbox_expires_for_every_agent_of_the_conversation(
    fake_cache, exec_client, local_sandbox_settings
) -> None:
    exec_client.created_ids = ["sb_conversation", "sb_other_user"]
    shared = await _open_skill("agent-p", "pptx")
    other_user = await _open_skill("agent-p", "pptx", user=OTHER_USER)
    exec_client.gone.add(shared)

    assert (await _exec("agent-b", shared))["error"]["code"] == "sandbox_expired"
    assert (await _exec("agent-p", shared))["error"]["code"] == "sandbox_expired"
    assert await _exec("agent-p", other_user, user=OTHER_USER) == "ok\n[exit_code: 0]"


# ── 5. legacy single-slot bindings (delete 7 days after deploy) ─────────


def _legacy(agent_id: str, *, sandbox_id: str = "sb_legacy", skill_id: str = "skill_pptx") -> dict:
    return {
        "sandbox_id": sandbox_id,
        "skill_id": skill_id,
        "skill_key": "pptx",
        "entity_id": ENTITY,
        "user_id": USER,
        "agent_id": agent_id,
        "pptx_checkpoint_rel_path": "decks/a.pptx",
    }


@pytest.mark.asyncio
async def test_matching_legacy_binding_is_adopted_and_migrated(fake_cache, exec_client, local_sandbox_settings) -> None:
    from packages.core.ai.runtime.sandbox import RuntimeSandboxKind, RuntimeSandboxScope, runtime_agent_sandbox

    legacy_key = f"sandbox:ctx:{CONVERSATION}"
    fake_cache.store[legacy_key] = _legacy("agent-p")

    # Any agent of the same owner's conversation may use it now.
    assert await _exec("agent-b", "sb_legacy") == "ok\n[exit_code: 0]"

    scope = RuntimeSandboxScope(ENTITY, USER, CONVERSATION)
    record = fake_cache.store[scope.key("sbx", "sb_legacy")]
    assert record["sandbox_id"] == "sb_legacy"
    assert record["pptx_checkpoint_rel_path"] == "decks/a.pptx"
    assert (record["created_by_agent"], record["agents"]) == ("agent-p", ["agent-p"])
    assert legacy_key not in fake_cache.store
    # The legacy agent keeps driving its Skill there.
    assert (await runtime_agent_sandbox(scope, "agent-p", RuntimeSandboxKind.SKILL))[0] == "sb_legacy"
    # Served from the scoped key from now on.
    assert await _exec("agent-p", "sb_legacy") == "ok\n[exit_code: 0]"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy",
    [
        {**_legacy("agent-p"), "user_id": OTHER_USER},  # another user's
        {**_legacy("agent-p"), "entity_id": "other-entity"},  # another tenant's
        _legacy("agent-p", sandbox_id="sb_another"),  # another sandbox
    ],
)
async def test_foreign_legacy_binding_is_never_adopted(
    fake_cache, exec_client, local_sandbox_settings, legacy
) -> None:
    legacy_key = f"sandbox:ctx:{CONVERSATION}"
    fake_cache.store[legacy_key] = legacy

    assert (await _exec("agent-p", "sb_legacy"))["error"]["code"] == "sandbox_access_denied"
    assert fake_cache.store[legacy_key] == legacy
    assert not [key for key in fake_cache.store if key.startswith("sandbox:bind:")]


@pytest.mark.asyncio
async def test_legacy_generic_binding_becomes_the_agents_generic_view(
    fake_cache, exec_client, local_sandbox_settings
) -> None:
    from packages.core.ai.runtime.sandbox import RuntimeSandboxKind, RuntimeSandboxScope, runtime_agent_sandbox

    fake_cache.store[f"sandbox:ctx:{CONVERSATION}"] = _legacy("agent-p", skill_id="")

    assert await _exec("agent-p", "sb_legacy") == "ok\n[exit_code: 0]"
    scope = RuntimeSandboxScope(ENTITY, USER, CONVERSATION)
    assert (await runtime_agent_sandbox(scope, "agent-p", RuntimeSandboxKind.GENERIC))[0] == "sb_legacy"
    assert await runtime_agent_sandbox(scope, "agent-p", RuntimeSandboxKind.SKILL) is None


@pytest.mark.asyncio
async def test_released_legacy_binding_does_not_resurface(fake_cache, exec_client, local_sandbox_settings) -> None:
    from packages.core.ai.tools import sandbox_tools

    fake_cache.store[f"sandbox:ctx:{CONVERSATION}"] = _legacy("agent-p")
    released = await sandbox_tools._sandbox_destroy(
        entity_id=ENTITY, user_id=USER, sandbox_id="sb_legacy",
        conversation_id=CONVERSATION, _agent_id_from_context="agent-p",
    )
    assert "released" in released
    # Not a warm session of the scope: destroyed, unbound for good.
    assert "destroyed" in released
    assert (await _exec("agent-p", "sb_legacy"))["error"]["code"] == "sandbox_access_denied"
    assert f"sandbox:ctx:{CONVERSATION}" not in fake_cache.store


# ── 6. guard the class: one scoped API for every reader and writer ──────

_BINDING_MODULE = "packages/core/ai/runtime/sandbox.py"

# Every production module that reads or writes sandbox bindings, mapped to
# the test that exercises its use. A new reader/writer must be added here
# (with a behavioural test) or this guard fails.
_BINDING_USERS = {
    "packages/core/ai/runtime/__init__.py": "re-export only",
    "packages/core/ai/tools/sandbox_tools.py": "test_delegated_sub_agent_shares_the_parents_sandbox",
    "packages/core/services/sandbox_session_service.py": "test_parallel_delegates_share_one_sandbox",
    "packages/core/ai/tools/extended_tools.py": (
        "tests/test_image_sandbox_delivery.py::test_never_writes_into_another_conversations_sandbox"
    ),
    "packages/core/ai/tools/generate_file/tool.py": (
        "tests/test_document_skill_media_guard.py::test_dispatch_ignores_another_agents_document_sandbox"
    ),
    "packages/core/ai/tools/bash_tool.py": (
        "tests/test_bash_tool_sandbox_bridge.py::test_bash_reuses_the_bound_generic_sandbox"
    ),
    "apps/api/routers/chat.py": (
        "tests/test_chat_mode_video_routing.py::test_live_owned_video_skill_sandbox_sets_structured_route_state"
    ),
    "packages/core/ai/tools/video_edit_tools.py": (
        "tests/test_video_edit_runtime.py::test_video_containers_are_admitted_in_the_callers_per_user_sandbox_scope"
    ),
}

# Derived from the binding module: every public binding function or class it
# defines. A new reader/writer added there is covered without editing this.
_BINDING_NAMES = tuple(
    sorted(
        set(
            re.findall(
                r"^(?:async def|def|class) ((?:runtime_|primary_)\w+|RuntimeSandboxScope)\b",
                (ROOT / _BINDING_MODULE).read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            )
        )
        # Pure helpers that never touch a binding.
        - {
            "runtime_sandbox_client",
            "runtime_sandbox_agent_key",
            "runtime_sandbox_agent_workdir",
            "runtime_sandbox_kind_of",
            "runtime_binding_is_live",
            "runtime_binding_was_released",
            "runtime_task_binding_key",
            "runtime_workflow_binding_key",
            "runtime_is_task_binding_key",
            "runtime_task_id_from_binding_key",
            # Work-area layout and key shapes: pure path/name helpers.
            "runtime_sandbox_area_key",
            "runtime_sandbox_layout",
            "runtime_sandbox_layout_for",
            "runtime_is_room_binding_key",
            "runtime_room_binding_key",
            "runtime_binding_skills",
        }
    )
)
_BINDING_API = re.compile(r"\b(" + "|".join(map(re.escape, _BINDING_NAMES)) + r")\b")


def _production_python_files() -> list[str]:
    return production_python_files(ROOT)


def test_the_binding_api_derivation_finds_every_reader_and_writer() -> None:
    expected = {
        "RuntimeSandboxScope",
        "primary_sandbox_scope_for",
        "runtime_sandbox_scope",
        "runtime_load_sandbox_binding",
        "runtime_save_sandbox_binding",
        "runtime_init_sandbox_binding",
        "runtime_delete_sandbox_binding",
        "runtime_expire_sandbox_binding",
        "runtime_agent_sandbox",
        "runtime_skill_sandbox",
        "runtime_release_agent_sandbox",
        "runtime_agent_holds_skill",
        "runtime_agent_skill_in",
        "runtime_agent_viewed_skill_in",
        "runtime_work_skill_sandbox",
    }
    assert expected <= set(_BINDING_NAMES)


def test_no_module_builds_a_binding_key_outside_the_scoped_api() -> None:
    offenders = []
    for path in _production_python_files():
        if path == _BINDING_MODULE:
            continue
        source = (ROOT / path).read_text(encoding="utf-8")
        for marker in ("sandbox:ctx", "sandbox:bind", "RUNTIME_SANDBOX_BINDING_PREFIX", "_LEGACY_SANDBOX_CONTEXT_PREFIX"):
            if marker in source:
                offenders.append(f"{path}: {marker}")
    assert offenders == []


def test_the_single_slot_and_per_agent_slot_apis_are_gone() -> None:
    removed = (
        "runtime_load_sandbox_context",
        "runtime_save_sandbox_context",
        "runtime_init_sandbox_context",
        "runtime_delete_sandbox_context",
        "runtime_sandbox_context_owner_matches",
        "RUNTIME_SANDBOX_CONTEXT_PREFIX",
        # The per-agent ownership slots of the previous model.
        "runtime_sandbox_scopes",
        "runtime_find_sandbox_binding",
    )
    offenders = [
        f"{path}: {name}"
        for path in _production_python_files()
        for name in removed
        if re.search(rf"\b{name}\b", (ROOT / path).read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_ownership_never_includes_the_acting_agent() -> None:
    from dataclasses import fields

    from packages.core.ai.runtime.sandbox import RuntimeSandboxScope
    from packages.core.services.sandbox_session_service import SandboxOwner

    assert "agent_id" not in {field.name for field in fields(RuntimeSandboxScope)}
    assert "agent_id" not in {field.name for field in fields(SandboxOwner)}
    source = (ROOT / "packages/core/services/sandbox_session_service.py").read_text(encoding="utf-8")
    assert ":agent:" not in source


def test_every_binding_reader_and_writer_is_registered_and_covered() -> None:
    users = {
        path
        for path in _production_python_files()
        if path != _BINDING_MODULE and _BINDING_API.search((ROOT / path).read_text(encoding="utf-8"))
    }
    assert users == set(_BINDING_USERS)
    for coverage in _BINDING_USERS.values():
        if "::" not in coverage:
            if coverage != "re-export only":
                assert f"def {coverage}(" in Path(__file__).read_text(encoding="utf-8"), coverage
            continue
        test_file, test_name = coverage.split("::")
        assert f"def {test_name}(" in (ROOT / test_file).read_text(encoding="utf-8"), coverage


def test_legacy_fallback_is_one_deletable_function() -> None:
    source = (ROOT / _BINDING_MODULE).read_text(encoding="utf-8")
    assert source.count("_LEGACY_SANDBOX_CONTEXT_PREFIX") == 2  # definition + one use
    assert source.count("_adopt_legacy_sandbox_binding(") == 2  # definition + one call
    assert "7 days after" in source


# ── 7. user-less runs are scoped by their task; a missing scope is denied ─

USERLESS_TASK = "01JTASKUSERLESS0000000000A"
OTHER_USERLESS_TASK = "01JTASKUSERLESS0000000000B"


def _userless_task_call(task: str = USERLESS_TASK, **extra):
    # What runtime injection hands a Strategist/scheduler task worker: no
    # user, the task id, the agent -- here even with the conversation it was
    # created from, which a user-less run must never share.
    return {
        "entity_id": ENTITY,
        "user_id": "",
        "task_id": task,
        "conversation_id": CONVERSATION,
        "_agent_id_from_context": "agent-s",
        **extra,
    }


@pytest.mark.asyncio
async def test_userless_task_operates_only_the_sandbox_of_its_own_task(
    fake_cache, exec_client, local_sandbox_settings, monkeypatch
) -> None:
    from packages.core.ai.runtime.sandbox import RuntimeSandboxScope, runtime_task_binding_key
    from packages.core.ai.tools import sandbox_tools

    exec_client.created_ids = ["sb_userless", "sb_human", "sb_other_task"]
    created = await sandbox_tools._sandbox_create(**_userless_task_call())
    assert "sandbox_id: sb_userless" in created
    # A human's sandbox in the conversation the task came from, and another task's.
    human = await _open_skill("agent-p", "pptx")
    other_task = await sandbox_tools._sandbox_create(**_userless_task_call(OTHER_USERLESS_TASK))
    assert (human, "sandbox_id: sb_other_task" in other_task) == ("sb_human", True)

    scope = RuntimeSandboxScope(ENTITY, "", runtime_task_binding_key(USERLESS_TASK))
    assert scope.principal == "system"
    assert fake_cache.store[scope.key("sbx", "sb_userless")]["sandbox_id"] == "sb_userless"

    own = await sandbox_tools._sandbox_exec(**_userless_task_call(sandbox_id="sb_userless", command="pwd"))
    assert own == "ok\n[exit_code: 0]"
    # Any other sandbox id -- another task's, a user's -- is denied.
    for victim in ("sb_human", "sb_other_task", "sb_victim"):
        foreign = json.loads(await sandbox_tools._sandbox_exec(**_userless_task_call(sandbox_id=victim, command="id")))
        assert foreign["error"]["code"] == "sandbox_access_denied", victim
    assert not [call for call in exec_client.calls if call.get("sandbox_id") in {"sb_human", "sb_other_task", "sb_victim"} and call["handler"] == "exec"]
    # ...and the human never reaches the task's sandbox.
    assert (await _exec("agent-p", "sb_userless"))["error"]["code"] == "sandbox_access_denied"

    released: list = []

    async def record_release(**kwargs):
        released.append(kwargs)
        return "destroyed"

    monkeypatch.setattr(sandbox_tools, "release_sandbox_session", record_release)
    denied = json.loads(await sandbox_tools._sandbox_destroy(**_userless_task_call(sandbox_id="sb_victim")))
    assert denied["error"]["code"] == "sandbox_access_denied"
    assert released == []
    assert "released" in await sandbox_tools._sandbox_destroy(**_userless_task_call(sandbox_id="sb_userless"))
    assert [call["sandbox_id"] for call in released] == ["sb_userless"]


def _action_params(action: str) -> dict:
    return {
        "exec": {"sandbox_id": "sb_any", "command": "id"},
        "status": {"sandbox_id": "sb_any", "execution_id": "exec-1"},
        "respond": {"sandbox_id": "sb_any", "execution_id": "exec-1", "event_id": "ev-1", "payload": {}},
        "cancel": {"sandbox_id": "sb_any", "execution_id": "exec-1"},
        "read_file": {"sandbox_id": "sb_any", "path": "/skill/a.txt"},
        "write_file": {"sandbox_id": "sb_any", "path": "/skill/a.txt", "content": "x"},
        "save_result": {"sandbox_id": "sb_any", "file_path": "/skill/a.txt", "filename": "a.txt"},
        "destroy": {"sandbox_id": "sb_any"},
        "create": {},
    }[action]


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", ["", "user_1"])
async def test_every_sandbox_action_without_a_scope_is_denied_before_runner_io(
    fake_cache, monkeypatch, user_id
) -> None:
    """Guard the class: derived from the action enum, so a new action is
    covered without editing this test. No conversation and no task means no
    binding scope, and nothing reaches a runner or the session service."""

    from packages.core.ai.runtime.composite_tools import SandboxToolAction
    from packages.core.ai.tools import sandbox_tools

    def forbid(*_args, **_kwargs):
        raise AssertionError("an unscoped call must not reach a sandbox")

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", forbid)
    monkeypatch.setattr(sandbox_tools, "_get_client", forbid)
    monkeypatch.setattr(sandbox_tools, "release_sandbox_session", forbid)
    monkeypatch.setattr(sandbox_tools, "acquire_sandbox_session", forbid)

    for action in SandboxToolAction:
        result = await sandbox_tools._sandbox_handler(
            entity_id=ENTITY,
            user_id=user_id,
            action=action.value,
            params=_action_params(action.value),
            _agent_id_from_context="agent-x",
        )
        payload = json.loads(result)
        assert payload["error"]["code"] == "sandbox_access_denied", action
        assert "no sandbox binding scope" in payload["error"]["message"], action


@pytest.mark.asyncio
@pytest.mark.parametrize("intruder", ["other_user", "other_entity", "userless_run_in_the_same_conversation"])
async def test_every_sandbox_action_on_a_foreign_sandbox_is_denied_before_runner_io(
    fake_cache, exec_client, local_sandbox_settings, monkeypatch, intruder
) -> None:
    """Guard the class: for every action, a sandbox of another user, another
    tenant, or user-less system work is denied by id before any runner I/O (the same
    user in another conversation shares it: see the test above)."""

    from packages.core.ai.runtime.composite_tools import SandboxToolAction
    from packages.core.ai.tools import sandbox_tools

    exec_client.created_ids = ["sb_any"]
    assert await _open_skill("agent-p", "pptx") == "sb_any"

    def forbid(*_args, **_kwargs):
        raise AssertionError("a foreign sandbox must not reach a runner")

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", forbid)
    monkeypatch.setattr(sandbox_tools, "release_sandbox_session", forbid)
    identity = {
        "other_user": {"entity_id": ENTITY, "user_id": OTHER_USER, "conversation_id": CONVERSATION},
        "userless_run_in_the_same_conversation": {"entity_id": ENTITY, "user_id": "", "conversation_id": CONVERSATION},
        "other_entity": {"entity_id": "ent_2", "user_id": USER, "conversation_id": CONVERSATION},
    }[intruder]
    for action in SandboxToolAction:
        if action.value == "create":
            continue
        params = _action_params(action.value)
        if action.value == "save_result":
            # save_result also takes a url; with a sandbox_id it is a sandbox read.
            params = {**params}
        result = await sandbox_tools._sandbox_handler(
            action=action.value, params=params, _agent_id_from_context="agent-p", **identity,
        )
        payload = json.loads(result)
        assert payload["error"]["code"] == "sandbox_access_denied", (intruder, action, payload)


@pytest.mark.asyncio
async def test_sandbox_skill_without_a_scope_is_refused_before_allocation(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from packages.core.config import get_settings
    from packages.core.services import skill_service

    def forbid(*_args, **_kwargs):
        raise AssertionError("no container may be admitted for an unscoped run")

    skill_dir = tmp_path / "skill_x"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# x", encoding="utf-8")
    (skill_dir / "run.py").write_text("print('ok')", encoding="utf-8")
    monkeypatch.setattr(skill_service, "acquire_sandbox_session", forbid)
    monkeypatch.setattr(get_settings(), "SANDBOX_SERVICE_URL", "http://sandbox-service")

    result = await skill_service._invoke_sandbox_skill(
        SimpleNamespace(
            id="skill_x", entity_id=None, name="x", slug="x", system_prompt="# x",
            config={"source": "builtin", "skill_dir": str(skill_dir)},
        ),
        ENTITY,
        None,
        "run",
        agent_id="agent-x",
        conversation_id=None,
        sandbox_binding_key=None,
    )

    assert result["stop_reason"] == "error"
    assert "no sandbox binding scope" in result["error"]


# ── 8. cancelling one run stops only its own command in the shared sandbox ─


@pytest.mark.asyncio
async def test_cancelling_one_agents_call_stops_only_its_own_execution(
    fake_cache, exec_client, local_sandbox_settings, monkeypatch
) -> None:
    from types import SimpleNamespace

    from packages.core.ai.tools import sandbox_tools

    exec_client.created_ids = ["sb_conversation"]
    exec_client.load_results = [_load_result("sb_conversation")]
    sandbox = await _open_skill("agent-a", "pptx")
    await _open_skill("agent-b", "pptx")

    started: dict[str, asyncio.Event] = {"agent-a": asyncio.Event(), "agent-b": asyncio.Event()}
    release_b = asyncio.Event()
    cancelled: list[tuple[str, str]] = []

    class BlockingClient(ExecViaStart):
        async def exec(self, *, sandbox_id, command, timeout, execution_id=None, **_placement):
            started[command].set()
            if command == "agent-b":
                await release_b.wait()
                return SimpleNamespace(stdout="b done", stderr="", exit_code=0)
            await asyncio.Event().wait()  # runs until its caller is cancelled

        async def cancel_execution(self, sandbox_id, execution_id):
            cancelled.append((sandbox_id, execution_id))

        async def close(self):
            return None

    async def client_for(_sandbox_id):
        return BlockingClient()

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", client_for)

    def call(agent: str):
        return sandbox_tools._sandbox_exec(
            entity_id=ENTITY, user_id=USER, sandbox_id=sandbox, command=agent,
            conversation_id=CONVERSATION, _agent_id_from_context=agent,
            _runtime_tool_call_id_from_context=f"call-{agent}",
        )

    a = asyncio.create_task(call("agent-a"))
    b = asyncio.create_task(call("agent-b"))
    await asyncio.wait_for(started["agent-a"].wait(), 5)
    await asyncio.wait_for(started["agent-b"].wait(), 5)

    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    a_execution = sandbox_tools._runtime_sandbox_execution_id(
        SimpleNamespace(runtime_run_id=None, runtime_tool_call_id="call-agent-a", runtime_tool_attempt=1), sandbox,
    )
    # Exactly agent-a's command was stopped; agent-b's keeps running.
    assert cancelled == [(sandbox, a_execution)]
    assert not b.done()
    release_b.set()
    assert "b done" in await b
