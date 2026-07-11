import os
from types import SimpleNamespace

import pytest

from packages.core.ai.tools.bash_tool import (
    BASH_SCHEMA,
    _handle_mv_cp,
    _may_create_files,
    _split_simple_shell_commands,
    _validate_command,
    _visible_mutation_paths,
)
from packages.core.ai.tools.document_tools import GENERATE_DOCUMENT_FILE_SCHEMA
from packages.core.ai.tools.file_tools import DELETE_FILE_SCHEMA, EDIT_FILE_SCHEMA, WRITE_FILE_SCHEMA
from packages.core.ai.tools.generate_file_tool import GENERATE_FILE_SCHEMA
from packages.core.ai.tools.manor_tool import MANOR_SCHEMA
from packages.core.ai.tools.sandbox_tools import _SANDBOX_SAVE_RESULT_SCHEMA
from packages.core.ai.tools.sandbox_file_tools import SAVE_SANDBOX_FILE_SCHEMA
from packages.core.services.ai_file_permissions import (
    _hitl_payload,
    _approval_prompt,
    classify_file_approval_reply,
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


def test_bash_visible_mutation_paths_for_user_visible_changes():
    assert _visible_mutation_paths("rm docs/report.md") == ["docs/report.md"]
    assert _visible_mutation_paths("echo hello > docs/report.md") == ["docs/report.md"]
    assert _visible_mutation_paths("echo hello>docs/report.md") == ["docs/report.md"]
    assert _visible_mutation_paths("echo hello > docs/a.md && rm docs/b.md") == ["."]
    assert _visible_mutation_paths("rm docs/report.md && echo done") == ["."]
    assert _visible_mutation_paths("mv docs/a.md docs/b.md && echo done") == ["."]
    assert _visible_mutation_paths("printf hi | tee docs/out.md") == ["."]
    assert _visible_mutation_paths("ls && python3 scripts/build.py") == ["."]
    assert _visible_mutation_paths("xargs -0 rm") == ["."]
    assert _visible_mutation_paths("xargs rm") == ["."]
    assert _visible_mutation_paths("echo rm && echo done") == []
    assert _visible_mutation_paths("grep rm docs/report.md | wc -l") == []
    assert _visible_mutation_paths("cat docs/report.md") == []
    assert _may_create_files("ls && python3 scripts/build.py") is True


def test_bash_validate_checks_each_shell_segment_and_nested_commands():
    assert _validate_command("rg old docs | head -20") is None
    assert _validate_command("rg -l old docs | xargs rm") is None
    assert _validate_command("find docs -name '*.tmp' -exec rm {} \\;") is None

    assert "git" in (_validate_command("ls; git status") or "")
    assert "sh" in (_validate_command("echo sh | xargs sh") or "")
    assert "sh" in (_validate_command("find docs -exec sh -c 'echo hi' \\;") or "")


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
    assert doc.is_trashed is True
    assert doc.trashed_at is not None
    assert doc.vector_status == "failed"
    assert doc.metadata_["file_integrity"]["status"] == "missing"
    assert doc.metadata_["file_integrity"]["recoverable"] is False

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
