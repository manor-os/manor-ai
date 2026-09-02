from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_sandbox_create_returns_retry_message_when_capacity_is_full(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    class CapacityError(Exception):
        status_code = 429

    class FakeSandboxClient:
        async def create_from_files(self, **_kwargs):
            raise CapacityError("Max active sandbox limit reached (2)")

        async def close(self):
            return None

    monkeypatch.setattr(sandbox_tools, "_get_client", lambda: FakeSandboxClient())

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

    calls: dict[str, object] = {}

    class FakeSandboxClient:
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

    monkeypatch.setattr(sandbox_tools, "_get_client", lambda: FakeSandboxClient())

    result = await sandbox_tools._sandbox_create(entity_id="entity-1")

    assert calls == {
        "skill_name": "generic-sandbox",
        "files": {
            "README.md": (
                "# Generic sandbox\n\n"
                "Temporary isolated workspace for ad hoc command execution.\n"
            ),
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

    recorded: dict[str, object] = {}

    class FakeSandboxClient:
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

    monkeypatch.setattr(sandbox_tools, "_get_client", lambda: FakeSandboxClient())
    monkeypatch.setattr(sandbox_tools, "_init_ctx", record_context)

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
async def test_sandbox_create_reuses_owned_generic_conversation_instance(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    class FakeSandboxClient:
        async def status(self, sandbox_id):
            assert sandbox_id == "sandbox-existing"
            return SimpleNamespace(status="ready", workdir="/skill")

        async def close(self):
            return None

    async def load_context(_conversation_id):
        return {
            "sandbox_id": "sandbox-existing",
            "skill_id": "",
            "entity_id": "entity-1",
            "user_id": "user-1",
            "agent_id": "agent-1",
        }

    async def get_client(_sandbox_id):
        return FakeSandboxClient()

    monkeypatch.setattr(sandbox_tools, "_load_ctx", load_context)
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", get_client)
    monkeypatch.setattr(
        sandbox_tools,
        "_get_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("an owned conversation sandbox must not be replaced")
        ),
    )

    result = await sandbox_tools._sandbox_create(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        _agent_id_from_context="agent-1",
    )

    assert "sandbox_id: sandbox-existing" in result
    assert "reused: true" in result


@pytest.mark.asyncio
async def test_generic_sandbox_create_rejects_external_runner_bypass(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    from packages.core.config import get_settings

    monkeypatch.setattr(
        get_settings(),
        "SANDBOX_COORDINATION_MODE",
        "external-runner",
    )

    result = await sandbox_tools._sandbox_create(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
    )

    assert json.loads(result)["error"]["code"] == "sandbox_generic_unavailable"


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

    destroyed: list[str] = []

    class FakeSandboxClient:
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

    monkeypatch.setattr(sandbox_tools, "_get_client", lambda: FakeSandboxClient())
    monkeypatch.setattr(sandbox_tools, "_init_ctx", fail_context)

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
async def test_sandbox_destroy_keeps_context_after_retryable_failure(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    deleted: list[str] = []

    class FakeSandboxClient:
        async def destroy(self, **_kwargs):
            raise RuntimeError("runner temporarily unavailable")

        async def close(self):
            return None

    async def allow_access(**_kwargs):
        return None

    async def record_delete(conversation_id):
        deleted.append(conversation_id)

    async def get_client(_sandbox_id):
        return FakeSandboxClient()

    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", allow_access)
    monkeypatch.setattr(sandbox_tools, "_delete_ctx", record_delete)
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", get_client)

    result = await sandbox_tools._sandbox_destroy(
        entity_id="entity-1",
        user_id="user-1",
        conversation_id="conversation-1",
        sandbox_id="sandbox-owned",
        _agent_id_from_context="agent-1",
    )

    assert json.loads(result)["error"]["code"] == "sandbox_destroy_failed"
    assert deleted == []


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
        lambda: (_ for _ in ()).throw(
            AssertionError("foreign sandbox response must not be delivered")
        ),
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
                ] if after_sequence == 0 else [],
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
            "message": (
                "Authenticated sandbox creation requires a conversation owner context."
            ),
        },
    }


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

    calls: dict[str, object] = {}

    class FakeSandboxClient:
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
    monkeypatch.setattr(sandbox_tools, "_get_client", lambda: FakeSandboxClient())

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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("denied files must not be opened")
        ),
    )

    content, blocked = await sandbox_tools._read_workspace_bytes(
        "entity_1",
        "/workspace/uploads/chat/private.bin",
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
            "workspace_id": "workspace_1",
            "runtime_envelope": None,
            "tool_name": "sandbox",
            "paths": ["uploads/chat/private.bin"],
        }
    ]
