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


def sniff_image_type(data: bytes) -> str | None:
    """The image type downloaded bytes really are, whatever the URL or headers claim."""
    header = data[:256]
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    if header[:6] in {b"GIF87a", b"GIF89a"}:
        return "gif"
    if len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in {b"avif", b"avis"}:
        return "avif"
    heif = heif_mime_from_header(header)
    return "heic" if heif == "image/heic" else "heif" if heif else None


def register_heif_decoder() -> None:
    from pillow_heif import register_heif_opener

    register_heif_opener(thumbnails=False)
