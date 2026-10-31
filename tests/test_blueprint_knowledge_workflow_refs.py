"""Blueprint Knowledge dependencies must survive a fresh install and native read."""
import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from packages.core.blueprints.exporter import ExportContext, ExportError, export_workspace, list_exportable_knowledge_documents
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


@pytest.mark.unit
def test_initial_file_template_is_portable_without_a_live_renderer():
    from packages.core.blueprints.knowledge_templates import normalize_knowledge_template

    data = payload()
    starter = data["embedded"]["knowledge_packs"][0]["starter_documents"][0]
    starter["template"] = {"id": "creator-watchlist", "mode": "initial_file", "version": 1}
    validate_payload(data)
    for invalid in ({"id": ""}, {"version": 0}, {"version": True}, {"renderer": "live-data"}):
        malformed = copy.deepcopy(data)
        malformed["embedded"]["knowledge_packs"][0]["starter_documents"][0]["template"].update(invalid)
        with pytest.raises(PayloadError):
            validate_payload(malformed)
    # Legacy raw live-projection payloads normalize before persistence; the
    # new initial-file contract must not reject their historical spelling.
    starter["template"] = {"id": 42, "mode": " live_projection ", "renderer": " ledger ", "version": 1}
    validate_payload(data)
    assert normalize_knowledge_template(starter["template"]) == {
        "id": "42", "mode": "live_projection", "renderer": "ledger", "version": 1,
    }


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


async def test_initial_support_handoff_excludes_runtime_content_and_unmarked_artifacts(
    db_session, entity_id, fs_settings, isolated_test_redis,
):
    from packages.core.constants.blueprints import BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES

    initial = "# Support handoff\n\n## Open tickets\n\n## Next shift\n"
    data = payload()
    data["embedded"]["knowledge_packs"] = [{
        "slug": "support", "title": "Support", "mode": "inline_text", "starter_documents": [{
            "key": "handoff", "path": "support/handoff.md", "body_md": initial,
            "template": {"id": "support-handoff", "mode": "initial_file", "version": 1},
        }],
    }]
    workflow_spec = data["recipe"]["workflows"][0]
    workflow_spec.update(slug="read-handoff", name="Read support handoff")
    workflow_spec["steps"][0]["config"]["arguments"] = {"knowledge_document_key": "handoff"}
    data["recipe"]["operating_model"]["app_view"] = {
        "title": "Support handoff", "hero": "workflow:read-handoff",
        "order": ["workflow:read-handoff"], "targets": {"workflow:read-handoff": "workflow:read-handoff"},
    }
    installed = await install_blueprint(db_session, entity_id=entity_id, payload=data, mode=InstallMode.LIVE)
    await db_session.commit()
    workflow = (await db_session.scalars(select(WorkflowDefinition).where(WorkflowDefinition.workspace_id == installed.workspace_id))).one()
    read_step = next(step for step in workflow.steps if step["id"] == "read")
    doc = await db_session.get(Document, read_step["config"]["arguments"]["document_id"])
    assert doc.metadata_["blueprint_initial_body_md"] == initial
    current = "Private ticket evidence.\n" + "x" * BLUEPRINT_KNOWLEDGE_MAX_DOCUMENT_BYTES
    doc.metadata_ = {**doc.metadata_, "content_text": current, "artifact": {"storage_scope": "artifact"}}
    doc.file_size = len(current.encode())
    write_entity_file_atomic(entity_id, doc.fs_path, current.encode())
    original_metadata = copy.deepcopy(doc.metadata_)
    ordinary = Document(
        entity_id=entity_id, name="incident-output.md", mime_type="text/markdown",
        visibility="workspace", classification="public", quarantine_status="clean",
        metadata_={"content_text": "Unmarked runtime incident", "artifact": {"storage_scope": "artifact"}},
    )
    db_session.add(ordinary)
    await db_session.flush()
    group = (await db_session.scalars(select(DocumentGroup).where(DocumentGroup.workspace_id == installed.workspace_id))).one()
    db_session.add(DocumentGroupMember(document_id=ordinary.id, group_id=group.id))
    await db_session.commit()

    selected = ExportContext(knowledge_pack_mode="inline_text", knowledge_document_ids=frozenset({doc.id}))
    candidates = await list_exportable_knowledge_documents(db_session, entity_id=entity_id, workspace_id=installed.workspace_id)
    assert [item["id"] for item in candidates] == [doc.id]
    assert candidates[0]["file_size"] == len(initial.encode())
    exported = await export_workspace(db_session, installed.workspace_id, title="Support", context=selected)
    [starter] = exported["embedded"]["knowledge_packs"][0]["starter_documents"]
    assert starter["body_md"] == initial
    assert starter["template"] == data["embedded"]["knowledge_packs"][0]["starter_documents"][0]["template"]
    assert "Private ticket evidence" not in json.dumps(exported)
    with pytest.raises(ExportError, match="unavailable or ineligible"):
        await export_workspace(db_session, installed.workspace_id, title="Support", context=ExportContext(
            knowledge_pack_mode="inline_text", knowledge_document_ids=frozenset({doc.id, ordinary.id}),
        ))
    with pytest.raises(ExportError, match="not included"):
        await export_workspace(db_session, installed.workspace_id, title="Support", context=ExportContext(
            knowledge_pack_mode="inline_text", knowledge_document_ids=frozenset(),
        ))

    # Declaring an initial file must never fall back to its current runtime body.
    missing_body = copy.deepcopy(original_metadata)
    missing_body.pop("blueprint_initial_body_md")
    invalid_metadata = [missing_body, {**original_metadata, "blueprint_initial_body_md": ""}]
    for changes in ({"renderer": "live-data"}, {"version": 0}):
        invalid_metadata.append({
            **original_metadata, "blueprint_template": {**original_metadata["blueprint_template"], **changes},
        })
    orphaned_initial = {**original_metadata, "content_text": "Current runtime row", "artifact": {}}
    orphaned_initial.pop("blueprint_template")
    invalid_metadata.extend([orphaned_initial, {**orphaned_initial, "blueprint_template": None}])
    for invalid in invalid_metadata:
        doc.metadata_ = invalid
        await db_session.flush()
        assert not await list_exportable_knowledge_documents(db_session, entity_id=entity_id, workspace_id=installed.workspace_id)
        with pytest.raises(ExportError, match="unavailable or ineligible"):
            await export_workspace(db_session, installed.workspace_id, title="Support", context=selected)
    doc.metadata_ = original_metadata
    doc.visibility = "private"
    await db_session.flush()
    assert not await list_exportable_knowledge_documents(db_session, entity_id=entity_id, workspace_id=installed.workspace_id)
    with pytest.raises(ExportError, match="unavailable or ineligible"):
        await export_workspace(db_session, installed.workspace_id, title="Support", context=selected)
    doc.visibility = "workspace"
    await db_session.flush()
    await db_session.refresh(doc)
    assert doc.metadata_ == original_metadata
    assert Path(fs_settings.MANOR_FS_ROOT, entity_id, doc.fs_path).read_bytes() == current.encode()


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
