"""An authorized bash write into a Workspace commits what its receipt approved.

Local eval 2026-10-17 (Video Studio picture book, task in a Workspace on
``automatic`` approval): the agent downloaded a public font to ``/tmp`` and
saved it into the project with ``mkdir -p .../fonts && cp ... .../fonts/`` and
then with ``manor push``. Both calls were approved, then every save failed
with ``authorization_receipt_mismatch``: the receipt predicted from the
command text named other paths/kinds than the files the command produced.

These tests drive the real Runtime boundary (classifier -> receipt -> bash
handler -> working copy save-back -> inner guard -> atomic commit) with a
local sandbox that runs the commands for real.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from packages.core.ai.runtime import (
    AIRuntimeRequest,
    ChatSurface,
    RuntimeResolver,
    runtime_execute_registered_tool,
)
from packages.core.ai.tools import bash_tool
from packages.core.governance.approval_policy_service import (
    account_defaults,
    initialize_personal_approval,
    initialize_workspace_approval,
)
from packages.core.models.base import generate_ulid
from packages.core.models.task import Conversation, Task
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from tests.bash_sandbox_fakes import install

# A few bytes of a TrueType header plus bytes that are not UTF-8.
FONT_BYTES = b"\x00\x01\x00\x00\x00\x0f\x00\x80\x00\x03\x00pFFTM\xff\xfe\xc3\x28" * 64
LICENSE_TEXT = "SIL Open Font License 1.1\n"


@dataclass
class _Context:
    entity_id: str
    user_id: str
    workspace_id: str
    task_id: str
    conversation_id: str
    root: Path
    storage_root: str
    task_base: str

    def envelope(self):
        from packages.core.constants.agents import MANOR_AGENT_ID

        return RuntimeResolver().resolve_trace_envelope(
            AIRuntimeRequest(
                surface=ChatSurface.SCHEDULED_AGENT_RUN,
                entity_id=self.entity_id,
                user_id=self.user_id,
                agent_id=MANOR_AGENT_ID,
                workspace_id=self.workspace_id,
                task_id=self.task_id,
                conversation_id=None,
            ),
            tool_schemas=[{"type": "function", "function": {"name": "bash"}}],
            allowed_tool_names={"bash"},
        )

    async def bash(self, command: str) -> dict:
        from packages.core.constants.agents import MANOR_AGENT_ID

        result = await runtime_execute_registered_tool(
            tool_name="bash",
            arguments={
                "command": command,
                "_runtime_run_id_from_context": generate_ulid(),
                "_runtime_tool_call_id_from_context": f"call_{generate_ulid()}",
            },
            handler_resolver=lambda _name: bash_tool._bash,
            entity_id=self.entity_id,
            user_id=self.user_id,
            agent_id=MANOR_AGENT_ID,
            workspace_id=self.workspace_id,
            conversation_id=None,
            task_id=self.task_id,
            runtime_envelope=self.envelope(),
        )
        return json.loads(result)


@pytest.fixture
def bash_fs(monkeypatch, tmp_path):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path / "fs"))
    monkeypatch.setattr(settings, "APP_URL", "https://manor.example.test")
    monkeypatch.setattr(settings, "SANDBOX_CLI_API_URL", "")
    (tmp_path / "fs").mkdir()
    # The sandbox image's `manor` CLI: `manor push` writes into the working copy.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = Path(__file__).resolve().parents[1] / "packages/core/services/sandbox_cli/manor_cli.py"
    shim = bin_dir / "manor"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{cli}" "$@"\n', encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return tmp_path / "fs"


async def _automatic_workspace_task(db, fs_root: Path) -> _Context:
    from packages.core.ai.runtime.approval_classifier import resolve_runtime_workspace_file_scope
    from packages.core.services.entity_fs import provision_entity_filesystem
    from packages.core.services.generated_media_naming import workspace_storage_root
    from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder

    entity_id, user_id, conversation_id, task_id = (generate_ulid() for _ in range(4))
    defaults = account_defaults()
    defaults["policy"]["mode"] = "automatic"
    db.add(Entity(id=entity_id, name="Bash receipt test", settings={"approval_defaults": defaults}))
    db.add(User(id=user_id, entity_id=entity_id, email=f"{user_id}@test.com",
                password_hash="unused", role="owner", status="active"))
    db.add(UserMembership(user_id=user_id, entity_id=entity_id, role="owner", status="active", is_primary=True))
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id, name="Video Studio", operating_model={})
    db.add(workspace)
    await db.flush()
    await ensure_workspace_artifact_folder(db, workspace)
    await initialize_workspace_approval(db, workspace)
    db.add(Conversation(id=conversation_id, entity_id=entity_id, user_id=user_id, workspace_id=workspace.id,
                        scope="workspace_main", status="active"))
    await db.flush()
    db.add(Task(id=task_id, entity_id=entity_id, workspace_id=workspace.id, conversation_id=conversation_id,
                creator_id=user_id, title="Picture book", status="in_progress", task_type="agent", details={}))
    await initialize_personal_approval(db, user_id=user_id, entity_id=entity_id)
    await db.commit()
    provision_entity_filesystem(entity_id)
    scope = await resolve_runtime_workspace_file_scope(
        tool_name="bash", arguments={"command": "touch probe.txt"},
        entity_id=entity_id, workspace_id=workspace.id, task_id=task_id,
    )
    assert scope is not None and scope.artifact_base_dir
    storage_root = workspace_storage_root(scope.artifact_base_dir)
    # The project already exists, as in the incident (its CANVAS was there).
    project = fs_root / entity_id / storage_root / "Videos" / "picture-book"
    project.mkdir(parents=True)
    (project / "CANVAS.md").write_text("# Picture book\n", encoding="utf-8")
    return _Context(
        entity_id=entity_id, user_id=user_id, workspace_id=workspace.id, task_id=task_id,
        conversation_id=conversation_id, root=fs_root / entity_id,
        storage_root=storage_root,
        task_base=scope.artifact_base_dir,
    )


def _assert_saved(result: dict) -> None:
    detail = json.dumps(result, ensure_ascii=False)
    assert result.get("exit_code") == 0 and not result.get("error"), detail
    assert result.get("file_changes_saved") is not False, detail


def _scratch(sandbox, name: str, data: bytes) -> str:
    path = sandbox.scratch / "picture-font" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return f"/tmp/picture-font/{name}"


# Every shape writes the font (binary) and its licence (text). ``{project}`` is
# a Workspace project folder that exists; ``{task}`` is this Task's artifact
# folder, where relative outputs are scoped like every Workspace write of a Task.
@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "written"), [
    pytest.param(
        "test -s {font} && mkdir -p '{project}/fonts' && cp {font} {license} '{project}/fonts/'",
        ["{project}/fonts/LongCang-Regular.ttf", "{project}/fonts/OFL.txt"],
        id="incident-round-7-mkdir-then-cp-into-new-folder",
    ),
    pytest.param(
        "manor push {font} '{project}/fonts/LongCang-Regular.ttf' --json && "
        "manor push {license} '{project}/fonts/OFL.txt' --json",
        ["{project}/fonts/LongCang-Regular.ttf", "{project}/fonts/OFL.txt"],
        id="incident-round-9-manor-push-chain-into-new-folder",
    ),
    pytest.param(
        "mkdir -p '{project}/fonts' && cp {font} '{project}/fonts' && cp {license} '{project}/fonts'",
        ["{project}/fonts/LongCang-Regular.ttf", "{project}/fonts/OFL.txt"],
        id="cp-into-new-folder-without-slash",
    ),
    pytest.param(
        "manor push {font} '{project}/LongCang-Regular.ttf'",
        ["{project}/LongCang-Regular.ttf"],
        id="single-manor-push",
    ),
    pytest.param(
        "cp {font} '{project}/CANVAS.md' && cp {license} '{project}/OFL.txt'",
        ["{project}/CANVAS.md", "{project}/OFL.txt"],
        id="replace-existing-and-create-new",
    ),
    pytest.param(
        "cp {font} '{task}/LongCang-Regular.ttf' && cat {license} > '{task}/OFL.txt'",
        ["{task}/LongCang-Regular.ttf", "{task}/OFL.txt"],
        id="task-artifact-folder",
    ),
    pytest.param(
        "cp {font} LongCang-Regular.ttf",
        ["{task}/artifacts/LongCang-Regular.ttf"],
        id="relative-output-scoped-into-the-task",
    ),
])
async def test_authorized_bash_writes_commit_in_automatic_mode(
    db_session, bash_fs, monkeypatch, tmp_path, command, written,
):
    context = await _automatic_workspace_task(db_session, bash_fs)
    sandbox = install(monkeypatch, tmp_path)
    names = {
        "font": _scratch(sandbox, "LongCang-Regular.ttf", FONT_BYTES),
        "license": _scratch(sandbox, "OFL.txt", LICENSE_TEXT.encode()),
        "project": f"{context.storage_root}/Videos/picture-book",
        "task": context.task_base,
    }

    _assert_saved(await context.bash(command.format(**names)))

    for path in (template.format(**names) for template in written):
        expected = LICENSE_TEXT.encode() if path.endswith("OFL.txt") else FONT_BYTES
        assert (context.root / path).read_bytes() == expected, path


@pytest.mark.asyncio
async def test_writes_the_command_did_not_name_are_refused_as_its_own_not_a_platform_error(
    db_session, bash_fs, monkeypatch, tmp_path,
):
    context = await _automatic_workspace_task(db_session, bash_fs)
    sandbox = install(monkeypatch, tmp_path)
    project = f"{context.storage_root}/Videos/picture-book"
    _scratch(sandbox, "OFL.txt", LICENSE_TEXT.encode())

    # The approved call names the folder `out`; the program writes elsewhere.
    result = await context.bash(
        f"mkdir -p '{project}/out' && sh -c \"cat /tmp/picture-font/OFL.txt > '{project}/other.txt'\""
    )

    assert result["error"] == "bash_unapproved_changes", result
    assert result["file_changes_saved"] is False
    assert f"{project}/other.txt" in result["operation"]["paths"]
    assert not (context.root / project / "other.txt").exists()
