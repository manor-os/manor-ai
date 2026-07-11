"""Render PPTX slides as images using LibreOffice + pdftoppm.

Converts a binary PPTX file to per-slide JPEG images.
Results are cached on the filesystem to avoid re-rendering.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


async def render_slides(
    pptx_path: str, cache_dir: str, *, dpi: int = 150,
) -> list[str]:
    """Convert PPTX to per-slide JPEG images.

    Returns a list of absolute paths to the rendered slide images,
    ordered by slide number. Uses a content-hash based cache so
    unchanged files aren't re-rendered.
    """
    if not os.path.isfile(pptx_path):
        raise FileNotFoundError(f"PPTX not found: {pptx_path}")

    # Content-hash for cache key
    file_hash = await asyncio.to_thread(_file_hash, pptx_path)
    slide_dir = os.path.join(cache_dir, file_hash)

    # Check cache
    existing = _get_cached_slides(slide_dir)
    if existing:
        return existing

    # Render in a thread to avoid blocking the event loop
    return await asyncio.to_thread(_render_sync, pptx_path, slide_dir, dpi)


async def render_first_page(
    file_path: str, cache_dir: str, *, dpi: int = 150, source_ext: str | None = None,
) -> str:
    """Render the first page of a PDF or office file (pptx/ppt/docx/xlsx) as a
    single JPEG, for use as a document thumbnail. Content-hash cached.

    PDFs go straight to ``pdftoppm``; office files are converted to PDF via
    LibreOffice first. Returns the absolute path to the rendered image.
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    file_hash = await asyncio.to_thread(_file_hash, file_path)
    out_dir = os.path.join(cache_dir, file_hash)

    cached = _get_cached_first_page(out_dir)
    if cached:
        return cached

    return await asyncio.to_thread(_render_first_page_sync, file_path, out_dir, dpi, source_ext)


def _get_cached_first_page(out_dir: str) -> str | None:
    if not os.path.isdir(out_dir):
        return None
    files = sorted(Path(out_dir).glob("page-*.jpg"))
    return str(files[0]) if files else None


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
                    "soffice", "--headless", "--convert-to", "pdf",
                    "--outdir", tmp, soffice_input,
                ],
                capture_output=True, text=True,
                env=_get_soffice_env(),
                timeout=120,
            )
            if result.returncode != 0 or not os.path.isfile(pdf_path):
                raise RuntimeError(
                    f"LibreOffice PDF conversion failed: {result.stderr[:500]}"
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

        dest = os.path.join(out_dir, "page-1.jpg")
        shutil.move(str(pages[0]), dest)
        return dest


def _normalize_source_ext(source_ext: str | None) -> str | None:
    ext = (source_ext or "").strip().lower()
    if not ext:
        return None
    return ext if ext.startswith(".") else f".{ext}"


def _prepare_soffice_input(file_path: str, tmp_dir: str, source_ext: str) -> str:
    current_ext = os.path.splitext(file_path)[1].lower()
    if not source_ext or current_ext == source_ext:
        return file_path
    hinted_path = os.path.join(tmp_dir, f"{Path(file_path).stem or 'document'}{source_ext}")
    shutil.copy2(file_path, hinted_path)
    return hinted_path


def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _get_cached_slides(slide_dir: str) -> list[str]:
    if not os.path.isdir(slide_dir):
        return []
    files = sorted(Path(slide_dir).glob("slide-*.jpg"))
    return [str(f) for f in files] if files else []


def _get_soffice_env() -> dict:
    """Minimal env for headless LibreOffice."""
    env = os.environ.copy()
    env["SAL_USE_VCLPLUGIN"] = "svp"
    return env


def _render_sync(pptx_path: str, slide_dir: str, dpi: int) -> list[str]:
    os.makedirs(slide_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        # Step 1: PPTX → PDF via LibreOffice
        pdf_path = os.path.join(tmp, Path(pptx_path).stem + ".pdf")
        result = subprocess.run(
            [
                "soffice", "--headless", "--convert-to", "pdf",
                "--outdir", tmp, pptx_path,
            ],
            capture_output=True, text=True,
            env=_get_soffice_env(),
            timeout=120,
        )
        if result.returncode != 0 or not os.path.isfile(pdf_path):
            raise RuntimeError(
                f"LibreOffice PDF conversion failed: {result.stderr[:500]}"
            )

        # Step 2: PDF → per-slide JPEG via pdftoppm
        result = subprocess.run(
            [
                "pdftoppm", "-jpeg", "-r", str(dpi),
                pdf_path, os.path.join(tmp, "slide"),
            ],
            capture_output=True, text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"pdftoppm conversion failed: {result.stderr[:500]}"
            )

        # Move rendered slides to cache dir
        slide_files = sorted(Path(tmp).glob("slide-*.jpg"))
        if not slide_files:
            raise RuntimeError("No slide images produced")

        paths = []
        for f in slide_files:
            dest = os.path.join(slide_dir, f.name)
            shutil.move(str(f), dest)
            paths.append(dest)

    return paths
