import asyncio
import errno
import hashlib
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from packages.core.services.slide_renderer import (
    DOCUMENT_PAGE_RENDER_CACHE_VERSION,
    DOCUMENT_PAGE_RENDER_MAX_PAGES,
    FIRST_PAGE_RENDER_CACHE_VERSION,
    OfficeRenderLimitError,
    PRESENTATION_OBJECT_RENDER_CACHE_VERSION,
    SLIDE_RENDER_CACHE_VERSION,
    SLIDE_RENDER_MAX_PAGES,
    _build_presentation_object_variant,
    _combine_presentation_object_mattes,
    _document_page_render_lock_path,
    _prune_document_page_cache,
    _prune_presentation_object_version_cache,
    _render_document_pages_sync,
    _render_first_page_sync,
    _render_sync,
    _validate_presentation_object_render_size,
    open_cached_document_page,
    open_cached_slide,
    open_first_page,
    open_presentation_object,
    publish_current_preview_version,
    render_document_pages,
    render_slides,
)


def test_presentation_object_variant_isolates_and_normalizes_nested_graphic_frame(tmp_path):
    source = tmp_path / "source.pptx"
    destination = tmp_path / "isolated.pptx"
    presentation = '''<?xml version="1.0" encoding="UTF-8"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="rId1"/><p:sldId id="257" r:id="rId2"/></p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/></p:presentation>'''
    relationships = '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide2.xml"/></Relationships>'''
    slide = '''<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1"/></p:nvGrpSpPr><p:grpSpPr/><p:sp><p:nvSpPr><p:cNvPr id="2"/></p:nvSpPr></p:sp><p:grpSp><p:nvGrpSpPr><p:cNvPr id="10"/></p:nvGrpSpPr><p:grpSpPr/><p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="42"/></p:nvGraphicFramePr><p:xfrm rot="5400000"><a:off x="100000" y="200000"/><a:ext cx="3000000" cy="1800000"/></p:xfrm><a:graphic/></p:graphicFrame><p:sp><p:nvSpPr><p:cNvPr id="43"/></p:nvSpPr></p:sp></p:grpSp></p:spTree></p:cSld></p:sld>'''
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/_rels/presentation.xml.rels", relationships)
        archive.writestr("ppt/slides/slide1.xml", slide)
        archive.writestr("ppt/slides/slide2.xml", slide.replace('id="42"', 'id="52"'))

    _build_presentation_object_variant(
        str(source),
        str(destination),
        slide_index=0,
        object_id="42",
        background="000000",
    )

    with zipfile.ZipFile(destination) as archive:
        isolated_presentation = archive.read("ppt/presentation.xml").decode()
        isolated_slide = archive.read("ppt/slides/slide1.xml").decode()
    assert isolated_presentation.count("<p:sldId ") == 1
    assert 'cx="3000000"' in isolated_presentation
    assert 'cy="1800000"' in isolated_presentation
    assert 'id="42"' in isolated_slide
    assert 'id="2"' not in isolated_slide
    assert 'id="43"' not in isolated_slide
    assert 'showMasterSp="0"' in isolated_slide
    assert 'rot="5400000"' not in isolated_slide
    assert 'x="0"' in isolated_slide and 'y="0"' in isolated_slide
    assert 'val="000000"' in isolated_slide


def test_presentation_object_matte_removes_render_background(tmp_path):
    black_path = tmp_path / "black.png"
    white_path = tmp_path / "white.png"
    output_path = tmp_path / "object.png"
    Image.new("RGB", (1, 1), (100, 25, 0)).save(black_path)
    Image.new("RGB", (1, 1), (227, 152, 127)).save(white_path)

    _combine_presentation_object_mattes(str(black_path), str(white_path), str(output_path))

    red, green, blue, alpha = Image.open(output_path).convert("RGBA").getpixel((0, 0))
    assert abs(alpha - 128) <= 1
    assert abs(red - 199) <= 2
    assert abs(green - 50) <= 2
    assert blue <= 1


def test_presentation_object_render_size_is_bounded():
    assert _validate_presentation_object_render_size(12_192_000, 6_858_000, 192) == (2560, 1440)

    with pytest.raises(ValueError, match="render size limit"):
        _validate_presentation_object_render_size(120_000_000, 120_000_000, 192)


def test_presentation_object_matte_rejects_oversized_render(tmp_path, monkeypatch):
    black_path = tmp_path / "black.png"
    white_path = tmp_path / "white.png"
    output_path = tmp_path / "object.png"
    Image.new("RGB", (2, 1), (0, 0, 0)).save(black_path)
    Image.new("RGB", (2, 1), (255, 255, 255)).save(white_path)
    monkeypatch.setattr(
        "packages.core.services.slide_renderer.PRESENTATION_OBJECT_RENDER_MAX_PIXELS",
        1,
    )

    with pytest.raises(ValueError, match="size limit"):
        _combine_presentation_object_mattes(str(black_path), str(white_path), str(output_path))


@pytest.mark.asyncio
async def test_render_presentation_object_hashes_and_renders_one_snapshot(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "Proposal.pptx"
    source.write_bytes(b"first-version")
    cache_dir = tmp_path / ".slide-object-cache" / "document"
    rendered_snapshots: list[bytes] = []

    def fake_render(snapshot_path, object_dir, _slide_index, _object_id, _dpi):
        snapshot_bytes = Path(snapshot_path).read_bytes()
        rendered_snapshots.append(snapshot_bytes)
        source.write_bytes(b"second-version")
        target = Path(object_dir)
        target.mkdir(parents=True)
        output = target / "object.png"
        output.write_bytes(snapshot_bytes)
        return str(output)

    monkeypatch.setattr(
        "packages.core.services.slide_renderer._render_presentation_object_sync",
        fake_render,
    )

    opened = await open_presentation_object(
        str(source),
        str(cache_dir),
        slide_index=0,
        object_id="42",
    )
    try:
        expected_hash = hashlib.sha256()
        expected_hash.update(f"{PRESENTATION_OBJECT_RENDER_CACHE_VERSION}-192dpi".encode())
        expected_hash.update(b"first-version")
        assert rendered_snapshots == [b"first-version"]
        assert Path(opened.name).parents[1].name == expected_hash.hexdigest()[:16]
        assert opened.read() == b"first-version"
    finally:
        opened.close()


@pytest.mark.asyncio
async def test_render_presentation_object_deduplicates_same_version(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "Proposal.pptx"
    source.write_bytes(b"same-version")
    cache_dir = tmp_path / ".slide-object-cache" / "document"
    render_count = 0

    def fake_render(_snapshot_path, object_dir, _slide_index, _object_id, _dpi):
        nonlocal render_count
        render_count += 1
        time.sleep(0.05)
        target = Path(object_dir)
        target.mkdir(parents=True)
        output = target / "object.png"
        output.write_bytes(b"object")
        return str(output)

    monkeypatch.setattr(
        "packages.core.services.slide_renderer._render_presentation_object_sync",
        fake_render,
    )

    first, second = await asyncio.gather(
        open_presentation_object(str(source), str(cache_dir), slide_index=0, object_id="42"),
        open_presentation_object(str(source), str(cache_dir), slide_index=0, object_id="42"),
    )
    try:
        assert first.name == second.name
        assert render_count == 1
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_open_presentation_object_survives_cache_eviction(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "Proposal.pptx"
    source.write_bytes(b"same-version")
    cache_dir = tmp_path / ".slide-object-cache" / "document"

    def fake_render(_snapshot_path, object_dir, _slide_index, _object_id, _dpi):
        target = Path(object_dir)
        target.mkdir(parents=True)
        output = target / "object.png"
        output.write_bytes(b"object-stream")
        return str(output)

    def evict_current(_cache_dir, _version_dir, current_object_dir):
        shutil.rmtree(current_object_dir)

    monkeypatch.setattr(
        "packages.core.services.slide_renderer._render_presentation_object_sync",
        fake_render,
    )
    monkeypatch.setattr(
        "packages.core.services.slide_renderer._prune_presentation_object_cache",
        evict_current,
    )

    opened = await open_presentation_object(
        str(source),
        str(cache_dir),
        slide_index=0,
        object_id="42",
    )
    try:
        assert not Path(opened.name).exists()
        assert opened.read() == b"object-stream"
    finally:
        opened.close()


def test_render_lock_files_are_mapped_to_fixed_buckets(tmp_path):
    lock_dir = tmp_path / ".document-page-render-locks"
    lock_dir.mkdir()
    paths = {
        _document_page_render_lock_path(
            lock_dir,
            "presentation-object",
            "document",
            "0",
            str(object_id),
        )
        for object_id in range(10_000)
    }
    for path in paths:
        path.touch()

    assert len(paths) <= 64
    assert len(list(lock_dir.glob("render-*.lock"))) <= 64


@pytest.mark.asyncio
async def test_failed_presentation_object_versions_remain_bounded(tmp_path, monkeypatch):
    source = tmp_path / "Proposal.pptx"
    cache_dir = tmp_path / ".slide-object-cache" / "document"

    def failed_render(_snapshot_path, object_dir, _slide_index, _object_id, _dpi):
        Path(object_dir).parent.mkdir(parents=True, exist_ok=True)
        raise ValueError("Presentation object not found")

    monkeypatch.setattr(
        "packages.core.services.slide_renderer._render_presentation_object_sync",
        failed_render,
    )
    for version in range(3):
        source.write_bytes(f"version-{version}".encode())
        with pytest.raises(ValueError, match="not found"):
            await open_presentation_object(
                str(source),
                str(cache_dir),
                slide_index=0,
                object_id="999",
            )

    assert len([path for path in cache_dir.iterdir() if path.is_dir()]) == 2


@pytest.mark.asyncio
async def test_render_presentation_object_uses_shared_render_slots(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "Proposal.pptx"
    source.write_bytes(b"same-version")
    cache_dir = tmp_path / ".slide-object-cache" / "document"
    state_lock = threading.Lock()
    two_started = threading.Event()
    release = threading.Event()
    active = 0
    peak = 0

    def fake_render(_snapshot_path, object_dir, _slide_index, _object_id, _dpi):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
            if active >= 2:
                two_started.set()
        try:
            assert release.wait(2)
            target = Path(object_dir)
            target.mkdir(parents=True)
            output = target / "object.png"
            output.write_bytes(b"object")
            return str(output)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(
        "packages.core.services.slide_renderer._render_presentation_object_sync",
        fake_render,
    )
    tasks = [
        asyncio.create_task(open_presentation_object(
            str(source),
            str(cache_dir),
            slide_index=0,
            object_id=str(object_id),
        ))
        for object_id in (42, 43, 44)
    ]

    try:
        assert await asyncio.to_thread(two_started.wait, 1)
        await asyncio.sleep(0.05)
        with state_lock:
            assert peak == 2
    finally:
        release.set()
        opened = await asyncio.gather(*tasks)
        for handle in opened:
            handle.close()


def test_presentation_object_cache_is_bounded_per_source_version(tmp_path):
    version_dir = tmp_path / "0123456789abcdef"
    object_dirs = [
        version_dir / f"slide-0-object-{object_id}"
        for object_id in range(1, 5)
    ]
    for timestamp, object_dir in enumerate(object_dirs, start=1):
        object_dir.mkdir(parents=True)
        (object_dir / "object.png").write_bytes(b"1234")
        os.utime(object_dir, ns=(timestamp, timestamp))

    _prune_presentation_object_version_cache(
        str(version_dir),
        str(object_dirs[0]),
        max_objects=2,
        max_bytes=8,
    )

    assert object_dirs[0].is_dir()
    assert not object_dirs[1].exists()
    assert not object_dirs[2].exists()
    assert object_dirs[3].is_dir()


def test_render_first_page_copies_office_file_to_hint_extension_for_libreoffice(tmp_path, monkeypatch):
    source_path = tmp_path / "Personal Deck"
    source_path.write_bytes(b"PK\x03\x04pptx")
    out_dir = tmp_path / "thumbs"
    soffice_inputs: list[str] = []
    soffice_profiles: list[str] = []

    def fake_run(args, **kwargs):
        if args[0] == "soffice":
            assert callable(kwargs["preexec_fn"])
            profile_arg = next(arg for arg in args if arg.startswith("-env:UserInstallation=file://"))
            soffice_profiles.append(profile_arg)
            assert kwargs["env"]["HOME"] == args[args.index("--outdir") + 1]
            assert kwargs["env"]["XDG_CONFIG_HOME"].startswith(kwargs["env"]["HOME"])
            assert kwargs["env"]["XDG_CACHE_HOME"].startswith(kwargs["env"]["HOME"])
            soffice_input = args[-1]
            soffice_inputs.append(soffice_input)
            assert soffice_input.endswith(".pptx")
            assert Path(soffice_input).read_bytes() == source_path.read_bytes()
            pdf_path = Path(args[args.index("--outdir") + 1]) / f"{Path(soffice_input).stem}.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if args[0] == "pdfinfo":
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout="Pages: 1\nPage size: 960 x 540 pts\n",
            )
        if args[0] == "pdftoppm":
            output_prefix = Path(args[-1])
            output_prefix.with_name(f"{output_prefix.name}-1.jpg").write_bytes(b"jpeg")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr("packages.core.services.slide_renderer.subprocess.run", fake_run)

    rendered = _render_first_page_sync(str(source_path), str(out_dir), 150, source_ext=".pptx")

    assert Path(rendered).read_bytes() == b"jpeg"
    assert len(soffice_inputs) == 1
    assert len(soffice_profiles) == 1


def test_render_slides_uses_lossless_png(tmp_path, monkeypatch):
    source_path = tmp_path / "deck.pptx"
    source_path.write_bytes(b"PK\x03\x04pptx")
    out_dir = tmp_path / "slides"

    def fake_run(args, **kwargs):
        if args[0] == "soffice":
            assert callable(kwargs["preexec_fn"])
            pdf_path = Path(args[args.index("--outdir") + 1]) / "deck.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if args[0] == "pdfinfo":
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout="Pages: 3\nPage size: 960 x 540 pts\n",
            )
        if args[0] == "pdftoppm":
            assert "-png" in args
            assert "-jpeg" not in args
            page_number = int(args[args.index("-f") + 1])
            output_prefix = Path(args[-1])
            output_prefix.with_suffix(".png").write_bytes(f"png-{page_number}".encode())
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr("packages.core.services.slide_renderer.subprocess.run", fake_run)

    rendered = _render_sync(str(source_path), str(out_dir), 150)

    assert [Path(path).name for path in rendered] == [
        "slide-1.png",
        "slide-2.png",
        "slide-3.png",
    ]
    assert [Path(path).read_bytes() for path in rendered] == [b"png-1", b"png-2", b"png-3"]
    assert (out_dir / ".complete").read_text(encoding="utf-8") == "3"


def test_render_slides_rejects_decks_over_page_budget_before_rasterizing(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "oversized-deck.pptx"
    source_path.write_bytes(b"PK\x03\x04pptx")
    out_dir = tmp_path / "slides"
    rasterized = False

    def fake_run(args, **_kwargs):
        nonlocal rasterized
        if args[0] == "soffice":
            pdf_path = Path(args[args.index("--outdir") + 1]) / "oversized-deck.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if args[0] == "pdfinfo":
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=f"Pages: {SLIDE_RENDER_MAX_PAGES + 1}\n",
            )
        if args[0] == "pdftoppm":
            rasterized = True
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr("packages.core.services.slide_renderer.subprocess.run", fake_run)

    with pytest.raises(OfficeRenderLimitError, match="slide limit"):
        _render_sync(str(source_path), str(out_dir), 150)
    assert rasterized is False
    assert not (out_dir / ".complete").exists()


def test_render_slides_rejects_oversized_later_page_before_rasterizing(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "oversized-second-slide.pptx"
    source_path.write_bytes(b"PK\x03\x04pptx")
    out_dir = tmp_path / "slides"
    rasterized = False

    def fake_run(args, **_kwargs):
        nonlocal rasterized
        if args[0] == "soffice":
            pdf_path = Path(args[args.index("--outdir") + 1]) / "oversized-second-slide.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if args[0] == "pdfinfo" and "-f" not in args:
            return SimpleNamespace(returncode=0, stderr="", stdout="Pages: 2\n")
        if args[0] == "pdfinfo":
            assert "-box" in args
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=(
                    "Page    1 size: 960 x 540 pts\n"
                    "Page    2 size: 100000 x 100000 pts\n"
                ),
            )
        if args[0] == "pdftoppm":
            rasterized = True
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr("packages.core.services.slide_renderer.subprocess.run", fake_run)

    with pytest.raises(OfficeRenderLimitError, match="render size limit"):
        _render_sync(str(source_path), str(out_dir), 150)
    assert rasterized is False
    assert not (out_dir / ".complete").exists()


def test_render_slides_rejects_total_pixel_budget_before_rasterizing(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "too-many-total-pixels.pptx"
    source_path.write_bytes(b"PK\x03\x04pptx")
    out_dir = tmp_path / "slides"
    rasterized = False

    def fake_run(args, **_kwargs):
        nonlocal rasterized
        if args[0] == "soffice":
            pdf_path = Path(args[args.index("--outdir") + 1]) / "too-many-total-pixels.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if args[0] == "pdfinfo" and "-f" not in args:
            return SimpleNamespace(returncode=0, stderr="", stdout="Pages: 2\n")
        if args[0] == "pdfinfo":
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=(
                    "Page    1 size: 960 x 540 pts\n"
                    "Page    2 size: 960 x 540 pts\n"
                ),
            )
        if args[0] == "pdftoppm":
            rasterized = True
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr("packages.core.services.slide_renderer.subprocess.run", fake_run)
    monkeypatch.setattr(
        "packages.core.services.slide_renderer.SLIDE_RENDER_MAX_TOTAL_PIXELS",
        1,
    )

    with pytest.raises(OfficeRenderLimitError, match="total pixel limit"):
        _render_sync(str(source_path), str(out_dir), 150)
    assert rasterized is False
    assert not (out_dir / ".complete").exists()


def test_render_slides_stops_rasterizing_when_cumulative_output_is_too_large(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "large-output.pptx"
    source_path.write_bytes(b"PK\x03\x04pptx")
    out_dir = tmp_path / "slides"
    rasterized_pages: list[int] = []

    def fake_run(args, **_kwargs):
        if args[0] == "soffice":
            pdf_path = Path(args[args.index("--outdir") + 1]) / "large-output.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if args[0] == "pdfinfo" and "-f" not in args:
            return SimpleNamespace(returncode=0, stderr="", stdout="Pages: 3\n")
        if args[0] == "pdfinfo":
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout="Pages: 3\nPage size: 960 x 540 pts\n",
            )
        if args[0] == "pdftoppm":
            page_number = int(args[args.index("-f") + 1])
            rasterized_pages.append(page_number)
            Path(args[-1]).with_suffix(".png").write_bytes(b"1234")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr("packages.core.services.slide_renderer.subprocess.run", fake_run)
    monkeypatch.setattr("packages.core.services.slide_renderer.SLIDE_RENDER_MAX_BYTES", 5)

    with pytest.raises(OfficeRenderLimitError, match="rendered file size limit"):
        _render_sync(str(source_path), str(out_dir), 150)
    assert rasterized_pages == [1, 2]
    assert not (out_dir / ".complete").exists()


def test_render_first_page_rejects_oversized_page_before_rasterizing(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "oversized.pdf"
    source_path.write_bytes(b"%PDF-1.4")
    out_dir = tmp_path / "thumbnail"
    rasterized = False

    def fake_run(args, **_kwargs):
        nonlocal rasterized
        if args[0] == "pdfinfo":
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout="Pages: 1\nPage size: 100000 x 100000 pts\n",
            )
        if args[0] == "pdftoppm":
            rasterized = True
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr("packages.core.services.slide_renderer.subprocess.run", fake_run)

    with pytest.raises(OfficeRenderLimitError, match="render size limit"):
        _render_first_page_sync(
            str(source_path),
            str(out_dir),
            150,
            source_ext=".pdf",
        )
    assert rasterized is False


@pytest.mark.asyncio
async def test_open_first_page_pins_file_after_cache_path_is_removed(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"%PDF-1.4")
    cache_dir = tmp_path / "thumbnail-cache"

    def fake_render(_source, out_dir, _dpi, _source_ext):
        output = Path(out_dir) / "page-1.jpg"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"pinned-thumbnail")
        return str(output)

    monkeypatch.setattr(
        "packages.core.services.slide_renderer._render_first_page_sync",
        fake_render,
    )

    handle, output_path = await open_first_page(
        str(source_path),
        str(cache_dir),
        source_ext=".pdf",
    )
    shutil.rmtree(Path(output_path).parent)
    try:
        assert handle.read() == b"pinned-thumbnail"
    finally:
        handle.close()


@pytest.mark.asyncio
async def test_render_slides_hashes_and_renders_one_typed_snapshot(tmp_path, monkeypatch):
    source_path = tmp_path / "Legacy Deck"
    source_path.write_bytes(b"before-save")
    cache_dir = tmp_path / ".slide-cache" / "document"
    observed: dict[str, object] = {}

    def fake_render(snapshot_path, slide_dir, _dpi):
        observed["suffix"] = Path(snapshot_path).suffix
        observed["bytes"] = Path(snapshot_path).read_bytes()
        source_path.write_bytes(b"after-save")
        target = Path(slide_dir)
        target.mkdir(parents=True)
        output = target / "slide-1.png"
        output.write_bytes(observed["bytes"])
        return [str(output)]

    monkeypatch.setattr("packages.core.services.slide_renderer._render_sync", fake_render)

    rendered = await render_slides(
        str(source_path),
        str(cache_dir),
        source_ext=".ppt",
    )

    expected_hash = hashlib.sha256()
    expected_hash.update(f"{SLIDE_RENDER_CACHE_VERSION}-150dpi-ppt".encode())
    expected_hash.update(b"before-save")
    assert observed == {"suffix": ".ppt", "bytes": b"before-save"}
    assert Path(rendered[0]).parent.name == expected_hash.hexdigest()[:16]
    assert Path(rendered[0]).read_bytes() == b"before-save"
    assert source_path.read_bytes() == b"after-save"


@pytest.mark.asyncio
async def test_render_slides_separates_identical_bytes_by_source_format(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "Legacy Deck"
    source_path.write_bytes(b"same-container-bytes")
    cache_dir = tmp_path / ".slide-cache" / "document"
    observed_extensions: list[str] = []

    def fake_render(snapshot_path, slide_dir, _dpi):
        observed_extensions.append(Path(snapshot_path).suffix)
        target = Path(slide_dir)
        target.mkdir(parents=True)
        output = target / "slide-1.png"
        output.write_bytes(b"slide")
        (target / ".complete").write_text("1", encoding="utf-8")
        return [str(output)]

    monkeypatch.setattr("packages.core.services.slide_renderer._render_sync", fake_render)

    legacy = await render_slides(
        str(source_path),
        str(cache_dir),
        source_ext=".ppt",
    )
    modern = await render_slides(
        str(source_path),
        str(cache_dir),
        source_ext=".pptx",
    )

    assert observed_extensions == [".ppt", ".pptx"]
    assert Path(legacy[0]).parent != Path(modern[0]).parent


@pytest.mark.asyncio
async def test_render_slides_removes_incomplete_failed_cache(tmp_path, monkeypatch):
    source_path = tmp_path / "Deck.pptx"
    source_path.write_bytes(b"broken-version")
    cache_dir = tmp_path / ".slide-cache" / "document"

    def failed_render(_snapshot_path, slide_dir, _dpi):
        target = Path(slide_dir)
        target.mkdir(parents=True)
        (target / "slide-1.png").write_bytes(b"partial")
        raise RuntimeError("conversion failed")

    monkeypatch.setattr("packages.core.services.slide_renderer._render_sync", failed_render)

    with pytest.raises(RuntimeError, match="conversion failed"):
        await render_slides(str(source_path), str(cache_dir))

    assert not any(cache_dir.iterdir())


@pytest.mark.asyncio
async def test_open_cached_slide_pins_file_before_version_is_removed(tmp_path):
    source_path = tmp_path / "source.pptx"
    source_path.write_bytes(b"presentation")
    cache_dir = tmp_path / ".slide-cache" / "document"
    version_dir = cache_dir / "0123456789abcdef"
    version_dir.mkdir(parents=True)
    slide_path = version_dir / "slide-1.png"
    slide_path.write_bytes(b"pinned-slide")
    (version_dir / ".complete").write_text("1", encoding="utf-8")
    publish_current_preview_version(
        str(cache_dir),
        "0123456789abcdef",
        str(source_path),
    )

    handle = await open_cached_slide(
        str(cache_dir),
        "0123456789abcdef",
        0,
        str(source_path),
    )
    shutil.rmtree(version_dir)
    try:
        assert handle.read() == b"pinned-slide"
    finally:
        handle.close()


@pytest.mark.asyncio
async def test_open_cached_document_page_pins_file_before_version_is_removed(tmp_path):
    source_path = tmp_path / "source.docx"
    source_path.write_bytes(b"document")
    cache_dir = tmp_path / ".document-page-cache" / "document"
    version_dir = cache_dir / "0123456789abcdef"
    version_dir.mkdir(parents=True)
    page_path = version_dir / "page-1.png"
    page_path.write_bytes(b"pinned-page")
    (version_dir / ".complete").write_text("1", encoding="utf-8")
    publish_current_preview_version(
        str(cache_dir),
        "0123456789abcdef",
        str(source_path),
    )

    handle = await open_cached_document_page(
        str(cache_dir),
        "0123456789abcdef",
        0,
        str(source_path),
    )
    shutil.rmtree(version_dir)
    try:
        assert handle.read() == b"pinned-page"
    finally:
        handle.close()


@pytest.mark.asyncio
async def test_cached_preview_rejects_retained_noncurrent_version(tmp_path):
    source_path = tmp_path / "source.pptx"
    source_path.write_bytes(b"presentation")
    cache_dir = tmp_path / ".slide-cache" / "document"
    old_version_dir = cache_dir / "0123456789abcdef"
    old_version_dir.mkdir(parents=True)
    (old_version_dir / "slide-1.png").write_bytes(b"removed-content")
    (old_version_dir / ".complete").write_text("1", encoding="utf-8")
    publish_current_preview_version(
        str(cache_dir),
        "fedcba9876543210",
        str(source_path),
    )

    with pytest.raises(FileNotFoundError, match="Preview version not found"):
        await open_cached_slide(
            str(cache_dir),
            "0123456789abcdef",
            0,
            str(source_path),
        )


@pytest.mark.asyncio
async def test_cached_preview_rejects_replaced_source_without_explicit_invalidation(
    tmp_path,
):
    source_path = tmp_path / "source.pptx"
    source_path.write_bytes(b"before")
    cache_dir = tmp_path / ".slide-cache" / "document"
    version_dir = cache_dir / "0123456789abcdef"
    version_dir.mkdir(parents=True)
    (version_dir / "slide-1.png").write_bytes(b"stale-preview")
    (version_dir / ".complete").write_text("1", encoding="utf-8")
    publish_current_preview_version(
        str(cache_dir),
        "0123456789abcdef",
        str(source_path),
    )

    replacement = tmp_path / "replacement.pptx"
    replacement.write_bytes(b"after-with-a-different-size")
    os.replace(replacement, source_path)

    with pytest.raises(FileNotFoundError, match="Preview version not found"):
        await open_cached_slide(
            str(cache_dir),
            "0123456789abcdef",
            0,
            str(source_path),
        )


@pytest.mark.asyncio
async def test_open_cached_slide_closes_handle_when_request_is_cancelled(
    tmp_path,
    monkeypatch,
):
    cache_dir = tmp_path / ".slide-cache" / "document"
    slide_path = tmp_path / "slide.png"
    slide_path.write_bytes(b"slide")
    started = threading.Event()
    release = threading.Event()
    opened: dict[str, object] = {}

    def blocked_open(_cache_dir, _version, _slide_index, _source_path):
        started.set()
        assert release.wait(2)
        handle = slide_path.open("rb")
        opened["handle"] = handle
        return handle

    monkeypatch.setattr(
        "packages.core.services.slide_renderer._open_cached_slide_sync",
        blocked_open,
    )
    task = asyncio.create_task(open_cached_slide(
        str(cache_dir),
        "0123456789abcdef",
        0,
        str(slide_path),
    ))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert opened["handle"].closed


@pytest.mark.asyncio
async def test_render_first_page_hashes_and_renders_one_snapshot(tmp_path, monkeypatch):
    source_path = tmp_path / "Proposal"
    source_path.write_bytes(b"before-save")
    cache_dir = tmp_path / ".doc-thumb-cache" / "document"
    observed: dict[str, object] = {}

    def fake_render(snapshot_path, out_dir, _dpi, source_ext):
        observed["suffix"] = Path(snapshot_path).suffix
        observed["source_ext"] = source_ext
        observed["bytes"] = Path(snapshot_path).read_bytes()
        source_path.write_bytes(b"after-save")
        target = Path(out_dir)
        target.mkdir(parents=True)
        output = target / "page-1.jpg"
        output.write_bytes(observed["bytes"])
        return str(output)

    monkeypatch.setattr(
        "packages.core.services.slide_renderer._render_first_page_sync",
        fake_render,
    )

    rendered_file, rendered = await open_first_page(
        str(source_path),
        str(cache_dir),
        source_ext=".docx",
    )
    try:
        expected_hash = hashlib.sha256()
        expected_hash.update(f"{FIRST_PAGE_RENDER_CACHE_VERSION}-150dpi-docx".encode())
        expected_hash.update(b"before-save")
        assert observed == {
            "suffix": ".docx",
            "source_ext": ".docx",
            "bytes": b"before-save",
        }
        assert Path(rendered).parent.name == expected_hash.hexdigest()[:16]
        assert rendered_file.read() == b"before-save"
        assert source_path.read_bytes() == b"after-save"
    finally:
        rendered_file.close()


def test_render_document_pages_publishes_complete_lossless_cache(tmp_path, monkeypatch):
    source_path = tmp_path / "Service Proposal"
    source_path.write_bytes(b"PK\x03\x04docx")
    page_dir = tmp_path / "pages" / "content-hash"
    command_count = 0

    def fake_run(args, **kwargs):
        nonlocal command_count
        command_count += 1
        if args[0] == "soffice":
            assert callable(kwargs["preexec_fn"])
            soffice_input = Path(args[-1])
            assert soffice_input.suffix == ".docx"
            assert soffice_input.read_bytes() == source_path.read_bytes()
            pdf_path = Path(args[args.index("--outdir") + 1]) / f"{soffice_input.stem}.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if args[0] == "pdfinfo" and "-f" not in args:
            return SimpleNamespace(returncode=0, stderr="", stdout="Pages: 3\n")
        if args[0] == "pdfinfo":
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout="Pages: 3\nPage size: 960 x 540 pts\n",
            )
        if args[0] == "pdftoppm":
            assert "-png" in args
            page_number = int(args[args.index("-f") + 1])
            output_prefix = Path(args[-1])
            output_prefix.with_suffix(".png").write_bytes(f"page-{page_number}".encode())
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr("packages.core.services.slide_renderer.subprocess.run", fake_run)

    rendered = _render_document_pages_sync(
        str(source_path),
        str(page_dir),
        150,
        source_ext=".docx",
    )

    assert [Path(path).name for path in rendered] == ["page-1.png", "page-2.png", "page-3.png"]
    assert [Path(path).read_bytes() for path in rendered] == [b"page-1", b"page-2", b"page-3"]
    assert (page_dir / ".complete").read_text(encoding="utf-8") == "3"

    rendered_again = _render_document_pages_sync(
        str(source_path),
        str(page_dir),
        150,
        source_ext=".docx",
    )
    assert rendered_again == rendered
    assert command_count == 6


def test_render_document_pages_accepts_concurrent_macos_cache_publish(tmp_path, monkeypatch):
    source_path = tmp_path / "Concurrent Proposal.docx"
    source_path.write_bytes(b"PK\x03\x04docx")
    page_dir = tmp_path / "pages" / "content-hash"

    def fake_run(args, **_kwargs):
        if args[0] == "soffice":
            soffice_input = Path(args[-1])
            pdf_path = Path(args[args.index("--outdir") + 1]) / f"{soffice_input.stem}.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if args[0] == "pdfinfo" and "-f" not in args:
            return SimpleNamespace(returncode=0, stderr="", stdout="Pages: 1\n")
        if args[0] == "pdfinfo":
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout="Pages: 1\nPage size: 960 x 540 pts\n",
            )
        if args[0] == "pdftoppm":
            output_prefix = Path(args[-1])
            output_prefix.with_suffix(".png").write_bytes(b"local-page")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        raise AssertionError(f"unexpected command: {args}")

    def concurrent_publish(_source, destination):
        destination_path = Path(destination)
        destination_path.mkdir(parents=True)
        (destination_path / "page-1.png").write_bytes(b"winning-page")
        (destination_path / ".complete").write_text("1", encoding="utf-8")
        raise OSError(errno.ENOTEMPTY, "Directory not empty", destination)

    monkeypatch.setattr("packages.core.services.slide_renderer.subprocess.run", fake_run)
    monkeypatch.setattr("packages.core.services.slide_renderer.os.rename", concurrent_publish)

    rendered = _render_document_pages_sync(
        str(source_path),
        str(page_dir),
        150,
        source_ext=".docx",
    )

    assert rendered == [str(page_dir / "page-1.png")]
    assert Path(rendered[0]).read_bytes() == b"winning-page"


def test_render_document_pages_rejects_documents_over_page_budget(tmp_path, monkeypatch):
    source_path = tmp_path / "Long Proposal.docx"
    source_path.write_bytes(b"PK\x03\x04docx")
    page_dir = tmp_path / "pages" / "content-hash"

    def fake_run(args, **_kwargs):
        if args[0] == "soffice":
            soffice_input = Path(args[-1])
            pdf_path = Path(args[args.index("--outdir") + 1]) / f"{soffice_input.stem}.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")
            return SimpleNamespace(returncode=0, stderr="", stdout="")
        if args[0] == "pdfinfo":
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=f"Pages: {DOCUMENT_PAGE_RENDER_MAX_PAGES + 1}\n",
            )
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr("packages.core.services.slide_renderer.subprocess.run", fake_run)

    with pytest.raises(OfficeRenderLimitError, match="page limit"):
        _render_document_pages_sync(
            str(source_path),
            str(page_dir),
            150,
            source_ext=".docx",
        )
    assert not page_dir.exists()


@pytest.mark.asyncio
async def test_render_document_pages_hashes_and_renders_one_snapshot(tmp_path, monkeypatch):
    source_path = tmp_path / "Changing Proposal.docx"
    source_path.write_bytes(b"before-save")
    cache_dir = tmp_path / "pages"
    observed: dict[str, object] = {}

    def fake_hash(path, version):
        observed["hash_path"] = path
        observed["hash_bytes"] = Path(path).read_bytes()
        observed["hash_version"] = version
        source_path.write_bytes(b"after-save")
        return "0123456789abcdef"

    def fake_render(path, page_dir, _dpi, _source_ext):
        observed["render_path"] = path
        observed["render_bytes"] = Path(path).read_bytes()
        target = Path(page_dir)
        target.mkdir(parents=True)
        (target / "page-1.png").write_bytes(b"page")
        (target / ".complete").write_text("1", encoding="utf-8")
        return [str(target / "page-1.png")]

    monkeypatch.setattr("packages.core.services.slide_renderer._file_hash", fake_hash)
    monkeypatch.setattr(
        "packages.core.services.slide_renderer._render_document_pages_sync",
        fake_render,
    )

    rendered = await render_document_pages(str(source_path), str(cache_dir))

    assert rendered == [str(cache_dir / "0123456789abcdef" / "page-1.png")]
    assert observed["hash_path"] == observed["render_path"]
    assert observed["hash_bytes"] == observed["render_bytes"] == b"before-save"
    assert observed["hash_version"] == (
        f"{DOCUMENT_PAGE_RENDER_CACHE_VERSION}-192dpi-docx"
    )
    assert source_path.read_bytes() == b"after-save"


@pytest.mark.asyncio
async def test_render_document_pages_coalesces_same_version(tmp_path, monkeypatch):
    source_path = tmp_path / "Concurrent Proposal.docx"
    source_path.write_bytes(b"same-version")
    cache_dir = tmp_path / "pages"
    render_count = 0

    def fake_render(_path, page_dir, _dpi, _source_ext):
        nonlocal render_count
        render_count += 1
        time.sleep(0.05)
        target = Path(page_dir)
        target.mkdir(parents=True)
        (target / "page-1.png").write_bytes(b"page")
        (target / ".complete").write_text("1", encoding="utf-8")
        return [str(target / "page-1.png")]

    monkeypatch.setattr(
        "packages.core.services.slide_renderer._render_document_pages_sync",
        fake_render,
    )

    first, second = await asyncio.gather(
        render_document_pages(str(source_path), str(cache_dir)),
        render_document_pages(str(source_path), str(cache_dir)),
    )

    assert first == second
    assert render_count == 1


@pytest.mark.asyncio
async def test_render_document_pages_bounds_different_version_concurrency(
    tmp_path,
    monkeypatch,
):
    sources = []
    for index in range(3):
        source = tmp_path / f"Proposal {index}.docx"
        source.write_bytes(f"version-{index}".encode())
        sources.append(source)
    cache_root = tmp_path / ".document-page-cache"
    state_lock = threading.Lock()
    two_started = threading.Event()
    release = threading.Event()
    active = 0
    peak = 0

    def fake_render(_path, page_dir, _dpi, _source_ext):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
            if active >= 2:
                two_started.set()
        try:
            assert release.wait(2)
            target = Path(page_dir)
            target.mkdir(parents=True)
            (target / "page-1.png").write_bytes(b"page")
            (target / ".complete").write_text("1", encoding="utf-8")
            return [str(target / "page-1.png")]
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(
        "packages.core.services.slide_renderer._render_document_pages_sync",
        fake_render,
    )
    tasks = [
        asyncio.create_task(render_document_pages(
            str(source),
            str(cache_root / f"document-{index}"),
        ))
        for index, source in enumerate(sources)
    ]

    try:
        assert await asyncio.to_thread(two_started.wait, 1)
        await asyncio.sleep(0.05)
        with state_lock:
            assert peak == 2
    finally:
        release.set()
        await asyncio.gather(*tasks)


def test_document_page_cache_retains_current_and_one_recent_version(tmp_path):
    cache_dir = tmp_path / "pages"
    versions = [
        cache_dir / "0000000000000000",
        cache_dir / "1111111111111111",
        cache_dir / "2222222222222222",
    ]
    for timestamp, version in enumerate(versions, start=1):
        version.mkdir(parents=True)
        (version / ".complete").write_text("1", encoding="utf-8")
        (version / "page-1.png").write_bytes(b"page")
        os.utime(version, ns=(timestamp, timestamp))

    _prune_document_page_cache(str(cache_dir), str(versions[0]))

    assert versions[0].is_dir()
    assert not versions[1].exists()
    assert versions[2].is_dir()


def test_document_page_cache_retains_recently_leased_version(tmp_path):
    cache_dir = tmp_path / "pages"
    old_version = cache_dir / "0000000000000000"
    leased_version = cache_dir / "1111111111111111"
    current_version = cache_dir / "2222222222222222"
    for version in (old_version, leased_version, current_version):
        version.mkdir(parents=True)
        (version / ".complete").write_text("1", encoding="utf-8")
        (version / "page-1.png").write_bytes(b"page")
    old_ns = 1
    recent_ns = time.time_ns()
    os.utime(old_version, ns=(old_ns, old_ns))
    os.utime(leased_version, ns=(recent_ns, recent_ns))
    os.utime(current_version, ns=(recent_ns, recent_ns))

    _prune_document_page_cache(str(cache_dir), str(current_version))

    assert not old_version.exists()
    assert leased_version.is_dir()
    assert current_version.is_dir()


def test_document_page_cache_hard_caps_recently_leased_versions(tmp_path):
    cache_dir = tmp_path / "pages"
    versions = [cache_dir / f"{index:016x}" for index in range(8)]
    recent_ns = time.time_ns()
    for version in versions:
        version.mkdir(parents=True)
        (version / ".complete").write_text("1", encoding="utf-8")
        (version / "page-1.png").write_bytes(b"page")
        os.utime(version, ns=(recent_ns, recent_ns))

    _prune_document_page_cache(
        str(cache_dir),
        str(versions[-1]),
        max_versions=2,
    )

    assert sum(version.is_dir() for version in versions) == 2
    assert versions[-1].is_dir()


@pytest.mark.asyncio
async def test_render_document_pages_admits_before_copying_snapshots(
    tmp_path,
    monkeypatch,
):
    sources = []
    for index in range(3):
        source = tmp_path / f"Snapshot {index}.docx"
        source.write_bytes(f"snapshot-{index}".encode())
        sources.append(source)
    cache_root = tmp_path / ".document-page-cache"
    state_lock = threading.Lock()
    two_started = threading.Event()
    release = threading.Event()
    active = 0
    peak = 0

    def blocked_copy(source, destination):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
            if active >= 2:
                two_started.set()
        try:
            assert release.wait(2)
            Path(destination).write_bytes(Path(source).read_bytes())
        finally:
            with state_lock:
                active -= 1

    def fake_render(_path, page_dir, _dpi, _source_ext):
        target = Path(page_dir)
        target.mkdir(parents=True)
        (target / "page-1.png").write_bytes(b"page")
        (target / ".complete").write_text("1", encoding="utf-8")
        return [str(target / "page-1.png")]

    monkeypatch.setattr(
        "packages.core.services.slide_renderer.shutil.copy2",
        blocked_copy,
    )
    monkeypatch.setattr(
        "packages.core.services.slide_renderer._render_document_pages_sync",
        fake_render,
    )
    tasks = [
        asyncio.create_task(render_document_pages(
            str(source),
            str(cache_root / f"document-{index}"),
        ))
        for index, source in enumerate(sources)
    ]

    try:
        assert await asyncio.to_thread(two_started.wait, 1)
        await asyncio.sleep(0.05)
        with state_lock:
            assert peak == 2
    finally:
        release.set()
        await asyncio.gather(*tasks)
