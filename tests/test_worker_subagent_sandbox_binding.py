"""Conversation-less task-worker runs bind their sandbox to a synthetic key.

A subagent launched by the task worker has no chat thread, so its
``conversation_id`` is None. Without a binding key nothing registers the
sandbox, the authorization recheck bails, and every ``sandbox`` call is denied
right after ``generate_file`` handed the tool over. These tests pin the
synthetic key end to end: the tool-context choke point that mints it as
``sandbox_binding_key`` (and only there -- it is never injected as a plain
``conversation_id`` kwarg), the skill handoff that carries it, the session
service that binds and scopes by it, the sandbox handlers' per-call owner
check, the authorization request for the sandbox tool, the approval origin
that must store the key so the worker's own approval resolves, and the shield
that keeps the key out of conversation lookups.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from tests.sandbox_exec_fakes import ExecViaStart  # noqa: E402


class FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.leases: dict[str, str] = {}
        self.available = True

    async def get(self, key):
        if not self.available:
            return None
        if key not in self.store:
            return None
        return json.loads(json.dumps(self.store[key]))

    async def set(self, key, value, ttl=300):
        if not self.available:
            return False
        self.store[key] = json.loads(json.dumps(value, default=str))
        return True

    async def delete(self, key):
        if not self.available:
            return False
        self.store.pop(key, None)
        return True

    async def acquire_lease(self, key, token, ttl=60):
        if not self.available:
            return None
        if key in self.leases:
            return False
        self.leases[key] = token
        return True

    async def release_lease(self, key, token):
        if not self.available:
            return False
        if self.leases.get(key) == token:
            del self.leases[key]
            return True
        return False

    async def extend_lease(self, key, token, ttl=60):
        if not self.available:
            return None
        if self.leases.get(key) != token:
            return False
        return True


@pytest.fixture
def fake_cache(monkeypatch):
    from packages.core import cache as cache_module

    fake = FakeCache()
    for name in (
        "get",
        "set",
        "delete",
        "acquire_lease",
        "release_lease",
        "extend_lease",
    ):
        monkeypatch.setattr(cache_module.cache, name, getattr(fake, name))
    return fake


# ── 1. tool context mints the key ───────────────────────────────


def test_tool_context_synthesizes_task_binding_key_when_no_conversation() -> None:
    from packages.core.ai.runtime.tool_context import (
        runtime_tool_call_context_from_kwargs,
    )

    actor = {"_user_id_from_context": "user1", "_agent_id_from_context": "agent1"}

    synthesized = runtime_tool_call_context_from_kwargs({**actor, "task_id": "01ABC"})
    # The key lives on its own field. The conversation stays real (absent), so
    # no unrelated handler persists or looks up a conversation that cannot exist.
    assert synthesized.conversation_id is None
    assert synthesized.sandbox_binding_key == "U01ABC"
    assert synthesized.task_id == "01ABC"

    # A real conversation always wins; the key is a fallback, not an override.
    real = runtime_tool_call_context_from_kwargs({**actor, "task_id": "01ABC", "conversation_id": "conv-1"})
    assert real.conversation_id == "conv-1"
    assert real.sandbox_binding_key == "conv-1"

    # Neither scope: still nothing to bind to.
    neither = runtime_tool_call_context_from_kwargs(actor)
    assert neither.conversation_id is None
    assert neither.sandbox_binding_key is None

    # A user-less task (Strategist/scheduler) is bound by its task too: there
    # is no identity-free path that skips the owner check any more.
    no_actor = runtime_tool_call_context_from_kwargs({"task_id": "01ABC"})
    assert no_actor.conversation_id is None
    assert no_actor.sandbox_binding_key == "U01ABC"
    assert runtime_tool_call_context_from_kwargs({}).sandbox_binding_key is None

    envelope = SimpleNamespace(
        entity_id="ent1",
        user_id="user1",
        agent_id="agent1",
        workspace_id=None,
        conversation_id=None,
        task_id="01ABC",
    )
    from_envelope = runtime_tool_call_context_from_kwargs({}, runtime_envelope=envelope)
    assert from_envelope.conversation_id is None
    assert from_envelope.sandbox_binding_key == "U01ABC"
    # The envelope keeps a null conversation so nothing persisted from it
    # (runtime_event_logs, token_usage_logs, billing_logs) sees the key.
    assert envelope.conversation_id is None


def test_workflow_sandbox_binding_preserves_identity_and_isolates_work() -> None:
    from packages.core.ai.runtime.sandbox import (
        primary_sandbox_scope_for,
        runtime_is_room_binding_key,
        runtime_task_id_from_binding_key,
    )
    from packages.core.ai.runtime.tool_context import (
        runtime_authorization_sandbox_binding_key,
        runtime_injected_tool_context_args,
        runtime_tool_call_context_from_kwargs,
    )

    def context(run_id="01M51P1YJSCBPQ2EYY261CJDMY", **kwargs):
        return runtime_tool_call_context_from_kwargs(
            {"_workflow_run_id_from_context": run_id, **kwargs}, entity_id="ent1",
        )

    workflow = context(_user_id_from_context="user1")
    key = workflow.sandbox_binding_key
    assert key and len(key) == 26
    assert workflow.conversation_id is None and workflow.task_id is None
    assert runtime_task_id_from_binding_key(key) is None
    assert not runtime_is_room_binding_key(key)
    assert context(_user_id_from_context="user1").sandbox_binding_key == key
    assert context("01M51P1YJSCBPQ2EYY261CJDMZ").sandbox_binding_key != key
    scope = primary_sandbox_scope_for(workflow)
    assert scope is not None and scope.user_id == "user1"
    assert runtime_authorization_sandbox_binding_key(workflow, "sandbox") == key
    assert runtime_authorization_sandbox_binding_key(workflow, "generate_file") is None
    injected = runtime_injected_tool_context_args(
        user_id=workflow.user_id, conversation_id=workflow.conversation_id, task_id=workflow.task_id,
    )
    assert key not in str(injected)

    # Chat and Task retain their established binding; a user-less workflow
    # owns its run even if its trigger has a chat reference.
    assert context(_user_id_from_context="user1", conversation_id="conv").sandbox_binding_key == "conv"
    assert context(task_id="01ABC").sandbox_binding_key == "U01ABC"
    assert context(conversation_id="conv").sandbox_binding_key == key
    other = context("01M51P1YJSCBPQ2EYY261CJDMZ")
    assert primary_sandbox_scope_for(context()).key("sbx", "one") != primary_sandbox_scope_for(other).key("sbx", "one")
    assert context("").sandbox_binding_key is None
    assert runtime_tool_call_context_from_kwargs({"workflow_run_id": "untrusted"}).sandbox_binding_key is None


def test_injected_handler_kwargs_never_carry_the_binding_key() -> None:
    """Every tool handler receives the real conversation, never the key.

    The injected ``conversation_id`` kwarg reaches every handler, so a key
    leaking into it would be stored as a child task's conversation, stamped on
    message_logs by send_email, and billed against credit_usage_logs.
    """

    from packages.core.ai.runtime.tool_context import (
        runtime_injected_tool_context_args,
        runtime_tool_call_context_from_kwargs,
    )

    worker = runtime_tool_call_context_from_kwargs(
        {
            "_user_id_from_context": "user1",
            "_agent_id_from_context": "agent1",
            "task_id": "01ABC",
        }
    )
    assert worker.sandbox_binding_key == "U01ABC"

    injected = runtime_injected_tool_context_args(
        agent_id=worker.agent_id,
        user_id=worker.user_id,
        workspace_id=worker.workspace_id,
        conversation_id=worker.conversation_id,
        task_id=worker.task_id,
    )
    assert injected.get("conversation_id") is None
    assert "U01ABC" not in str(injected)
    assert injected["task_id"] == "01ABC"

    # A real conversation is still injected untouched.
    real = runtime_tool_call_context_from_kwargs(
        {"_user_id_from_context": "user1", "task_id": "01ABC", "conversation_id": "conv-1"}
    )
    assert (
        runtime_injected_tool_context_args(
            user_id=real.user_id,
            conversation_id=real.conversation_id,
            task_id=real.task_id,
        )["conversation_id"]
        == "conv-1"
    )


def test_sandbox_authorization_request_carries_the_binding_key() -> None:
    """Only the sandbox tool's authorization request presents the key.

    It travels beside the real conversation, never in its place: approvals use
    the key as their scope, receipts and inner file guards the conversation.
    """

    from packages.core.ai.runtime.approvals import RuntimeApprovalRequest
    from packages.core.ai.runtime.tool_context import (
        RUNTIME_SANDBOX_BOUND_TOOL_NAMES,
        runtime_authorization_sandbox_binding_key,
        runtime_tool_call_context_from_kwargs,
    )

    worker = runtime_tool_call_context_from_kwargs(
        {
            "_user_id_from_context": "user1",
            "_agent_id_from_context": "agent1",
            "task_id": "01ABC",
        }
    )

    assert runtime_authorization_sandbox_binding_key(worker, "sandbox") == "U01ABC"
    assert runtime_authorization_sandbox_binding_key(worker, "sandbox_exec") == "U01ABC"
    # Everything else carries no binding.
    assert runtime_authorization_sandbox_binding_key(worker, "generate_file") is None
    assert runtime_authorization_sandbox_binding_key(worker, "workspace_create_task") is None
    assert runtime_authorization_sandbox_binding_key(worker, None) is None

    request = RuntimeApprovalRequest(
        tool_name="sandbox", arguments={}, entity_id="ent1", user_id="user1",
        conversation_id=worker.conversation_id,
        sandbox_binding_key=runtime_authorization_sandbox_binding_key(worker, "sandbox"),
    )
    assert request.conversation_id is None
    assert request.approval_scope_id == "U01ABC"

    # Every sandbox alias is covered, so a new one cannot fall out of the set.
    from packages.core.ai.runtime.capabilities import _TOOL_CAPABILITY_ALIAS_TARGETS

    assert RUNTIME_SANDBOX_BOUND_TOOL_NAMES >= {
        name for name, target in _TOOL_CAPABILITY_ALIAS_TARGETS.items() if target == "sandbox"
    } | {"sandbox"}

    real = runtime_tool_call_context_from_kwargs(
        {"_user_id_from_context": "user1", "task_id": "01ABC", "conversation_id": "conv-1"}
    )
    assert runtime_authorization_sandbox_binding_key(real, "sandbox") == "conv-1"
    assert runtime_authorization_sandbox_binding_key(real, "generate_file") is None


def test_task_binding_key_helpers_round_trip() -> None:
    from packages.core.ai.runtime import (
        runtime_is_task_binding_key,
        runtime_task_binding_key,
        runtime_task_id_from_binding_key,
    )

    assert runtime_task_binding_key("01ABC") == "U01ABC"
    assert runtime_task_binding_key("  01ABC  ") == "U01ABC"
    assert runtime_task_binding_key("") == ""
    assert runtime_is_task_binding_key("U01ABC") is True
    assert runtime_is_task_binding_key("conv-1") is False
    assert runtime_is_task_binding_key(None) is False

    # A short or non-ULID id never lost a character, so it comes back verbatim.
    assert runtime_task_id_from_binding_key("U01ABC") == "01ABC"
    assert runtime_task_id_from_binding_key("conv-1") is None
    assert runtime_task_id_from_binding_key(None) is None

    # No ULID can start with "U": Crockford base32 omits I, L, O and U, so the
    # two namespaces cannot collide.
    assert "U" not in "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def test_task_binding_key_fits_every_conversation_column() -> None:
    """No conversation column in the schema can truncate the key.

    The key is stored, not shielded: it reaches media_jobs, usage logs, and
    anything else that records the run's conversation scope. A column narrower
    than the key would raise StringDataRightTruncation at that write, so this
    walks the whole mapped schema rather than the sites known today.
    """

    import packages.core.models  # noqa: F401 - registers every table on Base
    from packages.core.ai.runtime import (
        runtime_task_binding_key,
        runtime_task_id_from_binding_key,
    )
    from packages.core.models.base import Base

    task_id = "01JQTASKAAAAAAAAAAAAAAAAAA"
    assert len(task_id) == 26
    key = runtime_task_binding_key(task_id)
    assert len(key) == 26
    assert key.startswith("U")
    # A real ULID's leading character stays "0" until December 3084, so the
    # one character the prefix displaces is recoverable.
    assert runtime_task_id_from_binding_key(key) == task_id

    inspected: list[str] = []
    too_narrow: list[str] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if column.name != "conversation_id" and not column.name.endswith("_conversation_id"):
                continue
            length = getattr(column.type, "length", None)
            if not length:
                continue
            inspected.append(f"{table.name}.{column.name}({length})")
            if len(key) > length:
                too_narrow.append(f"{table.name}.{column.name}({length})")

    assert inspected, "no conversation columns found; is the model import right?"
    assert not too_narrow, f"binding key {key!r} ({len(key)} chars) does not fit: " + ", ".join(sorted(too_narrow))


# ── 2. generate_file hands the key to the skill ─────────────────


class _FakeSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_exc):
        return None


def _office_skill_ready(monkeypatch, captured: dict) -> None:
    from packages.core import database
    from packages.core.ai.tools import generate_file_tool
    from packages.core.ai.tools.generate_file import office_common
    from packages.core.services import skill_service

    async def fake_scope(**kwargs):
        return kwargs.get("name") or "artifact.xlsx"

    async def fake_list_skills(_db, _entity_id):
        return []

    async def fake_invoke(_db, skill, _entity_id, _input_text, **kwargs):
        captured["skill"] = skill
        captured["kwargs"] = kwargs
        return {
            "skill": skill,
            "content": "✓ Sandbox ready for skill xlsx",
            "usage": {},
            "tools_used": [],
            "rounds": 0,
            "stop_reason": "sandbox_ready",
            "sandbox_id": "sandbox-9",
        }

    monkeypatch.setattr(database, "async_session", lambda: _FakeSession())
    monkeypatch.setattr(skill_service, "list_skills", fake_list_skills)
    monkeypatch.setattr(office_common, "runtime_invoke_skill", fake_invoke)
    monkeypatch.setattr(generate_file_tool, "_scope_workspace_output_name", fake_scope)


@pytest.mark.parametrize(
    "run_context",
    [{"task_id": "01ABC"}, {"_workflow_run_id_from_context": "01M51P1YJSCBPQ2EYY261CJDMY"}],
    ids=["task", "workflow"],
)
@pytest.mark.asyncio
async def test_generate_file_office_kind_binds_sandbox_for_worker_run(
    monkeypatch, run_context,
) -> None:
    from packages.core.ai.runtime.envelope import RuntimeDiscoveredToolGrants
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs
    from packages.core.ai.tools import generate_file_tool

    captured: dict = {}
    _office_skill_ready(monkeypatch, captured)
    envelope = SimpleNamespace(discovered_tool_grants=RuntimeDiscoveredToolGrants())

    result = await generate_file_tool._generate_file_handler(
        entity_id="ent1",
        user_id="user1",
        kind="spreadsheet",
        name="crm.xlsx",
        prompt="Consolidate partner leads into a CRM workbook",
        **run_context,
        _runtime_envelope_from_context=envelope,
        _agent_id_from_context="agent1",
    )

    payload = json.loads(result)
    assert payload["status"] == "sandbox_ready"
    assert payload["loaded_tools"] == ["sandbox"]
    assert envelope.discovered_tool_grants.tool_names == {"sandbox"}
    # The real conversation binding is written by the session service inside
    # invoke_skill (stubbed here); what matters is the key that reaches it.
    # It travels on its own keyword: the plain conversation stays absent so a
    # nested prompt-skill loop cannot forward the key into other tool calls.
    expected = runtime_tool_call_context_from_kwargs(run_context).sandbox_binding_key
    assert expected and captured["kwargs"]["sandbox_binding_key"] == expected
    assert captured["kwargs"]["conversation_id"] is None
    assert captured["kwargs"]["task_id"] == run_context.get("task_id")


# ── 3. the session service binds and scopes by the key ──────────


def _fake_create_result(sandbox_id: str, *, scripts=None):
    return SimpleNamespace(
        sandbox_id=sandbox_id,
        container_name=f"skill-sbx-{sandbox_id}",
        status="running",
        workdir="/skill",
        env_blocked=[],
        skill=SimpleNamespace(
            entry_hint=None,
            scripts=scripts or [],
            requirements_txt=None,
        ),
    )


class ScriptedSandboxClient:
    """Minimal sandbox client script for one admission."""

    calls: list[dict] = []
    created_ids: list[str] = []

    @classmethod
    def reset(cls) -> None:
        cls.calls = []
        cls.created_ids = ["sbx-1"]

    def __init__(self, *, base_url: str, timeout: float, api_token: str | None = None):
        type(self).calls.append({"handler": "init", "base_url": base_url})

    async def health(self):
        type(self).calls.append({"handler": "health"})
        return {"status": "ok", "sandbox_image_available": True}

    async def create_from_builtin(self, skill_name, files, **kwargs):
        type(self).calls.append({"handler": "create_from_builtin", "skill_name": skill_name})
        await asyncio.sleep(0)
        return _fake_create_result(type(self).created_ids.pop(0), scripts=["run.py"])

    async def create_from_files(self, skill_name, files, **kwargs):
        type(self).calls.append({"handler": "create_from_files", "skill_name": skill_name})
        await asyncio.sleep(0)
        return _fake_create_result(type(self).created_ids.pop(0), scripts=["run.py"])

    async def status(self, sandbox_id):
        type(self).calls.append({"handler": "status", "sandbox_id": sandbox_id})
        return SimpleNamespace(status="ready", workdir="/skill")

    async def destroy(self, sandbox_id):
        type(self).calls.append({"handler": "destroy", "sandbox_id": sandbox_id})

    async def close(self):
        type(self).calls.append({"handler": "close"})


def _task_skill_scope(*, user_id="user1"):
    from packages.core.ai.runtime.sandbox import RuntimeSandboxScope

    return RuntimeSandboxScope("ent1", user_id, "U01ABC")


async def _bind_task_skill_sandbox(sandbox_id="sbx-1", *, agent_id="agent1", **scope_kwargs):
    from packages.core.ai.runtime import runtime_init_sandbox_binding

    scope = _task_skill_scope(**scope_kwargs)
    await runtime_init_sandbox_binding(scope, sandbox_id, "skill_xlsx", agent_id=agent_id)
    return scope


@pytest.fixture
def scripted_client(monkeypatch):
    import packages.core.services.sandbox_sdk as sandbox_sdk

    ScriptedSandboxClient.reset()
    monkeypatch.setattr(sandbox_sdk, "SandboxClient", ScriptedSandboxClient)
    return ScriptedSandboxClient


@pytest.fixture
def local_sandbox_settings(monkeypatch):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "SANDBOX_SERVICE_URL", "http://sandbox-service")
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", "local")
    monkeypatch.setattr(settings, "SANDBOX_QUEUE_POLL_SECONDS", 1)
    monkeypatch.setattr(settings, "SANDBOX_QUEUE_MAX_WAIT_SECONDS", 3)
    return settings


@pytest.mark.asyncio
async def test_session_service_binds_task_key_and_scopes_by_it(
    fake_cache, scripted_client, local_sandbox_settings
) -> None:
    from packages.core.ai.runtime import runtime_load_sandbox_binding
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxBundle,
        SandboxOwner,
        SandboxSession,
        acquire_sandbox_session,
    )

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent1", user_id="user1"),
        bundle=SandboxBundle(
            skill_name="xlsx",
            files={"SKILL.md": "# skill", "run.py": "print('ok')"},
            env={},
            allowed_sensitive_keys=(),
            source="builtin",
            config=None,
            auto_install=True,
        ),
        binding=SandboxBinding(
            conversation_id="U01ABC",
            agent_id="agent1",
            skill_id="skill_xlsx",
        ),
    )

    assert isinstance(session, SandboxSession)
    assert session.sandbox_id == "sbx-1"
    # ``:`` is percent-encoded, so the key cannot forge another scope.
    # A task of a human user uses the user's one general sandbox.
    assert fake_cache.store["sandbox:owner:ent1:user1:user"]["sandbox_id"] == "sbx-1"

    ctx = await runtime_load_sandbox_binding(_task_skill_scope(), "sbx-1")
    assert ctx is not None
    assert ctx["sandbox_id"] == "sbx-1"
    assert ctx["entity_id"] == "ent1"
    assert ctx["user_id"] == "user1"
    assert ctx["created_by_agent"] == "agent1"


# ── 4. the per-call owner check accepts the key ─────────────────


@pytest.mark.asyncio
async def test_sandbox_exec_access_check_accepts_task_binding(fake_cache) -> None:
    from packages.core.ai.runtime import runtime_sandbox_scope
    from packages.core.ai.tools import sandbox_tools

    scope = await _bind_task_skill_sandbox()
    assert scope.key("sbx", "sbx-1") in fake_cache.store

    def _scope(**overrides):
        return runtime_sandbox_scope(
            **{
                "entity_id": "ent1",
                "user_id": "user1",
                "binding_key": "U01ABC",
                **overrides,
            }
        )

    assert await sandbox_tools._sandbox_instance_access_error(sandbox_id="sbx-1", scope=_scope()) is None
    # Another task (or conversation) of the same user shares the user's sandbox.
    assert await sandbox_tools._sandbox_instance_access_error(
        sandbox_id="sbx-1", scope=_scope(binding_key="U01OTHER"),
    ) is None

    for wrong_scope, wrong_sandbox in (
        ({"user_id": "user2"}, "sbx-1"),
        ({"user_id": ""}, "sbx-1"),
        ({"user_id": "", "binding_key": "U01OTHER"}, "sbx-1"),
        ({"entity_id": "ent2"}, "sbx-1"),
        ({}, "sbx-other"),
    ):
        error = await sandbox_tools._sandbox_instance_access_error(
            sandbox_id=wrong_sandbox, scope=_scope(**wrong_scope),
        )
        assert isinstance(error, str) and error, (wrong_scope, wrong_sandbox)


@pytest.mark.asyncio
async def test_sandbox_handlers_use_binding_key_for_access(fake_cache, local_sandbox_settings, monkeypatch) -> None:
    """The handlers read the key off the context, not off ``conversation_id``.

    Runtime injection hands these handlers the run's real conversation, which
    for a task worker is empty. If a handler kept scoping by that argument the
    owner check would find no context and deny every call.
    """

    from packages.core.ai.tools import sandbox_tools
    from packages.core.services.sandbox_session_service import SandboxSession

    skill_scope = await _bind_task_skill_sandbox()

    class _StubClient(ExecViaStart):
        async def exec(self, *, sandbox_id, command, timeout, execution_id=None, **_placement):
            return SimpleNamespace(stdout="hi", stderr="", exit_code=0)

        async def close(self):
            return None

    async def fake_client(_sandbox_id):
        return _StubClient()

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", fake_client)

    exec_result = await sandbox_tools._sandbox_exec(
        entity_id="ent1",
        user_id="user1",
        sandbox_id="sbx-1",
        command="echo hi",
        task_id="01ABC",
        _agent_id_from_context="agent1",
    )
    assert "hi" in exec_result
    assert "ACCESS_DENIED" not in exec_result

    # Creation binds by the key too, and its owner-context guard accepts the
    # run instead of refusing it for want of a conversation. A generic create
    # refuses to run over a bound Skill sandbox, so retire the one above first.
    from packages.core.ai.runtime import runtime_delete_sandbox_binding

    assert await runtime_delete_sandbox_binding(skill_scope, "sbx-1")
    captured: dict = {}

    async def fake_acquire(*, owner, bundle, binding, **kwargs):
        captured["binding"] = binding
        captured["owner"] = owner
        return SandboxSession(
            sandbox_id="sbx-2",
            base_url="http://sandbox-service",
            created_at=0.0,
            last_acquired_at=0.0,
        )

    monkeypatch.setattr(sandbox_tools, "acquire_sandbox_session", fake_acquire)

    create_result = await sandbox_tools._sandbox_create(
        entity_id="ent1",
        user_id="user1",
        task_id="01ABC",
        _agent_id_from_context="agent1",
    )
    assert "sandbox_id:" in create_result
    assert captured["binding"].conversation_id == "U01ABC"
    assert captured["binding"].agent_id == "agent1"


# ── 5. the authorization verifier accepts the key ───────────────


@pytest.mark.asyncio
async def test_binding_verifier_accepts_task_binding_key(fake_cache, monkeypatch) -> None:
    from packages.core.services import skill_service
    from packages.core.services.runtime_authorization.binding_verifier import (
        CurrentRuntimeToolBindingVerifier,
    )
    from packages.core.services.runtime_authorization.domain import (
        RuntimeAuthorizationRequest,
        RuntimeToolBindingSource,
    )

    await _bind_task_skill_sandbox()

    async def fake_list_skills_for_agent(_db, _entity_id, _agent_id, **_kwargs):
        return [SimpleNamespace(id="skill_xlsx")]

    monkeypatch.setattr(skill_service, "list_skills_for_agent", fake_list_skills_for_agent)

    def _request(agent_id: str) -> RuntimeAuthorizationRequest:
        return RuntimeAuthorizationRequest.create(
            entity_id="ent1",
            user_id="user1",
            workspace_id=None,
            action_key="sandbox.exec",
            capability_id="sandbox.exec",
            principal_agent_id=agent_id,
            tool_name="sandbox",
            conversation_id="U01ABC",
            task_id="01ABC",
        )

    # The fast path never touches storage, so a session that would explode on
    # use proves the grant came from the sandbox binding alone.
    db = SimpleNamespace()
    decision = await CurrentRuntimeToolBindingVerifier().verify(db, _request("agent1"))
    assert decision.source is RuntimeToolBindingSource.CONTEXTUAL

    # The Sandbox handler owns the per-instance check: the sandbox belongs to
    # the user x binding key, so every agent of that scope may use it, and
    # another user or another binding key may not.
    from packages.core.ai.runtime.sandbox import runtime_sandbox_scope
    from packages.core.ai.tools.sandbox_tools import _sandbox_instance_access_error

    def _scope(user_id: str = "user1", binding_key: str = "U01ABC"):
        return runtime_sandbox_scope(entity_id="ent1", user_id=user_id, binding_key=binding_key)

    assert await _sandbox_instance_access_error(sandbox_id="sbx-1", scope=_scope()) is None
    assert await _sandbox_instance_access_error(sandbox_id="sbx-1", scope=_scope(user_id="user2")) is not None
    # The same user's other task shares it (one sandbox per user).
    assert await _sandbox_instance_access_error(sandbox_id="sbx-1", scope=_scope(binding_key="U01OTHER")) is None
    assert await _sandbox_instance_access_error(
        sandbox_id="sbx-1", scope=_scope(user_id="", binding_key="U01OTHER"),
    ) is not None


# ── 6 & 7. the shield, and the approval round trip ──────────────


@pytest.mark.asyncio
async def test_load_conversation_runtime_context_ignores_task_binding_key() -> None:
    from packages.core.services.workspace_runtime import (
        load_conversation_runtime_context,
    )

    calls: list = []

    async def never(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a synthetic binding key must not hit the database")

    db = SimpleNamespace(execute=never)

    assert await load_conversation_runtime_context(db, conversation_id="U01ABC", entity_id="ent1") == {}
    assert calls == []


@pytest.mark.asyncio
async def test_worker_sandbox_approval_round_trips_on_the_binding_key() -> None:
    """The approval a worker's sandbox call mints is findable again by its key.

    The sandbox tool presents the binding key as its authorization scope, so
    that is what lands on ``hitl_requests.origin_conversation_id``.
    ``_load_runtime_request`` matches on exactly that column, so dropping the
    key at mint time (it fits the VARCHAR(26)) would leave the worker's own
    approval unresolvable while still looking granted.
    """

    from packages.core.ai.runtime.approval_service import (
        _RUNTIME_ORIGIN_KIND,
        _load_runtime_request,
    )

    req = SimpleNamespace(
        id="req-1",
        entity_id="ent1",
        origin_kind=_RUNTIME_ORIGIN_KIND,
        origin_conversation_id="U01ABC",
        origin_task_id="01ABC",
    )

    class _Result:
        def scalar_one_or_none(self):
            return req

    compiled: list[str] = []

    class _Db:
        async def execute(self, query):
            # The entity/id filter lives in SQL; record it so this stays a
            # test of the whole lookup and not just the Python guard.
            compiled.append(str(query))
            return _Result()

    db = _Db()

    found = await _load_runtime_request(db, request_id="req-1", entity_id="ent1", conversation_id="U01ABC")
    assert found is req

    # Another run's key must not reach this request.
    assert await _load_runtime_request(db, request_id="req-1", entity_id="ent1", conversation_id="U01XYZ") is None

    # A step-origin card stays invisible to the runtime resolver chain.
    req.origin_kind = "step"
    assert await _load_runtime_request(db, request_id="req-1", entity_id="ent1", conversation_id="U01ABC") is None

    assert compiled and "hitl_requests.entity_id" in compiled[0]


@pytest.mark.asyncio
async def test_invoke_skill_action_threads_the_binding_key(monkeypatch) -> None:
    """``invoke_skill`` receives the key on its own keyword, not as the conversation.

    ``invoke_skill`` also drives prompt-skill loops whose nested tool executor
    forwards ``conversation_id`` into every nested tool call. Substituting the
    key there would spread it well past sandbox admission, so the sandbox scope
    travels separately and the real (absent) conversation is left alone.
    """

    from packages.core import database
    from packages.core.ai.runtime import skills as runtime_skills
    from packages.core.ai.runtime.envelope import RuntimeDiscoveredToolGrants
    from packages.core.ai.runtime.tool_context import (
        runtime_tool_call_context_from_kwargs,
    )
    from packages.core.services import skill_service

    captured: dict = {}

    async def fake_get_skill(_db, _skill_id):
        return SimpleNamespace(
            id="skill_xlsx",
            slug="xlsx",
            name="xlsx",
            status="active",
            entity_id=None,
        )

    async def fake_runtime_invoke_skill(_db, skill_id, _entity_id, _input_text, **kwargs):
        captured["skill_id"] = skill_id
        captured["kwargs"] = kwargs
        return {
            "skill": "xlsx",
            "content": "sandbox instructions",
            "usage": {},
            "stop_reason": "sandbox_ready",
            "sandbox_id": "sandbox-1",
        }

    monkeypatch.setattr(database, "async_session", lambda: _FakeSession())
    monkeypatch.setattr(skill_service, "get_skill", fake_get_skill)
    monkeypatch.setattr(runtime_skills, "runtime_invoke_skill", fake_runtime_invoke_skill)

    envelope = SimpleNamespace(discovered_tool_grants=RuntimeDiscoveredToolGrants())
    runtime_context = runtime_tool_call_context_from_kwargs(
        {
            "_user_id_from_context": "user1",
            "_agent_id_from_context": "agent-1",
            "task_id": "01ABC",
            "_runtime_envelope_from_context": envelope,
        }
    )
    assert runtime_context.conversation_id is None
    assert runtime_context.sandbox_binding_key == "U01ABC"

    result = await runtime_skills.runtime_invoke_skill_action(
        entity_id="ent1",
        user_id="user1",
        skill="xlsx",
        runtime_context=runtime_context,
    )

    payload = json.loads(result)
    assert payload["status"] == "sandbox_ready"
    assert payload["loaded_tools"] == ["sandbox"]
    assert envelope.discovered_tool_grants.tool_names == {"sandbox"}

    assert captured["kwargs"]["conversation_id"] is None
    assert captured["kwargs"]["sandbox_binding_key"] == "U01ABC"
    assert captured["kwargs"]["task_id"] == "01ABC"
    assert captured["kwargs"]["agent_id"] == "agent-1"


async def _guard_sandbox_exec_for_task_origin(
    monkeypatch, *, origin_conversation_id, presented_conversation_id,
    tool_name="sandbox", runtime_envelope=None,
):
    """Run the guard for a worker task's ``sandbox exec`` and capture the origin.

    ``_load_runtime_request`` matches ``origin_conversation_id``, so an origin
    minted with anything but the binding key leaves the worker's own approval
    unresolvable. Patch ``resolve_approval`` — the one consumer of the origin —
    and read back what the guard handed it.
    """

    from packages.core import database
    from packages.core.ai.runtime import approval_service
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.governance import approvals as governance_approvals
    from packages.core.services import runtime_authorization
    from packages.core.services.runtime_authorization import PermissionDecision

    # A worker task (not a general chat-comment task). A task created through
    # the API has no conversation row at all; its sandbox tools present the
    # synthetic binding key as their authorization scope instead.
    origin_task = SimpleNamespace(
        id="01ABC",
        status="in_progress",
        workspace_id=None,
        task_type="agent",
        conversation_id=origin_conversation_id,
    )

    class _Db:
        async def commit(self):
            return None

        async def execute(self, _query):
            # The guard loads (and locks) the origin task before minting.
            return SimpleNamespace(scalar_one_or_none=lambda: origin_task)

    @asynccontextmanager
    async def fake_session():
        yield _Db()

    from packages.core.services import task_runtime_approvals

    async def no_lock(_db, *, task_id):
        assert task_id == "01ABC"

    monkeypatch.setattr(task_runtime_approvals, "lock_task_runtime_approval_boundary", no_lock)

    async def allow(*_args, **_kwargs):
        # The real decision type: the guard reads its collaborator scope too.
        return PermissionDecision.allow("allowed")

    origins: list = []

    async def capture_resolve(_db, *, origin, **_kwargs):
        origins.append(origin)
        return SimpleNamespace(
            outcome=governance_approvals.ApprovalOutcome.ALLOW,
            request=None,
        )

    monkeypatch.setattr(database, "async_session", fake_session)
    monkeypatch.setattr(runtime_authorization, "authorize_runtime_action", allow)
    monkeypatch.setattr(governance_approvals, "resolve_approval", capture_resolve)

    arguments = (
        {"action": "exec", "sandbox_id": "sbx-1", "command": "ls"}
        if tool_name == "sandbox" else {"outcome": "blocked", "summary": "blocked"}
    )
    classification = classify_runtime_tool(tool_name, arguments, entity_id="ent1", workspace_id=None, task_id="01ABC")

    result = await approval_service.guard_runtime_tool_action(
        name=tool_name,
        arguments=arguments,
        entity_id="ent1",
        user_id="user1",
        workspace_id=None,
        conversation_id=presented_conversation_id,
        task_id="01ABC",
        runtime_envelope=runtime_envelope,
        classification=classification,
        operation_id=f"runtime:01ABC:call:{tool_name}",
    )
    return result, origins


@pytest.mark.asyncio
async def test_worker_result_remains_executable_after_task_thread_is_created(monkeypatch) -> None:
    worker = SimpleNamespace(
        surface="scheduled_agent_run", profile="default", task_id="01ABC", conversation_id=None,
    )
    result, origins = await _guard_sandbox_exec_for_task_origin(
        monkeypatch,
        origin_conversation_id="conv-1",
        presented_conversation_id=None,
        tool_name="complete_task",
        runtime_envelope=worker,
    )
    assert result is None
    assert origins == []


@pytest.mark.asyncio
async def test_only_same_task_worker_can_submit_without_conversation(monkeypatch) -> None:
    for envelope in (
        None,
        SimpleNamespace(surface="task_comment_thread", profile="default", task_id="01ABC", conversation_id=None),
        SimpleNamespace(surface="scheduled_agent_run", profile="default", task_id="01XYZ", conversation_id=None),
    ):
        result, origins = await _guard_sandbox_exec_for_task_origin(
            monkeypatch,
            origin_conversation_id="conv-1",
            presented_conversation_id=None,
            tool_name="complete_task",
            runtime_envelope=envelope,
        )
        assert json.loads(result)["error"] == "approval_origin_not_executable"
        assert origins == []


@pytest.mark.asyncio
async def test_guard_runtime_tool_action_stores_the_binding_key_on_the_origin(monkeypatch) -> None:
    """The MINT side of the round trip: the key reaches ``ApprovalOrigin``.

    The origin Task has no conversation (it was created through the API and
    runs on a worker), and the sandbox tool presents its binding key as the
    authorization scope. The task-origin check must accept that key: refusing
    it with ``approval_origin_not_executable`` leaves every conversation-less
    task unable to run a single sandbox command.
    """

    result, origins = await _guard_sandbox_exec_for_task_origin(
        monkeypatch, origin_conversation_id=None, presented_conversation_id="U01ABC",
    )

    assert result is None
    assert len(origins) == 1
    assert origins[0].conversation_id == "U01ABC"
    assert origins[0].task_id == "01ABC"


@pytest.mark.asyncio
async def test_guard_runtime_tool_action_refuses_a_foreign_scope_for_a_task_origin(monkeypatch) -> None:
    """Accepting the binding key must not loosen the origin check for anything else."""

    import json

    for origin_conversation_id, presented in (
        (None, "U01XYZ"),  # another task's binding key
        (None, "conv-1"),  # a real conversation the task never had
        ("conv-1", "U01ABC"),  # the task's own key is still bound to that task
        ("conv-1", "conv-2"),  # a different conversation
    ):
        result, origins = await _guard_sandbox_exec_for_task_origin(
            monkeypatch,
            origin_conversation_id=origin_conversation_id,
            presented_conversation_id=presented,
        )
        expect_refusal = presented not in {"U01ABC", origin_conversation_id}
        if expect_refusal:
            assert json.loads(result)["error"] == "approval_origin_not_executable", (origin_conversation_id, presented)
            assert origins == []
        else:
            assert result is None, (origin_conversation_id, presented)
            assert origins[0].conversation_id == presented
