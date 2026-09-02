"""Lightweight file type detection for Knowledge documents."""
from __future__ import annotations

import json
import os
import zipfile
from dataclasses import dataclass

from packages.core.contracts.file_engine import TEXT_CONTENT_TYPES

@dataclass(frozen=True)
class DetectedFileType:
    extension: str | None
    mime_type: str
    display_name: str
    mismatch: bool = False
    sniffed_extension: str | None = None


_MIME_BY_EXT: dict[str, str] = {
    "md": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "json": "application/json",
    "diagram.json": "application/json",
    "diagram": "application/json",
    "mmd": "text/plain",
    "mermaid": "text/plain",
    "drawio": "application/xml",
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
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "zip": "application/zip",
}

_PRESERVE_DECLARED_TEXT_EXTS = TEXT_CONTENT_TYPES
_TEXT_SNIFF_BYTES = 4096
_MAX_JSON_VALIDATION_BYTES = 8 * 1024 * 1024
_INVALID_JSON_SNIFF = "__invalid_json__"
_UNKNOWN_JSON_SNIFF = "__unknown_json__"


def mime_for_extension(ext: str | None) -> str:
    return _MIME_BY_EXT.get((ext or "").lower(), "application/octet-stream")


def detect_file_type(path: str, *, declared_name: str | None = None) -> DetectedFileType:
    """Detect the stored file type and avoid trusting misleading extensions."""
    name = os.path.basename(declared_name or path)
    declared_ext = (
        "diagram.json"
        if name.lower().endswith(".diagram.json")
        else os.path.splitext(name)[1].lstrip(".").lower() or None
    )
    sniffed_ext = _detect_extension(path)
    detected_ext = _resolve_detected_extension(declared_ext, sniffed_ext)
    normalized_sniffed_ext = _resolve_detected_extension(None, sniffed_ext)
    mime_type = mime_for_extension(detected_ext)
    mismatch = bool(declared_ext and detected_ext and declared_ext != detected_ext)
    display_name = name
    if mismatch:
        stem = os.path.splitext(name)[0]
        display_name = f"{stem}.{detected_ext}"
    return DetectedFileType(
        detected_ext,
        mime_type,
        display_name,
        mismatch=mismatch,
        sniffed_extension=normalized_sniffed_ext,
    )


def _resolve_detected_extension(declared_ext: str | None, sniffed_ext: str | None) -> str | None:
    if sniffed_ext == _UNKNOWN_JSON_SNIFF:
        sniffed_ext = None
    if sniffed_ext == _INVALID_JSON_SNIFF:
        if declared_ext in {"json", "diagram.json", "diagram"}:
            return "txt"
        sniffed_ext = "txt"
    if declared_ext == "diagram.json" and sniffed_ext in {"json", "txt"}:
        return declared_ext
    if declared_ext == "diagram" and sniffed_ext in {"json", "txt"}:
        return declared_ext
    if declared_ext in _PRESERVE_DECLARED_TEXT_EXTS and sniffed_ext == "txt":
        return declared_ext
    return sniffed_ext or declared_ext


def _detect_extension(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            head = f.read(_TEXT_SNIFF_BYTES + 1)
    except OSError:
        return None

    truncated = len(head) > _TEXT_SNIFF_BYTES
    head = head[:_TEXT_SNIFF_BYTES]

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
        return _detect_text_extension(head, truncated=truncated, path=path)
    return None


def _detect_zip_office(path: str) -> str | None:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "xl/workbook.xml" in names and "[Content_Types].xml" in names:
                # A macro-enabled workbook must not be projected as XLSX after
                # editing. Read only the bounded content-type manifest.
                from xml.etree import ElementTree

                with zf.open("[Content_Types].xml") as manifest:
                    content = manifest.read(1024 * 1024 + 1)
                if len(content) > 1024 * 1024:
                    return None
                try:
                    types = ElementTree.fromstring(content)
                except ElementTree.ParseError:
                    return None
                for item in types:
                    if (
                        item.get("PartName") == "/xl/workbook.xml"
                        and item.get("ContentType") == "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
                    ):
                        return "xlsm"
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


def _detect_text_extension(sample: bytes, *, truncated: bool, path: str) -> str:
    text = sample.decode("utf-8", errors="ignore").lstrip()
    lower = text.lower()
    json_extension = _detect_json_container_extension(text, truncated=truncated, path=path)
    if json_extension == _UNKNOWN_JSON_SNIFF:
        return json_extension
    if json_extension and json_extension != _INVALID_JSON_SNIFF:
        return json_extension
    if _looks_like_markdown(text):
        return "md"
    if lower.startswith("<!doctype html") or lower.startswith("<html"):
        return "html"
    if "," in text.splitlines()[0] if text.splitlines() else False:
        return "csv"
    if json_extension == _INVALID_JSON_SNIFF:
        return json_extension
    return "txt"


def _looks_like_markdown(text: str) -> bool:
    return text.startswith("#") or "\n#" in text[:1000] or "```" in text[:1000]


def _detect_json_container_extension(
    text: str,
    *,
    truncated: bool,
    path: str,
) -> str | None:
    if not text.startswith(("{", "[")):
        return None
    try:
        _, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as error:
        if not truncated:
            return _INVALID_JSON_SNIFF
        if error.msg == "Unterminated string starting at" or error.pos >= len(text) - 8:
            in_string, escaped = _json_string_state(text)
            validation = _validate_json_file(path)
            if validation is True:
                return "json"
            tail_extension = _detect_json_tail_extension(
                path,
                in_string=in_string,
                escaped=escaped,
            )
            if tail_extension:
                return tail_extension
            return _INVALID_JSON_SNIFF if validation is False else _UNKNOWN_JSON_SNIFF
        return _detect_json_tail_extension(path) or _INVALID_JSON_SNIFF
    else:
        if text[end:].strip():
            return _INVALID_JSON_SNIFF
        if not truncated:
            return "json"
        validation = _validate_json_file(path)
        if validation is True:
            return "json"
        tail_extension = _detect_json_tail_extension(path)
        if tail_extension:
            return tail_extension
        return _INVALID_JSON_SNIFF if validation is False else _UNKNOWN_JSON_SNIFF


def _validate_json_file(path: str) -> bool | None:
    """Validate bounded JSON files; return None when validation is skipped.

    Larger files keep their declared text identity instead of making an
    unbounded allocation solely for type sniffing.
    """
    try:
        if os.path.getsize(path) > _MAX_JSON_VALIDATION_BYTES:
            return None
        with open(path, encoding="utf-8") as f:
            json.load(f)
    except OSError:
        return None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    except (ValueError, RecursionError):
        return None
    return True


def _json_string_state(text: str) -> tuple[bool, bool]:
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if ord(char) < 0x20:
                in_string = False
                escaped = False
            elif escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
    return in_string, escaped


def _detect_json_tail_extension(
    path: str,
    *,
    in_string: bool | None = None,
    escaped: bool = False,
) -> str | None:
    outside_sample = bytearray()
    try:
        with open(path, "rb") as f:
            f.seek(_TEXT_SNIFF_BYTES)
            chunk = f.read(_TEXT_SNIFF_BYTES)
            if in_string is None:
                outside_sample.extend(chunk)
            else:
                for byte in chunk:
                    if in_string:
                        if byte < 0x20:
                            in_string = False
                            escaped = False
                            outside_sample.append(byte)
                        elif escaped:
                            escaped = False
                        elif byte == ord("\\"):
                            escaped = True
                        elif byte == ord('"'):
                            in_string = False
                    elif byte == ord('"'):
                        in_string = True
                    else:
                        outside_sample.append(byte)
    except OSError:
        return None
    tail_text = bytes(outside_sample).decode("utf-8", errors="ignore").lstrip()
    if not tail_text:
        return None
    if _looks_like_markdown(tail_text):
        return "md"
    return _INVALID_JSON_SNIFF if in_string is None else None
