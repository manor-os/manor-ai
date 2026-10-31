"""File tools accept a file given by any address the platform handed out."""
from __future__ import annotations

from packages.core.ai.tools.file_tools import _file_reference_arg


def test_fs_url_of_this_entity_becomes_its_path(tmp_path):
    assert _file_reference_arg("ent1", str(tmp_path), "/api/v1/fs/ent1/Reports/Q3%20plan%20%E6%8A%A5%E5%91%8A.md") == (
        "Reports/Q3 plan 报告.md", "", False,
    )
    assert _file_reference_arg("ent1", str(tmp_path), "https://app.example/api/v1/fs/ent1/a/b.md") == ("a/b.md", "", False)
    # An unencoded URL (models write names raw) is decoded once, not twice.
    assert _file_reference_arg("ent1", str(tmp_path), "/api/v1/fs/ent1/a/100%25 done.md") == ("a/100% done.md", "", False)


def test_fs_url_of_another_entity_is_foreign(tmp_path):
    path, document_id, foreign = _file_reference_arg("ent1", str(tmp_path), "/api/v1/fs/ent2/a.md")
    assert foreign and not document_id


def test_viewer_url_is_a_document_id(tmp_path):
    assert _file_reference_arg("ent1", str(tmp_path), "/viewer/doc_123") == ("", "doc_123", False)


def test_sandbox_mount_and_knowledge_prefix_are_entity_paths(tmp_path):
    assert _file_reference_arg("ent1", str(tmp_path), "/workspace/a/b.md") == ("a/b.md", "", False)
    assert _file_reference_arg("ent1", str(tmp_path), "Knowledge/a/b.md") == ("a/b.md", "", False)


def test_real_knowledge_folder_wins_over_prefix_stripping(tmp_path):
    (tmp_path / "Knowledge").mkdir()
    (tmp_path / "Knowledge" / "b.md").write_text("x")
    assert _file_reference_arg("ent1", str(tmp_path), "Knowledge/b.md") == ("Knowledge/b.md", "", False)


async def test_runtime_resolves_file_addresses_before_the_approval_receipt(tmp_path, monkeypatch):
    """The receipt must name the path the handler writes, not the raw address."""
    from packages.core.ai.runtime import tool_execution

    monkeypatch.setattr("packages.core.services.entity_fs.get_entity_root", lambda _entity: str(tmp_path))
    patched = await tool_execution._runtime_canonical_file_arguments(
        "patch_file", {"path": "/api/v1/fs/ent1/Workspaces/a.csv", "operations": []}, entity_id="ent1",
    )
    assert patched == {"path": "Workspaces/a.csv", "operations": []}
    replaced = await tool_execution._runtime_canonical_file_arguments(
        "replace_file", {"source_path": "/workspace/new.csv", "target_path": "Knowledge/a.csv"}, entity_id="ent1",
    )
    assert replaced == {"source_path": "new.csv", "target_path": "a.csv"}
    foreign = {"path": "/api/v1/fs/ent2/a.csv"}
    assert await tool_execution._runtime_canonical_file_arguments("patch_file", foreign, entity_id="ent1") == foreign
    other = {"path": "/api/v1/fs/ent1/a.csv"}
    assert await tool_execution._runtime_canonical_file_arguments("read_file", other, entity_id="ent1") == other


def test_plain_and_unsafe_paths_pass_through_unchanged(tmp_path):
    assert _file_reference_arg("ent1", str(tmp_path), "a/./b.md") == ("a/./b.md", "", False)
    assert _file_reference_arg("ent1", str(tmp_path), "../etc/passwd") == ("../etc/passwd", "", False)
    assert _file_reference_arg("ent1", str(tmp_path), "/api/v1/fs/public/tok/x.png") == (
        "/api/v1/fs/public/tok/x.png", "", False,
    )
