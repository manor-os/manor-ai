"""Create editable OOXML copies of legacy Office files without mutating them."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO

from packages.core.services.office_process import office_conversion_file_limit


LEGACY_OFFICE_EDITABLE_CACHE_VERSION = "v1-exact-source-bounded"
LEGACY_OFFICE_EDITABLE_CACHE_MAX_VERSIONS = 2
LEGACY_OFFICE_EDITABLE_MAX_BYTES = 500 * 1024 * 1024
LEGACY_OFFICE_EDITABLE_MEMORY_SPOOL_BYTES = 1024 * 1024
_LEGACY_OFFICE_EDITABLE_CACHE_KEY_RE = re.compile(r"^[0-9a-f]{16}$")


@dataclass(frozen=True)
class EditableOfficeFile:
    handle: BinaryIO
    size: int
    filename: str
    mime_type: str


class OfficeFileFamily(str, Enum):
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"


class OfficeFileFormat(str, Enum):
    DOCX = "docx"
    DOC = "doc"
    WPS = "wps"
    XLSX = "xlsx"
    XLS = "xls"
    ET = "et"
    PPTX = "pptx"
    PPT = "ppt"
    DPS = "dps"

    @property
    def suffix(self) -> str:
        return f".{self.value}"

    @property
    def is_editable_ooxml(self) -> bool:
        return self in {OfficeFileFormat.DOCX, OfficeFileFormat.XLSX, OfficeFileFormat.PPTX}


@dataclass(frozen=True)
class OfficeConversionSpec:
    source_format: OfficeFileFormat
    target_format: OfficeFileFormat
    family: OfficeFileFamily
    mime_type: str

    def editable_filename(self, source_name: str) -> str:
        visible_name = os.path.basename(str(source_name or "").replace("\\", "/"))
        stem = Path(visible_name).stem or "document"
        return f"{stem}.{self.target_format.value}"


class OfficeConverterUnavailableError(RuntimeError):
    """Raised when the configured host cannot start LibreOffice."""


class OfficeConversionLimitError(RuntimeError):
    """Raised when a converted editable file exceeds its resource budget."""


def _close_editable_office_file(converted: EditableOfficeFile) -> None:
    converted.handle.close()


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

_OFFICE_FORMAT_FAMILIES = {
    OfficeFileFormat.DOCX: OfficeFileFamily.DOCUMENT,
    OfficeFileFormat.DOC: OfficeFileFamily.DOCUMENT,
    OfficeFileFormat.WPS: OfficeFileFamily.DOCUMENT,
    OfficeFileFormat.XLSX: OfficeFileFamily.SPREADSHEET,
    OfficeFileFormat.XLS: OfficeFileFamily.SPREADSHEET,
    OfficeFileFormat.ET: OfficeFileFamily.SPREADSHEET,
    OfficeFileFormat.PPTX: OfficeFileFamily.PRESENTATION,
    OfficeFileFormat.PPT: OfficeFileFamily.PRESENTATION,
    OfficeFileFormat.DPS: OfficeFileFamily.PRESENTATION,
}
_OFFICE_FORMATS_BY_MIME = {
    _DOCX_MIME: OfficeFileFormat.DOCX,
    "application/msword": OfficeFileFormat.DOC,
    _XLSX_MIME: OfficeFileFormat.XLSX,
    "application/vnd.ms-excel": OfficeFileFormat.XLS,
    _PPTX_MIME: OfficeFileFormat.PPTX,
    "application/vnd.ms-powerpoint": OfficeFileFormat.PPT,
}
_GENERIC_OFFICE_FAMILIES = {
    family.value: family
    for family in OfficeFileFamily
}
_CONVERSION_SPECS = {
    OfficeFileFormat.DOC: OfficeConversionSpec(
        OfficeFileFormat.DOC, OfficeFileFormat.DOCX, OfficeFileFamily.DOCUMENT, _DOCX_MIME,
    ),
    OfficeFileFormat.WPS: OfficeConversionSpec(
        OfficeFileFormat.WPS, OfficeFileFormat.DOCX, OfficeFileFamily.DOCUMENT, _DOCX_MIME,
    ),
    OfficeFileFormat.XLS: OfficeConversionSpec(
        OfficeFileFormat.XLS, OfficeFileFormat.XLSX, OfficeFileFamily.SPREADSHEET, _XLSX_MIME,
    ),
    OfficeFileFormat.ET: OfficeConversionSpec(
        OfficeFileFormat.ET, OfficeFileFormat.XLSX, OfficeFileFamily.SPREADSHEET, _XLSX_MIME,
    ),
    OfficeFileFormat.PPT: OfficeConversionSpec(
        OfficeFileFormat.PPT, OfficeFileFormat.PPTX, OfficeFileFamily.PRESENTATION, _PPTX_MIME,
    ),
    OfficeFileFormat.DPS: OfficeConversionSpec(
        OfficeFileFormat.DPS, OfficeFileFormat.PPTX, OfficeFileFamily.PRESENTATION, _PPTX_MIME,
    ),
}


class OfficeConversionFactory:
    """Resolve one conversion specification from durable metadata first."""

    @staticmethod
    def format_from_mime(value: str | None) -> OfficeFileFormat | None:
        mime = str(value or "").split(";", 1)[0].strip().lower()
        return _OFFICE_FORMATS_BY_MIME.get(mime)

    @classmethod
    def _format_from_value(cls, value: str | None) -> OfficeFileFormat | None:
        normalized = str(value or "").strip().lower().lstrip(".")
        try:
            return OfficeFileFormat(normalized)
        except ValueError:
            return cls.format_from_mime(normalized)

    @classmethod
    def resolve_format(
        cls,
        source_name: str,
        *,
        source_format: str | None = None,
        source_mime: str | None = None,
    ) -> OfficeFileFormat | None:
        normalized_format = str(source_format or "").strip().lower().lstrip(".")
        durable_format = cls._format_from_value(normalized_format)
        if durable_format is not None:
            return durable_format

        expected_family = _GENERIC_OFFICE_FAMILIES.get(normalized_format)
        if normalized_format and expected_family is None and normalized_format != "file":
            return None
        candidates = [
            cls.format_from_mime(source_mime),
            cls._format_from_value(Path(str(source_name or "")).suffix),
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            if expected_family is not None and _OFFICE_FORMAT_FAMILIES[candidate] is not expected_family:
                continue
            return candidate
        return None

    @classmethod
    def conversion_spec(
        cls,
        source_name: str,
        *,
        source_format: str | None = None,
        source_mime: str | None = None,
    ) -> OfficeConversionSpec | None:
        source = cls.resolve_format(
            source_name,
            source_format=source_format,
            source_mime=source_mime,
        )
        return _CONVERSION_SPECS.get(source) if source is not None else None


async def convert_legacy_office_for_editing(
    source_path: str,
    source_name: str,
    *,
    source_format: str | None = None,
    source_mime: str | None = None,
) -> EditableOfficeFile:
    """Return an editable OOXML copy while leaving ``source_path`` untouched."""
    from packages.core.config import get_settings
    from packages.core.services.slide_renderer import run_office_render_thread

    configured_root = Path(get_settings().MANOR_FS_ROOT).resolve()
    resolved_source = Path(source_path).resolve()
    if configured_root.is_dir() and resolved_source.is_relative_to(configured_root):
        admission_cache_dir = configured_root / "_global" / ".office-editable-cache" / "conversion"
    else:
        admission_cache_dir = resolved_source.parent / ".office-editable-cache" / "conversion"
    return await run_office_render_thread(
        str(admission_cache_dir),
        os.path.abspath(source_path),
        _convert_legacy_office_for_editing_sync,
        source_path,
        source_name,
        source_format,
        source_mime,
        release_result=_close_editable_office_file,
    )


async def convert_legacy_office_for_editing_cached(
    source_path: str,
    source_name: str,
    cache_dir: str,
    *,
    source_format: str | None = None,
    source_mime: str | None = None,
) -> EditableOfficeFile:
    """Return a bounded, content-addressed editable copy for preview rendering."""
    from packages.core.services.slide_renderer import run_office_render_thread

    return await run_office_render_thread(
        cache_dir,
        f"{os.path.abspath(cache_dir)}:{os.path.abspath(source_path)}",
        _convert_legacy_office_for_editing_cached_sync,
        source_path,
        source_name,
        cache_dir,
        source_format,
        source_mime,
        release_result=_close_editable_office_file,
    )


def _convert_legacy_office_for_editing_cached_sync(
    source_path: str,
    source_name: str,
    cache_dir: str,
    source_format: str | None = None,
    source_mime: str | None = None,
) -> EditableOfficeFile:
    spec = OfficeConversionFactory.conversion_spec(
        source_name,
        source_format=source_format,
        source_mime=source_mime,
    )
    if spec is None:
        raise ValueError("This Office format does not need legacy conversion")
    if not os.path.isfile(source_path):
        raise FileNotFoundError(source_path)

    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="manor-legacy-office-snapshot-") as temporary_directory:
        snapshot_path = Path(temporary_directory) / f"source{spec.source_format.suffix}"
        digest = hashlib.sha256()
        digest.update(
            (
                f"{LEGACY_OFFICE_EDITABLE_CACHE_VERSION}:"
                f"{spec.source_format.value}:{spec.target_format.value}"
            ).encode("utf-8"),
        )
        with open(source_path, "rb") as source, snapshot_path.open("xb") as snapshot:
            for chunk in iter(lambda: source.read(65_536), b""):
                digest.update(chunk)
                snapshot.write(chunk)
        version_dir = cache_root / digest.hexdigest()[:16]
        output_path = version_dir / f"editable.{spec.target_format.value}"
        if not output_path.is_file():
            converted = _convert_legacy_office_for_editing_sync(
                str(snapshot_path),
                source_name,
                source_format,
                source_mime,
            )
            version_dir.mkdir(parents=True, exist_ok=True)
            temporary_output_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=".editable-",
                    dir=version_dir,
                    delete=False,
                ) as temporary_output:
                    temporary_output_path = temporary_output.name
                    shutil.copyfileobj(
                        converted.handle,
                        temporary_output,
                        length=65_536,
                    )
                os.replace(temporary_output_path, output_path)
            finally:
                converted.handle.close()
                if temporary_output_path is not None:
                    try:
                        os.remove(temporary_output_path)
                    except FileNotFoundError:
                        pass
        try:
            os.utime(version_dir, None, follow_symlinks=False)
        except OSError:
            pass
        _prune_legacy_office_editable_cache(cache_root, version_dir)
        return _open_editable_office_file(
            output_path,
            filename=spec.editable_filename(source_name),
            mime_type=spec.mime_type,
        )


def _open_editable_office_file(
    path: Path,
    *,
    filename: str,
    mime_type: str,
) -> EditableOfficeFile:
    size = path.stat().st_size
    if size <= 0 or size > LEGACY_OFFICE_EDITABLE_MAX_BYTES:
        raise OfficeConversionLimitError(
            "Converted Office file exceeds the editable file size limit",
        )
    handle = path.open("rb")
    return EditableOfficeFile(
        handle=handle,
        size=size,
        filename=filename,
        mime_type=mime_type,
    )


def _spool_editable_office_file(
    path: Path,
    *,
    filename: str,
    mime_type: str,
) -> EditableOfficeFile:
    size = path.stat().st_size
    if size <= 0 or size > LEGACY_OFFICE_EDITABLE_MAX_BYTES:
        raise OfficeConversionLimitError(
            "Converted Office file exceeds the editable file size limit",
        )
    handle = tempfile.SpooledTemporaryFile(
        max_size=LEGACY_OFFICE_EDITABLE_MEMORY_SPOOL_BYTES,
        mode="w+b",
    )
    try:
        with path.open("rb") as source:
            shutil.copyfileobj(source, handle, length=65_536)
        handle.seek(0)
    except BaseException:
        handle.close()
        raise
    return EditableOfficeFile(
        handle=handle,
        size=size,
        filename=filename,
        mime_type=mime_type,
    )


def _prune_legacy_office_editable_cache(cache_root: Path, current_version: Path) -> None:
    try:
        versions = [
            child
            for child in cache_root.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and _LEGACY_OFFICE_EDITABLE_CACHE_KEY_RE.fullmatch(child.name)
        ]
        versions.sort(key=lambda child: child.stat().st_mtime_ns, reverse=True)
    except OSError:
        return
    kept = {current_version.name}
    for version in versions:
        if len(kept) >= LEGACY_OFFICE_EDITABLE_CACHE_MAX_VERSIONS:
            break
        kept.add(version.name)
    for version in versions:
        if version.name not in kept:
            shutil.rmtree(version, ignore_errors=True)


def _convert_legacy_office_for_editing_sync(
    source_path: str,
    source_name: str,
    source_format: str | None = None,
    source_mime: str | None = None,
) -> EditableOfficeFile:
    spec = OfficeConversionFactory.conversion_spec(
        source_name,
        source_format=source_format,
        source_mime=source_mime,
    )
    if spec is None:
        raise ValueError("This Office format does not need legacy conversion")
    if not os.path.isfile(source_path):
        raise FileNotFoundError(source_path)

    converter = shutil.which("soffice") or shutil.which("libreoffice")
    if not converter:
        raise OfficeConverterUnavailableError("LibreOffice is not installed on this server")

    target_extension = spec.target_format.value
    with tempfile.TemporaryDirectory() as temporary_directory:
        input_name = f"source{spec.source_format.suffix}"
        input_path = os.path.join(temporary_directory, input_name)
        shutil.copy2(source_path, input_path)
        profile_uri = (Path(temporary_directory).resolve() / "libreoffice-profile").as_uri()
        environment = os.environ.copy()
        environment["SAL_USE_VCLPLUGIN"] = "svp"
        environment["HOME"] = temporary_directory
        environment["XDG_CONFIG_HOME"] = os.path.join(temporary_directory, ".config")
        environment["XDG_CACHE_HOME"] = os.path.join(temporary_directory, ".cache")
        try:
            result = subprocess.run(
                [
                    converter,
                    f"-env:UserInstallation={profile_uri}",
                    "--headless",
                    "--convert-to",
                    target_extension,
                    "--outdir",
                    temporary_directory,
                    input_path,
                ],
                capture_output=True,
                text=True,
                env=environment,
                preexec_fn=office_conversion_file_limit(LEGACY_OFFICE_EDITABLE_MAX_BYTES),
                timeout=120,
            )
        except FileNotFoundError as exc:
            raise OfficeConverterUnavailableError("LibreOffice is not installed on this server") from exc
        output_path = os.path.join(temporary_directory, f"source.{target_extension}")
        if result.returncode != 0 or not os.path.isfile(output_path):
            detail = (result.stderr or result.stdout or "conversion produced no file").strip()[:500]
            raise RuntimeError(f"Office conversion failed: {detail}")
        return _spool_editable_office_file(
            Path(output_path),
            filename=spec.editable_filename(source_name),
            mime_type=spec.mime_type,
        )
