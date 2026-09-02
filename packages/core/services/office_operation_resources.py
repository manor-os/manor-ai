"""Read exact Knowledge snapshots used as native Office operation resources."""

from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageOps, UnidentifiedImageError

from packages.core.contracts.file_engine import IMAGE_FILE_TYPES, file_type_from_path
from packages.core.services.entity_fs import (
    EntityFilesystemStaleWriteError,
    open_entity_file_snapshot,
)

OFFICE_OPERATION_IMAGE_MAX_BYTES = 12_000_000
OFFICE_OPERATION_IMAGE_MAX_PIXELS = 80_000_000
OFFICE_OPERATION_IMAGE_MAX_DECODED_BYTES = 64_000_000
OFFICE_OPERATION_RESOURCES_MAX_BYTES = 128_000_000


def operation_resource_key(source: dict[str, str]) -> tuple[str, str]:
    return source["path"], source["expected_sha256"]


def _validated_image_bytes(path: str, data: bytes) -> bytes:
    if file_type_from_path(path) not in IMAGE_FILE_TYPES:
        raise ValueError("picture source must be a supported Knowledge image file")
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            image_format = str(image.format or "").upper()
            if width < 1 or height < 1 or width * height > OFFICE_OPERATION_IMAGE_MAX_PIXELS:
                raise ValueError("picture source dimensions are outside the supported range")
            decoded_bytes = width * height * max(4, len(image.getbands()))
            if decoded_bytes > OFFICE_OPERATION_IMAGE_MAX_DECODED_BYTES:
                raise ValueError(
                    f"decoded image exceeds {OFFICE_OPERATION_IMAGE_MAX_DECODED_BYTES} bytes",
                )
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("picture source is not a readable image") from exc
    if image_format in {"BMP", "GIF", "JPEG", "PNG", "TIFF"}:
        return data
    # OOXML does not natively support WebP/AVIF/ICO. Decode the exact approved
    # snapshot once and store an editable PNG rendition inside the package.
    try:
        with Image.open(io.BytesIO(data)) as image:
            rendered = ImageOps.exif_transpose(image)
            rendered.load()
            if rendered.mode not in {"RGB", "RGBA"}:
                rendered = rendered.convert("RGBA" if "transparency" in image.info else "RGB")
            output = io.BytesIO()
            rendered.save(output, "PNG", optimize=False)
            converted = output.getvalue()
            if len(converted) > OFFICE_OPERATION_IMAGE_MAX_DECODED_BYTES:
                raise ValueError(
                    f"converted image exceeds {OFFICE_OPERATION_IMAGE_MAX_DECODED_BYTES} bytes",
                )
            return converted
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("picture source cannot be converted to an Office image") from exc


def read_office_operation_resources(
    entity_id: str, resources: list[dict[str, str]],
) -> dict[tuple[str, str], bytes]:
    """Read resource bytes once without following aliases or accepting a stale hash."""
    result: dict[tuple[str, str], bytes] = {}
    total_bytes = 0
    for source in resources:
        key = operation_resource_key(source)
        with open_entity_file_snapshot(
            entity_id,
            source["path"],
            expected_content_sha256=source["expected_sha256"],
        ) as snapshot:
            with open(snapshot.descriptor_path, "rb") as stream:
                data = stream.read(OFFICE_OPERATION_IMAGE_MAX_BYTES + 1)
        if len(data) > OFFICE_OPERATION_IMAGE_MAX_BYTES:
            raise ValueError(
                f"picture source exceeds {OFFICE_OPERATION_IMAGE_MAX_BYTES} bytes",
            )
        if hashlib.sha256(data).hexdigest() != source["expected_sha256"]:
            raise EntityFilesystemStaleWriteError("Picture source changed while being read")
        validated = _validated_image_bytes(source["path"], data)
        total_bytes += len(validated)
        if total_bytes > OFFICE_OPERATION_RESOURCES_MAX_BYTES:
            raise ValueError(
                f"picture sources exceed {OFFICE_OPERATION_RESOURCES_MAX_BYTES} bytes in one operation batch",
            )
        result[key] = validated
    return result
