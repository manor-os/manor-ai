"""HEIF detection shared by upload validation, vision and file conversion."""
from __future__ import annotations


def heif_mime_from_header(header: bytes) -> str | None:
    if len(header) < 16 or header[4:8] != b"ftyp":
        return None
    size = int.from_bytes(header[:4], "big")
    if size < 16 or size % 4:
        return None
    brands = {header[8:12]} | {
        header[offset:offset + 4]
        for offset in range(16, min(size, len(header), 256) - 3, 4)
    }
    if brands & {b"avif", b"avis"}:
        return None
    if brands & {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"}:
        return "image/heic"
    if brands & {b"mif1", b"msf1"}:
        return "image/heif"
    return None


def register_heif_decoder() -> None:
    from pillow_heif import register_heif_opener

    register_heif_opener(thumbnails=False)
