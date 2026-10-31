"""Remove location and device metadata from still photos before they are stored.

Phone and browser photos often carry GPS coordinates, camera serials, and XMP
or IPTC text. ``upload_security`` validates the bytes; this module rewrites the
supported still-image types so the stored file keeps only its pixels and the
colour profile needed to display them correctly. The EXIF orientation is
applied to the pixels first, so the photo still displays upright.

Other file types are returned untouched. A supported image that cannot be
rewritten raises ``ImageMetadataStripError`` so the upload fails closed instead
of storing the original bytes with their metadata.
"""
from __future__ import annotations

import hashlib
import io

from packages.core.services.image_formats import heif_mime_from_header, register_heif_decoder
from packages.core.services.upload_security import UploadSecurityError

# Still images that phones and browsers commonly upload. GIF, BMP, TIFF, AVIF,
# and documents are outside this policy and keep their original bytes.
STRIPPED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"})

# Above a 48 MP phone capture, below Pillow's decompression-bomb warning
# threshold, so one request cannot force an unbounded decode.
MAX_PIXELS = 80_000_000

# Pillow writes EXIF, XMP, and IPTC only when they are passed explicitly, so
# omitting them here drops location and camera metadata. Lossy formats get an
# explicit quality because Pillow's default is lower than a phone photo; WebP
# is saved losslessly so its pixels are not degraded.
_SAVE_OPTIONS = {
    "JPEG": {"quality": 95},
    "PNG": {},
    "WEBP": {"lossless": True},
    "HEIF": {"quality": 90},
}


class ImageMetadataStripError(UploadSecurityError):
    """A supported photo could not be cleaned, so it must not be stored as-is."""

    code = "upload.image_metadata_unavailable"

    def __init__(self) -> None:
        super().__init__(
            "This photo couldn't be prepared safely, so it wasn't added. Try exporting it again.",
            status_code=422,
        )


def strip_image_metadata(content: bytes, *, media_type: str) -> bytes | None:
    """Return a metadata-free encoding of a supported still image.

    ``None`` means the media type is outside the photo policy and the caller
    keeps the original bytes. Any failure for a supported type raises
    ``ImageMetadataStripError``.
    """
    if media_type not in STRIPPED_MEDIA_TYPES:
        return None
    try:
        return _rewrite(content)
    except ImageMetadataStripError:
        raise
    except Exception as exc:
        raise ImageMetadataStripError() from exc


def strip_image_metadata_path(path: str, *, media_type: str) -> tuple[int, str] | None:
    """Rewrite a private upload file in place and return its ``(size, sha256)``.

    Returns ``None`` when the media type is not a supported photo, leaving the
    file untouched. The caller must hash and size the file from the returned
    values, not from the bytes it received before the rewrite.
    """
    if media_type not in STRIPPED_MEDIA_TYPES:
        return None
    with open(path, "rb") as source:
        cleaned = strip_image_metadata(source.read(), media_type=media_type)
    if cleaned is None:
        return None
    with open(path, "wb") as target:
        target.write(cleaned)
    return len(cleaned), hashlib.sha256(cleaned).hexdigest()


def _rewrite(content: bytes) -> bytes:
    from PIL import Image, ImageOps

    if heif_mime_from_header(content[:256]):
        register_heif_decoder()
    with Image.open(io.BytesIO(content)) as source:
        save_options = _SAVE_OPTIONS.get(source.format or "")
        if save_options is None or getattr(source, "n_frames", 1) != 1:
            # Re-encoding an animation or multi-frame image would drop frames.
            raise ImageMetadataStripError()
        if source.width * source.height > MAX_PIXELS:
            raise ImageMetadataStripError()
        options = dict(save_options)
        if source.info.get("icc_profile"):
            options["icc_profile"] = source.info["icc_profile"]
        if source.format == "HEIF" and source.info.get("nclx_profile"):
            options["nclx_profile"] = source.info["nclx_profile"]
        image = ImageOps.exif_transpose(source)
        # exif_transpose carries the source EXIF over in ``info``, and the HEIF
        # encoder writes whatever it finds there. Keep only the transparency key
        # that palette PNGs need; colour profiles are passed explicitly above.
        image.info = {key: value for key, value in image.info.items() if key == "transparency"}
        output = io.BytesIO()
        image.save(output, format=source.format, **options)
    return output.getvalue()
