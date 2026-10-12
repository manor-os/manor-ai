from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from tests.test_sandbox_session_service import fake_cache  # noqa: F401 - fixture


@pytest.mark.asyncio
async def test_sandbox_create_returns_retry_message_when_capacity_is_full(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    import packages.core.services.sandbox_sdk as sandbox_sdk
    from packages.core.config import get_settings
    from packages.core.services.sandbox_sdk.exceptions import SandboxCapacityError

    class FakeSandboxClient:
        def __init__(self, *, base_url: str, timeout: float, api_token: str | None = None):
            pass

        async def health(self):
            return {"status": "ok", "sandbox_image_available": True}

        async def create_from_files(self, **_kwargs):
            raise SandboxCapacityError("Max active sandbox limit reached (2)", status_code=429)

        async def close(self):
            return None

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", FakeSandboxClient)
    monkeypatch.setattr(get_settings(), "SANDBOX_SERVICE_URL", "http://sandbox-service")

    result = await sandbox_tools._sandbox_create(
        entity_id="entity-1",
        conversation_id="conversation-1",
    )

    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "sandbox_capacity_full"
    assert "Sandbox capacity is full" in payload["error"]["message"]
    assert "Please retry later" in payload["error"]["message"]
    assert "Max active sandbox limit reached" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_sandbox_create_without_skill_uses_blank_generic_manifest(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    import packages.core.services.sandbox_sdk as sandbox_sdk
    from packages.core.config import get_settings

    calls: dict[str, object] = {}

    class FakeSandboxClient:
        def __init__(self, *, base_url: str, timeout: float, api_token: str | None = None):
            pass

        async def health(self):
            return {"status": "ok", "sandbox_image_available": True}

        async def create_from_files(self, **kwargs):
            calls.update(kwargs)
            return SimpleNamespace(
                sandbox_id="sandbox_generic",
                status="running",
                workdir="/skill",
                env_blocked=[],
                skill=SimpleNamespace(
                    entry_hint=None,
                    scripts=[],
                    requirements_txt=None,
                ),
            )

        async def close(self):
            return None

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", FakeSandboxClient)
    monkeypatch.setattr(get_settings(), "SANDBOX_SERVICE_URL", "http://sandbox-service")

    result = await sandbox_tools._sandbox_create(entity_id="entity-1")

    assert calls == {
        "skill_name": "generic-sandbox",
        "files": {
            "README.md": ("# Generic sandbox\n\nTemporary isolated workspace for ad hoc command execution.\n"),
        },
        "env": {},
        "allowed_sensitive_keys": [],
        "auto_install": False,
        "config": None,
    }
    assert "sandbox_id: sandbox_generic" in result
    assert "sandbox_kind: generic" in result


@pytest.mark.asyncio
async def test_sandbox_create_records_conversation_owner(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    import packages.core.services.sandbox_sdk as sandbox_sdk
    from packages.core.config import get_settings
    from packages.core.services import sandbox_session_service

    recorded: dict[str, object] = {}

    class FakeSandboxClient:
        def __init__(self, *, base_url: str, timeout: float, api_token: str | None = None):
            pass

        async def health(self):
            return {"status": "ok", "sandbox_image_available": True}

        async def create_from_files(self, **_kwargs):
            return SimpleNamespace(
                sandbox_id="sandbox_owned",
                status="running",
                workdir="/skill",
                env_blocked=[],
                skill=SimpleNamespace(
                    entry_hint=None,
                    scripts=[],
                    requirements_txt=None,
                ),
            )

        async def close(self):
            return None

    async def record_context(conversation_id, sandbox_id, skill_id, **kwargs):
        recorded.update(
            conversation_id=conversation_id,
            sandbox_id=sandbox_id,
            skill_id=skill_id,
            **kwargs,
        )
        return recorded

    async def no_registry(_owner, *, scope=None):
        return None

    async def register(_owner, _session, *, scope=None):
        return True

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", FakeSandboxClient)
    monkeypatch.setattr(sandbox_session_service, "runtime_init_sandbox_context", record_context)
    monkeypatch.setattr(sandbox_session_service, "load_sandbox_session", no_registry)
    monkeypatch.setattr(sandbox_session_service, "register_sandbox_session", register)
    monkeypatch.setattr(get_settings(), "SANDBOX_SERVICE_URL", "http://sandbox-service")

    await sandbox_tools._sandbox_create(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        _agent_id_from_context="agent-1",
    )

    assert recorded == {
        "conversation_id": "conversation-1",
        "sandbox_id": "sandbox_owned",
        "skill_id": "",
        "entity_id": "entity-1",
        "user_id": "user-1",
        "agent_id": "agent-1",
    }


@pytest.mark.asyncio
async def test_sandbox_create_reuses_owner_sandbox_from_registry(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    import packages.core.services.sandbox_sdk as sandbox_sdk
    from packages.core.config import get_settings
    from packages.core.services import sandbox_session_service
    from packages.core.services.sandbox_session_service import SandboxSession

    class FakeSandboxClient:
        def __init__(self, *, base_url: str, timeout: float, api_token: str | None = None):
            pass

        async def status(self, sandbox_id):
            assert sandbox_id == "sandbox-existing"
            return SimpleNamespace(status="ready", workdir="/skill")

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
            assert sandbox_id == "sandbox-existing"
            assert skill_name == "generic-sandbox"
            return SimpleNamespace(
                sandbox_id="sandbox-existing",
                reused=True,
                skill=SimpleNamespace(scripts=[], requirements_txt=None, entry_hint=None),
            )

        async def create_from_files(self, **_kwargs):
            raise AssertionError("an owned sandbox must not be replaced")

        async def close(self):
            return None

    async def load_registry(owner, *, scope=None):
        assert owner.entity_id == "entity-1"
        assert owner.user_id == "user-1"
        assert scope == "conversation:conversation-2:agent:agent-1:generic"
        return SandboxSession(
            sandbox_id="sandbox-existing",
            base_url="http://sandbox-service",
            created_at=1.0,
            last_acquired_at=1.0,
            loaded_skills=[],
        )

    async def register(_owner, _session, *, scope=None):
        return True

    async def bind(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", FakeSandboxClient)
    monkeypatch.setattr(sandbox_session_service, "load_sandbox_session", load_registry)
    monkeypatch.setattr(sandbox_session_service, "register_sandbox_session", register)
    monkeypatch.setattr(sandbox_session_service, "runtime_init_sandbox_context", bind)
    monkeypatch.setattr(get_settings(), "SANDBOX_SERVICE_URL", "http://sandbox-service")

    result = await sandbox_tools._sandbox_create(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-2",
        _agent_id_from_context="agent-1",
    )

    assert "sandbox_id: sandbox-existing" in result
    assert "reused: true" in result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generic_sandbox_create_queues_and_attaches_like_a_skill(
    db_session, fake_cache, monkeypatch,
) -> None:
    """External runners admit the generic sandbox through the same reservation
    queue as a Skill: the durable run parks on the ticket, and re-executing the
    same call after allocation attaches the container. It used to be refused
    outright, so staging models got sandbox_generic_unavailable every time."""

    import uuid
    from contextlib import asynccontextmanager

    from sqlalchemy import select

    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.config import get_settings
    from packages.core.models.runtime_run import (
        RuntimeRun,
        RuntimeRunStatus,
        SandboxReservation,
        SandboxReservationStatus,
        SandboxRunner,
        SandboxRunnerStatus,
    )
    from packages.core.services import sandbox_session_service
    from packages.core.services.sandbox_queue_service import (
        claim_next_sandbox_allocation,
        complete_sandbox_allocation,
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", "external-runner")
    monkeypatch.setattr(settings, "MANOR_RUNTIME_EXECUTION_MODE", "durable")

    @asynccontextmanager
    async def test_db():
        yield db_session

    async def exec_injection(_base_url):
        return "exec"

    monkeypatch.setattr(sandbox_tools, "_sandbox_admission_db", test_db)
    monkeypatch.setattr(sandbox_session_service, "_runner_env_injection", exec_injection)

    suffix = uuid.uuid4().hex.upper()[:20]
    run_id = f"01GR{suffix}00"[:26]
    conversation_id = f"01GC{suffix}00"[:26]
    entity_id, user_id = f"ent-{suffix}", f"user-{suffix}"
    run = RuntimeRun(
        id=run_id, root_run_id=run_id, conversation_id=conversation_id,
        entity_id=entity_id, user_id=user_id, status=RuntimeRunStatus.RUNNING.value,
    )
    runner = SandboxRunner(
        id=f"runner-{suffix}", base_url="http://runner-generic:8000",
        status=SandboxRunnerStatus.HEALTHY.value, active_limit=50, executing_limit=50,
    )
    db_session.add_all([run, runner])
    await db_session.commit()
    call = {
        "entity_id": entity_id, "user_id": user_id, "conversation_id": conversation_id,
        "_runtime_run_id_from_context": run_id, "_runtime_tool_call_id_from_context": "call-generic",
    }

    first = await sandbox_tools._sandbox_create(**call)

    assert isinstance(first, RuntimeToolSuspension) and first.kind == "waiting_resource"
    reservation = (await db_session.execute(
        select(SandboxReservation).where(SandboxReservation.runtime_run_id == run_id)
    )).scalar_one()
    assert reservation.tool_call_id == "call-generic"
    assert reservation.request_payload["skill_name"] == "generic-sandbox"

    # The persisted suspension activates the ticket; re-executing before the
    # runner allocates keeps waiting on the same ticket instead of queueing again.
    reservation.status = SandboxReservationStatus.QUEUED.value
    await db_session.commit()
    waiting = await sandbox_tools._sandbox_create(**call)
    assert isinstance(waiting, RuntimeToolSuspension) and waiting.reservation_id == reservation.id

    claim = await claim_next_sandbox_allocation(db_session)
    assert claim is not None and claim[0].id == reservation.id
    await db_session.commit()
    await db_session.refresh(reservation)
    await complete_sandbox_allocation(
        db_session, reservation_id=reservation.id, sandbox_id=f"sbx-{suffix}"[:24],
        runner_id=claim[1].id, expected_reservation_version=reservation.version,
    )
    await db_session.commit()

    attached = await sandbox_tools._sandbox_create(**call)

    assert f"sandbox_id: sbx-{suffix}"[:36] in attached and "sandbox_kind: generic" in attached
    await db_session.refresh(reservation)
    assert reservation.status == SandboxReservationStatus.CONSUMED.value
    ctx = fake_cache.store[f"sandbox:ctx:{conversation_id}"]
    assert ctx["sandbox_id"] == f"sbx-{suffix}"[:24] and ctx["skill_id"] == ""

    # This test commits; leave no queued or active rows for the next one.
    from sqlalchemy import delete

    from packages.core.models.runtime_run import SandboxInstance

    await db_session.execute(delete(SandboxInstance).where(SandboxInstance.runtime_run_id == run_id))
    await db_session.execute(delete(SandboxReservation).where(SandboxReservation.runtime_run_id == run_id))
    await db_session.execute(delete(RuntimeRun).where(RuntimeRun.id == run_id))
    await db_session.execute(delete(SandboxRunner).where(SandboxRunner.id == runner.id))
    await db_session.commit()


def test_sandbox_tool_is_available_for_durable_external_runners(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "SANDBOX_SERVICE_URL", "")
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", "external-runner")
    monkeypatch.setattr(settings, "SANDBOX_RUNNERS_JSON", '[{"base_url":"http://runner"}]')

    assert sandbox_tools._sandbox_available() is True


@pytest.mark.asyncio
async def test_sandbox_create_destroys_unadmitted_instance_when_context_save_fails(
    monkeypatch,
) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    import packages.core.services.sandbox_sdk as sandbox_sdk
    from packages.core.config import get_settings
    from packages.core.services import sandbox_session_service

    destroyed: list[str] = []

    class FakeSandboxClient:
        def __init__(self, *, base_url: str, timeout: float, api_token: str | None = None):
            pass

        async def health(self):
            return {"status": "ok", "sandbox_image_available": True}

        async def create_from_files(self, **_kwargs):
            return SimpleNamespace(
                sandbox_id="sandbox_unadmitted",
                status="running",
                workdir="/skill",
                env_blocked=[],
                skill=SimpleNamespace(
                    entry_hint=None,
                    scripts=[],
                    requirements_txt=None,
                ),
            )

        async def destroy(self, sandbox_id):
            destroyed.append(sandbox_id)

        async def close(self):
            return None

    async def fail_context(*_args, **_kwargs):
        raise RuntimeError("Sandbox owner context could not be persisted.")

    async def no_registry(_owner, *, scope=None):
        return None

    async def register(_owner, _session, *, scope=None):
        return True

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", FakeSandboxClient)
    monkeypatch.setattr(sandbox_session_service, "runtime_init_sandbox_context", fail_context)
    monkeypatch.setattr(sandbox_session_service, "load_sandbox_session", no_registry)
    monkeypatch.setattr(sandbox_session_service, "register_sandbox_session", register)
    monkeypatch.setattr(get_settings(), "SANDBOX_SERVICE_URL", "http://sandbox-service")

    result = await sandbox_tools._sandbox_create(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        _agent_id_from_context="agent-1",
    )

    assert json.loads(result)["error"]["code"] == "sandbox_creation_failed"
    assert destroyed == ["sandbox_unadmitted"]


@pytest.mark.asyncio
async def test_sandbox_access_rejects_agent_switch(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    async def load_context(_conversation_id):
        return {
            "sandbox_id": "sandbox-owned",
            "entity_id": "entity-1",
            "user_id": "user-1",
            "agent_id": "agent-1",
        }

    monkeypatch.setattr(sandbox_tools, "_load_ctx", load_context)

    result = await sandbox_tools._sandbox_instance_access_error(
        sandbox_id="sandbox-owned",
        entity_id="entity-1",
        user_id="user-1",
        agent_id="agent-2",
        conversation_id="conversation-1",
    )

    assert json.loads(result)["error"]["code"] == "sandbox_access_denied"


@pytest.mark.asyncio
async def test_sandbox_destroy_releases_binding_and_keeps_container(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    from packages.core.services import sandbox_session_service

    released: list[dict] = []

    async def allow_access(**_kwargs):
        return None

    async def keep_warm(**kwargs):
        released.append(kwargs)
        return "kept"

    async def forbid_client(_sandbox_id):
        raise AssertionError("release must not contact the sandbox service")

    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", allow_access)
    monkeypatch.setattr(sandbox_session_service, "release_sandbox_session", keep_warm)
    monkeypatch.setattr(sandbox_tools, "release_sandbox_session", keep_warm)
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", forbid_client)

    result = await sandbox_tools._sandbox_destroy(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        sandbox_id="sandbox-owned",
        _agent_id_from_context="agent-1",
    )

    assert "released for reuse" in result
    assert "stays warm" in result
    assert released and released[0]["sandbox_id"] == "sandbox-owned"
    assert released[0]["conversation_id"] == "conversation-1"


@pytest.mark.asyncio
async def test_sandbox_destroy_reports_destroyed_when_not_reusable(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    from packages.core.services import sandbox_session_service

    async def allow_access(**_kwargs):
        return None

    async def destroyed(**_kwargs):
        return "destroyed"

    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", allow_access)
    monkeypatch.setattr(sandbox_session_service, "release_sandbox_session", destroyed)
    monkeypatch.setattr(sandbox_tools, "release_sandbox_session", destroyed)

    result = await sandbox_tools._sandbox_destroy(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        sandbox_id="sandbox-owned",
        _agent_id_from_context="agent-1",
    )

    assert "released and destroyed" in result


@pytest.mark.asyncio
async def test_sandbox_exec_rejects_foreign_conversation_sandbox(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    async def load_foreign_context(_conversation_id):
        return {
            "sandbox_id": "sandbox-other",
            "entity_id": "entity-1",
            "user_id": "user-1",
        }

    monkeypatch.setattr(sandbox_tools, "_load_ctx", load_foreign_context)
    monkeypatch.setattr(
        sandbox_tools,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("foreign sandbox must not be opened")),
    )

    result = await sandbox_tools._sandbox_exec(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        sandbox_id="sandbox-requested",
        command="pwd",
    )

    assert json.loads(result)["error"]["code"] == "sandbox_access_denied"


@pytest.mark.asyncio
async def test_sandbox_respond_rejects_foreign_conversation_sandbox(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    async def load_foreign_context(_conversation_id):
        return {
            "sandbox_id": "sandbox-other",
            "entity_id": "entity-1",
            "user_id": "user-1",
        }

    monkeypatch.setattr(sandbox_tools, "_load_ctx", load_foreign_context)
    monkeypatch.setattr(
        sandbox_tools,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("foreign sandbox response must not be delivered")),
    )

    result = await sandbox_tools._sandbox_respond(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        sandbox_id="sandbox-requested",
        execution_id="execution-1",
        event_id="input-1",
        payload={"choice": "b"},
    )

    assert json.loads(result)["error"]["code"] == "sandbox_access_denied"


@pytest.mark.asyncio
async def test_sandbox_agent_can_start_poll_and_cancel_background_command(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    calls: list[tuple[str, str]] = []

    class FakeSandboxClient:
        async def start_exec(self, sandbox_id, command, timeout, execution_id):
            calls.append(("start", execution_id or ""))
            return SimpleNamespace(
                sandbox_id=sandbox_id,
                execution_id=execution_id or "execution-1",
                status="queued",
                created_at=10.0,
                started_at=None,
                finished_at=None,
                stdout=None,
                stderr=None,
                exit_code=None,
                error=None,
                terminal=False,
            )

        async def execution_status(self, sandbox_id, execution_id, after_sequence=0):
            calls.append(("status", execution_id))
            return SimpleNamespace(
                sandbox_id=sandbox_id,
                execution_id=execution_id,
                status="running",
                created_at=10.0,
                started_at=11.0,
                finished_at=None,
                stdout=None,
                stderr=None,
                exit_code=None,
                error=None,
                terminal=False,
                events=[
                    SimpleNamespace(
                        sequence=1,
                        event_id="input-1",
                        type="need_input",
                        message="Choose",
                        payload={"choices": ["a", "b"]},
                        requires_response=True,
                        responded=False,
                        created_at=11.0,
                    )
                ]
                if after_sequence == 0
                else [],
                next_sequence=1,
                waiting_for_response=True,
            )

        async def send_execution_response(
            self,
            sandbox_id,
            execution_id,
            event_id,
            *,
            payload,
            message,
        ):
            calls.append(("respond", event_id))
            assert payload == {"choice": "b"}
            assert message == "Use b"
            return SimpleNamespace(
                sandbox_id=sandbox_id,
                execution_id=execution_id,
                event_id=event_id,
                accepted=True,
                duplicate=False,
            )

        async def cancel_execution(self, sandbox_id, execution_id):
            calls.append(("cancel", execution_id))
            return SimpleNamespace(
                sandbox_id=sandbox_id,
                execution_id=execution_id,
                cancelled=True,
            )

        async def close(self):
            return None

    async def allow_access(**_kwargs):
        return None

    async def get_client(_sandbox_id):
        return FakeSandboxClient()

    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", allow_access)
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", get_client)

    started = json.loads(
        await sandbox_tools._sandbox_exec(
            sandbox_id="sandbox-1",
            command="sleep 60",
            background=True,
        )
    )
    execution_id = started["execution_id"]
    polled = json.loads(
        await sandbox_tools._sandbox_status(
            sandbox_id="sandbox-1",
            execution_id=execution_id,
        )
    )
    responded = json.loads(
        await sandbox_tools._sandbox_respond(
            sandbox_id="sandbox-1",
            execution_id=execution_id,
            event_id="input-1",
            payload={"choice": "b"},
            message="Use b",
        )
    )
    cancelled = json.loads(
        await sandbox_tools._sandbox_cancel(
            sandbox_id="sandbox-1",
            execution_id=execution_id,
        )
    )

    assert started["status"] == "queued"
    assert polled["status"] == "running"
    assert polled["events"][0]["type"] == "need_input"
    assert polled["waiting_for_response"] is True
    assert responded["accepted"] is True
    assert cancelled["cancelled"] is True
    assert calls == [
        ("start", ""),
        ("status", "execution-1"),
        ("status", "execution-1"),
        ("respond", "input-1"),
        ("cancel", "execution-1"),
    ]


@pytest.mark.asyncio
async def test_sandbox_respond_issues_actor_scoped_credential_reference(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    import packages.core.database as database_module
    import packages.core.services.agent_permission_service as permission_module

    permission_call: dict[str, object] = {}

    class FakeSessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class FakeClient:
        async def execution_status(self, sandbox_id, execution_id):
            assert (sandbox_id, execution_id) == ("sandbox-1", "execution-1")
            return SimpleNamespace(
                events=[
                    SimpleNamespace(
                        event_id="credential-1",
                        type="need_credential",
                        payload={"provider": "google_drive"},
                    )
                ]
            )

    async def allow_integration(_db, **kwargs):
        permission_call.update(kwargs)
        return SimpleNamespace(
            allowed=True,
            reason="allowed",
            account_id="account-owned-1",
        )

    monkeypatch.setattr(database_module, "async_session", FakeSessionContext)
    monkeypatch.setattr(permission_module, "can_use_integration", allow_integration)
    monkeypatch.setattr(
        sandbox_tools,
        "_get_sandbox_api_token",
        lambda: "test-signing-key",
    )

    payload, message = await sandbox_tools._prepare_sandbox_response(
        client=FakeClient(),
        sandbox_id="sandbox-1",
        execution_id="execution-1",
        event_id="credential-1",
        entity_id="entity-1",
        user_id="user-1",
        payload={"integration_account_id": "account-requested-1"},
        message="",
    )

    assert permission_call == {
        "user_id": "user-1",
        "entity_id": "entity-1",
        "provider": "google_drive",
        "integration_account_id": "account-requested-1",
        "allow_env_fallback": False,
    }
    assert payload["provider"] == "google_drive"
    assert payload["integration_account_id"] == "account-owned-1"
    assert payload["credential_ref"].startswith("sbxcred.v1.")
    assert message == ""


@pytest.mark.asyncio
async def test_sandbox_create_routes_stored_skills_through_invoke_skill(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    from packages.core.ai.runtime.streams import (
        runtime_tool_call_error,
        runtime_tool_status_for_chat,
    )

    result = await sandbox_tools._sandbox_create(
        entity_id="entity-1",
        skill_id="missing-skill",
    )

    assert json.loads(result) == {
        "ok": False,
        "error": {
            "code": "sandbox_skill_requires_invoke",
            "message": (
                "Stored Skills must be opened with invoke_skill so Skill permissions "
                "and bindings are enforced. Omit skill_id to create a generic sandbox."
            ),
        },
    }
    assert runtime_tool_status_for_chat(result) == "error"
    assert runtime_tool_call_error(result) == json.dumps(
        {
            "code": "sandbox_skill_requires_invoke",
            "message": (
                "Stored Skills must be opened with invoke_skill so Skill permissions "
                "and bindings are enforced. Omit skill_id to create a generic sandbox."
            ),
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_authenticated_sandbox_create_requires_conversation_owner() -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    result = await sandbox_tools._sandbox_create(
        entity_id="entity-1",
        user_id="user-1",
    )

    assert json.loads(result) == {
        "ok": False,
        "error": {
            "code": "sandbox_access_denied",
            "message": ("Authenticated sandbox creation requires a conversation owner context."),
        },
    }


@pytest.mark.asyncio
async def test_sandbox_create_rejects_foreign_generic_context(monkeypatch) -> None:
    """A bound generic sandbox is still owner-checked, not silently taken over."""

    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    async def load_foreign_generic_context(_conversation_id):
        return {
            "sandbox_id": "sb-x",
            "skill_id": "",
            "entity_id": "entity-1",
            "user_id": "other-user",
            "agent_id": "agent-1",
        }

    monkeypatch.setattr(sandbox_tools, "_load_ctx", load_foreign_generic_context)

    result = await sandbox_tools._sandbox_create(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        _agent_id_from_context="agent-1",
    )

    assert json.loads(result)["error"]["code"] == "sandbox_access_denied"


@pytest.mark.asyncio
async def test_sandbox_create_requires_entity_context() -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    result = await sandbox_tools._sandbox_create(conversation_id="conversation-1")

    assert json.loads(result)["error"]["code"] == "sandbox_access_denied"
    assert "entity context" in json.loads(result)["error"]["message"]


@pytest.mark.asyncio
async def test_sandbox_action_validation_errors_share_one_contract() -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    from packages.core.ai.runtime.streams import runtime_tool_status_for_chat

    results = [
        await sandbox_tools._sandbox_exec(),
        await sandbox_tools._sandbox_read_file(),
        await sandbox_tools._sandbox_write_file(),
        await sandbox_tools._sandbox_save_result(),
        await sandbox_tools._sandbox_destroy(),
        await sandbox_tools._sandbox_handler(action="unknown"),
    ]

    for result in results:
        payload = json.loads(result)
        assert payload["ok"] is False
        assert set(payload["error"]) == {"code", "message"}
        assert payload["error"]["code"]
        assert payload["error"]["message"]
        assert runtime_tool_status_for_chat(result) == "error"


@pytest.mark.asyncio
async def test_sandbox_create_does_not_bind_entity_fs_by_default(
    monkeypatch,
    tmp_path,
) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    import packages.core.services.sandbox_sdk as sandbox_sdk
    from packages.core.config import get_settings

    calls: dict[str, object] = {}

    class FakeSandboxClient:
        def __init__(self, *, base_url: str, timeout: float, api_token: str | None = None):
            pass

        async def health(self):
            return {"status": "ok", "sandbox_image_available": True}

        async def create_from_files(self, **kwargs):
            calls.update(kwargs)
            return SimpleNamespace(
                sandbox_id="sandbox_1",
                container_name="skill-sbx-sandbox_1",
                status="running",
                workdir="/skill",
                env_blocked=[],
                skill=SimpleNamespace(
                    entry_hint=None,
                    scripts=[],
                    requirements_txt=None,
                ),
            )

        async def close(self):
            return None

    entity_root = tmp_path / "entity_1"
    entity_root.mkdir()
    monkeypatch.setenv("MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setenv("MANOR_FS_ENABLED", "true")
    monkeypatch.setattr(sandbox_sdk, "SandboxClient", FakeSandboxClient)
    monkeypatch.setattr(get_settings(), "SANDBOX_SERVICE_URL", "http://sandbox-service")

    result = await sandbox_tools._sandbox_create(
        entity_id="entity_1",
    )

    assert calls["config"] is None
    assert "workspace_mount: (none" in result
    assert "read-only entity filesystem" not in result


@pytest.mark.asyncio
async def test_sandbox_write_file_injects_entity_fs_bytes_with_base64(
    monkeypatch,
    tmp_path,
) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    from packages.core.config import get_settings

    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    entity_dir = tmp_path / "entity_1" / "uploads" / "chat"
    entity_dir.mkdir(parents=True)
    (entity_dir / "image.bin").write_bytes(b"\x89PNG\r\n\x1a\n")
    calls: list[dict[str, object]] = []

    class FakeSandboxClient:
        async def write_file_base64(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(path="/skill/input.png", written=True)

        async def close(self):
            return None

    monkeypatch.setattr(sandbox_tools, "_get_client", lambda: FakeSandboxClient())

    async def allow_file_read(**_kwargs):
        return None

    monkeypatch.setattr(
        sandbox_tools,
        "runtime_guard_file_read_access",
        allow_file_read,
    )
    try:
        result = await sandbox_tools._sandbox_write_file(
            entity_id="entity_1",
            sandbox_id="sandbox_1",
            path="/skill/input.png",
            workspace_path="/workspace/uploads/chat/image.bin",
        )
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root

    assert "Written to /skill/input.png" in result
    assert calls == [
        {
            "sandbox_id": "sandbox_1",
            "path": "/skill/input.png",
            "content_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii"),
            "mkdir": True,
        }
    ]


@pytest.mark.asyncio
async def test_sandbox_workspace_read_stops_before_bytes_when_acl_denies(
    monkeypatch,
    tmp_path,
) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    entity_root = tmp_path / "entity_1"
    entity_root.mkdir()
    guarded: list[dict[str, object]] = []

    class FakeReadLock:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_exc):
            return None

    async def deny_file_read(**kwargs):
        guarded.append(kwargs)
        return '{"error":"entity_file_acl_denied"}'

    monkeypatch.setattr(
        sandbox_tools,
        "runtime_entity_file_root",
        lambda _entity_id: str(entity_root),
    )
    monkeypatch.setattr(
        sandbox_tools,
        "runtime_entity_filesystem_read_lock",
        lambda _entity_root: FakeReadLock(),
    )
    monkeypatch.setattr(
        sandbox_tools,
        "runtime_guard_file_read_access",
        deny_file_read,
    )
    monkeypatch.setattr(
        sandbox_tools,
        "runtime_open_entity_file_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("denied files must not be opened")),
    )

    content, blocked = await sandbox_tools._read_workspace_bytes(
        "entity_1",
        "/workspace/uploads/chat/private.bin",
        conversation_id="conversation_1",
        user_id="user_1",
        workspace_id="workspace_1",
        runtime_envelope=None,
    )

    assert content is None
    assert blocked == '{"error":"entity_file_acl_denied"}'
    assert guarded == [
        {
            "entity_id": "entity_1",
            "user_id": "user_1",
            "conversation_id": "conversation_1",
            "workspace_id": "workspace_1",
            "runtime_envelope": None,
            "tool_name": "sandbox",
            "paths": ["uploads/chat/private.bin"],
        }
    ]


@pytest.mark.asyncio
async def test_sandbox_destroy_twice_is_benign(monkeypatch) -> None:
    """A second release finds no binding at all; that is done, not denied."""

    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    async def no_binding(_conversation_id):
        return None

    async def access_denied(**_kwargs):
        return '{"error":{"code":"sandbox_access_denied"}}'

    async def unexpected_release(**_kwargs):
        raise AssertionError("an already-released sandbox must not be released again")

    monkeypatch.setattr(sandbox_tools, "_load_ctx", no_binding)
    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", access_denied)
    monkeypatch.setattr(sandbox_tools, "release_sandbox_session", unexpected_release)

    result = await sandbox_tools._sandbox_destroy(
        entity_id="entity-1",
        user_id="user-1",
        sandbox_id="sandbox-gone",
        conversation_id="conversation-1",
    )

    assert "already released" in result
    assert "denied" not in result


@pytest.mark.asyncio
async def test_sandbox_client_falls_back_to_the_warm_registry_route(monkeypatch):
    """A warm container must stay reachable when its instance row is gone."""
    from types import SimpleNamespace

    from packages.core.ai.tools import sandbox_tools
    from packages.core.services import sandbox_queue_service, sandbox_session_service

    monkeypatch.setattr(
        "packages.core.config.get_settings",
        lambda: SimpleNamespace(SANDBOX_COORDINATION_MODE="external-runner", SANDBOX_API_TOKEN="t"),
    )

    class _Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr("packages.core.database.async_session", lambda: _Db())

    async def no_route(db, sandbox_id):
        raise sandbox_queue_service.SandboxRouteNotFoundError(f"no active runner route for sandbox {sandbox_id}")

    async def warm_url(sandbox_id):
        return "http://runner-warm" if sandbox_id == "sb_warm" else None

    monkeypatch.setattr(sandbox_queue_service, "resolve_sandbox_runner", no_route)
    monkeypatch.setattr(sandbox_session_service, "warm_sandbox_base_url", warm_url)

    client = await sandbox_tools._get_client_for_sandbox("sb_warm")
    assert str(getattr(client, "base_url", "")).rstrip("/") == "http://runner-warm" or "runner-warm" in repr(
        vars(client)
    )

    with pytest.raises(sandbox_queue_service.SandboxRouteNotFoundError):
        await sandbox_tools._get_client_for_sandbox("sb_unknown")
