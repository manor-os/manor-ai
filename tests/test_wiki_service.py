from packages.core.config import get_settings
from packages.core.services.wiki_service import (
    build_file_index,
    extract_wiki_links,
    lint_entity,
    resolve_link,
)


def test_wiki_map_route_is_retired_without_removing_wiki_links():
    from apps.api.routers.filesystem import router

    routes = {route.path for route in router.routes}
    assert "/api/v1/fs/wiki-index" not in routes
    assert "/api/v1/fs/wiki-links" in routes


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
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled
