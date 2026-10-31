from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest


class FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.leases: dict[str, str] = {}
        self.extended: list[tuple[str, str, int]] = []
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
        self.extended.append((key, token, ttl))
        return True


@pytest.fixture
def fake_cache(monkeypatch):
    from packages.core import cache as cache_module

    fake = FakeCache()
    for name in ("get", "set", "delete", "acquire_lease", "release_lease", "extend_lease"):
        monkeypatch.setattr(cache_module.cache, name, getattr(fake, name))
    return fake


def test_owner_requires_entity_and_builds_registry_key() -> None:
    from packages.core.services.sandbox_session_service import SandboxOwner

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    assert owner.registry_key("conversation:c1") == "sandbox:owner:ent_1:user_1:conversation:c1"
    assert owner.lock_key("conversation:c1") == "sandbox:owner:ent_1:user_1:conversation:c1:lock"
    assert owner.registrable is True

    # Every scope is conversation-prefixed; there is no bare owner key.
    with pytest.raises(ValueError):
        owner.registry_key("")
    with pytest.raises(ValueError):
        owner.lock_key("")

    # User-less system work is owned by its workspace (attribution) and keyed
    # as ``system``: its scope (one task) isolates it, and it registers too.
    userless = SandboxOwner(entity_id="ent_1", user_id="", workspace_id="ws_1")
    assert userless.registrable is True
    assert userless.registry_key("conversation:Utask") == "sandbox:owner:ent_1:system:conversation:Utask"
    # A human owner never carries a workspace: it cannot widen the key.
    assert SandboxOwner(entity_id="ent_1", user_id="user_1", workspace_id="ws_1").workspace_id == ""
    with pytest.raises(ValueError):
        SandboxOwner(entity_id="", user_id="user_1")


@pytest.mark.asyncio
async def test_registry_round_trip_and_forget(fake_cache) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSession,
        forget_sandbox_session,
        load_sandbox_session,
        register_sandbox_session,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    scope = "conversation:conversation_1"
    assert await load_sandbox_session(owner, scope=scope) is None

    session = SandboxSession(
        sandbox_id="sb_1",
        base_url="http://sandbox-service",
        created_at=100.0,
        last_acquired_at=100.0,
        loaded_skills=["xlsx"],
    )
    assert await register_sandbox_session(owner, session, scope=scope) is True
    assert "sandbox:owner:ent_1:user_1:conversation:conversation_1" in fake_cache.store

    loaded = await load_sandbox_session(owner, scope=scope)
    assert loaded is not None
    assert loaded.sandbox_id == "sb_1"
    assert loaded.base_url == "http://sandbox-service"
    assert loaded.loaded_skills == ["xlsx"]
    assert loaded.reused is False

    # Another conversation's scope is a different key entirely.
    assert await load_sandbox_session(owner, scope="conversation:conversation_2") is None

    await forget_sandbox_session(owner, "sb_other", scope=scope)
    assert (await load_sandbox_session(owner, scope=scope)) is not None
    await forget_sandbox_session(owner, "sb_1", scope=scope)
    assert await load_sandbox_session(owner, scope=scope) is None


def test_session_error_marks_only_capacity_and_busy_as_retryable() -> None:
    from packages.core.services.sandbox_session_service import (
        SANDBOX_SESSION_ERROR_CODES,
        SandboxSessionError,
    )

    assert SANDBOX_SESSION_ERROR_CODES == frozenset(
        {
            "not_configured",
            "image_unavailable",
            "capacity_full",
            "busy_timeout",
            "create_failed",
            "context_persist_failed",
            "nested_allocation_unavailable",
        }
    )
    assert SandboxSessionError.of("capacity_full", "full").retryable is True
    assert SandboxSessionError.of("nested_allocation_unavailable", "nested").retryable is False
    assert SandboxSessionError.of("busy_timeout", "busy").retryable is True
    assert SandboxSessionError.of("create_failed", "boom").retryable is False
    with pytest.raises(ValueError):
        SandboxSessionError.of("unknown", "x")


def test_owner_normalizes_ids_and_rejects_colons() -> None:
    from packages.core.services.sandbox_session_service import SandboxOwner

    owner = SandboxOwner(entity_id="  ent_1 ", user_id=" user_1 ")
    assert owner.registry_key("conversation:c1") == "sandbox:owner:ent_1:user_1:conversation:c1"

    with pytest.raises(ValueError):
        SandboxOwner("a:b", "c")
    with pytest.raises(ValueError):
        SandboxOwner("a", "b:c")


def test_retryable_codes_are_a_subset_of_error_codes() -> None:
    from packages.core.services.sandbox_session_service import (
        SANDBOX_SESSION_ERROR_CODES,
        _RETRYABLE_CODES,
    )

    assert _RETRYABLE_CODES <= SANDBOX_SESSION_ERROR_CODES


def test_from_record_tolerates_malformed_values() -> None:
    from packages.core.services.sandbox_session_service import SandboxSession

    session = SandboxSession.from_record({"sandbox_id": "sb", "created_at": "not-a-float", "loaded_skills": "xlsx"})
    assert session is not None
    assert session.created_at == 0.0
    assert session.loaded_skills == []

    assert SandboxSession.from_record({"sandbox_id": ""}) is None
    assert SandboxSession.from_record("junk") is None


@pytest.mark.asyncio
async def test_load_degrades_malformed_fields_instead_of_dropping_record(fake_cache) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        load_sandbox_session,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    scope = "conversation:conversation_1"
    fake_cache.store[owner.registry_key(scope)] = {"sandbox_id": "sb", "loaded_skills": 123}

    loaded = await load_sandbox_session(owner, scope=scope)
    assert loaded is not None
    assert loaded.loaded_skills == []


@pytest.mark.asyncio
async def test_userless_owner_registers_under_its_own_system_key(fake_cache) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSession,
        load_sandbox_session,
        register_sandbox_session,
    )

    owner = SandboxOwner("ent_1", "", workspace_id="ws_1")
    human = SandboxOwner("ent_1", "user_1")
    scope = "conversation:U01TASK"
    assert await load_sandbox_session(owner, scope=scope) is None

    session = SandboxSession(
        sandbox_id="sb_1",
        base_url="http://sandbox-service",
        created_at=1.0,
        last_acquired_at=1.0,
    )
    assert await register_sandbox_session(owner, session, scope=scope) is True
    assert (await load_sandbox_session(owner, scope=scope)).sandbox_id == "sb_1"
    # Never visible to a human owner, even under the same scope string.
    assert await load_sandbox_session(human, scope=scope) is None


@pytest.mark.asyncio
async def test_register_returns_false_when_cache_unavailable(fake_cache) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSession,
        load_sandbox_session,
        register_sandbox_session,
    )

    fake_cache.available = False
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    session = SandboxSession(
        sandbox_id="sb_1",
        base_url="http://sandbox-service",
        created_at=1.0,
        last_acquired_at=1.0,
    )
    scope = "conversation:conversation_1"
    assert await register_sandbox_session(owner, session, scope=scope) is False
    assert await load_sandbox_session(owner, scope=scope) is None


@pytest.mark.asyncio
async def test_release_drops_only_the_agents_view_while_the_conversation_keeps_the_sandbox(fake_cache) -> None:
    from packages.core.ai.runtime.sandbox import (
        RuntimeSandboxKind,
        runtime_agent_sandbox,
        runtime_init_sandbox_binding,
        runtime_load_sandbox_binding,
        runtime_sandbox_scope,
    )
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        release_sandbox_session,
    )

    owner = SandboxOwner("ent_1", "user_1")
    # sb_1 is the conversation scope's warm session, so every release below
    # keeps the container and only the unbind behaviour is under test.
    registry_key = owner.registry_key("conversation:conversation_1")
    fake_cache.store[registry_key] = {"sandbox_id": "sb_1", "base_url": "http://sandbox-service"}
    fake_cache.store["sandbox:warm:sb_1"] = registry_key

    scope = runtime_sandbox_scope(entity_id="ent_1", user_id="user_1", binding_key="conversation_1")
    await runtime_init_sandbox_binding(scope, "sb_1", "skill_xlsx", agent_id="agent_1")
    await runtime_init_sandbox_binding(scope, "sb_1", "", agent_id="agent_2")

    # agent_1 releases: its Skill view goes, the conversation keeps sb_1 and
    # agent_2 still operates it.
    assert await release_sandbox_session(owner=owner, sandbox_id="sb_1", scope=scope, agent_id="agent_1") == "kept"
    assert await runtime_agent_sandbox(scope, "agent_1", RuntimeSandboxKind.SKILL) is None
    assert (await runtime_agent_sandbox(scope, "agent_2", RuntimeSandboxKind.GENERIC))[0] == "sb_1"
    record = await runtime_load_sandbox_binding(scope, "sb_1")
    assert record["sandbox_id"] == "sb_1" and record["agents"] == ["agent_1", "agent_2"]

    # A container that is not the warm session is destroyed, and unbound.
    fake_cache.store[registry_key] = {"sandbox_id": "sb_newer", "base_url": "http://sandbox-service"}


@pytest.mark.asyncio
async def test_discard_one_shot_sandbox_destroys_only_matching_owner_scope(
    fake_cache, scripted_client, local_sandbox_settings,
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner, SandboxSession, discard_sandbox_session,
        register_sandbox_session,
    )

    from packages.core.ai.runtime.sandbox import (
        runtime_init_sandbox_binding,
        runtime_room_binding_key,
        runtime_sandbox_scope,
    )

    owner = SandboxOwner("ent_1", "user_1")
    # A group room run's one-shot key: never the requester's own (user) scope.
    room = runtime_room_binding_key("01HROOMRUN0000000000000000")
    scope = f"conversation:{room}"

    await runtime_init_sandbox_binding(
        runtime_sandbox_scope(entity_id="ent_1", user_id="user_1", binding_key=room),
        "sb_room", "skill_x", agent_id="agent_1",
    )
    session = SandboxSession(
        sandbox_id="sb_room", base_url="http://sandbox-service",
        created_at=1.0, last_acquired_at=1.0,
    )
    assert await register_sandbox_session(owner, session, scope=scope)
    assert await discard_sandbox_session(
        owner=SandboxOwner("ent_1", "other_user"), sandbox_id="sb_room",
        conversation_id=room, agent_id="agent_1",
    ) == "not_owned"
    assert fake_cache.store[owner.registry_key(scope)]["sandbox_id"] == "sb_room"
    assert await discard_sandbox_session(
        owner=owner, sandbox_id="sb_room", conversation_id=room,
        agent_id="agent_1",
    ) == "destroyed"
    assert {"handler": "destroy", "sandbox_id": "sb_room"} in scripted_client.calls
    assert owner.registry_key(scope) not in fake_cache.store
    assert "sandbox:warm:sb_room" not in fake_cache.store


@pytest.mark.asyncio
async def test_register_writes_and_forget_clears_the_warm_marker(fake_cache) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSession,
        forget_sandbox_session,
        register_sandbox_session,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    scope = "conversation:c1"
    session = SandboxSession(
        sandbox_id="sb_1",
        base_url="http://sandbox-service",
        created_at=1.0,
        last_acquired_at=1.0,
    )

    assert await register_sandbox_session(owner, session, scope=scope) is True
    assert fake_cache.store["sandbox:warm:sb_1"] == "sandbox:owner:ent_1:user_1:conversation:c1"

    await forget_sandbox_session(owner, "sb_1", scope=scope)
    assert "sandbox:warm:sb_1" not in fake_cache.store
    assert "sandbox:owner:ent_1:user_1:conversation:c1" not in fake_cache.store


@pytest.mark.asyncio
async def test_release_keeps_the_current_warm_session(fake_cache, scripted_client, local_sandbox_settings) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        release_sandbox_session,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    registry_key = owner.registry_key("conversation:conversation_1")
    fake_cache.store[registry_key] = {"sandbox_id": "sb_warm", "base_url": "http://sandbox-service"}
    fake_cache.store["sandbox:warm:sb_warm"] = registry_key

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_warm")

    assert outcome == "kept"
    assert fake_cache.store["sandbox:warm:sb_warm"] == registry_key
    # A kept container is never contacted: no client is even constructed.
    assert scripted_client.calls == []


@pytest.mark.asyncio
async def test_kept_warm_release_keeps_the_instance_row_active(
    fake_cache, scripted_client, local_sandbox_settings, monkeypatch
) -> None:
    """The instance row is how a later exec in the reused container resolves
    its runner; releasing it here made every exec after a warm reuse fail
    (seen end-to-end). Run-cancel protection lives in the cancel cleanup."""
    from packages.core.services import sandbox_queue_service
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        release_sandbox_session,
    )

    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_COORDINATION_MODE", "external-runner")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_SERVICE_URL", "")
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    registry_key = owner.registry_key("conversation:conversation_1")
    fake_cache.store[registry_key] = {"sandbox_id": "sb_warm", "base_url": "http://runner"}
    fake_cache.store["sandbox:warm:sb_warm"] = registry_key

    async def must_not_release(db, *, sandbox_id):
        raise AssertionError("a kept-warm release must not release the instance row")

    monkeypatch.setattr(sandbox_queue_service, "release_sandbox_instance", must_not_release)

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_warm")

    assert outcome == "kept"
    assert fake_cache.store["sandbox:warm:sb_warm"] == registry_key
    assert scripted_client.calls == []


@pytest.mark.asyncio
async def test_release_destroys_a_container_without_registration(
    fake_cache, scripted_client, local_sandbox_settings
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        release_sandbox_session,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_orphan")

    assert outcome == "destroyed"
    assert {"handler": "destroy", "sandbox_id": "sb_orphan"} in scripted_client.calls
    assert scripted_client.calls[-1]["handler"] == "close"


@pytest.mark.asyncio
async def test_release_destroys_a_superseded_container(fake_cache, scripted_client, local_sandbox_settings) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        release_sandbox_session,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    registry_key = owner.registry_key("conversation:conversation_1")
    # sb_old's marker still points at the scope, but the scope moved to sb_new.
    fake_cache.store[registry_key] = {"sandbox_id": "sb_new", "base_url": "http://sandbox-service"}
    fake_cache.store["sandbox:warm:sb_old"] = registry_key
    fake_cache.store["sandbox:warm:sb_new"] = registry_key

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_old")

    assert outcome == "destroyed"
    assert {"handler": "destroy", "sandbox_id": "sb_old"} in scripted_client.calls
    assert "sandbox:warm:sb_old" not in fake_cache.store
    assert fake_cache.store[registry_key]["sandbox_id"] == "sb_new"
    assert fake_cache.store["sandbox:warm:sb_new"] == registry_key


@pytest.mark.asyncio
async def test_release_reports_destroy_failure_without_raising(
    fake_cache, scripted_client, local_sandbox_settings
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        release_sandbox_session,
    )

    scripted_client.destroy_exc = RuntimeError("sandbox service down")
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_orphan")

    assert outcome == "destroy_failed"
    assert scripted_client.calls[-1]["handler"] == "close"


@pytest.mark.asyncio
async def test_release_keeps_container_when_cache_is_unreachable(
    fake_cache, scripted_client, local_sandbox_settings
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        release_sandbox_session,
    )

    # No marker and no cache: "not registered" is indistinguishable from
    # "cannot read the registry", so the container is left to the reaper.
    fake_cache.available = False
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_unknown")

    assert outcome == "kept"
    assert scripted_client.calls == []


@pytest.mark.asyncio
async def test_release_destroys_when_marker_absent_and_cache_is_up(
    fake_cache, scripted_client, local_sandbox_settings
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        release_sandbox_session,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_orphan")

    assert outcome == "destroyed"
    assert {"handler": "destroy", "sandbox_id": "sb_orphan"} in scripted_client.calls
    # The reachability probe never leaves a lease behind.
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_release_reports_destroyed_even_if_bookkeeping_fails(
    monkeypatch, fake_cache, scripted_client, local_sandbox_settings
) -> None:
    from packages.core.services import sandbox_session_service
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        release_sandbox_session,
    )

    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_COORDINATION_MODE", "external-runner")

    async def runner_base_url(sandbox_id, *, db, external):
        return "http://runner"

    async def failing_bookkeeping(sandbox_id, *, db):
        raise RuntimeError("runner row is locked")

    monkeypatch.setattr(sandbox_session_service, "_resolve_release_base_url", runner_base_url)
    monkeypatch.setattr(sandbox_session_service, "_mark_sandbox_released", failing_bookkeeping)

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    registry_key = owner.registry_key("conversation:conversation_1")
    fake_cache.store[registry_key] = {"sandbox_id": "sb_new", "base_url": "http://runner"}
    fake_cache.store["sandbox:warm:sb_old"] = registry_key

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_old", db=object())

    assert outcome == "destroyed"
    assert {"handler": "destroy", "sandbox_id": "sb_old"} in scripted_client.calls
    assert "sandbox:warm:sb_old" not in fake_cache.store


@pytest.mark.asyncio
async def test_release_uses_one_db_session_in_external_runner_mode(
    monkeypatch, fake_cache, scripted_client, local_sandbox_settings
) -> None:
    from packages.core import database as database_module
    from packages.core.services import sandbox_queue_service
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        release_sandbox_session,
    )

    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_COORDINATION_MODE", "external-runner")

    opened: list[SimpleNamespace] = []
    released: list[str] = []

    class FakeSessionFactory:
        def __call__(self):
            session = SimpleNamespace(committed=0)

            async def commit():
                session.committed += 1

            session.commit = commit
            opened.append(session)
            return _FakeSessionContext(session)

    class _FakeSessionContext:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_exc):
            return False

    async def fake_resolve(db, sandbox_id):
        assert db is opened[0]
        return SimpleNamespace(base_url="http://runner")

    async def fake_release_instance(db, *, sandbox_id):
        assert db is opened[0]
        released.append(sandbox_id)
        return True

    monkeypatch.setattr(database_module, "async_session", FakeSessionFactory())
    monkeypatch.setattr(sandbox_queue_service, "resolve_sandbox_runner", fake_resolve)
    monkeypatch.setattr(sandbox_queue_service, "release_sandbox_instance", fake_release_instance)

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_orphan")

    assert outcome == "destroyed"
    assert len(opened) == 1
    assert released == ["sb_orphan"]
    assert opened[0].committed == 1


@pytest.mark.asyncio
async def test_release_treats_not_found_as_destroyed(fake_cache, scripted_client, local_sandbox_settings) -> None:
    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        release_sandbox_session,
    )

    scripted_client.destroy_exc = SandboxNotFoundError("sb_orphan")
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_orphan")

    assert outcome == "destroyed"


def _fake_create_result(sandbox_id: str, *, scripts=None, requirements_txt=None, env_injection="exec"):
    return SimpleNamespace(
        sandbox_id=sandbox_id,
        container_name=f"skill-sbx-{sandbox_id}",
        status="running",
        workdir="/skill",
        env_blocked=[],
        env_injection=env_injection,
        skill=SimpleNamespace(
            entry_hint=None,
            scripts=scripts or [],
            requirements_txt=requirements_txt,
        ),
    )


class ScriptedSandboxClient:
    """Shared script for every FakeSandboxClient instance in one test."""

    calls: list[dict] = []
    health_payload: dict = {"status": "ok", "sandbox_image_available": True}
    create_exc: Exception | None = None
    status_exc: Exception | None = None
    status_results: list = []
    status_workdir: str = "/skill"
    load_results: list = []
    created_ids: list[str] = []
    create_delay: float = 0.0
    destroy_exc: Exception | None = None

    @classmethod
    def reset(cls) -> None:
        cls.calls = []
        cls.health_payload = {"status": "ok", "sandbox_image_available": True}
        cls.create_exc = None
        cls.status_exc = None
        cls.status_results = []
        cls.status_workdir = "/skill"
        cls.load_results = []
        cls.created_ids = ["sb_new"]
        cls.create_delay = 0.0
        cls.destroy_exc = None

    def __init__(self, *, base_url: str, timeout: float, api_token: str | None = None):
        type(self).calls.append({"handler": "init", "base_url": base_url})

    async def health(self):
        type(self).calls.append({"handler": "health"})
        return dict(type(self).health_payload)

    async def create_from_builtin(
        self,
        skill_name,
        files,
        env=None,
        allowed_sensitive_keys=None,
        config=None,
        auto_install=True,
        idempotency_key=None,
        **extra,
    ):
        type(self).calls.append(
            {
                "handler": "create_from_builtin",
                "skill_name": skill_name,
                "files": files,
                "env": env,
                "allowed_sensitive_keys": allowed_sensitive_keys,
                "config": config,
                "auto_install": auto_install,
                # Only an isolation resolver's create arguments land here.
                **({"extra": extra} if extra else {}),
            }
        )
        await asyncio.sleep(type(self).create_delay)
        if type(self).create_exc is not None:
            raise type(self).create_exc
        sandbox_id = type(self).created_ids.pop(0)
        return _fake_create_result(sandbox_id, scripts=["run.py"])

    async def create_from_files(
        self,
        skill_name,
        files,
        env=None,
        allowed_sensitive_keys=None,
        config=None,
        auto_install=True,
        idempotency_key=None,
        **extra,
    ):
        type(self).calls.append(
            {
                "handler": "create_from_files",
                "skill_name": skill_name,
                "files": files,
                "env": env,
                "allowed_sensitive_keys": allowed_sensitive_keys,
                "config": config,
                "auto_install": auto_install,
                # Only an isolation resolver's create arguments land here.
                **({"extra": extra} if extra else {}),
            }
        )
        await asyncio.sleep(type(self).create_delay)
        if type(self).create_exc is not None:
            raise type(self).create_exc
        sandbox_id = type(self).created_ids.pop(0)
        return _fake_create_result(sandbox_id, scripts=["run.py"])

    async def status(self, sandbox_id):
        type(self).calls.append({"handler": "status", "sandbox_id": sandbox_id})
        if type(self).status_results:
            outcome = type(self).status_results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if type(self).status_exc is not None:
            raise type(self).status_exc
        return SimpleNamespace(status="ready", workdir=type(self).status_workdir)

    async def load_skill(
        self,
        sandbox_id,
        skill_name,
        files,
        auto_install=True,
        idle_threshold=30,
        source="workspace",
        env=None,
        allowed_sensitive_keys=None,
    ):
        type(self).calls.append(
            {
                "handler": "load_skill",
                "sandbox_id": sandbox_id,
                "skill_name": skill_name,
                "files": files,
                "auto_install": auto_install,
                "idle_threshold": idle_threshold,
                "source": source,
                "env": dict(env or {}),
                "allowed_sensitive_keys": list(allowed_sensitive_keys or []),
            }
        )
        outcome = type(self).load_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def destroy(self, sandbox_id):
        type(self).calls.append({"handler": "destroy", "sandbox_id": sandbox_id})
        if type(self).destroy_exc is not None:
            raise type(self).destroy_exc

    async def close(self):
        type(self).calls.append({"handler": "close"})


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


@pytest.fixture
def recorded_contexts(monkeypatch):
    from packages.core.services import sandbox_session_service as svc

    contexts: list[dict] = []

    async def fake_init(scope, sandbox_id, skill_id, **kwargs):
        contexts.append(
            {
                "conversation_id": scope.binding_key,
                "sandbox_id": sandbox_id,
                "skill_id": skill_id,
                "entity_id": scope.entity_id,
                "user_id": scope.user_id,
                "agent_id": kwargs.pop("agent_id", None),
                **{key: value for key, value in kwargs.items() if key not in {"skill_key", "coexist"}},
            }
        )
        return {}

    monkeypatch.setattr(svc, "runtime_init_sandbox_binding", fake_init)
    return contexts


def _bundle(skill_name="xlsx", source="builtin"):
    from packages.core.services.sandbox_session_service import SandboxBundle

    return SandboxBundle(
        skill_name=skill_name,
        files={"SKILL.md": "# skill", "run.py": "print('ok')"},
        env={},
        allowed_sensitive_keys=(),
        source=source,
        config=None,
        auto_install=True,
    )


def _binding(conversation_id="conversation_1", agent_id="agent_1", skill_id="skill_xlsx"):
    from packages.core.services.sandbox_session_service import SandboxBinding

    return SandboxBinding(
        conversation_id=conversation_id,
        agent_id=agent_id,
        skill_id=skill_id,
    )


@pytest.mark.asyncio
async def test_acquire_creates_registers_and_binds_when_registry_empty(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSession,
        acquire_sandbox_session,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    session = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert isinstance(session, SandboxSession)
    assert session.sandbox_id == "sb_new"
    assert session.reused is False
    assert session.scripts == ["run.py"]
    handlers = [c["handler"] for c in scripted_client.calls]
    assert handlers == ["init", "health", "create_from_builtin", "close"]
    create_call = next(c for c in scripted_client.calls if c["handler"] == "create_from_builtin")
    assert create_call == {
        "handler": "create_from_builtin",
        "skill_name": "xlsx",
        "files": {"SKILL.md": "# skill", "run.py": "print('ok')"},
        "env": {},
        "allowed_sensitive_keys": [],
        "config": None,
        "auto_install": True,
    }
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:user"]["sandbox_id"]
        == "sb_new"
    )
    assert fake_cache.leases == {}
    assert recorded_contexts == [
        {
            "conversation_id": "conversation_1",
            "sandbox_id": "sb_new",
            "skill_id": "skill_xlsx",
            "entity_id": "ent_1",
            "user_id": "user_1",
            "agent_id": "agent_1",
            "holds_credentials": False,
            "credentials_owner_user_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_acquire_uses_create_from_files_for_workspace_bundles(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    await acquire_sandbox_session(owner=owner, bundle=_bundle(source="workspace"), binding=_binding())
    handlers = [c["handler"] for c in scripted_client.calls]
    assert "create_from_files" in handlers
    assert "create_from_builtin" not in handlers


@pytest.mark.asyncio
async def test_acquire_maps_missing_image_and_capacity_errors(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_sdk.exceptions import SandboxCapacityError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    scripted_client.health_payload = {
        "status": "ok",
        "sandbox_image": "sandbox-skill:test",
        "sandbox_image_available": False,
    }
    result = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())
    assert isinstance(result, SandboxSessionError)
    assert result.code == "image_unavailable"
    assert result.retryable is False
    assert "sandbox-skill:test" in result.message
    assert "create_from_builtin" not in [c["handler"] for c in scripted_client.calls]

    scripted_client.reset()
    scripted_client.create_exc = SandboxCapacityError("Max active sandbox limit reached (2)", status_code=429)
    result = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())
    assert isinstance(result, SandboxSessionError)
    assert result.code == "capacity_full"
    assert result.retryable is True
    assert "Sandbox resources are busy" in result.message
    assert "Max active sandbox limit reached" in result.message
    assert "sandbox:owner:ent_1:user_1:user" not in fake_cache.store


@pytest.mark.asyncio
async def test_acquire_destroys_new_container_when_binding_fails(
    fake_cache, scripted_client, local_sandbox_settings, monkeypatch
) -> None:
    from packages.core.services import sandbox_session_service as svc
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    async def fail_context(*_args, **_kwargs):
        raise RuntimeError("Sandbox owner context could not be persisted.")

    monkeypatch.setattr(svc, "runtime_init_sandbox_binding", fail_context)

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    result = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert isinstance(result, SandboxSessionError)
    assert result.code == "context_persist_failed"
    assert "owner context" in result.message
    assert {"handler": "destroy", "sandbox_id": "sb_new"} in scripted_client.calls
    assert "sandbox:owner:ent_1:user_1:user" not in fake_cache.store


@pytest.mark.asyncio
async def test_acquire_returns_not_configured_without_service_url(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, monkeypatch
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_SERVICE_URL", "")
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    result = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())
    assert isinstance(result, SandboxSessionError)
    assert result.code == "not_configured"
    assert scripted_client.calls == []


@pytest.mark.asyncio
async def test_acquire_maps_transport_failures_to_create_failed(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    scripted_client.create_exc = httpx.ConnectError("connection refused")
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    result = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert isinstance(result, SandboxSessionError)
    assert result.code == "create_failed"
    assert result.retryable is False
    assert "connection refused" in result.message
    assert scripted_client.calls[-1]["handler"] == "close"


@pytest.mark.asyncio
async def test_acquire_returns_session_even_when_registry_write_fails(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSession,
        acquire_sandbox_session,
    )

    fake_cache.available = False
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    result = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert isinstance(result, SandboxSession)
    assert result.sandbox_id == "sb_new"
    assert len(recorded_contexts) == 1
    assert fake_cache.store == {}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _seed_registry(
    fake_cache,
    *,
    entity="ent_1",
    user="user_1",
    sandbox_id="sb_warm",
    conversation="conversation_1",
    agent="agent_1",
):
    # A human user's general sandbox serves all of the user's conversations.
    scope = "user" if user else f"conversation:{conversation}"
    fake_cache.store[f"sandbox:owner:{entity}:{user or 'system'}:{scope}"] = {
        "sandbox_id": sandbox_id,
        "base_url": "http://sandbox-service",
        "created_at": 1.0,
        "last_acquired_at": 1.0,
        "loaded_skills": ["docx"],
    }


def _seed_own_container(fake_cache, *, skill="xlsx", sandbox_id="sb_own"):
    """A Skill's own container of user_1 whose env changed since it loaded.

    Same Skill, other env: the container must be reloaded (load_skill) before
    it can serve the new env, so a busy runner is waited for.
    """

    fake_cache.store[f"sandbox:owner:ent_1:user_1:user:skill:{skill}"] = {
        "sandbox_id": sandbox_id,
        "base_url": "http://sandbox-service",
        "created_at": 1.0,
        "last_acquired_at": 1.0,
        "loaded_skills": [skill],
        "current_skill": skill,
        "env_injection": "exec",
        "env_fingerprint": "stale",
    }


def _load_result(sandbox_id="sb_warm", scripts=("run.py",)):
    return SimpleNamespace(
        sandbox_id=sandbox_id,
        reused=True,
        skill=SimpleNamespace(scripts=list(scripts), requirements_txt="openpyxl", entry_hint=None),
    )


@pytest.mark.asyncio
async def test_acquire_reuses_owner_sandbox_within_a_conversation(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache, conversation="conversation_2")
    scripted_client.load_results = [_load_result()]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=_binding(conversation_id="conversation_2")
    )

    assert session.sandbox_id == "sb_warm"
    assert session.reused is True
    assert session.requirements_installed is True
    handlers = [c["handler"] for c in scripted_client.calls]
    assert "create_from_builtin" not in handlers
    assert handlers.index("status") < handlers.index("load_skill")
    load_call = next(c for c in scripted_client.calls if c["handler"] == "load_skill")
    assert load_call["sandbox_id"] == "sb_warm"
    assert load_call["skill_name"] == "xlsx"
    assert load_call["idle_threshold"] == 30
    assert load_call["source"] == "builtin"
    warm_key = "sandbox:owner:ent_1:user_1:user"
    assert fake_cache.store[warm_key]["loaded_skills"] == ["docx", "xlsx"]
    assert fake_cache.store[warm_key]["last_acquired_at"] > 1.0
    assert recorded_contexts[0]["conversation_id"] == "conversation_2"
    assert recorded_contexts[0]["sandbox_id"] == "sb_warm"


@pytest.mark.asyncio
async def test_acquire_isolates_entities_and_users(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    scripted_client.created_ids = ["sb_a", "sb_b", "sb_c"]
    for entity, user in (("ent_1", "user_1"), ("ent_2", "user_1"), ("ent_1", "user_2")):
        await acquire_sandbox_session(
            owner=SandboxOwner(entity_id=entity, user_id=user),
            bundle=_bundle(),
            binding=_binding(),
        )

    creates = [c for c in scripted_client.calls if c["handler"] == "create_from_builtin"]
    assert len(creates) == 3
    assert {
        fake_cache.store[key]["sandbox_id"]
        for key in (
            "sandbox:owner:ent_1:user_1:user",
            "sandbox:owner:ent_2:user_1:user",
            "sandbox:owner:ent_1:user_2:user",
        )
    } == {"sb_a", "sb_b", "sb_c"}


@pytest.mark.asyncio
async def test_acquire_replaces_dead_registry_entry(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache, sandbox_id="sb_dead")
    scripted_client.status_exc = SandboxNotFoundError("gone", status_code=404)
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert session.sandbox_id == "sb_new"
    assert session.reused is False
    assert "load_skill" not in [c["handler"] for c in scripted_client.calls]
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:user"]["sandbox_id"]
        == "sb_new"
    )


@pytest.mark.asyncio
async def test_skill_reload_waits_while_its_own_container_is_busy(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_sdk.exceptions import SandboxError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_own_container(fake_cache)
    scripted_client.load_results = [
        SandboxError("busy", status_code=409),
        SandboxError("busy", status_code=409),
        _load_result("sb_own"),
    ]
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=_binding(), sleep=clock.sleep, clock=clock
    )

    assert session.sandbox_id == "sb_own"
    assert session.reused is True
    assert clock.sleeps == [1, 1]


@pytest.mark.asyncio
async def test_busy_primary_holding_another_skill_gives_the_skill_its_own_container(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """Never wait on, and never wipe, a Skill another agent is using right now."""
    from packages.core.services.sandbox_sdk.exceptions import SandboxError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache)  # the primary holds docx
    scripted_client.load_results = [SandboxError("busy", status_code=409, code="sandbox_busy")]
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(
        owner=owner, bundle=_bundle("xlsx"), binding=_binding(), sleep=clock.sleep, clock=clock
    )

    assert (session.sandbox_id, session.reused) == ("sb_new", False)
    assert clock.sleeps == []
    primary = fake_cache.store["sandbox:owner:ent_1:user_1:user"]
    assert primary["sandbox_id"] == "sb_warm" and primary["loaded_skills"] == ["docx"]
    own = fake_cache.store["sandbox:owner:ent_1:user_1:user:skill:xlsx"]
    assert own["sandbox_id"] == "sb_new"
    assert "destroy" not in [c["handler"] for c in scripted_client.calls]
    # Both are the conversation's: the binding names the new container.
    assert recorded_contexts[-1]["sandbox_id"] == "sb_new"

    # The next xlsx admission goes straight to the Skill's own container.
    scripted_client.reset()
    scripted_client.load_results = [_load_result("sb_new")]
    again = await acquire_sandbox_session(owner=owner, bundle=_bundle("xlsx"), binding=_binding(agent_id="agent_2"))
    assert again.sandbox_id == "sb_new"
    assert [c["sandbox_id"] for c in scripted_client.calls if c["handler"] == "load_skill"] == ["sb_new"]


@pytest.mark.asyncio
async def test_busy_primary_holding_the_same_skill_is_shared_as_loaded(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """Parallel delegates on one Skill share its container instead of waiting for a reload."""
    from packages.core.services.sandbox_sdk.exceptions import SandboxError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache)
    fake_cache.store["sandbox:owner:ent_1:user_1:user"]["current_skill"] = "xlsx"
    scripted_client.load_results = [SandboxError("busy", status_code=409, code="sandbox_busy")]
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(
        owner=owner, bundle=_bundle("xlsx"), binding=_binding(agent_id="agent_2"), sleep=clock.sleep, clock=clock
    )

    assert (session.sandbox_id, session.reused) == ("sb_warm", True)
    assert clock.sleeps == []
    assert "create_from_builtin" not in [c["handler"] for c in scripted_client.calls]
    assert recorded_contexts[-1]["agent_id"] == "agent_2"


@pytest.mark.asyncio
async def test_acquire_returns_busy_timeout_after_max_wait(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_sdk.exceptions import SandboxError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    _seed_own_container(fake_cache)
    scripted_client.load_results = [SandboxError("busy", status_code=409)] * 10
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=_binding(), sleep=clock.sleep, clock=clock
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "busy_timeout"
    assert result.retryable is True
    assert "create_from_builtin" not in [c["handler"] for c in scripted_client.calls]
    assert clock.now >= 3
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:user:skill:xlsx"]["sandbox_id"]
        == "sb_own"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["sandbox_busy", None])
async def test_acquire_waits_only_for_a_busy_409(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, code
) -> None:
    """``sandbox_busy`` (or a code-less 409 from an older runner) clears by waiting."""
    from packages.core.services.sandbox_sdk.exceptions import SandboxError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_own_container(fake_cache)
    scripted_client.load_results = [
        SandboxError("busy", status_code=409, code=code),
        _load_result("sb_own"),
    ]
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=_binding(), sleep=clock.sleep, clock=clock
    )

    assert session.sandbox_id == "sb_own"
    assert session.reused is True
    assert clock.sleeps == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["sandbox_unavailable", "execution_conflict", "execution_state_conflict"]
)
async def test_acquire_creates_fresh_on_a_409_that_waiting_cannot_fix(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, code
) -> None:
    """A quarantined or destroying sandbox never becomes free: no busy-wait."""
    from packages.core.services.sandbox_sdk.exceptions import SandboxError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache)
    scripted_client.load_results = [SandboxError("unavailable", status_code=409, code=code)] * 10
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=_binding(), sleep=clock.sleep, clock=clock
    )

    assert session.sandbox_id == "sb_new"
    assert clock.sleeps == []
    assert [c["handler"] for c in scripted_client.calls].count("load_skill") == 1


@pytest.mark.asyncio
async def test_acquire_recreates_when_load_skill_reports_sandbox_gone(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_sdk.exceptions import SandboxNotFoundError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache)
    scripted_client.load_results = [SandboxNotFoundError("gone", status_code=404)]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert session.sandbox_id == "sb_new"
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:user"]["sandbox_id"]
        == "sb_new"
    )


@pytest.mark.asyncio
async def test_acquire_destroys_a_container_it_gives_up_on(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """Dropping the registry entry without a destroy would leak the container."""

    from packages.core.services.sandbox_sdk.exceptions import (
        SandboxError,
        SandboxNotFoundError,
    )
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    # Alive, but load_skill failed for a reason that is not "busy": the warm
    # container is unusable, so admission destroys it before forgetting it.
    _seed_registry(fake_cache)
    scripted_client.load_results = [SandboxError("boom", status_code=500)]
    session = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert session.sandbox_id == "sb_new"
    assert {"handler": "destroy", "sandbox_id": "sb_warm"} in scripted_client.calls

    # Already gone: there is nothing left to destroy.
    scripted_client.reset()
    _seed_registry(fake_cache)
    scripted_client.status_exc = SandboxNotFoundError("gone", status_code=404)
    session = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert session.sandbox_id == "sb_new"
    assert "destroy" not in [c["handler"] for c in scripted_client.calls]


@pytest.mark.asyncio
async def test_acquire_reports_reused_binding_failure_without_destroying(
    fake_cache, scripted_client, local_sandbox_settings, monkeypatch
) -> None:
    from packages.core.services import sandbox_session_service as svc
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    async def fail_context(*_args, **_kwargs):
        raise RuntimeError("Sandbox owner context could not be persisted.")

    monkeypatch.setattr(svc, "runtime_init_sandbox_binding", fail_context)
    _seed_registry(fake_cache)
    scripted_client.load_results = [_load_result()]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert isinstance(result, SandboxSessionError)
    assert result.code == "context_persist_failed"
    assert "no Sandbox tool access was granted" in result.message
    assert "destroy" not in [c["handler"] for c in scripted_client.calls]
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:user"]["sandbox_id"]
        == "sb_warm"
    )


@pytest.mark.asyncio
async def test_anonymous_owner_never_touches_registry(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache)
    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id=""),
        bundle=_bundle(),
        binding=_binding(conversation_id=None),
    )
    assert session.sandbox_id == "sb_new"
    assert "status" not in [c["handler"] for c in scripted_client.calls]
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:user"]["sandbox_id"]
        == "sb_warm"
    )
    assert fake_cache.leases == {}
    assert recorded_contexts == []


@pytest.mark.asyncio
async def test_acquire_clamps_last_poll_to_remaining_budget(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, monkeypatch
) -> None:
    from packages.core.services.sandbox_sdk.exceptions import SandboxError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_QUEUE_POLL_SECONDS", 5)
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_QUEUE_MAX_WAIT_SECONDS", 12)
    _seed_own_container(fake_cache)
    scripted_client.load_results = [SandboxError("busy", status_code=409)] * 10
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=_binding(), sleep=clock.sleep, clock=clock
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "busy_timeout"
    assert clock.sleeps == [5, 5, 2]
    assert clock.now == 12
    assert "12s" in result.message


@pytest.mark.asyncio
async def test_acquire_retries_inconclusive_probe_before_reusing(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache)
    scripted_client.status_results = [httpx.ConnectError("blip")]
    scripted_client.load_results = [_load_result()]
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=_binding(), sleep=clock.sleep, clock=clock
    )

    assert session.sandbox_id == "sb_warm"
    assert session.reused is True
    assert clock.sleeps == [1]
    assert "create_from_builtin" not in [c["handler"] for c in scripted_client.calls]


@pytest.mark.asyncio
async def test_acquire_treats_two_inconclusive_probes_as_dead(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache)
    scripted_client.status_exc = httpx.ConnectError("down")
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=_binding(), sleep=clock.sleep, clock=clock
    )

    assert session.sandbox_id == "sb_new"
    assert clock.sleeps == [1]
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:user"]["sandbox_id"]
        == "sb_new"
    )


@pytest.mark.asyncio
async def test_acquire_keeps_registry_when_load_skill_transport_fails(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache)
    scripted_client.load_results = [httpx.ConnectError("mid-load")]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert isinstance(result, SandboxSessionError)
    assert result.code == "create_failed"
    assert "sb_warm" in result.message
    assert "create_from_builtin" not in [c["handler"] for c in scripted_client.calls]
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:user"]["sandbox_id"]
        == "sb_warm"
    )


@pytest.mark.asyncio
async def test_acquire_carries_workdir_from_probe(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    scripted_client.status_workdir = "/custom/wd"
    _seed_registry(fake_cache)
    scripted_client.load_results = [_load_result()]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert session.workdir == "/custom/wd"


@pytest.mark.asyncio
async def test_credentialed_bundle_replaces_a_container_whose_env_was_baked_at_create(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """A registry record without an injection mode came from an older service
    that baked env into the container. A credentialed skill must not load into
    it; the shared container stays for the agents using it and the Skill gets
    a container of its own."""
    from packages.core.services.sandbox_session_service import (
        SandboxBundle,
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache)
    bundle = SandboxBundle(
        skill_name="xlsx",
        files={"SKILL.md": "# skill", "run.py": "print('ok')"},
        env={"API_KEY": "secret"},
        allowed_sensitive_keys=(),
        source="builtin",
        config=None,
        auto_install=True,
    )
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(owner=owner, bundle=bundle, binding=_binding())

    assert session.sandbox_id == "sb_new"
    assert session.reused is False
    handlers = [c["handler"] for c in scripted_client.calls]
    assert "load_skill" not in handlers
    assert "destroy" not in handlers, "the shared primary serves the conversation's other agents"
    create_call = next(c for c in scripted_client.calls if c["handler"] == "create_from_builtin")
    assert create_call["env"] == {"API_KEY": "secret"}
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:user"]["sandbox_id"]
        == "sb_warm"
    )
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:user:skill:xlsx"]["sandbox_id"]
        == "sb_new"
    )
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_acquire_uses_one_client_for_probe_and_reuse(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache)
    scripted_client.load_results = [_load_result()]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    handlers = [c["handler"] for c in scripted_client.calls]
    assert handlers.count("init") == 1
    assert handlers[-1] == "close"


@pytest.mark.asyncio
async def test_acquire_single_flight_waits_for_lock_holder(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    fake_cache.leases["sandbox:owner:ent_1:user_1:user:lock"] = "other-token"
    clock = FakeClock()

    async def sleep_then_release(seconds: float) -> None:
        await clock.sleep(seconds)
        fake_cache.leases.pop("sandbox:owner:ent_1:user_1:user:lock", None)
        _seed_registry(fake_cache)

    scripted_client.load_results = [_load_result()]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=_binding(), sleep=sleep_then_release, clock=clock
    )

    assert session.sandbox_id == "sb_warm"
    assert session.reused is True
    assert clock.sleeps == [1]
    assert "create_from_builtin" not in [c["handler"] for c in scripted_client.calls]
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_acquire_lock_wait_times_out_as_busy(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    fake_cache.leases["sandbox:owner:ent_1:user_1:user:lock"] = "other-token"
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=_binding(), sleep=clock.sleep, clock=clock
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "busy_timeout"
    assert scripted_client.calls == []
    assert fake_cache.leases == {
        "sandbox:owner:ent_1:user_1:user:lock": "other-token"
    }


@pytest.mark.asyncio
async def test_lock_wait_and_busy_wait_share_one_deadline(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """Waiting for the owner lock spends the same budget as the busy wait."""

    from packages.core.services.sandbox_sdk.exceptions import SandboxError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    fake_cache.leases["sandbox:owner:ent_1:user_1:user:skill:xlsx:lock"] = "other-token"
    clock = FakeClock()

    async def sleep_then_release(seconds: float) -> None:
        await clock.sleep(seconds)
        fake_cache.leases.pop("sandbox:owner:ent_1:user_1:user:skill:xlsx:lock", None)
        _seed_own_container(fake_cache)

    scripted_client.load_results = [SandboxError("busy", status_code=409)] * 5
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=_binding(), sleep=sleep_then_release, clock=clock
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "busy_timeout"
    # One second of lock wait plus two of busy wait: the single 3s budget, not
    # 3s of lock wait followed by a fresh 3s of busy wait.
    assert clock.now == 3


@pytest.mark.asyncio
async def test_acquire_proceeds_without_lock_when_redis_is_down(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    fake_cache.available = False
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    session = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())
    assert session.sandbox_id == "sb_new"
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_acquire_heartbeats_the_lock_during_long_creates(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, monkeypatch
) -> None:
    from packages.core.services import sandbox_session_service as svc
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    monkeypatch.setattr(svc, "_LOCK_HEARTBEAT_SECONDS", 0.01)
    scripted_client.create_delay = 0.06
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert session.sandbox_id == "sb_new"
    assert len(fake_cache.extended) >= 1
    assert all(ttl == svc._LOCK_TTL_SECONDS for _key, _token, ttl in fake_cache.extended)
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_acquire_refuses_durable_external_without_runtime_context(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, monkeypatch
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    monkeypatch.setattr(local_sandbox_settings, "MANOR_RUNTIME_EXECUTION_MODE", "durable")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_COORDINATION_MODE", "external-runner")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_SERVICE_URL", "")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_RUNNERS_JSON", '[{"base_url":"http://runner"}]')
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    # No conversation and no task: nothing could bind the sandbox, and there
    # is neither a run to park nor a scope ticket to wait on.
    result = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding(conversation_id=None))

    assert isinstance(result, SandboxSessionError)
    assert result.code == "not_configured"
    assert "requires runtime context" in result.message
    assert scripted_client.calls == []
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_acquire_enqueues_durable_reservation_with_runtime_context(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, monkeypatch
) -> None:
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.services import sandbox_queue_service
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxOwner,
        acquire_sandbox_session,
    )

    monkeypatch.setattr(local_sandbox_settings, "MANOR_RUNTIME_EXECUTION_MODE", "durable")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_COORDINATION_MODE", "external-runner")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_SERVICE_URL", "")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_RUNNERS_JSON", '[{"base_url":"http://runner"}]')

    run = SimpleNamespace(id="run_1", entity_id="ent_1", user_id="user_1")
    enqueued: list[dict] = []
    commits: list[bool] = []

    class FakeDatabase:
        async def get(self, _model, run_id):
            assert run_id == "run_1"
            return run

        async def commit(self):
            commits.append(True)

    async def fake_enqueue(db, *, run, tool_call_id, request_payload, activate):
        enqueued.append(
            {
                "run": run.id,
                "tool_call_id": tool_call_id,
                "payload": request_payload,
                "activate": activate,
            }
        )
        return SimpleNamespace(id="res_1", deadline_at="2026-09-03T00:00:00+00:00")

    monkeypatch.setattr(sandbox_queue_service, "enqueue_sandbox_reservation", fake_enqueue)
    _empty_scope_head(monkeypatch)

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    result = await acquire_sandbox_session(
        owner=owner,
        bundle=_bundle(),
        binding=SandboxBinding(
            conversation_id="conversation_1",
            agent_id="agent_1",
            skill_id="skill_xlsx",
            runtime_run_id="run_1",
            runtime_tool_call_id="tool_1",
        ),
        db=FakeDatabase(),
    )

    assert isinstance(result, RuntimeToolSuspension)
    assert result.kind == "waiting_resource"
    assert result.reservation_id == "res_1"
    assert enqueued[0]["tool_call_id"] == "tool_1"
    assert enqueued[0]["activate"] is False
    assert enqueued[0]["payload"]["skill_name"] == "xlsx"
    assert enqueued[0]["payload"]["source"] == "builtin"
    assert commits == [True]
    assert scripted_client.calls == []
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_acquire_refuses_a_reservation_inside_a_prompt_skill_loop(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, monkeypatch
) -> None:
    """A nested loop cannot be parked, so admission must not queue a ticket for it."""

    from packages.core.services import sandbox_queue_service
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    monkeypatch.setattr(local_sandbox_settings, "MANOR_RUNTIME_EXECUTION_MODE", "durable")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_COORDINATION_MODE", "external-runner")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_SERVICE_URL", "")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_RUNNERS_JSON", '[{"base_url":"http://runner"}]')

    async def unexpected_enqueue(*_args, **_kwargs):
        raise AssertionError("a nested loop must not enqueue a durable reservation")

    monkeypatch.setattr(sandbox_queue_service, "enqueue_sandbox_reservation", unexpected_enqueue)
    _empty_scope_head(monkeypatch)

    class UnexpectedDatabase:
        async def get(self, *_args):
            raise AssertionError("admission must refuse before touching the run")

    result = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=_bundle(skill_name="pdf"),
        binding=SandboxBinding(
            conversation_id="conversation_1",
            agent_id="agent_1",
            skill_id="skill_pdf",
            runtime_run_id="run_1",
            runtime_tool_call_id="outer_call_1",
            parent_skill="skill-creator",
        ),
        db=UnexpectedDatabase(),
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "nested_allocation_unavailable"
    assert result.retryable is False
    assert "'pdf'" in result.message and "'skill-creator'" in result.message
    assert scripted_client.calls == []
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_durable_queue_full_maps_to_capacity_full(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, monkeypatch
) -> None:
    """A full queue is a retryable admission outcome, and admission must not
    roll back the caller's session: that would expire every instance the tool
    still holds (skill, run) and discard its pending work."""
    from packages.core.services import sandbox_queue_service
    from packages.core.services.sandbox_queue_service import SandboxQueueFullError
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    monkeypatch.setattr(local_sandbox_settings, "MANOR_RUNTIME_EXECUTION_MODE", "durable")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_COORDINATION_MODE", "external-runner")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_SERVICE_URL", "")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_RUNNERS_JSON", '[{"base_url":"http://runner"}]')

    run = SimpleNamespace(id="run_1", entity_id="ent_1", user_id="user_1")
    commits: list[bool] = []
    rollbacks: list[bool] = []

    class FakeDatabase:
        async def get(self, _model, run_id):
            assert run_id == "run_1"
            return run

        async def commit(self):
            commits.append(True)

        async def rollback(self):
            rollbacks.append(True)

    async def full_enqueue(db, *, run, tool_call_id, request_payload, activate):
        raise SandboxQueueFullError("sandbox_queue_full")

    monkeypatch.setattr(sandbox_queue_service, "enqueue_sandbox_reservation", full_enqueue)
    _empty_scope_head(monkeypatch)

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    result = await acquire_sandbox_session(
        owner=owner,
        bundle=_bundle(),
        binding=SandboxBinding(
            conversation_id="conversation_1",
            agent_id="agent_1",
            skill_id="skill_xlsx",
            runtime_run_id="run_1",
            runtime_tool_call_id="tool_1",
        ),
        db=FakeDatabase(),
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "capacity_full"
    assert result.retryable is True
    assert "sandbox_queue_full" in result.message
    assert commits == []
    assert rollbacks == [], "admission must never roll back the caller's session"
    assert scripted_client.calls == []
    assert fake_cache.leases == {}


class _FakeDbSession:
    """Just enough AsyncSession for the task-reservation admission path."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.expired = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    def expire_all(self) -> None:
        self.expired += 1


class _FakeSessionFactory:
    def __init__(self, session) -> None:
        self.session = session
        self.opened = 0

    def __call__(self):
        self.opened += 1
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_exc):
        return False


def _ticket(status, *, sandbox_id=None, last_error=None):
    return SimpleNamespace(
        id="res_1",
        status=status,
        sandbox_id=sandbox_id,
        last_error=last_error,
        deadline_at="2026-09-04T00:00:00+00:00",
    )


class FakeQueue:
    """Scripted stand-in for the queue service the session service calls."""

    def __init__(self) -> None:
        self.db = _FakeDbSession()
        self.factory = _FakeSessionFactory(self.db)
        self.enqueued: list[dict] = []
        self.enqueue_error: Exception | None = None
        self.statuses: list[SimpleNamespace] = [_ticket("queued")]
        self.loads = 0
        self.cancelled: list[dict] = []
        self.cancel_result = True
        self.resolved: list[str] = []
        self.consumed: list[str] = []
        self.consume_error: Exception | None = None
        self.runner = SimpleNamespace(id="runner-1", base_url="http://runner-1")
        # Another admission's in-flight ticket for the same scope, returned by
        # enqueue instead of a new one (single flight).
        self.joined: SimpleNamespace | None = None

    async def enqueue(
        self,
        db,
        *,
        run=None,
        tool_call_id,
        request_payload,
        priority=100,
        now=None,
        max_pending=None,
        max_wait_seconds=None,
        activate=True,
        entity_id=None,
        user_id=None,
        requester_kind="run",
        requester_id=None,
        scope_key=None,
    ):
        # Mirrors the real ``enqueue_sandbox_reservation`` signature, so a
        # caller that passes an argument the queue service does not accept
        # fails here instead of silently landing in ``**kwargs``.
        self.enqueued.append(
            {
                "run": run,
                "tool_call_id": tool_call_id,
                "request_payload": request_payload,
                "priority": priority,
                "now": now,
                "max_pending": max_pending,
                "max_wait_seconds": max_wait_seconds,
                "activate": activate,
                "entity_id": entity_id,
                "user_id": user_id,
                "requester_kind": requester_kind,
                "requester_id": requester_id,
                "scope_key": scope_key,
            }
        )
        if self.enqueue_error is not None:
            raise self.enqueue_error
        if self.joined is not None:
            return self.joined
        ticket = _ticket("queued")
        ticket.tool_call_id = tool_call_id
        return ticket

    async def load(self, db, reservation_id):
        self.loads += 1
        return self.statuses[min(self.loads - 1, len(self.statuses) - 1)]

    async def cancel(self, db, *, reservation_id, reason, now=None):
        self.cancelled.append({"reservation_id": reservation_id, "reason": reason})
        return self.cancel_result

    async def resolve_runner(self, db, sandbox_id):
        self.resolved.append(sandbox_id)
        return self.runner

    async def consume(self, db, *, reservation_id, now=None):
        self.consumed.append(reservation_id)
        if self.consume_error is not None:
            raise self.consume_error
        return self.statuses[-1]


@pytest.fixture
def fake_queue(monkeypatch):
    import packages.core.database as database_module
    from packages.core.services import sandbox_queue_service

    queue = FakeQueue()
    monkeypatch.setattr(sandbox_queue_service, "enqueue_sandbox_reservation", queue.enqueue)
    monkeypatch.setattr(sandbox_queue_service, "load_sandbox_reservation", queue.load)
    monkeypatch.setattr(sandbox_queue_service, "cancel_queued_sandbox_reservation", queue.cancel)
    monkeypatch.setattr(sandbox_queue_service, "resolve_sandbox_runner", queue.resolve_runner)
    monkeypatch.setattr(sandbox_queue_service, "consume_sandbox_reservation", queue.consume)
    monkeypatch.setattr(database_module, "async_session", queue.factory)
    _empty_scope_head(monkeypatch)
    return queue


def _empty_scope_head(monkeypatch):
    """Postgres knows no ticket and no container for the scope (fake DB sessions)."""

    from packages.core.services import sandbox_queue_service

    async def no_lock(db, scope_key):
        return None

    async def nothing(db, *, scope_key, exclude_sandbox_ids=(), now=None):
        return None, None

    async def no_container(db, *, scope_key, exclude_sandbox_ids=()):
        return None

    monkeypatch.setattr(sandbox_queue_service, "lock_sandbox_scope", no_lock)
    monkeypatch.setattr(sandbox_queue_service, "find_scope_head", nothing)
    monkeypatch.setattr(sandbox_queue_service, "find_scope_container", no_container)


def _external_runner_settings(settings, monkeypatch):
    monkeypatch.setattr(settings, "MANOR_RUNTIME_EXECUTION_MODE", "durable")
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", "external-runner")
    monkeypatch.setattr(settings, "SANDBOX_SERVICE_URL", "")
    monkeypatch.setattr(settings, "SANDBOX_RUNNERS_JSON", '[{"base_url":"http://runner-1"}]')
    return settings


def _task_binding():
    from packages.core.services.sandbox_session_service import SandboxBinding

    return SandboxBinding(
        conversation_id="U01ABC",
        agent_id="agent_1",
        skill_id="skill_xlsx",
        task_id="01ABC",
    )


@pytest.mark.asyncio
async def test_admit_sandbox_attaches_the_calls_durable_ticket_before_acquiring(
    local_sandbox_settings, monkeypatch
) -> None:
    """A durable run re-executes the tool call that queued its ticket: the
    entry point must find that ticket first, or the idempotent enqueue hands
    the call its own reservation back and it waits forever."""
    from packages.core.services import sandbox_session_service as service
    from packages.core.services.sandbox_session_service import SandboxBinding, SandboxOwner

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    calls: list[str] = []
    allocated = object()
    attached: dict[str, object] = {"value": allocated}

    async def attach(*, owner, bundle, binding, db):
        calls.append("attach")
        return attached["value"]

    async def acquire(*, owner, bundle, binding, db, sleep, clock):
        calls.append("acquire")
        return "acquired"

    monkeypatch.setattr(service, "attach_durable_reservation", attach)
    monkeypatch.setattr(service, "acquire_sandbox_session", acquire)
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    binding = SandboxBinding(runtime_run_id="run_1", runtime_tool_call_id="call_1")
    db = object()

    assert await service.admit_sandbox(owner=owner, bundle=_bundle(), binding=binding, db=db) is allocated
    assert calls == ["attach"]

    attached["value"] = None
    assert await service.admit_sandbox(owner=owner, bundle=_bundle(), binding=binding, db=db) == "acquired"
    assert calls == ["attach", "attach", "acquire"]


@pytest.mark.asyncio
async def test_task_requester_never_polls_on_the_callers_session(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    """The wait rolls back per poll; that would expire the caller's loaded rows.

    The skill service hands its own session in (it still holds the Skill row
    the execution contract is built from). The ticket flow must open a private
    session instead of committing/rolling back the caller's.
    """
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSession,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    fake_queue.statuses = [_ticket("queued"), _ticket("allocated", sandbox_id="sb_task")]
    clock = FakeClock()
    caller_db = _FakeDbSession()

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=_bundle(),
        binding=_task_binding(),
        db=caller_db,
        sleep=clock.sleep,
        clock=clock,
    )

    assert isinstance(session, SandboxSession)
    assert session.sandbox_id == "sb_task"
    assert (caller_db.commits, caller_db.rollbacks, caller_db.expired) == (0, 0, 0)
    # Private sessions only: enqueue (under the owner lock), the wait (outside
    # it, so joiners are not blocked), and the attach.
    assert fake_queue.factory.opened == 3
    assert fake_queue.db.rollbacks >= len(clock.sleeps)
    assert fake_queue.db.commits >= 2  # ticket enqueue + consume


@pytest.mark.asyncio
async def test_task_requester_waits_for_allocation_then_attaches(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSession,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    fake_queue.statuses = [
        _ticket("queued"),
        _ticket("allocating"),
        _ticket("allocated", sandbox_id="sb_task"),
    ]
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(
        owner=owner,
        bundle=_bundle(),
        binding=_task_binding(),
        sleep=clock.sleep,
        clock=clock,
    )

    assert isinstance(session, SandboxSession)
    assert session.sandbox_id == "sb_task"
    assert session.base_url == "http://runner-1"
    assert session.reused is False
    assert session.scripts == ["run.py"]
    assert clock.sleeps == [1, 1, 1]
    # Every poll ends the previous transaction so the next SELECT sees the
    # scheduler's committed write instead of a snapshot frozen at enqueue.
    assert fake_queue.db.rollbacks >= len(clock.sleeps)
    assert fake_queue.db.expired >= len(clock.sleeps)

    enqueued = fake_queue.enqueued[0]
    assert enqueued["run"] is None
    assert enqueued["requester_kind"] == "task"
    assert enqueued["requester_id"] == "01ABC"
    assert enqueued["activate"] is True
    assert enqueued["entity_id"] == "ent_1"
    assert enqueued["user_id"] == "user_1"
    assert enqueued["tool_call_id"].startswith("01ABC:")
    assert enqueued["request_payload"]["skill_name"] == "xlsx"

    assert fake_queue.resolved == ["sb_task"]
    assert fake_queue.consumed == ["res_1"]
    assert fake_queue.cancelled == []
    assert recorded_contexts[0]["conversation_id"] == "U01ABC"
    assert recorded_contexts[0]["sandbox_id"] == "sb_task"
    assert fake_cache.store["sandbox:owner:ent_1:user_1:user"]["sandbox_id"] == "sb_task"
    # Admission never touches the sandbox service directly on this path.
    # The task flow never sees the create response, so it asks the runner how
    # it injects skill env and how it lays out Skills (one probe each; the
    # layout answer is cached per runner). The fake health reports neither,
    # hence "create" and no side-by-side Skill slots.
    assert [call["handler"] for call in scripted_client.calls] == ["init", "health", "close"] * 2
    assert session.env_injection == "create"
    assert session.skill_slots is False
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_task_requester_failed_ticket_is_retryable_capacity(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    fake_queue.statuses = [
        _ticket("queued"),
        _ticket("failed", last_error="sandbox_allocation_failed"),
    ]
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(
        owner=owner,
        bundle=_bundle(),
        binding=_task_binding(),
        sleep=clock.sleep,
        clock=clock,
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "capacity_full"
    assert result.retryable is True
    assert "sandbox_allocation_failed" in result.message
    # The ticket's recorded code is the caller's to read (a runner refusal
    # reaches its user by name).
    assert result.cause == "sandbox_allocation_failed"
    assert fake_queue.consumed == []
    assert fake_queue.cancelled == []
    assert recorded_contexts == []
    assert scripted_client.calls == []
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_task_requester_times_out_and_cancels(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    fake_queue.statuses = [_ticket("queued")]
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(
        owner=owner,
        bundle=_bundle(),
        binding=_task_binding(),
        sleep=clock.sleep,
        clock=clock,
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "capacity_full"
    assert result.retryable is True
    assert "timed out" in result.message
    assert "cancelled" in result.message
    assert clock.now >= 3
    assert fake_queue.cancelled == [{"reservation_id": "res_1", "reason": "task_wait_timeout"}]
    assert fake_queue.consumed == []
    assert scripted_client.calls == []
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_task_requester_keeps_waiting_while_the_runner_is_allocating(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    """A ticket claimed at the deadline is seconds from usable: wait for it.

    Giving up at that instant abandons the container the runner is building
    and makes the model queue a second ticket for the same skill.
    """
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSession,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    # max_wait=3, poll=1: the third poll lands on the deadline and sees the
    # scheduler's claim; the fourth sees the container.
    fake_queue.statuses = [
        _ticket("queued"),
        _ticket("queued"),
        _ticket("allocating"),
        _ticket("allocated", sandbox_id="sb_late"),
    ]
    clock = FakeClock()

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=_bundle(),
        binding=_task_binding(),
        sleep=clock.sleep,
        clock=clock,
    )

    assert isinstance(session, SandboxSession)
    assert session.sandbox_id == "sb_late"
    assert clock.sleeps == [1, 1, 1, 1]
    assert fake_queue.cancelled == []


@pytest.mark.asyncio
async def test_task_requester_waits_as_long_as_its_ticket_may_wait(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    """A pool whose fleet boots a machine gives its tickets a longer deadline
    than the queue's default; the poller must not cancel the ticket early."""
    from datetime import datetime, timedelta, timezone

    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSession,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    later = datetime.now(timezone.utc) + timedelta(minutes=15)
    queued = SimpleNamespace(**{**vars(_ticket("queued")), "deadline_at": later})
    # max_wait=3, poll=1: six queued polls outlast the default budget.
    fake_queue.statuses = [queued] * 6 + [_ticket("allocated", sandbox_id="sb_booted")]
    clock = FakeClock()

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=_bundle(),
        binding=_task_binding(),
        sleep=clock.sleep,
        clock=clock,
    )

    assert isinstance(session, SandboxSession)
    assert session.sandbox_id == "sb_booted"
    assert fake_queue.cancelled == []


@pytest.mark.asyncio
async def test_task_requester_allocating_grace_is_bounded(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    from packages.core.services import sandbox_session_service
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    monkeypatch.setattr(sandbox_session_service, "_TASK_ALLOCATING_GRACE_SECONDS", 4)
    fake_queue.statuses = [_ticket("queued"), _ticket("queued"), _ticket("allocating")]
    fake_queue.cancel_result = False
    clock = FakeClock()

    result = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=_bundle(),
        binding=_task_binding(),
        sleep=clock.sleep,
        clock=clock,
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "capacity_full"
    # 3 polls to the deadline, then exactly the grace on top; the grace is
    # granted once, so a ticket stuck ALLOCATING cannot pin the worker.
    assert clock.sleeps == [1] * 7
    assert fake_queue.cancelled == [{"reservation_id": "res_1", "reason": "task_wait_timeout"}]


@pytest.mark.asyncio
async def test_task_requester_timeout_leaves_a_claimed_ticket_to_the_runner(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    # ALLOCATING owns a runner slot: the cancel is refused and the container
    # the runner is still building becomes the idle reaper's problem.
    fake_queue.statuses = [_ticket("allocating")]
    fake_queue.cancel_result = False
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(
        owner=owner,
        bundle=_bundle(),
        binding=_task_binding(),
        sleep=clock.sleep,
        clock=clock,
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "capacity_full"
    assert "timed out" in result.message
    assert "idle reaper" in result.message
    assert fake_queue.cancelled == [{"reservation_id": "res_1", "reason": "task_wait_timeout"}]


@pytest.mark.asyncio
async def test_task_requester_queue_full(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    from packages.core.services.sandbox_queue_service import SandboxQueueFullError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    fake_queue.enqueue_error = SandboxQueueFullError("sandbox_queue_full")
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(
        owner=owner,
        bundle=_bundle(),
        binding=_task_binding(),
        sleep=clock.sleep,
        clock=clock,
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "capacity_full"
    assert result.retryable is True
    assert "sandbox_queue_full" in result.message
    # A refused admission never waits for a ticket that was never created,
    # and hands its own session back with no open transaction.
    assert clock.sleeps == []
    assert fake_queue.loads == 0
    assert fake_queue.db.commits == 0
    assert fake_queue.db.rollbacks == 1
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_task_requester_bind_failure_destroys_allocated_container(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    from packages.core.services import sandbox_session_service as svc
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    fake_queue.statuses = [_ticket("allocated", sandbox_id="sb_task")]

    async def failing_init(*_args, **_kwargs):
        raise RuntimeError("redis down")

    destroyed: list[dict] = []

    async def fake_destroy(sandbox_id, *, db, external):
        destroyed.append({"sandbox_id": sandbox_id, "external": external})
        return "destroyed"

    monkeypatch.setattr(svc, "runtime_init_sandbox_binding", failing_init)
    monkeypatch.setattr(svc, "_destroy_container", fake_destroy)
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(
        owner=owner,
        bundle=_bundle(),
        binding=_task_binding(),
        sleep=clock.sleep,
        clock=clock,
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "context_persist_failed"
    assert "newly created Sandbox was destroyed" in result.message
    assert destroyed == [{"sandbox_id": "sb_task", "external": True}]
    assert fake_queue.consumed == []
    assert "sandbox:owner:ent_1:user_1:user" not in fake_cache.store
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_task_requester_consume_race_unbinds_and_is_retryable(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    from packages.core.services import sandbox_session_service as svc
    from packages.core.services.sandbox_queue_service import SandboxAllocationStateError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    fake_queue.statuses = [_ticket("allocated", sandbox_id="sb_task")]
    # The abandoned-allocation sweep released the ticket while this call bound.
    fake_queue.consume_error = SandboxAllocationStateError("reservation_not_allocated")

    unbound: list[tuple[str, str, str | None]] = []

    async def fake_delete(scope, sandbox_id):
        unbound.append((scope.binding_key, scope.user_id, sandbox_id))
        return True

    destroyed: list[str] = []

    async def fake_destroy(sandbox_id, *, db, external):
        destroyed.append(sandbox_id)
        return "destroyed"

    monkeypatch.setattr(svc, "runtime_delete_sandbox_binding", fake_delete)
    monkeypatch.setattr(svc, "_destroy_container", fake_destroy)
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(
        owner=owner,
        bundle=_bundle(),
        binding=_task_binding(),
        sleep=clock.sleep,
        clock=clock,
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "capacity_full"
    assert result.retryable is True
    assert "released before it could be consumed" in result.message
    # The slot is handed back unbound (only while it still names this
    # container), so the next call is not denied by a binding pointing at a
    # container this one no longer owns.
    assert unbound == [("U01ABC", "user_1", "sb_task")]
    # The sweep already released the instance; the idle reaper owns the rest.
    assert destroyed == []
    assert "sandbox:owner:ent_1:user_1:user" not in fake_cache.store
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_delegate_without_run_or_task_polls_the_conversations_ticket(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    """A delegated agent runs inside its parent's tool call: no run to park,
    no task. It waits on the conversation's ticket like a task worker."""
    from packages.core.services.sandbox_session_service import (
        SANDBOX_SCOPE_PAYLOAD_KEY,
        SandboxBinding,
        SandboxOwner,
        SandboxSession,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    fake_queue.statuses = [_ticket("allocated", sandbox_id="sb_conv")]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    clock = FakeClock()

    result = await acquire_sandbox_session(
        owner=owner,
        bundle=_bundle(),
        binding=SandboxBinding(conversation_id="conversation_1", agent_id="agent_1", skill_id="skill_xlsx"),
        sleep=clock.sleep,
        clock=clock,
    )
    assert isinstance(result, SandboxSession) and result.sandbox_id == "sb_conv"
    [enqueued] = fake_queue.enqueued
    assert enqueued["requester_kind"] == "task"
    assert enqueued["requester_id"] == "conversation:conversation_1"
    scope_key = "sandbox:owner:ent_1:user_1:user"
    assert enqueued["scope_key"] == scope_key
    assert enqueued["request_payload"][SANDBOX_SCOPE_PAYLOAD_KEY] == scope_key
    assert fake_cache.store[scope_key]["sandbox_id"] == "sb_conv"
    assert fake_queue.consumed == ["res_1"]


@pytest.mark.asyncio
async def test_a_joined_ticket_is_never_cancelled_by_the_joiner_and_a_run_ticket_is_never_consumed(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxOwner,
        SandboxSession,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    binding = SandboxBinding(conversation_id="conversation_1", agent_id="agent_2", skill_id="skill_xlsx")

    # Joined another admission's ticket, which never allocates: the joiner
    # gives up without cancelling it.
    fake_queue.joined = SimpleNamespace(**vars(_ticket("queued")), tool_call_id="someone-else")
    fake_queue.statuses = [_ticket("queued")]
    clock = FakeClock()
    result = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=binding, sleep=clock.sleep, clock=clock,
    )
    assert isinstance(result, SandboxSessionError) and result.code == "capacity_full"
    assert fake_queue.cancelled == []

    # Joined a durable run's ticket that allocated: attach (register + bind)
    # without consuming it; the run consumes its own ticket when it resumes.
    fake_queue.joined = SimpleNamespace(**vars(_ticket("queued")), tool_call_id="call-of-the-run")
    allocated = _ticket("allocated", sandbox_id="sb_run")
    allocated.runtime_run_id = "run_1"
    fake_queue.statuses = [allocated]
    fake_queue.loads = 0
    result = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=binding, sleep=clock.sleep, clock=clock,
    )
    assert isinstance(result, SandboxSession) and result.sandbox_id == "sb_run"
    assert fake_queue.consumed == []
    assert fake_cache.store["sandbox:owner:ent_1:user_1:user"]["sandbox_id"] == "sb_run"


@pytest.mark.asyncio
async def test_acquire_reuses_registered_sandbox_in_durable_mode_without_reservation(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, monkeypatch
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    monkeypatch.setattr(local_sandbox_settings, "MANOR_RUNTIME_EXECUTION_MODE", "durable")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_COORDINATION_MODE", "external-runner")
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_SERVICE_URL", "")
    _seed_registry(fake_cache)
    fake_cache.store["sandbox:owner:ent_1:user_1:user"]["base_url"] = (
        "http://runner-7"
    )
    scripted_client.load_results = [_load_result()]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert session.sandbox_id == "sb_warm"
    assert session.reused is True
    assert scripted_client.calls[0] == {"handler": "init", "base_url": "http://runner-7"}


@pytest.mark.asyncio
async def test_credentialed_bundles_share_a_conversation_container_with_env_replaced(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """A credentialed skill reuses the conversation's container; load_skill
    carries its env and the next skill's env replaces it (a plain skill sends
    an empty env, which clears the secrets)."""
    from packages.core.services.sandbox_session_service import (
        SandboxBundle,
        SandboxOwner,
        acquire_sandbox_session,
    )

    def credentialed(skill_name: str, key: str) -> SandboxBundle:
        return SandboxBundle(
            skill_name=skill_name,
            files={"SKILL.md": "# skill", "run.py": "print('ok')"},
            env={key: "secret"},
            allowed_sensitive_keys=(key,),
            source="workspace",
        )

    plain = SandboxBundle(
        skill_name="pptx",
        files={"SKILL.md": "# skill", "run.py": "print('ok')"},
        source="workspace",
    )
    scripted_client.created_ids = ["sb_cred_a", "sb_cred_other"]
    scripted_client.load_results = [
        _load_result(sandbox_id="sb_cred_a"),
        _load_result(sandbox_id="sb_cred_a"),
        _load_result(sandbox_id="sb_cred_a"),
        _load_result(sandbox_id="sb_cred_a"),
    ]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    binding = _binding(conversation_id="conversation_1")

    first = await acquire_sandbox_session(owner=owner, bundle=credentialed("xlsx", "XLSX_KEY"), binding=binding)
    second = await acquire_sandbox_session(owner=owner, bundle=credentialed("xlsx", "XLSX_KEY"), binding=binding)
    third = await acquire_sandbox_session(owner=owner, bundle=credentialed("docx", "DOCX_KEY"), binding=binding)
    fourth = await acquire_sandbox_session(owner=owner, bundle=plain, binding=binding)
    other = await acquire_sandbox_session(
        owner=owner, bundle=credentialed("xlsx", "XLSX_KEY"), binding=_binding(conversation_id="conversation_2")
    )

    assert (first.sandbox_id, first.reused) == ("sb_cred_a", False)
    assert (second.sandbox_id, second.reused) == ("sb_cred_a", True)
    assert (third.sandbox_id, third.reused) == ("sb_cred_a", True)
    assert (fourth.sandbox_id, fourth.reused) == ("sb_cred_a", True)
    # The same user in another conversation: the same container, its env per load.
    assert (other.sandbox_id, other.reused) == ("sb_cred_a", True)

    loads = [call for call in scripted_client.calls if call["handler"] == "load_skill"]
    assert [(call["skill_name"], call["env"], call["allowed_sensitive_keys"]) for call in loads] == [
        ("xlsx", {"XLSX_KEY": "secret"}, ["XLSX_KEY"]),
        ("docx", {"DOCX_KEY": "secret"}, ["DOCX_KEY"]),
        ("pptx", {}, []),
        ("xlsx", {"XLSX_KEY": "secret"}, ["XLSX_KEY"]),
    ]
    assert "sandbox:owner:ent_1:user_1:user" in fake_cache.store
    assert not any(":skill:" in key for key in fake_cache.store)
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_credentialed_bundle_never_reuses_a_container_with_env_baked_at_create(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """An older service bakes the first skill's env into the container; a
    second credentialed skill must get a fresh container, not that one."""
    from packages.core.services.sandbox_session_service import (
        SandboxBundle,
        SandboxOwner,
        acquire_sandbox_session,
    )

    def credentialed(skill_name: str, key: str) -> SandboxBundle:
        return SandboxBundle(
            skill_name=skill_name,
            files={"SKILL.md": "# skill", "run.py": "print('ok')"},
            env={key: "secret"},
            allowed_sensitive_keys=(key,),
            source="workspace",
        )

    _seed_registry(fake_cache)
    fake_cache.store["sandbox:owner:ent_1:user_1:user"]["env_injection"] = "create"
    scripted_client.created_ids = ["sb_fresh"]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(
        owner=owner, bundle=credentialed("docx", "DOCX_KEY"), binding=_binding(conversation_id="conversation_1")
    )

    assert (session.sandbox_id, session.reused) == ("sb_fresh", False)
    assert not any(call["handler"] == "load_skill" for call in scripted_client.calls)
    assert session.env_injection == "exec"


@pytest.mark.asyncio
async def test_baked_env_container_serves_only_the_skill_it_holds(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """On an older runner env cannot be replaced: the same credentialed skill
    may reuse its own container, a different plain skill must not inherit the
    credentials, and plain-to-plain sharing stays allowed."""
    from packages.core.services.sandbox_session_service import (
        SandboxBundle,
        SandboxOwner,
        acquire_sandbox_session,
    )

    key = "sandbox:owner:ent_1:user_1:user"
    _seed_registry(fake_cache)
    from packages.core.services.sandbox_session_service import env_fingerprint

    fake_cache.store[key].update(
        {
            "env_injection": "create",
            "current_skill": "xlsx",
            "holds_credentials": True,
            "env_fingerprint": env_fingerprint({"K": "v"}),
        }
    )
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    binding = _binding(conversation_id="conversation_1")
    cred_xlsx = SandboxBundle(skill_name="xlsx", files={"SKILL.md": "#"}, env={"K": "v"}, allowed_sensitive_keys=("K",))
    plain_docx = SandboxBundle(skill_name="docx", files={"SKILL.md": "#"})

    scripted_client.load_results = [_load_result()]
    same = await acquire_sandbox_session(owner=owner, bundle=cred_xlsx, binding=binding)
    assert (same.sandbox_id, same.reused) == ("sb_warm", True)
    assert fake_cache.store[key]["holds_credentials"] is True

    scripted_client.reset()
    scripted_client.created_ids = ["sb_fresh"]
    other = await acquire_sandbox_session(owner=owner, bundle=plain_docx, binding=binding)
    assert (other.sandbox_id, other.reused) == ("sb_fresh", False)
    assert not any(c["handler"] == "load_skill" for c in scripted_client.calls)
    # The plain Skill got a container of its own; the credentialed one keeps its.
    assert fake_cache.store[f"{key}:skill:docx"]["holds_credentials"] is False
    assert fake_cache.store[key]["holds_credentials"] is True

    # A baked-env container that never held credentials still shares plainly.
    scripted_client.reset()
    fake_cache.store[key].update({"env_injection": "create", "current_skill": "docx", "holds_credentials": False})
    scripted_client.load_results = [_load_result()]
    plain_pptx = SandboxBundle(skill_name="pptx", files={"SKILL.md": "#"})
    shared = await acquire_sandbox_session(owner=owner, bundle=plain_pptx, binding=binding)
    assert shared.reused is True


@pytest.mark.asyncio
async def test_reuse_reports_blocked_env_keys(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from types import SimpleNamespace

    from packages.core.services.sandbox_session_service import (
        SandboxBundle,
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache)
    fake_cache.store["sandbox:owner:ent_1:user_1:user"]["env_injection"] = "exec"
    loaded = _load_result()
    scripted_client.load_results = [SimpleNamespace(**{**loaded.__dict__, "env_blocked": ["AWS_SECRET"]})]
    bundle = SandboxBundle(skill_name="xlsx", files={"SKILL.md": "#"}, env={"AWS_SECRET": "x"})

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=bundle,
        binding=_binding(conversation_id="conversation_1"),
    )

    assert session.reused is True
    assert session.env_blocked == ["AWS_SECRET"]


@pytest.mark.asyncio
async def test_allocated_session_records_the_runners_injection_mode(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import SandboxBundle, build_allocated_session

    bundle = SandboxBundle(skill_name="xlsx", files={"SKILL.md": "#"}, env={"K": "v"})
    scripted_client.health_payload = {"status": "ok", "env_injection": "exec"}
    session = await build_allocated_session(sandbox_id="sb_alloc", base_url="http://runner-1", bundle=bundle)
    assert (session.env_injection, session.current_skill, session.holds_credentials) == ("exec", "xlsx", True)
    assert session.loaded_skills == ["xlsx"]

    scripted_client.reset()
    scripted_client.health_payload = {"status": "ok"}
    session = await build_allocated_session(sandbox_id="sb_alloc", base_url="http://runner-1", bundle=bundle)
    assert session.env_injection == "create"


def test_no_module_outside_the_session_service_builds_a_session_record() -> None:
    """Every registry record is built inside sandbox_session_service (whose
    constructors record the injection mode); a bare SandboxSession(...) elsewhere
    would silently register "create" and retire the container on the next
    credentialed turn. Guards the class, not one call site."""
    import re
    from pathlib import Path

    offenders = []
    for root in ("packages", "apps"):
        for path in Path(root).rglob("*.py"):
            if path.name == "sandbox_session_service.py":
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"\bSandboxSession\(", text):
                offenders.append(str(path))
    assert offenders == [], offenders
    # A scheduler-allocated container is attached in one place, which builds
    # its record; every durable admission (Skills and the generic sandbox)
    # goes through it instead of re-implementing the attach.
    service = Path("packages/core/services/sandbox_session_service.py").read_text(encoding="utf-8")
    attach = service[service.index("async def attach_durable_reservation("):]
    assert "await build_allocated_session(" in attach.split("\nasync def ", 1)[0]
    for caller in ("packages/core/services/skill_service.py", "packages/core/ai/tools/sandbox_tools.py"):
        text = Path(caller).read_text(encoding="utf-8")
        assert "await attach_durable_reservation(" in text, caller
        assert "SandboxReservation.tool_call_id ==" not in text, caller


@pytest.mark.asyncio
async def test_same_skill_that_gained_credentials_does_not_reuse_a_baked_container(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxBundle,
        SandboxOwner,
        acquire_sandbox_session,
    )

    key = "sandbox:owner:ent_1:user_1:user"
    _seed_registry(fake_cache)
    fake_cache.store[key].update({"env_injection": "create", "current_skill": "xlsx", "holds_credentials": False})
    scripted_client.created_ids = ["sb_fresh"]
    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=SandboxBundle(skill_name="xlsx", files={"SKILL.md": "#"}, env={"K": "v"}),
        binding=_binding(conversation_id="conversation_1"),
    )
    assert (session.sandbox_id, session.reused) == ("sb_fresh", False)
    # Each step below tests the shared primary's rule, not the Skill's own
    # container the previous step created.
    fake_cache.store.pop(f"{key}:skill:xlsx")

    # Rotated credentials: same skill, same key, different value.
    from packages.core.services.sandbox_session_service import env_fingerprint

    scripted_client.reset()
    scripted_client.created_ids = ["sb_rotated"]
    fake_cache.store[key].update(
        {
            "env_injection": "create",
            "current_skill": "xlsx",
            "holds_credentials": True,
            "env_fingerprint": env_fingerprint({"K": "old"}),
        }
    )
    rotated = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=SandboxBundle(skill_name="xlsx", files={"SKILL.md": "#"}, env={"K": "new"}),
        binding=_binding(conversation_id="conversation_1"),
    )
    assert (rotated.sandbox_id, rotated.reused) == ("sb_rotated", False)
    fake_cache.store.pop(f"{key}:skill:xlsx")

    # Revoked credentials: same skill, env removed.
    scripted_client.reset()
    scripted_client.created_ids = ["sb_revoked"]
    fake_cache.store[key].update(
        {
            "env_injection": "create",
            "current_skill": "xlsx",
            "holds_credentials": True,
            "env_fingerprint": env_fingerprint({"K": "old"}),
        }
    )
    revoked = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=SandboxBundle(skill_name="xlsx", files={"SKILL.md": "#"}),
        binding=_binding(conversation_id="conversation_1"),
    )
    assert (revoked.sandbox_id, revoked.reused) == ("sb_revoked", False)


def test_env_fingerprint_is_keyed(monkeypatch, local_sandbox_settings) -> None:
    """A registry reader must not be able to confirm a guessed credential."""
    from packages.core.services.sandbox_session_service import env_fingerprint

    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_API_TOKEN", "key-one")
    one = env_fingerprint({"K": "v"})
    monkeypatch.setattr(local_sandbox_settings, "SANDBOX_API_TOKEN", "key-two")
    two = env_fingerprint({"K": "v"})
    assert one != two
    assert env_fingerprint({}) is None and env_fingerprint(None) is None


def test_session_record_round_trips_env_injection() -> None:
    from packages.core.services.sandbox_session_service import SandboxSession

    session = SandboxSession(
        sandbox_id="sb_1", base_url="http://r", created_at=1.0, last_acquired_at=2.0, env_injection="exec"
    )
    assert SandboxSession.from_record(session.to_record()).env_injection == "exec"
    rich = SandboxSession(
        sandbox_id="sb_2",
        base_url="http://r",
        created_at=1.0,
        last_acquired_at=2.0,
        env_injection="create",
        current_skill="xlsx",
        holds_credentials=True,
    )
    back = SandboxSession.from_record(rich.to_record())
    assert (back.current_skill, back.holds_credentials) == ("xlsx", True)
    # Records written before the field existed are treated conservatively.
    assert SandboxSession.from_record({"sandbox_id": "sb_old", "base_url": "http://r"}).env_injection == "create"


@pytest.mark.asyncio
async def test_credentialed_bundle_without_conversation_never_registers(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxBundle,
        SandboxOwner,
        acquire_sandbox_session,
    )

    bundle = SandboxBundle(
        skill_name="xlsx",
        files={"SKILL.md": "# skill", "run.py": "print('ok')"},
        env={"API_KEY": "secret"},
        source="workspace",
    )
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    session = await acquire_sandbox_session(owner=owner, bundle=bundle, binding=_binding(conversation_id=None))
    assert session.sandbox_id == "sb_new"
    assert fake_cache.store == {}
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_plain_bundles_reuse_the_users_container_across_conversations(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """Warmth is per USER: every conversation of the user reuses the one container."""

    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    scripted_client.created_ids = ["sb_c1", "sb_c2"]
    scripted_client.load_results = [_load_result(sandbox_id="sb_c1"), _load_result(sandbox_id="sb_c1")]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    first = await acquire_sandbox_session(
        owner=owner, bundle=_bundle("xlsx"), binding=_binding(conversation_id="conversation_1")
    )
    second = await acquire_sandbox_session(
        owner=owner, bundle=_bundle("docx"), binding=_binding(conversation_id="conversation_1")
    )
    third = await acquire_sandbox_session(
        owner=owner, bundle=_bundle("xlsx"), binding=_binding(conversation_id="conversation_2")
    )

    assert (first.sandbox_id, first.reused) == ("sb_c1", False)
    assert (second.sandbox_id, second.reused) == ("sb_c1", True)
    assert (third.sandbox_id, third.reused) == ("sb_c1", True)

    handlers = [c["handler"] for c in scripted_client.calls]
    assert handlers.count("create_from_builtin") == 1
    assert handlers.count("load_skill") == 2

    assert [key for key in fake_cache.store if key.startswith("sandbox:owner:")] == [
        "sandbox:owner:ent_1:user_1:user"
    ]
    assert fake_cache.store["sandbox:owner:ent_1:user_1:user"]["sandbox_id"] == "sb_c1"
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_generic_scope_is_conversation_prefixed(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxBundle,
        SandboxOwner,
        acquire_sandbox_session,
    )

    scripted_client.created_ids = ["sb_plain", "sb_generic"]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    generic = SandboxBundle(
        skill_name="generic-sandbox",
        files={"SKILL.md": "# generic"},
        reuse_scope="generic",
    )

    plain_session = await acquire_sandbox_session(
        owner=owner, bundle=_bundle("xlsx"), binding=_binding(conversation_id="conversation_1")
    )
    generic_session = await acquire_sandbox_session(
        owner=owner, bundle=generic, binding=_binding(conversation_id="conversation_1", agent_id="agent_2")
    )

    assert (plain_session.sandbox_id, plain_session.reused) == ("sb_plain", False)
    # The generic sandbox is the conversation's primary container: it is
    # shared, and joining it loads nothing (a reload would wipe the Skill).
    assert (generic_session.sandbox_id, generic_session.reused) == ("sb_plain", True)
    assert "load_skill" not in [c["handler"] for c in scripted_client.calls]
    assert [c["handler"] for c in scripted_client.calls].count("create_from_builtin") == 1

    primary = fake_cache.store["sandbox:owner:ent_1:user_1:user"]
    assert primary["sandbox_id"] == "sb_plain" and primary["current_skill"] == "xlsx"
    assert not [key for key in fake_cache.store if key.endswith(":generic")]
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_agents_in_one_conversation_share_one_container(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """The sandbox belongs to the user x conversation: every agent of the
    conversation (master, sub-agents, delegates) shares it, and each acquire
    records the acting agent on the binding for audit."""

    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    scripted_client.created_ids = ["sb_conversation", "sb_unexpected"]
    scripted_client.load_results = [_load_result("sb_conversation")]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    first = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding(agent_id="agent_1"))
    second = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding(agent_id="agent_2"))

    assert (first.sandbox_id, first.reused) == ("sb_conversation", False)
    assert (second.sandbox_id, second.reused) == ("sb_conversation", True)
    assert len([c for c in scripted_client.calls if c["handler"] == "create_from_builtin"]) == 1
    # Same Skill already loaded: an overlay reload, no /skill wipe.
    [load] = [c for c in scripted_client.calls if c["handler"] == "load_skill"]
    assert load["idle_threshold"] == 0
    assert [key for key in fake_cache.store if key.startswith("sandbox:owner:")] == [
        "sandbox:owner:ent_1:user_1:user"
    ]
    assert [context["agent_id"] for context in recorded_contexts] == ["agent_1", "agent_2"]
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_other_users_never_share_a_container_the_same_user_always_does(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    scripted_client.created_ids = ["sb_u1_c1", "sb_u2_c1", "sb_u1_c2"]
    scripted_client.load_results = [_load_result(sandbox_id="sb_u1_c1")]
    alice = SandboxOwner(entity_id="ent_1", user_id="user_1")
    bob = SandboxOwner(entity_id="ent_1", user_id="user_2")

    a = await acquire_sandbox_session(owner=alice, bundle=_bundle(), binding=_binding(conversation_id="c1"))
    # Another member of the same workspace conversation: their own sandbox.
    b = await acquire_sandbox_session(owner=bob, bundle=_bundle(), binding=_binding(conversation_id="c1"))
    # The same user in another conversation: the user's one sandbox.
    c = await acquire_sandbox_session(owner=alice, bundle=_bundle(), binding=_binding(conversation_id="c2"))

    assert [a.sandbox_id, b.sandbox_id, c.sandbox_id] == ["sb_u1_c1", "sb_u2_c1", "sb_u1_c1"]
    assert c.reused is True


@pytest.mark.asyncio
async def test_no_conversation_means_no_reuse_and_no_lock(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    session = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding(conversation_id=None))

    assert session.sandbox_id == "sb_new"
    assert session.reused is False
    assert "status" not in [c["handler"] for c in scripted_client.calls]
    assert fake_cache.store == {}
    assert fake_cache.leases == {}


def test_scope_segments_are_percent_encoded_and_cannot_collide() -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxBundle,
        SandboxOwner,
        sandbox_reuse_scope,
    )

    bundle_a = SandboxBundle(
        skill_name="x:skill:b",
        files={},
        env={"K": "v"},
    )
    binding_a = SandboxBinding(conversation_id="c1")
    bundle_b = SandboxBundle(
        skill_name="b",
        files={},
        env={"K": "v"},
    )
    binding_b = SandboxBinding(conversation_id="c1:skill:x")

    reusable_a, scope_a = sandbox_reuse_scope(bundle_a, binding_a)
    reusable_b, scope_b = sandbox_reuse_scope(bundle_b, binding_b)

    assert reusable_a is True
    assert reusable_b is True
    assert scope_a != scope_b

    owner = SandboxOwner(entity_id="e1", user_id="u1")
    assert owner.registry_key(scope_a) != owner.registry_key(scope_b)

    # A dedicated container whose Skill name carries ':' cannot forge another
    # conversation's scope either.
    dedicated_a = SandboxBundle(skill_name="a:b", files={}, config={"memory": "2g"})
    dedicated_b = SandboxBundle(skill_name="b", files={}, config={"memory": "2g"})
    _, dedicated_scope_a = sandbox_reuse_scope(dedicated_a, SandboxBinding(conversation_id="c:skill"))
    _, dedicated_scope_b = sandbox_reuse_scope(dedicated_b, SandboxBinding(conversation_id="c:skill:a"))
    assert dedicated_scope_a == "conversation:c%3Askill:skill:a%3Ab"
    assert dedicated_scope_b == "conversation:c%3Askill%3Aa:skill:b"

    # No conversation, no scope — plain or credentialed.
    assert sandbox_reuse_scope(SandboxBundle(skill_name="xlsx", files={}), SandboxBinding()) == (False, None)
    assert sandbox_reuse_scope(SandboxBundle(skill_name="xlsx", files={}, env={"K": "v"}), SandboxBinding()) == (
        False,
        None,
    )


def test_every_scope_is_conversation_prefixed() -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxBundle,
        sandbox_reuse_scope,
    )

    # The generic sandbox is the conversation's primary container.
    generic = SandboxBundle(skill_name="generic-sandbox", files={}, reuse_scope="generic")
    assert sandbox_reuse_scope(generic, SandboxBinding(conversation_id="c1", agent_id="a1")) == (
        True,
        "conversation:c1",
    )
    # A bundle with its own container config runs in a dedicated container of
    # the same conversation.
    remotion = SandboxBundle(skill_name="remotion", files={}, config={"memory": "2g"})
    assert sandbox_reuse_scope(remotion, SandboxBinding(conversation_id="c1")) == (
        True,
        "conversation:c1:skill:remotion",
    )
    plain = SandboxBundle(skill_name="xlsx", files={})
    assert sandbox_reuse_scope(plain, SandboxBinding(conversation_id="c1")) == (True, "conversation:c1")
    # Credentialed bundles share the conversation scope: load_skill replaces
    # the exec-time env, so the previous skill's secrets never carry over.
    credentialed = SandboxBundle(skill_name="xlsx", files={}, env={"K": "v"})
    assert sandbox_reuse_scope(credentialed, SandboxBinding(conversation_id="c1")) == (
        True,
        "conversation:c1",
    )

    # Without a conversation there is nothing to key on, so nothing is reused.
    for bundle in (generic, plain, credentialed):
        assert sandbox_reuse_scope(bundle, SandboxBinding()) == (False, None)
        assert sandbox_reuse_scope(bundle, SandboxBinding(conversation_id="")) == (False, None)


def test_consumed_reservations_do_not_pin_runner_slots() -> None:
    from packages.core.models.runtime_run import SandboxReservationStatus
    from packages.core.services.sandbox_queue_service import _RUNNER_SLOT_RESERVATION_STATUSES

    # Warm containers outlive their reservation; only in-flight allocation states hold a slot.
    assert SandboxReservationStatus.CONSUMED.value not in _RUNNER_SLOT_RESERVATION_STATUSES
    assert SandboxReservationStatus.ALLOCATING.value in _RUNNER_SLOT_RESERVATION_STATUSES
    assert SandboxReservationStatus.ALLOCATED.value in _RUNNER_SLOT_RESERVATION_STATUSES


@pytest.mark.asyncio
async def test_heartbeat_survives_transient_cache_blip(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, monkeypatch
) -> None:
    import packages.core.cache as cache_module
    from packages.core.services import sandbox_session_service as svc
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    monkeypatch.setattr(svc, "_LOCK_HEARTBEAT_SECONDS", 0.01)
    scripted_client.create_delay = 0.1

    calls: list[int] = []

    async def flaky_extend_lease(key, token, ttl=60):
        calls.append(1)
        if len(calls) == 1:
            return None
        return await fake_cache.extend_lease(key, token, ttl=ttl)

    monkeypatch.setattr(cache_module.cache, "extend_lease", flaky_extend_lease)

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    session = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert len(calls) >= 2
    assert len(fake_cache.extended) >= 1
    assert session.sandbox_id == "sb_new"
    assert fake_cache.leases == {}


def test_suite_runs_against_an_explicit_redis_test_resource() -> None:
    """The suite must never inherit a developer's runtime Redis URL."""

    import os

    from packages.core.config import get_settings

    url = get_settings().REDIS_URL
    assert url == os.environ.get("TEST_REDIS_URL") or url.startswith("unix://"), url


@pytest.mark.asyncio
async def test_warm_sandbox_base_url_reads_the_current_registry_record(fake_cache, local_sandbox_settings) -> None:
    from packages.core.services.sandbox_session_service import SandboxOwner, warm_sandbox_base_url

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    registry_key = owner.registry_key("conversation:conversation_1")
    fake_cache.store[registry_key] = {"sandbox_id": "sb_warm", "base_url": "http://runner-7"}
    fake_cache.store["sandbox:warm:sb_warm"] = registry_key

    assert await warm_sandbox_base_url("sb_warm") == "http://runner-7"
    # A superseded container (marker points at a key that now names another sandbox) is not routed.
    fake_cache.store[registry_key] = {"sandbox_id": "sb_newer", "base_url": "http://runner-7"}
    assert await warm_sandbox_base_url("sb_warm") is None
    assert await warm_sandbox_base_url("sb_unknown") is None


# ── warm reuse respects the container's config ──
#
# One rule (sandbox-user-conversation): a container that cannot serve a
# bundle is never destroyed while it serves other work. The user's shared
# general container is never replaced; a dedicated one only while nothing
# runs or waits in it. Otherwise the bundle gets a dedicated container of
# the user's alongside.


_DEFAULT_CONTAINER = {
    "memory": "512m", "cpus": 1.0, "pids_limit": 256, "workdir_tmpfs_size": "256m", "network": "bridge",
}
_GENERAL_KEY = "sandbox:owner:ent_1:user_1:user"
_REMOTION_KEY = "sandbox:owner:ent_1:user_1:user:skill:remotion-render"


def _sized_bundle(config, skill_name="remotion-render"):
    from packages.core.services.sandbox_session_service import SandboxBundle

    return SandboxBundle(
        skill_name=skill_name,
        files={"SKILL.md": "# skill", "run.py": "print('ok')"},
        source="builtin",
        config=config,
    )


def _seed_sized_registry(fake_cache, *, requested, effective=None, key=_REMOTION_KEY):
    """A warm container of user_1 under ``key`` (a sized Skill's dedicated one by default)."""

    fake_cache.store[key] = {
        "sandbox_id": "sb_warm",
        "base_url": "http://sandbox-service",
        "created_at": 1.0,
        "last_acquired_at": 1.0,
        "loaded_skills": ["remotion-render"],
        "current_skill": "remotion-render",
        "env_injection": "exec",
        "requested_config": requested,
    }
    if effective is not None:
        fake_cache.store[key]["container_config"] = effective
    return key


def _status_with(config, **extra):
    return SimpleNamespace(status="ready", workdir="/skill", config=dict(config), **extra)


def _fit_key(role, config):
    from packages.core.services.sandbox_session_service import _misfit_role

    return f"sandbox:owner:ent_1:user_1:user:{_misfit_role(_sized_bundle(config), role)}"


@pytest.mark.asyncio
async def test_an_idle_undersized_dedicated_container_is_replaced_in_its_own_scope(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """A 512m container warmed for a Skill's dedicated role must not be handed
    (via load_skill) to that Skill now asking 2g: it would OOM. Nothing runs or
    waits in it, so it is replaced in place by a right-sized one."""

    from packages.core.services.sandbox_session_service import SandboxOwner, acquire_sandbox_session

    key = _seed_sized_registry(fake_cache, requested={"memory": "512m"})
    scripted_client.status_results = [_status_with(_DEFAULT_CONTAINER)]
    remotion = {"memory": "2g", "workdir_tmpfs_size": "1g", "pids_limit": 512}

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=_sized_bundle(remotion),
        binding=_binding(),
    )

    handlers = [call["handler"] for call in scripted_client.calls]
    assert "load_skill" not in handlers
    assert {"handler": "destroy", "sandbox_id": "sb_warm"} in scripted_client.calls
    create = next(call for call in scripted_client.calls if call["handler"] == "create_from_builtin")
    assert create["config"] == remotion
    assert session.sandbox_id == "sb_new" and session.reused is False
    assert fake_cache.store[key]["sandbox_id"] == "sb_new", "the replacement takes the same role"
    assert fake_cache.store[key]["requested_config"] == remotion


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "busy",
    [
        {"active_execution_ids": ["exec-other-conversation"]},
        # A command waiting for a slot (a durable run parked on it) is work too.
        {"queued_execution_ids": ["exec-parked"]},
        {"active_execution_id": "exec-older-runner"},
    ],
    ids=["running", "queued", "single-exec-runner"],
)
async def test_an_undersized_dedicated_container_with_work_in_it_gets_a_sized_one_alongside(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, busy
) -> None:
    from packages.core.services.sandbox_session_service import SandboxOwner, acquire_sandbox_session

    key = _seed_sized_registry(fake_cache, requested={"memory": "512m"})
    scripted_client.status_results = [_status_with(_DEFAULT_CONTAINER, **busy)]
    clock = FakeClock()

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=_sized_bundle({"memory": "2g"}),
        binding=_binding(),
        sleep=clock.sleep,
        clock=clock,
    )

    handlers = [call["handler"] for call in scripted_client.calls]
    assert "destroy" not in handlers, "never destroy a container work runs or waits in"
    assert clock.sleeps == [], "never busy: no waiting for the other work to end"
    assert session.sandbox_id == "sb_new"
    assert fake_cache.store[key]["sandbox_id"] == "sb_warm"
    fit_key = _fit_key("skill:remotion-render", {"memory": "2g"})
    assert fit_key.startswith(_REMOTION_KEY + ":fit:")
    assert fake_cache.store[fit_key]["sandbox_id"] == "sb_new"
    assert fake_cache.store[fit_key]["requested_config"] == {"memory": "2g"}


@pytest.mark.asyncio
async def test_the_shared_general_container_is_never_replaced_for_a_skill_it_cannot_serve(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """The general container (here created with low explicit limits and no
    network) serves all of the user's work: even idle it is not replaced. The
    Skill wanting the runner's defaults gets its own container alongside."""

    from packages.core.services.sandbox_session_service import SandboxOwner, acquire_sandbox_session

    _seed_sized_registry(fake_cache, requested={"network": "none", "pids_limit": 128}, key=_GENERAL_KEY)
    scripted_client.status_results = [
        _status_with({**_DEFAULT_CONTAINER, "network": "none", "pids_limit": 128})
    ]
    scripted_client.health_payload = {
        "status": "ok", "sandbox_image_available": True,
        "container_defaults": {**_DEFAULT_CONTAINER, "memory_mb": 512},
    }

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"), bundle=_bundle(), binding=_binding(),
    )

    handlers = [call["handler"] for call in scripted_client.calls]
    assert "destroy" not in handlers and "load_skill" not in handlers
    assert session.sandbox_id == "sb_new"
    assert fake_cache.store[_GENERAL_KEY]["sandbox_id"] == "sb_warm"
    assert fake_cache.store["sandbox:owner:ent_1:user_1:user:skill:xlsx"]["sandbox_id"] == "sb_new"


@pytest.mark.asyncio
async def test_a_generic_bundle_the_general_container_cannot_serve_gets_a_sized_one_alongside(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        GENERIC_SANDBOX_REUSE_SCOPE,
        GENERIC_SANDBOX_SKILL_NAME,
        SandboxBundle,
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_sized_registry(fake_cache, requested={"network": "none"}, key=_GENERAL_KEY)
    scripted_client.status_results = [_status_with({**_DEFAULT_CONTAINER, "network": "none"})]
    scripted_client.health_payload = {
        "status": "ok", "sandbox_image_available": True,
        "container_defaults": {**_DEFAULT_CONTAINER, "memory_mb": 512},
    }
    generic = SandboxBundle(
        skill_name=GENERIC_SANDBOX_SKILL_NAME, files={}, source="builtin", reuse_scope=GENERIC_SANDBOX_REUSE_SCOPE,
    )

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"), bundle=generic, binding=_binding(skill_id=""),
    )

    assert "destroy" not in [call["handler"] for call in scripted_client.calls]
    assert session.sandbox_id == "sb_new"
    assert fake_cache.store[_GENERAL_KEY]["sandbox_id"] == "sb_warm"
    fit = [key for key in fake_cache.store if key.startswith(_GENERAL_KEY + ":fit:")]
    assert len(fit) == 1 and fake_cache.store[fit[0]]["sandbox_id"] == "sb_new"


@pytest.mark.asyncio
@pytest.mark.parametrize("holds_credentials", [True, False])
async def test_generic_work_never_attaches_to_credentials_an_old_runner_cannot_isolate(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, holds_credentials
) -> None:
    """On a runner without Skill slots a Skill's env reaches every command in
    its container (baked at create, or handed to every exec): the user's
    general container that a credentialed Skill created is never shared with
    generic work, which gets its own container alongside instead."""

    from packages.core.services.sandbox_session_service import (
        GENERIC_SANDBOX_REUSE_SCOPE,
        GENERIC_SANDBOX_SKILL_NAME,
        SandboxBundle,
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_sized_registry(fake_cache, requested={}, key=_GENERAL_KEY)
    fake_cache.store[_GENERAL_KEY].update(
        env_injection="create", skill_slots=False, holds_credentials=holds_credentials,
    )
    scripted_client.status_results = [_status_with(_DEFAULT_CONTAINER)]
    scripted_client.health_payload = {
        "status": "ok", "sandbox_image_available": True,
        "container_defaults": {**_DEFAULT_CONTAINER, "memory_mb": 512},
    }
    generic = SandboxBundle(
        skill_name=GENERIC_SANDBOX_SKILL_NAME, files={}, source="builtin", reuse_scope=GENERIC_SANDBOX_REUSE_SCOPE,
    )

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"), bundle=generic, binding=_binding(skill_id=""),
    )

    assert fake_cache.store[_GENERAL_KEY]["sandbox_id"] == "sb_warm"
    if holds_credentials:
        assert session.sandbox_id == "sb_new"
        assert "destroy" not in [call["handler"] for call in scripted_client.calls], "the Skill keeps its container"
    else:
        assert session.sandbox_id == "sb_warm" and session.reused is True


@pytest.mark.asyncio
async def test_reuse_accepts_a_warm_container_at_least_as_large(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import SandboxOwner, acquire_sandbox_session

    key = _seed_sized_registry(fake_cache, requested={"memory": "2g", "pids_limit": 512})
    scripted_client.status_results = [
        _status_with({**_DEFAULT_CONTAINER, "memory": "2g", "pids_limit": 512, "workdir_tmpfs_size": "1g"})
    ]
    scripted_client.load_results = [_load_result()]
    # pids_limit is left to the default by this bundle but was explicit at
    # create: it is compared against the runner's default (256 <= 512).
    scripted_client.health_payload = {
        "status": "ok", "sandbox_image_available": True,
        "container_defaults": {**_DEFAULT_CONTAINER, "memory_mb": 512},
    }

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        # A smaller ask fits in the 2g container.
        bundle=_sized_bundle({"memory": "1g", "workdir_tmpfs_size": "512m"}),
        binding=_binding(),
    )

    assert session.sandbox_id == "sb_warm" and session.reused is True
    assert "create_from_builtin" not in [call["handler"] for call in scripted_client.calls]
    # Loading a skill does not change the container: its origin is kept.
    assert fake_cache.store[key]["requested_config"] == {"memory": "2g", "pids_limit": 512}
    assert fake_cache.store[key]["container_config"]["memory"] == "2g"


@pytest.mark.asyncio
async def test_a_default_request_reuses_a_container_made_with_defaults(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import SandboxOwner, acquire_sandbox_session

    _seed_sized_registry(fake_cache, requested={}, key=_GENERAL_KEY)
    scripted_client.status_results = [_status_with(_DEFAULT_CONTAINER)]
    scripted_client.load_results = [_load_result()]

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"), bundle=_bundle(), binding=_binding(),
    )

    assert session.sandbox_id == "sb_warm"
    # Nothing to compare against the runner's defaults: no extra health call.
    assert "health" not in [call["handler"] for call in scripted_client.calls]


@pytest.mark.asyncio
async def test_a_clamped_container_is_reused_for_the_same_request(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """Video asks 4g; a 2 GiB runner clamps it to 1792m. The next video call
    asks 4g again and must reuse that container, not churn an identical one."""

    from packages.core.services.sandbox_session_service import SandboxOwner, acquire_sandbox_session

    video = {"network": "none", "memory": "4g", "cpus": 4.0, "pids_limit": 512, "workdir_tmpfs_size": "2g"}
    _seed_sized_registry(fake_cache, requested=video)
    scripted_client.status_results = [
        _status_with({**_DEFAULT_CONTAINER, **video, "memory": "1792m", "cpus": 1.0})
    ]
    scripted_client.load_results = [_load_result()]

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"), bundle=_sized_bundle(video), binding=_binding(),
    )

    assert session.sandbox_id == "sb_warm" and session.reused is True


@pytest.mark.asyncio
async def test_a_different_network_posture_is_never_reused(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import SandboxOwner, acquire_sandbox_session

    _seed_sized_registry(fake_cache, requested={})
    scripted_client.status_results = [_status_with(_DEFAULT_CONTAINER)]

    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=_sized_bundle({"network": "none"}),
        binding=_binding(),
    )

    assert session.sandbox_id == "sb_new"
    assert "load_skill" not in [call["handler"] for call in scripted_client.calls]


@pytest.mark.asyncio
async def test_create_records_the_requested_and_effective_config(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, monkeypatch
) -> None:
    from packages.core.services.sandbox_session_service import SandboxOwner, acquire_sandbox_session

    original = _fake_create_result

    def created(sandbox_id, **kwargs):
        result = original(sandbox_id, **kwargs)
        result.config = {**_DEFAULT_CONTAINER, "memory": "1792m"}
        return result

    monkeypatch.setitem(globals(), "_fake_create_result", created)

    await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=_sized_bundle({"memory": "4g"}),
        binding=_binding(),
    )

    record = fake_cache.store[_REMOTION_KEY]
    assert record["requested_config"] == {"memory": "4g"}
    assert record["container_config"]["memory"] == "1792m"


def test_a_sandbox_is_in_use_while_any_command_runs_or_waits_in_it() -> None:
    from packages.core.services.sandbox_session_service import _sandbox_in_use

    assert not _sandbox_in_use(SimpleNamespace(status="ready"))
    assert not _sandbox_in_use(SimpleNamespace(status="ready", active_execution_ids=[], queued_execution_ids=[]))
    assert _sandbox_in_use(SimpleNamespace(status="ready", active_execution_ids=["a"]))
    assert _sandbox_in_use(SimpleNamespace(status="ready", queued_execution_ids=["q"]))
    assert _sandbox_in_use(SimpleNamespace(status="executing"))
def test_tool_owned_bundles_never_bind_a_sandbox_tool_slot() -> None:
    """SESSION and ONE_SHOT containers belong to the tool that admitted them:
    admitting one must never write (or overwrite) the agent's generic or
    Skill binding, and a ONE_SHOT has no reuse scope at all."""

    from packages.core.services.sandbox_session_service import (
        GENERIC_SANDBOX_REUSE_SCOPE,
        SandboxBinding,
        SandboxBundle,
        SandboxLifetime,
        SandboxOwner,
        sandbox_binding_scope,
        sandbox_reuse_scope,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    binding = SandboxBinding(conversation_id="conv_1", agent_id="agent_1", runtime_tool_call_id="call_1")
    session = SandboxBundle(
        skill_name="video-edit-runtime", files={}, reuse_scope="video-edit",
        lifetime=SandboxLifetime.SESSION, purpose="video-session",
    )
    one_shot = SandboxBundle(skill_name="bash-tool", files={}, lifetime=SandboxLifetime.ONE_SHOT, purpose="bash")
    warm = SandboxBundle(skill_name="xlsx", files={})

    assert session.binding_kind is None and one_shot.binding_kind is None
    assert sandbox_binding_scope(owner, session, binding) is None
    assert sandbox_binding_scope(owner, one_shot, binding) is None
    assert sandbox_binding_scope(owner, warm, binding) is not None
    # A SESSION container is a dedicated role container of the user's
    # (sandbox-user-conversation), whatever conversation admits it.
    assert sandbox_reuse_scope(session, binding, owner) == (True, "user:video-edit")
    assert sandbox_reuse_scope(one_shot, binding, owner) == (False, None)
    # Each container of one durable call has its own reservation key.
    assert session.reservation_suffix == ":video-session" and warm.reservation_suffix == ""
    with pytest.raises(ValueError):
        SandboxBundle(skill_name="s", files={}, lifetime=SandboxLifetime.SESSION)
    with pytest.raises(ValueError):
        SandboxBundle(
            skill_name="s", files={}, reuse_scope=GENERIC_SANDBOX_REUSE_SCOPE, lifetime=SandboxLifetime.ONE_SHOT,
        )


# ── PERSISTENT: one long-lived container per owner scope, reached without a conversation ──

_DESKTOP_KEY = "sandbox:owner:ent_1:user_1:user:desktop"


def _persistent_bundle(*, isolation_key: str = "", isolation_ref: str | None = None):
    from packages.core.services.sandbox_session_service import SandboxBundle, SandboxLifetime

    return SandboxBundle(
        skill_name="desktop", files={"README.md": "desktop\n"}, source="workspace", auto_install=False,
        config={"memory": "2g"}, lifetime=SandboxLifetime.PERSISTENT, reuse_scope="desktop",
        isolation_key=isolation_key, isolation_ref=isolation_ref,
    )


def test_a_persistent_bundle_is_owner_scoped_and_needs_no_conversation() -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxBundle,
        SandboxLifetime,
        SandboxOwner,
        sandbox_binding_scope,
        sandbox_reuse_scope,
    )

    bundle = _persistent_bundle()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    # Tool-owned: never a model-visible sandbox slot.
    assert bundle.binding_kind is None
    assert sandbox_binding_scope(owner, bundle, SandboxBinding()) is None
    for binding in (SandboxBinding(), SandboxBinding(conversation_id="conv_1")):
        assert sandbox_reuse_scope(bundle, binding, owner) == (True, "user:desktop")
    # It belongs to a person; user-less work has no persistent container.
    assert sandbox_reuse_scope(bundle, SandboxBinding(), SandboxOwner(entity_id="ent_1", user_id=None)) == (False, None)
    with pytest.raises(ValueError):
        SandboxBundle(skill_name="s", files={}, lifetime=SandboxLifetime.PERSISTENT)
    with pytest.raises(ValueError):
        SandboxBundle(skill_name="s", files={}, isolation_ref="ref:1")  # a ref names its key


@pytest.mark.asyncio
async def test_a_persistent_container_is_reused_across_admissions_not_replaced(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import SandboxBinding, SandboxOwner, acquire_sandbox_session

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    first = await acquire_sandbox_session(owner=owner, bundle=_persistent_bundle(isolation_key="k1"), binding=SandboxBinding())
    assert first.sandbox_id == "sb_new"
    assert fake_cache.store[_DESKTOP_KEY]["isolation_key"] == "k1"

    scripted_client.calls.clear()
    scripted_client.status_results = [_status_with({**_DEFAULT_CONTAINER, "memory": "2g"})]
    scripted_client.load_results = [_load_result(sandbox_id="sb_new")]
    again = await acquire_sandbox_session(owner=owner, bundle=_persistent_bundle(isolation_key="k1"), binding=SandboxBinding())

    handlers = [call["handler"] for call in scripted_client.calls]
    assert again.sandbox_id == "sb_new" and again.reused is True
    assert "destroy" not in handlers and "create_from_files" not in handlers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lifetime",
    ["persistent", "warm"],
    ids=["persistent-desktop", "warm-skill"],
)
async def test_a_container_is_never_reused_for_another_isolation_key(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, lifetime
) -> None:
    """A container made for one isolation (in the cloud: one exit IP) never
    serves a bundle that asks for another; an idle one is replaced."""

    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxBundle,
        SandboxOwner,
        acquire_sandbox_session,
    )

    if lifetime == "persistent":
        key, bundle, binding = _DESKTOP_KEY, _persistent_bundle(isolation_key="k2"), SandboxBinding()
        skill = "desktop"
    else:
        key, skill = _REMOTION_KEY, "remotion-render"
        bundle = SandboxBundle(
            skill_name=skill, files={"SKILL.md": "# skill"}, source="builtin", config={"memory": "2g"},
            isolation_key="k2",
        )
        binding = _binding()
    fake_cache.store[key] = {
        "sandbox_id": "sb_warm", "base_url": "http://sandbox-service", "created_at": 1.0,
        "last_acquired_at": 1.0, "loaded_skills": [skill], "current_skill": skill,
        "env_injection": "exec", "requested_config": {"memory": "2g"}, "isolation_key": "k1",
    }
    scripted_client.status_results = [_status_with({**_DEFAULT_CONTAINER, "memory": "2g"})]

    session = await acquire_sandbox_session(owner=SandboxOwner(entity_id="ent_1", user_id="user_1"), bundle=bundle, binding=binding)

    assert {"handler": "destroy", "sandbox_id": "sb_warm"} in scripted_client.calls
    assert session.sandbox_id == "sb_new"
    assert fake_cache.store[key]["isolation_key"] == "k2"


@pytest.mark.asyncio
async def test_an_isolation_ref_is_resolved_into_the_create_and_fails_closed_when_unknown(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, monkeypatch
) -> None:
    from packages.core.services import sandbox_isolation
    from packages.core.services.sandbox_session_service import SandboxBinding, SandboxOwner, acquire_sandbox_session

    monkeypatch.setattr(sandbox_isolation, "_RESOLVERS", {})

    async def resolve(ref: str) -> dict:
        assert ref == "test:7"
        return {"network_profile": {"route": "seven"}}

    sandbox_isolation.register_isolation_resolver("test:", resolve)
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(
        owner=owner, bundle=_persistent_bundle(isolation_key="k7", isolation_ref="test:7"), binding=SandboxBinding(),
    )
    create = next(call for call in scripted_client.calls if call["handler"] == "create_from_files")
    assert create["extra"] == {"network_profile": {"route": "seven"}}
    assert session.sandbox_id == "sb_new"

    fake_cache.store.clear()
    scripted_client.calls.clear()
    failed = await acquire_sandbox_session(
        owner=owner, bundle=_persistent_bundle(isolation_key="k8", isolation_ref="unknown:8"), binding=SandboxBinding(),
    )
    assert getattr(failed, "code", None) == "create_failed"
    assert failed.cause == "isolation_unavailable"
    assert not any(call["handler"].startswith("create_from") for call in scripted_client.calls)


def test_a_ticket_carries_the_isolation_ref_and_key_never_what_they_resolve_to() -> None:
    from packages.core.services.sandbox_session_service import _bundle_from_payload, _reservation_request_payload

    bundle = _persistent_bundle(isolation_key="k9", isolation_ref="test:9")
    payload = _reservation_request_payload(bundle)
    assert payload["isolation_ref"] == "test:9" and payload["isolation_key"] == "k9"
    restored = _bundle_from_payload(payload, bundle)
    assert (restored.isolation_ref, restored.isolation_key) == ("test:9", "k9")


@pytest.mark.asyncio
async def test_a_persistent_container_is_destroyed_and_forgotten_only_when_its_owner_retires_it(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxOwner,
        acquire_sandbox_session,
        discard_session_sandbox,
    )

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    bundle = _persistent_bundle(isolation_key="k1")
    session = await acquire_sandbox_session(owner=owner, bundle=bundle, binding=SandboxBinding())
    assert _DESKTOP_KEY in fake_cache.store

    await discard_session_sandbox(owner=owner, bundle=bundle, binding=SandboxBinding(), sandbox_id=session.sandbox_id)

    assert {"handler": "destroy", "sandbox_id": session.sandbox_id} in scripted_client.calls
    assert _DESKTOP_KEY not in fake_cache.store


@pytest.mark.asyncio
async def test_a_background_service_waits_on_its_own_ticket_for_a_persistent_container(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    """Outside every runtime (no run, task or conversation) a service booting
    a PERSISTENT container waits on a ticket of its own, which carries only
    the isolation reference."""
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxOwner,
        SandboxSession,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    fake_queue.statuses = [_ticket("allocated", sandbox_id="sb_desktop")]
    clock = FakeClock()

    result = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=_persistent_bundle(isolation_key="k1", isolation_ref="test:1"),
        binding=SandboxBinding(background_requester="service:boot-1"),
        sleep=clock.sleep,
        clock=clock,
    )

    assert isinstance(result, SandboxSession) and result.sandbox_id == "sb_desktop"
    [enqueued] = fake_queue.enqueued
    assert enqueued["requester_kind"] == "task"
    assert enqueued["requester_id"] == "service:boot-1"
    assert enqueued["scope_key"] == _DESKTOP_KEY
    assert enqueued["request_payload"]["isolation_ref"] == "test:1"
    assert fake_cache.store[_DESKTOP_KEY]["sandbox_id"] == "sb_desktop"
    assert fake_cache.store[_DESKTOP_KEY]["isolation_key"] == "k1"
