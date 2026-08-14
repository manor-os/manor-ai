from __future__ import annotations

import json
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
    TextStringObject,
)

from packages.core.ai.skills.pdf.scripts.fill_fillable_fields import fill_pdf_fields


def _write_text_form(path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    font_ref = writer._add_object(font)
    field = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject("full_name"),
            NameObject("/V"): TextStringObject(""),
            NameObject("/Rect"): RectangleObject([72, 700, 300, 730]),
            NameObject("/F"): NumberObject(4),
            NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
        }
    )
    field_ref = writer._add_object(field)
    page[NameObject("/Annots")] = ArrayObject([field_ref])
    acroform = DictionaryObject(
        {
            NameObject("/Fields"): ArrayObject([field_ref]),
            NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
            NameObject("/DR"): DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/Helv"): font_ref})}
            ),
        }
    )
    writer.root_object[NameObject("/AcroForm")] = writer._add_object(acroform)
    with path.open("wb") as stream:
        writer.write(stream)


def _write_values(path: Path) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "field_id": "full_name",
                    "description": "Applicant full name",
                    "page": 1,
                    "value": "Ada Lovelace",
                }
            ]
        ),
        encoding="utf-8",
    )


def test_fill_pdf_fields_preserves_interactivity_and_validates_appearances(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    values = tmp_path / "values.json"
    output = tmp_path / "output" / "filled.pdf"
    _write_text_form(source)
    _write_values(values)
    source_bytes = source.read_bytes()

    result = fill_pdf_fields(str(source), str(values), str(output))

    assert result == {
        "output": str(output.resolve()),
        "fields_updated": 1,
        "flattened": False,
        "validated": True,
    }
    assert source.read_bytes() == source_bytes
    reader = PdfReader(output)
    assert reader.get_fields()["full_name"]["/V"] == "Ada Lovelace"
    widget = reader.pages[0]["/Annots"][0].get_object()
    assert widget["/V"] == "Ada Lovelace"
    assert widget["/AP"]["/N"] is not None
    need_appearances = reader.trailer["/Root"]["/AcroForm"].get("/NeedAppearances")
    assert need_appearances is None or getattr(need_appearances, "value", need_appearances) is False


def test_fill_pdf_fields_flatten_removes_widgets_and_acroform(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    values = tmp_path / "values.json"
    output = tmp_path / "filled-static.pdf"
    _write_text_form(source)
    _write_values(values)

    result = fill_pdf_fields(str(source), str(values), str(output), flatten=True)

    assert result["flattened"] is True
    reader = PdfReader(output)
    assert reader.get_fields() in (None, {})
    assert reader.trailer["/Root"].get("/AcroForm") is None
    assert all(
        annotation.get_object().get("/Subtype") != "/Widget"
        for page in reader.pages
        for annotation in (page.get("/Annots", ()) or ())
    )


def test_fill_pdf_fields_refuses_source_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    values = tmp_path / "values.json"
    _write_text_form(source)
    _write_values(values)

    with pytest.raises(ValueError, match="Input and output paths must differ"):
        fill_pdf_fields(str(source), str(values), str(source))
