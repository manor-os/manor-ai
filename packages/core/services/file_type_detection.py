"""Lightweight file type detection for Knowledge documents."""
from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass


@dataclass(frozen=True)
class DetectedFileType:
    extension: str | None
    mime_type: str
    display_name: str
    mismatch: bool = False


_MIME_BY_EXT: dict[str, str] = {
    "md": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
    "json": "application/json",
    "html": "text/html",
    "css": "text/css",
    "scss": "text/x-scss",
    "sass": "text/x-sass",
    "less": "text/less",
    "js": "text/javascript",
    "mjs": "text/javascript",
    "cjs": "text/javascript",
    "jsx": "text/javascript",
    "ts": "text/typescript",
    "tsx": "text/typescript",
    "vue": "text/x-vue",
    "svelte": "text/x-svelte",
    "py": "text/x-python",
    "sh": "text/x-shellscript",
    "sql": "application/sql",
    "xml": "application/xml",
    "yaml": "application/yaml",
    "yml": "application/yaml",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "zip": "application/zip",
}

_PRESERVE_DECLARED_TEXT_EXTS: set[str] = {
    "md", "csv", "json", "html", "css", "scss", "sass", "less",
    "js", "mjs", "cjs", "jsx", "ts", "tsx", "vue", "svelte",
    "py", "sh", "sql", "xml", "yaml", "yml",
}


def mime_for_extension(ext: str | None) -> str:
    return _MIME_BY_EXT.get((ext or "").lower(), "application/octet-stream")


def detect_file_type(path: str, *, declared_name: str | None = None) -> DetectedFileType:
    """Detect the stored file type and avoid trusting misleading extensions."""
    name = os.path.basename(declared_name or path)
    declared_ext = os.path.splitext(name)[1].lstrip(".").lower() or None
    sniffed_ext = _detect_extension(path)
    detected_ext = _resolve_detected_extension(declared_ext, sniffed_ext)
    mime_type = mime_for_extension(detected_ext)
    mismatch = bool(declared_ext and detected_ext and declared_ext != detected_ext)
    display_name = name
    if mismatch:
        stem = os.path.splitext(name)[0]
        display_name = f"{stem}.{detected_ext}"
    return DetectedFileType(detected_ext, mime_type, display_name, mismatch=mismatch)


def _resolve_detected_extension(declared_ext: str | None, sniffed_ext: str | None) -> str | None:
    if declared_ext in _PRESERVE_DECLARED_TEXT_EXTS and sniffed_ext == "txt":
        return declared_ext
    return sniffed_ext or declared_ext


def _detect_extension(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return None

    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head.lstrip().startswith(b"<svg"):
        return "svg"
    if head.startswith(b"PK\x03\x04"):
        return _detect_zip_office(path) or "zip"
    if _looks_like_text(head):
        return _detect_text_extension(head)
    return None


def _detect_zip_office(path: str) -> str | None:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        return None
    if "word/document.xml" in names:
        return "docx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    return None


def _looks_like_text(sample: bytes) -> bool:
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        try:
            sample.decode("utf-16")
            return True
        except UnicodeDecodeError:
            return False


def _detect_text_extension(sample: bytes) -> str:
    text = sample.decode("utf-8", errors="ignore").lstrip()
    lower = text.lower()
    if text.startswith("#") or "\n#" in text[:1000] or "```" in text[:1000]:
        return "md"
    if lower.startswith("<!doctype html") or lower.startswith("<html"):
        return "html"
    if lower.startswith("{") or lower.startswith("["):
        return "json"
    if "," in text.splitlines()[0] if text.splitlines() else False:
        return "csv"
    return "txt"
