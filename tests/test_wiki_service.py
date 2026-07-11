from packages.core.config import get_settings
from packages.core.services.wiki_service import (
    build_file_index,
    build_wiki_graph,
    extract_wiki_links,
    lint_entity,
    resolve_link,
)


def test_wiki_links_resolve_visible_markdown_and_lint_broken_links(tmp_path):
    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        entity_id = "ent_wiki"
        root = tmp_path / entity_id
        docs = root / "Knowledge"
        hidden = root / ".ai"
        docs.mkdir(parents=True)
        hidden.mkdir()

        (docs / "Lease Playbook.md").write_text(
            "See [[Client FAQ|FAQ]] and [[Missing Page]].",
            encoding="utf-8",
        )
        (docs / "Client FAQ.md").write_text("Approved answers.", encoding="utf-8")
        (hidden / "Hidden.md").write_text("internal", encoding="utf-8")

        links = extract_wiki_links((docs / "Lease Playbook.md").read_text(encoding="utf-8"))
        assert links == [("Client FAQ", "FAQ"), ("Missing Page", None)]

        index = build_file_index(entity_id)
        assert index["client faq"] == "Knowledge/Client FAQ.md"
        assert index["knowledge/client faq"] == "Knowledge/Client FAQ.md"
        assert "hidden" not in index

        assert resolve_link("Client FAQ", entity_id, index) == "Knowledge/Client FAQ.md"
        assert resolve_link("Knowledge/Client FAQ.md", entity_id, index) == "Knowledge/Client FAQ.md"
        assert resolve_link(".ai/Hidden", entity_id, index) is None

        lint = lint_entity(entity_id)
        assert {"file": "Knowledge/Lease Playbook.md", "link": "Missing Page"} in lint["broken_links"]

        graph = build_wiki_graph(entity_id)
        assert graph["page_count"] == 2
        assert graph["link_count"] == 2
        assert graph["missing_count"] == 1
        page_by_title = {page["title"]: page for page in graph["pages"]}
        assert page_by_title["Lease Playbook"]["links"][0]["resolved_path"] == "Knowledge/Client FAQ.md"
        assert page_by_title["Client FAQ"]["backlinks"] == [
            {"source_path": "Knowledge/Lease Playbook.md", "source_title": "Lease Playbook"}
        ]
        assert graph["missing_links"][0]["target"] == "Missing Page"

        scoped_graph = build_wiki_graph(entity_id, allowed_paths={"Knowledge/Lease Playbook.md"})
        assert scoped_graph["page_count"] == 1
        assert scoped_graph["pages"][0]["path"] == "Knowledge/Lease Playbook.md"
        assert scoped_graph["pages"][0]["links"][0]["exists"] is True
        assert scoped_graph["pages"][0]["links"][0]["resolved_path"] == "Knowledge/Client FAQ.md"
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


def test_wiki_graph_deduplicates_same_physical_markdown_file(tmp_path):
    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        entity_id = "ent_wiki_dedup"
        root = tmp_path / entity_id
        notes = root / "Notes"
        alias = root / "Alias"
        notes.mkdir(parents=True)
        alias.mkdir()

        source = notes / "Shared.md"
        source.write_text("Shared page.", encoding="utf-8")
        try:
            (alias / "Shared.md").symlink_to(source)
        except (NotImplementedError, OSError):
            return

        graph = build_wiki_graph(entity_id)
        shared_pages = [page for page in graph["pages"] if page["title"] == "Shared"]

        assert graph["page_count"] == 1
        assert len(shared_pages) == 1
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled
