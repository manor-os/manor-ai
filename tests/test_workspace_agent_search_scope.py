"""An agent working in a Workspace searches that Workspace by default.

The incident: a Workflow step in a Workspace found ``creators-24.csv``, the
owner's private file at the root of their Knowledge, unrelated to the Workspace.
It was not a permission leak (the agent acts as the owner), but discovery must
stop at the Workspace unless a person or the run names something else.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import packages.core.database as db_module
from packages.core.ai.runtime.bash_mirror import MirrorManifest
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.ai.runtime.tool_context import (
    runtime_tool_call_context_from_kwargs,
    runtime_tool_call_context_is_person_chat,
)
from packages.core.ai.tools.bash_tool import _plan_staging
from packages.core.ai.tools.document_tools import _list_documents, _search_documents
from packages.core.ai.tools.file_tools import _glob_files, _grep_files, _list_files, _read_file
from packages.core.ai.tools.manor_tool import _dispatch_action
from packages.core.ai.tools.rag_tools import _rag
from packages.core.config import get_settings
from packages.core.models.base import generate_ulid
from packages.core.models.document import DocumentFolder, DocumentGroup, DocumentGroupMember
from packages.core.models.task import Conversation, Message
from packages.core.models.user import Entity, User, UserMembership
from packages.core.models.workspace import Workspace
from packages.core.services.auth_service import hash_password
from packages.core.services.document_service import create_document
from packages.core.services.sandbox_cli.operations import cli_glob, cli_ls
from packages.core.services.sandbox_cli.tokens import SandboxCliClaims
from packages.core.services.workspace_artifacts import workspace_artifact_storage_base

OUTSIDE = "creators-24.csv"
CHAT_UPLOAD = "uploads/chat/0123456789abcdef.csv"


@dataclass
class World:
    root: object
    entity_id: str
    owner_id: str
    member_id: str
    workspace_id: str
    home: str
    inside: str
    attached: str
    attached_sibling: str
    named: str
    other_workspace: str
    members_private: str
    restricted: str
    outside_id: str
    named_folder_id: str
    conversation_id: str


@pytest.fixture
def fs_enabled(tmp_path):
    settings = get_settings()
    old_enabled, old_root = settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    try:
        yield tmp_path
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root


async def _user(db, entity_id: str, role: str) -> str:
    user = User(
        entity_id=entity_id, email=f"{role}-{entity_id}@test.com", display_name=role,
        password_hash=hash_password("pass123"), role=role, status="active",
    )
    db.add(user)
    await db.flush()
    db.add(UserMembership(user_id=user.id, entity_id=entity_id, role=role, status="active", is_primary=True))
    return user.id


@pytest.fixture
async def world(fs_enabled) -> World:
    """A Workspace, Knowledge attached to it, and the owner's unrelated files."""
    entity_id = generate_ulid()
    root = fs_enabled / entity_id
    async with db_module.async_session() as db:
        db.add(Entity(id=entity_id, name="Scope entity"))
        await db.flush()
        owner_id = await _user(db, entity_id, "owner")
        member_id = await _user(db, entity_id, "member")
        workspace = Workspace(
            entity_id=entity_id, name="Scope", artifact_folder_id=generate_ulid(), status="active",
        )
        other = Workspace(
            entity_id=entity_id, name="Other", artifact_folder_id=generate_ulid(), status="active",
        )
        db.add_all([workspace, other])
        await db.flush()
        home = workspace_artifact_storage_base(workspace.artifact_folder_id)
        inside = f"{home}/documents/creators-inside.csv"
        attached = "Client Briefs/creators-acme.csv"
        attached_sibling = "Client Briefs/creators-other-client.csv"
        named = "Reports/creators-q3.csv"
        other_workspace = (
            f"{workspace_artifact_storage_base(other.artifact_folder_id)}/documents/creators-other.csv"
        )
        members_private = "creators-members-private.csv"
        restricted = "Confidential/creators-restricted.csv"
        folder = DocumentFolder(entity_id=entity_id, name="Reports", owner_id=owner_id)
        db.add(folder)
        await db.flush()
        documents = {}
        for path, kwargs in (
            (OUTSIDE, {"owner_id": owner_id, "visibility": "private"}),
            (inside, {"owner_id": owner_id, "visibility": "private"}),
            (attached, {"owner_id": owner_id, "visibility": "private"}),
            (attached_sibling, {"owner_id": owner_id, "visibility": "private"}),
            (named, {"owner_id": owner_id, "visibility": "private", "folder_id": folder.id}),
            (other_workspace, {"owner_id": owner_id, "visibility": "entity"}),
            (members_private, {"owner_id": member_id, "visibility": "private"}),
            (restricted, {"owner_id": owner_id, "visibility": "private", "classification": "restricted"}),
        ):
            documents[path] = await create_document(
                db, entity_id, name=path.rsplit("/", 1)[-1], fs_path=path, file_type="csv",
                source="upload", **kwargs,
            )
        net = DocumentGroup(
            entity_id=entity_id, workspace_id=workspace.id, name="Client Briefs",
            settings={"kind": "knowledge_net"},
        )
        db.add(net)
        await db.flush()
        db.add(DocumentGroupMember(document_id=documents[attached].id, group_id=net.id))
        conversation = Conversation(
            entity_id=entity_id, user_id=owner_id, workspace_id=workspace.id,
            scope="workspace_main", status="active", title="Scope",
        )
        db.add(conversation)
        await db.flush()
        db.add(Message(
            conversation_id=conversation.id, role="user", author_kind="user",
            content="see this file", attachments=[{"kind": "chat_upload", "path": CHAT_UPLOAD}],
        ))
        built = World(
            root=root, entity_id=entity_id, owner_id=owner_id, member_id=member_id,
            workspace_id=workspace.id, home=home, inside=inside, attached=attached,
            attached_sibling=attached_sibling, named=named, other_workspace=other_workspace,
            members_private=members_private, restricted=restricted, outside_id=documents[OUTSIDE].id,
            named_folder_id=folder.id, conversation_id=conversation.id,
        )
        await db.commit()
    for path in (
        OUTSIDE, inside, attached, attached_sibling, named, other_workspace, members_private, restricted, CHAT_UPLOAD,
    ):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(f"needle in {path}", encoding="utf-8")
    return built


def _run(world: World, *, user: str | None = None, surface: ChatSurface = ChatSurface.WORKFLOW_AGENT_STEP) -> dict:
    """Trusted context of a run in the Workspace; a Workflow step unless told otherwise."""
    user_id = user or world.owner_id
    return {
        "workspace_id": world.workspace_id,
        "_user_id_from_context": user_id,
        "_runtime_envelope_from_context": SimpleNamespace(
            surface=surface, user_id=user_id, workspace_id=world.workspace_id,
        ),
    }


def _claims(world: World, *, workspace: bool = True) -> SandboxCliClaims:
    return SandboxCliClaims(
        entity_id=world.entity_id, sandbox_id="sandbox", exec_id="exec", expires_at=time.time() + 60,
        user_id=world.owner_id, workspace_id=world.workspace_id if workspace else None,
    )


async def _stage(world: World, command: str, *, workspace: bool = True):
    return await _plan_staging(
        entity_id=world.entity_id,
        entity_root=str(world.root),
        runtime_context=SimpleNamespace(
            user_id=world.owner_id, workspace_id=world.workspace_id if workspace else None,
        ),
        command=command,
        manifest=MirrorManifest.fresh("manifest"),
    )


def _names(payload: dict, key: str = "documents") -> set[str]:
    return {document["name"] for document in payload[key]}


@pytest.mark.asyncio
async def test_file_search_naming_no_location_stops_at_the_workspace(world):
    """The incident: ``**/*.csv`` and a root grep found the owner's unrelated file."""
    glob_result = json.loads(await _glob_files(world.entity_id, pattern="**/*.csv", **_run(world)))
    assert set(glob_result["files"]) == {world.inside, world.attached}
    assert "Workspace only" in glob_result["scope_note"]

    grep_result = json.loads(await _grep_files(world.entity_id, pattern="needle", **_run(world)))
    assert {match["file"] for match in grep_result["matches"]} == {world.inside, world.attached}
    assert "Workspace only" in grep_result["scope_note"]

    # /workspace is the bash spelling of the entity root, not a named folder.
    listed = json.loads(await _list_files(world.entity_id, path="/workspace", **_run(world)))
    paths = {entry["path"] for entry in listed["entries"]}
    assert paths == {"Client Briefs", "Workspaces"}
    assert "Workspace only" in listed["scope_note"]


@pytest.mark.asyncio
async def test_nothing_found_says_the_search_stopped_at_the_workspace(world, monkeypatch):
    """An empty result must not read as "the file does not exist"."""
    found = json.loads(await _glob_files(world.entity_id, pattern=f"**/{OUTSIDE}", **_run(world)))
    assert found["files"] == []
    assert "1 file(s) outside" in found["scope_note"]

    searched = json.loads(await _search_documents(world.entity_id, query=OUTSIDE, **_run(world)))
    assert searched["documents"] == []
    assert "1 file(s) outside" in searched["scope_note"]

    async def no_index_hits(*_args, **_kwargs):
        return []

    monkeypatch.setattr("packages.core.services.embedding_service.hybrid_search", no_index_hits)
    rag = json.loads(await _rag(world.entity_id, question=OUTSIDE, **_run(world)))
    assert rag["source_count"] == 0
    assert "this Workspace only" in rag["message"]


@pytest.mark.asyncio
async def test_results_in_scope_without_exclusions_carry_no_note(world):
    found = json.loads(await _glob_files(world.entity_id, pattern=f"{world.home}/**/*.csv", **_run(world)))
    assert found["files"] == [world.inside]
    assert "scope_note" not in found


@pytest.mark.asyncio
@pytest.mark.parametrize("via_manor", [False, True], ids=["tool", "manor_action"])
@pytest.mark.parametrize("action", ["list_documents", "search_documents"])
async def test_document_listing_stops_at_the_workspace_and_keeps_bound_knowledge(world, action, via_manor):
    """``manor(list_documents)`` never received the run's Workspace at all."""
    params = {"query": "creators", "limit": 50}
    if via_manor:
        runtime_context = runtime_tool_call_context_from_kwargs(_run(world))
        result = await _dispatch_action(
            action, params, world.entity_id, user_id=world.owner_id, workspace_id=world.workspace_id,
            runtime_context=runtime_context,
        )
    else:
        handler = _list_documents if action == "list_documents" else _search_documents
        result = await handler(world.entity_id, **params, **_run(world))
    payload = json.loads(result)
    assert _names(payload) == {"creators-inside.csv", "creators-acme.csv"}
    assert "Workspace only" in payload["scope_note"]


@pytest.mark.asyncio
async def test_a_named_folder_is_read_under_the_acl_not_the_default_scope(world):
    payload = json.loads(await _list_documents(
        world.entity_id, folder_id=world.named_folder_id, **_run(world),
    ))
    assert _names(payload) == {"creators-q3.csv"}
    assert "scope_note" not in payload

    # The top level is not a location: it is where the unrelated files are.
    root_listing = json.loads(await _list_documents(world.entity_id, folder_id="root", **_run(world)))
    assert OUTSIDE not in _names(root_listing)


@pytest.mark.asyncio
async def test_named_paths_keep_working(world):
    """Whatever a person or the run names is judged by the read ACL alone."""
    run = _run(world)
    named_dir = world.named.rsplit("/", 1)[0]

    for ref in (
        {"path": OUTSIDE},  # a path in a prompt or required_refs entry
        {"document_id": world.outside_id},
        {"path": f"/viewer/{world.outside_id}"},  # a file linked in the message
    ):
        read = json.loads(await _read_file(world.entity_id, **ref, **run))
        assert read.get("content") == f"needle in {OUTSIDE}", read

    listed = json.loads(await _list_files(world.entity_id, path=named_dir, **run))
    assert [entry["path"] for entry in listed["entries"]] == [world.named]
    grepped = json.loads(await _grep_files(world.entity_id, pattern="needle", path=named_dir, **run))
    assert [match["file"] for match in grepped["matches"]] == [world.named]
    globbed = json.loads(await _glob_files(world.entity_id, pattern=f"{named_dir}/*.csv", **run))
    assert globbed["files"] == [world.named]
    exact = json.loads(await _glob_files(world.entity_id, pattern=OUTSIDE, **run))
    assert exact["files"] == [OUTSIDE]
    for payload in (listed, grepped, globbed, exact):
        assert "scope_note" not in payload


@pytest.mark.asyncio
async def test_file_attached_to_the_conversation_keeps_working(world):
    read = json.loads(await _read_file(
        world.entity_id, path=CHAT_UPLOAD, conversation_id=world.conversation_id, **_run(world),
    ))
    assert read.get("content") == f"needle in {CHAT_UPLOAD}", read


@pytest.mark.asyncio
async def test_the_master_agent_outside_any_workspace_is_unchanged(world):
    context = {"_user_id_from_context": world.owner_id}
    globbed = json.loads(await _glob_files(world.entity_id, pattern="**/*.csv", **context))
    assert {OUTSIDE, world.inside, world.attached, world.attached_sibling, world.named} <= set(globbed["files"])
    assert "scope_note" not in globbed

    grepped = json.loads(await _grep_files(world.entity_id, pattern="needle", **context))
    assert OUTSIDE in {match["file"] for match in grepped["matches"]}

    listed = json.loads(await _list_files(world.entity_id, **context))
    assert OUTSIDE in {entry["path"] for entry in listed["entries"]}

    searched = json.loads(await _search_documents(world.entity_id, query="creators", limit=50, **context))
    assert {OUTSIDE, "creators-other.csv"} <= _names(searched)
    assert "scope_note" not in searched

    assert OUTSIDE in (await _stage(world, "ls", workspace=False)).placeholders


@pytest.mark.asyncio
async def test_a_person_in_chat_can_ask_to_look_outside_the_workspace(world):
    chat = _run(world, surface=ChatSurface.WORKSPACE_CHAT)
    hinted = json.loads(await _search_documents(world.entity_id, query=OUTSIDE, **chat))
    assert hinted["documents"] == []
    assert "scope='all_knowledge'" in hinted["scope_note"]

    wide = json.loads(await _search_documents(
        world.entity_id, query="creators", scope="all_knowledge", limit=50, **chat,
    ))
    assert {OUTSIDE, "creators-acme.csv", "creators-q3.csv"} <= _names(wide)
    assert "scope_note" not in wide
    # Not broader than this run's reads: another Workspace's file stays out.
    assert "creators-other.csv" not in _names(wide)
    # What it lists, the run can open.
    for document in wide["documents"]:
        opened = json.loads(await _read_file(world.entity_id, document_id=document["document_id"], **chat))
        assert "content" in opened, document["name"]

    # A member asking the same never sees the owner's private files.
    member_chat = _run(world, user=world.member_id, surface=ChatSurface.WORKSPACE_CHAT)
    member_wide = json.loads(await _search_documents(
        world.entity_id, query="creators", scope="all_knowledge", limit=50, **member_chat,
    ))
    assert _names(member_wide) == {"creators-members-private.csv"}


@pytest.mark.asyncio
async def test_a_workflow_step_cannot_widen_its_own_search(world, monkeypatch):
    """The run's model choosing ``all_knowledge`` is not a person asking."""
    from packages.core.cache import cache

    cached = {}

    async def get(key):
        return cached.get(key)

    async def set_value(key, value, ttl=300):
        cached[key] = value
        return True

    monkeypatch.setattr(cache, "get", get)
    monkeypatch.setattr(cache, "set", set_value)
    for handler in (_search_documents, _list_documents):
        # A normal query must not cache away the later refusal explanation.
        ordinary = json.loads(await handler(
            world.entity_id, query="creators", limit=50, **_run(world),
        ))
        payload = json.loads(await handler(
            world.entity_id, query="creators", scope="all_knowledge", limit=50, **_run(world),
        ))
        assert _names(payload) == {"creators-inside.csv", "creators-acme.csv"}
        assert payload["documents"] == ordinary["documents"]
        assert "applies only when a person asks" in payload["scope_note"]
        # The rejected request must not attach its explanation to a normal one.
        assert json.loads(await handler(
            world.entity_id, query="creators", limit=50, **_run(world),
        )) == ordinary


@pytest.mark.parametrize(
    ("surface", "ids", "expected"),
    [
        (ChatSurface.WORKSPACE_CHAT, {}, True),
        (ChatSurface.AGENT_DM, {}, True),
        (ChatSurface.WORKSPACE_DRAFT_ARCHITECT, {}, True),
        (ChatSurface.WORKSPACE_CHAT, {"task_id": "task"}, False),
        (ChatSurface.WORKSPACE_CHAT, {"_workflow_run_id_from_context": "run"}, False),
        (ChatSurface.WORKFLOW_AGENT_STEP, {}, False),
        (ChatSurface.SCHEDULED_AGENT_RUN, {}, False),
        (ChatSurface.PUBLIC_CUSTOMER_CHAT, {}, False),
        (ChatSurface.EXTERNAL_CHANNEL_CHAT, {}, False),
    ],
)
def test_only_a_signed_in_person_in_chat_counts_as_a_person_asking(surface, ids, expected):
    context = runtime_tool_call_context_from_kwargs({
        "_user_id_from_context": "user",
        "_runtime_envelope_from_context": SimpleNamespace(surface=surface, user_id="user"),
        **ids,
    })
    assert runtime_tool_call_context_is_person_chat(context) is expected


def test_a_chat_with_no_user_is_not_a_person_asking():
    context = runtime_tool_call_context_from_kwargs({
        "_runtime_envelope_from_context": SimpleNamespace(surface=ChatSurface.WORKSPACE_CHAT),
    })
    assert runtime_tool_call_context_is_person_chat(context) is False


@pytest.mark.asyncio
async def test_exclusions_only_count_files_the_actor_could_read(world):
    """The note must not confirm files the ACL hides."""
    # Left out and readable: the owner's root file, the attached file's sibling,
    # the named folder's file and the member's file (the owner may read it).
    # Agents cannot read the restricted file and this run cannot open the other
    # Workspace's, so neither is listed or counted by either search.
    found = json.loads(await _glob_files(world.entity_id, pattern="**/creators-*.csv", **_run(world)))
    assert set(found["files"]) == {world.inside, world.attached}
    assert "4 file(s) outside" in found["scope_note"]

    searched = json.loads(await _search_documents(world.entity_id, query="creators", limit=50, **_run(world)))
    assert _names(searched) == {"creators-inside.csv", "creators-acme.csv"}
    assert "4 file(s) outside" in searched["scope_note"]


@pytest.mark.asyncio
async def test_sandbox_cli_listing_stops_at_the_workspace(world):
    claims = _claims(world)
    listed = await cli_ls(claims, {"path": ""})
    assert {entry["path"] for entry in listed["entries"]} == {"Client Briefs", "Workspaces"}
    assert "Workspace only" in listed["scope_note"]

    deep = await cli_ls(claims, {"path": "", "recursive": True})
    assert {entry["path"] for entry in deep["entries"]} == {world.inside, world.attached}

    named = await cli_ls(claims, {"path": "Reports"})
    assert [entry["path"] for entry in named["entries"]] == [world.named]
    assert "scope_note" not in named

    assert OUTSIDE not in (await cli_glob(claims, {"pattern": "**/*.csv"}))["files"]
    unscoped = await cli_ls(_claims(world, workspace=False), {"path": ""})
    assert OUTSIDE in {entry["path"] for entry in unscoped["entries"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    ["ls", "ls -R", "find . -name '*.csv'", "grep -r needle", "rg needle", "cat *.csv"],
)
async def test_bash_working_copy_gets_no_unnamed_files_from_outside(world, command):
    plan = await _stage(world, command)
    staged = {*plan.files, *plan.placeholders}
    assert OUTSIDE not in staged
    assert world.named not in staged
    assert world.attached_sibling not in staged
    assert plan.files_outside_workspace > 0


@pytest.mark.asyncio
async def test_bash_commands_naming_a_file_or_folder_keep_working(world):
    named_file = await _stage(world, f"cat {OUTSIDE}")
    assert OUTSIDE in named_file.files
    assert named_file.files_outside_workspace == 0

    named_folder = await _stage(world, "grep -r needle Reports")
    assert world.named in named_folder.files
    assert "Reports" in named_folder.dirs
    assert named_folder.files_outside_workspace == 0

    bound = await _stage(world, "grep -r needle")
    assert {world.inside, world.attached} <= set(bound.files)
