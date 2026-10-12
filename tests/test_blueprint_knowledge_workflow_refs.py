"""Blueprint Knowledge dependencies must survive a fresh install and native read."""
import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.blueprints.exporter import ExportContext, ExportError, export_workspace
from packages.core.blueprints.installer import InstallError, InstallMode, install_blueprint, _materialize_workflow_document_refs
from packages.core.blueprints.knowledge_files import create_starter_file
from packages.core.blueprints.payload import PayloadError, validate_payload
from packages.core.blueprints.workflow_dependencies import WorkflowDependencyError, WorkflowDocumentDependencyFactory as Refs
from packages.core.config import get_settings
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.workflow import WorkflowDefinition
from packages.core.services.entity_fs import write_entity_file_atomic
from test_blueprint_installer_embedded import _base_payload
from packages.core.models.user import Entity
from packages.core.governance.approval_policy_service import account_defaults


@pytest.fixture
async def entity_id(db_session):
    row = Entity(id=generate_ulid(), name="Blank template test", settings={"approval_defaults": account_defaults()})
    db_session.add(row)
    await db_session.commit()
    return row.id


@pytest.fixture
def fs_settings(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "oss")
    return settings


def payload():
    return _base_payload(**{
        "embedded.knowledge_packs": [{"slug": "templates", "title": "Templates", "mode": "inline_text",
            "starter_documents": [{"key": "watchlist", "path": "research/watchlist.csv", "body_md": "creator_key,handle,status\n"}]}],
        "recipe.operating_model": {"app_view": {"title": "Template reader", "hero": "workflow:read-watchlist", "order": ["workflow:read-watchlist"], "targets": {"workflow:read-watchlist": "workflow:read-watchlist"}}},
        "recipe.workflows": [{"slug": "read-watchlist", "name": "Read watchlist", "trigger_type": "manual",
            "steps": [{"id": "read", "type": "tool", "config": {"tool": "read_file", "arguments": {"knowledge_document_key": "watchlist"}}, "next": ["done"]},
                      {"id": "done", "type": "end", "config": {}, "next": []}]}],
    })


def test_document_refs_preserve_runtime_templates_and_reject_missing_fixed_ids():
    source_id = generate_ulid()
    node = [{"arguments": {"document_id": source_id}}, {"arguments": {"document_id": "{{output.document_id}}", "path": "{{output.fs_path}}"}}]
    saved = copy.deepcopy(node)
    portable = Refs.to_portable(node, document_key_by_id={source_id: "brand"})
    assert portable[0]["arguments"] == {"knowledge_document_key": "brand"}
    assert portable[1] == node[1]
    assert node == saved
    target_id = generate_ulid()
    assert Refs.to_runtime(portable, document_id_by_key={"brand": target_id})[0]["arguments"] == {"document_id": target_id}
    with pytest.raises(WorkflowDependencyError, match="not included"):
        Refs.to_portable(node, document_key_by_id={})
    with pytest.raises(WorkflowDependencyError, match="mixes a local ID"):
        Refs.to_portable({"document_id": "{{result.document_id}} " + source_id}, document_key_by_id={})
    with pytest.raises(WorkflowDependencyError, match="deployment-local path"):
        Refs.to_portable({"document_id": source_id, "path": "old/root.md"}, document_key_by_id={source_id: "brand"})


@pytest.mark.parametrize("bad", ["missing", "skeleton", "conflicting", "blank", "object"])
def test_payload_rejects_unresolvable_or_ambiguous_file_reference(bad):
    data = payload()
    args = data["recipe"]["workflows"][0]["steps"][0]["config"]["arguments"]
    if bad == "missing":
        args["knowledge_document_key"] = "not-included"
    elif bad == "skeleton":
        data["embedded"]["knowledge_packs"][0]["mode"] = "skeleton"
    elif bad == "conflicting":
        args["path"] = "old.csv"
    else:
        args["knowledge_document_key"] = "" if bad == "blank" else {}
    with pytest.raises(PayloadError):
        validate_payload(data)


@pytest.mark.parametrize("mode", [InstallMode.LIVE, InstallMode.SIMULATE])
async def test_install_native_read_reexport_and_second_install(db_session, entity_id, fs_settings, monkeypatch, mode):
    import packages.core.database as database
    from packages.core.ai.tools.file_tools import _read_file

    monkeypatch.setattr(database, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False))
    installed = await install_blueprint(db_session, entity_id=entity_id, payload=payload(), mode=mode)
    await db_session.commit()
    workflow = (await db_session.scalars(select(WorkflowDefinition).where(WorkflowDefinition.workspace_id == installed.workspace_id))).one()
    args = next(step for step in workflow.steps if step["id"] == "read")["config"]["arguments"]
    doc = await db_session.get(Document, args["document_id"])
    assert doc.mime_type == "text/csv" and doc.file_type == "csv"
    assert Path(fs_settings.MANOR_FS_ROOT, entity_id, doc.fs_path).read_text() == "creator_key,handle,status\n"
    read = json.loads(await _read_file(entity_id, workspace_id=installed.workspace_id, **args))
    assert "error" not in read, read
    assert "creator_key" in read["content"]
    assert read["document_id"] == doc.id
    exported = await export_workspace(db_session, workspace_id=installed.workspace_id, title="Template test", context=ExportContext(knowledge_pack_mode="inline_text"))
    validate_payload(exported)
    assert doc.id not in json.dumps(exported)
    assert doc.fs_path not in json.dumps(exported)
    starter = exported["embedded"]["knowledge_packs"][0]["starter_documents"][0]
    assert starter["path"] == "research/watchlist.csv"
    assert starter["body_md"] == "creator_key,handle,status\n"
    installed2 = await install_blueprint(db_session, entity_id=entity_id, payload=exported, mode=mode)
    await db_session.commit()
    workflow2 = (await db_session.scalars(select(WorkflowDefinition).where(WorkflowDefinition.workspace_id == installed2.workspace_id))).one()
    args2 = next(step for step in workflow2.steps if step["id"] == "read")["config"]["arguments"]
    assert args2["document_id"] != doc.id
    read2 = json.loads(await _read_file(entity_id, workspace_id=installed2.workspace_id, **args2))
    assert read2["content"] == read["content"]
    again = await export_workspace(db_session, workspace_id=installed2.workspace_id, title="Template test", context=ExportContext(knowledge_pack_mode="inline_text"))
    assert exported["recipe"]["operating_model"]["app_view"]["hero"] == "workflow:read-watchlist"
    assert again["recipe"]["operating_model"]["app_view"] == exported["recipe"]["operating_model"]["app_view"]
    assert again["recipe"]["workflows"] == exported["recipe"]["workflows"]
    assert again["embedded"]["knowledge_packs"] == exported["embedded"]["knowledge_packs"]
    with pytest.raises(ExportError, match="not included"):
        await export_workspace(db_session, workspace_id=installed.workspace_id, title="Template test")
    with pytest.raises(ExportError, match="not included"):
        await export_workspace(db_session, workspace_id=installed.workspace_id, title="Template test", context=ExportContext(knowledge_pack_mode="inline_text", knowledge_document_ids=frozenset()))
    denied = json.loads(await _read_file(entity_id, workspace_id=installed2.workspace_id, **args))
    assert "error" in denied


async def test_document_reference_cannot_borrow_another_workspaces_starter(db_session, entity_id):
    a = await install_blueprint(db_session, entity_id=entity_id, payload=payload())
    b_data = _base_payload()
    b = await install_blueprint(db_session, entity_id=entity_id, payload=b_data)
    with pytest.raises(InstallError, match="missing or ambiguous"):
        await _materialize_workflow_document_refs(db_session, entity_id=entity_id, workspace_id=b.workspace_id, node={"knowledge_document_key": "watchlist"})
    # Even membership cannot borrow a Document owned by another Workspace.
    original = (await db_session.scalars(select(Document).where(Document.entity_id == entity_id))).one()
    group = DocumentGroup(id=generate_ulid(), entity_id=entity_id, workspace_id=b.workspace_id, name="Borrowed")
    db_session.add(group)
    await db_session.flush()
    db_session.add(DocumentGroupMember(document_id=original.id, group_id=group.id))
    await db_session.flush()
    with pytest.raises(InstallError):
        await _materialize_workflow_document_refs(db_session, entity_id=entity_id, workspace_id=b.workspace_id, node={"knowledge_document_key": "watchlist"})
    assert a.workspace_id != b.workspace_id


@pytest.mark.parametrize("ending", ["commit", "rollback", "close", "nested-rollback", "nested-commit-rollback"])
async def test_starter_files_follow_transaction_lifetime(db_session, fs_settings, ending):
    await db_session.begin()
    outer_path = "templates/outer.md"
    create_starter_file(db_session, entity_id="test-files", path=outer_path, body="outer")
    outer = Path(fs_settings.MANOR_FS_ROOT, "test-files", outer_path)
    if ending.startswith("nested"):
        nested = await db_session.begin_nested()
        create_starter_file(db_session, entity_id="test-files", path="templates/inner.md", body="inner")
        inner = outer.with_name("inner.md")
        if ending == "nested-rollback":
            await nested.rollback()
            assert not inner.exists() and outer.exists()
            await db_session.commit()
            assert outer.read_text() == "outer"
        else:
            await nested.commit()
            assert inner.exists() and outer.exists()
            await db_session.rollback()
            assert not inner.exists() and not outer.exists()
        return
    await getattr(db_session, ending)()
    assert outer.exists() is (ending == "commit")


async def test_existing_and_later_edited_files_survive(db_session, fs_settings):
    await db_session.begin()
    path = "templates/watchlist.csv"
    write_entity_file_atomic("files", path, b"operator data")
    with pytest.raises(ValueError, match="occupied"):
        create_starter_file(db_session, entity_id="files", path=path, body="template")
    target = Path(fs_settings.MANOR_FS_ROOT, "files", path)
    assert target.read_text() == "operator data"
    create_starter_file(db_session, entity_id="files", path="templates/new.md", body="template")
    write_entity_file_atomic("files", "templates/new.md", b"later edit")
    await db_session.rollback()
    assert target.read_text() == "operator data"
    assert target.with_name("new.md").read_text() == "later edit"
