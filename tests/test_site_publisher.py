"""site_publisher — publish target resolution, snapshot validation, link check."""
import os

import pytest

from packages.core.services import site_publisher as sp


@pytest.fixture
def entity_fs(tmp_path, monkeypatch):
    """Point MANOR_FS_ROOT at tmp and create an entity dir."""
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    # Unique per test: the shared test DB is not truncated between tests in
    # the same run, so a fixed entity_id would leak Site rows across tests.
    from packages.core.models.base import generate_ulid

    eid = generate_ulid()
    (tmp_path / eid).mkdir()
    return tmp_path / eid, eid


def _mk(root, rel, content=b"x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


class TestResolvePublishTarget:
    def test_folder_with_index(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "mysite/index.html")
        t = sp.resolve_publish_target(eid, "mysite")
        assert t and t.kind == "folder" and t.root_rel == "mysite"

    def test_index_file_resolves_to_parent_folder(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "mysite/index.html")
        t = sp.resolve_publish_target(eid, "mysite/index.html")
        assert t and t.kind == "folder" and t.root_rel == "mysite"

    def test_sibling_html_in_bundle_resolves_to_folder(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "mysite/index.html")
        _mk(root, "mysite/about.html")
        t = sp.resolve_publish_target(eid, "mysite/about.html")
        assert t and t.kind == "folder" and t.root_rel == "mysite"

    def test_standalone_html_is_single_file(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "notes/report.html")
        t = sp.resolve_publish_target(eid, "notes/report.html")
        assert t and t.kind == "file" and t.root_rel == "notes/report.html"

    def test_folder_without_index_not_publishable(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "data/foo.csv")
        assert sp.resolve_publish_target(eid, "data") is None

    def test_non_html_file_not_publishable(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "doc.pdf")
        assert sp.resolve_publish_target(eid, "doc.pdf") is None

    def test_missing_path_not_publishable(self, entity_fs):
        root, eid = entity_fs
        assert sp.resolve_publish_target(eid, "nope") is None


class TestCollectSnapshot:
    def test_reference_code_excluded_but_reported(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "s/index.html", b"<html></html>")
        _mk(root, "s/css/style.css")
        _mk(root, "s/reference/App.jsx")
        _mk(root, "s/reference/notes.md")
        snap = sp.collect_snapshot(str(root / "s"))
        inc = {f.rel for f in snap.included}
        exc = {f.rel for f in snap.excluded}
        assert inc == {"index.html", "css/style.css"}
        assert exc == {"reference/App.jsx", "reference/notes.md"}

    def test_env_flagged_sensitive(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "s/index.html")
        _mk(root, "s/.env", b"SECRET=1")
        snap = sp.collect_snapshot(str(root / "s"))
        assert any(f.sensitive for f in snap.excluded)

    def test_ds_store_silently_skipped(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "s/index.html")
        _mk(root, "s/.DS_Store")
        snap = sp.collect_snapshot(str(root / "s"))
        assert all(f.rel != ".DS_Store" for f in snap.included + snap.excluded)

    def test_symlink_rejected(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "s/index.html")
        _mk(root, "outside.txt")
        os.symlink(str(root / "outside.txt"), str(root / "s" / "link.txt"))
        with pytest.raises(sp.SitePublishError, match="symlink"):
            sp.collect_snapshot(str(root / "s"))

    def test_file_count_limit(self, entity_fs, monkeypatch):
        monkeypatch.setattr(sp, "MAX_FILES", 10)
        root, eid = entity_fs
        _mk(root, "s/index.html")
        for i in range(11):
            _mk(root, f"s/a/f{i}.txt")
        with pytest.raises(sp.SitePublishError, match="file count"):
            sp.collect_snapshot(str(root / "s"))

    def test_oversize_file_rejected(self, entity_fs, monkeypatch):
        monkeypatch.setattr(sp, "MAX_FILE_BYTES", 10)
        root, eid = entity_fs
        _mk(root, "s/index.html", b"x" * 11)
        with pytest.raises(sp.SitePublishError, match="too large"):
            sp.collect_snapshot(str(root / "s"))


class TestCheckLinks:
    def test_broken_internal_ref_fails(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "s/index.html", b'<img src="missing.png">')
        snap = sp.collect_snapshot(str(root / "s"))
        errors = sp.check_links(str(root / "s"), snap)
        assert errors and "missing.png" in errors[0]

    def test_ref_to_excluded_file_fails(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "s/index.html", b'<script src="reference/App.jsx"></script>')
        _mk(root, "s/reference/App.jsx")
        snap = sp.collect_snapshot(str(root / "s"))
        errors = sp.check_links(str(root / "s"), snap)
        assert errors and "App.jsx" in errors[0]

    def test_platform_absolute_path_fails(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "s/index.html", b'<img src="/api/v1/fs/e1/x.png">')
        snap = sp.collect_snapshot(str(root / "s"))
        errors = sp.check_links(str(root / "s"), snap)
        assert errors and "/api/" in errors[0]

    def test_clean_bundle_passes(self, entity_fs):
        root, eid = entity_fs
        _mk(
            root,
            "s/index.html",
            b'<main id="top"></main><link href="css/style.css"><a href="about.html">a</a>'
            b'<a href="https://example.com">x</a><a href="#top">t</a>'
            b'<img src="data:image/png;base64,AAAA">',
        )
        _mk(root, "s/css/style.css")
        _mk(root, "s/about.html")
        snap = sp.collect_snapshot(str(root / "s"))
        assert sp.check_links(str(root / "s"), snap) == []

    def test_placeholder_and_missing_anchor_destinations_fail(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "s/index.html", b'<a href="#">one</a><a>two</a>')
        snap = sp.collect_snapshot(str(root / "s"))
        errors = sp.check_links(str(root / "s"), snap)
        assert len(errors) == 2
        assert all("no real destination" in error for error in errors)

    def test_missing_fragment_target_fails(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "s/index.html", b'<a href="#missing">jump</a>')
        snap = sp.collect_snapshot(str(root / "s"))
        errors = sp.check_links(str(root / "s"), snap)
        assert errors and "fragment target" in errors[0]

    def test_srcset_css_and_javascript_static_refs_are_checked(self, entity_fs):
        root, eid = entity_fs
        _mk(
            root,
            "s/index.html",
            b'<img srcset="img/one.png 1x, img/two.png 2x">'
            b'<link rel="stylesheet" href="site.css"><script src="app.js"></script>',
        )
        _mk(root, "s/img/one.png")
        _mk(root, "s/site.css", b".hero { background: url('img/bg.png'); }")
        _mk(root, "s/app.js", b"fetch('data/content.json'); import('./module.js');")
        snap = sp.collect_snapshot(str(root / "s"))
        errors = sp.check_links(str(root / "s"), snap)
        assert any("img/two.png" in error for error in errors)
        assert any("img/bg.png" in error for error in errors)
        assert any("data/content.json" in error for error in errors)
        assert any("module.js" in error for error in errors)

    def test_detects_unwired_button_and_form(self, entity_fs):
        root, eid = entity_fs
        _mk(
            root,
            "s/index.html",
            b'<button>Nothing happens</button><form><button type="submit">Send</button></form>',
        )
        snap = sp.collect_snapshot(str(root / "s"))
        errors = sp.check_links(str(root / "s"), snap)
        assert any("button has no detectable behavior" in error for error in errors)
        assert any("form has no action" in error for error in errors)

    def test_script_hooks_make_buttons_and_forms_valid(self, entity_fs):
        root, eid = entity_fs
        _mk(
            root,
            "s/index.html",
            b'<button class="toggle">Toggle</button>'
            b'<form id="signup"><button type="submit">Send</button></form>'
            b'<script>document.querySelector(".toggle").addEventListener("click", run);'
            b'document.querySelector("#signup").addEventListener("submit", run);</script>',
        )
        snap = sp.collect_snapshot(str(root / "s"))
        assert sp.check_links(str(root / "s"), snap) == []

    def test_subdir_relative_ref_resolves(self, entity_fs):
        root, eid = entity_fs
        _mk(root, "s/index.html", b'<a href="pages/two.html">2</a>')
        _mk(root, "s/pages/two.html", b'<img src="../img/logo.png">')
        _mk(root, "s/img/logo.png")
        snap = sp.collect_snapshot(str(root / "s"))
        assert sp.check_links(str(root / "s"), snap) == []


class TestSlug:
    def test_generated_slug_is_entity_prefixed_and_dns_valid(self):
        eid = "01JABCDEFGHJKMNPQRSTVWXYZ0"
        s = sp.generate_site_slug(eid)
        assert sp._SLUG_RE.match(s)
        assert s.startswith(eid.lower()[:20])
        assert len(s) <= 63

    def test_generated_slugs_do_not_collide(self):
        eid = "01JABCDEFGHJKMNPQRSTVWXYZ0"
        assert sp.generate_site_slug(eid) != sp.generate_site_slug(eid)

    def test_empty_entity_still_valid(self):
        s = sp.generate_site_slug("")
        assert sp._SLUG_RE.match(s) and s.startswith("site-")


async def _publish_bundle(db_session, entity_fs, name="My site"):
    root, eid = entity_fs
    _mk(root, "s/index.html", b"<html>v1</html>")
    _mk(root, "s/css/style.css")
    _mk(root, "s/reference/App.jsx")
    return await sp.publish(db_session, entity_id=eid, rel_path="s", name=name)


@pytest.mark.asyncio
class TestPublish:
    async def test_publish_creates_site_and_snapshot(self, db_session, entity_fs):
        root, eid = entity_fs
        result = await _publish_bundle(db_session, entity_fs)
        assert result.site.revision == 1
        assert result.site.status == "active"
        rev_dir = root / ".sites" / result.site.id / "rev1"
        assert (rev_dir / "index.html").exists()
        assert (rev_dir / "css" / "style.css").exists()
        assert not (rev_dir / "reference").exists()
        assert {f.rel for f in result.excluded} == {"reference/App.jsx"}

    async def test_republish_bumps_revision_and_prunes(self, db_session, entity_fs):
        root, eid = entity_fs
        r1 = await _publish_bundle(db_session, entity_fs)
        r = r1
        for _ in range(3):
            r = await sp.publish(db_session, entity_id=eid, rel_path="s", name="My site")
        assert r.site.revision == 4
        site_dir = root / ".sites" / r.site.id
        revs = sorted(p.name for p in site_dir.iterdir())
        assert revs == ["rev2", "rev3", "rev4"]  # keep last 3
        assert r.site.id == r1.site.id  # same source -> same site row

    async def test_broken_links_block_publish(self, db_session, entity_fs):
        root, eid = entity_fs
        _mk(root, "b/index.html", b'<img src="nope.png">')
        with pytest.raises(sp.SitePublishError, match="nope.png"):
            await sp.publish(db_session, entity_id=eid, rel_path="b", name="Broken")

    async def test_single_html_file_publish(self, db_session, entity_fs):
        root, eid = entity_fs
        _mk(root, "solo.html", b"<html>solo</html>")
        r = await sp.publish(db_session, entity_id=eid, rel_path="solo.html", name="Solo")
        rev_dir = root / ".sites" / r.site.id / "rev1"
        assert (rev_dir / "index.html").read_bytes() == b"<html>solo</html>"

    async def test_slugs_are_generated_and_distinct(self, db_session, entity_fs):
        root, eid = entity_fs
        _mk(root, "a/index.html", b"<html>a</html>")
        _mk(root, "b2/index.html", b"<html>b</html>")
        ra = await sp.publish(db_session, entity_id=eid, rel_path="a", name="Same Name")
        rb = await sp.publish(db_session, entity_id=eid, rel_path="b2", name="Same Name")
        assert ra.site.slug != rb.site.slug
        prefix = eid.lower()[:20]
        assert ra.site.slug.startswith(prefix)
        assert rb.site.slug.startswith(prefix)

    async def test_unpublishable_path_raises(self, db_session, entity_fs):
        root, eid = entity_fs
        _mk(root, "d.csv", b"a,b")
        with pytest.raises(sp.SitePublishError, match="index.html"):
            await sp.publish(db_session, entity_id=eid, rel_path="d.csv", name="Bad")
