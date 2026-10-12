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

    anonymous = SandboxOwner(entity_id="ent_1", user_id="")
    assert anonymous.registrable is False
    with pytest.raises(ValueError):
        anonymous.registry_key("conversation:c1")
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
async def test_anonymous_owner_skips_registry(fake_cache) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        SandboxSession,
        load_sandbox_session,
        register_sandbox_session,
    )

    owner = SandboxOwner("ent_1", "")
    scope = "conversation:conversation_1"
    assert await load_sandbox_session(owner, scope=scope) is None

    session = SandboxSession(
        sandbox_id="sb_1",
        base_url="http://sandbox-service",
        created_at=1.0,
        last_acquired_at=1.0,
    )
    assert await register_sandbox_session(owner, session, scope=scope) is False
    assert fake_cache.store == {}


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
async def test_release_only_unbinds_matching_conversation(monkeypatch, fake_cache) -> None:
    from packages.core.services import sandbox_session_service
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        release_sandbox_session,
    )

    deleted: list[str] = []

    async def fake_delete(conversation_id):
        deleted.append(conversation_id)

    async def fake_load_other(conversation_id):
        return {"sandbox_id": "sb_other"}

    monkeypatch.setattr(sandbox_session_service, "runtime_load_sandbox_context", fake_load_other)
    monkeypatch.setattr(sandbox_session_service, "runtime_delete_sandbox_context", fake_delete)

    owner = SandboxOwner("ent_1", "user_1")
    # sb_1 is the conversation scope's warm session, so every release below
    # keeps the container and only the unbind behaviour is under test.
    registry_key = owner.registry_key("conversation:conversation_1")
    fake_cache.store[registry_key] = {"sandbox_id": "sb_1", "base_url": "http://sandbox-service"}
    fake_cache.store["sandbox:warm:sb_1"] = registry_key

    assert await release_sandbox_session(owner=owner, sandbox_id="sb_1", conversation_id="conversation_1") == "kept"
    assert deleted == []

    async def fake_load_matching(conversation_id):
        return {"sandbox_id": "sb_1"}

    monkeypatch.setattr(sandbox_session_service, "runtime_load_sandbox_context", fake_load_matching)
    assert await release_sandbox_session(owner=owner, sandbox_id="sb_1", conversation_id="conversation_1") == "kept"
    assert deleted == ["conversation_1"]

    assert await release_sandbox_session(owner=owner, sandbox_id="sb_1", conversation_id=None) == "kept"
    assert deleted == ["conversation_1"]


@pytest.mark.asyncio
async def test_discard_one_shot_sandbox_destroys_only_matching_owner_scope(
    fake_cache, scripted_client, local_sandbox_settings,
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxOwner, SandboxSession, discard_sandbox_session,
        register_sandbox_session,
    )

    owner = SandboxOwner("ent_1", "user_1")
    scope = "conversation:room-run:agent:agent_1"
    session = SandboxSession(
        sandbox_id="sb_room", base_url="http://sandbox-service",
        created_at=1.0, last_acquired_at=1.0,
    )
    assert await register_sandbox_session(owner, session, scope=scope)
    assert await discard_sandbox_session(
        owner=SandboxOwner("ent_1", "other_user"), sandbox_id="sb_room",
        conversation_id="room-run", agent_id="agent_1",
    ) == "not_owned"
    assert fake_cache.store[owner.registry_key(scope)]["sandbox_id"] == "sb_room"
    assert await discard_sandbox_session(
        owner=owner, sandbox_id="sb_room", conversation_id="room-run",
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

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_warm", conversation_id=None)

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

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_warm", conversation_id=None)

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

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_orphan", conversation_id=None)

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

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_old", conversation_id=None)

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

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_orphan", conversation_id=None)

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

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_unknown", conversation_id=None)

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

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_orphan", conversation_id=None)

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

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_old", conversation_id=None, db=object())

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
    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_orphan", conversation_id=None)

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

    outcome = await release_sandbox_session(owner=owner, sandbox_id="sb_orphan", conversation_id=None)

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

    async def fake_init(conversation_id, sandbox_id, skill_id, **kwargs):
        contexts.append(
            {
                "conversation_id": conversation_id,
                "sandbox_id": sandbox_id,
                "skill_id": skill_id,
                **kwargs,
            }
        )
        return {}

    monkeypatch.setattr(svc, "runtime_init_sandbox_context", fake_init)
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
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["sandbox_id"]
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
    assert "sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1" not in fake_cache.store


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

    monkeypatch.setattr(svc, "runtime_init_sandbox_context", fail_context)

    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")
    result = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert isinstance(result, SandboxSessionError)
    assert result.code == "context_persist_failed"
    assert "owner context" in result.message
    assert {"handler": "destroy", "sandbox_id": "sb_new"} in scripted_client.calls
    assert "sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1" not in fake_cache.store


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
    fake_cache.store[f"sandbox:owner:{entity}:{user}:conversation:{conversation}:agent:{agent}"] = {
        "sandbox_id": sandbox_id,
        "base_url": "http://sandbox-service",
        "created_at": 1.0,
        "last_acquired_at": 1.0,
        "loaded_skills": ["docx"],
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
    warm_key = "sandbox:owner:ent_1:user_1:conversation:conversation_2:agent:agent_1"
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
            "sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1",
            "sandbox:owner:ent_2:user_1:conversation:conversation_1:agent:agent_1",
            "sandbox:owner:ent_1:user_2:conversation:conversation_1:agent:agent_1",
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
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["sandbox_id"]
        == "sb_new"
    )


@pytest.mark.asyncio
async def test_acquire_waits_while_sandbox_is_busy(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    from packages.core.services.sandbox_sdk.exceptions import SandboxError
    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    _seed_registry(fake_cache)
    scripted_client.load_results = [
        SandboxError("busy", status_code=409),
        SandboxError("busy", status_code=409),
        _load_result(),
    ]
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    session = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=_binding(), sleep=clock.sleep, clock=clock
    )

    assert session.sandbox_id == "sb_warm"
    assert session.reused is True
    assert clock.sleeps == [1, 1]


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

    _seed_registry(fake_cache)
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
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["sandbox_id"]
        == "sb_warm"
    )


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
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["sandbox_id"]
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

    monkeypatch.setattr(svc, "runtime_init_sandbox_context", fail_context)
    _seed_registry(fake_cache)
    scripted_client.load_results = [_load_result()]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

    assert isinstance(result, SandboxSessionError)
    assert result.code == "context_persist_failed"
    assert "no Sandbox tool access was granted" in result.message
    assert "destroy" not in [c["handler"] for c in scripted_client.calls]
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["sandbox_id"]
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
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["sandbox_id"]
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
    _seed_registry(fake_cache)
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
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["sandbox_id"]
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
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["sandbox_id"]
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
    it; the warm container is retired and a fresh one takes its place."""
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
    assert "destroy" in handlers, "the superseded container must not be left to leak"
    create_call = next(c for c in scripted_client.calls if c["handler"] == "create_from_builtin")
    assert create_call["env"] == {"API_KEY": "secret"}
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["sandbox_id"]
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

    fake_cache.leases["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1:lock"] = "other-token"
    clock = FakeClock()

    async def sleep_then_release(seconds: float) -> None:
        await clock.sleep(seconds)
        fake_cache.leases.pop("sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1:lock", None)
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

    fake_cache.leases["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1:lock"] = "other-token"
    clock = FakeClock()
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(
        owner=owner, bundle=_bundle(), binding=_binding(), sleep=clock.sleep, clock=clock
    )

    assert isinstance(result, SandboxSessionError)
    assert result.code == "busy_timeout"
    assert scripted_client.calls == []
    assert fake_cache.leases == {
        "sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1:lock": "other-token"
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

    fake_cache.leases["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1:lock"] = "other-token"
    clock = FakeClock()

    async def sleep_then_release(seconds: float) -> None:
        await clock.sleep(seconds)
        fake_cache.leases.pop("sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1:lock", None)
        _seed_registry(fake_cache)

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

    result = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding())

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
            }
        )
        if self.enqueue_error is not None:
            raise self.enqueue_error
        return _ticket("queued")

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
    return queue


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
    assert fake_queue.factory.opened == 1
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
    assert fake_cache.store["sandbox:owner:ent_1:user_1:conversation:U01ABC:agent:agent_1"]["sandbox_id"] == "sb_task"
    # Admission never touches the sandbox service directly on this path.
    # The task flow never sees the create response, so it asks the runner how
    # it injects skill env; the fake health reports no mode, hence "create".
    assert [call["handler"] for call in scripted_client.calls] == ["init", "health", "close"]
    assert session.env_injection == "create"
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

    monkeypatch.setattr(svc, "runtime_init_sandbox_context", failing_init)
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
    assert "sandbox:owner:ent_1:user_1:conversation:U01ABC:agent:agent_1" not in fake_cache.store
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

    unbound: list[str] = []

    async def fake_delete(conversation_id):
        unbound.append(conversation_id)

    destroyed: list[str] = []

    async def fake_destroy(sandbox_id, *, db, external):
        destroyed.append(sandbox_id)
        return "destroyed"

    monkeypatch.setattr(svc, "runtime_delete_sandbox_context", fake_delete)
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
    # The conversation is handed back unbound, so the next call is not denied
    # by a context pointing at a container this one no longer owns.
    assert unbound == ["U01ABC"]
    # The sweep already released the instance; the idle reaper owns the rest.
    assert destroyed == []
    assert "sandbox:owner:ent_1:user_1:conversation:U01ABC:agent:agent_1" not in fake_cache.store
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_durable_without_run_or_task_is_not_configured(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts, fake_queue, monkeypatch
) -> None:
    from packages.core.services.sandbox_session_service import (
        SandboxBinding,
        SandboxOwner,
        SandboxSessionError,
        acquire_sandbox_session,
    )

    _external_runner_settings(local_sandbox_settings, monkeypatch)
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    result = await acquire_sandbox_session(
        owner=owner,
        bundle=_bundle(),
        binding=SandboxBinding(conversation_id="conversation_1", agent_id="agent_1", skill_id="skill_xlsx"),
    )
    assert isinstance(result, SandboxSessionError)
    assert result.code == "not_configured"
    assert "requires runtime context" in result.message

    # A task id without an actor cannot own a reservation either.
    anonymous = SandboxOwner(entity_id="ent_1", user_id="")
    anonymous_result = await acquire_sandbox_session(
        owner=anonymous,
        bundle=_bundle(),
        binding=_task_binding(),
    )
    assert isinstance(anonymous_result, SandboxSessionError)
    assert anonymous_result.code == "not_configured"
    assert fake_queue.enqueued == []


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
    fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["base_url"] = (
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
    assert (other.sandbox_id, other.reused) == ("sb_cred_other", False)

    loads = [call for call in scripted_client.calls if call["handler"] == "load_skill"]
    assert [(call["skill_name"], call["env"], call["allowed_sensitive_keys"]) for call in loads] == [
        ("xlsx", {"XLSX_KEY": "secret"}, ["XLSX_KEY"]),
        ("docx", {"DOCX_KEY": "secret"}, ["DOCX_KEY"]),
        ("pptx", {}, []),
    ]
    assert "sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1" in fake_cache.store
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
    fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["env_injection"] = "create"
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

    key = "sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"
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
    assert fake_cache.store[key]["holds_credentials"] is False

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
    fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["env_injection"] = "exec"
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

    key = "sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"
    _seed_registry(fake_cache)
    fake_cache.store[key].update({"env_injection": "create", "current_skill": "xlsx", "holds_credentials": False})
    scripted_client.created_ids = ["sb_fresh"]
    session = await acquire_sandbox_session(
        owner=SandboxOwner(entity_id="ent_1", user_id="user_1"),
        bundle=SandboxBundle(skill_name="xlsx", files={"SKILL.md": "#"}, env={"K": "v"}),
        binding=_binding(conversation_id="conversation_1"),
    )
    assert (session.sandbox_id, session.reused) == ("sb_fresh", False)

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
async def test_plain_bundles_reuse_within_a_conversation_only(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """Warmth is per conversation: load_skill clears /skill on a skill change."""

    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    scripted_client.created_ids = ["sb_c1", "sb_c2"]
    scripted_client.load_results = [_load_result(sandbox_id="sb_c1")]
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
    assert (third.sandbox_id, third.reused) == ("sb_c2", False)

    handlers = [c["handler"] for c in scripted_client.calls]
    assert handlers.count("create_from_builtin") == 2
    assert handlers.count("load_skill") == 1

    assert "sandbox:owner:ent_1:user_1" not in fake_cache.store
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["sandbox_id"]
        == "sb_c1"
    )
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_2:agent:agent_1"]["sandbox_id"]
        == "sb_c2"
    )
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
        owner=owner, bundle=generic, binding=_binding(conversation_id="conversation_1")
    )

    assert (plain_session.sandbox_id, plain_session.reused) == ("sb_plain", False)
    # The generic sandbox never lands in the conversation's skill container.
    assert (generic_session.sandbox_id, generic_session.reused) == ("sb_generic", False)
    assert "load_skill" not in [c["handler"] for c in scripted_client.calls]

    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["sandbox_id"]
        == "sb_plain"
    )
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1:generic"]["sandbox_id"]
        == "sb_generic"
    )
    assert "sandbox:owner:ent_1:user_1:generic" not in fake_cache.store
    assert fake_cache.leases == {}


@pytest.mark.asyncio
async def test_agents_in_one_conversation_do_not_share_a_container(
    fake_cache, scripted_client, local_sandbox_settings, recorded_contexts
) -> None:
    """Warm reuse is gated on the driving agent, not just the conversation.

    Sharing would let either agent's ``load_skill`` wipe the other's
    ``/skill``, and rebinding the container to the second agent locks the
    first out of the sandbox it is still using.
    """

    from packages.core.services.sandbox_session_service import (
        SandboxOwner,
        acquire_sandbox_session,
    )

    scripted_client.created_ids = ["sb_agent_1", "sb_agent_2"]
    owner = SandboxOwner(entity_id="ent_1", user_id="user_1")

    first = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding(agent_id="agent_1"))
    second = await acquire_sandbox_session(owner=owner, bundle=_bundle(), binding=_binding(agent_id="agent_2"))

    assert (first.sandbox_id, first.reused) == ("sb_agent_1", False)
    assert (second.sandbox_id, second.reused) == ("sb_agent_2", False)
    assert len([c for c in scripted_client.calls if c["handler"] == "create_from_builtin"]) == 2
    assert "load_skill" not in [c["handler"] for c in scripted_client.calls]
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_1"]["sandbox_id"]
        == "sb_agent_1"
    )
    assert (
        fake_cache.store["sandbox:owner:ent_1:user_1:conversation:conversation_1:agent:agent_2"]["sandbox_id"]
        == "sb_agent_2"
    )
    assert fake_cache.leases == {}


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

    # A plain bundle whose reuse_scope carries ':' cannot forge another
    # conversation's scope either.
    plain_a = SandboxBundle(skill_name="x", files={}, reuse_scope="a:b")
    plain_b = SandboxBundle(skill_name="x", files={}, reuse_scope="b")
    _, plain_scope_a = sandbox_reuse_scope(plain_a, SandboxBinding(conversation_id="c"))
    _, plain_scope_b = sandbox_reuse_scope(plain_b, SandboxBinding(conversation_id="c:a"))
    assert plain_scope_a == "conversation:c:a%3Ab"
    assert plain_scope_b == "conversation:c%3Aa:b"

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

    generic = SandboxBundle(skill_name="generic-sandbox", files={}, reuse_scope="generic")
    assert sandbox_reuse_scope(generic, SandboxBinding(conversation_id="c1")) == (
        True,
        "conversation:c1:generic",
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
