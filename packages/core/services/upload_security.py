"""Security gate for user-controlled uploads.

The gate combines a product allowlist, signature checks for common binary
formats, executable-file rejection, and an optional ClamAV INSTREAM scan.
Cloud deployments require the AV scanner and fail closed when it is missing.
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import socket
import struct
from pathlib import Path
from typing import BinaryIO

from packages.core.config import get_settings
from packages.core.services.image_formats import heif_mime_from_header


class UploadSecurityError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# Product content types. Active web content is allowed for code/website work,
# but every filesystem response forces those extensions to download under a
# sandbox CSP instead of executing them in the authenticated app origin.
ALLOWED_UPLOAD_EXTENSIONS = frozenset({
    ".aac", ".ai", ".avif", ".avi", ".bmp", ".c", ".cpp", ".css",
    ".csv", ".doc", ".docx", ".dps", ".et", ".flac", ".gif", ".go",
    ".heic", ".heif", ".htm", ".html", ".java", ".jpeg", ".jpg",
    ".js", ".json", ".jsonl", ".jsx", ".m4a", ".m4v", ".md", ".mjs",
    ".mkv", ".mov", ".mp3", ".mp4", ".odp", ".ods", ".odt", ".ogg",
    ".opus", ".pdf", ".png", ".ppt", ".pptx", ".py", ".rb", ".rs",
    ".rtf", ".scss", ".sql", ".srt", ".svg", ".swift", ".tar", ".text",
    ".tif", ".tiff", ".toml", ".ts", ".tsv", ".tsx", ".txt", ".wav",
    ".webm", ".webp", ".wps", ".xls", ".xlsx", ".xml", ".xhtml",
    ".yaml", ".yml", ".zip",
})

_TEXT_EXTENSIONS = frozenset({
    ".c", ".cpp", ".css", ".csv", ".go", ".htm", ".html", ".java",
    ".js", ".json", ".jsonl", ".jsx", ".md", ".mjs", ".py", ".rb",
    ".rs", ".scss", ".sql", ".srt", ".swift", ".text", ".toml", ".ts", ".tsv",
    ".tsx", ".txt", ".xml", ".xhtml", ".yaml", ".yml",
})
_ZIP_EXTENSIONS = frozenset({
    ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".zip",
})
_IMAGE_SIGNATURES = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
    ".bmp": (b"BM",),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
}
_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
_EXECUTABLE_SIGNATURES = (
    b"MZ",                    # Windows PE
    b"\x7fELF",               # Linux ELF
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",  # Mach-O 32
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",  # Mach-O 64
    b"\xca\xfe\xba\xbe",    # Java class / Mach-O universal
)


def _normalise_extension(filename: str | None) -> str:
    safe_name = os.path.basename(str(filename or "").replace("\\", "/"))
    return Path(safe_name).suffix.lower()


def validate_upload_header(
    *,
    filename: str | None,
    header: bytes,
    declared_content_type: str | None = None,
    allowed_extensions: set[str] | frozenset[str] | None = None,
) -> str:
    """Validate an upload header and return a server-derived media type."""
    extension = _normalise_extension(filename)
    allowlist = allowed_extensions or ALLOWED_UPLOAD_EXTENSIONS
    if not extension or extension not in allowlist:
        raise UploadSecurityError(f"Unsupported upload file type: {extension or '(none)'}")
    if any(header.startswith(signature) for signature in _EXECUTABLE_SIGNATURES):
        raise UploadSecurityError("Executable files are not accepted")

    # Some phone exports retain HEIC bytes under a .png/.jpg filename. Detect
    # that specific image container; the full bytes still go through ClamAV.
    heif_mime = heif_mime_from_header(header)
    if heif_mime and extension in {*_IMAGE_SIGNATURES, ".heic", ".heif"}:
        return heif_mime
    if extension in {".heic", ".heif"}:
        raise UploadSecurityError("File content does not match its HEIF extension")

    if extension in _ZIP_EXTENSIONS and not header.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        raise UploadSecurityError("File content does not match its archive/document extension")
    if extension == ".pdf" and not header.startswith(b"%PDF-"):
        raise UploadSecurityError("File content does not match the PDF extension")
    expected_signatures = _IMAGE_SIGNATURES.get(extension)
    if expected_signatures and not any(header.startswith(sig) for sig in expected_signatures):
        raise UploadSecurityError("File content does not match its image extension")
    if extension == ".webp" and (len(header) < 12 or header[8:12] != b"WEBP"):
        raise UploadSecurityError("File content does not match the WebP extension")
    if extension in _TEXT_EXTENSIONS and b"\x00" in header:
        raise UploadSecurityError("Text uploads must not contain binary NUL bytes")

    inferred = _IMAGE_MIME_TYPES.get(extension) or mimetypes.guess_type(f"file{extension}")[0] or "application/octet-stream"
    declared = (declared_content_type or "").split(";", 1)[0].strip().lower()
    if declared.startswith("image/") and not inferred.startswith("image/"):
        raise UploadSecurityError("Declared image type does not match the filename")
    return inferred


# Every upload is virus-scanned; when the scan cannot finish, the file is
# fine to send again, so the person is told exactly that.
SCAN_UNAVAILABLE_MESSAGE = "The virus check didn't finish, so the file wasn't added. Try uploading it again."


def _av_required() -> bool:
    if get_settings().DEPLOYMENT_MODE.strip().lower() == "cloud":
        return True
    return os.getenv("UPLOAD_AV_REQUIRED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _clamav_scan(stream: BinaryIO) -> None:
    host = os.getenv("CLAMAV_HOST", "").strip()
    required = _av_required()
    if not host:
        if required:
            raise UploadSecurityError(SCAN_UNAVAILABLE_MESSAGE, status_code=503)
        return

    port = int(os.getenv("CLAMAV_PORT", "3310"))
    timeout = float(os.getenv("CLAMAV_TIMEOUT_SECONDS", "300"))
    try:
        with socket.create_connection((host, port), timeout=timeout) as client:
            client.settimeout(timeout)
            client.sendall(b"zINSTREAM\x00")
            while chunk := stream.read(1024 * 256):
                client.sendall(struct.pack(">I", len(chunk)))
                client.sendall(chunk)
            client.sendall(struct.pack(">I", 0))
            response = client.recv(4096).decode("utf-8", errors="replace").strip("\x00\r\n")
    except UploadSecurityError:
        raise
    except Exception as exc:
        if required:
            raise UploadSecurityError(SCAN_UNAVAILABLE_MESSAGE, status_code=503) from exc
        return

    if response.endswith(" FOUND"):
        raise UploadSecurityError("Upload rejected by malware scanner")
    if not response.endswith(" OK"):
        raise UploadSecurityError(SCAN_UNAVAILABLE_MESSAGE, status_code=503)


async def inspect_upload_content(
    content: bytes,
    *,
    filename: str | None,
    declared_content_type: str | None = None,
    allowed_extensions: set[str] | frozenset[str] | None = None,
) -> str:
    import io

    media_type = validate_upload_header(
        filename=filename,
        header=content[:8192],
        declared_content_type=declared_content_type,
        allowed_extensions=allowed_extensions,
    )
    await asyncio.to_thread(_clamav_scan, io.BytesIO(content))
    return media_type


async def inspect_upload_path(
    path: str,
    *,
    filename: str | None,
    declared_content_type: str | None = None,
    allowed_extensions: set[str] | frozenset[str] | None = None,
) -> str:
    with open(path, "rb") as source:
        header = source.read(8192)
    media_type = validate_upload_header(
        filename=filename,
        header=header,
        declared_content_type=declared_content_type,
        allowed_extensions=allowed_extensions,
    )

    def _scan_path() -> None:
        with open(path, "rb") as source:
            _clamav_scan(source)

    await asyncio.to_thread(_scan_path)
    return media_type


async def inspect_upload_stream(
    source: BinaryIO,
    *,
    filename: str | None,
    declared_content_type: str | None = None,
    allowed_extensions: set[str] | frozenset[str] | None = None,
) -> str:
    """Scan a private, seekable upload without materializing its full bytes."""
    def _inspect() -> str:
        try:
            source.seek(0)
            media_type = validate_upload_header(
                filename=filename,
                header=source.read(8192),
                declared_content_type=declared_content_type,
                allowed_extensions=allowed_extensions,
            )
            source.seek(0)
            _clamav_scan(source)
            return media_type
        finally:
            source.seek(0)

    return await asyncio.to_thread(_inspect)
