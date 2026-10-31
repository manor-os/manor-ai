"""bash runs in the caller's sandbox, with entity files staged in and saved back.

Driven against ``bash_sandbox_fakes.LocalSandbox`` (real ``sh``, real mirror
scripts) and the real Knowledge/ACL/projection services. The compatibility
matrix covers every command family the old local bash ran
(``LOCAL_ONLY_COMMANDS``) plus the executables it sent to a mountless
sandbox, and the mixed pipelines it used to refuse.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

import packages.core.database as db_module
from packages.core.ai.tools import bash_tool
from packages.core.config import get_settings
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document
from packages.core.models.user import Entity, User, UserMembership
from packages.core.services.auth_service import hash_password
from packages.core.services.document_service import create_document
from tests.bash_sandbox_fakes import install

SECRET = "BOARD-COMP-SECRET quarterly comp"


@pytest.fixture
def fs_root(tmp_path):
    settings = get_settings()
    old = settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT, settings.APP_URL, settings.SANDBOX_CLI_API_URL
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path / "fs")
    settings.APP_URL = "https://manor.example.test"
    settings.SANDBOX_CLI_API_URL = ""
    (tmp_path / "fs").mkdir()
    try:
        yield tmp_path / "fs"
    finally:
        (settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT, settings.APP_URL,
         settings.SANDBOX_CLI_API_URL) = old


async def _user(entity_id: str, name: str, role: str) -> str:
    async with db_module.async_session() as db:
        user = User(
            entity_id=entity_id, email=f"{name}-{entity_id.lower()}@test.com", display_name=name,
            password_hash=hash_password("pass123"), role=role, status="active",
        )
        db.add(user)
        await db.flush()
        db.add(UserMembership(user_id=user.id, entity_id=entity_id, role=role, status="active", is_primary=True))
        await db.commit()
        return user.id


@pytest.fixture
async def world(client, fs_root, tmp_path, monkeypatch):
    from packages.core.services.knowledge_sync import ensure_folder_path

    entity_id = generate_ulid()
    async with db_module.async_session() as db:
        db.add(Entity(id=entity_id, name="Bash Sandbox Test"))
        await db.commit()
    owner = await _user(entity_id, "bash_owner", "owner")
    member = await _user(entity_id, "bash_member", "member")
    root = fs_root / entity_id
    (root / "Knowledge").mkdir(parents=True)
    (root / "Knowledge" / "board-comp.md").write_text(SECRET, encoding="utf-8")
    (root / "Knowledge" / "handbook.md").write_text("team handbook\nsecond line\n", encoding="utf-8")
    (root / "Knowledge" / "data.csv").write_text("x,y\n1,2\n3,4\n", encoding="utf-8")
    await ensure_folder_path(entity_id, "Knowledge")
    async with db_module.async_session() as db:
        for name, visibility in (("board-comp.md", "private"), ("handbook.md", "entity"), ("data.csv", "entity")):
            await create_document(
                db, entity_id, name=name, fs_path=f"Knowledge/{name}", file_type=name.rsplit(".", 1)[1],
                source="upload", visibility=visibility, owner_id=owner,
            )
        await db.commit()

    guard_calls: list[dict] = []
    guard_result: dict = {"value": None}

    async def guard(**kwargs):
        guard_calls.append(kwargs)
        value = guard_result["value"]
        return value(kwargs) if callable(value) else value

    from packages.core.services import ai_file_permissions

    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_mutation", guard)
    sandbox = install(monkeypatch, tmp_path)
    return {
        "entity_id": entity_id, "owner": owner, "member": member, "root": root,
        "sandbox": sandbox, "guard_calls": guard_calls, "guard_result": guard_result,
    }


async def run(world, command: str, *, user: str = "member", **kwargs) -> dict:
    user_id = world[user] if user else None
    payload = await bash_tool._bash(
        world["entity_id"],
        command=command,
        _user_id_from_context=user_id,
        _agent_id_from_context="agent-1",
        conversation_id="conv-1",
        **kwargs,
    )
    return json.loads(payload)


async def _doc(entity_id: str, path: str) -> Document | None:
    async with db_module.async_session() as db:
        return await db.scalar(select(Document).where(
            Document.entity_id == entity_id, Document.fs_path == path, Document.is_trashed.is_(False),
        ))


# ── reads ──


@pytest.mark.asyncio
async def test_read_commands_see_only_readable_entity_files(world):
    listing = await run(world, "ls Knowledge")
    assert listing["exit_code"] == 0, listing
    assert listing["stdout"].split() == ["data.csv", "handbook.md"], "a member never sees a private doc"

    for command, expected in (
        ("cat Knowledge/handbook.md", "team handbook\nsecond line\n"),
        ("head -n 1 Knowledge/handbook.md", "team handbook\n"),
        ("tail -n 1 Knowledge/handbook.md", "second line\n"),
        ("wc -l Knowledge/data.csv", "3 Knowledge/data.csv"),
        ("grep -n second Knowledge/handbook.md", "2:second line\n"),
        ("sort Knowledge/data.csv | head -n 1", "1,2\n"),
        ("cut -d, -f1 Knowledge/data.csv | uniq | tr '\\n' ' '", "x 1 3 "),
        ("diff Knowledge/data.csv Knowledge/data.csv && echo same", "same\n"),
        ("file Knowledge/data.csv", "Knowledge/data.csv:"),
        ("find Knowledge -name '*.csv'", "Knowledge/data.csv\n"),
        ("echo hi; date >/dev/null; pwd", "hi\n"),
    ):
        result = await run(world, command)
        assert result["exit_code"] == 0, (command, result)
        assert expected in result["stdout"], (command, result)


@pytest.mark.asyncio
async def test_naming_a_private_document_refuses_the_command(world):
    denied = await run(world, "cat Knowledge/board-comp.md | grep SECRET")
    assert denied["command_executed"] is False
    assert "Access denied" in denied["error"]
    assert SECRET not in json.dumps(denied)
    assert world["sandbox"].user_commands() == [], "the command never ran"

    searched = await run(world, "grep -r BOARD Knowledge; echo done")
    assert SECRET not in json.dumps(searched)
    assert not (world["sandbox"].mirror / "Knowledge" / "board-comp.md").exists()

    owner = await run(world, "cat Knowledge/board-comp.md", user="owner")
    assert SECRET in owner["stdout"]


@pytest.mark.asyncio
async def test_staged_files_are_cached_and_refreshed_when_they_change(world):
    await run(world, "cat Knowledge/handbook.md")
    stages = [c for c in world["sandbox"].calls if "manor-stage" in c["command"]]
    await run(world, "cat Knowledge/handbook.md")
    assert [c for c in world["sandbox"].calls if "manor-stage" in c["command"]] == stages, "no re-copy"
    (world["root"] / "Knowledge" / "handbook.md").write_text("edited in Manor\n", encoding="utf-8")
    refreshed = await run(world, "cat Knowledge/handbook.md")
    assert refreshed["stdout"] == "edited in Manor\n"


@pytest.mark.asyncio
async def test_staging_is_bounded_with_a_pointer_to_the_manor_cli(world, monkeypatch):
    monkeypatch.setattr(get_settings(), "BASH_SANDBOX_STAGE_MAX_BYTES", 10)
    refused = await run(world, "cat Knowledge/handbook.md")
    assert refused["error"] == "bash_staging_limit"
    assert "manor grep" in refused["hint"]
    assert refused["command_executed"] is False


@pytest.mark.asyncio
async def test_mixed_pipelines_and_executables_now_work(world):
    piped = await run(world, "grep -n , Knowledge/data.csv | python3 -c 'import sys; print(len(sys.stdin.read().splitlines()))'")
    assert piped["exit_code"] == 0, piped
    assert piped["stdout"].strip() == "3"
    node = await run(world, "python3 --version")
    assert node["exit_code"] == 0


@pytest.mark.asyncio
async def test_cli_token_reaches_only_the_user_command_and_is_revoked(world):
    from packages.core.services.sandbox_cli.tokens import resolve_sandbox_cli_token

    await run(world, "echo $MANOR_API_URL")
    env = world["sandbox"].envs[-1]
    assert env["MANOR_API_URL"] == "https://manor.example.test"
    assert env["MANOR_CLI_TOKEN"].startswith("mcli_")
    assert await resolve_sandbox_cli_token(env["MANOR_CLI_TOKEN"]) is None, "revoked when the command returns"
    helper_envs = [c["env"] for c in world["sandbox"].calls if "MANOR_CLI_TOKEN" not in c["env"]]
    assert helper_envs, "staging/snapshot helpers never get the token"


# ── writes ──


@pytest.mark.asyncio
async def test_script_output_is_saved_back_through_the_guard(world):
    result = await run(
        world,
        "python3 -c \"import csv; rows=list(csv.reader(open('Knowledge/data.csv'))); "
        "open('Knowledge/summary.txt','w').write(str(len(rows)))\" Knowledge/data.csv",
        user="owner",
    )
    assert result["exit_code"] == 0, result
    assert (world["root"] / "Knowledge" / "summary.txt").exists(), (result, world["sandbox"].calls[-3:])
    assert (world["root"] / "Knowledge" / "summary.txt").read_text() == "3"
    call = world["guard_calls"][-1]
    assert call["paths"] == ["Knowledge/summary.txt"]
    assert call["tool_name"] == "bash" and str(call["action"].value) == "write"
    assert result["file_changes"] == {"created": ["Knowledge/summary.txt"]}
    assert await _doc(world["entity_id"], "Knowledge/summary.txt") is not None

    # The filesystem ACL still applies per caller: a member may not write here.
    denied = await run(world, "python3 -c \"open('Knowledge/member.txt','w').write('m')\"")
    assert denied["error"] == "file_permission_denied", denied
    assert denied["file_changes_saved"] is False
    assert not (world["root"] / "Knowledge" / "member.txt").exists()
    assert not (world["sandbox"].mirror / "Knowledge" / "member.txt").exists()


@pytest.mark.asyncio
async def test_move_is_checked_before_it_runs_and_keeps_document_identity(world):
    before = await _doc(world["entity_id"], "Knowledge/handbook.md")
    moved = await run(world, "mv Knowledge/handbook.md Knowledge/guide.md", user="owner")
    assert moved["exit_code"] == 0, moved
    call = world["guard_calls"][0]
    assert call["paths"] == ["Knowledge/handbook.md", "Knowledge/guide.md"]
    assert str(call["action"].value) == "edit"
    assert len(world["guard_calls"]) == 1, "a pre-checked command is not asked twice"
    assert not (world["root"] / "Knowledge" / "handbook.md").exists()
    after = await _doc(world["entity_id"], "Knowledge/guide.md")
    assert after is not None and after.id == before.id


@pytest.mark.asyncio
async def test_approval_required_before_a_named_change_runs_nothing(world):
    world["guard_result"]["value"] = json.dumps({"__hitl__": True, "error": "approval_required", "approval_token": "t1"})
    blocked = await run(world, "rm Knowledge/data.csv", user="owner")
    assert blocked["error"] == "approval_required"
    assert world["sandbox"].user_commands() == []
    assert (world["root"] / "Knowledge" / "data.csv").exists()


@pytest.mark.asyncio
async def test_approval_for_unnamed_changes_reverts_then_applies_on_rerun(world):
    command = "python3 -c \"open('Knowledge/report.md','w').write('# Report')\""

    def needs_token(kwargs):
        if kwargs.get("approval_token") == "t2":
            return None
        return json.dumps({"__hitl__": True, "error": "approval_required", "approval_token": "t2"})

    world["guard_result"]["value"] = needs_token
    first = await run(world, command, user="owner")
    assert first["__hitl__"] is True and first["command_executed"] is True
    assert first["file_changes_saved"] is False
    assert not (world["root"] / "Knowledge" / "report.md").exists()
    assert not (world["sandbox"].mirror / "Knowledge" / "report.md").exists(), "the working copy is reverted"

    second = await run(world, command, user="owner", approval_token="t2")
    assert second["exit_code"] == 0, second
    assert (world["root"] / "Knowledge" / "report.md").read_text() == "# Report"
    assert world["guard_calls"][0]["paths"] == world["guard_calls"][1]["paths"] == ["Knowledge/report.md"]
    assert world["guard_calls"][1]["approval_token"] == "t2"


@pytest.mark.asyncio
async def test_delete_redirect_mkdir_touch_cp_are_saved(world):
    for command, check in (
        ("echo hello > Knowledge/new.md", lambda r: (r / "Knowledge" / "new.md").read_text() == "hello\n"),
        ("echo more >> Knowledge/new.md", lambda r: (r / "Knowledge" / "new.md").read_text() == "hello\nmore\n"),
        ("mkdir -p Knowledge/Archive", lambda r: (r / "Knowledge" / "Archive").is_dir()),
        ("touch Knowledge/Archive/empty.md", lambda r: (r / "Knowledge" / "Archive" / "empty.md").is_file()),
        ("cp Knowledge/data.csv Knowledge/Archive/data.csv", lambda r: (r / "Knowledge" / "Archive" / "data.csv").read_text().startswith("x,y")),
        ("rm Knowledge/new.md", lambda r: not (r / "Knowledge" / "new.md").exists()),
    ):
        result = await run(world, command, user="owner")
        assert result["exit_code"] == 0, (command, result, [c["command"][-300:] for c in world["sandbox"].calls])
        assert check(world["root"]), (command, result)
    assert await _doc(world["entity_id"], "Knowledge/new.md") is None
    assert await _doc(world["entity_id"], "Knowledge/Archive/data.csv") is not None
    actions = [str(call["action"].value) for call in world["guard_calls"]]
    assert actions[-1] == "delete"


@pytest.mark.asyncio
async def test_workspace_runs_save_new_files_only_where_they_were_placed_explicitly(world):
    """In a Workspace run any program learns its output folder; a new file it
    leaves at the entity root is refused and reverted (never saved or
    indexed), while $MANOR_OUTPUT_DIR outputs, new files in existing folders
    and edits of existing files are saved. Without a Workspace see
    test_concurrent_commands_of_two_conversations_... (root files saved)."""
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_artifacts import ensure_workspace_artifact_folder

    async with db_module.async_session() as db:
        workspace = Workspace(entity_id=world["entity_id"], name="Studio", status="active")
        db.add(workspace)
        await db.flush()
        await ensure_workspace_artifact_folder(db, workspace)
        await db.commit()
        workspace_id, folder_id = workspace.id, workspace.artifact_folder_id
    output_dir = f"Workspaces/_by_id/{folder_id}"
    (world["root"] / "notes.md").write_text("notes", encoding="utf-8")

    stray = await run(
        world, "python3 -c \"open('summary.txt','w').write('x')\"", user="owner", workspace_id=workspace_id,
    )
    assert stray["error"] == "bash_output_outside_workspace", stray
    assert stray["operation"]["paths"] == ["summary.txt"] and stray["file_changes_saved"] is False
    assert not (world["root"] / "summary.txt").exists()
    assert not (world["sandbox"].mirror / "summary.txt").exists(), "the working copy is reverted"
    assert await _doc(world["entity_id"], "summary.txt") is None

    saved = await run(
        world, "python3 -c \"import os; open(os.environ['MANOR_OUTPUT_DIR'] + '/summary.txt','w').write('x')\"",
        user="owner", workspace_id=workspace_id,
    )
    assert saved["file_changes"] == {"created": [f"{output_dir}/summary.txt"]}, saved
    assert world["sandbox"].user_commands()[-1]["env"]["MANOR_OUTPUT_DIR"].endswith(f"/{output_dir}")
    document = await _doc(world["entity_id"], f"{output_dir}/summary.txt")
    assert document is not None and document.folder_id == folder_id, "indexed in the Workspace's folder"

    explicit = await run(
        world, "python3 -c \"open('notes.md','a').write('+'); open('Knowledge/w.md','w').write('w')\" notes.md",
        user="owner", workspace_id=workspace_id,
    )
    assert explicit["file_changes"] == {"created": ["Knowledge/w.md"], "modified": ["notes.md"]}, explicit
    assert (world["root"] / "notes.md").read_text() == "notes+"


@pytest.mark.asyncio
async def test_listed_but_unstaged_files_cannot_be_edited_blindly(world):
    await run(world, "ls Knowledge", user="owner")
    sneaky = await run(
        world,
        "python3 -c \"import os; os.chmod('Knowledge/data.csv', 0o644); open('Knowledge/data.csv','a').write('x')\"",
        user="owner",
    )
    assert sneaky["error"] == "bash_unstaged_content"
    assert (world["root"] / "Knowledge" / "data.csv").read_text() == "x,y\n1,2\n3,4\n"


@pytest.mark.asyncio
async def test_edits_of_a_file_changed_in_manor_meanwhile_are_not_saved(world):
    server_file = world["root"] / "Knowledge" / "handbook.md"
    # The local stand-in can reach the entity filesystem: simulate a user
    # saving the same file in Manor while the command runs.
    result = await run(
        world,
        f"echo agent-edit >> Knowledge/handbook.md && printf 'user edit\\n' > {server_file}",
        user="owner",
    )
    assert result["error"] == "file_changed_since_staged", result
    assert server_file.read_text() == "user edit\n", "the user's edit wins"
    assert result["file_changes_saved"] is False
    again = await run(world, "cat Knowledge/handbook.md", user="owner")
    assert again["stdout"] == "user edit\n", "the working copy is reset to the entity version"


@pytest.mark.asyncio
async def test_queued_manor_operations_are_applied_through_the_guard(world, monkeypatch):
    from packages.core.services.sandbox_cli import tokens

    async def drained(exec_id):
        return [{"op": "mv", "paths": ["Knowledge/data.csv", "Knowledge/table.csv"], "recursive": False}]

    monkeypatch.setattr(tokens, "drain_sandbox_cli_operations", drained)
    before = await _doc(world["entity_id"], "Knowledge/data.csv")
    result = await run(world, "echo queued", user="owner")
    assert result["exit_code"] == 0, result
    assert (world["root"] / "Knowledge" / "table.csv").exists(), result
    assert world["guard_calls"][-1]["paths"] == ["Knowledge/data.csv", "Knowledge/table.csv"]
    after = await _doc(world["entity_id"], "Knowledge/table.csv")
    assert after is not None and after.id == before.id


# ── runtime contract ──


@pytest.mark.asyncio
async def test_timeout_and_output_truncation_fields(world):
    slow = await run(world, "sleep 5", timeout=1)
    assert slow["timed_out"] is True and slow["exit_code"] == -9
    big = await run(world, "python3 -c \"import sys; sys.stdout.write('x' * 70000)\"")
    assert big["stdout_truncated"] is True and big["stdout_chars"] == 70000
    assert len(big["stdout"]) == 65536


@pytest.mark.asyncio
async def test_user_less_runs_follow_the_read_file_rules(world):
    from packages.core.ai.tools.file_tools import _read_file

    direct = json.loads(await _read_file(
        world["entity_id"], path="Knowledge/handbook.md", _user_id_from_context=None,
        _agent_id_from_context="agent-1",
    ))
    via_bash = await run(world, "cat Knowledge/handbook.md", user=None)
    assert bool(direct.get("error")) == bool(via_bash.get("error")), (direct, via_bash)


@pytest.mark.asyncio
async def test_external_principals_never_reach_the_sandbox(world):
    from types import SimpleNamespace

    envelope = SimpleNamespace(surface="public_customer_chat", profile=None)
    refused = await run(world, "ls", _runtime_envelope_from_context=envelope)
    assert refused["error"] == "bash_unavailable"
    assert world["sandbox"].calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("health", ["august", "september"])
async def test_an_outdated_runner_is_named_before_anything_is_staged_or_run(world, health):
    # An older runner has no work areas, side channel or per-command env: the
    # staging would fail obscurely (or look like a vanished sandbox).
    from tests.sandbox_exec_fakes import AUGUST_RUNNER_HEALTH, SEPTEMBER_RUNNER_HEALTH

    world["sandbox"].health = dict(AUGUST_RUNNER_HEALTH if health == "august" else SEPTEMBER_RUNNER_HEALTH)

    refused = await run(world, "cat Knowledge/handbook.md")

    assert refused["error"] == "sandbox_runner_outdated", refused
    assert refused["command_executed"] is False
    assert "outdated and must be upgraded" in refused["reason"]
    assert world["sandbox"].calls == [], "nothing staged, nothing started"


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_user_id", [None, "user_1"])
@pytest.mark.parametrize("command", ["cat private.md", "grep --regexp=TOP_SECRET private.md", "rg TOP_SECRET"])
async def test_staging_acl_uses_the_runtime_actor_and_workspace(monkeypatch, tmp_path, runtime_user_id, command):
    """Ported from the local-bash read ACL test: same actor, Workspace and agent actor type."""
    from types import SimpleNamespace

    from packages.core import database
    from packages.core.services import document_access

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path / "fs"))
    entity_root = tmp_path / "fs" / "entity"
    entity_root.mkdir(parents=True)
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

    monkeypatch.setattr(database, "async_session", SessionContext)
    monkeypatch.setattr(document_access, "unreadable_document_paths", unreadable)
    sandbox = install(monkeypatch, tmp_path)

    result = json.loads(await bash_tool._bash(
        "entity", command=command, workspace_id="ws_1", _user_id_from_context=runtime_user_id,
    ))

    expected = {"rel_paths": ["private.md"], "user_id": runtime_user_id, "workspace_id": "ws_1", "actor_type": "agent"}
    assert expected.items() <= calls[0].items(), calls
    assert not (sandbox.mirror / "private.md").exists()
    if command.startswith("rg"):
        # A whole-tree search runs; the unreadable file was never copied in.
        assert "TOP_SECRET" not in json.dumps(result)
    else:
        assert "Access denied" in result["error"]
        assert sandbox.user_commands() == []


# ── one shared sandbox, several conversations (sandbox-user-conversation) ──


async def _run_in(world, command: str, *, conversation: str, agent: str = "agent-1") -> dict:
    return json.loads(await bash_tool._bash(
        world["entity_id"],
        command=command,
        _user_id_from_context=world["member"],
        _agent_id_from_context=agent,
        conversation_id=conversation,
    ))


@pytest.mark.asyncio
async def test_concurrent_commands_of_two_conversations_are_credited_only_with_their_own_files(world, monkeypatch):
    """All of a user's conversations share ONE sandbox, and their bash commands
    run in it at the same time. Each works on its own conversation's working
    copy (its agent's directory of that work area), so a file another
    conversation's command created meanwhile is never saved as -- or
    attributed to -- this command's change."""

    import asyncio

    written = asyncio.Event()
    release = asyncio.Event()
    slow_command = "echo a > notes-a.md"
    sandbox_run = world["sandbox"]._run

    async def hold_after_write(command, **kwargs):
        result = await sandbox_run(command, **kwargs)
        if slow_command in command:
            written.set()
            await release.wait()
        return result

    monkeypatch.setattr(world["sandbox"], "_run", hold_after_write)
    slow = asyncio.create_task(_run_in(world, slow_command, conversation="conv-a"))
    try:
        await asyncio.wait_for(written.wait(), timeout=30)
        # Keep the real file present and A's runner execution pending throughout
        # B's save-back, regardless of machine speed or database latency.
        assert (world["sandbox"].mirror_for("conv-a") / "notes-a.md").read_text() == "a\n"
        fast = await asyncio.wait_for(_run_in(world, "echo b > notes-b.md", conversation="conv-b"), timeout=30)
        assert not slow.done(), "conv-a's command is still running"
    finally:
        release.set()
        finished = await asyncio.wait_for(slow, timeout=30)

    assert fast["exit_code"] == 0 and finished["exit_code"] == 0, (fast, finished)
    assert fast["file_changes"]["created"] == ["notes-b.md"], fast
    assert finished["file_changes"]["created"] == ["notes-a.md"], finished
    assert (world["root"] / "notes-a.md").read_text() == "a\n"
    assert (world["root"] / "notes-b.md").read_text() == "b\n"
    saved = {}
    for call in world["guard_calls"]:
        saved.setdefault(call["conversation_id"], set()).update(call["paths"])
    assert saved == {"conv-a": {"notes-a.md"}, "conv-b": {"notes-b.md"}}, saved
    # Each conversation's command queued fairly in its own work area.
    fair_keys = {call["fair_key"] for call in world["sandbox"].user_commands()}
    assert fair_keys == {"conv-a", "conv-b"}
    assert world["sandbox"].mirror_for("conv-a") != world["sandbox"].mirror_for("conv-b")


@pytest.mark.asyncio
async def test_a_failed_command_is_no_effect_only_when_it_changed_nothing_in_manor(world):
    """Every Manor change goes through the save-back, so a failed discovery
    command that saved and queued nothing is host-proved no effect; a failed
    command whose file was saved back is not."""
    from packages.core.ai.runtime.receipts import runtime_tool_effect_state

    async def raw(command: str) -> str:
        return await bash_tool._bash(
            world["entity_id"], command=command, _user_id_from_context=world["owner"],
            _agent_id_from_context="agent-1", conversation_id="conv-1",
        )

    probe = "python3 -c \"open('Knowledge/s01.wav')\""
    failed = await raw(probe)
    assert json.loads(failed)["exit_code"] != 0, failed
    assert runtime_tool_effect_state("bash", {"command": probe}, failed).value == "none"

    partial = "python3 -c \"open('Knowledge/partial.txt','w').write('x'); raise SystemExit(3)\""
    wrote = await raw(partial)
    payload = json.loads(wrote)
    assert payload["exit_code"] == 3 and payload["file_changes"] == {"created": ["Knowledge/partial.txt"]}, payload
    assert runtime_tool_effect_state("bash", {"command": partial}, wrote).value != "none"
