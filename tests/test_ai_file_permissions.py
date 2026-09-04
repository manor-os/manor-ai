import os
import json
import asyncio
import hashlib
import inspect
from types import SimpleNamespace

import pytest

from packages.core.ai.tools.bash_tool import (
    BASH_SCHEMA,
    _bash_mutation_action,
    _bash,
    _expanded_read_paths,
    _handle_mv_cp,
    _may_create_files,
    _split_simple_shell_commands,
    _validate_command,
    _visible_read_paths,
    _visible_mutation_paths,
)
from packages.core.ai.tools.document_tools import GENERATE_DOCUMENT_FILE_SCHEMA
from packages.core.ai.tools.file_tools import DELETE_FILE_SCHEMA, EDIT_FILE_SCHEMA, WRITE_FILE_SCHEMA
from packages.core.ai.tools.generate_file_tool import GENERATE_FILE_SCHEMA
from packages.core.ai.tools.manor_tool import MANOR_SCHEMA
from packages.core.ai.tools.sandbox_tools import _SANDBOX_SAVE_RESULT_SCHEMA
from packages.core.ai.tools.sandbox_file_tools import SAVE_SANDBOX_FILE_SCHEMA
from packages.core.ai.runtime.file_contracts import (
    FileApprovalOperationFactory,
    FileMutationAction,
)
from packages.core.services.ai_file_permissions import (
    _hitl_payload,
    _approval_prompt,
    _resource_file_acl_denial,
    classify_file_approval_reply,
    canonical_visible_user_paths,
    guard_ai_file_read_access,
    guard_ai_file_mutation,
    guard_ai_file_resource_access,
    normalize_file_permission_mode,
    visible_user_paths,
)


def _props(schema: dict) -> dict:
    return schema["function"]["parameters"]["properties"]


def test_file_permission_mode_aliases():
    assert normalize_file_permission_mode(None) == "approval"
    assert normalize_file_permission_mode("always approval") == "always_approve"
    assert normalize_file_permission_mode("always-approve") == "always_approve"
    assert normalize_file_permission_mode({"mode": "deny"}) == "deny"
    assert normalize_file_permission_mode("ask_each_time") == "approval"


def test_file_approval_operation_factory_binds_exact_payload():
    first = FileApprovalOperationFactory.create(
        tool_name="write_file",
        action=FileMutationAction.WRITE,
        paths=["docs/report.md"],
        payload={"content": "approved", "options": {"format": "md"}},
    )
    reordered = FileApprovalOperationFactory.create(
        tool_name="write_file",
        action="write",
        paths=["docs/report.md"],
        payload={"options": {"format": "md"}, "content": "approved"},
    )
    changed = FileApprovalOperationFactory.create(
        tool_name="write_file",
        action="write",
        paths=["docs/report.md"],
        payload={"content": "different", "options": {"format": "md"}},
    )

    assert first.payload_fingerprint == reordered.payload_fingerprint
    assert first.payload_fingerprint != changed.payload_fingerprint
    assert first.matches(first.to_record())
    assert not changed.matches(first.to_record())


def test_file_mutation_action_rejects_unknown_values():
    with pytest.raises(ValueError, match="Unsupported file mutation action"):
        FileMutationAction.normalize("publish_everything")


@pytest.mark.asyncio
async def test_file_permission_guard_fails_closed_for_unknown_action():
    denied = json.loads(await guard_ai_file_mutation(
        entity_id="ent_1",
        user_id="user_1",
        conversation_id="conv_1",
        tool_name="unknown_tool",
        action="publish_everything",
        paths=["docs/report.md"],
    ))

    assert denied["mode"] == "invalid_file_action"
    assert denied["operation"]["action"] == "publish_everything"


@pytest.mark.asyncio
async def test_file_read_guard_uses_agent_knowledge_acl(monkeypatch, tmp_path):
    from packages.core import database
    from packages.core.config import get_settings
    from packages.core.services import document_access

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    source = tmp_path / "ent_1" / "docs" / "private.md"
    source.parent.mkdir(parents=True)
    source.write_text("private", encoding="utf-8")
    calls = []

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    async def unreadable(_db, **kwargs):
        calls.append(kwargs)
        return {"docs/private.md"}

    monkeypatch.setattr(database, "async_session", SessionContext)
    monkeypatch.setattr(document_access, "unreadable_document_paths", unreadable)

    denied = json.loads(await guard_ai_file_read_access(
        entity_id="ent_1",
        user_id="user_1",
        workspace_id="ws_1",
        tool_name="manor",
        paths=["docs/private.md"],
    ))

    assert denied["mode"] == "entity_file_acl_denied"
    assert denied["operation"]["action"] == "read"
    assert calls == [{
        "entity_id": "ent_1",
        "rel_paths": ["docs/private.md"],
        "user_id": "user_1",
        "workspace_id": "ws_1",
        "actor_type": "agent",
    }]


def test_file_approval_state_transitions_lock_conversation_row():
    from packages.core.services.ai_file_permissions import (
        cancel_pending_file_approvals,
        resolve_file_approval_message,
        resolve_pending_file_approval_from_reply,
    )

    for handler in (
        guard_ai_file_mutation,
        cancel_pending_file_approvals,
        resolve_file_approval_message,
        resolve_pending_file_approval_from_reply,
    ):
        source = inspect.getsource(handler)
        assert "with_for_update" in source
        assert "populate_existing" in source


def test_canonical_visible_paths_reject_filesystem_alias(monkeypatch, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    entity_root = tmp_path / "ent_1"
    actual = entity_root / "actual"
    actual.mkdir(parents=True)
    (entity_root / "alias").symlink_to(actual, target_is_directory=True)

    with pytest.raises(ValueError, match="filesystem alias"):
        canonical_visible_user_paths("ent_1", ["alias/report.md"])


@pytest.mark.asyncio
async def test_manor_upload_approval_commits_the_staged_bytes(monkeypatch, tmp_path):
    from packages.core.ai.runtime import manor_actions
    from packages.core.config import get_settings
    from packages.core.services import ai_file_permissions, knowledge_sync

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path / "entity-fs"))
    source = tmp_path / "entity-fs" / "ent_upload" / "docs" / "source.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"approved bytes")
    approval_calls = []

    async def approve(**kwargs):
        approval_calls.append(kwargs)
        source.write_bytes(b"changed after approval check")
        return None

    async def sync(**_kwargs):
        return SimpleNamespace(synced=True, document_id="doc_1", reason=None)

    async def no_cache(*_args):
        return None

    async def allow_read(**_kwargs):
        return None

    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_mutation", approve)
    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_read_access", allow_read)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", sync)
    monkeypatch.setattr(manor_actions, "_runtime_manor_invalidate_read_cache", no_cache)

    result = json.loads(await manor_actions.runtime_manor_upload_document(
        entity_id="ent_upload",
        params={"file_path": str(source), "name": "docs/report.md"},
        user_id="user_1",
        conversation_id="conv_1",
    ))

    target = tmp_path / "entity-fs" / "ent_upload" / "docs" / "report.md"
    assert result["status"] == "uploaded"
    assert target.read_bytes() == b"approved bytes"
    assert approval_calls[0]["approval_payload"]["content_sha256"] == hashlib.sha256(
        b"approved bytes"
    ).hexdigest()


@pytest.mark.asyncio
async def test_manor_upload_removes_new_file_when_knowledge_projection_fails(
    monkeypatch,
    tmp_path,
):
    from packages.core.ai.runtime import manor_actions
    from packages.core.config import get_settings
    from packages.core.services import ai_file_permissions, knowledge_sync

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path / "entity-fs"))
    source = tmp_path / "entity-fs" / "ent_upload" / "source.md"
    source.parent.mkdir(parents=True)
    source.write_text("source", encoding="utf-8")

    async def allow(**_kwargs):
        return None

    async def fail_sync(**_kwargs):
        return SimpleNamespace(
            synced=False,
            document_id=None,
            reason="storage_limit",
        )

    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_mutation", allow)
    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_read_access", allow)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", fail_sync)

    result = json.loads(await manor_actions.runtime_manor_upload_document(
        entity_id="ent_upload",
        params={"file_path": str(source), "name": "docs/report.md"},
        user_id="user_1",
    ))

    assert result["uploaded"] is False
    assert result["knowledge_sync_reason"] == "storage_limit"
    assert not (
        tmp_path / "entity-fs" / "ent_upload" / "docs" / "report.md"
    ).exists()
    assert not (tmp_path / "entity-fs" / "ent_upload" / "docs").exists()


@pytest.mark.asyncio
async def test_manor_upload_ignores_model_supplied_provenance_ids(
    monkeypatch,
    tmp_path,
):
    from packages.core.ai.runtime import manor_actions
    from packages.core.config import get_settings
    from packages.core.services import ai_file_permissions, knowledge_sync

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path / "entity-fs"))
    source = tmp_path / "entity-fs" / "ent_upload" / "source.md"
    source.parent.mkdir(parents=True)
    source.write_text("source", encoding="utf-8")
    sync_calls = []

    async def allow(**_kwargs):
        return None

    async def sync(**kwargs):
        sync_calls.append(kwargs)
        return SimpleNamespace(
            synced=True,
            document_id="doc_1",
            reason=None,
            content_changed=True,
        )

    async def no_cache(*_args):
        return None

    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_mutation", allow)
    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_read_access", allow)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", sync)
    monkeypatch.setattr(manor_actions, "_runtime_manor_invalidate_read_cache", no_cache)

    result = json.loads(await manor_actions.runtime_manor_upload_document(
        entity_id="ent_upload",
        params={
            "file_path": str(source),
            "name": "report.md",
            "workspace_id": "forged_workspace",
            "task_id": "forged_task",
            "agent_id": "forged_agent",
        },
        user_id="user_1",
        agent_id="trusted_agent",
    ))

    assert result["status"] == "uploaded"
    assert sync_calls[0]["workspace_id"] is None
    assert sync_calls[0]["task_id"] is None
    assert sync_calls[0]["agent_id"] == "trusted_agent"


@pytest.mark.asyncio
async def test_manor_upload_rejects_sources_outside_entity(monkeypatch, tmp_path):
    from packages.core.ai.runtime.manor_actions import runtime_manor_upload_document
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path / "entity-fs"))

    denied = json.loads(await runtime_manor_upload_document(
        entity_id="ent_upload",
        params={"file_path": "/etc/hosts", "name": "hosts.txt"},
        user_id="user_1",
    ))

    assert "user-visible file inside the Entity filesystem" in denied["error"]

    temp_source = tmp_path / "shared-temp-source.md"
    temp_source.write_text("not entity-scoped", encoding="utf-8")
    denied_temp = json.loads(await runtime_manor_upload_document(
        entity_id="ent_upload",
        params={"file_path": str(temp_source), "name": "temp.txt"},
        user_id="user_1",
    ))
    assert "user-visible file inside the Entity filesystem" in denied_temp["error"]


@pytest.mark.asyncio
async def test_manor_upload_rejects_hidden_entity_source(monkeypatch, tmp_path):
    from packages.core.ai.runtime.manor_actions import runtime_manor_upload_document
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path / "entity-fs"))
    source = tmp_path / "entity-fs" / "ent_upload" / ".ai" / "secret.md"
    source.parent.mkdir(parents=True)
    source.write_text("secret", encoding="utf-8")

    denied = json.loads(await runtime_manor_upload_document(
        entity_id="ent_upload",
        params={"file_path": str(source), "name": "published-secret.md"},
        user_id="user_1",
    ))

    assert denied["error"] == "Cannot upload a hidden/system source file"


def test_mutating_file_tools_receive_the_runtime_envelope():
    from packages.core.ai.runtime.tool_execution import (
        RUNTIME_ENVELOPE_AWARE_TOOLS,
        RUNTIME_LLM_METADATA_AWARE_TOOLS,
    )

    assert {
        "write_file",
        "edit_file",
        "patch_file",
        "delete_file",
        "generate_file",
        "generate_document_file",
        "save_sandbox_file",
        "sandbox_save_result",
        "bash",
        "manor",
    }.issubset(RUNTIME_ENVELOPE_AWARE_TOOLS)
    assert "write_file" not in RUNTIME_LLM_METADATA_AWARE_TOOLS


def test_file_approval_reply_classifier_is_conservative():
    assert classify_file_approval_reply("是的") == "approve"
    assert classify_file_approval_reply("删除吧") == "approve"
    assert classify_file_approval_reply("always approve") == "always_approve"
    assert classify_file_approval_reply("不要") == "reject"
    assert classify_file_approval_reply("不是这个文件") == "reject"
    assert classify_file_approval_reply("yes, but use the other draft instead because this is wrong") is None


def test_visible_user_paths_preserves_root_marker_and_filters_hidden_paths():
    assert visible_user_paths([".", "./", "/", ".ai/memory.md", "docs/report.md"]) == [
        ".",
        ".",
        ".",
        "docs/report.md",
    ]


@pytest.mark.asyncio
async def test_hidden_file_guards_fail_closed_before_database_access():
    hidden_path = ".ai/workspaces/ws_other/memory/facts/secret.md"
    paths = [hidden_path]

    resource = json.loads(await guard_ai_file_resource_access(
        entity_id="entity_hidden_guard",
        user_id=None,
        conversation_id=None,
        tool_name="delete_file",
        action=FileMutationAction.DELETE,
        paths=paths,
    ))
    mutation = json.loads(await guard_ai_file_mutation(
        entity_id="entity_hidden_guard",
        user_id=None,
        conversation_id=None,
        tool_name="delete_file",
        action=FileMutationAction.DELETE,
        paths=paths,
    ))

    assert resource["mode"] == "entity_file_acl_denied"
    assert mutation["mode"] == "entity_file_acl_denied"
    assert resource["operation"]["paths"] == []
    assert mutation["operation"]["paths"] == []

    mixed_resource = json.loads(await guard_ai_file_resource_access(
        entity_id="entity_hidden_guard",
        user_id=None,
        conversation_id=None,
        tool_name="bash",
        action=FileMutationAction.DELETE,
        paths=["docs/public.md", hidden_path],
    ))
    mixed_mutation = json.loads(await guard_ai_file_mutation(
        entity_id="entity_hidden_guard",
        user_id=None,
        conversation_id=None,
        tool_name="bash",
        action=FileMutationAction.DELETE,
        paths=["docs/public.md", hidden_path],
    ))

    assert mixed_resource["mode"] == "entity_file_acl_denied"
    assert mixed_mutation["mode"] == "entity_file_acl_denied"
    assert mixed_resource["operation"]["paths"] == ["docs/public.md"]
    assert mixed_mutation["operation"]["paths"] == ["docs/public.md"]


def test_file_approval_prompt_uses_human_readable_details():
    assert (
        _approval_prompt(
            action="create_document",
            tool_name="generate_document_file",
            paths=["docs/report.md"],
            mode="approval",
        )
        == "Allow Manor to create file docs/report.md?"
    )
    assert (
        _approval_prompt(
            action="shell_modify",
            tool_name="bash",
            paths=["."],
            mode="approval",
        )
        == "Allow Manor to run a command that may modify files Knowledge root?"
    )


def test_file_approval_payload_always_includes_content_preview():
    import json

    payload = json.loads(
        _hitl_payload(
            "hitl_1",
            action="write",
            tool_name="write_file",
            paths=["docs/report.md"],
            mode="approval",
            content_preview="# Report\n\nDraft body",
        )
    )

    assert payload["hitl"]["content"] == "# Report\n\nDraft body"

    fallback = json.loads(
        _hitl_payload(
            "hitl_2",
            action="delete",
            tool_name="delete_file",
            paths=["docs/report.md"],
            mode="approval",
        )
    )

    assert "delete_file" in fallback["hitl"]["content"]
    assert "docs/report.md" in fallback["hitl"]["content"]


@pytest.mark.asyncio
async def test_autonomous_agent_file_write_keeps_runtime_principal_and_workspace_policy(
    monkeypatch,
    tmp_path,
):
    """A Task agent must not become an anonymous legacy caller inside file tools."""
    from packages.core import database
    from packages.core.config import get_settings
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import RuntimePrincipal, RuntimePrincipalKind
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.governance import service as governance_service
    from packages.core.services import runtime_authorization
    from packages.core.services.runtime_authorization import PermissionDecision

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _statement):
            return "folder_1"

    authorization_calls = []
    policy_calls = []

    async def authorize(_db, **kwargs):
        authorization_calls.append(kwargs)
        return PermissionDecision.allow("agent_tool_binding")

    async def auto_approve(_db, **kwargs):
        policy_calls.append(kwargs)
        return True

    monkeypatch.setattr(database, "async_session", FakeSession)
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_AI_FILE_HITL_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(runtime_authorization, "authorize_runtime_action", authorize)
    monkeypatch.setattr(governance_service, "workspace_policy_auto_approves", auto_approve)

    envelope = RuntimeEnvelope(
        surface=ChatSurface.SCHEDULED_AGENT_RUN,
        profile=RuntimeProfile.BACKGROUND_WORKER,
        principal=RuntimePrincipal(
            kind=RuntimePrincipalKind.AGENT,
            entity_id="ent_1",
            agent_id="agent_1",
            workspace_id="ws_1",
        ),
        entity_id="ent_1",
        agent_id="agent_1",
        workspace_id="ws_1",
        task_id="task_1",
        allowed_tool_names=("write_file",),
        tool_bindings=({"name": "write_file"},),
    )

    artifact_path = (
        "Workspaces/_by_id/folder_1/tasks/task_1/documents/report.md"
    )
    blocked = await guard_ai_file_mutation(
        entity_id="ent_1",
        user_id=None,
        conversation_id=None,
        workspace_id="ws_1",
        task_id="task_1",
        runtime_envelope=envelope,
        tool_name="write_file",
        action="write",
        paths=[artifact_path],
    )

    assert blocked is None
    assert authorization_calls == [
        {
            "entity_id": "ent_1",
            "user_id": None,
            "workspace_id": "ws_1",
            "action_key": "workspace.file.write",
            "capability_id": "file.write",
            "principal_kind": RuntimePrincipalKind.AGENT.value,
            "principal_agent_id": "agent_1",
            "principal_execution_user_id": None,
            "tool_name": "write_file",
            "bound_tool_names": {"write_file"},
            "conversation_id": None,
            "task_id": "task_1",
            "runtime_surface": ChatSurface.SCHEDULED_AGENT_RUN,
        }
    ]
    assert policy_calls == [
        {
            "workspace_id": "ws_1",
            "action_key": "workspace.file.write",
            "resource_id": artifact_path,
            "capability_id": "file.write",
        }
    ]


@pytest.mark.asyncio
async def test_autonomous_file_write_does_not_bypass_authorization_or_safe_policy(
    monkeypatch,
    tmp_path,
):
    from packages.core import database
    from packages.core.config import get_settings
    from packages.core.governance import service as governance_service
    from packages.core.services import runtime_authorization
    from packages.core.services.runtime_authorization import PermissionDecision

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _statement):
            return "folder_1"

    monkeypatch.setattr(database, "async_session", FakeSession)
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_AI_FILE_HITL_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    artifact_path = (
        "Workspaces/_by_id/folder_1/tasks/task_1/documents/report.md"
    )

    async def deny_authorization(_db, **_kwargs):
        return PermissionDecision.deny("agent binding was revoked", "agent_tool_binding")

    policy_calls = []

    async def auto_approve(_db, **kwargs):
        policy_calls.append(kwargs)
        return True

    monkeypatch.setattr(runtime_authorization, "authorize_runtime_action", deny_authorization)
    monkeypatch.setattr(governance_service, "workspace_policy_auto_approves", auto_approve)
    denied = json.loads(await guard_ai_file_mutation(
        entity_id="ent_1",
        user_id=None,
        conversation_id=None,
        workspace_id="ws_1",
        task_id="task_1",
        tool_name="write_file",
        action="write",
        paths=[artifact_path],
    ))
    assert denied["error"] == "file_permission_denied"
    assert denied["reason"] == "agent binding was revoked"
    assert policy_calls == []

    async def allow_authorization(_db, **_kwargs):
        return PermissionDecision.allow("agent_tool_binding")

    async def require_approval(_db, **_kwargs):
        return False

    monkeypatch.setattr(runtime_authorization, "authorize_runtime_action", allow_authorization)
    monkeypatch.setattr(governance_service, "workspace_policy_auto_approves", require_approval)
    held = json.loads(await guard_ai_file_mutation(
        entity_id="ent_1",
        user_id=None,
        conversation_id=None,
        workspace_id="ws_1",
        task_id="task_1",
        tool_name="write_file",
        action="write",
        paths=[artifact_path],
    ))
    assert held["error"] == "file_permission_denied"
    assert "no conversation_id" in held["reason"]


@pytest.mark.asyncio
async def test_autonomous_agent_cannot_create_outside_workspace_artifact_root(
    monkeypatch,
    tmp_path,
):
    from packages.core import database
    from packages.core.config import get_settings
    from packages.core.services import runtime_authorization
    from packages.core.services.runtime_authorization import PermissionDecision

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _statement):
            return "folder_1"

    async def allow_authorization(_db, **_kwargs):
        return PermissionDecision.allow("agent_tool_binding")

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(
        runtime_authorization,
        "authorize_runtime_action",
        allow_authorization,
    )

    denied = json.loads(await guard_ai_file_mutation(
        entity_id="ent_1",
        user_id=None,
        conversation_id=None,
        workspace_id="ws_1",
        task_id="task_1",
        tool_name="write_file",
        action="write",
        paths=["docs/report.md"],
    ))

    assert denied["mode"] == "entity_file_acl_denied"
    assert "Workspace artifact root" in denied["reason"]

    outside = tmp_path / "outside"
    outside.mkdir()
    workspace_link = tmp_path / "ent_1" / "Workspaces" / "_by_id" / "folder_1"
    workspace_link.parent.mkdir(parents=True)
    workspace_link.symlink_to(outside, target_is_directory=True)
    symlink_denied = json.loads(await guard_ai_file_mutation(
        entity_id="ent_1",
        user_id=None,
        conversation_id=None,
        workspace_id="ws_1",
        task_id="task_1",
        tool_name="write_file",
        action="write",
        paths=["Workspaces/_by_id/folder_1/escaped.md"],
    ))
    assert symlink_denied["mode"] == "entity_file_acl_denied"


@pytest.mark.asyncio
async def test_registered_generate_file_task_authorizes_inner_materializer_once(
    monkeypatch,
    tmp_path,
):
    """End-to-end regression for a delegated, conversation-less Task write."""

    from unittest.mock import AsyncMock

    from packages.core import database
    from packages.core.ai.runtime import (
        AIRuntimeRequest,
        ChatSurface,
        RuntimeResolver,
        runtime_execute_registered_tool,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        runtime_current_tool_authorization_receipt,
    )
    from packages.core.ai.runtime.tool_context import (
        runtime_tool_call_context_from_kwargs,
    )
    from packages.core.services import ai_file_permissions, runtime_authorization
    from packages.core.services.runtime_authorization import PermissionDecision
    from packages.core.config import get_settings

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _statement):
            return "folder_1"

    monkeypatch.setattr(database, "async_session", FakeSession)
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    authorization_calls = []
    invocation = 0

    async def allow_authorization(_db, **kwargs):
        authorization_calls.append(kwargs)
        return PermissionDecision.allow("agent_tool_binding")

    async def unexpected_legacy_preference(*_args, **_kwargs):
        raise AssertionError("The inner materializer must reuse Workspace governance")

    monkeypatch.setattr(
        runtime_authorization,
        "authorize_runtime_action",
        allow_authorization,
    )
    monkeypatch.setattr(
        ai_file_permissions,
        "load_user_file_permission_mode",
        unexpected_legacy_preference,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.approval_service.guard_runtime_tool_action",
        AsyncMock(return_value=None),
    )

    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.SCHEDULED_AGENT_RUN,
            entity_id="ent_1",
            user_id=None,
            agent_id="agent_1",
            workspace_id="ws_1",
            task_id="task_1",
        ),
        tool_schemas=[{"type": "function", "function": {"name": "generate_file"}}],
        allowed_tool_names={"generate_file"},
    )

    async def delegated_document_materializer(
        entity_id: str = "",
        user_id: str = "",
        **kwargs,
    ) -> str:
        nonlocal invocation
        invocation += 1
        expected_action_key = (
            "workspace.file.create"
            if invocation == 1
            else "workspace.file.modify"
        )
        context = runtime_tool_call_context_from_kwargs(kwargs)
        receipt = runtime_current_tool_authorization_receipt()
        assert receipt is not None
        assert receipt.tool_name == "generate_file"
        assert receipt.capability_id == "file.write"
        assert receipt.action_key == expected_action_key
        assert receipt.resource_ids == ("documents/cram-pack.md",)
        assert not receipt.authorizes_workspace_file_mutation(
            capability_id="file.write",
            action_key=expected_action_key,
            paths=["documents/cram-pack.md"],
            entity_id=entity_id,
            user_id=None,
            workspace_id=None,
            conversation_id=None,
            task_id="task_1",
        )
        assert not receipt.authorizes_workspace_file_mutation(
            capability_id="file.write",
            action_key=expected_action_key,
            paths=["documents/cram-pack.md"],
            entity_id=entity_id,
            user_id=None,
            workspace_id="ws_1",
            conversation_id=None,
            task_id="another_task",
        )
        assert not receipt.authorizes_workspace_file_mutation(
            capability_id="file.write",
            action_key="workspace.file.delete",
            paths=["documents/cram-pack.md"],
            entity_id=entity_id,
            user_id=None,
            workspace_id="ws_1",
            conversation_id=None,
            task_id="task_1",
        )
        assert not receipt.authorizes_workspace_file_mutation(
            capability_id="file.write",
            action_key=(
                "workspace.file.modify"
                if expected_action_key == "workspace.file.create"
                else "workspace.file.create"
            ),
            paths=["documents/cram-pack.md"],
            entity_id=entity_id,
            user_id=None,
            workspace_id="ws_1",
            conversation_id=None,
            task_id="task_1",
        )
        assert not receipt.authorizes_workspace_file_mutation(
            capability_id="file.write",
            action_key=expected_action_key,
            paths=["documents/another-file.md"],
            entity_id=entity_id,
            user_id=None,
            workspace_id="ws_1",
            conversation_id=None,
            task_id="task_1",
        )
        assert not receipt.authorizes_workspace_file_mutation(
            capability_id="file.write",
            action_key=expected_action_key,
            paths=["documents/cram-pack.md/unapproved-child.txt"],
            entity_id=entity_id,
            user_id=None,
            workspace_id="ws_1",
            conversation_id=None,
            task_id="task_1",
        )
        blocked = await guard_ai_file_mutation(
            entity_id=entity_id,
            user_id=user_id or None,
            conversation_id=context.conversation_id,
            workspace_id=context.workspace_id,
            task_id=context.task_id,
            runtime_envelope=context.runtime_envelope,
            tool_name="generate_document_file",
            action="create_document",
            paths=[
                "Workspaces/_by_id/folder_1/tasks/task_1/documents/cram-pack.md"
            ],
        )
        if invocation == 1:
            assert blocked is None
            return "materialized"
        return blocked or "unexpected-existing-write"

    result = await runtime_execute_registered_tool(
        tool_name="generate_file",
        arguments={
            "kind": "document",
            "name": "cram-pack.md",
            "content": "# Cram pack",
        },
        handler_resolver=lambda _name: delegated_document_materializer,
        entity_id="ent_1",
        agent_id="agent_1",
        workspace_id="ws_1",
        task_id="task_1",
        runtime_envelope=envelope,
    )

    assert result == "materialized"
    existing = (
        tmp_path
        / "ent_1"
        / "Workspaces"
        / "_by_id"
        / "folder_1"
        / "tasks"
        / "task_1"
        / "documents"
        / "cram-pack.md"
    )
    existing.parent.mkdir(parents=True)
    existing.write_text("existing", encoding="utf-8")
    retry_result = await runtime_execute_registered_tool(
        tool_name="generate_file",
        arguments={
            "kind": "document",
            "name": "cram-pack.md",
            "content": "# Updated cram pack",
        },
        handler_resolver=lambda _name: delegated_document_materializer,
        entity_id="ent_1",
        agent_id="agent_1",
        workspace_id="ws_1",
        task_id="task_1",
        runtime_envelope=envelope,
    )

    assert json.loads(retry_result)["mode"] == "entity_file_acl_denied"
    assert len(authorization_calls) == 2
    assert [call["tool_name"] for call in authorization_calls] == [
        "generate_file",
        "generate_file",
    ]
    assert [call["action_key"] for call in authorization_calls] == [
        "workspace.file.create",
        "workspace.file.modify",
    ]
    assert all(
        call["bound_tool_names"] == {"generate_file"}
        for call in authorization_calls
    )
    assert runtime_current_tool_authorization_receipt() is None


@pytest.mark.asyncio
async def test_runtime_approval_receipt_uses_the_guarded_classification_once():
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalMiddleware,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )

    class CountingApprovalAdapter:
        def __init__(self):
            self.classification_calls = 0
            self.classification = RuntimeToolClassification.action_call(
                RuntimeApprovalAction(
                    kind="action",
                    action_key="workspace.file.create",
                    risk_level="medium",
                    title="generate document",
                    resource_kind="file",
                    operation="create",
                    resource_id="cram-pack.md",
                )
            )

        def classify_request(self, request):
            self.classification_calls += 1
            return self.classification

        async def guard_request(self, request):
            assert request.classification is self.classification
            return None

    adapter = CountingApprovalAdapter()
    middleware = RuntimeApprovalMiddleware(policy_adapter=adapter)
    request = RuntimeApprovalRequest(
        tool_name="generate_file",
        arguments={"kind": "document", "name": "cram-pack.md"},
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
    )

    decision = await middleware.guard_request(request)
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=decision,
        request=request,
    )

    assert adapter.classification_calls == 1
    assert decision.classification is adapter.classification
    assert receipt is not None
    assert receipt.action_key == "workspace.file.create"
    assert receipt.resource_ids == ("documents/cram-pack.md",)


def test_runtime_receipt_factory_models_code_bundle_as_a_typed_resource_tree():
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        WorkspaceFileResourceMatchKind,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )

    classification = RuntimeToolClassification.action_call(
        RuntimeApprovalAction(
            kind="action",
            action_key="workspace.file.create",
            risk_level="medium",
            title="generate code bundle",
            resource_kind="file",
            operation="create",
            resource_id="site.zip",
        )
    )
    request = RuntimeApprovalRequest(
        tool_name="generate_file",
        arguments={
            "kind": "code",
            "name": "site.zip",
            "files": [{"path": "index.html", "content": "<h1>Site</h1>"}],
        },
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )

    assert receipt is not None
    assert receipt.resource_ids == ("code/site",)
    assert receipt.file_resources[0].match_kind is WorkspaceFileResourceMatchKind.TREE
    common = {
        "capability_id": "file.write",
        "action_key": "workspace.file.create",
        "entity_id": "ent_1",
        "user_id": None,
        "workspace_id": "ws_1",
        "conversation_id": None,
        "task_id": "task_1",
    }
    assert receipt.authorizes_workspace_file_mutation(
        paths=["code/site/index.html"],
        **common,
    )
    assert not receipt.authorizes_workspace_file_mutation(
        paths=["code/site-copy/index.html"],
        **common,
    )


def test_runtime_receipt_factory_projects_media_outputs_from_typed_contracts():
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        WorkspaceFileResourceMatchKind,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
        RuntimeWorkspaceFileScope,
        WorkspaceFileExistence,
    )

    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        kind="action",
        action_key="workspace.file.create",
        risk_level="medium",
        title="prepare narration timeline",
        resource_kind="file",
        operation="create",
    ))
    request = RuntimeApprovalRequest(
        tool_name="prepare_narration_timeline",
        arguments={
            "normalized_output_directory": "runs/topic/audio/normalized",
            "timeline_name": "runs/topic/timeline/final.json",
        },
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
        workspace_file_scope=RuntimeWorkspaceFileScope(
            WorkspaceFileExistence.MISSING,
            "Workspaces/_by_id/folder_1/tasks/task_1",
        ),
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )

    assert receipt is not None
    assert receipt.resource_ids == (
        "runs/topic/audio/normalized",
        "technical",
        "runs/topic/timeline",
        "subtitles",
    )
    assert all(
        resource.match_kind is WorkspaceFileResourceMatchKind.TREE
        for resource in receipt.file_resources
    )
    common = {
        "capability_id": "file.write",
        "action_key": "workspace.file.create",
        "entity_id": "ent_1",
        "user_id": None,
        "workspace_id": "ws_1",
        "conversation_id": None,
        "task_id": "task_1",
    }
    assert receipt.authorizes_workspace_file_mutation(
        paths=[
            "Workspaces/_by_id/folder_1/tasks/task_1/"
            "runs/topic/audio/normalized/puck/segment-001.wav"
        ],
        **common,
    )
    assert not receipt.authorizes_workspace_file_mutation(
        paths=[
            "Workspaces/_by_id/folder_1/tasks/task_1/"
            "runs/another-topic/final.mp4"
        ],
        **common,
    )

    full_path_request = RuntimeApprovalRequest(
        tool_name="merge_videos",
        arguments={
            "output_name": (
                "Workspaces/_by_id/folder_1/tasks/task_1/"
                "runs/topic/video/final.mp4"
            )
        },
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
        workspace_file_scope=request.workspace_file_scope,
    )
    full_path_receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=full_path_request,
    )
    assert full_path_receipt is not None
    assert full_path_receipt.authorizes_workspace_file_mutation(
        paths=[
            "Workspaces/_by_id/folder_1/tasks/task_1/"
            "runs/topic/video/final.mp4"
        ],
        **common,
    )


@pytest.mark.asyncio
async def test_media_tool_scope_resolves_the_workspace_task_storage_root(monkeypatch):
    from packages.core import database
    from packages.core.ai.runtime.approval_classifier import (
        resolve_runtime_workspace_file_scope,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        WorkspaceFileExistence,
    )

    class ScalarResult:
        def scalar_one_or_none(self):
            return "folder_1"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return ScalarResult()

    monkeypatch.setattr(database, "async_session", FakeSession)

    scope = await resolve_runtime_workspace_file_scope(
        tool_name="merge_videos",
        arguments={"output_name": "runs/topic/video/final.mp4"},
        entity_id="ent_1",
        workspace_id="ws_1",
        task_id="TASK01",
    )

    assert scope is not None
    assert scope.existence is WorkspaceFileExistence.MISSING
    assert scope.artifact_base_dir == (
        "Workspaces/_by_id/folder_1/tasks/TASK01"
    )


@pytest.mark.asyncio
async def test_generate_image_workspace_asset_scope_resolves_workspace_storage_root(
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.runtime.approval_classifier import (
        resolve_runtime_workspace_file_scope,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        WorkspaceFileExistence,
    )

    class ScalarResult:
        def scalar_one_or_none(self):
            return "folder_1"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return ScalarResult()

    monkeypatch.setattr(database, "async_session", FakeSession)

    scope = await resolve_runtime_workspace_file_scope(
        tool_name="generate_image",
        arguments={"workspace_asset_key": "stickman_character"},
        entity_id="ent_1",
        workspace_id="ws_1",
        task_id="TASK01",
    )

    assert scope is not None
    assert scope.existence is WorkspaceFileExistence.MISSING
    assert scope.artifact_base_dir == "Workspaces/_by_id/folder_1"


@pytest.mark.asyncio
async def test_generate_image_without_workspace_asset_stays_in_task_storage_root(
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.runtime.approval_classifier import (
        resolve_runtime_workspace_file_scope,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        WorkspaceFileExistence,
    )

    class ScalarResult:
        def scalar_one_or_none(self):
            return "folder_1"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return ScalarResult()

    monkeypatch.setattr(database, "async_session", FakeSession)

    scope = await resolve_runtime_workspace_file_scope(
        tool_name="generate_image",
        arguments={"prompt": "A task-scoped image"},
        entity_id="ent_1",
        workspace_id="ws_1",
        task_id="TASK01",
    )

    assert scope is not None
    assert scope.existence is WorkspaceFileExistence.MISSING
    assert scope.artifact_base_dir == (
        "Workspaces/_by_id/folder_1/tasks/TASK01"
    )


@pytest.mark.asyncio
async def test_generate_file_workspace_image_asset_uses_workspace_storage_root(
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.runtime.approval_classifier import (
        resolve_runtime_workspace_file_scope,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        WorkspaceFileExistence,
    )

    class ScalarResult:
        def scalar_one_or_none(self):
            return "folder_1"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return ScalarResult()

    monkeypatch.setattr(database, "async_session", FakeSession)

    scope = await resolve_runtime_workspace_file_scope(
        tool_name="generate_file",
        arguments={
            "kind": "image",
            "name": "brand/stickman-character.png",
            "params": {
                "workspace_asset_key": "stickman_character",
                "reuse_if_exists": True,
            },
        },
        entity_id="ent_1",
        workspace_id="ws_1",
        task_id="TASK01",
    )

    assert scope is not None
    assert scope.existence is WorkspaceFileExistence.MISSING
    assert scope.artifact_base_dir == "Workspaces/_by_id/folder_1"

    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
    )

    arguments = {
        "kind": "image",
        "name": "brand/stickman-character.png",
        "params": {
            "workspace_asset_key": "stickman_character",
            "reuse_if_exists": True,
        },
    }
    classification = classify_runtime_tool(
        "generate_file",
        arguments,
        entity_id="ent_1",
        workspace_id="ws_1",
        task_id="TASK01",
        workspace_file_scope=scope,
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=RuntimeApprovalRequest(
            tool_name="generate_file",
            arguments=arguments,
            entity_id="ent_1",
            user_id="user_1",
            workspace_id="ws_1",
            task_id="TASK01",
            workspace_file_scope=scope,
        ),
    )

    assert receipt is not None
    assert receipt.resource_ids == ("brand/stickman-character.png",)
    assert receipt.authorizes_workspace_file_commit(
        entity_id="ent_1",
        path="Workspaces/_by_id/folder_1/brand/stickman-character.png",
        target_exists=False,
    )
    assert not receipt.authorizes_workspace_file_commit(
        entity_id="ent_1",
        path=(
            "Workspaces/_by_id/folder_1/tasks/TASK01/"
            "brand/stickman-character.png"
        ),
        target_exists=False,
    )


@pytest.mark.asyncio
async def test_generate_file_task_image_without_asset_stays_in_task_storage_root(
    monkeypatch,
):
    from packages.core import database
    from packages.core.ai.runtime.approval_classifier import (
        resolve_runtime_workspace_file_scope,
    )

    class ScalarResult:
        def scalar_one_or_none(self):
            return "folder_1"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return ScalarResult()

    monkeypatch.setattr(database, "async_session", FakeSession)

    scope = await resolve_runtime_workspace_file_scope(
        tool_name="generate_file",
        arguments={
            "kind": "image",
            "name": "images/scene-01.png",
            "params": {"quality": "high"},
        },
        entity_id="ent_1",
        workspace_id="ws_1",
        task_id="TASK01",
    )

    assert scope is not None
    assert scope.artifact_base_dir == (
        "Workspaces/_by_id/folder_1/tasks/TASK01"
    )


@pytest.mark.asyncio
async def test_sandbox_save_scope_resolves_the_workspace_task_storage_root(monkeypatch):
    from packages.core import database
    from packages.core.ai.runtime.approval_classifier import (
        resolve_runtime_workspace_file_scope,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        WorkspaceFileExistence,
    )

    class ScalarResult:
        def scalar_one_or_none(self):
            return "folder_1"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return ScalarResult()

    monkeypatch.setattr(database, "async_session", FakeSession)

    scope = await resolve_runtime_workspace_file_scope(
        tool_name="sandbox_save_result",
        arguments={"filename": "result.txt", "sandbox_id": "sb_1"},
        entity_id="ent_1",
        workspace_id="ws_1",
        task_id="TASK01",
    )

    assert scope is not None
    assert scope.existence is WorkspaceFileExistence.MISSING
    assert scope.artifact_base_dir == "Workspaces/_by_id/folder_1/tasks/TASK01"


def test_runtime_receipt_factory_binds_direct_document_to_exact_scoped_target():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        WorkspaceFileResourceMatchKind,
    )

    arguments = {"name": "cram-pack", "file_type": "md", "content": "# Pack"}
    classification = classify_runtime_tool(
        "generate_document_file",
        arguments,
        entity_id="ent_1",
    )
    assert classification.action is not None
    assert classification.action.resource_id == "cram-pack"
    request = RuntimeApprovalRequest(
        tool_name="generate_document_file",
        arguments=arguments,
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )

    assert receipt is not None
    assert receipt.resource_ids == ("documents/cram-pack.md",)
    assert receipt.file_resources[0].match_kind is WorkspaceFileResourceMatchKind.EXACT


def test_runtime_receipt_factory_limits_unnamed_generation_to_artifact_kind_tree():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        WorkspaceFileResourceMatchKind,
    )

    arguments = {"kind": "image", "prompt": "A study diagram"}
    classification = classify_runtime_tool(
        "generate_file",
        arguments,
        entity_id="ent_1",
    )
    request = RuntimeApprovalRequest(
        tool_name="generate_file",
        arguments=arguments,
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )

    assert receipt is not None
    assert receipt.resource_ids == ("images",)
    assert receipt.file_resources[0].match_kind is WorkspaceFileResourceMatchKind.TREE
    common = {
        "capability_id": "file.write",
        "action_key": "workspace.file.create",
        "entity_id": "ent_1",
        "user_id": None,
        "workspace_id": "ws_1",
        "conversation_id": None,
        "task_id": "task_1",
    }
    assert receipt.authorizes_workspace_file_mutation(
        paths=["images/generated.png"],
        **common,
    )
    assert not receipt.authorizes_workspace_file_mutation(
        paths=["documents/generated.md"],
        **common,
    )


@pytest.mark.parametrize(
    ("requested_name", "generated_path", "rejected_path", "resource_id"),
    [
        (
            "technical/stickman-narrator-setup-sample.mp3",
            "Workspaces/_by_id/folder_1/tasks/task_1/audio/puck/"
            "stickman-narrator-setup-sample.mp3",
            "Workspaces/_by_id/folder_1/tasks/task_1/documents/"
            "stickman-narrator-setup-sample.mp3",
            "audio",
        ),
        (
            "runs/topic/audio/segment-001.wav",
            "Workspaces/_by_id/folder_1/tasks/task_1/runs/topic/audio/puck/"
            "segment-001.wav",
            "Workspaces/_by_id/folder_1/tasks/task_1/runs/other/audio/puck/"
            "segment-001.wav",
            "runs/topic/audio",
        ),
    ],
)
def test_generate_file_narration_receipt_authorizes_runtime_voice_subdirectory(
    requested_name,
    generated_path,
    rejected_path,
    resource_id,
):
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        WorkspaceFileResourceMatchKind,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeWorkspaceFileScope,
        WorkspaceFileExistence,
    )

    arguments = {
        "kind": "audio",
        "purpose": "narration",
        "narration_voice_mode": "fixed_per_workspace",
        "name": requested_name,
        "prompt": "A short narration sample.",
    }
    classification = classify_runtime_tool(
        "generate_file",
        arguments,
        entity_id="ent_1",
        workspace_id="ws_1",
        task_id="task_1",
    )
    scope = RuntimeWorkspaceFileScope(
        WorkspaceFileExistence.MISSING,
        "Workspaces/_by_id/folder_1/tasks/task_1",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=RuntimeApprovalRequest(
            tool_name="generate_file",
            arguments=arguments,
            entity_id="ent_1",
            user_id="user_1",
            workspace_id="ws_1",
            task_id="task_1",
            workspace_file_scope=scope,
        ),
    )

    assert receipt is not None
    assert receipt.resource_ids == (resource_id,)
    assert (
        receipt.file_resources[0].match_kind
        is WorkspaceFileResourceMatchKind.TREE
    )
    assert receipt.authorizes_workspace_file_commit(
        entity_id="ent_1",
        path=generated_path,
        target_exists=False,
    )
    assert not receipt.authorizes_workspace_file_commit(
        entity_id="ent_1",
        path=rejected_path,
        target_exists=False,
    )
    assert not receipt.authorizes_workspace_file_commit(
        entity_id="ent_1",
        path=generated_path,
        target_exists=True,
    )


def test_generate_file_non_narration_audio_receipt_remains_exact():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        WorkspaceFileResourceMatchKind,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeWorkspaceFileScope,
        WorkspaceFileExistence,
    )

    arguments = {
        "kind": "audio",
        "purpose": "music",
        "name": "runs/topic/audio/music.mp3",
        "prompt": "A quiet music bed.",
    }
    classification = classify_runtime_tool(
        "generate_file",
        arguments,
        entity_id="ent_1",
        workspace_id="ws_1",
        task_id="task_1",
    )
    scope = RuntimeWorkspaceFileScope(
        WorkspaceFileExistence.MISSING,
        "Workspaces/_by_id/folder_1/tasks/task_1",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=RuntimeApprovalRequest(
            tool_name="generate_file",
            arguments=arguments,
            entity_id="ent_1",
            user_id="user_1",
            workspace_id="ws_1",
            task_id="task_1",
            workspace_file_scope=scope,
        ),
    )

    assert receipt is not None
    assert receipt.resource_ids == ("runs/topic/audio/music.mp3",)
    assert (
        receipt.file_resources[0].match_kind
        is WorkspaceFileResourceMatchKind.EXACT
    )
    assert receipt.authorizes_workspace_file_commit(
        entity_id="ent_1",
        path=(
            "Workspaces/_by_id/folder_1/tasks/task_1/"
            "runs/topic/audio/music.mp3"
        ),
        target_exists=False,
    )
    assert not receipt.authorizes_workspace_file_commit(
        entity_id="ent_1",
        path=(
            "Workspaces/_by_id/folder_1/tasks/task_1/"
            "runs/topic/audio/puck/music.mp3"
        ),
        target_exists=False,
    )


def test_generate_image_receipt_binds_explicit_workspace_output_directory():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        WorkspaceFileResourceMatchKind,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeWorkspaceFileScope,
        WorkspaceFileExistence,
    )

    arguments = {
        "prompt": "A reusable stickman character",
        "name": "brand/stickman-character.png",
        "workspace_asset_key": "stickman_character",
    }
    classification = classify_runtime_tool(
        "generate_image",
        arguments,
        entity_id="ent_1",
        workspace_id="ws_1",
        task_id="task_1",
    )
    request = RuntimeApprovalRequest(
        tool_name="generate_image",
        arguments=arguments,
        entity_id="ent_1",
        user_id="user_1",
        workspace_id="ws_1",
        task_id="task_1",
        workspace_file_scope=RuntimeWorkspaceFileScope(
            WorkspaceFileExistence.MISSING,
            "Workspaces/_by_id/folder_1",
        ),
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )

    assert receipt is not None
    assert receipt.resource_ids == ("brand",)
    assert (
        receipt.file_resources[0].match_kind
        is WorkspaceFileResourceMatchKind.TREE
    )
    assert receipt.authorizes_workspace_file_commit(
        entity_id="ent_1",
        path="Workspaces/_by_id/folder_1/brand/stickman-character.png",
        target_exists=False,
    )
    assert not receipt.authorizes_workspace_file_commit(
        entity_id="ent_1",
        path="Workspaces/_by_id/folder_1/images/unapproved.png",
        target_exists=False,
    )


def test_runtime_sandbox_receipt_binds_collision_safe_requested_file():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        WorkspaceFileAuthorizationResourceFactory,
        WorkspaceFileResourceMatchKind,
        WorkspaceFileScopeBinding,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeWorkspaceFileScope,
        WorkspaceFileExistence,
    )

    arguments = {"filename": "result.txt", "sandbox_id": "sb_1"}
    classification = classify_runtime_tool(
        "sandbox_save_result",
        arguments,
        entity_id="ent_1",
        workspace_id="ws_1",
        task_id="task_1",
    )
    artifact_base = "Workspaces/_by_id/folder_1/tasks/task_1"
    request = RuntimeApprovalRequest(
        tool_name="sandbox_save_result",
        arguments=arguments,
        entity_id="ent_1",
        user_id="user_1",
        workspace_id="ws_1",
        task_id="task_1",
        workspace_file_scope=RuntimeWorkspaceFileScope(
            WorkspaceFileExistence.MISSING,
            artifact_base,
        ),
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )

    assert receipt is not None
    assert WorkspaceFileAuthorizationResourceFactory.supports_workspace_scope(
        "sandbox_save_result"
    )
    assert receipt.workspace_scope_binding is WorkspaceFileScopeBinding.EXACT
    assert receipt.resource_ids == ("artifacts/result.txt",)
    assert (
        receipt.file_resources[0].match_kind
        is WorkspaceFileResourceMatchKind.COLLISION_SAFE_FILE
    )
    common = {
        "capability_id": "file.write",
        "action_key": "workspace.file.create",
        "entity_id": "ent_1",
        "user_id": "user_1",
        "workspace_id": "ws_1",
        "conversation_id": None,
        "task_id": "task_1",
    }
    assert receipt.authorizes_workspace_file_mutation(
        paths=[f"{artifact_base}/artifacts/result.txt"],
        **common,
    )
    assert receipt.authorizes_workspace_file_mutation(
        paths=[f"{artifact_base}/artifacts/result_1.txt"],
        **common,
    )
    assert receipt.authorizes_workspace_file_mutation(
        paths=[
            f"{artifact_base}/artifacts/"
            "result_01ARZ3NDEKTSV4RRFFQ69G5FAV.txt"
        ],
        **common,
    )
    assert not receipt.authorizes_workspace_file_mutation(
        paths=[f"{artifact_base}/artifacts/another-result.txt"],
        **common,
    )
    assert not receipt.authorizes_workspace_file_mutation(
        paths=[f"{artifact_base}/artifacts/result_not-a-ulid.txt"],
        **common,
    )
    assert not receipt.authorizes_workspace_file_mutation(
        paths=[f"{artifact_base}/artifacts/result_01.txt"],
        **common,
    )
    assert not receipt.authorizes_workspace_file_mutation(
        paths=[f"{artifact_base}/documents/result.txt"],
        **common,
    )


def test_runtime_classifier_detects_existing_task_artifact_for_retry(
    tmp_path,
    monkeypatch,
):
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    existing = (
        tmp_path
        / "ent_1"
        / "Workspaces"
        / "_by_id"
        / "folder_1"
        / "tasks"
        / "task_1"
        / "documents"
        / "cram-pack.md"
    )
    existing.parent.mkdir(parents=True)
    existing.write_text("old", encoding="utf-8")

    classification = classify_runtime_tool(
        "generate_file",
        {"kind": "document", "name": "cram-pack.md"},
        entity_id="ent_1",
        workspace_id="ws_1",
        task_id="task_1",
    )

    assert classification.action is not None
    assert classification.action.action_key == "workspace.file.modify"
    assert classification.action.operation == "modify"


@pytest.mark.asyncio
async def test_runtime_approval_classifies_existing_workspace_artifact_without_task_as_modify(
    monkeypatch,
    tmp_path,
):
    from unittest.mock import AsyncMock

    from packages.core import database
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalMiddleware,
        RuntimeApprovalRequest,
    )
    from packages.core.config import get_settings

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return "folder_1"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return Result()

    monkeypatch.setattr(database, "async_session", FakeSession)
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    existing = (
        tmp_path
        / "ent_1"
        / "Workspaces"
        / "_by_id"
        / "folder_1"
        / "documents"
        / "existing.md"
    )
    existing.parent.mkdir(parents=True)
    existing.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(
        "packages.core.ai.runtime.approval_service.guard_runtime_tool_action",
        AsyncMock(return_value=None),
    )

    decision = await RuntimeApprovalMiddleware().guard_request(
        RuntimeApprovalRequest(
            tool_name="generate_document_file",
            arguments={"name": "existing", "file_type": "md"},
            entity_id="ent_1",
            user_id="",
            workspace_id="ws_1",
            task_id=None,
        )
    )

    assert decision.classification.action is not None
    assert decision.classification.action.action_key == "workspace.file.modify"
    assert decision.classification.action.operation == "modify"


@pytest.mark.asyncio
async def test_runtime_approval_keeps_existing_entity_path_exact_outside_workspace_storage(
    monkeypatch,
    tmp_path,
):
    from unittest.mock import AsyncMock

    from packages.core import database
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalMiddleware,
        RuntimeApprovalRequest,
    )
    from packages.core.config import get_settings

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return "folder_1"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return Result()

    monkeypatch.setattr(database, "async_session", FakeSession)
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    existing = tmp_path / "ent_1" / "shared" / "notes.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(
        "packages.core.ai.runtime.approval_service.guard_runtime_tool_action",
        AsyncMock(return_value=None),
    )

    decision = await RuntimeApprovalMiddleware().guard_request(
        RuntimeApprovalRequest(
            tool_name="write_file",
            arguments={"path": "shared/notes.md", "content": "updated"},
            entity_id="ent_1",
            user_id="",
            workspace_id="ws_1",
        )
    )

    assert decision.request is not None
    assert decision.request.workspace_file_scope is None
    assert decision.classification.action is not None
    assert decision.classification.action.action_key == "workspace.file.modify"


@pytest.mark.asyncio
async def test_runtime_scope_fails_closed_for_existing_other_workspace_artifact(
    monkeypatch,
    tmp_path,
):
    from packages.core import database
    from packages.core.ai.runtime.approval_classifier import (
        resolve_runtime_workspace_file_scope,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        WorkspaceFileExistence,
    )
    from packages.core.config import get_settings

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return "folder_1"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return Result()

    monkeypatch.setattr(database, "async_session", FakeSession)
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    other_path = (
        "Workspaces/_by_id/folder_2/tasks/task_2/documents/notes.md"
    )
    existing = tmp_path / "ent_1" / other_path
    existing.parent.mkdir(parents=True)
    existing.write_text("other workspace", encoding="utf-8")

    scope = await resolve_runtime_workspace_file_scope(
        tool_name="write_file",
        arguments={"path": other_path, "content": "updated"},
        entity_id="ent_1",
        workspace_id="ws_1",
        task_id="task_1",
    )

    assert scope is not None
    assert scope.existence is WorkspaceFileExistence.UNKNOWN
    assert scope.artifact_base_dir is None


def test_runtime_workspace_commit_rejects_symlink_alias(monkeypatch, tmp_path):
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        RuntimeToolAuthorizationWriteError,
        runtime_begin_tool_authorization_scope,
        runtime_end_tool_authorization_scope,
    )
    from packages.core.ai.runtime.file_actions import runtime_write_entity_file_atomic
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    entity_root = tmp_path / "ent_1"
    other_target = (
        entity_root
        / "Workspaces"
        / "_by_id"
        / "folder_b"
        / "documents"
        / "notes.md"
    )
    other_target.parent.mkdir(parents=True)
    other_target.write_text("workspace B", encoding="utf-8")
    alias = entity_root / "shared" / "alias.md"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(other_target)

    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        kind="action",
        action_key="workspace.file.modify",
        risk_level="medium",
        title="Modify file",
        resource_kind="file",
        operation="write",
        resource_id="shared/alias.md",
    ))
    request = RuntimeApprovalRequest(
        tool_name="write_file",
        arguments={"path": "shared/alias.md", "content": "workspace A"},
        entity_id="ent_1",
        user_id="user_1",
        workspace_id="ws_a",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )
    assert receipt is not None
    scope = runtime_begin_tool_authorization_scope(
        receipt,
        arguments=request.arguments,
    )
    try:
        with pytest.raises(RuntimeToolAuthorizationWriteError):
            runtime_write_entity_file_atomic(
                "ent_1",
                "shared/alias.md",
                b"workspace A",
                allow_empty=False,
            )
    finally:
        runtime_end_tool_authorization_scope(scope)

    assert other_target.read_text(encoding="utf-8") == "workspace B"


def test_runtime_workspace_commit_rejects_swapped_symlink_alias(monkeypatch, tmp_path):
    from contextlib import contextmanager

    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        RuntimeToolAuthorizationWriteError,
        runtime_begin_tool_authorization_scope,
        runtime_end_tool_authorization_scope,
    )
    from packages.core.ai.runtime.file_actions import runtime_write_entity_file_atomic
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )
    from packages.core.config import get_settings
    from packages.core.services import entity_fs

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    entity_root = tmp_path / "ent_1"
    other_target = entity_root / "workspace-b" / "notes.md"
    other_target.parent.mkdir(parents=True)
    other_target.write_text("workspace B", encoding="utf-8")
    alias = entity_root / "shared" / "alias.md"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(other_target)

    @contextmanager
    def swap_alias_before_commit(*_args, **_kwargs):
        alias.unlink()
        yield

    monkeypatch.setattr(
        entity_fs,
        "_entity_editor_write_intent_lock",
        swap_alias_before_commit,
    )
    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        kind="action",
        action_key="workspace.file.modify",
        risk_level="medium",
        title="Modify file",
        resource_kind="file",
        operation="write",
        resource_id="shared/alias.md",
    ))
    request = RuntimeApprovalRequest(
        tool_name="write_file",
        arguments={"path": "shared/alias.md", "content": "workspace A"},
        entity_id="ent_1",
        user_id="user_1",
        workspace_id="ws_a",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )
    assert receipt is not None
    scope = runtime_begin_tool_authorization_scope(
        receipt,
        arguments=request.arguments,
    )
    try:
        with pytest.raises(RuntimeToolAuthorizationWriteError):
            runtime_write_entity_file_atomic(
                "ent_1",
                "shared/alias.md",
                b"workspace A",
                allow_empty=False,
            )
    finally:
        runtime_end_tool_authorization_scope(scope)

    assert other_target.read_text(encoding="utf-8") == "workspace B"


def test_runtime_scope_resolver_covers_all_exact_path_mutations():
    from packages.core.ai.runtime.authorization_receipts import (
        WorkspaceFileAuthorizationResourceFactory,
    )

    assert all(
        WorkspaceFileAuthorizationResourceFactory.supports_workspace_scope(tool_name)
        for tool_name in ("write_file", "edit_file", "delete_file")
    )


@pytest.mark.parametrize(
    ("tool_name", "action", "action_key", "expected_acl"),
    (
        ("write_file", "write", "workspace.file.modify", "write"),
        ("edit_file", "edit", "workspace.file.modify", "write"),
        ("delete_file", "delete", "workspace.file.delete", "delete"),
    ),
)
@pytest.mark.asyncio
async def test_unbound_workspace_file_receipt_revalidates_knowledge_acl(
    monkeypatch,
    tmp_path,
    tool_name,
    action,
    action_key,
    expected_acl,
):
    from packages.core import database
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        runtime_begin_tool_authorization_scope,
        runtime_end_tool_authorization_scope,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )
    from packages.core.config import get_settings
    from packages.core import permissions
    from packages.core.services import filesystem_access, runtime_authorization
    from packages.core.services.filesystem_access import FilesystemAccessDenied
    from packages.core.services.runtime_authorization import PermissionDecision

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _statement):
            return SimpleNamespace(id="user_1", entity_id="ent_1", role="member")

    async def allow_authorization(_db, **_kwargs):
        return PermissionDecision.allow("workspace_role")

    async def resolve_actor_role(_db, **_kwargs):
        return "member"

    acl_calls = []

    async def deny_write(_db, **kwargs):
        acl_calls.append(("write", kwargs["rel_path"]))
        raise FilesystemAccessDenied(403, "Edit access is required to write this file")

    async def deny_delete(_db, **kwargs):
        acl_calls.append(("delete", kwargs["rel_path"]))
        raise FilesystemAccessDenied(403, "Delete access is required to delete this path")

    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(
        permissions,
        "resolve_effective_user_role_name",
        resolve_actor_role,
    )
    monkeypatch.setattr(
        runtime_authorization,
        "authorize_runtime_action",
        allow_authorization,
    )
    monkeypatch.setattr(filesystem_access, "require_path_write_access", deny_write)
    monkeypatch.setattr(
        filesystem_access,
        "require_path_mutation_access",
        deny_delete,
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    target = tmp_path / "ent_1" / "shared" / "notes.md"
    target.parent.mkdir(parents=True)
    target.write_text("protected", encoding="utf-8")

    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        kind="action",
        action_key=action_key,
        risk_level="medium",
        title=f"{action} file",
        resource_kind="file",
        operation=action,
        resource_id="shared/notes.md",
    ))
    request = RuntimeApprovalRequest(
        tool_name=tool_name,
        arguments={"path": "shared/notes.md"},
        entity_id="ent_1",
        user_id="user_1",
        workspace_id="ws_1",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )
    assert receipt is not None
    scope = runtime_begin_tool_authorization_scope(
        receipt,
        arguments=request.arguments,
    )
    try:
        denied = await guard_ai_file_mutation(
            entity_id="ent_1",
            user_id="user_1",
            conversation_id=None,
            workspace_id="ws_1",
            task_id=None,
            runtime_envelope=SimpleNamespace(
                allowed_tool_names=(tool_name,),
                tool_bindings=(),
            ),
            tool_name=tool_name,
            action=action,
            paths=["shared/notes.md"],
        )
    finally:
        runtime_end_tool_authorization_scope(scope)

    payload = json.loads(denied or "{}")
    assert payload["mode"] == "entity_file_acl_denied"
    assert acl_calls == [(expected_acl, "shared/notes.md")]


@pytest.mark.asyncio
async def test_exact_workspace_receipt_does_not_bypass_existing_document_acl(
    monkeypatch,
    tmp_path,
):
    from packages.core import database
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        WorkspaceFileScopeBinding,
        runtime_begin_tool_authorization_scope,
        runtime_end_tool_authorization_scope,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
        RuntimeWorkspaceFileScope,
        WorkspaceFileExistence,
    )
    from packages.core.config import get_settings
    from packages.core import permissions
    from packages.core.services import filesystem_access, runtime_authorization
    from packages.core.services.filesystem_access import FilesystemAccessDenied
    from packages.core.services.runtime_authorization import PermissionDecision

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _statement):
            return SimpleNamespace(id="user_1", entity_id="ent_1", role="member")

    async def allow_authorization(_db, **_kwargs):
        return PermissionDecision.allow("workspace_role")

    async def resolve_actor_role(_db, **_kwargs):
        return "member"

    acl_calls = []

    async def deny_write(_db, **kwargs):
        acl_calls.append(kwargs["rel_path"])
        raise FilesystemAccessDenied(403, "Edit access is required")

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(
        permissions,
        "resolve_effective_user_role_name",
        resolve_actor_role,
    )
    monkeypatch.setattr(
        runtime_authorization,
        "authorize_runtime_action",
        allow_authorization,
    )
    monkeypatch.setattr(filesystem_access, "require_path_write_access", deny_write)

    artifact_base = "Workspaces/_by_id/folder_1/tasks/task_1"
    target_path = f"{artifact_base}/documents/notes.md"
    target = tmp_path / "ent_1" / target_path
    target.parent.mkdir(parents=True)
    target.write_text("protected", encoding="utf-8")
    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        kind="action",
        action_key="workspace.file.modify",
        risk_level="medium",
        title="update notes",
        resource_kind="file",
        operation="modify",
        resource_id="notes.md",
    ))
    request = RuntimeApprovalRequest(
        tool_name="write_file",
        arguments={"path": "notes.md", "content": "updated"},
        entity_id="ent_1",
        user_id="user_1",
        workspace_id="ws_1",
        task_id="task_1",
        workspace_file_scope=RuntimeWorkspaceFileScope(
            WorkspaceFileExistence.EXISTS,
            artifact_base,
        ),
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )
    assert receipt is not None
    assert receipt.workspace_scope_binding is WorkspaceFileScopeBinding.EXACT
    scope = runtime_begin_tool_authorization_scope(
        receipt,
        arguments=request.arguments,
    )
    try:
        mutation_denied = await guard_ai_file_mutation(
            entity_id="ent_1",
            user_id="user_1",
            conversation_id=None,
            workspace_id="ws_1",
            task_id="task_1",
            runtime_envelope=SimpleNamespace(allowed_tool_names=("write_file",)),
            tool_name="write_file",
            action="write",
            paths=[target_path],
        )
        resource_denied = await guard_ai_file_resource_access(
            entity_id="ent_1",
            user_id="user_1",
            conversation_id=None,
            workspace_id="ws_1",
            task_id="task_1",
            tool_name="write_file",
            action="write",
            paths=[target_path],
        )
    finally:
        runtime_end_tool_authorization_scope(scope)

    assert json.loads(mutation_denied or "{}")["mode"] == "entity_file_acl_denied"
    assert json.loads(resource_denied or "{}")["mode"] == "entity_file_acl_denied"
    assert acl_calls == [target_path, target_path]


@pytest.mark.parametrize(
    ("status", "deleted", "membership_status"),
    (
        ("inactive", False, None),
        ("active", True, None),
        ("active", False, "inactive"),
    ),
)
@pytest.mark.asyncio
async def test_inactive_or_deleted_file_actor_is_rejected(
    monkeypatch,
    tmp_path,
    status,
    deleted,
    membership_status,
):
    from datetime import datetime, timezone

    from packages.core import database
    from packages.core.config import get_settings
    from packages.core.models.user import User, UserMembership
    from packages.core.services import filesystem_access

    entity_id = f"ent_actor_{status}_{int(deleted)}"
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    actor = User(
        entity_id=entity_id,
        email=f"{entity_id}@test.example",
        password_hash="unused",
        role="owner",
        status=status,
    )
    if deleted:
        actor.deleted_at = datetime.now(timezone.utc)
    async with database.async_session() as db:
        db.add(actor)
        await db.flush()
        actor_id = actor.id
        if membership_status:
            db.add(UserMembership(
                user_id=actor_id,
                entity_id=entity_id,
                role="owner",
                status=membership_status,
                is_primary=True,
            ))
        await db.commit()

    async def unexpected_acl(*_args, **_kwargs):
        raise AssertionError("filesystem ACL must not receive an inactive actor")

    monkeypatch.setattr(
        filesystem_access,
        "require_path_write_access",
        unexpected_acl,
    )
    async with database.async_session() as db:
        denied = await _resource_file_acl_denial(
            db,
            runtime_envelope=None,
            entity_id=entity_id,
            user_id=actor_id,
            workspace_id=None,
            action="write",
            tool_name="write_file",
            paths=["notes.md"],
        )

    assert json.loads(denied or "{}")["mode"] == "entity_file_acl_denied"


@pytest.mark.asyncio
async def test_secondary_entity_workspace_member_can_create_new_artifact(
    db_session,
    monkeypatch,
    tmp_path,
):
    from packages.core import database
    from packages.core.ai.tools.bash_tool import _bash_resource_mutation_error
    from packages.core.config import get_settings
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import Entity, User, UserMembership
    from packages.core.models.workspace import Workspace, WorkspaceStaff
    from packages.core.services.document_access import unreadable_document_paths
    from packages.core.services.workspace_artifacts import (
        ensure_workspace_artifact_folder,
        workspace_artifact_storage_base,
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    primary_entity = Entity(
        id=generate_ulid(),
        name="Primary entity",
        slug=f"primary-{generate_ulid().lower()}",
    )
    target_entity = Entity(
        id=generate_ulid(),
        name="Target entity",
        slug=f"target-{generate_ulid().lower()}",
    )
    actor = User(
        entity_id=primary_entity.id,
        email=f"secondary-{generate_ulid()}@test.example",
        password_hash="unused",
        role="member",
        status="active",
    )
    workspace = Workspace(
        entity_id=target_entity.id,
        name="Member artifacts",
        settings={"access_mode": "members_only"},
        status="active",
    )
    db_session.add_all([primary_entity, target_entity, actor, workspace])
    await db_session.flush()
    db_session.add_all([
        UserMembership(
            user_id=actor.id,
            entity_id=target_entity.id,
            role="member",
            status="active",
        ),
        WorkspaceStaff(
            workspace_id=workspace.id,
            user_id=actor.id,
            role="contributor",
            status="active",
        ),
    ])
    artifact_folder = await ensure_workspace_artifact_folder(db_session, workspace)
    await db_session.flush()
    rel_path = (
        f"{workspace_artifact_storage_base(artifact_folder.id)}"
        "/tasks/task_1/artifacts/result.txt"
    )

    denied = await _resource_file_acl_denial(
        db_session,
        runtime_envelope=None,
        entity_id=target_entity.id,
        user_id=actor.id,
        workspace_id=workspace.id,
        action="save_file",
        tool_name="sandbox_save_result",
        paths=[rel_path],
    )

    class ExistingSessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_args):
            return None

    entity_root = tmp_path / target_entity.id
    entity_root.mkdir(parents=True)
    monkeypatch.setattr(database, "async_session", ExistingSessionContext)
    bash_denied = await _bash_resource_mutation_error(
        entity_id=target_entity.id,
        user_id=actor.id,
        workspace_id=workspace.id,
        cwd=str(entity_root),
        command=f"touch {rel_path}",
        paths=[rel_path],
    )
    fake_artifact_path = (
        f"shared/Workspaces/_by_id/{artifact_folder.id}/escaped.txt"
    )
    fake_denied = await _resource_file_acl_denial(
        db_session,
        runtime_envelope=None,
        entity_id=target_entity.id,
        user_id=actor.id,
        workspace_id=workspace.id,
        action="save_file",
        tool_name="sandbox_save_result",
        paths=[fake_artifact_path],
    )
    fake_bash_denied = await _bash_resource_mutation_error(
        entity_id=target_entity.id,
        user_id=actor.id,
        workspace_id=workspace.id,
        cwd=str(entity_root),
        command=f"touch {fake_artifact_path}",
        paths=[fake_artifact_path],
    )
    fake_read_blocked = await unreadable_document_paths(
        db_session,
        entity_id=target_entity.id,
        rel_paths=[fake_artifact_path],
        user_id=actor.id,
        workspace_id=workspace.id,
        actor_type="agent",
    )

    assert denied is None
    assert bash_denied is None
    assert json.loads(fake_denied or "{}")["mode"] == "entity_file_acl_denied"
    assert json.loads(fake_bash_denied or "{}")["error"] == "file_permission_denied"
    assert fake_artifact_path in fake_read_blocked
    assert actor.entity_id == primary_entity.id


@pytest.mark.asyncio
async def test_workspace_runtime_cannot_mutate_another_workspace_artifact(
    db_session,
    monkeypatch,
    tmp_path,
):
    from packages.core import database
    from packages.core.ai.tools.bash_tool import _bash_resource_mutation_error
    from packages.core.config import get_settings
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document
    from packages.core.models.user import Entity, User
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_artifacts import (
        ensure_workspace_artifact_folder,
        workspace_artifact_storage_base,
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    entity = Entity(
        id=generate_ulid(),
        name="Workspace isolation entity",
        slug=f"workspace-isolation-{generate_ulid().lower()}",
    )
    actor = User(
        entity_id=entity.id,
        email=f"workspace-isolation-{generate_ulid()}@test.example",
        password_hash="unused",
        role="owner",
        status="active",
    )
    workspace_a = Workspace(entity_id=entity.id, name="Workspace A", status="active")
    workspace_b = Workspace(entity_id=entity.id, name="Workspace B", status="active")
    db_session.add_all([entity, actor, workspace_a, workspace_b])
    await db_session.flush()
    await ensure_workspace_artifact_folder(db_session, workspace_a)
    folder_b = await ensure_workspace_artifact_folder(db_session, workspace_b)
    await db_session.flush()
    other_path = (
        f"{workspace_artifact_storage_base(folder_b.id)}"
        "/tasks/task_b/documents/notes.md"
    )
    db_session.add(Document(
        entity_id=entity.id,
        name="notes.md",
        fs_path=other_path,
        file_type="md",
        source="agent",
        visibility="entity",
        owner_id=actor.id,
        metadata_={"origin": {"workspace_id": workspace_b.id}},
    ))
    generic_other_path = "shared/workspace-b-notes.md"
    db_session.add(Document(
        entity_id=entity.id,
        name="workspace-b-notes.md",
        fs_path=generic_other_path,
        file_type="md",
        source="upload",
        visibility="entity",
        owner_id=actor.id,
        metadata_={"origin": {"workspace_id": workspace_b.id}},
    ))
    await db_session.flush()
    entity_root = tmp_path / entity.id
    target = entity_root / other_path
    target.parent.mkdir(parents=True)
    target.write_text("workspace B", encoding="utf-8")
    generic_target = entity_root / generic_other_path
    generic_target.parent.mkdir(parents=True)
    generic_target.write_text("workspace B generic path", encoding="utf-8")
    alias_path = "shared/workspace-b-alias.md"
    alias_target = entity_root / alias_path
    alias_target.symlink_to(target)

    denied = await _resource_file_acl_denial(
        db_session,
        runtime_envelope=None,
        entity_id=entity.id,
        user_id=actor.id,
        workspace_id=workspace_a.id,
        action="write",
        tool_name="write_file",
        paths=[other_path],
    )
    generic_denied = await _resource_file_acl_denial(
        db_session,
        runtime_envelope=None,
        entity_id=entity.id,
        user_id=actor.id,
        workspace_id=workspace_a.id,
        action="write",
        tool_name="write_file",
        paths=[generic_other_path],
    )
    alias_denied = await _resource_file_acl_denial(
        db_session,
        runtime_envelope=None,
        entity_id=entity.id,
        user_id=actor.id,
        workspace_id=workspace_a.id,
        action="write",
        tool_name="write_file",
        paths=[alias_path],
    )

    class ExistingSessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(database, "async_session", ExistingSessionContext)
    bash_denied = await _bash_resource_mutation_error(
        entity_id=entity.id,
        user_id=actor.id,
        workspace_id=workspace_a.id,
        cwd=str(entity_root),
        command=f"touch {other_path}",
        paths=[other_path],
    )

    assert json.loads(denied or "{}")["mode"] == "entity_file_acl_denied"
    assert json.loads(generic_denied or "{}")["mode"] == "entity_file_acl_denied"
    assert json.loads(alias_denied or "{}")["mode"] == "entity_file_acl_denied"
    assert json.loads(bash_denied or "{}")["error"] == "file_permission_denied"


@pytest.mark.asyncio
async def test_workspace_document_scope_queries_are_batched(db_session):
    from sqlalchemy import event

    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document, DocumentFolder
    from packages.core.models.user import Entity
    from packages.core.models.workspace import Workspace
    from packages.core.services.filesystem_access import (
        _require_documents_workspace_scope,
    )
    from packages.core.services.workspace_artifacts import (
        ensure_workspace_artifact_folder,
    )

    entity = Entity(
        id=generate_ulid(),
        name="Workspace scope batch entity",
        slug=f"workspace-scope-batch-{generate_ulid().lower()}",
    )
    workspace = Workspace(entity_id=entity.id, name="Workspace", status="active")
    db_session.add_all([entity, workspace])
    await db_session.flush()
    artifact_folder = await ensure_workspace_artifact_folder(db_session, workspace)
    child_folder = DocumentFolder(
        entity_id=entity.id,
        name="Documents",
        parent_id=artifact_folder.id,
        visibility="workspace",
    )
    db_session.add(child_folder)
    await db_session.flush()
    documents = [
        Document(
            entity_id=entity.id,
            name=f"document-{index}.md",
            fs_path=f"documents/document-{index}.md",
            file_type="md",
            source="agent",
            visibility="entity",
            folder_id=child_folder.id,
        )
        for index in range(50)
    ]
    db_session.add_all(documents)
    await db_session.flush()

    statements: list[str] = []
    sync_engine = db_session.bind.sync_engine

    def count_statement(_conn, _cursor, statement, _parameters, _context, _many):
        lowered = statement.lower()
        if any(table in lowered for table in (
            "document_groups",
            "document_folders",
            "workspaces",
        )):
            statements.append(statement)

    event.listen(sync_engine, "before_cursor_execute", count_statement)
    try:
        await _require_documents_workspace_scope(
            db_session,
            documents=documents,
            workspace_id=workspace.id,
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", count_statement)

    assert len(statements) <= 6


@pytest.mark.asyncio
async def test_workspace_document_scope_uses_missing_raw_folder_binding(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document
    from packages.core.models.user import Entity
    from packages.core.models.workspace import Workspace
    from packages.core.services.filesystem_access import (
        FilesystemAccessDenied,
        _require_documents_workspace_scope,
    )

    entity = Entity(
        id=generate_ulid(),
        name="Missing folder scope entity",
        slug=f"missing-folder-scope-{generate_ulid().lower()}",
    )
    workspace_a = Workspace(
        entity_id=entity.id,
        name="Workspace A",
        status="active",
        artifact_folder_id=generate_ulid(),
    )
    workspace_b = Workspace(
        entity_id=entity.id,
        name="Workspace B",
        status="active",
        artifact_folder_id=generate_ulid(),
    )
    document = Document(
        entity_id=entity.id,
        name="orphaned.md",
        fs_path="shared/orphaned.md",
        file_type="md",
        source="agent",
        visibility="entity",
        folder_id=workspace_b.artifact_folder_id,
    )
    db_session.add_all([entity, workspace_a, workspace_b, document])
    await db_session.flush()

    with pytest.raises(FilesystemAccessDenied):
        await _require_documents_workspace_scope(
            db_session,
            documents=[document],
            workspace_id=workspace_a.id,
        )


@pytest.mark.parametrize(
    ("tool_name", "action", "expected_acl"),
    (
        ("edit_file", "edit", "write"),
        ("delete_file", "delete", "delete"),
    ),
)
@pytest.mark.asyncio
async def test_legacy_file_mutation_revalidates_resource_acl_before_user_policy(
    monkeypatch,
    tmp_path,
    tool_name,
    action,
    expected_acl,
):
    from packages.core import database
    from packages.core.config import get_settings
    from packages.core import permissions
    from packages.core.services import filesystem_access, runtime_authorization
    from packages.core.services.filesystem_access import FilesystemAccessDenied
    from packages.core.services.runtime_authorization import PermissionDecision

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _statement):
            return SimpleNamespace(id="user_1", entity_id="ent_1", role="member")

    async def allow_authorization(_db, **_kwargs):
        return PermissionDecision.allow("workspace_role")

    async def resolve_actor_role(_db, **_kwargs):
        return "member"

    acl_calls = []

    async def deny_write(_db, **kwargs):
        acl_calls.append(("write", kwargs["rel_path"]))
        raise FilesystemAccessDenied(403, "Edit access is required")

    async def deny_delete(_db, **kwargs):
        acl_calls.append(("delete", kwargs["rel_path"]))
        raise FilesystemAccessDenied(403, "Delete access is required")

    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(
        permissions,
        "resolve_effective_user_role_name",
        resolve_actor_role,
    )
    monkeypatch.setattr(
        runtime_authorization,
        "authorize_runtime_action",
        allow_authorization,
    )
    monkeypatch.setattr(filesystem_access, "require_path_write_access", deny_write)
    monkeypatch.setattr(
        filesystem_access,
        "require_path_mutation_access",
        deny_delete,
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    target = tmp_path / "ent_1" / "shared" / "notes.md"
    target.parent.mkdir(parents=True)
    target.write_text("protected", encoding="utf-8")

    denied = await guard_ai_file_mutation(
        entity_id="ent_1",
        user_id="user_1",
        conversation_id=None,
        workspace_id="ws_1",
        runtime_envelope=SimpleNamespace(
            allowed_tool_names=(tool_name,),
            tool_bindings=(),
        ),
        tool_name=tool_name,
        action=action,
        paths=["shared/notes.md"],
    )

    payload = json.loads(denied or "{}")
    assert payload["mode"] == "entity_file_acl_denied"
    assert acl_calls == [(expected_acl, "shared/notes.md")]


def test_runtime_file_receipt_without_resource_never_authorizes_arbitrary_path():
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )

    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        kind="action",
        action_key="workspace.file.create",
        risk_level="medium",
        title="generate image",
        resource_kind="file",
        operation="create",
    ))
    request = RuntimeApprovalRequest(
        tool_name="generate_image",
        arguments={"prompt": "A diagram"},
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )

    assert receipt is not None
    assert not receipt.authorizes_workspace_file_mutation(
        capability_id="file.write",
        action_key="workspace.file.create",
        paths=["documents/unapproved.md"],
        entity_id="ent_1",
        user_id=None,
        workspace_id="ws_1",
        conversation_id=None,
        task_id="task_1",
    )


def test_runtime_bash_receipt_binds_all_move_operands():
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )

    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        kind="action",
        action_key="workspace.file.modify",
        risk_level="high",
        title="modify files in this workspace",
        resource_kind="file",
        operation="modify",
    ))
    request = RuntimeApprovalRequest(
        tool_name="bash",
        arguments={"command": "mv documents/old.md documents/new.md"},
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )

    assert receipt is not None
    assert receipt.resource_ids == (
        "documents/old.md",
        "documents/new.md",
    )


@pytest.mark.asyncio
async def test_runtime_authorization_lease_revokes_inherited_child_context():
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        runtime_begin_tool_authorization_scope,
        runtime_current_tool_authorization_receipt,
        runtime_end_tool_authorization_scope,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )

    classification = RuntimeToolClassification.action_call(
        RuntimeApprovalAction(
            kind="action",
            action_key="workspace.file.create",
            risk_level="medium",
            title="generate document",
            resource_kind="file",
            operation="create",
            resource_id="cram-pack.md",
        )
    )
    request = RuntimeApprovalRequest(
        tool_name="generate_file",
        arguments={"kind": "document", "name": "cram-pack.md"},
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )
    assert receipt is not None

    release_child = asyncio.Event()

    async def inherited_child():
        await release_child.wait()
        return runtime_current_tool_authorization_receipt()

    scope = runtime_begin_tool_authorization_scope(
        receipt,
        arguments=request.arguments,
    )
    child = asyncio.create_task(inherited_child())
    runtime_end_tool_authorization_scope(scope)
    release_child.set()

    assert await child is None
    assert runtime_current_tool_authorization_receipt() is None


@pytest.mark.asyncio
async def test_prepared_runtime_authorization_lease_is_single_use():
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationLeaseFactory,
        RuntimeToolAuthorizationReceiptFactory,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )
    from packages.core.ai.runtime.tool_execution import (
        RuntimePreparedToolExecution,
        runtime_execute_prepared_tool_handler,
    )

    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        kind="action",
        action_key="workspace.file.create",
        risk_level="medium",
        title="generate document",
        resource_kind="file",
        operation="create",
        resource_id="cram-pack.md",
    ))
    request = RuntimeApprovalRequest(
        tool_name="generate_file",
        arguments={"kind": "document", "name": "cram-pack.md"},
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )
    prepared = RuntimePreparedToolExecution(
        arguments=dict(request.arguments),
        authorization_lease=RuntimeToolAuthorizationLeaseFactory.create(receipt),
    )
    calls = 0

    def handler(**_kwargs):
        nonlocal calls
        calls += 1
        return "ok"

    first = await runtime_execute_prepared_tool_handler(
        tool_name="generate_file",
        handler=handler,
        prepared=prepared,
        entity_id="ent_1",
    )
    replay = await runtime_execute_prepared_tool_handler(
        tool_name="generate_file",
        handler=handler,
        prepared=prepared,
        entity_id="ent_1",
    )

    assert first == "ok"
    assert "already consumed" in replay
    assert calls == 1


@pytest.mark.asyncio
async def test_prepared_runtime_authorization_rejects_arguments_changed_after_approval():
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationLeaseFactory,
        RuntimeToolAuthorizationReceiptFactory,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )
    from packages.core.ai.runtime.tool_execution import (
        RuntimePreparedToolExecution,
        runtime_execute_prepared_tool_handler,
    )

    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        kind="action",
        action_key="workspace.file.create",
        risk_level="medium",
        title="generate document",
        resource_kind="file",
        operation="create",
        resource_id="cram-pack.md",
    ))
    request = RuntimeApprovalRequest(
        tool_name="generate_file",
        arguments={
            "kind": "document",
            "name": "cram-pack.md",
            "content": "approved content",
        },
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )
    prepared = RuntimePreparedToolExecution(
        arguments={**request.arguments, "content": "changed after approval"},
        authorization_lease=RuntimeToolAuthorizationLeaseFactory.create(receipt),
    )
    calls = 0

    def handler(**_kwargs):
        nonlocal calls
        calls += 1
        return "unexpected"

    result = await runtime_execute_prepared_tool_handler(
        tool_name="generate_file",
        handler=handler,
        prepared=prepared,
        entity_id="ent_1",
    )

    assert "arguments changed after approval" in result
    assert calls == 0


@pytest.mark.asyncio
async def test_runtime_handler_business_type_error_is_not_retried():
    from packages.core.ai.runtime.tool_execution import (
        RuntimePreparedToolExecution,
        runtime_execute_prepared_tool_handler,
    )

    calls = 0

    def handler(**_kwargs):
        nonlocal calls
        calls += 1
        raise TypeError("business validation failed")

    result = await runtime_execute_prepared_tool_handler(
        tool_name="side_effect",
        handler=handler,
        prepared=RuntimePreparedToolExecution(arguments={}),
        entity_id="ent_1",
        user_id="user_1",
    )

    assert "business validation failed" in result
    assert calls == 1


def test_runtime_create_receipt_revalidates_existence_at_atomic_commit(
    monkeypatch,
    tmp_path,
):
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        RuntimeToolAuthorizationWriteError,
        runtime_begin_tool_authorization_scope,
        runtime_end_tool_authorization_scope,
    )
    from packages.core.ai.runtime.file_actions import runtime_write_entity_file_atomic
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
        RuntimeWorkspaceFileScope,
        WorkspaceFileExistence,
    )
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        kind="action",
        action_key="workspace.file.create",
        risk_level="medium",
        title="generate document",
        resource_kind="file",
        operation="create",
        resource_id="cram-pack.md",
    ))
    request = RuntimeApprovalRequest(
        tool_name="generate_file",
        arguments={"kind": "document", "name": "cram-pack.md"},
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
        workspace_file_scope=RuntimeWorkspaceFileScope(
            existence=WorkspaceFileExistence.MISSING,
            artifact_base_dir="Workspaces/_by_id/folder_1/tasks/task_1",
        ),
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )
    assert receipt is not None
    scope = runtime_begin_tool_authorization_scope(
        receipt,
        arguments=request.arguments,
    )
    try:
        path = "Workspaces/_by_id/folder_1/tasks/task_1/documents/cram-pack.md"
        runtime_write_entity_file_atomic(
            "ent_1",
            path,
            b"first",
            expected_size=5,
        )
        with pytest.raises(RuntimeToolAuthorizationWriteError):
            runtime_write_entity_file_atomic(
                "ent_1",
                path,
                b"second",
                expected_size=6,
            )
    finally:
        runtime_end_tool_authorization_scope(scope)


def test_collision_safe_write_rejects_target_created_after_selection(
    monkeypatch,
    tmp_path,
):
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationWriteError,
    )
    from packages.core.ai.runtime.file_actions import runtime_write_entity_file_atomic
    from packages.core.config import get_settings
    from packages.core.services.generated_media_naming import collision_safe_artifact_path

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    entity_root = tmp_path / "ent_1"
    selected_path = collision_safe_artifact_path(
        str(entity_root),
        "artifacts/result.txt",
    )
    runtime_write_entity_file_atomic(
        "ent_1",
        selected_path,
        b"first",
        require_missing=True,
    )

    with pytest.raises(RuntimeToolAuthorizationWriteError):
        runtime_write_entity_file_atomic(
            "ent_1",
            selected_path,
            b"second",
            require_missing=True,
        )

    assert (entity_root / selected_path).read_bytes() == b"first"


def test_runtime_create_receipt_revalidates_atomic_file_copy(
    monkeypatch,
    tmp_path,
):
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        RuntimeToolAuthorizationWriteError,
        runtime_begin_tool_authorization_scope,
        runtime_end_tool_authorization_scope,
    )
    from packages.core.ai.runtime.file_actions import runtime_copy_entity_file_atomic
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
        RuntimeWorkspaceFileScope,
        WorkspaceFileExistence,
    )
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        kind="action",
        action_key="workspace.file.create",
        risk_level="medium",
        title="merge videos",
        resource_kind="file",
        operation="create",
    ))
    request = RuntimeApprovalRequest(
        tool_name="merge_videos",
        arguments={"output_name": "videos/final.mp4"},
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
        workspace_file_scope=RuntimeWorkspaceFileScope(
            WorkspaceFileExistence.MISSING,
            "Workspaces/_by_id/folder_1/tasks/task_1",
        ),
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )
    assert receipt is not None
    source = tmp_path / "rendered.mp4"
    source.write_bytes(b"video")
    target = "Workspaces/_by_id/folder_1/tasks/task_1/videos/final.mp4"
    scope = runtime_begin_tool_authorization_scope(
        receipt,
        arguments=request.arguments,
    )
    try:
        runtime_copy_entity_file_atomic("ent_1", target, str(source))
        with pytest.raises(RuntimeToolAuthorizationWriteError):
            runtime_copy_entity_file_atomic("ent_1", target, str(source))
    finally:
        runtime_end_tool_authorization_scope(scope)


def test_runtime_workspace_write_with_unresolved_physical_scope_fails_closed(
    monkeypatch,
    tmp_path,
):
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        RuntimeToolAuthorizationWriteError,
        runtime_begin_tool_authorization_scope,
        runtime_end_tool_authorization_scope,
    )
    from packages.core.ai.runtime.file_actions import runtime_write_entity_file_atomic
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
        RuntimeWorkspaceFileScope,
        WorkspaceFileExistence,
    )
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    classification = RuntimeToolClassification.action_call(RuntimeApprovalAction(
        kind="action",
        action_key="workspace.file.write",
        risk_level="medium",
        title="write workspace file",
        resource_kind="file",
        operation="modify",
        resource_id="cram-pack.md",
    ))
    request = RuntimeApprovalRequest(
        tool_name="write_file",
        arguments={"path": "cram-pack.md", "content": "content"},
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        workspace_file_scope=RuntimeWorkspaceFileScope(
            existence=WorkspaceFileExistence.UNKNOWN,
        ),
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )
    assert receipt is not None
    scope = runtime_begin_tool_authorization_scope(
        receipt,
        arguments=request.arguments,
    )
    try:
        with pytest.raises(RuntimeToolAuthorizationWriteError):
            runtime_write_entity_file_atomic(
                "ent_1",
                "Workspaces/_by_id/other/documents/cram-pack.md",
                b"content",
            )
    finally:
        runtime_end_tool_authorization_scope(scope)


@pytest.mark.asyncio
async def test_workspace_file_receipt_mismatch_fails_closed_before_legacy_policy(
    monkeypatch,
    tmp_path,
):
    from packages.core import database
    from packages.core.ai.runtime.approvals import (
        RuntimeApprovalAction,
        RuntimeApprovalDecision,
        RuntimeApprovalRequest,
    )
    from packages.core.ai.runtime.authorization_receipts import (
        RuntimeToolAuthorizationReceiptFactory,
        runtime_begin_tool_authorization_scope,
        runtime_end_tool_authorization_scope,
    )
    from packages.core.ai.runtime.tool_effect_classification import (
        RuntimeToolClassification,
    )
    from packages.core.config import get_settings
    from packages.core.services import ai_file_permissions, runtime_authorization
    from packages.core.services.runtime_authorization import PermissionDecision

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def allow_authorization(_db, **_kwargs):
        return PermissionDecision.allow("agent_tool_binding")

    async def unexpected_legacy_preference(*_args, **_kwargs):
        raise AssertionError("A mismatched receipt must fail closed")

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(database, "async_session", FakeSession)
    monkeypatch.setattr(
        runtime_authorization,
        "authorize_runtime_action",
        allow_authorization,
    )
    monkeypatch.setattr(
        ai_file_permissions,
        "load_user_file_permission_mode",
        unexpected_legacy_preference,
    )

    classification = RuntimeToolClassification.action_call(
        RuntimeApprovalAction(
            kind="action",
            action_key="workspace.file.create",
            risk_level="medium",
            title="generate document",
            resource_kind="file",
            operation="create",
            resource_id="cram-pack.md",
        )
    )
    request = RuntimeApprovalRequest(
        tool_name="generate_file",
        arguments={"kind": "document", "name": "cram-pack.md"},
        entity_id="ent_1",
        user_id="",
        workspace_id="ws_1",
        task_id="task_1",
    )
    receipt = RuntimeToolAuthorizationReceiptFactory.create(
        decision=RuntimeApprovalDecision.allow(classification),
        request=request,
    )
    assert receipt is not None

    existing_path = (
        tmp_path
        / "ent_1"
        / "Workspaces"
        / "_by_id"
        / "folder_1"
        / "tasks"
        / "task_1"
        / "documents"
        / "cram-pack.md"
    )
    existing_path.parent.mkdir(parents=True)
    existing_path.write_text("existing", encoding="utf-8")
    scope = runtime_begin_tool_authorization_scope(
        receipt,
        arguments=request.arguments,
    )
    try:
        modify_result = await guard_ai_file_mutation(
            entity_id="ent_1",
            user_id=None,
            conversation_id=None,
            workspace_id="ws_1",
            task_id="task_1",
            runtime_envelope=SimpleNamespace(allowed_tool_names=("generate_file",)),
            tool_name="generate_document_file",
            action="create_document",
            paths=[str(existing_path.relative_to(tmp_path / "ent_1"))],
        )
        wrong_resource_result = await guard_ai_file_mutation(
            entity_id="ent_1",
            user_id=None,
            conversation_id=None,
            workspace_id="ws_1",
            task_id="task_1",
            runtime_envelope=SimpleNamespace(allowed_tool_names=("generate_file",)),
            tool_name="generate_document_file",
            action="create_document",
            paths=[
                "Workspaces/_by_id/folder_1/tasks/task_1/documents/another-file.md"
            ],
        )
    finally:
        runtime_end_tool_authorization_scope(scope)

    assert json.loads(modify_result or "{}")["mode"] == (
        "authorization_receipt_mismatch"
    )
    assert json.loads(wrong_resource_result or "{}")["mode"] == (
        "authorization_receipt_mismatch"
    )


def test_bash_visible_mutation_paths_for_user_visible_changes():
    assert _visible_mutation_paths("rm docs/report.md") == ["docs/report.md"]
    assert _visible_mutation_paths("echo hello > docs/report.md") == ["docs/report.md"]
    assert _visible_mutation_paths("echo hello>docs/report.md") == ["docs/report.md"]
    assert _visible_mutation_paths("echo hello > docs/a.md && rm docs/b.md") == ["."]
    assert _visible_mutation_paths("rm docs/report.md && echo done") == ["."]
    assert _visible_mutation_paths("mv docs/a.md docs/b.md && echo done") == ["."]
    assert _visible_mutation_paths("mv --target-directory=private source.md") == ["."]
    assert _visible_mutation_paths("mv -tprivate source.md") == ["."]
    assert _visible_mutation_paths("printf hi | tee docs/out.md") == ["."]
    assert _visible_mutation_paths("ls && python3 scripts/build.py") == ["."]
    assert _visible_mutation_paths("echo safe & rm docs/private.md") == ["."]
    assert _visible_mutation_paths("xargs -0 rm") == ["."]
    assert _visible_mutation_paths("xargs rm") == ["."]
    assert _visible_mutation_paths("echo rm && echo done") == []
    assert _visible_mutation_paths("grep rm docs/report.md | wc -l") == []
    assert _visible_mutation_paths("cat docs/report.md") == []
    assert _may_create_files("ls && python3 scripts/build.py") is True


def test_bash_force_clobber_redirection_is_a_visible_mutation():
    from packages.core.ai.runtime.approval_classifier import (
        classify_runtime_tool_action,
    )

    command = "echo secret >| private.md"
    assert _validate_command(command) is None
    assert _visible_mutation_paths(command) == ["private.md"]
    assert _may_create_files(command) is True

    action = classify_runtime_tool_action(
        "bash",
        {"command": command},
        entity_id="entity_bash_clobber",
    )
    assert action is not None
    assert action.action_key == "workspace.file.modify"
    assert action.risk_level == "high"


def test_bash_mutations_use_their_real_authorization_action():
    assert _bash_mutation_action("rm docs/report.md") == "delete"
    assert _bash_mutation_action("echo hi > docs/a.md && rm docs/b.md") == "delete"
    assert _bash_mutation_action("mv docs/a.md docs/b.md") == "edit"
    assert _bash_mutation_action("mkdir docs/new") == "write"


def test_bash_projection_sync_stays_bounded_to_recoverable_paths():
    import inspect

    from packages.core.ai.tools import bash_tool

    source = inspect.getsource(bash_tool._bash_impl)
    assert "_repair_projection_paths" in source
    assert "reconcile_entity_filesystem" not in source


@pytest.mark.asyncio
async def test_bash_projection_failure_is_durable_and_repaired_before_next_command(
    monkeypatch, tmp_path,
):
    from packages.core.ai.tools import bash_tool
    from packages.core.config import get_settings
    from packages.core.services import ai_file_permissions, knowledge_sync
    from packages.core.services.knowledge_sync import KnowledgeSyncResult

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    root = tmp_path / "entity-recovery"
    root.mkdir()

    async def allow_mutation(**_kwargs):
        return None

    async def fake_execute(command: str, _timeout: int, cwd: str) -> str:
        if command.startswith("touch "):
            (root / "report.md").write_text("changed", encoding="utf-8")
        return json.dumps({"exit_code": 0, "stdout": "", "stderr": ""})

    async def no_structured_sync(*_args, **_kwargs):
        return None

    async def fail_projection(**_kwargs):
        raise RuntimeError("knowledge unavailable")

    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_mutation", allow_mutation)
    monkeypatch.setattr(bash_tool, "_execute_local", fake_execute)
    monkeypatch.setattr(bash_tool, "_sync_documents_after_bash", no_structured_sync)
    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", fail_projection)

    failed = json.loads(await _bash("entity-recovery", command="touch report.md"))
    marker = root / ".ai" / "bash-projection-recovery.json"
    assert failed["exit_code"] == 1
    assert failed["filesystem_changed"] is True
    assert failed["knowledge_sync_pending"] is True
    assert marker.is_file()
    marker_text = marker.read_text(encoding="utf-8")
    assert "report.md" in marker_text
    assert "touch" not in marker_text

    async def repair_projection(**_kwargs):
        return KnowledgeSyncResult(True, document_id="doc-repaired")

    monkeypatch.setattr(knowledge_sync, "sync_file_to_knowledge", repair_projection)
    repaired = json.loads(await _bash("entity-recovery", command="date"))
    assert repaired["exit_code"] == 0
    assert not marker.exists()


@pytest.mark.asyncio
async def test_bash_serializes_mutations_while_allowing_concurrent_shared_reads(
    monkeypatch,
    tmp_path,
):
    from packages.core.ai.tools import bash_tool
    from packages.core import database
    from packages.core.config import get_settings

    class FakeTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeSession(FakeTransaction):
        def begin(self):
            return FakeTransaction()

        async def execute(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(database, "async_session", FakeSession)

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    (tmp_path / "entity-serialized").mkdir()
    active = 0
    maximum_active = 0

    async def observed_impl(_entity_id: str, **_kwargs):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return json.dumps({"exit_code": 0})

    monkeypatch.setattr(bash_tool, "_bash_impl", observed_impl)
    await asyncio.gather(
        _bash("entity-serialized", command="ls"),
        _bash("entity-serialized", command="ls"),
    )
    assert maximum_active == 2

    maximum_active = 0
    await asyncio.gather(
        _bash("entity-serialized", command="touch first.md"),
        _bash("entity-serialized", command="touch second.md"),
    )
    assert maximum_active == 1


@pytest.mark.asyncio
async def test_bash_shared_read_blocks_a_concurrent_mutation(monkeypatch, tmp_path):
    from packages.core import database
    from packages.core.ai.tools import bash_tool
    from packages.core.config import get_settings

    class FakeTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeSession(FakeTransaction):
        def begin(self):
            return FakeTransaction()

        async def execute(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(database, "async_session", FakeSession)
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    (tmp_path / "entity-read-write").mkdir()
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    mutation_started = asyncio.Event()

    async def observed_impl(_entity_id: str, **kwargs):
        if kwargs["command"] == "ls":
            read_started.set()
            await release_read.wait()
        else:
            mutation_started.set()
        return json.dumps({"exit_code": 0})

    monkeypatch.setattr(bash_tool, "_bash_impl", observed_impl)
    reader = asyncio.create_task(_bash("entity-read-write", command="ls"))
    await asyncio.wait_for(read_started.wait(), timeout=1)
    writer = asyncio.create_task(_bash("entity-read-write", command="touch report.md"))
    await asyncio.sleep(0.05)
    assert not mutation_started.is_set()

    release_read.set()
    await asyncio.gather(reader, writer)
    assert mutation_started.is_set()


@pytest.mark.asyncio
async def test_cancelled_bash_mutation_finishes_before_releasing_its_lock(
    monkeypatch,
    tmp_path,
):
    from packages.core.ai.tools import bash_tool
    from packages.core import database
    from packages.core.config import get_settings
    from packages.core.services import ai_file_permissions
    from packages.core.services.entity_fs import entity_filesystem_mutation_lock

    class FakeTransaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeSession(FakeTransaction):
        def begin(self):
            return FakeTransaction()

        async def execute(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(database, "async_session", FakeSession)

    async def allow_mutation(**_kwargs):
        return None

    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_mutation", allow_mutation)

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    (tmp_path / "entity-cancelled").mkdir()
    started = asyncio.Event()
    execute_release = asyncio.Event()
    projection_started = asyncio.Event()
    projection_release = asyncio.Event()
    projection_finished = asyncio.Event()
    contender_entered = asyncio.Event()

    async def observed_execute(_command: str, _timeout: int, _cwd: str):
        started.set()
        await execute_release.wait()
        return json.dumps({"exit_code": 0, "stdout": "", "stderr": ""})

    async def observed_sync(*_args, **_kwargs):
        return None

    async def observed_projection(**_kwargs):
        projection_started.set()
        await projection_release.wait()
        projection_finished.set()
        return 1

    monkeypatch.setattr(bash_tool, "_execute_local", observed_execute)
    monkeypatch.setattr(bash_tool, "_sync_documents_after_bash", observed_sync)
    monkeypatch.setattr(bash_tool, "_repair_projection_paths", observed_projection)
    request = asyncio.create_task(
        _bash("entity-cancelled", command="touch report.md"),
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    request.cancel()
    await asyncio.sleep(0)

    assert not request.done()
    assert not projection_started.is_set()

    async def contend_for_lock():
        async with entity_filesystem_mutation_lock(
            str(tmp_path / "entity-cancelled"),
            timeout_seconds=1,
        ):
            contender_entered.set()

    contender = asyncio.create_task(contend_for_lock())
    execute_release.set()
    await asyncio.wait_for(projection_started.wait(), timeout=1)
    await asyncio.sleep(0.05)
    assert not contender_entered.is_set()

    projection_release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert projection_finished.is_set()
    await asyncio.wait_for(contender, timeout=1)
    assert contender_entered.is_set()


@pytest.mark.asyncio
async def test_bash_rejects_unbounded_mutations_even_without_user_context(tmp_path):
    from packages.core.ai.tools.bash_tool import _bash_resource_mutation_error

    denied = await _bash_resource_mutation_error(
        entity_id="entity",
        user_id=None,
        cwd=str(tmp_path),
        command="find . -delete",
        paths=["."],
    )

    assert denied is not None
    assert json.loads(denied)["error"] == "file_permission_denied"


@pytest.mark.asyncio
async def test_bash_move_rejects_implicit_destination_overwrite(tmp_path):
    from packages.core.ai.tools.bash_tool import _bash_resource_mutation_error

    source = tmp_path / "source.md"
    destination = tmp_path / "destination.md"
    source.write_text("new", encoding="utf-8")
    destination.write_text("existing", encoding="utf-8")

    for command in (
        "mv source.md destination.md",
        "mv -f source.md destination.md",
        "mv --no-target-directory source.md destination.md",
    ):
        denied = await _bash_resource_mutation_error(
            entity_id="entity",
            user_id=None,
            cwd=str(tmp_path),
            command=command,
            paths=["source.md", "destination.md"],
        )

        assert denied is not None
        payload = json.loads(denied)
        assert payload["error"] == "file_destination_exists"
        assert payload["operation"]["paths"] == ["destination.md"]
    assert source.read_text(encoding="utf-8") == "new"
    assert destination.read_text(encoding="utf-8") == "existing"


@pytest.mark.asyncio
async def test_bash_move_into_directory_allows_only_new_child(tmp_path):
    from packages.core.ai.tools.bash_tool import _bash_resource_mutation_error

    source = tmp_path / "source.md"
    destination = tmp_path / "archive"
    source.write_text("new", encoding="utf-8")
    destination.mkdir()

    allowed = await _bash_resource_mutation_error(
        entity_id="entity",
        user_id=None,
        cwd=str(tmp_path),
        command="mv source.md archive",
        paths=["source.md", "archive"],
    )
    assert allowed is None

    (destination / "source.md").write_text("existing", encoding="utf-8")
    denied = await _bash_resource_mutation_error(
        entity_id="entity",
        user_id=None,
        cwd=str(tmp_path),
        command="mv source.md archive",
        paths=["source.md", "archive"],
    )
    assert denied is not None
    assert json.loads(denied)["operation"]["paths"] == ["archive/source.md"]


def test_bash_validate_checks_each_shell_segment_and_nested_commands():
    assert _validate_command("rg old docs | head -20") is None
    assert "xargs" in (_validate_command("rg -l old docs | xargs rm") or "")
    assert "find" in (_validate_command("find docs -name '*.tmp' -exec rm {} \\;") or "")

    assert "git" in (_validate_command("ls; git status") or "")
    assert "xargs" in (_validate_command("echo sh | xargs sh") or "")
    assert "find" in (_validate_command("find docs -exec sh -c 'echo hi' \\;") or "")
    assert _validate_command("echo $(rm docs/private.md)") is not None
    assert _validate_command("echo `cat docs/private.md`") is not None
    assert _validate_command("rg secret docs | python3 -c 'print(1)'") is not None
    assert _validate_command("awk 'BEGIN { system(\"rm docs/private.md\") }'") is not None
    assert _validate_command("sed -n '1p' docs/private.md") is not None
    assert _validate_command("cp --target-directory=docs source.md") is not None
    assert _validate_command("mv -tprivate source.md") is not None
    assert _validate_command("sort --output=private.md public.md") is not None
    assert _validate_command("file -f public.list") is not None
    assert _validate_command("file --files-from=public.list") is not None
    assert _validate_command("file -m private.magic public.bin") is not None
    assert _validate_command("wc --files0-from=public.list") is not None
    assert _validate_command("diff --from-file=private.md public.md") is not None
    assert _validate_command("diff --to-file private.md public.md") is not None
    assert _validate_command("echo secret >& private.md") is not None
    assert _validate_command("echo secret 2>&1") is not None
    assert _validate_command("echo .ai/*") is not None
    assert _validate_command("which /etc/*") is not None
    assert _validate_command("echo ~") is not None
    assert _validate_command("chmod --reference=.ai/mode docs/report.md") is not None
    assert _validate_command("touch -r .ai/timestamp docs/report.md") is not None


def test_bash_search_read_paths_cover_option_patterns_and_implicit_rg_root():
    assert _visible_read_paths("rg TOP_SECRET") == ["."]
    assert _visible_read_paths("rg -e TOP_SECRET private.md") == ["private.md"]
    assert _visible_read_paths("rg -g '*.md' TOP_SECRET") == ["."]
    assert _visible_read_paths("rg -g '*.md' TOP_SECRET docs") == ["docs"]
    assert _visible_read_paths("grep --regexp=TOP_SECRET private.md") == [
        "private.md"
    ]
    assert _visible_read_paths("grep -f patterns.txt public.md") == [
        "patterns.txt",
        "public.md",
    ]
    assert _visible_read_paths("ls /etc") == ["/etc"]
    assert _visible_read_paths("rg --files /etc") == ["/etc"]


@pytest.mark.parametrize(
    "command",
    (
        "find /etc -maxdepth 1",
        "tree /etc",
        "ls -la .",
        "grep --directories=recurse TOP_SECRET .",
        "grep -d recurse TOP_SECRET .",
        "grep -drecurse TOP_SECRET .",
    ),
)
def test_bash_metadata_and_recursive_hidden_reads_fail_closed(command):
    with pytest.raises(ValueError, match="permission checking"):
        _visible_read_paths(command)


def test_bash_search_read_paths_reject_unsupported_options():
    with pytest.raises(ValueError, match="Unsupported rg option"):
        _visible_read_paths("rg --pre 'cat private.md' TOP_SECRET")
    with pytest.raises(ValueError, match="Unsupported rg option"):
        _visible_read_paths("rg --follow TOP_SECRET")
    with pytest.raises(ValueError, match="Unsupported grep option"):
        _visible_read_paths("grep -R TOP_SECRET")
    with pytest.raises(ValueError, match="Unsupported grep option"):
        _visible_read_paths("grep -r TOP_SECRET")
    with pytest.raises(ValueError, match="Unsupported rg option"):
        _visible_read_paths("rg --hidden TOP_SECRET")
    with pytest.raises(ValueError, match="hidden files"):
        _visible_read_paths("rg -uu TOP_SECRET")
    with pytest.raises(ValueError, match="hidden files"):
        _visible_read_paths("rg -u -u TOP_SECRET")


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_user_id", [None, "user_1"])
@pytest.mark.parametrize(
    "command",
    ["cat private.md", "rg TOP_SECRET", "grep --regexp=TOP_SECRET private.md"],
)
async def test_bash_read_acl_uses_runtime_actor_and_workspace(
    monkeypatch,
    tmp_path,
    runtime_user_id,
    command,
):
    from packages.core import database
    from packages.core.ai.tools import bash_tool
    from packages.core.config import get_settings
    from packages.core.services import document_access

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    entity_root = tmp_path / "entity"
    entity_root.mkdir()
    (entity_root / "private.md").write_text("TOP_SECRET", encoding="utf-8")
    calls = []

    class SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    async def unreadable(_db, **kwargs):
        calls.append(kwargs)
        return {"private.md"}

    async def forbidden_execute(*_args, **_kwargs):
        raise AssertionError("denied Bash reads must not execute")

    monkeypatch.setattr(database, "async_session", SessionContext)
    monkeypatch.setattr(document_access, "unreadable_document_paths", unreadable)
    monkeypatch.setattr(bash_tool, "_execute_local", forbidden_execute)

    denied = json.loads(await bash_tool._bash_impl(
        "entity",
        command=command,
        workspace_id="ws_1",
        _user_id_from_context=runtime_user_id,
        _projection_repaired=True,
    ))

    assert "Access denied" in denied["error"]
    assert calls == [{
        "entity_id": "entity",
        "rel_paths": ["private.md"],
        "user_id": runtime_user_id,
        "workspace_id": "ws_1",
        "actor_type": "agent",
    }]


@pytest.mark.asyncio
async def test_bash_code_execution_fails_closed_without_sandbox(monkeypatch):
    from packages.core.ai.tools import bash_tool

    async def forbidden_local(*_args, **_kwargs):
        raise AssertionError("executable commands must never run in the API container")

    monkeypatch.delenv("SANDBOX_SERVICE_URL", raising=False)
    monkeypatch.setattr(bash_tool, "_execute_local", forbidden_local)
    result = json.loads(await _bash("", command="python3 --version"))

    assert result["error"] == "sandbox_required"
    assert result["command_executed"] is False


@pytest.mark.asyncio
async def test_bash_local_file_commands_require_entity_filesystem(monkeypatch, tmp_path):
    from packages.core.ai.tools import bash_tool
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", False)
    host_file = tmp_path / "host-secret.md"
    host_file.write_text("HOST_ONLY_SECRET", encoding="utf-8")

    async def forbidden_local(*_args, **_kwargs):
        raise AssertionError("host filesystem commands must not execute")

    monkeypatch.setattr(bash_tool, "_execute_local", forbidden_local)
    denied = json.loads(await bash_tool._bash_impl(
        "entity",
        user_id="user_1",
        command=f"cat {host_file}",
        _projection_repaired=True,
    ))

    assert denied["error"] == "entity_filesystem_unavailable"
    assert denied["command_executed"] is False


@pytest.mark.asyncio
async def test_bash_internal_content_reads_fail_before_execution(monkeypatch, tmp_path):
    from packages.core.ai.tools import bash_tool
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    secret = (
        tmp_path
        / "entity"
        / ".ai"
        / "workspaces"
        / "ws_other"
        / "memory"
        / "facts"
        / "secret.md"
    )
    secret.parent.mkdir(parents=True)
    secret.write_text("OTHER_WORKSPACE_MEMORY", encoding="utf-8")

    async def forbidden_local(*_args, **_kwargs):
        raise AssertionError("internal paths must be denied before execution")

    monkeypatch.setattr(bash_tool, "_execute_local", forbidden_local)
    denied = json.loads(await bash_tool._bash_impl(
        "entity",
        user_id="user_1",
        workspace_id="ws_current",
        command="cat .ai/workspaces/ws_other/memory/facts/secret.md",
        _projection_repaired=True,
    ))

    assert denied["error"] == "file_permission_denied"
    assert "internal" in denied["reason"]


def test_bash_content_reads_cannot_escape_entity_root(tmp_path):
    root = tmp_path / "entity"
    root.mkdir()

    with pytest.raises(ValueError, match="inside the entity filesystem"):
        _expanded_read_paths(
            entity_root=str(root),
            cwd=str(root),
            paths=["/etc/passwd"],
        )
    for path in (
        "~/outside-secret",
        "{private,public}.md",
    ):
        with pytest.raises(ValueError, match="shell expansion"):
            _expanded_read_paths(
                entity_root=str(root),
                cwd=str(root),
                paths=[path],
            )

    internal = root / ".ai" / "memory.md"
    internal.parent.mkdir()
    internal.write_text("private", encoding="utf-8")
    with pytest.raises(ValueError, match="internal"):
        _expanded_read_paths(
            entity_root=str(root),
            cwd=str(root),
            paths=[".ai/memory.md"],
        )
    assert _visible_read_paths("echo ignored < /etc/passwd") == ["/etc/passwd"]


@pytest.mark.asyncio
async def test_bash_rejects_mutation_globs_before_acl_lookup(tmp_path):
    from packages.core.ai.tools.bash_tool import _bash_resource_mutation_error

    denied = await _bash_resource_mutation_error(
        entity_id="entity",
        user_id=None,
        cwd=str(tmp_path),
        command="rm docs/*.md",
        paths=["docs/*.md"],
    )

    assert denied is not None
    assert json.loads(denied)["error"] == "file_permission_denied"

    escaped = await _bash_resource_mutation_error(
        entity_id="entity",
        user_id=None,
        cwd=str(tmp_path),
        command="cp /etc/passwd stolen.txt",
        paths=["/etc/passwd", "stolen.txt"],
    )
    assert escaped is not None
    assert json.loads(escaped)["error"] == "file_permission_denied"

    brace_expanded = await _bash_resource_mutation_error(
        entity_id="entity",
        user_id=None,
        cwd=str(tmp_path),
        command="rm {public,private}.md",
        paths=["{public,private}.md"],
    )
    assert brace_expanded is not None
    assert json.loads(brace_expanded)["error"] == "file_permission_denied"


@pytest.mark.asyncio
async def test_bash_move_recovery_preserves_document_identity_and_security(
    db_session,
    tmp_path,
    monkeypatch,
):
    from sqlalchemy import select

    from packages.core.ai.tools.bash_tool import (
        _repair_pending_projection,
        _write_projection_recovery_marker,
    )
    from packages.core.models.document import Document, VectorStatus
    from packages.core.config import get_settings

    entity_id = "entity_bash_move_recovery"
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    root = tmp_path / entity_id
    root.mkdir()
    (root / "renamed.md").write_text("classified", encoding="utf-8")
    document = Document(
        id="doc_bash_move_recovery",
        entity_id=entity_id,
        name="source.md",
        fs_path="source.md",
        file_type="md",
        mime_type="text/markdown",
        source="upload",
        vector_status=VectorStatus.READY,
        visibility="private",
        classification="restricted",
        owner_id="user_bash_recovery",
    )
    db_session.add(document)
    await db_session.commit()
    _write_projection_recovery_marker(
        str(root),
        entity_id=entity_id,
        user_id="user_bash_recovery",
        paths=["source.md", "renamed.md"],
        operations=[{
            "id": "move-op-1",
            "kind": "move",
            "source": "source.md",
            "destination": "renamed.md",
            "source_is_directory": False,
            "requires_projection": True,
        }],
    )

    await _repair_pending_projection(entity_id, str(root))

    recovered = await db_session.scalar(
        select(Document).where(Document.id == "doc_bash_move_recovery")
    )
    await db_session.refresh(recovered)
    assert recovered.fs_path == "renamed.md"
    assert recovered.visibility == "private"
    assert recovered.classification == "restricted"
    assert recovered.owner_id == "user_bash_recovery"


@pytest.mark.asyncio
async def test_bash_copy_recovery_is_idempotent_and_preserves_security(
    db_session,
    tmp_path,
    monkeypatch,
):
    from sqlalchemy import select

    from packages.core.ai.tools.bash_tool import (
        _repair_pending_projection,
        _write_projection_recovery_marker,
    )
    from packages.core.models.document import Document, VectorStatus
    from packages.core.config import get_settings

    entity_id = "entity_bash_copy_recovery"
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    root = tmp_path / entity_id
    root.mkdir()
    (root / "source.md").write_text("classified", encoding="utf-8")
    (root / "copy.md").write_text("classified", encoding="utf-8")
    source = Document(
        id="doc_bash_copy_source",
        entity_id=entity_id,
        name="source.md",
        fs_path="source.md",
        file_type="md",
        mime_type="text/markdown",
        source="upload",
        vector_status=VectorStatus.READY,
        visibility="private",
        classification="restricted",
        owner_id="user_bash_copy",
    )
    db_session.add(source)
    await db_session.commit()
    operation = {
        "id": "copy-op-1",
        "kind": "copy",
        "source": "source.md",
        "destination": "copy.md",
        "source_is_directory": False,
        "requires_projection": True,
    }

    for _attempt in range(2):
        _write_projection_recovery_marker(
            str(root),
            entity_id=entity_id,
            user_id="user_bash_copy",
            paths=["source.md", "copy.md"],
            operations=[operation],
        )
        await _repair_pending_projection(entity_id, str(root))

    copies = list((await db_session.scalars(
        select(Document).where(
            Document.entity_id == entity_id,
            Document.fs_path == "copy.md",
            Document.is_trashed.is_(False),
        )
    )).all())
    assert len(copies) == 1
    copied = copies[0]
    assert copied.visibility == "private"
    assert copied.classification == "restricted"
    assert copied.owner_id == "user_bash_copy"
    assert copied.metadata_["filesystem_copy_operation_id"] == "copy-op-1"
    assert copied.metadata_["filesystem_copy_source_document_id"] == source.id


def test_post_bash_sync_splits_simple_move_chain():
    assert _split_simple_shell_commands(
        "mkdir -p 'Workspaces/Foo/documents' && mv 'a b.md' 'Workspaces/Foo/documents/'"
    ) == [
        "mkdir -p Workspaces/Foo/documents",
        "mv 'a b.md' Workspaces/Foo/documents/",
    ]


def test_post_bash_sync_skips_pipelines():
    assert _split_simple_shell_commands("printf hi | tee docs/out.md") == []


def test_document_file_state_missing_and_available_markers():
    from packages.core.services.document_file_state import (
        mark_document_file_available,
        mark_document_file_missing,
    )

    doc = SimpleNamespace(
        metadata_={},
        vector_status="ready",
        is_trashed=False,
        trashed_at=None,
    )

    assert mark_document_file_missing(doc, source="move") is True
    assert doc.is_trashed is False
    assert doc.trashed_at is None
    assert doc.vector_status == "failed"
    assert doc.metadata_["file_integrity"]["status"] == "missing"
    assert doc.metadata_["file_integrity"]["recoverable"] is False

    assert mark_document_file_missing(doc, source="delete", trash=True) is True
    assert doc.is_trashed is True
    assert doc.trashed_at is not None

    assert mark_document_file_available(doc, source="filesystem") is True
    assert doc.vector_status == "pending"
    assert doc.metadata_["file_integrity"]["status"] == "ok"
    assert "recoverable" not in doc.metadata_["file_integrity"]


def _resolver_for(root):
    def _resolve(path: str) -> str | None:
        full = os.path.realpath(os.path.join(root, path))
        real_root = os.path.realpath(root)
        if os.path.commonpath([real_root, full]) != real_root:
            return None
        return os.path.relpath(full, real_root)

    return _resolve


@pytest.mark.asyncio
async def test_post_bash_mv_directory_rename_uses_destination_path(monkeypatch, tmp_path):
    from packages.core.services import knowledge_sync

    root = tmp_path / "entity"
    (root / "NewFolder").mkdir(parents=True)
    calls: list[tuple[str, str]] = []

    async def fake_move_path(_entity_id: str, old_rel: str, new_rel: str) -> bool:
        calls.append((old_rel, new_rel))
        return True

    monkeypatch.setattr(knowledge_sync, "move_path", fake_move_path)

    await _handle_mv_cp(
        "mv",
        ["OldFolder", "NewFolder"],
        "ent_1",
        str(root),
        _resolver_for(str(root)),
        str(root),
    )

    assert calls == [("OldFolder", "NewFolder")]


@pytest.mark.asyncio
async def test_post_bash_mv_into_directory_uses_child_path(monkeypatch, tmp_path):
    from packages.core.services import knowledge_sync

    root = tmp_path / "entity"
    (root / "Target" / "brief.md").parent.mkdir(parents=True)
    (root / "Target" / "brief.md").write_text("moved", encoding="utf-8")
    calls: list[tuple[str, str]] = []

    async def fake_move_path(_entity_id: str, old_rel: str, new_rel: str) -> bool:
        calls.append((old_rel, new_rel))
        return True

    monkeypatch.setattr(knowledge_sync, "move_path", fake_move_path)

    await _handle_mv_cp(
        "mv",
        ["brief.md", "Target"],
        "ent_1",
        str(root),
        _resolver_for(str(root)),
        str(root),
    )

    assert calls == [("brief.md", "Target/brief.md")]


def test_mutating_file_tool_schemas_accept_approval_token():
    schemas = [
        BASH_SCHEMA,
        WRITE_FILE_SCHEMA,
        EDIT_FILE_SCHEMA,
        DELETE_FILE_SCHEMA,
        GENERATE_DOCUMENT_FILE_SCHEMA,
        GENERATE_FILE_SCHEMA,
        _SANDBOX_SAVE_RESULT_SCHEMA,
        SAVE_SANDBOX_FILE_SCHEMA,
        MANOR_SCHEMA,
    ]
    assert all("approval_token" in _props(schema) for schema in schemas)


@pytest.mark.asyncio
async def test_cancel_pending_file_approvals_marks_saved_hitl_card_resolved(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message
    from packages.core.services.ai_file_permissions import cancel_pending_file_approvals

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Direct chat",
            meta={
                "file_approvals": {
                    "hitl_file": {
                        "status": "pending",
                        "requested_by_user_id": user_id,
                    }
                }
            },
        )
    )
    msg = Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="assistant",
        content="",
        message_kind="hitl_request",
        meta={"hitl_requests": [{"id": "hitl_file", "type": "approval"}]},
    )
    db_session.add(msg)
    await db_session.flush()

    cancelled = await cancel_pending_file_approvals(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_ids=["hitl_file"],
    )

    assert cancelled == 1
    await db_session.refresh(msg)
    assert msg.meta["hitl_requests"][0]["resolved"] is True
    assert msg.meta["hitl_requests"][0]["resolution"] == "cancelled"
