"""Render Office documents as images using LibreOffice + pdftoppm.

Converts binary PPTX and DOCX files to lossless PNG images. Results are cached
on the filesystem to avoid re-rendering.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import math
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Callable, TypeVar

from defusedxml import ElementTree as DefusedET
from PIL import Image

from packages.core.services.office_process import office_conversion_file_limit


logger = logging.getLogger(__name__)


# Bump when rendering dependencies or font substitution rules change so stale
# previews are not served from an older, visually incompatible cache.
SLIDE_RENDER_CACHE_VERSION = "v5-complete-lossless-png-symbol-fonts"
SLIDE_CACHE_MAX_VERSIONS = 2
SLIDE_RENDER_MAX_PAGES = 200
SLIDE_RENDER_MAX_BYTES = 256 * 1024 * 1024
SLIDE_CONVERSION_MAX_FILE_BYTES = 512 * 1024 * 1024
SLIDE_RENDER_MAX_PIXELS = 16_000_000
SLIDE_RENDER_MAX_TOTAL_PIXELS = 512_000_000
SLIDE_RENDER_MAX_DIMENSION = 8192
FIRST_PAGE_RENDER_CACHE_VERSION = "v2-first-page-snapshot-bounded"
FIRST_PAGE_CACHE_MAX_VERSIONS = 2
FIRST_PAGE_RENDER_MAX_BYTES = 32 * 1024 * 1024
FIRST_PAGE_CONVERSION_MAX_FILE_BYTES = 128 * 1024 * 1024
FIRST_PAGE_RENDER_MAX_PIXELS = 16_000_000
FIRST_PAGE_RENDER_MAX_DIMENSION = 8192
PRESENTATION_OBJECT_RENDER_CACHE_VERSION = "v2-isolated-transparent-object-bounded"
PRESENTATION_OBJECT_CACHE_MAX_VERSIONS = 2
PRESENTATION_OBJECT_CACHE_MAX_OBJECTS = 64
PRESENTATION_OBJECT_CACHE_MAX_BYTES = 256 * 1024 * 1024
PRESENTATION_OBJECT_RENDER_MAX_PIXELS = 8_000_000
PRESENTATION_OBJECT_RENDER_MAX_DIMENSION = 8192
PRESENTATION_OBJECT_MATTE_CHUNK_PIXELS = 262_144
DOCUMENT_PAGE_RENDER_CACHE_VERSION = "v2-lossless-png-complete-symbol-fonts"
DOCUMENT_PAGE_RENDER_MAX_PAGES = 200
DOCUMENT_PAGE_RENDER_MAX_BYTES = 256 * 1024 * 1024
DOCUMENT_PAGE_CONVERSION_MAX_FILE_BYTES = 512 * 1024 * 1024
DOCUMENT_PAGE_RENDER_MAX_PIXELS = 20_000_000
DOCUMENT_PAGE_RENDER_MAX_TOTAL_PIXELS = 512_000_000
DOCUMENT_PAGE_RENDER_MAX_DIMENSION = 8192
DOCUMENT_PAGE_CACHE_MAX_VERSIONS = 2
DOCUMENT_PAGE_RENDER_MAX_CONCURRENCY = 2
_DOCUMENT_PAGE_CACHE_KEY_RE = re.compile(r"^[0-9a-f]{16}$")
_CURRENT_PREVIEW_VERSION_FILE = ".current"
_PRESENTATION_OBJECT_CACHE_DIR_RE = re.compile(r"^slide-[0-9]+-object-[0-9]+$")
_DOCUMENT_PAGE_RENDER_LOCK_RETRY_SECONDS = 0.02
_DOCUMENT_PAGE_RENDER_LOCK_BUCKETS = 64
_ThreadResult = TypeVar("_ThreadResult")


class PresentationObjectRenderLimitError(ValueError):
    """Raised when a presentation object would exceed preview resource limits."""


class OfficeRenderLimitError(ValueError):
    """Raised when an Office preview would exceed its rendering budget."""


async def render_slides(
    pptx_path: str,
    cache_dir: str,
    *,
    dpi: int = 150,
    source_ext: str | None = ".pptx",
) -> list[str]:
    """Convert PPTX to per-slide lossless PNG images.

    Returns a list of absolute paths to the rendered slide images,
    ordered by slide number. Uses a content-hash based cache so
    unchanged files aren't re-rendered.
    """
    if not os.path.isfile(pptx_path):
        raise FileNotFoundError(f"PPTX not found: {pptx_path}")

    ext = _normalize_source_ext(source_ext) or os.path.splitext(pptx_path)[1].lower() or ".pptx"
    lock_dir = await _run_document_page_thread(
        _document_page_render_lock_dir,
        cache_dir,
    )
    async with _document_page_named_lock(
        _document_page_render_lock_path(lock_dir, "slides", os.path.abspath(cache_dir)),
    ):
        async with _document_page_render_slot(lock_dir):
            with tempfile.TemporaryDirectory(prefix="manor-slide-snapshot-") as snapshot_dir:
                snapshot_path = os.path.join(snapshot_dir, f"presentation{ext}")
                cache_key = await _run_document_page_thread(
                    _snapshot_file_with_hash,
                    pptx_path,
                    snapshot_path,
                    _render_cache_salt(SLIDE_RENDER_CACHE_VERSION, dpi, ext),
                )
                slide_dir = os.path.join(cache_dir, cache_key)
                existing = _get_cached_slides(slide_dir)
                if existing:
                    await _run_document_page_thread(touch_document_page_cache, slide_dir)
                    await _run_document_page_thread(
                        _prune_document_page_cache,
                        cache_dir,
                        slide_dir,
                        max_versions=SLIDE_CACHE_MAX_VERSIONS,
                    )
                    return existing
                try:
                    rendered = await _run_document_page_thread(
                        _render_sync,
                        snapshot_path,
                        slide_dir,
                        dpi,
                    )
                except BaseException:
                    await _run_document_page_thread(_remove_incomplete_slide_cache, slide_dir)
                    raise
                await _run_document_page_thread(
                    _prune_document_page_cache,
                    cache_dir,
                    slide_dir,
                    max_versions=SLIDE_CACHE_MAX_VERSIONS,
                )
                return rendered


async def open_cached_slide(
    cache_dir: str,
    version: str,
    slide_index: int,
    source_path: str,
) -> BinaryIO:
    """Open one source-bound cached slide without waiting for a renderer."""
    return await _open_cached_slide_file(
        cache_dir,
        version,
        slide_index,
        source_path,
    )


async def _open_cached_slide_file(
    cache_dir: str,
    version: str,
    slide_index: int,
    source_path: str,
) -> BinaryIO:
    task = asyncio.create_task(asyncio.to_thread(
        _open_cached_slide_sync,
        cache_dir,
        version,
        slide_index,
        source_path,
    ))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            handle = await task
        except BaseException:
            pass
        else:
            handle.close()
        raise


async def open_cached_document_page(
    cache_dir: str,
    version: str,
    page_index: int,
    source_path: str,
) -> BinaryIO:
    """Open one source-bound Word page without waiting for a renderer."""
    return await _open_cached_document_page_file(
        cache_dir,
        version,
        page_index,
        source_path,
    )


async def _open_cached_document_page_file(
    cache_dir: str,
    version: str,
    page_index: int,
    source_path: str,
) -> BinaryIO:
    task = asyncio.create_task(asyncio.to_thread(
        _open_cached_document_page_sync,
        cache_dir,
        version,
        page_index,
        source_path,
    ))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            handle = await task
        except BaseException:
            pass
        else:
            handle.close()
        raise


async def open_presentation_object(
    pptx_path: str,
    cache_dir: str,
    *,
    slide_index: int,
    object_id: str,
    dpi: int = 192,
) -> BinaryIO:
    """Render and open one object before cache eviction can unlink it."""
    return await _open_presentation_object(
        pptx_path,
        cache_dir,
        slide_index=slide_index,
        object_id=object_id,
        dpi=dpi,
    )


async def _open_presentation_object(
    pptx_path: str,
    cache_dir: str,
    *,
    slide_index: int,
    object_id: str,
    dpi: int,
) -> BinaryIO:
    if not os.path.isfile(pptx_path):
        raise FileNotFoundError(f"PPTX not found: {pptx_path}")
    if slide_index < 0 or not re.fullmatch(r"[0-9]+", object_id):
        raise ValueError("Invalid presentation object reference")
    lock_dir = await _run_document_page_thread(
        _document_page_render_lock_dir,
        cache_dir,
    )
    async with _document_page_named_lock(
        _document_page_render_lock_path(
            lock_dir,
            "presentation-object",
            os.path.abspath(cache_dir),
            str(slide_index),
            object_id,
        ),
    ):
        async with _document_page_render_slot(lock_dir):
            with tempfile.TemporaryDirectory(
                prefix="manor-presentation-object-snapshot-",
            ) as snapshot_dir:
                snapshot_path = os.path.join(snapshot_dir, "presentation.pptx")
                cache_key = await _run_document_page_thread(
                    _snapshot_file_with_hash,
                    pptx_path,
                    snapshot_path,
                    f"{PRESENTATION_OBJECT_RENDER_CACHE_VERSION}-{dpi}dpi",
                )
                version_dir = os.path.join(cache_dir, cache_key)
                object_dir = os.path.join(
                    version_dir,
                    f"slide-{slide_index}-object-{object_id}",
                )
                output_path = os.path.join(object_dir, "object.png")
                if os.path.isfile(output_path):
                    await _run_document_page_thread(
                        _touch_presentation_object_cache,
                        version_dir,
                        object_dir,
                    )
                    return await _open_presentation_object_cache_file(
                        output_path,
                        cache_dir,
                        version_dir,
                        object_dir,
                    )
                try:
                    rendered = await _run_document_page_thread(
                        _render_presentation_object_sync,
                        snapshot_path,
                        object_dir,
                        slide_index,
                        object_id,
                        dpi,
                    )
                except BaseException:
                    await _run_document_page_thread(
                        _prune_presentation_object_cache,
                        cache_dir,
                        version_dir,
                        object_dir,
                    )
                    raise
                await _run_document_page_thread(
                    _touch_presentation_object_cache,
                    version_dir,
                    object_dir,
                )
                return await _open_presentation_object_cache_file(
                    rendered,
                    cache_dir,
                    version_dir,
                    object_dir,
                )


async def render_document_pages(
    file_path: str,
    cache_dir: str,
    *,
    dpi: int = 192,
    source_ext: str | None = ".docx",
) -> list[str]:
    """Convert a Word document to ordered, lossless page images.

    The content-addressed cache is published only after every page is rendered,
    so a failed conversion can never be mistaken for a complete document.
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Document not found: {file_path}")

    ext = _normalize_source_ext(source_ext) or os.path.splitext(file_path)[1].lower() or ".docx"
    lock_dir = await _run_document_page_thread(
        _document_page_render_lock_dir,
        cache_dir,
    )
    async with _document_page_named_lock(
        _document_page_render_lock_path(lock_dir, "document", os.path.abspath(cache_dir)),
    ):
        async with _document_page_render_slot(lock_dir):
            with tempfile.TemporaryDirectory(prefix="manor-document-snapshot-") as snapshot_dir:
                snapshot_path = os.path.join(snapshot_dir, f"document{ext}")
                await _run_document_page_thread(
                    shutil.copy2,
                    file_path,
                    snapshot_path,
                )
                file_hash = await _run_document_page_thread(
                    _file_hash,
                    snapshot_path,
                    _render_cache_salt(DOCUMENT_PAGE_RENDER_CACHE_VERSION, dpi, ext),
                )
                page_dir = os.path.join(cache_dir, file_hash)
                existing = _get_cached_document_pages(page_dir)
                if existing:
                    await _run_document_page_thread(
                        touch_document_page_cache,
                        page_dir,
                    )
                    await _run_document_page_thread(
                        _prune_document_page_cache,
                        cache_dir,
                        page_dir,
                    )
                    return existing

                rendered = await _run_document_page_thread(
                    _render_document_pages_sync,
                    snapshot_path,
                    page_dir,
                    dpi,
                    ext,
                )
                await _run_document_page_thread(
                    _prune_document_page_cache,
                    cache_dir,
                    page_dir,
                )
                return rendered


async def _run_document_page_thread(
    function: Callable[..., _ThreadResult],
    *args,
    _release_result: Callable[[_ThreadResult], None] | None = None,
    **kwargs,
) -> _ThreadResult:
    """Keep temporary inputs and admission locks alive until thread work stops."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            result = await task
        except BaseException:
            pass
        else:
            if _release_result is not None:
                try:
                    _release_result(result)
                except Exception:
                    logger.exception(
                        "Office render result cleanup failed while cancellation was pending",
                    )
        raise


async def run_office_render_thread(
    cache_dir: str,
    lock_identity: str,
    function: Callable[..., _ThreadResult],
    *args,
    release_result: Callable[[_ThreadResult], None] | None = None,
) -> _ThreadResult:
    """Run one external Office conversion under shared cross-process admission."""
    lock_dir = await _run_document_page_thread(
        _document_page_render_lock_dir,
        cache_dir,
    )
    async with _document_page_named_lock(
        _document_page_render_lock_path(lock_dir, "office-conversion", lock_identity),
    ):
        async with _document_page_render_slot(lock_dir):
            return await _run_document_page_thread(
                function,
                *args,
                _release_result=release_result,
            )


def _open_and_prune_presentation_object_cache_file(
    output_path: str,
    cache_dir: str,
    version_dir: str,
    object_dir: str,
) -> BinaryIO:
    handle = open(output_path, "rb")
    try:
        _prune_presentation_object_cache(cache_dir, version_dir, object_dir)
    except BaseException:
        handle.close()
        raise
    return handle


async def _open_presentation_object_cache_file(
    output_path: str,
    cache_dir: str,
    version_dir: str,
    object_dir: str,
) -> BinaryIO:
    task = asyncio.create_task(asyncio.to_thread(
        _open_and_prune_presentation_object_cache_file,
        output_path,
        cache_dir,
        version_dir,
        object_dir,
    ))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            handle = await task
        except BaseException:
            pass
        else:
            handle.close()
        raise


def _document_page_render_lock_dir(cache_dir: str) -> Path:
    cache_path = Path(cache_dir).resolve()
    cache_container_names = {
        ".doc-thumb-cache",
        ".document-page-cache",
        ".office-editable-cache",
        ".slide-cache",
        ".slide-object-cache",
    }
    lock_root = cache_path.parent
    for ancestor in (cache_path, *cache_path.parents):
        if ancestor.name in cache_container_names and len(ancestor.parents) >= 2:
            lock_root = ancestor.parents[1]
            break
    lock_dir = lock_root / ".document-page-render-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


def _document_page_render_lock_path(lock_dir: Path, *identity: str) -> Path:
    """Map unbounded document/object identities onto a fixed set of lock files."""
    digest = hashlib.sha256("\0".join(identity).encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % _DOCUMENT_PAGE_RENDER_LOCK_BUCKETS
    return lock_dir / f"render-{bucket}.lock"


def _try_document_page_file_lock(path: Path) -> BinaryIO | None:
    handle = path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except Exception:
        handle.close()
        raise
    return handle


def _release_document_page_file_lock(handle: BinaryIO) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


async def _acquire_document_page_file_lock(path: Path) -> BinaryIO:
    while True:
        handle = _try_document_page_file_lock(path)
        if handle is not None:
            return handle
        await asyncio.sleep(_DOCUMENT_PAGE_RENDER_LOCK_RETRY_SECONDS)


@asynccontextmanager
async def _document_page_named_lock(path: Path) -> AsyncIterator[None]:
    handle = await _acquire_document_page_file_lock(path)
    try:
        yield
    finally:
        _release_document_page_file_lock(handle)


@asynccontextmanager
async def _document_page_render_slot(lock_dir: Path) -> AsyncIterator[None]:
    handle: BinaryIO | None = None
    while handle is None:
        for index in range(DOCUMENT_PAGE_RENDER_MAX_CONCURRENCY):
            handle = _try_document_page_file_lock(lock_dir / f"slot-{index}.lock")
            if handle is not None:
                break
        if handle is None:
            await asyncio.sleep(_DOCUMENT_PAGE_RENDER_LOCK_RETRY_SECONDS)
    try:
        yield
    finally:
        _release_document_page_file_lock(handle)


async def open_first_page(
    file_path: str,
    cache_dir: str,
    *,
    dpi: int = 150,
    source_ext: str | None = None,
) -> tuple[BinaryIO, str]:
    """Render and open a first page before another version can prune it."""
    return await _open_first_page(
        file_path,
        cache_dir,
        dpi=dpi,
        source_ext=source_ext,
    )


async def _open_first_page(
    file_path: str,
    cache_dir: str,
    *,
    dpi: int,
    source_ext: str | None,
) -> tuple[BinaryIO, str]:
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = _normalize_source_ext(source_ext) or os.path.splitext(file_path)[1].lower() or ".pdf"
    lock_dir = await _run_document_page_thread(
        _document_page_render_lock_dir,
        cache_dir,
    )
    async with _document_page_named_lock(
        _document_page_render_lock_path(lock_dir, "first-page", os.path.abspath(cache_dir)),
    ):
        async with _document_page_render_slot(lock_dir):
            with tempfile.TemporaryDirectory(prefix="manor-first-page-snapshot-") as snapshot_dir:
                snapshot_path = os.path.join(snapshot_dir, f"document{ext}")
                cache_key = await _run_document_page_thread(
                    _snapshot_file_with_hash,
                    file_path,
                    snapshot_path,
                    _render_cache_salt(FIRST_PAGE_RENDER_CACHE_VERSION, dpi, ext),
                )
                out_dir = os.path.join(cache_dir, cache_key)
                cached = _get_cached_first_page(out_dir)
                if cached:
                    await _run_document_page_thread(touch_document_page_cache, out_dir)
                    return await _open_first_page_cache_file(
                        cached,
                        cache_dir,
                        out_dir,
                    )
                try:
                    rendered = await _run_document_page_thread(
                        _render_first_page_sync,
                        snapshot_path,
                        out_dir,
                        dpi,
                        ext,
                    )
                except BaseException:
                    await _run_document_page_thread(_remove_incomplete_first_page_cache, out_dir)
                    raise
                return await _open_first_page_cache_file(
                    rendered,
                    cache_dir,
                    out_dir,
                )


def _open_and_prune_first_page_cache_file(
    output_path: str,
    cache_dir: str,
    out_dir: str,
) -> tuple[BinaryIO, str]:
    handle = open(output_path, "rb")
    try:
        _prune_document_page_cache(
            cache_dir,
            out_dir,
            max_versions=FIRST_PAGE_CACHE_MAX_VERSIONS,
        )
    except BaseException:
        handle.close()
        raise
    return handle, output_path


async def _open_first_page_cache_file(
    output_path: str,
    cache_dir: str,
    out_dir: str,
) -> tuple[BinaryIO, str]:
    return await _run_document_page_thread(
        _open_and_prune_first_page_cache_file,
        output_path,
        cache_dir,
        out_dir,
        _release_result=lambda result: result[0].close(),
    )


def _get_cached_first_page(out_dir: str) -> str | None:
    if not os.path.isdir(out_dir):
        return None
    files = sorted(Path(out_dir).glob("page-*.jpg"))
    return str(files[0]) if files else None


def _remove_incomplete_first_page_cache(out_dir: str) -> None:
    if _get_cached_first_page(out_dir) is None:
        shutil.rmtree(out_dir, ignore_errors=True)


def _render_first_page_sync(
    file_path: str, out_dir: str, dpi: int, source_ext: str | None = None,
) -> str:
    os.makedirs(out_dir, exist_ok=True)
    ext = _normalize_source_ext(source_ext) or os.path.splitext(file_path)[1].lower()

    with tempfile.TemporaryDirectory() as tmp:
        if ext == ".pdf":
            pdf_path = file_path
        else:
            soffice_input = _prepare_soffice_input(file_path, tmp, ext)
            pdf_path = os.path.join(tmp, Path(soffice_input).stem + ".pdf")
            result = subprocess.run(
                [
                    "soffice", _soffice_user_installation_arg(tmp),
                    "--headless", "--convert-to", "pdf",
                    "--outdir", tmp, soffice_input,
                ],
                capture_output=True, text=True,
                env=_get_soffice_env(tmp),
                preexec_fn=office_conversion_file_limit(FIRST_PAGE_CONVERSION_MAX_FILE_BYTES),
                timeout=120,
            )
            if result.returncode != 0 or not os.path.isfile(pdf_path):
                raise RuntimeError(
                    f"LibreOffice PDF conversion failed: {result.stderr[:500]}"
                )

        _validate_pdf_raster_dimensions(
            pdf_path,
            dpi,
            max_pixels=FIRST_PAGE_RENDER_MAX_PIXELS,
            max_dimension=FIRST_PAGE_RENDER_MAX_DIMENSION,
        )

        # Only the first page — cheaper than rendering the whole document.
        result = subprocess.run(
            [
                "pdftoppm", "-jpeg", "-r", str(dpi), "-f", "1", "-l", "1",
                pdf_path, os.path.join(tmp, "page"),
            ],
            capture_output=True, text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"pdftoppm conversion failed: {result.stderr[:500]}")

        pages = sorted(Path(tmp).glob("page-*.jpg")) or sorted(Path(tmp).glob("page*.jpg"))
        if not pages:
            raise RuntimeError("No page image produced")
        if pages[0].stat().st_size > FIRST_PAGE_RENDER_MAX_BYTES:
            raise OfficeRenderLimitError(
                "Office thumbnail exceeds the rendered file size limit",
            )

        dest = os.path.join(out_dir, "page-1.jpg")
        shutil.move(str(pages[0]), dest)
        return dest


def _normalize_source_ext(source_ext: str | None) -> str | None:
    ext = (source_ext or "").strip().lower()
    if not ext:
        return None
    return ext if ext.startswith(".") else f".{ext}"


def _render_cache_salt(cache_version: str, dpi: int, source_ext: str) -> str:
    """Separate identical container bytes rendered under different type hints."""
    return f"{cache_version}-{dpi}dpi-{source_ext.lstrip('.')}"


def _prepare_soffice_input(file_path: str, tmp_dir: str, source_ext: str) -> str:
    current_ext = os.path.splitext(file_path)[1].lower()
    if not source_ext or current_ext == source_ext:
        return file_path
    hinted_path = os.path.join(tmp_dir, f"{Path(file_path).stem or 'document'}{source_ext}")
    shutil.copy2(file_path, hinted_path)
    return hinted_path


def _file_hash(path: str, cache_version: str = SLIDE_RENDER_CACHE_VERSION) -> str:
    h = hashlib.sha256()
    h.update(cache_version.encode("utf-8"))
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _snapshot_file_with_hash(
    source_path: str,
    snapshot_path: str,
    cache_version: str,
) -> str:
    """Copy one open file descriptor while hashing the exact copied bytes."""
    h = hashlib.sha256()
    h.update(cache_version.encode("utf-8"))
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)
    with open(source_path, "rb") as source, open(snapshot_path, "xb") as snapshot:
        for chunk in iter(lambda: source.read(65536), b""):
            h.update(chunk)
            snapshot.write(chunk)
    return h.hexdigest()[:16]


def _get_cached_slides(slide_dir: str) -> list[str]:
    manifest_path = Path(slide_dir) / ".complete"
    if not manifest_path.is_file():
        return []
    try:
        expected_count = int(manifest_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return []
    if expected_count < 1:
        return []
    slides = [Path(slide_dir) / f"slide-{index}.png" for index in range(1, expected_count + 1)]
    return [str(slide) for slide in slides] if all(slide.is_file() for slide in slides) else []


def _preview_source_fingerprint(source_path: str) -> dict[str, int | str]:
    resolved_path = os.path.realpath(source_path)
    metadata = os.stat(resolved_path)
    return {
        "path": resolved_path,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size": metadata.st_size,
        "modified_ns": metadata.st_mtime_ns,
        "changed_ns": metadata.st_ctime_ns,
    }


def publish_current_preview_version(
    cache_dir: str,
    version: str,
    source_path: str,
) -> None:
    """Atomically bind cache reads to the exact source validated by the API."""
    if not _DOCUMENT_PAGE_CACHE_KEY_RE.fullmatch(version):
        raise ValueError("Invalid preview version")
    payload = json.dumps(
        {
            "format": 1,
            "version": version,
            "source": _preview_source_fingerprint(source_path),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    os.makedirs(cache_dir, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".current-",
            dir=cache_dir,
            delete=False,
        ) as temporary_file:
            temporary_path = temporary_file.name
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(
            temporary_path,
            os.path.join(cache_dir, _CURRENT_PREVIEW_VERSION_FILE),
        )
    finally:
        if temporary_path is not None:
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass


def invalidate_document_preview_versions(entity_root: str, document_id: str) -> None:
    """Make retained slide/page caches unreadable after a document replacement."""
    for cache_name in (".slide-cache", ".document-page-cache"):
        marker_path = os.path.join(
            entity_root,
            cache_name,
            document_id,
            _CURRENT_PREVIEW_VERSION_FILE,
        )
        try:
            os.remove(marker_path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not invalidate document preview marker %s", marker_path)


def _require_current_preview_version(
    cache_dir: str,
    version: str,
    source_path: str,
) -> None:
    if not _DOCUMENT_PAGE_CACHE_KEY_RE.fullmatch(version):
        raise FileNotFoundError("Preview version not found")
    try:
        payload = json.loads(Path(cache_dir, _CURRENT_PREVIEW_VERSION_FILE).read_text(
            encoding="utf-8",
        ))
        current_version = payload["version"]
        current_source = payload["source"]
        expected_source = _preview_source_fingerprint(source_path)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FileNotFoundError("Preview version not found") from exc
    if current_version != version or current_source != expected_source:
        raise FileNotFoundError("Preview version not found")


def _open_cached_slide_sync(
    cache_dir: str,
    version: str,
    slide_index: int,
    source_path: str,
) -> BinaryIO:
    _require_current_preview_version(cache_dir, version, source_path)
    paths = _get_cached_slides(os.path.join(cache_dir, version))
    if not paths:
        raise FileNotFoundError("Slide preview version not found")
    if slide_index < 0 or slide_index >= len(paths):
        raise IndexError("Slide index out of range")
    handle = open(paths[slide_index], "rb")
    touch_document_page_cache(os.path.dirname(paths[slide_index]))
    return handle


def _open_cached_document_page_sync(
    cache_dir: str,
    version: str,
    page_index: int,
    source_path: str,
) -> BinaryIO:
    _require_current_preview_version(cache_dir, version, source_path)
    paths = _get_cached_document_pages(os.path.join(cache_dir, version))
    if not paths:
        raise FileNotFoundError("Word preview version not found")
    if page_index < 0 or page_index >= len(paths):
        raise IndexError("Page index out of range")
    handle = open(paths[page_index], "rb")
    touch_document_page_cache(os.path.dirname(paths[page_index]))
    return handle


def _remove_incomplete_slide_cache(slide_dir: str) -> None:
    if not _get_cached_slides(slide_dir):
        shutil.rmtree(slide_dir, ignore_errors=True)


def _get_cached_document_pages(page_dir: str) -> list[str]:
    manifest_path = Path(page_dir) / ".complete"
    if not manifest_path.is_file():
        return []
    try:
        expected_count = int(manifest_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return []
    if expected_count < 1:
        return []
    pages = [Path(page_dir) / f"page-{index}.png" for index in range(1, expected_count + 1)]
    return [str(page) for page in pages] if all(page.is_file() for page in pages) else []


def touch_document_page_cache(page_dir: str) -> None:
    """Refresh the short lease protecting page URLs issued to active viewers."""
    try:
        if os.path.isdir(page_dir) and not os.path.islink(page_dir):
            os.utime(page_dir, None, follow_symlinks=False)
    except OSError:
        return


def _prune_document_page_cache(
    cache_dir: str,
    current_page_dir: str,
    *,
    max_versions: int = DOCUMENT_PAGE_CACHE_MAX_VERSIONS,
) -> None:
    """Keep the current version and a hard-bounded set of recent leases."""
    cache_root = Path(cache_dir)
    try:
        if not cache_root.is_dir() or cache_root.is_symlink():
            return
        candidates = [
            child
            for child in cache_root.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and _DOCUMENT_PAGE_CACHE_KEY_RE.fullmatch(child.name)
        ]
        candidates.sort(
            key=lambda child: child.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return
    current_name = Path(current_page_dir).name
    keep_count = max(1, int(max_versions))
    kept = {current_name}
    for candidate in candidates:
        if len(kept) >= keep_count:
            break
        kept.add(candidate.name)
    for candidate in candidates:
        if candidate.name not in kept:
            shutil.rmtree(candidate, ignore_errors=True)


def _presentation_object_cache_size(path: Path) -> int:
    total = 0
    try:
        for root, directories, filenames in os.walk(path, followlinks=False):
            directories[:] = [
                name
                for name in directories
                if not os.path.islink(os.path.join(root, name))
            ]
            for filename in filenames:
                file_path = os.path.join(root, filename)
                if not os.path.islink(file_path):
                    total += os.path.getsize(file_path)
    except OSError:
        return total
    return total


def _touch_presentation_object_cache(version_dir: str, object_dir: str) -> None:
    touch_document_page_cache(object_dir)
    touch_document_page_cache(version_dir)


def _prune_presentation_object_version_cache(
    version_dir: str,
    current_object_dir: str,
    *,
    max_objects: int = PRESENTATION_OBJECT_CACHE_MAX_OBJECTS,
    max_bytes: int = PRESENTATION_OBJECT_CACHE_MAX_BYTES,
) -> None:
    version_root = Path(version_dir)
    try:
        if not version_root.is_dir() or version_root.is_symlink():
            return
        candidates = [
            child
            for child in version_root.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and _PRESENTATION_OBJECT_CACHE_DIR_RE.fullmatch(child.name)
        ]
        current_name = Path(current_object_dir).name
        candidates.sort(
            key=lambda child: (
                child.name == current_name,
                child.stat().st_mtime_ns,
            ),
            reverse=True,
        )
    except OSError:
        return

    keep_count = max(1, int(max_objects))
    byte_limit = max(1, int(max_bytes))
    kept: set[str] = set()
    kept_bytes = 0
    for candidate in candidates:
        size = _presentation_object_cache_size(candidate)
        must_keep = candidate.name == current_name
        if must_keep or (
            len(kept) < keep_count
            and kept_bytes + size <= byte_limit
        ):
            kept.add(candidate.name)
            kept_bytes += size
    for candidate in candidates:
        if candidate.name not in kept:
            shutil.rmtree(candidate, ignore_errors=True)


def _prune_presentation_object_cache(
    cache_dir: str,
    current_version_dir: str,
    current_object_dir: str,
) -> None:
    _prune_presentation_object_version_cache(
        current_version_dir,
        current_object_dir,
    )
    _prune_document_page_cache(
        cache_dir,
        current_version_dir,
        max_versions=PRESENTATION_OBJECT_CACHE_MAX_VERSIONS,
    )
    cache_root = Path(cache_dir)
    try:
        legacy_roots = [
            child
            for child in cache_root.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and _PRESENTATION_OBJECT_CACHE_DIR_RE.fullmatch(child.name)
        ]
    except OSError:
        return
    for legacy_root in legacy_roots:
        shutil.rmtree(legacy_root, ignore_errors=True)


def _pdf_page_count(pdf_path: str) -> int:
    result = subprocess.run(
        ["pdfinfo", pdf_path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"PDF metadata inspection failed: {result.stderr[:500]}")
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, flags=re.MULTILINE)
    if not match or int(match.group(1)) < 1:
        raise RuntimeError("PDF metadata did not contain a valid page count")
    return int(match.group(1))


def _pdf_page_pixel_dimensions(
    pdf_path: str,
    dpi: int,
    *,
    page_count: int,
) -> list[tuple[int, int]]:
    command = ["pdfinfo", "-f", "1", "-l", str(page_count)]
    if page_count > 1:
        command.append("-box")
    command.append(pdf_path)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"PDF metadata inspection failed: {result.stderr[:500]}")
    matches = re.findall(
        r"^Page(?:\s+(\d+))? size:\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s+x\s+"
        r"([0-9]+(?:\.[0-9]+)?)\s+pts\b",
        result.stdout,
        flags=re.MULTILINE,
    )
    if not matches:
        raise RuntimeError("PDF metadata did not contain valid page sizes")

    numbered = {
        int(page_number): (width_points, height_points)
        for page_number, width_points, height_points in matches
        if page_number
    }
    if numbered:
        expected_pages = set(range(1, page_count + 1))
        if set(numbered) != expected_pages:
            raise RuntimeError("PDF metadata did not contain every page size")
        point_sizes = [numbered[page_number] for page_number in range(1, page_count + 1)]
    elif len(matches) == 1:
        # Some pdfinfo versions collapse a uniform page range to one unnumbered
        # Page size line. In that case the same dimensions apply to every page.
        _, width_points, height_points = matches[0]
        point_sizes = [(width_points, height_points)] * page_count
    else:
        raise RuntimeError("PDF metadata contained ambiguous page sizes")

    dimensions = [
        (
            math.ceil(float(width_points) * dpi / 72),
            math.ceil(float(height_points) * dpi / 72),
        )
        for width_points, height_points in point_sizes
    ]
    if any(width < 1 or height < 1 for width, height in dimensions):
        raise RuntimeError("PDF metadata contained an invalid page size")
    return dimensions


def _validate_pdf_raster_dimensions(
    pdf_path: str,
    dpi: int,
    *,
    max_pixels: int,
    max_dimension: int,
    page_count: int = 1,
) -> int:
    dimensions = _pdf_page_pixel_dimensions(
        pdf_path,
        dpi,
        page_count=page_count,
    )
    for width, height in dimensions:
        if (
            width > max_dimension
            or height > max_dimension
            or width * height > max_pixels
        ):
            raise OfficeRenderLimitError(
                "Office preview page exceeds the render size limit",
            )
    return sum(width * height for width, height in dimensions)


def _rasterize_pdf_pages_bounded(
    pdf_path: str,
    output_dir: str,
    *,
    output_stem: str,
    dpi: int,
    page_count: int,
    max_bytes: int,
) -> list[Path]:
    """Rasterize one page at a time so output is rejected near its hard cap."""
    deadline = time.monotonic() + 120
    rendered: list[Path] = []
    rendered_bytes = 0
    for page_number in range(1, page_count + 1):
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise subprocess.TimeoutExpired("pdftoppm", 120)
        output_prefix = os.path.join(output_dir, f"{output_stem}-{page_number}")
        result = subprocess.run(
            [
                "pdftoppm", "-png", "-r", str(dpi),
                "-f", str(page_number), "-l", str(page_number),
                "-singlefile", pdf_path, output_prefix,
            ],
            capture_output=True,
            text=True,
            timeout=max(1, remaining_seconds),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"pdftoppm conversion failed: {result.stderr[:500]}"
            )
        output_path = Path(f"{output_prefix}.png")
        if not output_path.is_file():
            raise RuntimeError("PDF renderer produced an incomplete page set")
        rendered_bytes += output_path.stat().st_size
        if rendered_bytes > max_bytes:
            raise OfficeRenderLimitError(
                "Office preview exceeds the rendered file size limit",
            )
        rendered.append(output_path)
    return rendered


def _soffice_user_installation_arg(tmp_dir: str) -> str:
    """Point LibreOffice at an isolated, writable per-conversion profile."""
    profile_uri = (Path(tmp_dir).resolve() / "libreoffice-profile").as_uri()
    return f"-env:UserInstallation={profile_uri}"


def _get_soffice_env(tmp_dir: str | None = None) -> dict:
    """Minimal env for headless LibreOffice in a read-only container."""
    env = os.environ.copy()
    env["SAL_USE_VCLPLUGIN"] = "svp"
    if tmp_dir:
        env["HOME"] = tmp_dir
        env["XDG_CONFIG_HOME"] = os.path.join(tmp_dir, ".config")
        env["XDG_CACHE_HOME"] = os.path.join(tmp_dir, ".cache")
    return env


_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _presentation_tag(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _presentation_part_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


def _validate_presentation_object_render_size(
    width: int,
    height: int,
    dpi: int,
) -> tuple[int, int]:
    if width < 1 or height < 1 or dpi < 1:
        raise ValueError("Presentation object has an invalid render size")
    pixel_width = max(1, math.ceil(width * dpi / 914_400))
    pixel_height = max(1, math.ceil(height * dpi / 914_400))
    if (
        pixel_width > PRESENTATION_OBJECT_RENDER_MAX_DIMENSION
        or pixel_height > PRESENTATION_OBJECT_RENDER_MAX_DIMENSION
        or pixel_width * pixel_height > PRESENTATION_OBJECT_RENDER_MAX_PIXELS
    ):
        raise PresentationObjectRenderLimitError(
            "Presentation object exceeds the render size limit",
        )
    return pixel_width, pixel_height


def _build_presentation_object_variant(
    source_path: str,
    destination_path: str,
    *,
    slide_index: int,
    object_id: str,
    background: str,
    dpi: int = 192,
) -> None:
    """Build a one-slide PPTX containing only one normalized graphic frame."""
    ET.register_namespace("p", _PRESENTATION_NS)
    ET.register_namespace("a", _DRAWING_NS)
    ET.register_namespace("r", _OFFICE_REL_NS)
    with zipfile.ZipFile(source_path, "r") as source:
        presentation_root = DefusedET.fromstring(source.read("ppt/presentation.xml"))
        relationships_root = DefusedET.fromstring(source.read("ppt/_rels/presentation.xml.rels"))
        slide_id_list = presentation_root.find(_presentation_tag(_PRESENTATION_NS, "sldIdLst"))
        slide_ids = list(slide_id_list) if slide_id_list is not None else []
        if slide_id_list is None or slide_index < 0 or slide_index >= len(slide_ids):
            raise ValueError("Slide index out of range")
        selected_slide_id = slide_ids[slide_index]
        selected_relationship_id = selected_slide_id.get(_presentation_tag(_OFFICE_REL_NS, "id"))
        if not selected_relationship_id:
            raise ValueError("Selected slide has no relationship")
        slide_target = None
        for relationship in relationships_root:
            if relationship.get("Id") == selected_relationship_id:
                slide_target = relationship.get("Target")
                break
        if not slide_target:
            raise ValueError("Selected slide relationship is missing")
        slide_part = _presentation_part_target("ppt/presentation.xml", slide_target)
        for slide_id in slide_ids:
            if slide_id is not selected_slide_id:
                slide_id_list.remove(slide_id)

        slide_root = DefusedET.fromstring(source.read(slide_part))
        graphic_frame_tag = _presentation_tag(_PRESENTATION_NS, "graphicFrame")
        non_visual_tag = _presentation_tag(_PRESENTATION_NS, "cNvPr")
        target_frame = None
        for graphic_frame in slide_root.iter(graphic_frame_tag):
            non_visual = graphic_frame.find(f".//{non_visual_tag}")
            if non_visual is not None and non_visual.get("id") == object_id:
                target_frame = graphic_frame
                break
        if target_frame is None:
            raise ValueError("Presentation object not found")

        transform = target_frame.find(_presentation_tag(_PRESENTATION_NS, "xfrm"))
        if transform is None:
            transform = target_frame.find(_presentation_tag(_DRAWING_NS, "xfrm"))
        if transform is None:
            raise ValueError("Presentation object has no transform")
        extent = transform.find(_presentation_tag(_DRAWING_NS, "ext"))
        if extent is None:
            extent = transform.find(_presentation_tag(_PRESENTATION_NS, "ext"))
        offset = transform.find(_presentation_tag(_DRAWING_NS, "off"))
        if offset is None:
            offset = transform.find(_presentation_tag(_PRESENTATION_NS, "off"))
        width = int(extent.get("cx", "0")) if extent is not None else 0
        height = int(extent.get("cy", "0")) if extent is not None else 0
        if width < 1 or height < 1 or offset is None:
            raise ValueError("Presentation object has an invalid transform")
        _validate_presentation_object_render_size(width, height, dpi)
        for attribute in ("rot", "flipH", "flipV"):
            transform.attrib.pop(attribute, None)
        offset.set("x", "0")
        offset.set("y", "0")

        parent_by_child = {child: parent for parent in slide_root.iter() for child in parent}
        frame_parent = parent_by_child.get(target_frame)
        if frame_parent is None:
            raise ValueError("Presentation object is detached")
        frame_parent.remove(target_frame)
        shape_tree = slide_root.find(
            f"./{_presentation_tag(_PRESENTATION_NS, 'cSld')}/{_presentation_tag(_PRESENTATION_NS, 'spTree')}",
        )
        if shape_tree is None:
            raise ValueError("Presentation slide has no shape tree")
        preserved_tree_tags = {
            _presentation_tag(_PRESENTATION_NS, "nvGrpSpPr"),
            _presentation_tag(_PRESENTATION_NS, "grpSpPr"),
        }
        for child in list(shape_tree):
            if child.tag not in preserved_tree_tags:
                shape_tree.remove(child)
        shape_tree.append(target_frame)

        slide_root.set("showMasterSp", "0")
        common_slide = slide_root.find(_presentation_tag(_PRESENTATION_NS, "cSld"))
        if common_slide is None:
            raise ValueError("Presentation slide has no common slide data")
        existing_background = common_slide.find(_presentation_tag(_PRESENTATION_NS, "bg"))
        if existing_background is not None:
            common_slide.remove(existing_background)
        background_node = ET.Element(_presentation_tag(_PRESENTATION_NS, "bg"))
        background_properties = ET.SubElement(background_node, _presentation_tag(_PRESENTATION_NS, "bgPr"))
        solid_fill = ET.SubElement(background_properties, _presentation_tag(_DRAWING_NS, "solidFill"))
        ET.SubElement(solid_fill, _presentation_tag(_DRAWING_NS, "srgbClr"), {"val": background})
        ET.SubElement(background_properties, _presentation_tag(_DRAWING_NS, "effectLst"))
        common_slide.insert(0, background_node)

        slide_size = presentation_root.find(_presentation_tag(_PRESENTATION_NS, "sldSz"))
        if slide_size is None:
            slide_size = ET.SubElement(presentation_root, _presentation_tag(_PRESENTATION_NS, "sldSz"))
        slide_size.set("cx", str(width))
        slide_size.set("cy", str(height))

        replacements = {
            "ppt/presentation.xml": ET.tostring(presentation_root, encoding="utf-8", xml_declaration=True),
            slide_part: ET.tostring(slide_root, encoding="utf-8", xml_declaration=True),
        }
        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        with zipfile.ZipFile(destination_path, "w") as destination:
            for entry in source.infolist():
                destination.writestr(entry, replacements.get(entry.filename, source.read(entry.filename)))


def _combine_presentation_object_mattes(
    black_path: str,
    white_path: str,
    output_path: str,
) -> None:
    """Recover straight-alpha RGBA pixels from black and white renders."""
    with Image.open(black_path) as black_source, Image.open(white_path) as white_source:
        black = black_source.convert("RGB")
        white = white_source.convert("RGB")
        if black.size != white.size:
            raise RuntimeError("Presentation object matte sizes do not match")
        width, height = black.size
        if (
            width < 1
            or height < 1
            or width > PRESENTATION_OBJECT_RENDER_MAX_DIMENSION
            or height > PRESENTATION_OBJECT_RENDER_MAX_DIMENSION
            or width * height > PRESENTATION_OBJECT_RENDER_MAX_PIXELS
        ):
            raise PresentationObjectRenderLimitError(
                "Rendered presentation object exceeds the size limit",
            )
        output = Image.new("RGBA", black.size)
        rows_per_chunk = max(1, PRESENTATION_OBJECT_MATTE_CHUNK_PIXELS // width)
        for top in range(0, height, rows_per_chunk):
            bottom = min(height, top + rows_per_chunk)
            black_chunk = black.crop((0, top, width, bottom))
            white_chunk = white.crop((0, top, width, bottom))
            pixels = []
            for black_pixel, white_pixel in zip(
                black_chunk.get_flattened_data(),
                white_chunk.get_flattened_data(),
            ):
                background = round(sum(
                    max(0, white_pixel[index] - black_pixel[index])
                    for index in range(3)
                ) / 3)
                alpha = max(0, min(255, 255 - background))
                if alpha == 0:
                    pixels.append((0, 0, 0, 0))
                    continue
                pixels.append(tuple(
                    max(0, min(255, round(channel * 255 / alpha)))
                    for channel in black_pixel
                ) + (alpha,))
            output_chunk = Image.new("RGBA", (width, bottom - top))
            output_chunk.putdata(pixels)
            output.paste(output_chunk, (0, top))
            black_chunk.close()
            white_chunk.close()
            output_chunk.close()
        output.save(output_path, format="PNG", optimize=True)


def _render_presentation_object_sync(
    pptx_path: str,
    object_dir: str,
    slide_index: int,
    object_id: str,
    dpi: int,
) -> str:
    output_path = os.path.join(object_dir, "object.png")
    if os.path.isfile(output_path):
        return output_path
    cache_root = os.path.dirname(object_dir)
    os.makedirs(cache_root, exist_ok=True)
    publish_dir = tempfile.mkdtemp(prefix=".presentation-object-", dir=cache_root)
    try:
        black_pptx = os.path.join(publish_dir, "black.pptx")
        white_pptx = os.path.join(publish_dir, "white.pptx")
        _build_presentation_object_variant(
            pptx_path,
            black_pptx,
            slide_index=slide_index,
            object_id=object_id,
            background="000000",
            dpi=dpi,
        )
        _build_presentation_object_variant(
            pptx_path,
            white_pptx,
            slide_index=slide_index,
            object_id=object_id,
            background="FFFFFF",
            dpi=dpi,
        )
        black_render = _render_sync(black_pptx, os.path.join(publish_dir, "black"), dpi)[0]
        white_render = _render_sync(white_pptx, os.path.join(publish_dir, "white"), dpi)[0]
        rendered_path = os.path.join(publish_dir, "object.png")
        _combine_presentation_object_mattes(black_render, white_render, rendered_path)
        os.makedirs(object_dir, exist_ok=True)
        if not os.path.isfile(output_path):
            os.replace(rendered_path, output_path)
        return output_path
    finally:
        shutil.rmtree(publish_dir, ignore_errors=True)


def _render_sync(pptx_path: str, slide_dir: str, dpi: int) -> list[str]:
    os.makedirs(slide_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        # Step 1: PPTX → PDF via LibreOffice
        pdf_path = os.path.join(tmp, Path(pptx_path).stem + ".pdf")
        result = subprocess.run(
            [
                "soffice", _soffice_user_installation_arg(tmp),
                "--headless", "--convert-to", "pdf",
                "--outdir", tmp, pptx_path,
            ],
            capture_output=True, text=True,
            env=_get_soffice_env(tmp),
            preexec_fn=office_conversion_file_limit(SLIDE_CONVERSION_MAX_FILE_BYTES),
            timeout=120,
        )
        if result.returncode != 0 or not os.path.isfile(pdf_path):
            raise RuntimeError(
                f"LibreOffice PDF conversion failed: {result.stderr[:500]}"
            )

        page_count = _pdf_page_count(pdf_path)
        if page_count > SLIDE_RENDER_MAX_PAGES:
            raise OfficeRenderLimitError(
                f"Presentation preview exceeds the {SLIDE_RENDER_MAX_PAGES}-slide limit",
            )
        total_pixels = _validate_pdf_raster_dimensions(
            pdf_path,
            dpi,
            max_pixels=SLIDE_RENDER_MAX_PIXELS,
            max_dimension=SLIDE_RENDER_MAX_DIMENSION,
            page_count=page_count,
        )
        if total_pixels > SLIDE_RENDER_MAX_TOTAL_PIXELS:
            raise OfficeRenderLimitError(
                "Presentation preview exceeds the total pixel limit",
            )

        # Step 2: PDF → per-slide lossless PNG via bounded pdftoppm calls.
        slide_files = _rasterize_pdf_pages_bounded(
            pdf_path,
            tmp,
            output_stem="slide",
            dpi=dpi,
            page_count=page_count,
            max_bytes=SLIDE_RENDER_MAX_BYTES,
        )

        paths = []
        for index, f in enumerate(slide_files, start=1):
            dest = os.path.join(slide_dir, f"slide-{index}.png")
            shutil.move(str(f), dest)
            paths.append(dest)
        manifest_path = os.path.join(slide_dir, ".complete")
        temporary_manifest_path = f"{manifest_path}.tmp"
        Path(temporary_manifest_path).write_text(str(len(paths)), encoding="utf-8")
        os.replace(temporary_manifest_path, manifest_path)

    return paths


def _render_document_pages_sync(
    file_path: str,
    page_dir: str,
    dpi: int,
    source_ext: str | None = ".docx",
) -> list[str]:
    cached = _get_cached_document_pages(page_dir)
    if cached:
        return cached

    cache_root = os.path.dirname(page_dir)
    os.makedirs(cache_root, exist_ok=True)
    publish_dir = tempfile.mkdtemp(prefix=".document-pages-", dir=cache_root)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            ext = _normalize_source_ext(source_ext) or os.path.splitext(file_path)[1].lower()
            soffice_input = _prepare_soffice_input(file_path, tmp, ext)
            pdf_path = os.path.join(tmp, Path(soffice_input).stem + ".pdf")
            result = subprocess.run(
                [
                    "soffice", _soffice_user_installation_arg(tmp),
                    "--headless", "--convert-to", "pdf",
                    "--outdir", tmp, soffice_input,
                ],
                capture_output=True,
                text=True,
                env=_get_soffice_env(tmp),
                preexec_fn=office_conversion_file_limit(
                    DOCUMENT_PAGE_CONVERSION_MAX_FILE_BYTES,
                ),
                timeout=120,
            )
            if result.returncode != 0 or not os.path.isfile(pdf_path):
                raise RuntimeError(
                    f"LibreOffice PDF conversion failed: {result.stderr[:500]}"
                )

            page_count = _pdf_page_count(pdf_path)
            if page_count > DOCUMENT_PAGE_RENDER_MAX_PAGES:
                raise OfficeRenderLimitError(
                    "Document page preview exceeds the "
                    f"{DOCUMENT_PAGE_RENDER_MAX_PAGES}-page limit"
                )
            total_pixels = _validate_pdf_raster_dimensions(
                pdf_path,
                dpi,
                max_pixels=DOCUMENT_PAGE_RENDER_MAX_PIXELS,
                max_dimension=DOCUMENT_PAGE_RENDER_MAX_DIMENSION,
                page_count=page_count,
            )
            if total_pixels > DOCUMENT_PAGE_RENDER_MAX_TOTAL_PIXELS:
                raise OfficeRenderLimitError(
                    "Document page preview exceeds the total pixel limit",
                )

            rendered_pages = _rasterize_pdf_pages_bounded(
                pdf_path,
                tmp,
                output_stem="page",
                dpi=dpi,
                page_count=page_count,
                max_bytes=DOCUMENT_PAGE_RENDER_MAX_BYTES,
            )

            for index, page in enumerate(rendered_pages, start=1):
                shutil.move(str(page), os.path.join(publish_dir, f"page-{index}.png"))
            Path(publish_dir, ".complete").write_text(
                str(len(rendered_pages)),
                encoding="utf-8",
            )

        try:
            os.rename(publish_dir, page_dir)
        except OSError:
            # A concurrent publisher can surface as EEXIST on Linux or
            # ENOTEMPTY on macOS when the completed destination already exists.
            # Trust it only after validating the full manifest and page set.
            cached = _get_cached_document_pages(page_dir)
            if cached:
                return cached
            raise
        publish_dir = ""
        pages = _get_cached_document_pages(page_dir)
        if not pages:
            raise RuntimeError("Document page cache was not published completely")
        return pages
    finally:
        if publish_dir:
            shutil.rmtree(publish_dir, ignore_errors=True)
