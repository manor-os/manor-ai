"""Bounded, source-preserving format conversion for new Knowledge artifacts."""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import tempfile
from pathlib import Path
from zipfile import ZipFile

from packages.core.contracts.file_engine import (
    FILE_CONVERSION_MAX_INPUT_BYTES,
    FILE_CONVERSION_MAX_OUTPUT_BYTES,
    FILE_CONVERSION_TARGETS,
    IMAGE_CONVERSION_TYPES,
)
from packages.core.services.office_editing import (
    OfficeConversionLimitError,
    OfficeConverterUnavailableError,
)
from packages.core.services.office_process import (
    office_conversion_file_limit,
    run_in_process_group,
)

IMAGE_CONVERSION_MAX_PIXELS = 40_000_000


async def convert_file(data: bytes, source_type: str, target_type: str) -> bytes:
    if source_type not in IMAGE_CONVERSION_TYPES:
        return await convert_office_file(data, source_type, target_type)
    from packages.core.config import get_settings
    from packages.core.services.slide_renderer import run_office_render_thread

    cache_dir = Path(get_settings().MANOR_FS_ROOT) / "_global" / ".office-editable-cache" / "conversion"
    identity = f"image:{source_type}:{target_type}:{hashlib.sha256(data).hexdigest()}"
    return await run_office_render_thread(
        str(cache_dir), identity, _convert_image_file_sync, data, source_type, target_type,
    )


def _convert_image_file_sync(data: bytes, source_type: str, target_type: str) -> bytes:
    import warnings

    from PIL import Image, ImageOps
    from packages.core.services.image_formats import heif_mime_from_header, register_heif_decoder

    if source_type not in IMAGE_CONVERSION_TYPES or target_type not in FILE_CONVERSION_TARGETS[source_type]:
        raise ValueError("Unsupported image conversion")
    if not data or len(data) > FILE_CONVERSION_MAX_INPUT_BYTES:
        raise OfficeConversionLimitError("Source must be non-empty and at most 50 MiB")
    if heif_mime_from_header(data[:256]):
        try:
            register_heif_decoder()
        except ImportError as exc:
            raise OfficeConverterUnavailableError("HEIF decoder is not installed on this server") from exc

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > IMAGE_CONVERSION_MAX_PIXELS:
                raise OfficeConversionLimitError("Image exceeds the 40 megapixel conversion limit")
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("Multi-frame image conversion would discard frames")
            image = ImageOps.exif_transpose(image)
            options = {}
            if image.info.get("icc_profile"):
                options["icc_profile"] = image.info["icc_profile"]
            if image.getexif():
                options["exif"] = image.getexif().tobytes()
            if target_type in {"jpg", "jpeg"}:
                if "A" in image.getbands() or "transparency" in image.info:
                    rgba = image.convert("RGBA")
                    image = Image.new("RGB", image.size, "white")
                    image.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    image = image.convert("RGB")
                options["quality"] = 95
            elif image.mode not in {"RGB", "RGBA", "L", "LA", "P", "1", "I;16"}:
                image = image.convert("RGB")
            if target_type == "webp":
                options["lossless"] = True
            output = io.BytesIO()
            image.save(output, format={"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}[target_type], **options)
            converted = output.getvalue()
    if not 0 < len(converted) <= FILE_CONVERSION_MAX_OUTPUT_BYTES:
        raise OfficeConversionLimitError("Converted file exceeds the 100 MiB output limit")
    return converted


async def convert_office_file(data: bytes, source_type: str, target_type: str) -> bytes:
    from packages.core.config import get_settings
    from packages.core.services.slide_renderer import run_office_render_thread

    # Share admission/cancellation handling with previews and legacy conversion.
    cache_dir = Path(get_settings().MANOR_FS_ROOT) / "_global" / ".office-editable-cache" / "conversion"
    identity = f"{source_type}:{target_type}:{hashlib.sha256(data).hexdigest()}"
    return await run_office_render_thread(
        str(cache_dir), identity, _convert_office_file_sync, data, source_type, target_type,
    )


def _convert_office_file_sync(data: bytes, source_type: str, target_type: str) -> bytes:
    if target_type not in FILE_CONVERSION_TARGETS.get(source_type, ()):
        raise ValueError("Unsupported file conversion")
    if not data or len(data) > FILE_CONVERSION_MAX_INPUT_BYTES:
        raise OfficeConversionLimitError("Source must be non-empty and at most 50 MiB")
    converter = shutil.which("soffice") or shutil.which("libreoffice")
    if not converter:
        raise OfficeConverterUnavailableError("LibreOffice is not installed on this server")

    with tempfile.TemporaryDirectory(prefix="manor-file-conversion-") as directory:
        root = Path(directory)
        source = root / f"source.{source_type}"
        source.write_bytes(data)
        profile = root / "profile"
        profile.mkdir()
        # A fresh profile disables macros, active content and untrusted links.
        (profile / "user").mkdir()
        (profile / "user" / "registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
            '<prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>'
            '<prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>'
            '</item></oor:items>', encoding="utf-8",
        )
        environment = {**os.environ, "SAL_USE_VCLPLUGIN": "svp", "XDG_CACHE_HOME": str(root / "cache")}
        try:
            result = run_in_process_group(
                [converter, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
                 "--convert-to", target_type, "--outdir", directory, str(source)],
                capture_output=True, env=environment, timeout=120,
                preexec_fn=office_conversion_file_limit(FILE_CONVERSION_MAX_OUTPUT_BYTES),
            )
        except FileNotFoundError as exc:
            raise OfficeConverterUnavailableError("LibreOffice is not installed on this server") from exc
        output = root / f"source.{target_type}"
        if result.returncode != 0 or not output.is_file():
            # Do not echo document-derived process output into the Agent context.
            raise ValueError("Office conversion failed; the source may be invalid, encrypted or unsupported")
        if not 0 < output.stat().st_size <= FILE_CONVERSION_MAX_OUTPUT_BYTES:
            raise OfficeConversionLimitError("Converted file exceeds the 100 MiB output limit")
        converted = output.read_bytes()
        if target_type == "pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(converted))
            if reader.is_encrypted or not reader.pages:
                raise ValueError("Conversion did not produce a readable PDF")
        else:
            member = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml"}[target_type]
            with ZipFile(io.BytesIO(converted)) as archive:
                if member not in archive.namelist():
                    raise ValueError("Conversion did not produce the requested Office format")
        return converted
