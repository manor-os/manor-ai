import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.services.office_editing import (
    EditableOfficeFile,
    OfficeConversionFactory,
    OfficeConversionLimitError,
    OfficeConverterUnavailableError,
    OfficeFileFormat,
    convert_legacy_office_for_editing,
    convert_legacy_office_for_editing_cached,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_name", "target_extension", "target_mime"),
    [
        (
            "legacy.doc",
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            "legacy.wps",
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            "legacy.xls",
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        (
            "legacy.et",
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        (
            "legacy.ppt",
            "pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        (
            "legacy.dps",
            "pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
    ],
)
async def test_legacy_office_conversion_returns_editable_copy_without_mutating_source(
    tmp_path,
    monkeypatch,
    source_name,
    target_extension,
    target_mime,
):
    source = tmp_path / "stored-without-extension"
    original = b"legacy-office-content"
    source.write_bytes(original)

    def fake_run(args, **kwargs):
        assert args[0] == "soffice"
        assert callable(kwargs["preexec_fn"])
        assert args[args.index("--convert-to") + 1] == target_extension
        input_path = Path(args[-1])
        assert input_path.suffix == Path(source_name).suffix
        assert input_path.read_bytes() == original
        output_path = Path(args[args.index("--outdir") + 1]) / f"source.{target_extension}"
        output_path.write_bytes(b"PK\x03\x04editable-ooxml")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "packages.core.services.office_editing.shutil.which",
        lambda executable: "soffice" if executable == "soffice" else None,
    )
    monkeypatch.setattr("packages.core.services.office_editing.subprocess.run", fake_run)

    converted = await convert_legacy_office_for_editing(str(source), source_name)

    assert source.read_bytes() == original
    assert converted.filename == f"legacy.{target_extension}"
    assert converted.mime_type == target_mime
    try:
        assert converted.handle.read().startswith(b"PK")
    finally:
        converted.handle.close()


def test_conversion_factory_prefers_durable_legacy_format_after_rename():
    spec = OfficeConversionFactory.conversion_spec(
        "renamed-deck.json",
        source_format="ppt",
        source_mime="application/json",
    )

    assert spec is not None
    assert spec.source_format is OfficeFileFormat.PPT
    assert spec.target_format is OfficeFileFormat.PPTX
    assert spec.editable_filename("renamed-deck.json") == "renamed-deck.pptx"
    assert OfficeConversionFactory.conversion_spec(
        "misleading-deck.ppt",
        source_format="json",
        source_mime="application/vnd.ms-powerpoint",
    ) is None


@pytest.mark.parametrize(
    ("source_name", "source_format", "source_mime", "expected"),
    [
        ("renamed.bin", "docx", "application/octet-stream", OfficeFileFormat.DOCX),
        ("renamed.bin", "xlsx", "application/octet-stream", OfficeFileFormat.XLSX),
        ("renamed.bin", "pptx", "application/octet-stream", OfficeFileFormat.PPTX),
        ("renamed.bin", "file", "application/msword", OfficeFileFormat.DOC),
        ("renamed.bin", "file", "application/octet-stream", None),
    ],
)
def test_conversion_factory_resolves_all_office_formats_from_durable_metadata(
    source_name,
    source_format,
    source_mime,
    expected,
):
    assert OfficeConversionFactory.resolve_format(
        source_name,
        source_format=source_format,
        source_mime=source_mime,
    ) is expected


@pytest.mark.asyncio
async def test_conversion_reports_missing_libreoffice_as_service_dependency(tmp_path, monkeypatch):
    source = tmp_path / "legacy.ppt"
    source.write_bytes(b"legacy")
    monkeypatch.setattr("packages.core.services.office_editing.shutil.which", lambda _executable: None)

    with pytest.raises(OfficeConverterUnavailableError, match="LibreOffice"):
        await convert_legacy_office_for_editing(str(source), source.name)


@pytest.mark.asyncio
async def test_legacy_office_conversion_rejects_oversized_editable_output(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "legacy.ppt"
    source.write_bytes(b"legacy")

    def fake_run(args, **_kwargs):
        output_dir = Path(args[args.index("--outdir") + 1])
        (output_dir / "source.pptx").write_bytes(b"oversized")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "packages.core.services.office_editing.LEGACY_OFFICE_EDITABLE_MAX_BYTES",
        4,
    )
    monkeypatch.setattr(
        "packages.core.services.office_editing.shutil.which",
        lambda executable: "soffice" if executable == "soffice" else None,
    )
    monkeypatch.setattr(
        "packages.core.services.office_editing.subprocess.run",
        fake_run,
    )

    with pytest.raises(OfficeConversionLimitError, match="size limit"):
        await convert_legacy_office_for_editing(str(source), source.name)


@pytest.mark.asyncio
async def test_cached_legacy_conversion_reuses_and_bounds_exact_source_versions(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "legacy.ppt"
    cache_dir = tmp_path / "cache"
    conversion_inputs: list[bytes] = []

    def fake_convert(source_path, source_name, _source_format, _source_mime):
        content = Path(source_path).read_bytes()
        conversion_inputs.append(content)
        editable_content = b"PK\x03\x04" + content
        return EditableOfficeFile(
            handle=io.BytesIO(editable_content),
            size=len(editable_content),
            filename=f"{Path(source_name).stem}.pptx",
            mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )

    monkeypatch.setattr(
        "packages.core.services.office_editing._convert_legacy_office_for_editing_sync",
        fake_convert,
    )

    source.write_bytes(b"version-one")
    first = await convert_legacy_office_for_editing_cached(
        str(source),
        source.name,
        str(cache_dir),
    )
    repeated = await convert_legacy_office_for_editing_cached(
        str(source),
        source.name,
        str(cache_dir),
    )
    source.write_bytes(b"version-two")
    second = await convert_legacy_office_for_editing_cached(
        str(source),
        source.name,
        str(cache_dir),
    )
    source.write_bytes(b"version-three")
    latest = await convert_legacy_office_for_editing_cached(
        str(source),
        source.name,
        str(cache_dir),
    )

    try:
        assert first.handle.read() == b"PK\x03\x04version-one"
        assert repeated.handle.read() == b"PK\x03\x04version-one"
        assert latest.handle.read() == b"PK\x03\x04version-three"
    finally:
        first.handle.close()
        repeated.handle.close()
        second.handle.close()
        latest.handle.close()
    assert conversion_inputs == [b"version-one", b"version-two", b"version-three"]
    assert len([path for path in cache_dir.iterdir() if path.is_dir()]) == 2
